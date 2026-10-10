"""
Target-agnostic authenticated-session setup shared by all crawlers.

Config (`auth` section). Secrets stay in .env and are referenced as ${VAR}:

    "auth": {
      "enabled": true,
      "login": {                                   # optional: fetch a fresh token/session at startup
        "url": "http://localhost:3000/rest/user/login",
        "method": "POST",
        "json": {"email": "${AUTH_USER}", "password": "${AUTH_PASSWORD}"},   # or "form": {...}
        "token_path": "authentication.token",      # dot-path into the JSON response (optional)
        "token_var": "AUTH_TOKEN"                  # name usable as ${AUTH_TOKEN} below
      },
      "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
      "cookies": {"token": "${AUTH_TOKEN}"}
    }

Cookies set by the login response (Set-Cookie) are added automatically, which covers
classic form/session logins without any token_path.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

import requests

logger = logging.getLogger("orchestrator.auth")

PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_placeholders(value: Any, variables: Optional[Mapping[str, str]] = None) -> Any:
    """Recursively replaces ${VAR} with variables / environment values. Unknown names stay as-is."""
    variables = {**os.environ, **(variables or {})}
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda m: variables.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: expand_placeholders(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_placeholders(v, variables) for v in value]
    return value


@dataclass
class AuthContext:
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return bool(self.headers or self.cookies)

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


def _dig(data: Any, dot_path: str) -> Optional[str]:
    for key in dot_path.split("."):
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data if isinstance(data, str) else None


def prepare_auth(config: Dict[str, Any]) -> AuthContext:
    """Performs the optional login and returns the headers/cookies every crawler should send."""
    auth_cfg = config.get("auth", {})
    if not auth_cfg.get("enabled"):
        return AuthContext()

    variables: Dict[str, str] = {}
    login_cookies: Dict[str, str] = {}
    login = auth_cfg.get("login")
    if login:
        login = expand_placeholders(login)
        try:
            resp = requests.request(
                login.get("method", "POST"),
                login["url"],
                json=login.get("json"),
                data=login.get("form"),
                timeout=login.get("timeout", 20),
            )
            resp.raise_for_status()
            login_cookies = resp.cookies.get_dict()
            if login.get("token_path"):
                token = _dig(resp.json(), login["token_path"])
                if not token:
                    raise ValueError(f"no token at '{login['token_path']}' in login response")
                variables[login.get("token_var", "AUTH_TOKEN")] = token
            logger.info(f"[AUTH] Logged in via {login['url']} "
                        f"(token: {'yes' if variables else 'no'}, session cookies: {len(login_cookies)})")
        except Exception as e:
            logger.error(f"[AUTH] Login failed, crawling unauthenticated: {e}")
            return AuthContext()

    headers = expand_placeholders(auth_cfg.get("headers", {}), variables)
    cookies = {**login_cookies, **expand_placeholders(auth_cfg.get("cookies", {}), variables)}
    unresolved = [v for v in list(headers.values()) + list(cookies.values()) if PLACEHOLDER.search(v)]
    if unresolved:
        logger.error(f"[AUTH] Unresolved ${{...}} placeholders in auth headers/cookies; crawling unauthenticated.")
        return AuthContext()
    return AuthContext(headers=headers, cookies=cookies)
