"""
URL Normalization and Deduplication Engine.
Implements:
  Rule 1: Crawler Trap & Noise Filtering
  Rule 2: Multi-Source Deduplication (via a deterministic canonical hash)
  Rule 3: Path Template Generalization & Canonical Attack Surface Clustering
  Rule 4: SPA hash-route canonicalization (`/#/route`, `/#!/route`)

All rules in this module are target-agnostic. Target-specific behaviour is injected
through a NormalizationProfile built from the `normalization` section of the config:

    "normalization": {
      "host_aliases": ["localhost", "127.0.0.1", "host.docker.internal"],
      "path_rules": [{"pattern": "^/channel/[^/]+$", "replacement": "/channel/{user}"}],
      "query_aliases": {"channelName": "{user}"},
      "trailing_slash_dirs": ["/admin"],
      "junk_substrings": ["webSiteRootURL"]
    }
"""

import hashlib
import re
import urllib.parse as up
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Set, Tuple

from normalization.patterns import (
    FS_PREFIXES,
    HEX_HASH_SEGMENT,
    JS_CONCAT_MARKERS,
    JS_NULLISH_SEGMENT,
    JUNK_TAGS,
    NUMERIC_SEGMENT,
    NUMERIC_WITH_EXT_SEGMENT,
    OPAQUE_TOKEN_SEGMENT,
    STATIC_DOCUMENT_EXTENSIONS,
    ROUTE_PLACEHOLDER_SEGMENT,
    UNRENDERED_VARS_REGEX,
    UUID_SEGMENT,
    is_static_asset,
)

MAX_URL_LENGTH = 2048
MAX_PATH_SEGMENTS = 15


@dataclass
class NormalizationProfile:
    """Target-specific knobs for the generic normalization rules."""
    target_base: str = "http://localhost/"
    host_aliases: Set[str] = field(default_factory=lambda: {"localhost", "127.0.0.1", "host.docker.internal"})
    path_rules: List[Tuple[Pattern[str], str]] = field(default_factory=list)
    query_aliases: Dict[str, str] = field(default_factory=dict)
    trailing_slash_dirs: Set[str] = field(default_factory=set)
    junk_substrings: List[str] = field(default_factory=list)

    @property
    def target_origin(self) -> str:
        p = up.urlparse(self.target_base)
        return f"{p.scheme or 'http'}://{p.netloc}"

    @property
    def target_netloc(self) -> str:
        return up.urlparse(self.target_base).netloc

    def in_scope_host(self, hostname: Optional[str]) -> bool:
        """Relative URLs (no host), the target host itself, and declared aliases are in scope."""
        if not hostname:
            return True
        hostname = hostname.lower()
        return hostname == (up.urlparse(self.target_base).hostname or "").lower() or hostname in self.host_aliases

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "NormalizationProfile":
        target = config.get("target", {})
        norm = config.get("normalization", {})
        base_url = target.get("base_url", "http://localhost/")
        aliases = norm.get("host_aliases") or target.get("allowed_hosts") or ["localhost", "127.0.0.1"]
        return cls(
            target_base=base_url,
            host_aliases={h.lower() for h in aliases},
            path_rules=[(re.compile(r["pattern"]), r["replacement"]) for r in norm.get("path_rules", [])],
            query_aliases=dict(norm.get("query_aliases", {})),
            trailing_slash_dirs=set(norm.get("trailing_slash_dirs", [])),
            junk_substrings=list(norm.get("junk_substrings", [])),
        )


_default_profile = NormalizationProfile()


def configure_default_profile(profile: NormalizationProfile) -> None:
    """Sets the profile used when callers do not pass one explicitly."""
    global _default_profile
    _default_profile = profile


def get_default_profile() -> NormalizationProfile:
    return _default_profile


