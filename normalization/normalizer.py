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
    if ";" in dec or p.params != "" or path.endswith(";"):
        return True, "semicolon_artifact"

    # 6. Misparsed hash fragments in path (Rule 1: misparsed hash in backend path, preserve SPA)
    if "#" in dec or "%23" in url:
        p_raw = up.urlparse(url)
        if p_raw.path not in ("", "/"):
            return True, "misparsed_hash_in_backend_path"

    # 7. Dummy test / notfound paths
    if "notfound" in path:
        return True, "dummy_test_url"

    # 8. Phantom asset directories & Bundle/Module leaks
    if any(x in path for x in ["node_modules", "/build/lib", "/build/routes", "/build"]) or path.endswith("/build"):
        return True, "bundle_module_leak_junk"
    if re.search(r"assets/.*assets", path) or re.search(r"public/.*public", path) or re.search(r"images/.*images", path):
        return True, "asset_dir_phantom"
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
    if not path.startswith("/") and not path.startswith("#"):
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

    # 5. Juice Shop & REST API Generalization:
    path = re.sub(
        r"^/api/(Addresss|BasketItems|Cards|Challenges|Complaints|Deliverys|Feedbacks|Hints|Products|Quantitys|Recycles|SecurityAnswers|SecurityQuestions|Users|Wallets)/([0-9a-fA-F-]+|:\w+|\d+|null|undefined)(/.*)?$",
        r"/api/\1/{id}\3",
        path
    )
    path = re.sub(r"^/rest/basket/(\d+|undefined)(/.*)?$", r"/rest/basket/{id}\2", path)
    path = re.sub(r"^/rest/products/(\d+|undefined)(/.*)?$", r"/rest/products/{id}\2", path)
    path = re.sub(r"^/rest/track-order/([^/]+)$", r"/rest/track-order/{id}", path)
    path = re.sub(r"^/order-completion/([^/]+)$", r"/order-completion/{id}", path)
    path = re.sub(r"^/address/edit/([^/]+)$", r"/address/edit/{id}", path)
    path = re.sub(r"^/conversation/([^/]+)$", r"/conversation/{id}", path)
    path = re.sub(r"^/coding-challenge/([^/]+)$", r"/coding-challenge/{id}", path)
    path = re.sub(r"^/chatbot/conversation/([^/]+)$", r"/chatbot/conversation/{id}", path)
    path = re.sub(r"^/snippets/fixes/([^/]+)$", r"/snippets/fixes/{challenge}", path)
    path = re.sub(r"^/snippets/([^/]+)$", r"/snippets/{challenge}", path)

    # Generalize UUIDs and long tracking hashes in any path segment
    path = re.sub(r"/([0-9a-fA-F]{8,}-[0-9a-fA-F]{4,}-[0-9a-fA-F]{4,}-[0-9a-fA-F]{4,}-[0-9a-fA-F]{12})(/.*)?$", r"/{id}\2", path)
    path = re.sub(r"/([0-9a-fA-F]{4,}-[0-9a-fA-F]{10,})(/.*)?$", r"/{id}\2", path)

    # Trailing slash normalization:
    # All // to /
    while "//" in path:
        path = path.replace("//", "/")

    # Standardize trailing slash: /api/Addresss/ and /api/Addresss merged into /api/Addresss
    if path in DIR_ENDPOINTS:
        path = path + "/"
    elif path != "/" and path.endswith("/") and not path.startswith("/page/"):
        path = path.rstrip("/")

    return path


def generalize_query(query_str: str) -> Tuple[str, Tuple[str, ...]]:
    """
    Normalizes query strings into canonical generalized parameters sorted alphabetically (Rule 3).
    ?name=Score%20Board -> ?name={name}
    ?q=apple -> ?q={q}
    ?id=10 -> ?id={id}
    """
    if not query_str:
        return "", ()

    parsed_q = up.parse_qs(query_str, keep_blank_values=True)
    param_keys = tuple(sorted(parsed_q.keys()))
    parts = []

    for k in param_keys:
        if k in ("channelName", "user"):
            val = "{user}"
        elif k in ("playlists_id", "mediaId"):
            val = "{id}"
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
    target_base: str = "http://localhost:3000"
) -> Optional[dict]:
    """
    Full pipeline to clean, filter, generalize, and canonicalize a discovered URL.
    Returns None if URL is identified as noise/trap.
    """
    is_junk, reason = is_crawler_trap_or_junk(raw_url)
    if is_junk:
        return None

    # Standardize hostname mapping (e.g. host.docker.internal or 127.0.0.1 -> localhost:3000 or target_base)
    sanitized_url = raw_url.strip()
    sanitized_url = re.sub(r"://host\.docker\.internal(:\d+)?", "://localhost:3000", sanitized_url)
    sanitized_url = re.sub(r"://127\.0\.0\.1(:\d+)?", "://localhost:3000", sanitized_url)
    sanitized_url = re.sub(r"://localhost(:\d+)?", "://localhost:3000", sanitized_url)

    parsed = up.urlparse(sanitized_url)

    # SPA Hash-based Routing (Rule 4)
    if "#" in sanitized_url:
        fragment = parsed.fragment or "/"
        fpath = fragment.split("?")[0]
        fquery = fragment.split("?")[1] if "?" in fragment else ""
        if not fpath.startswith("/"):
            fpath = "/" + fpath

        # Generalize dynamic IDs in fragment (Rule 2 + Rule 4)
        gen_fpath = generalize_path(fpath)

        # Canonical key and query
        gen_query, param_keys = generalize_query(fquery)
        query_str = f"?{gen_query}" if gen_query else ""
        canonical_key = f"{method.strip().upper()} #{gen_fpath}{query_str}"
        canonical_hash = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()

        # Canonical SPA URL: http://localhost:3000/#/route
        normalized_url = f"{target_base.rstrip('/')}/#/{gen_fpath.lstrip('/')}{query_str}"
        gen_path = f"#{gen_fpath}"
    else:
        gen_path = generalize_path(parsed.path)
        gen_query, param_keys = generalize_query(parsed.query)
        query_str = f"?{gen_query}" if gen_query else ""
        canonical_key = f"{method.strip().upper()} {gen_path}{query_str}"
        canonical_hash = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
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
