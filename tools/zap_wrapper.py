"""
OWASP ZAP Wrapper for Traditional Spider and Ajax Spider (crawl-only, no active scan).
Extracts discovered endpoints from the ZAP message history via the ZAPv2 API.

Each run only returns the messages produced by that run (history offset), restricted
to the target origin, so repeated runs neither re-report old traffic nor leak
third-party hosts into the attack surface.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse, urlunparse

from zapv2 import ZAPv2

from normalization.patterns import is_static_asset
from tools.client_map import parse_client_map

logger = logging.getLogger("orchestrator.zap")

LOCAL_HOSTS = {"localhost", "127.0.0.1"}
AUTH_RULE_PREFIX = "dast-orchestrator-auth:"

DEFAULT_SPIDER_OPTIONS = {
    "max_depth": 5,
    "max_children": 0,           # 0 = unlimited
    "max_duration_min": 5,       # per scan, enforced by ZAP; 0/None = until the crawl finishes
    "thread_count": 5,
    "parse_sitemap_xml": True,
    "parse_robots_txt": True,
    "process_form": True,        # follow GET forms
    "post_form": False,          # never submit POST forms during discovery (avoids state changes)
    "poll_interval_sec": 2,
}

DEFAULT_AJAX_OPTIONS = {
    "browser_id": "firefox-headless",
    "number_of_browsers": 2,
    "max_crawl_depth": 5,
    "max_crawl_states": 0,       # 0 = unlimited
    "max_duration_min": 3,       # per seed, enforced by ZAP; 0/None = until the crawl finishes
    "event_wait_ms": 1000,       # time for SPA frameworks to render after an event
    "reload_wait_ms": 1000,
    "click_default_elems": True,
    "click_elems_once": True,
    "random_inputs": True,
    "max_seeds": None,           # None = every seed gets a browser pass
    "poll_interval_sec": 3,
}


DEFAULT_CLIENT_OPTIONS = {
    "browser_id": "firefox-headless",
    "number_of_browsers": 2,
    "max_depth": 0,              # 0 = unlimited (max-recall default)
    "max_children": 0,           # 0 = unlimited
    "max_duration_min": 0,       # per seed, enforced by ZAP; 0 = until the crawl finishes
    "initial_load_time_sec": 0,
    "page_load_time_sec": 0,
    "action_wait_time_sec": 0,
    "shutdown_time_sec": 5,      # idle time before the spider decides it is done
    "scope_check": "Flexible",
    "logout_avoidance": True,
    "max_seeds": None,           # None = every seed gets its own scan
    # Cap per scan when one run has several seeds (rule rounds): each scan explores the whole app
    # from its seed, so N seeds = N crawls. None = use max_duration_min (no limit by default).
    "per_seed_max_duration_min": None,
    "poll_interval_sec": 5,
}

DEFAULT_CLIENT_MAP_EXPORT = {
    "container_dir": "/zap/wrk",  # where ZAP writes the export (inside its container)
    "host_dir": "output/zap",     # the same directory as seen by the orchestrator (volume)
    "file_name": "client_map.yaml",
}


def _watchdog_seconds(max_duration_min: Optional[float], grace_sec: int) -> Optional[float]:
    """Client-side safety timeout matching ZAP's own duration limit; None = no limit."""
    return max_duration_min * 60 + grace_sec if max_duration_min else None


def _within(start: float, timeout: Optional[float]) -> bool:
    return timeout is None or time.time() - start < timeout