def is_crawler_trap_or_junk(url: str, profile: Optional[NormalizationProfile] = None) -> Tuple[bool, str]:
    """
    Evaluates whether a given URL is a crawler trap, static asset, or regex extraction artifact (Rule 1).
    """
    profile = profile or _default_profile
    if not url or not isinstance(url, str):
        return True, "empty_url"
    if len(url) > MAX_URL_LENGTH:
        return True, "oversized_url"

    dec = up.unquote(url)
    p = up.urlparse(dec)
    path = p.path.rstrip("/")

    if p.scheme and p.scheme not in ("http", "https"):
        return True, "non_http_scheme"

    if is_static_asset(p.path):
        return True, "static_asset"

    # 1. Wildcards from regex reading client-side JS code
    if "*" in dec:
        return True, "wildcard_regex_junk"

    # 2. Unrendered template variables / escaped string residue
    if UNRENDERED_VARS_REGEX.search(url) or UNRENDERED_VARS_REGEX.search(dec):
        return True, "unrendered_template_var"
    if "%5C%22" in url or '\\"' in dec or "\\\\" in dec:
        return True, "escaped_quote_or_backslash"

    # 3. Broken JavaScript string concatenations (generic + target-declared markers)
    if any(k in dec for k in JS_CONCAT_MARKERS) or any(k in dec for k in profile.junk_substrings):
        return True, "broken_js_concat"

    # 4. HTML tags mistakenly read as URLs
    if path.lstrip("/") in JUNK_TAGS:
        return True, "html_tag_noise"

    # 5. Leaked server filesystem paths
    if path in FS_PREFIXES or any(path.startswith(fp + "/") for fp in FS_PREFIXES):
        return True, "filesystem_leak"
    if ";" in dec or p.params != "" or path.endswith(";"):
        return True, "semicolon_artifact"

    # 6. Encoded '#' inside the backend path is a mis-extraction; a real fragment is fine (Rule 4)
    if "%23" in up.urlparse(url).path:
        return True, "misparsed_hash_in_backend_path"

    # 7. Stack-trace frames leaked by error pages ("routes/fileServer.js:53:13")
    if re.search(r"\.[A-Za-z0-9]{1,5}:\d+(:\d+)?$", path):
        return True, "stack_trace_frame"

    # 8. Artefacts around static documents (files, not pages)
    segments = [x for x in path.split("/") if x]
    if any(_is_static_document(seg) for seg in segments[:-1]):
        return True, "path_nested_under_file"
    if segments and _is_static_document(segments[-1]):
        raw = up.urlparse(url)
        if raw.fragment.startswith(("/", "!/")):
            return True, "client_route_on_file"
        if raw.query:
            return True, "query_on_static_document"

    # 9. Bundler / dependency directories leaked from source maps
    if "node_modules" in path:
        return True, "bundle_module_leak_junk"
    if re.search(r"(assets|public|images|static)/.*\1/", path + "/"):
        return True, "asset_dir_phantom"

    # 10. Recursive crawler traps (e.g. /dir/dir/dir/...) and absurdly deep paths
    parts = [x for x in path.split("/") if x]
    if len(parts) > MAX_PATH_SEGMENTS:
        return True, "path_too_deep"
    if len(parts) > 2 and len(parts) != len(set(parts)):
        return True, "recursive_path_trap"

    return False, ""


def _is_static_document(segment: str) -> bool:
    dot = segment.rfind(".")
    return dot > 0 and segment[dot:].lower() in STATIC_DOCUMENT_EXTENSIONS


def _is_slug(segment: str) -> bool:
    """'03-intersection-observer' is a readable name, not a random token: >= 2 alphabetic words."""
    words = [w for w in re.split(r"[-_.]", segment) if len(w) >= 3 and w.isalpha()]
    return len(words) >= 2


def _generalize_segment(seg: str) -> str:
    if not seg or (seg.startswith("{") and seg.endswith("}")):
        return seg
    if NUMERIC_SEGMENT.match(seg) or UUID_SEGMENT.match(seg) or seg in JS_NULLISH_SEGMENT:
        return "{id}"
    numbered = NUMERIC_WITH_EXT_SEGMENT.match(seg)
    if numbered:
        return "{id}" + numbered.group(1)
    if ROUTE_PLACEHOLDER_SEGMENT.match(seg):
        return "{id}"
    if HEX_HASH_SEGMENT.match(seg):
        return "{hash}"
    if OPAQUE_TOKEN_SEGMENT.match(seg) and not _is_slug(seg):
        return "{token}"
    return seg


