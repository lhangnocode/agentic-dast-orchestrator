"""
HTTP probe: one plain GET per URL to learn its real status code and content type.

Used for URLs whose status is unknown (status 0), e.g. Client Map nodes and Firecrawl links.
It never follows redirects (the 3xx itself is the observation), never submits forms, and skips
URLs with a client-side fragment route (`/#/…`), which never reach the server.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger("orchestrator.probe")


class HttpProbe:
    def __init__(self, headers: Optional[Dict[str, str]] = None, cookies: Optional[Dict[str, str]] = None,
                 timeout_sec: float = 10, max_workers: int = 8, verify_tls: bool = False):
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})
        self.timeout_sec = timeout_sec
        self.max_workers = max(1, max_workers)
        self.verify_tls = verify_tls
        self.last_request_count = 0
        self.last_status = "ok"

    @staticmethod
    def probe_target(url: str) -> bool:
        """URLs worth probing: absolute http(s) without a client-side route fragment."""
        p = urlparse(url)
        return p.scheme in ("http", "https") and not p.fragment.startswith(("/", "!/"))

    def _probe(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            with requests.get(url, headers=self.headers, cookies=self.cookies, timeout=self.timeout_sec,
                              allow_redirects=False, stream=True, verify=self.verify_tls) as resp:
                return {
                    "url": url,
                    "method": "GET",
                    "status_code": resp.status_code,
                    "content_type": (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower(),
                    "source": "http_probe",
                }
        except requests.RequestException as e:
            logger.debug(f"[PROBE] {url}: {e}")
            return None

    def run(self, urls: List[str]) -> List[Dict[str, Any]]:
        self.last_status = "ok"
        targets = [u for u in dict.fromkeys(urls) if self.probe_target(u)]
        self.last_request_count = len(targets)
        if not targets:
            return []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(targets))) as pool:
            results = [r for r in pool.map(self._probe, targets) if r]
        failed = len(targets) - len(results)
        if failed == len(targets):
            self.last_status = "failed"
        logger.info(f"[PROBE] {len(targets)} URL(s) probed, {failed} without response.")
        return results
