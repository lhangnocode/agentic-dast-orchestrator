"""
URL Normalization and Deduplication Engine.
Implements:
  Rule 1: Crawler Trap & Noise Filtering
  Rule 2: Multi-Source Deduplication & HTTP State Reconciliation
  Rule 3: Path Template Generalization & Canonical Attack Surface Clustering
"""

import hashlib
import re
import urllib.parse as up
from typing import Optional, Tuple

from normalization.patterns import (
    DIR_ENDPOINTS,
    FS_PREFIXES,
    JS_CONCAT_MARKERS,
    JUNK_TAGS,
    UNRENDERED_VARS_REGEX,
    is_static_asset,
)


def is_crawler_trap_or_junk(url: str) -> Tuple[bool, str]:
    """
    Evaluates whether a given URL is a crawler trap, static asset, or regex extraction artifact (Rule 1).
    """
    if not url or not isinstance(url, str):
        return True, "empty_url"

    dec = up.unquote(url)
    p = up.urlparse(dec)
    path = p.path.rstrip("/")

    # Check for static extension noise
    if is_static_asset(p.path):
        return True, "static_asset"

    # 1. Wildcards from regex reading client-side JS code
    if "*" in url or "*" in dec:
        return True, "wildcard_regex_junk"

    # 2. Unrendered template variables
    if UNRENDERED_VARS_REGEX.search(url) or UNRENDERED_VARS_REGEX.search(dec):
        return True, "unrendered_template_var"
    if "%5C%22" in url or '\\"' in dec or "\\\\" in dec:
        return True, "escaped_quote_or_backslash"

    # 3. Broken JavaScript string concatenations
    if any(k in dec for k in JS_CONCAT_MARKERS):
        return True, "broken_js_concat"

    # 4. HTML tags mistakenly read as URLs
    seg = path.lstrip("/")
    if seg in JUNK_TAGS:
        return True, "html_tag_noise"

    # 5. Leaked server filesystem paths
    if path in FS_PREFIXES or any(path.startswith(fp + "/") for fp in FS_PREFIXES):
        return True, "filesystem_leak"
    if path.endswith(";"):
        return True, "semicolon_artifact"

    # 6. Misparsed hash fragments in path
    if "%23" in url or "#" in dec:
        return True, "hash_fragment_in_path"

    # 7. Dummy test / notfound paths
    if "notfound" in path:
        return True, "dummy_test_url"

    # 8. Phantom asset directories from client bundles
    if path.endswith("/assets") or path.endswith("/assets/css") or path.endswith("/assets/js"):
        return True, "asset_dir_phantom"

    # 9. Recursive crawler traps (e.g. /dir/dir/dir/...)
    parts = [x for x in path.split("/") if x]
    if len(parts) > 2 and len(parts) != len(set(parts)):
        return True, "recursive_path_trap"

    return False, ""


def generalize_path(path: str) -> str:
    """
    Replaces dynamic path parameters with abstract tokens (Rule 3).
    """
    if not path or path == "":
        return "/"

    # Standardize leading slash
    if not path.startswith("/"):
        path = "/" + path

    # 1. Username / channel name -> {user}
    path = re.sub(
        r"^/channel/[a-zA-Z0-9_.-]+(/roku\.json)?$",
        lambda m: f"/channel/{{user}}{m.group(1) or ''}",
        path
    )

    # 2. Pagination page number -> {page}
    if path in ("/page", "/page/"):
        return "/page/{page}/"
    path = re.sub(r"^/page/\d+/?$", "/page/{page}/", path)

    # 3. Specific video ID -> {id}
    path = re.sub(r"^/video/\d+/?$", "/video/{id}", path)

    # 4. Media directory -> {video_dir}
    path = re.sub(r"^/videos/pix_[^/]+/?$", "/videos/{video_dir}/", path)

    # Trailing slash normalization for known directory endpoints
    if path in DIR_ENDPOINTS:
        path = path + "/"

    return path


def generalize_query(query_str: str) -> Tuple[str, Tuple[str, ...]]:
    """
    Normalizes query strings into canonical generalized parameters sorted alphabetically.
    """
    if not query_str:
        return "", ()

    parsed_q = up.parse_qs(query_str, keep_blank_values=True)
    param_keys = tuple(sorted(parsed_q.keys()))
    parts = []

    for k in param_keys:
        if k in ("channelName", "user"):
            val = "{user}"
        elif k in ("id", "playlists_id", "mediaId"):
            val = "{id}"
        elif k == "lang":
            val = "{lang}"
        else:
            val = f"{{{k}}}"
        parts.append(f"{k}={val}")

    return "&".join(parts), param_keys


def compute_canonical_key(
    method: str,
    path: str,
    query: str = ""
) -> Tuple[str, str, str, Tuple[str, ...]]:
    """
    Generates deterministic (canonical_key, canonical_hash, gen_path, param_keys).
    Format: METHOD /path?param1={param1}&param2={param2}
    """
    method_norm = method.strip().upper() or "GET"
    gen_path = generalize_path(path)
    gen_query, param_keys = generalize_query(query)

    query_suffix = f"?{gen_query}" if gen_query else ""
    canonical_key = f"{method_norm} {gen_path}{query_suffix}"
    canonical_hash = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()

    return canonical_key, canonical_hash, gen_path, param_keys


def normalize_endpoint(
    raw_url: str,
    method: str = "GET",
    source_tool: str = "",
    status_code: int = 200,
    target_base: str = "http://localhost"
) -> Optional[dict]:
    """
    Full pipeline to clean, filter, generalize, and canonicalize a discovered URL.
    Returns None if URL is identified as noise/trap.
    """
    is_junk, reason = is_crawler_trap_or_junk(raw_url)
    if is_junk:
        return None

    # Standardize hostname mapping (e.g., host.docker.internal or 127.0.0.1 -> localhost)
    sanitized_url = raw_url.strip().replace("://host.docker.internal", "://localhost").replace("://127.0.0.1", "://localhost")
    parsed = up.urlparse(sanitized_url)

    canonical_key, canonical_hash, gen_path, param_keys = compute_canonical_key(
        method=method,
        path=parsed.path,
        query=parsed.query
    )

    # Reconstruct normalized URL representation
    gen_query, _ = generalize_query(parsed.query)
    query_str = f"?{gen_query}" if gen_query else ""
    normalized_url = f"{target_base.rstrip('/')}{gen_path}{query_str}"

    return {
        "canonical_hash": canonical_hash,
        "canonical_key": canonical_key,
        "method": method.strip().upper(),
        "path_template": gen_path,
        "query_params": list(param_keys),
        "normalized_url": normalized_url,
        "raw_sample": raw_url,
        "source_tool": source_tool,
        "status_code": status_code,
    }
