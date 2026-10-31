"""
Wapiti Crawl-Only Wrapper.
Executes Wapiti strictly with `-m ""` (Crawl-only, zero attack payloads).
Extracts discovered endpoints and form parameter trees directly from the
generated SQLite session database.
"""

import json
import logging
import os
import sqlite3
import re
import subprocess
import threading
import time
import urllib.parse as up
from pathlib import Path
from typing import Any, Dict, List, Optional

from normalization.patterns import is_static_asset

logger = logging.getLogger("orchestrator.wapiti")


class WapitiWrapper:
    def __init__(
        self,
        binary_path: str = "wapiti",
        session_db_path: str = "output/temp/wapiti/session.db",
        report_path: str = "output/temp/wapiti/report.json",
        log_path: str = "output/logs/wapiti.log",
        timeout: int = 20,
        max_scan_time: Optional[int] = 300,
        max_parameters: int = 100,
        scope: str = "domain",
        headless: str = "no",
        wait_sec: Optional[float] = None,
        depth: Optional[int] = None,
        max_links_per_page: Optional[int] = None,
        cookie_file: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
    ):
        """
        headless: "no" | "hidden" | "both". "hidden"/"both" drive a headless Firefox so
        JavaScript-rendered links and forms of SPAs are discovered (slower).
        wait_sec: seconds to wait for client-side rendering before analysing a page (headless only).
        cookie_file: JSON cookie file passed to Wapiti (-c), e.g. an exported authenticated session.
        headers / cookies: sent with every request (-H / -C), e.g. from orchestrator.auth.
        """
        self.binary_path = binary_path
        self.session_db_path = Path(session_db_path)
        self.report_path = Path(report_path)
        self.log_path = Path(log_path)
        self.timeout = timeout
        self.max_scan_time = max_scan_time
        self.max_parameters = max_parameters
        self.scope = scope
        self.headless = headless or "no"
        self.wait_sec = wait_sec
        self.depth = depth
        self.max_links_per_page = max_links_per_page
        self.cookie_file = cookie_file
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})
        self.last_request_count = 0
        self.last_status = "ok"  # ok | failed | timeout
        self.last_first_request_at: Optional[float] = None

        self.session_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def run_crawl_only(
        self,
        base_url: str,
        seeds: Optional[List[str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        flush_session: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Executes Wapiti crawl-only mode using subprocess (without shell=True).
        Extracts discovered HTTP endpoints and form structures from SQLite session DB.
        """
        self.last_status = "ok"
        if flush_session and self.session_db_path.exists():
            try:
                if self.session_db_path.is_dir():
                    import shutil
                    shutil.rmtree(self.session_db_path)
                else:
                    self.session_db_path.unlink()
            except Exception as e:
                logger.warning(f"Could not delete old session DB: {e}")

        # Build command array
        cmd = [
            self.binary_path,
            "-u", base_url,
            "-m", "",  # Crawl-only: strictly NO attack modules
            "--scope", self.scope,
            "--store-session", str(self.session_db_path),
            "--max-parameters", str(self.max_parameters),
            "--timeout", str(self.timeout),
            "-f", "json",
            "-o", str(self.report_path),
            "--log", str(self.log_path),
        ]

        if flush_session:
            cmd.append("--flush-session")
        if self.max_scan_time:
            cmd.extend(["--max-scan-time", str(self.max_scan_time)])
        if self.headless != "no":
            cmd.extend(["--headless", self.headless])
            if self.wait_sec:
                cmd.extend(["--wait", str(self.wait_sec)])
        if self.depth:
            cmd.extend(["-d", str(self.depth)])
        if self.max_links_per_page:
            cmd.extend(["--max-links-per-page", str(self.max_links_per_page)])
        if self.cookie_file:
            cmd.extend(["-c", self.cookie_file])

        # Inject seeds. Hash routes (/#/x) only make sense for the headless crawler.
        for seed in dict.fromkeys(s.strip() for s in (seeds or [])):
            if not seed or seed.rstrip("/") == base_url.rstrip("/"):
                continue
            if "#" in seed and self.headless == "no":
                continue
            cmd.extend(["-s", seed])

        # Authenticated session: headers and cookies on every request
        for name, value in self.headers.items():
            cmd.extend(["-H", f"{name}: {value}"])
        all_cookies = {**self.cookies, **(cookies or {})}
        if all_cookies:
            cmd.extend(["-C", "; ".join(f"{k}={v}" for k, v in all_cookies.items())])

        logger.info(f"[WAPITI] Executing crawl-only command: {' '.join(cmd[:6])} ...")

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        self.last_first_request_at = None
        try:
            returncode, stderr = self._run_streaming(cmd, env, self.max_scan_time + 60 if self.max_scan_time else None)
            if returncode != 0:
                logger.warning(f"[WAPITI] Returned code {returncode}: {stderr[:200]}")
        except subprocess.TimeoutExpired:
            self.last_status = "timeout"
            logger.warning("[WAPITI] Execution exceeded max_scan_time timeout.")
        except Exception as e:
            self.last_status = "failed"
            logger.error(f"[WAPITI] Subprocess invocation failed: {e}")

        return self.extract_endpoints_from_db()

    CRAWLED_LINE = re.compile(r"^\[\+\] [A-Z]+ https?://")

    def _run_streaming(self, cmd: List[str], env: Dict[str, str], timeout: Optional[float],
                       poll_sec: float = 0.1):
        """
        Runs Wapiti and times its first crawled resource. Wapiti writes the "[+] <METHOD> <url>" lines
        only to its --log file (live), so the file is watched from its size at launch, every `poll_sec`.
        """
        log = self.log_path
        offset = log.stat().st_size if log.exists() else 0
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        out: List[str] = []
        err: List[str] = []
        readers = [threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True),
                   threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)]
        for t in readers:
            t.start()

        def first_crawled_line() -> bool:
            try:
                with open(log, encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    return any(self.CRAWLED_LINE.match(line) for line in f)
            except OSError:
                return False

        deadline = time.time() + timeout if timeout else None
        try:
            while proc.poll() is None:
                if self.last_first_request_at is None and first_crawled_line():
                    self.last_first_request_at = time.time()
                if deadline and time.time() > deadline:
                    proc.kill()
                    proc.wait()
                    raise subprocess.TimeoutExpired(cmd, timeout)
                time.sleep(poll_sec)
        finally:
            for t in readers:
                t.join(5)
        return proc.returncode, "".join(err)

    def extract_endpoints_from_db(self) -> List[Dict[str, Any]]:
        """
        Extracts reconstructed requests from Wapiti's SQLite session database.
        """
        # Wapiti may create a directory or append an extension, check path
        db_file = self._find_sqlite_file(self.session_db_path)
        if not db_file or not db_file.is_file():
            logger.warning(f"[WAPITI] Session DB not found at {self.session_db_path}")
            return []

        discovered: List[Dict[str, Any]] = []
        try:
            conn = sqlite3.connect(str(db_file))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Query paths and responses
            cursor.execute("""
                SELECT path_id, path, method, enctype, response_id
                FROM paths
            """)
            paths_rows = [dict(r) for r in cursor.fetchall()]
            self.last_request_count = len(paths_rows)

            cursor.execute("""
                SELECT param_id, path_id, type, position, name, value1
                FROM params
            """)
            params_rows = [dict(r) for r in cursor.fetchall()]

            cursor.execute("""
                SELECT response_id, url, status_code
                FROM responses
            """)
            responses_by_id = {r["response_id"]: dict(r) for r in cursor.fetchall()}
            conn.close()

            # Group params by path_id
            params_by_path: Dict[int, List[Dict[str, Any]]] = {}
            for p in params_rows:
                pid = p["path_id"]
                params_by_path.setdefault(pid, []).append(p)

            for req in paths_rows:
                raw_path = req["path"]
                method = (req.get("method") or "GET").upper()
                pid = req["path_id"]
                resp_info = responses_by_id.get(req.get("response_id")) or {}
                status_code = resp_info.get("status_code") or 0

                # Reconstruct full URL with query parameters from params table if not already present
                path_params = params_by_path.get(pid, [])
                url = self._build_url_with_params(raw_path, method, path_params)

                p = up.urlparse(url)
                if is_static_asset(p.path):
                    continue

                discovered.append({
                    "url": url,
                    "method": method,
                    "status_code": status_code,
                    "source": "wapiti_form_crawl",
                    "params": [pr["name"] for pr in path_params if pr.get("name")]
                })

        except Exception as e:
            logger.error(f"[WAPITI] Failed to extract from session DB: {e}")

        logger.info(f"[WAPITI] Extracted {len(discovered)} endpoints from session DB.")
        return discovered

    def _find_sqlite_file(self, target: Path) -> Optional[Path]:
        if target.is_file():
            return target
        if target.is_dir():
            for candidate in target.glob("*.db"):
                return candidate
        # Check without .db or with .db
        alt = target.with_suffix(".db")
        if alt.is_file():
            return alt
        return None

    def _build_url_with_params(self, base_path: str, method: str, params: List[Dict[str, Any]]) -> str:
        """Appends GET query params to base_path if method is GET."""
        if not params or method != "GET":
            return base_path

        p = up.urlparse(base_path)
        existing_qs = up.parse_qs(p.query)

        for param in params:
            name = param.get("name")
            val = param.get("value1") or ""
            if name:
                existing_qs[name] = [val]

        new_query = up.urlencode(existing_qs, doseq=True)
        reconstructed = up.urlunparse((
            p.scheme,
            p.netloc,
            p.path,
            p.params,
            new_query,
            p.fragment
        ))
        return reconstructed
