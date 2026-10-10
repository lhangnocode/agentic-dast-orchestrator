"""
Firecrawl Cloud Agent Client.
Interfaces with the Firecrawl /v2/agent API to perform semantic browser navigation
(menus, dropdowns, modals, client-side routes) on the seeds the orchestrator selects.

In strategy 2 Firecrawl is a *targeted* tool: it is only invoked after the ZAP/Wapiti
bootstrap, on the frontier those tools could not open up (auth walls, SPA shells).
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()
logger = logging.getLogger("orchestrator.firecrawl")

LOCAL_HOSTS = {"localhost", "127.0.0.1"}


class DiscoveredLinksSchema(BaseModel):
    links: List[str] = Field(description="List of URLs/routes found while interacting with the page")
    actions_taken: List[str] = Field(description="Actions the AI performed (click, scroll, open menu...)")


AGENT_PROMPT_TEMPLATE = """
You are a web application route discovery assistant working on an authorized test target.
Goal: reveal as many distinct application routes and interaction points as possible
(navigation links, client-side routes, API calls triggered by the UI).

Start from these pages:
{seed_list}
{focus_block}{auth_block}
How to explore:
1. Open every navigation element: header, sidebar, footer, account/avatar menus, dropdowns,
   tabs, modals, pagination and filter controls.