class ZapWrapper:
    def __init__(
        self,
        api_url: str = "http://127.0.0.1:18080",
        api_key: str = "changeme",
        target_base: str = "http://localhost/",
        target_host_alias: str = "",
        spider_options: Optional[Dict[str, Any]] = None,
        ajax_options: Optional[Dict[str, Any]] = None,
        client_options: Optional[Dict[str, Any]] = None,
        client_map_export: Optional[Dict[str, Any]] = None,
    ):
        """
        target_host_alias: hostname ZAP must use to reach a target that the orchestrator sees
        as localhost (e.g. "host.docker.internal" when ZAP runs in Docker). Empty = no rewrite.
        """
        self.api_url = api_url
        self.api_key = api_key
        self.target_base = target_base
        self.target_host_alias = target_host_alias
        self.spider_options = {**DEFAULT_SPIDER_OPTIONS, **(spider_options or {})}
        self.ajax_options = {**DEFAULT_AJAX_OPTIONS, **(ajax_options or {})}
        self.client_options = {**DEFAULT_CLIENT_OPTIONS, **(client_options or {})}
        self.client_map_export = {**DEFAULT_CLIENT_MAP_EXPORT, **(client_map_export or {})}
        self.client_spider_user: Optional[tuple] = None  # (context name, user name) once browser login works
        self.browser_login: Optional[Dict[str, Any]] = None  # set from config (auth.browser_login)
        self._browser_login_attempted = False
        self.last_request_count = 0
        self.last_status = "ok"  # ok | timeout (per run)
        self.auth_headers: Dict[str, str] = {}
        self._client: Optional[ZAPv2] = None

    def set_auth(self, headers: Dict[str, str], cookies: Dict[str, str]) -> None:
        """Headers/cookies ZAP adds to every request to the target (spider and Ajax browser traffic)."""
        self.auth_headers = dict(headers)
        if cookies:
            self.auth_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    @property
    def client(self) -> ZAPv2:
        if self._client is None:
            proxies = {"http": self.api_url, "https": self.api_url}
            self._client = ZAPv2(proxies=proxies, apikey=self.api_key)
        return self._client

    def new_session(self) -> bool:
        """
        Starts an empty ZAP session (history, Sites tree, Client Map) so that a run cannot reuse
        what earlier runs discovered. Used between benchmark repetitions.
        """
        try:
            self.client.core.new_session(overwrite=True)
            self.client_spider_user = None
            self._browser_login_attempted = False
            return True
        except Exception as e:
            logger.error(f"[ZAP] Could not start a new session: {e}")
            return False

    def is_alive(self) -> bool:
        """Check if ZAP daemon is reachable and responding."""
        try:
            return bool(self.client.core.version)
        except Exception as e:
            logger.warning(f"ZAP daemon connection failed: {e}")
            return False

    # ------------------------------------------------------------------ host mapping
    def to_zap_url(self, url: str) -> str:
        """Rewrites a local target URL to the host ZAP can reach."""
        p = urlparse(url)
        if not self.target_host_alias or (p.hostname or "") not in LOCAL_HOSTS:
            return url
        netloc = self.target_host_alias + (f":{p.port}" if p.port else "")
        return urlunparse(p._replace(netloc=netloc))

    def from_zap_url(self, url: str) -> str:
        """Maps a URL seen by ZAP back onto the target's own host."""
        p = urlparse(url)
        if not self.target_host_alias or p.hostname != self.target_host_alias:
            return url
        target_host = urlparse(self.target_base).hostname or "localhost"
        netloc = target_host + (f":{p.port}" if p.port else "")
        return urlunparse(p._replace(netloc=netloc))

    def _zap_base(self) -> str:
        p = urlparse(self.to_zap_url(self.target_base))
        return f"{p.scheme}://{p.netloc}"

    def _message_count(self) -> int:
        try:
            return int(self.client.core.number_of_messages(baseurl=self._zap_base()))
        except Exception:
            return 0

    # ------------------------------------------------------------------ auth
    def _remove_auth_rules(self) -> None:
        for rule in self.client.replacer.rules or []:
            if str(rule.get("description", "")).startswith(AUTH_RULE_PREFIX):
                self.client.replacer.remove_rule(rule["description"])

    def _apply_auth(self) -> None:
        """
        Installs Replacer rules that set the auth headers on every request to the target origin
        (added when absent, overwritten when present). Previous rules of this tool are replaced.
        """
        try:
            self._remove_auth_rules()
            url_regex = re.escape(self._zap_base()) + ".*"
            for name, value in self.auth_headers.items():
                self.client.replacer.add_rule(
                    description=f"{AUTH_RULE_PREFIX}{name}", enabled="true", matchtype="REQ_HEADER",
                    matchregex="false", matchstring=name, replacement=value, url=url_regex,
                )
            if self.auth_headers:
                logger.info(f"[ZAP] Auth headers injected for {self._zap_base()}: {list(self.auth_headers)}")
        except Exception as e:
            logger.error(f"[ZAP] Could not install auth Replacer rules: {e}")

    def setup_browser_login(self, login_page_url: str, username: str, password: str,
                            context_name: str = "dast-orchestrator", user_name: str = "crawler",
                            login_page_wait_sec: int = 5, browser_id: str = "firefox-headless",
                            step_delay_sec: Optional[int] = None) -> bool:
        """
        Configures ZAP browser-based authentication for the target and logs in once.
        ZAP opens the login page in its own browser and auto-detects the username/password
        fields (no per-site steps; the API offers none). Success = a form submission sent
        during the login was accepted (2xx/3xx). On success the Client Spider crawls as this user.
        """
        c = self.client
        try:
            if context_name in (c.context.context_list or []):
                c.context.remove_context(context_name)
            context_id = c.context.new_context(context_name)
            c.context.include_in_context(context_name, re.escape(self._zap_base()) + ".*")
            params = {"loginPageUrl": self.to_zap_url(login_page_url), "loginPageWait": str(login_page_wait_sec),
                      "browserId": browser_id}
            if step_delay_sec:
                params["stepDelay"] = str(step_delay_sec)
            c.authentication.set_authentication_method(context_id, "browserBasedAuthentication", urlencode(params))
            c.sessionManagement.set_session_management_method(context_id, "autoDetectSessionManagement")
            user_id = c.users.new_user(context_id, user_name)
            c.users.set_authentication_credentials(context_id, user_id,
                                                   urlencode({"username": username, "password": password}))
            c.users.set_user_enabled(context_id, user_id, "true")

            offset = self._message_count()
            c.users.authenticate_as_user(context_id, user_id)
            if not self._login_submission_accepted(offset):
                raise RuntimeError("no accepted login submission observed (fields not detected or credentials rejected)")
        except Exception as e:
            logger.warning(f"[ZAP] Browser login failed, keeping header/cookie injection: {e}")
            try:
                c.context.remove_context(context_name)
            except Exception:
                pass
            return False

        self.client_spider_user = (context_name, user_name)
        logger.info(f"[ZAP] Browser login succeeded via {login_page_url}; Client Spider will crawl as '{user_name}'.")
        return True

    def _login_submission_accepted(self, offset: int) -> bool:
        """True if a request with a body (form or JSON login) got a 2xx/3xx answer after `offset`."""
        for msg in self.client.core.messages(self._zap_base(), offset, 500) or []:
            parsed = self._parse_message(msg, "auth")
            if parsed and parsed["method"] in ("POST", "PUT") and 200 <= parsed["status_code"] < 400:
                return True
        return False

    def _ensure_browser_login(self) -> None:
        if self.browser_login and not self._browser_login_attempted:
            self._browser_login_attempted = True
            self.setup_browser_login(**self.browser_login)

    # ------------------------------------------------------------------ spiders
    def _apply_spider_options(self) -> None:
        o = self.spider_options
        s = self.client.spider
        for setter, value in (
            (s.set_option_max_depth, o["max_depth"]),
            (s.set_option_max_children, o["max_children"]),
            (s.set_option_max_duration, o["max_duration_min"] or 0),
            (s.set_option_thread_count, o["thread_count"]),
            (s.set_option_parse_sitemap_xml, o["parse_sitemap_xml"]),
            (s.set_option_parse_robots_txt, o["parse_robots_txt"]),
            (s.set_option_process_form, o["process_form"]),
            (s.set_option_post_form, o["post_form"]),
        ):
            try:
                setter(value)
            except Exception as e:
                logger.debug(f"[ZAP] Could not apply spider option via {setter.__name__}: {e}")

    def _apply_ajax_options(self) -> None:
        o = self.ajax_options
        a = self.client.ajaxSpider
        for setter, value in (
            (a.set_option_browser_id, o["browser_id"]),
            (a.set_option_number_of_browsers, o["number_of_browsers"]),
            (a.set_option_max_crawl_depth, o["max_crawl_depth"]),
            (a.set_option_max_crawl_states, o["max_crawl_states"]),
            (a.set_option_max_duration, o["max_duration_min"] or 0),
            (a.set_option_event_wait, o["event_wait_ms"]),
            (a.set_option_reload_wait, o["reload_wait_ms"]),
            (a.set_option_click_default_elems, o["click_default_elems"]),
            (a.set_option_click_elems_once, o["click_elems_once"]),
            (a.set_option_random_inputs, o["random_inputs"]),
        ):
            try:
                setter(value)
            except Exception as e:
                logger.debug(f"[ZAP] Could not apply ajax option via {setter.__name__}: {e}")

    def _apply_client_options(self) -> None:
        o = self.client_options
        c = self.client.clientSpider
        for setter, value in (
            (c.set_option_browser_id, o["browser_id"]),
            (c.set_option_max_depth, o["max_depth"] or 0),
            (c.set_option_max_children, o["max_children"] or 0),
            (c.set_option_max_duration, o["max_duration_min"] or 0),
            (c.set_option_initial_load_time_in_secs, o["initial_load_time_sec"]),
            (c.set_option_page_load_time_in_secs, o["page_load_time_sec"]),
            (c.set_option_action_wait_time_in_secs, o["action_wait_time_sec"]),
            (c.set_option_shutdown_time_in_secs, o["shutdown_time_sec"]),
            (c.set_option_scope_check, o["scope_check"]),
            (c.set_option_logout_avoidance, o["logout_avoidance"]),
        ):
            try:
                setter(value)
            except Exception as e:
                logger.debug(f"[ZAP] Could not apply client spider option via {setter.__name__}: {e}")

    def _prime_sites_tree(self, urls: List[str]) -> int:
        """Requests each URL once through ZAP so it becomes a node of the Sites tree."""
        primed = 0
        for url in urls:
            try:
                self.client.core.access_url(url, followredirects=False)
                primed += 1
            except Exception as e:
                logger.debug(f"[ZAP] Could not prime {url}: {e}")
        return primed

    def run_traditional_spider(self, seeds: List[str]) -> List[Dict[str, Any]]:
        """
        Runs the ZAP Traditional Spider seeded with every given URL (no seed limit).

        Seeds are first added to ZAP's Sites tree, then ONE recursive scan per origin starts at
        the origin root: with recurse=True ZAP uses every known node under the root as a seed,
        so N seeds cost N single requests + one crawl instead of N full crawls.
        Returns only the HTTP messages produced by this run.
        """
        if not self.is_alive():
            logger.warning("[ZAP] Daemon not reachable. Skipping spider.")
            return []

        self.last_status = "ok"
        self._apply_spider_options()
        self._apply_auth()
        offset = self._message_count()
        o = self.spider_options

        roots: Dict[str, None] = {}
        to_prime: Dict[str, None] = {}
        for seed in seeds:
            url = self.to_zap_url(seed.split("#", 1)[0])  # fragments never reach the server
            p = urlparse(url)
            root = f"{p.scheme}://{p.netloc}/"
            roots.setdefault(root)
            if url.rstrip("/") != root.rstrip("/"):
                to_prime.setdefault(url)

        primed = self._prime_sites_tree(list(to_prime))
        logger.info(f"[ZAP] Traditional Spider: {len(seeds)} seed(s), {primed} primed into the Sites tree, "
                    f"{len(roots)} recursive scan(s)...")

        timeout_sec = _watchdog_seconds(o["max_duration_min"], 30)
        scan_ids = []
        for root in roots:
            try:
                scan_id = self.client.spider.scan(root, recurse=True)
                if str(scan_id).isdigit():
                    scan_ids.append(scan_id)
                else:
                    logger.warning(f"[ZAP] Spider refused {root}: {scan_id}")
            except Exception as e:
                logger.error(f"[ZAP] Error starting spider on {root}: {e}")

        start = time.time()
        pending = set(scan_ids)
        while pending and _within(start, timeout_sec):
            for scan_id in list(pending):
                status = str(self.client.spider.status(scan_id))
                if not status.isdigit() or int(status) >= 100:
                    pending.discard(scan_id)
            if pending:
                time.sleep(o["poll_interval_sec"])
        for scan_id in pending:
            self.last_status = "timeout"
            logger.warning(f"[ZAP] Spider scan {scan_id} timed out; stopping it.")
            try:
                self.client.spider.stop(scan_id)
            except Exception:
                pass

        return self.collect_messages(start=offset, source="zap_traditional_spider")

    def run_ajax_spider(self, seeds: List[str]) -> List[Dict[str, Any]]:
        """
        Runs the ZAP Ajax Spider (real headless browser) on up to `max_seeds` seeds.
        This is the main SPA crawler: it executes JS, clicks elements and follows client routes.
        """
        if not self.is_alive():
            logger.warning("[ZAP] Daemon not reachable. Skipping Ajax Spider.")
            return []

        self.last_status = "ok"
        self._apply_ajax_options()
        self._apply_auth()
        offset = self._message_count()
        o = self.ajax_options
        selected = list(dict.fromkeys(seeds))
        if o["max_seeds"]:
            selected = selected[: o["max_seeds"]]
        timeout_sec = _watchdog_seconds(o["max_duration_min"], 60)
        logger.info(f"[ZAP] Ajax Spider on {len(selected)} seed(s) with {o['number_of_browsers']} browser(s)...")

        for seed in selected:
            try:
                self.client.ajaxSpider.scan(self.to_zap_url(seed))
                start = time.time()
                while _within(start, timeout_sec):
                    if str(self.client.ajaxSpider.status).lower() != "running":
                        break
                    time.sleep(o["poll_interval_sec"])
                else:
                    self.last_status = "timeout"
                    logger.warning(f"[ZAP] Ajax Spider timed out on {seed}; stopping it.")
                    self.client.ajaxSpider.stop()
            except Exception as e:
                logger.error(f"[ZAP] Error running Ajax Spider on {seed}: {e}")

        return self.collect_messages(start=offset, source="zap_ajax_spider")

    def run_client_spider(self, seeds: List[str]) -> List[Dict[str, Any]]:
        """
        Runs the ZAP Client Spider (client-side-integration add-on): browsers with the ZAP
        extension that see the DOM, so client-side routes are recorded in the Client Map.
        Returns the run's HTTP messages plus the URLs read from the exported Client Map.
        """
        if not self.is_alive():
            logger.warning("[ZAP] Daemon not reachable. Skipping Client Spider.")
            return []

        self.last_status = "ok"
        self._apply_client_options()
        self._ensure_browser_login()
        if self.client_spider_user:
            self._remove_auth_rules()  # the browser has a real session; injected cookies would overwrite it
        else:
            self._apply_auth()
        offset = self._message_count()
        o = self.client_options
        selected = list(dict.fromkeys(seeds))
        if o["max_seeds"]:
            selected = selected[: o["max_seeds"]]
        duration = o["max_duration_min"]
        if len(selected) > 1 and o.get("per_seed_max_duration_min"):
            duration = o["per_seed_max_duration_min"]
            try:
                self.client.clientSpider.set_option_max_duration(duration)
            except Exception as e:
                logger.debug(f"[ZAP] Could not set per-seed duration: {e}")
        timeout_sec = _watchdog_seconds(duration, 60)
        user = self.client_spider_user
        logger.info(f"[ZAP] Client Spider on {len(selected)} seed(s) with {o['number_of_browsers']} browser(s)"
                    + (f" as user '{user[1]}'" if user else "") + "...")

        for seed in selected:
            try:
                kwargs = {"url": self.to_zap_url(seed), "numberofbrowsers": o["number_of_browsers"]}
                if user:
                    kwargs.update(contextname=user[0], username=user[1])
                scan_id = self.client.clientSpider.scan(**kwargs)
                if not str(scan_id).isdigit():
                    logger.warning(f"[ZAP] Client Spider refused seed {seed}: {scan_id}")
                    continue
                start = time.time()
                while _within(start, timeout_sec):
                    if str(self.client.clientSpider.status(scan_id)) == "100":
                        break
                    time.sleep(o["poll_interval_sec"])
                else:
                    self.last_status = "timeout"
                    logger.warning(f"[ZAP] Client Spider timed out on {seed}; stopping it.")
                    self.client.clientSpider.stop(scan_id)
            except Exception as e:
                logger.error(f"[ZAP] Error running Client Spider on {seed}: {e}")

        results = self.collect_messages(start=offset, source="zap_client_spider")
        requests = self.last_request_count
        results += self.read_client_map()
        self.last_request_count = requests
        return results

    def read_client_map(self) -> List[Dict[str, Any]]:
        """Exports the Client Map and returns its URLs (routes have no HTTP status: 0)."""
        e = self.client_map_export
        container_path = f"{e['container_dir'].rstrip('/')}/{e['file_name']}"
        host_path = Path(e["host_dir"]) / e["file_name"]
        try:
            self.client.client.export_client_map(container_path)
        except Exception as ex:
            logger.error(f"[ZAP] Client Map export failed: {ex}")
            return []
        if not host_path.is_file():
            logger.error(f"[ZAP] Client Map export not found at {host_path}. Is {e['container_dir']} mounted "
                         f"to {e['host_dir']} (docker-compose volume)? Recreate the container with `make down && make up`.")
            return []

        with open(host_path, encoding="utf-8", errors="replace") as f:
            urls = parse_client_map(f)
        found = []
        for url in urls:
            url = self.from_zap_url(url)
            if is_static_asset(urlparse(url).path):
                continue
            found.append({"url": url, "method": "GET", "status_code": 0, "content_type": "",
                          "source": "zap_client_spider"})
        logger.info(f"[ZAP] Client Map: {len(urls)} URL(s), {len(found)} candidate(s) after dropping static assets.")
        return found

    # ------------------------------------------------------------------ results
    def collect_messages(self, start: int = 0, source: str = "zap_traditional_spider",
                         page_size: int = 500) -> List[Dict[str, Any]]:
        """
        Retrieves target-origin HTTP messages from ZAP history starting at `start`.
        Filters out static assets and returns standardized request dicts.
        """
        discovered: List[Dict[str, Any]] = []
        self.last_request_count = 0
        if not self.is_alive():
            return discovered

        try:
            while True:
                chunk = self.client.core.messages(self._zap_base(), start, page_size)
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
                    item = self._parse_message(msg, source)
                    if item is None:
                        continue
                    if item["status_code"] == 0:
                        # Placeholder ZAP creates for Sites-tree parent nodes ("HTTP/1.0 0"): never sent
                        continue
                    self.last_request_count += 1
                    if not is_static_asset(urlparse(item["url"]).path):
                        discovered.append(item)

                if len(chunk) < page_size:
                    break
                start += page_size
        except Exception as e:
            logger.error(f"[ZAP] Error collecting messages: {e}")

        logger.info(f"[ZAP] {source}: {self.last_request_count} request(s), {len(discovered)} candidate endpoint(s).")
        return discovered

    def _parse_message(self, msg: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
        req_header = msg.get("requestHeader", "") or ""
        res_header = msg.get("responseHeader", "") or ""

        lines = req_header.splitlines()
        if not lines:
            return None
        parts = lines[0].split()
        if len(parts) < 2:
            return None
        method = parts[0].upper()
        url = self.from_zap_url(parts[1])

        status_code = 0
        content_type = ""
        res_lines = res_header.splitlines()
        if res_lines:
            status_parts = res_lines[0].split()
            if len(status_parts) >= 2 and status_parts[1].isdigit():
                status_code = int(status_parts[1])
            for line in res_lines[1:]:
                if line.lower().startswith("content-type:"):
                    content_type = line.split(":", 1)[1].strip().lower()
                    break

        return {
            "url": url,
            "method": method,
            "status_code": status_code,
            "content_type": content_type,
            "source": source,
        }
