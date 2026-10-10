"""
Tool Registry: builds the crawler wrappers from config, reports which ones are usable,
and runs them behind one uniform interface. Shared by the adaptive loop and the
standalone baselines so tool wiring lives in exactly one place.
"""

import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator.auth import AuthContext, expand_placeholders, prepare_auth
from tools.firecrawl_client import FirecrawlClient
from tools.wapiti_wrapper import WapitiWrapper
from tools.zap_wrapper import ZapWrapper

logger = logging.getLogger("orchestrator.tools")

ZAP_TRADITIONAL = "zap_traditional_spider"
ZAP_AJAX = "zap_ajax_spider"
WAPITI = "wapiti_form_crawl"
FIRECRAWL = "firecrawl_navigate"
ALL_TOOLS = (ZAP_TRADITIONAL, ZAP_AJAX, WAPITI, FIRECRAWL)


@dataclass
class ToolRun:
    tool: str
    seeds: List[str]
    results: List[Dict[str, Any]] = field(default_factory=list)
    requests_sent: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None


def _resolve_binary(binary: str) -> Optional[str]:
    """Finds a CLI on PATH, or next to the running interpreter (un-activated virtualenv)."""
    found = shutil.which(binary)
    if found:
        return found
    candidate = Path(sys.executable).parent / binary
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


class ToolRegistry:
    def __init__(self, zap: ZapWrapper, wapiti: WapitiWrapper, firecrawl: FirecrawlClient,
                 disabled: Optional[List[str]] = None):
        self.zap = zap
        self.wapiti = wapiti
        self.firecrawl = firecrawl
        self.disabled = set(disabled or [])

    @classmethod
    def from_config(cls, config: Dict[str, Any], session_suffix: str = "",
                    auth: Optional[AuthContext] = None) -> "ToolRegistry":
        """
        session_suffix keeps Wapiti session/report files of separate runs apart (e.g. baselines).
        auth defaults to prepare_auth(config) (login + headers/cookies from the `auth` section).
        """
        auth = auth if auth is not None else prepare_auth(config)
        target = config.get("target", {})
        base_url = target.get("base_url", "http://localhost/")
        tunnel = (os.getenv("PUBLIC_TUNNEL_URL") or target.get("public_tunnel_url") or "").rstrip("/")
        tools_cfg = config.get("tools", {})

        zap_cfg = tools_cfg.get("zap", {})
        zap = ZapWrapper(
            api_url=zap_cfg.get("api_url", "http://127.0.0.1:18080"),
            api_key=zap_cfg.get("api_key", "changeme"),
            target_base=base_url,
            target_host_alias=zap_cfg.get("target_host_alias", ""),
            spider_options=zap_cfg.get("spider"),
            ajax_options=zap_cfg.get("ajax_spider"),
        )
        zap.set_auth(auth.headers, auth.cookies)

        w = tools_cfg.get("wapiti", {})

        def _suffixed(path: str) -> str:
            if not session_suffix:
                return path
            p = Path(path)
            return str(p.with_name(f"{p.stem}_{session_suffix}{p.suffix}"))

        wapiti = WapitiWrapper(
            binary_path=_resolve_binary(w.get("binary", "wapiti")) or w.get("binary", "wapiti"),
            scope=w.get("scope", "domain"),
            max_parameters=w.get("max_parameters", 100),
            max_scan_time=w.get("max_scan_time", 300),
            timeout=w.get("timeout", 20),
            session_db_path=_suffixed(w.get("session_file", "output/temp/wapiti/session.db")),
            report_path=_suffixed(w.get("report_file", "output/temp/wapiti/report.json")),
            log_path=w.get("log_file", "output/logs/wapiti.log"),
            headless=w.get("headless", "no"),
            wait_sec=w.get("wait_sec"),
            depth=w.get("depth"),
            max_links_per_page=w.get("max_links_per_page"),
            cookie_file=w.get("cookie_file"),
            headers=auth.headers,
            cookies=auth.cookies,
        )

        f = tools_cfg.get("firecrawl", {})
        firecrawl = FirecrawlClient(
            api_key=f.get("api_key"),
            api_url=f.get("api_url", "https://api.firecrawl.dev/v2"),
            model=f.get("model", "spark-2"),
            poll_timeout_seconds=f.get("poll_timeout_seconds", 600),
            poll_interval_seconds=f.get("poll_interval_seconds", 10),
            public_tunnel_url=tunnel,
            target_base=base_url,
            auth_instructions=expand_placeholders(f.get("auth_instructions", "")),
            max_seeds=f.get("max_seeds", 5),
            fallback_routes_file=f.get("fallback_routes_file"),
        )
        disabled = [t for t, cfg in (("zap", zap_cfg), ("wapiti", w), ("firecrawl", f)) if cfg.get("enabled") is False]
        disabled_tools = [tool for tool in ALL_TOOLS if tool.split("_")[0] in disabled]
        return cls(zap, wapiti, firecrawl, disabled=disabled_tools)

    def available_tools(self) -> List[str]:
        """Tools that are enabled and can actually run right now."""
        available = []
        zap_up = self.zap.is_alive()
        if zap_up:
            available += [ZAP_TRADITIONAL, ZAP_AJAX]
        else:
            logger.warning("[TOOLS] ZAP daemon unreachable: ZAP spiders unavailable (run `make up`).")
        if _resolve_binary(self.wapiti.binary_path):
            available.append(WAPITI)
        else:
            logger.warning(f"[TOOLS] Wapiti binary '{self.wapiti.binary_path}' not found: Wapiti unavailable.")
        if self.firecrawl.is_available():
            available.append(FIRECRAWL)
        return [t for t in available if t not in self.disabled]

    def run(self, tool: str, seeds: List[str], base_url: str, focus: str = "") -> ToolRun:
        """Runs one tool; never raises (errors are reported in ToolRun.error)."""
        run = ToolRun(tool=tool, seeds=list(seeds))
        start = time.time()
        try:
            if tool == ZAP_TRADITIONAL:
                run.results = self.zap.run_traditional_spider(seeds)
                run.requests_sent = self.zap.last_request_count
            elif tool == ZAP_AJAX:
                run.results = self.zap.run_ajax_spider(seeds)
                run.requests_sent = self.zap.last_request_count
            elif tool == WAPITI:
                run.results = self.wapiti.run_crawl_only(base_url=base_url, seeds=seeds)
                run.requests_sent = self.wapiti.last_request_count
            elif tool == FIRECRAWL:
                run.results = self.firecrawl.run_agent_discovery(seeds=seeds, focus=focus)
            else:
                run.error = f"unknown tool {tool}"
        except Exception as e:
            logger.error(f"[TOOLS] {tool} failed: {e}")
            run.error = str(e)
        run.duration_seconds = time.time() - start
        return run
