"""
Firecrawl Cloud Agent Client.
Interfaces with the Firecrawl /v2/agent API to perform semantic browser navigation
(menus, dropdowns, modals, client-side routes) on the seeds the orchestrator selects.

Firecrawl runs as stage 2, seeded with every page/route the ZAP/Wapiti stage found,
and again whenever its frontier grows (rule R6).
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

from tools.firecrawl_prompt import (DEFAULT_TEMPLATE, PromptParts, RenderedPrompt, read_text_file,
                                    render_prompt, save_prompt)

load_dotenv()
logger = logging.getLogger("orchestrator.firecrawl")

LOCAL_HOSTS = {"localhost", "127.0.0.1"}


class DiscoveredLinksSchema(BaseModel):
    links: List[str] = Field(description="List of URLs/routes found while interacting with the page")
    actions_taken: List[str] = Field(description="Actions the AI performed (click, scroll, open menu...)")


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
        max_seeds: Optional[int] = None,
        fallback_routes_file: Optional[str] = None,
        prompt_template_file: Optional[str] = None,
        context_file: Optional[str] = None,
        prompts_dir: Optional[str] = "output/prompts",
        responses_dir: Optional[str] = None,
        traffic_log: Optional[str] = None,
        known_routes_max_chars: int = 4000,
        batch_size: int = 10,
        max_credits: Optional[int] = None,
        effort: Optional[str] = None,
    ):
        """
        max_seeds: optional cap on seeds per run (None = all seeds, sent in jobs of `batch_size`).
        prompt_template_file / context_file: prompt template and per-target application context.
        known_routes_max_chars: budget for the "already known" summary inside the prompt.
        max_credits / effort: passed to the Agent API when set (API defaults otherwise).
        responses_dir: when set, every job's final API response (links, actions_taken, status...) is
        saved there as <job id>.json together with its seeds and prompt hash.
        traffic_log: JSONL log of the logging proxy behind the tunnel (tools/traffic_proxy.py). When set,
        each job's first observed request (and request count) is read from it: Firecrawl's startup time.
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
        self.prompt_template_file = prompt_template_file
        self.context_file = context_file
        self.prompts_dir = prompts_dir
        self.responses_dir = responses_dir
        self.traffic_log = traffic_log
        self.last_first_request_at: Optional[float] = None
        self.known_routes_max_chars = known_routes_max_chars
        self.batch_size = max(1, batch_size or 1)
        self.max_credits = max_credits
        self.effort = effort
        # Set by the orchestrator: callable(max_chars) -> summary of what is already discovered
        self.known_routes_provider = None
        self.last_prompt_files: List[str] = []
        self.last_response_files: List[str] = []
        self.last_status = "ok"  # ok | failed | timeout
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
    def build_prompt(self, seeds: List[str], focus: str = "") -> RenderedPrompt:
        template = read_text_file(self.prompt_template_file, "Prompt template") or DEFAULT_TEMPLATE
        known = self.known_routes_provider(self.known_routes_max_chars) if self.known_routes_provider else ""
        parts = PromptParts(target_url=self.target_base, seeds=seeds,
                            app_context=read_text_file(self.context_file, "Application context"),
                            known_routes=known, focus=focus, auth_instructions=self.auth_instructions)
        return render_prompt(template, parts)

    def run_agent_discovery(
        self,
        seeds: List[str],
        focus: str = "",
        schema: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Explores the seeds with the Agent, in jobs of `batch_size` seeds (sequentially),
        each with a prompt rendered from the template. Returns discovered links (GET, status 0).
        """
        self.last_status = "ok"
        self.last_prompt_files = []
        self.last_response_files = []
        seeds = [s for s in dict.fromkeys(seeds) if s]
        if self.max_seeds:
            seeds = seeds[: self.max_seeds]
        seeds = seeds or [self.target_base]

        if not self.is_configured() or not self.can_reach_target():
            reason = "API key not configured" if not self.is_configured() else "localhost target without PUBLIC_TUNNEL_URL"
            logger.warning(f"[FIRECRAWL] Skipping live run: {reason}.")
            return self._load_fallback_routes()

        batches = [seeds[i:i + self.batch_size] for i in range(0, len(seeds), self.batch_size)]
        logger.info(f"[FIRECRAWL] {len(seeds)} seed(s) in {len(batches)} job(s) of up to {self.batch_size}.")
        results: List[Dict[str, Any]] = []
        statuses = []
        for number, batch in enumerate(batches, start=1):
            results += self._run_job(batch, focus, schema, number, len(batches))
            statuses.append(self.last_status)
            self.last_status = "ok"
        # A run is only "ok" if every job was; otherwise report the first problem (retry rule input)
        self.last_status = next((st for st in statuses if st != "ok"), "ok")
        return results

    def _run_job(self, seeds: List[str], focus: str, schema: Optional[Dict[str, Any]],
                 number: int, total: int) -> List[Dict[str, Any]]:
        prompt = self.build_prompt(seeds, focus)
        saved = save_prompt(prompt, self.prompts_dir)
        if saved:
            self.last_prompt_files.append(str(saved))
        payload: Dict[str, Any] = {
            "urls": [self._to_cloud_url(s) for s in seeds],
            "prompt": prompt.text,
            "model": self.model,
            "schema": schema or DiscoveredLinksSchema.model_json_schema(),
        }
        if self.max_credits:
            payload["maxCredits"] = self.max_credits
        if self.effort:
            payload["effort"] = self.effort

        logger.info(f"[FIRECRAWL] Job {number}/{total}: {len(seeds)} seed(s), prompt {len(prompt.text)} chars "
                    f"(sha256 {prompt.sha256[:12]}{', saved to ' + str(saved) if saved else ''}).")
        logger.debug(f"[FIRECRAWL] Prompt:\n{prompt.text}")
        submitted = time.time()
        try:
            resp = self.session.post(self.agent_endpoint, headers=self._headers(), json=payload, timeout=30)
            if resp.status_code not in (200, 201, 202):
                logger.error(f"[FIRECRAWL] Agent start failed with {resp.status_code}: {resp.text[:300]}")
                self.last_status = "failed"
                return self._load_fallback_routes()

            job_id = resp.json().get("id")
            if not job_id:
                logger.error(f"[FIRECRAWL] No job id returned: {resp.text[:300]}")
                self.last_status = "failed"
                return []

            logger.info(f"[FIRECRAWL] Job started with ID: {job_id}. Polling for completion...")
            meta = {"job_id": job_id, "job": number, "jobs": total, "seeds": seeds,
                    "prompt_sha256": prompt.sha256, "submitted_at": submitted}
            return self._poll_job(job_id, meta)
        except Exception as e:
            logger.error(f"[FIRECRAWL] Request exception: {e}")
            self.last_status = "failed"
            return self._load_fallback_routes()

    def _save_response(self, meta: Dict[str, Any], outcome: str, data: Optional[Dict[str, Any]]) -> None:
        """Keeps the whole API answer (incl. actions_taken), which the results only use `links` of."""
        try:
            traffic = self._job_traffic(meta.get("submitted_at"))
        except Exception as e:   # never let bookkeeping lose a finished job's results
            logger.warning(f"[FIRECRAWL] Could not read the traffic log for job {meta.get('job_id')}: {e}")
            traffic = {}
        meta = {**meta, **traffic}
        first = traffic.get("first_request_at")   # absent when the job sent no request through the tunnel
        if first is not None and (self.last_first_request_at is None or first < self.last_first_request_at):
            self.last_first_request_at = first
        if not self.responses_dir:
            return
        try:
            out = Path(self.responses_dir)
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"{meta['job_id']}.json"
            record = {**meta, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "outcome": outcome, "response": data}
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            self.last_response_files.append(str(path))
        except Exception as e:
            logger.warning(f"[FIRECRAWL] Could not save the response of job {meta.get('job_id')}: {e}")

    def _job_traffic(self, since: Optional[float]) -> Dict[str, Any]:
        """Requests the proxy behind the tunnel saw since the job was submitted (jobs run one at a time)."""
        if not self.traffic_log or since is None:
            return {}
        from tools.traffic_proxy import read_traffic, record_time
        times = [t for t in (record_time(r) for r in read_traffic(self.traffic_log)) if t is not None and t >= since]
        if not times:
            return {"requests_observed": 0}
        first = min(times)
        return {"first_request_at": first, "startup_seconds": round(first - since, 3), "requests_observed": len(times)}

    def _poll_job(self, job_id: str, meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        elapsed = 0
        poll_url = f"{self.agent_endpoint}/{job_id}"
        meta = meta or {"job_id": job_id}
        data: Optional[Dict[str, Any]] = None

        while elapsed < self.poll_timeout_seconds:
            try:
                res = self.session.get(poll_url, headers=self._headers(), timeout=30)
                if res.status_code == 200:
                    data = res.json()
                    status = data.get("status")
                    logger.debug(f"[FIRECRAWL] Poll [{elapsed}s] status={status}")

                    if status == "completed":
                        result = data.get("data") or {}
                        links = result.get("links", [])
                        actions = result.get("actions_taken") or []
                        logger.info(f"[FIRECRAWL] Job completed. Found {len(links)} links, "
                                    f"{len(actions)} action(s) reported.")
                        self._save_response(meta, "completed", data)
                        return [self._as_result(self._from_cloud_url(link)) for link in links if isinstance(link, str)]

                    if status in ("failed", "cancelled"):
                        # A job stopped by its credit limit still returns best-effort `partial` data
                        partial = (data.get("partial") or {}).get("links", []) if isinstance(data.get("partial"), dict) else []
                        logger.warning(f"[FIRECRAWL] Job ended with status: {status} "
                                       f"({data.get('stopReason') or 'no reason'}); {len(partial)} partial link(s).")
                        self.last_status = "failed"
                        self._save_response(meta, status, data)
                        return [self._as_result(self._from_cloud_url(link)) for link in partial if isinstance(link, str)]
            except Exception as e:
                logger.warning(f"[FIRECRAWL] Error polling job {job_id}: {e}")

            time.sleep(self.poll_interval_seconds)
            elapsed += self.poll_interval_seconds

        logger.warning(f"[FIRECRAWL] Polling stopped after {elapsed}s without results.")
        self.last_status = "timeout"
        self._save_response(meta, "poll_timeout", data)   # last poll answer, if any
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
