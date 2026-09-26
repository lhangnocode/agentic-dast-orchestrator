"""
Safety Firewall and Scope Whitelist Validator.
Guarantees SSRF protection, scope confinement, and prevents crawler logout traps.
"""

import logging
from typing import List, Set
from urllib.parse import urlparse

logger = logging.getLogger("orchestrator.firewall")

# Known logout / session termination path markers to avoid premature crawler logouts
LOGOUT_PATTERNS = {
    "/logout", "/logoff", "/signout", "/sign_out", "/exit", "/user/logout"
}

# Dangerous internal network destinations
BLOCKED_HOSTS = {
    "169.254.169.254", "metadata.google.internal", "instance-data"
}


class SafetyFirewall:
    def __init__(self, allowed_hosts: List[str], base_url: str = "http://localhost/"):
        self.allowed_hosts: Set[str] = {h.strip().lower() for h in allowed_hosts if h.strip()}
        self.base_url = base_url

    def is_in_scope(self, url: str) -> bool:
        """Validates that URL scheme is HTTP/HTTPS and host is in the authorized whitelist."""
        if not url or not isinstance(url, str):
            return False

        try:
            parsed = urlparse(url.strip())
            if parsed.scheme not in ("http", "https"):
                return False

            host = (parsed.hostname or "").lower()
            if not host:
                return False

            if host in BLOCKED_HOSTS:
                logger.warning(f"[FIREWALL] Blocked SSRF attempt: {url}")
                return False

            # Check if host or port matches allowed hosts
            if host not in self.allowed_hosts and f"{host}:{parsed.port}" not in self.allowed_hosts:
                # Also allow localhost aliases
                if not any(host.endswith(ah) for ah in self.allowed_hosts):
                    logger.warning(f"[FIREWALL] Blocked out-of-scope host '{host}': {url}")
                    return False

            # Check logout traps
            path_lower = parsed.path.lower()
            if any(path_lower == lp or path_lower.startswith(f"{lp}/") for lp in LOGOUT_PATTERNS):
                logger.warning(f"[FIREWALL] Blocked logout route to preserve authenticated session: {url}")
                return False

            return True

        except Exception as e:
            logger.error(f"[FIREWALL] Error parsing URL {url}: {e}")
            return False

    def sanitize_seeds(self, seeds: List[str]) -> List[str]:
        """
        Filters a list of candidate seed URLs against scope and safety rules.
        If all seeds are invalid or empty, returns [self.base_url].
        """
        valid_seeds = []
        for s in seeds:
            s_clean = s.strip()
            if self.is_in_scope(s_clean):
                valid_seeds.append(s_clean)

        if not valid_seeds:
            logger.info(f"[FIREWALL] No valid seeds provided or all blocked. Defaulting to base_url: {self.base_url}")
            return [self.base_url]

        # Deduplicate preserving order
        return list(dict.fromkeys(valid_seeds))