2. For single-page applications, follow client-side routes (including /#/ routes) and wait
   for each view to render before reading its links.
3. Open forms and dialogs to reveal where they submit, but do not submit destructive actions
   (delete, logout, payment, account changes). Use harmless test data only, no attack payloads.
4. Stay on the target's own host.

Return ALL unique URLs/routes observed (with paths, query parameters and route segments)
and a short list of the actions taken.
"""


def build_agent_prompt(seeds: List[str], focus: str = "", auth_instructions: str = "") -> str:
    seed_list = "\n".join(f"- {s}" for s in seeds)
    focus_block = f"\nPriority for this run: {focus}\n" if focus else ""
    auth_block = f"\nSession / authentication instructions:\n{auth_instructions.strip()}\n" if auth_instructions.strip() else ""
    return AGENT_PROMPT_TEMPLATE.format(seed_list=seed_list, focus_block=focus_block, auth_block=auth_block)


class FirecrawlClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: str = "https://api.firecrawl.dev/v2",
        model: str = "spark-2",
        poll_timeout_seconds: int = 600,
        poll_interval_seconds: int = 10,
        public_tunnel_url: Optional[str] = None,
        target_base: str = "http://localhost/",
        auth_instructions: str = "",
        max_seeds: int = 5,
        fallback_routes_file: Optional[str] = None,
    ):
        """
        fallback_routes_file: optional JSON list of URLs replayed when Firecrawl cannot run
        (no API key / unreachable). Only for offline experiments; leave unset for real runs.
        """
        self.api_key = os.getenv("FIRECRAWL_API_KEY", "") if api_key is None else api_key
        self.api_url = api_url.rstrip("/")
        self.agent_endpoint = f"{self.api_url}/agent"
        self.model = model
        self.poll_timeout_seconds = poll_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.public_tunnel_url = (os.getenv("PUBLIC_TUNNEL_URL", "") if public_tunnel_url is None else public_tunnel_url).rstrip("/")
        self.target_base = target_base
        self.auth_instructions = auth_instructions
        self.max_seeds = max_seeds
        self.fallback_routes_file = fallback_routes_file
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key != "your_firecrawl_api_key_here")

    def can_reach_target(self) -> bool:
        """Firecrawl Cloud cannot reach a localhost target without a public tunnel."""
        host = urlparse(self.target_base).hostname or ""
        return host not in LOCAL_HOSTS or bool(self.public_tunnel_url)

    def is_available(self) -> bool:
        return (self.is_configured() and self.can_reach_target()) or bool(self.fallback_routes_file)

    # ------------------------------------------------------------------ URL mapping
    def _to_cloud_url(self, url: str) -> str:
        p = urlparse(url)
        if not self.public_tunnel_url or (p.hostname or "") not in LOCAL_HOSTS:
            return url
        tun = urlparse(self.public_tunnel_url)
        return urlunparse(p._replace(scheme=tun.scheme, netloc=tun.netloc))

    def _from_cloud_url(self, url: str) -> str:
        if not self.public_tunnel_url:
            return url
        p = urlparse(url)
        tun = urlparse(self.public_tunnel_url)
        if p.netloc != tun.netloc:
            return url
        target = urlparse(self.target_base)
        return urlunparse(p._replace(scheme=target.scheme, netloc=target.netloc))

    # ------------------------------------------------------------------ discovery
    def run_agent_discovery(
        self,
        seeds: List[str],
        focus: str = "",
        prompt: Optional[str] = None,
        schema: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Submits an exploration job on the given seeds and polls for results.
        Returns a list of discovered endpoint dicts (GET, status unknown = 0).
        """
        seeds = [s for s in seeds if s][: max(1, self.max_seeds)] or [self.target_base]

        if not self.is_configured() or not self.can_reach_target():
            reason = "API key not configured" if not self.is_configured() else "localhost target without PUBLIC_TUNNEL_URL"
            logger.warning(f"[FIRECRAWL] Skipping live run: {reason}.")
            return self._load_fallback_routes()

        payload = {
            "urls": [self._to_cloud_url(s) for s in seeds],
            "prompt": prompt or build_agent_prompt(seeds, focus=focus, auth_instructions=self.auth_instructions),
            "model": self.model,
            "schema": schema or DiscoveredLinksSchema.model_json_schema(),
        }

        logger.info(f"[FIRECRAWL] Submitting Agent task on {len(seeds)} seed(s)...")
        try:
            resp = self.session.post(self.agent_endpoint, headers=self._headers(), json=payload, timeout=30)
            if resp.status_code not in (200, 201, 202):
                logger.error(f"[FIRECRAWL] Agent start failed with {resp.status_code}: {resp.text[:300]}")
                return self._load_fallback_routes()

            job_id = resp.json().get("id")
            if not job_id:
                logger.error(f"[FIRECRAWL] No job id returned: {resp.text[:300]}")
                return []

            logger.info(f"[FIRECRAWL] Job started with ID: {job_id}. Polling for completion...")
            return self._poll_job(job_id)
        except Exception as e:
            logger.error(f"[FIRECRAWL] Request exception: {e}")
            return self._load_fallback_routes()

    def _poll_job(self, job_id: str) -> List[Dict[str, Any]]:
        elapsed = 0
        poll_url = f"{self.agent_endpoint}/{job_id}"

        while elapsed < self.poll_timeout_seconds:
            try:
                res = self.session.get(poll_url, headers=self._headers(), timeout=30)
                if res.status_code == 200:
                    data = res.json()
                    status = data.get("status")
                    logger.debug(f"[FIRECRAWL] Poll [{elapsed}s] status={status}")

                    if status == "completed":
                        links = (data.get("data") or {}).get("links", [])
                        logger.info(f"[FIRECRAWL] Job completed. Found {len(links)} links.")
                        return [self._as_result(self._from_cloud_url(link)) for link in links if isinstance(link, str)]

                    if status in ("failed", "cancelled"):
                        logger.warning(f"[FIRECRAWL] Job ended with status: {status}")
                        break
            except Exception as e:
                logger.warning(f"[FIRECRAWL] Error polling job {job_id}: {e}")

            time.sleep(self.poll_interval_seconds)
            elapsed += self.poll_interval_seconds

        logger.warning(f"[FIRECRAWL] Polling stopped after {elapsed}s without results.")
        return []

    def _load_fallback_routes(self) -> List[Dict[str, Any]]:
        """Replays a recorded list of URLs, if the experiment configured one."""
        if not self.fallback_routes_file:
            return []
        path = Path(self.fallback_routes_file)
        try:
            urls = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[FIRECRAWL] Could not read fallback routes file {path}: {e}")
            return []
        logger.warning(f"[FIRECRAWL] Replaying {len(urls)} recorded route(s) from {path} (offline mode).")
        return [self._as_result(u) for u in urls if isinstance(u, str)]

    @staticmethod
    def _as_result(url: str) -> Dict[str, Any]:
        # Firecrawl does not report HTTP status; 0 = unknown so it is not mistaken for a verified 200
        return {"url": url, "method": "GET", "source": "firecrawl_navigate", "status_code": 0}