def generalize_path(path: str, profile: Optional[NormalizationProfile] = None) -> str:
    """
    Replaces dynamic path parameters with abstract tokens (Rule 3).
    Order: collapse slashes -> strip trailing slash -> target rules -> generic segment rules.
    """
    profile = profile or _default_profile
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    while "//" in path:
        path = path.replace("//", "/")
    if path != "/":
        path = path.rstrip("/") or "/"

    # Target-declared rules run first; they may intentionally re-add a trailing slash
    for pattern, replacement in profile.path_rules:
        path = pattern.sub(replacement, path)

    keep_trailing = path.endswith("/") and path != "/"
    segments = [_generalize_segment(s) for s in path.strip("/").split("/")] if path != "/" else []
    path = "/" + "/".join(segments)
    if keep_trailing or path in profile.trailing_slash_dirs:
        path = path.rstrip("/") + "/"
    return path


def generalize_query(query_str: str, profile: Optional[NormalizationProfile] = None) -> Tuple[str, Tuple[str, ...]]:
    """
    Normalizes query strings into canonical generalized parameters sorted alphabetically (Rule 3).
    ?q=apple&id=10 -> ?id={id}&q={q}
    """
    profile = profile or _default_profile
    if not query_str:
        return "", ()

    parsed_q = up.parse_qs(query_str, keep_blank_values=True)
    param_keys = tuple(sorted(k for k in parsed_q.keys() if k))
    parts = [f"{k}={profile.query_aliases.get(k, '{' + k + '}')}" for k in param_keys]
    return "&".join(parts), param_keys


def compute_canonical_key(
    method: str,
    path: str,
    query: str = "",
    profile: Optional[NormalizationProfile] = None,
) -> Tuple[str, str, str, Tuple[str, ...]]:
    """
    Generates deterministic (canonical_key, canonical_hash, gen_path, param_keys).
    Format: METHOD /path?param1={param1}&param2={param2}
    """
    method_norm = method.strip().upper() or "GET"
    gen_path = generalize_path(path, profile)
    gen_query, param_keys = generalize_query(query, profile)

    query_suffix = f"?{gen_query}" if gen_query else ""
    canonical_key = f"{method_norm} {gen_path}{query_suffix}"
    canonical_hash = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
    return canonical_key, canonical_hash, gen_path, param_keys


def _hash_route(fragment: str) -> Optional[str]:
    """Returns the SPA route inside a fragment ('/x', '!/x'), or None for plain in-page anchors."""
    if fragment.startswith("!/"):
        fragment = fragment[1:]
    return fragment if fragment.startswith("/") else None


def normalize_endpoint(
    raw_url: str,
    method: str = "GET",
    source_tool: str = "",
    status_code: int = 200,
    target_base: Optional[str] = None,
    profile: Optional[NormalizationProfile] = None,
) -> Optional[dict]:
    """
    Full pipeline to clean, filter, generalize, and canonicalize a discovered URL.
    Returns None if the URL is noise, a trap, or outside the target's hosts.
    """
    profile = profile or _default_profile
    if target_base and target_base != profile.target_base:
        profile = NormalizationProfile(**{**profile.__dict__, "target_base": target_base})

    is_junk, _ = is_crawler_trap_or_junk(raw_url, profile)
    if is_junk:
        return None

    parsed = up.urlparse(raw_url.strip())
    if not profile.in_scope_host(parsed.hostname):
        return None

    method_norm = (method or "GET").strip().upper()
    route = _hash_route(parsed.fragment)
    if route is not None:
        # SPA hash routing (Rule 4): the route lives in the fragment
        fpath, _, fquery = route.partition("?")
        base_path = generalize_path(parsed.path, profile)
        prefix = "" if base_path == "/" else base_path.rstrip("/")
        gen_route = generalize_path(fpath, profile)
        gen_query, param_keys = generalize_query(fquery, profile)
        query_str = f"?{gen_query}" if gen_query else ""
        gen_path = f"{prefix}#{gen_route}"
        normalized_url = f"{profile.target_origin}{prefix or ''}/#{gen_route}{query_str}"
    else:
        gen_path = generalize_path(parsed.path, profile)
        gen_query, param_keys = generalize_query(parsed.query, profile)
        query_str = f"?{gen_query}" if gen_query else ""
        normalized_url = f"{profile.target_origin}{gen_path}{query_str}"

    canonical_key = f"{method_norm} {gen_path}{query_str}"
    canonical_hash = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()

    return {
        "canonical_hash": canonical_hash,
        "canonical_key": canonical_key,
        "method": method_norm,
        "path_template": gen_path,
        "query_params": list(param_keys),
        "normalized_url": normalized_url,
        "raw_sample": raw_url,
        "source_tool": source_tool,
        "status_code": status_code,
    }
