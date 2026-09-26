"""
OWASP ZAP Wrapper for Traditional Spider and Ajax Spider.
Extracts discovered endpoints and raw HTTP messages via ZAPv2 API.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from zapv2 import ZAPv2

from normalization.patterns import is_static_asset

logger = logging.getLogger("orchestrator.zap")


class ZapWrapper:
    def __init__(self, api_url: str = "http://127.0.0.1:18080", api_key: str = "changeme"):
        self.api_url = api_url
        self.api_key = api_key
        self._client: Optional[ZAPv2] = None

    @property
    def client(self) -> ZAPv2:
        if self._client is None:
            proxies = {"http": self.api_url, "https": self.api_url}
            self._client = ZAPv2(
                proxies=proxies,
                apikey=self.api_key
            )
        return self._client

    def is_alive(self) -> bool:
        """Check if ZAP daemon is reachable and responding."""
        try:
            ver = self.client.core.version
            return bool(ver)
        except Exception as e:
            logger.warning(f"ZAP daemon connection failed: {e}")
            return False

    def run_traditional_spider(
        self,
        seeds: List[str],
        max_depth: int = 5,
        poll_interval: int = 3,
        timeout_sec: int = 180
    ) -> List[Dict[str, Any]]:
        """
        Runs the ZAP Traditional Spider against provided seed URLs.
        Collects all discovered HTTP requests from ZAP core messages.
        """
        if not self.is_alive():
            logger.warning("[ZAP] Daemon not reachable. Skipping spider.")
            return []

        logger.info(f"[ZAP] Starting Traditional Spider with {len(seeds)} seeds...")
        self.client.spider.set_option_max_depth(max_depth)

        for seed in seeds:
            try:
                scan_id = self.client.spider.scan(seed, recurse=True)
                if not str(scan_id).isdigit():
                    logger.warning(f"[ZAP] Spider returned non-numeric scan ID: {scan_id}")
                    continue

                start_time = time.time()
                while time.time() - start_time < timeout_sec:
                    status_raw = self.client.spider.status(scan_id)
                    if not str(status_raw).isdigit():
                        break
                    progress = int(status_raw)
                    logger.debug(f"[ZAP] Spider scan {scan_id} progress: {progress}%")
                    if progress >= 100:
                        break
                    time.sleep(poll_interval)

                self.client.spider.stop(scan_id)
            except Exception as e:
                logger.error(f"[ZAP] Error running spider on {seed}: {e}")

        return self.collect_messages()

    def run_ajax_spider(
        self,
        seeds: List[str],
        timeout_sec: int = 120,
        poll_interval: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Runs the ZAP Ajax Spider (headless browser) against seed URLs.
        Useful for uncovering client-side JavaScript routes and dynamic DOM events.
        """
        if not self.is_alive():
            logger.warning("[ZAP] Daemon not reachable. Skipping Ajax Spider.")
            return []

        logger.info(f"[ZAP] Starting Ajax Spider with {len(seeds)} seeds...")
        for seed in seeds:
            try:
                self.client.ajaxSpider.scan(seed)
                start_time = time.time()
                while time.time() - start_time < timeout_sec:
                    status = str(self.client.ajaxSpider.status).lower()
                    if callable(self.client.ajaxSpider.status):
                        status = str(self.client.ajaxSpider.status()).lower()
                    logger.debug(f"[ZAP] Ajax Spider status: {status}")
                    if status == "stopped":
                        break
                    time.sleep(poll_interval)

                try:
                    self.client.ajaxSpider.stop()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"[ZAP] Error running Ajax Spider on {seed}: {e}")

        return self.collect_messages()

    def collect_messages(self, page_size: int = 500) -> List[Dict[str, Any]]:
        """
        Retrieves all recorded HTTP requests from ZAP core messages.
        Filters out static asset noise and returns standardized request dicts.
        """
        discovered: List[Dict[str, Any]] = []
        if not self.is_alive():
            return discovered

        try:
            start = 0
            while True:
                chunk = self.client.core.messages(None, start, page_size)
                if isinstance(chunk, str):
                    try:
                        chunk = json.loads(chunk).get("messages", [])
                    except Exception:
                        chunk = []
                elif isinstance(chunk, dict):
                    chunk = chunk.get("messages", [])

                if not chunk:
                    break

                for msg in chunk:
                    req_header = msg.get("requestHeader", "") or ""
                    req_body = msg.get("requestBody", "") or ""
                    res_header = msg.get("responseHeader", "") or ""

                    # Parse request line
                    lines = req_header.splitlines()
                    if not lines:
                        continue
                    parts = lines[0].split()
                    if len(parts) < 2:
                        continue
                    method = parts[0].upper()
                    url = parts[1]

                    # Parse response status
                    status_code = 200
                    if res_header:
                        status_line = res_header.splitlines()[0] if res_header.splitlines() else ""
                        status_parts = status_line.split()
                        if len(status_parts) >= 2 and status_parts[1].isdigit():
                            status_code = int(status_parts[1])

                    # Ignore static assets
                    p = urlparse(url)
                    if is_static_asset(p.path):
                        continue

                    discovered.append({
                        "url": url,
                        "method": method,
                        "status_code": status_code,
                        "source": "zap",
                        "body": req_body
                    })

                if len(chunk) < page_size:
                    break
                start += page_size

        except Exception as e:
            logger.error(f"[ZAP] Error collecting messages: {e}")

        return discovered
