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
import subprocess
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
        max_scan_time: int = 300,
        max_parameters: int = 100,
        scope: str = "domain",
    ):
        self.binary_path = binary_path
        self.session_db_path = Path(session_db_path)
        self.report_path = Path(report_path)
        self.log_path = Path(log_path)
        self.timeout = timeout
        self.max_scan_time = max_scan_time
        self.max_parameters = max_parameters
        self.scope = scope

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
            "--max-scan-time", str(self.max_scan_time),
            "--timeout", str(self.timeout),
            "-f", "json",
            "-o", str(self.report_path),
            "--log", str(self.log_path),
        ]

        if flush_session:
            cmd.append("--flush-session")

        # Inject seeds
        if seeds:
            for s in seeds:
                s_clean = s.strip()
                if s_clean:
                    cmd.extend(["-s", s_clean])

        # Inject cookies if provided
        if cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            cmd.extend(["-C", cookie_str])

        logger.info(f"[WAPITI] Executing crawl-only command: {' '.join(cmd[:6])} ...")

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            res = subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.max_scan_time + 30
            )
            if res.returncode != 0:
                logger.warning(f"[WAPITI] Returned code {res.returncode}: {res.stderr[:200]}")
        except subprocess.TimeoutExpired:
            logger.warning("[WAPITI] Execution exceeded max_scan_time timeout.")
        except Exception as e:
            logger.error(f"[WAPITI] Subprocess invocation failed: {e}")

        return self.extract_endpoints_from_db()

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
                status_code = resp_info.get("status_code", 200)

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
                    "source": "wapiti",
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
