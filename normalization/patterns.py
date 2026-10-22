"""
Generic, target-agnostic patterns for URL normalization, crawler-trap filtering,
and path generalization (Rules 1 & 3).

Target-specific rules (e.g. "/channel/<name>" -> "/channel/{user}") do NOT belong here:
declare them in the `normalization` section of the app config instead.
"""

import re

# Static extension noise that should not be indexed as API/web endpoints
STATIC_EXTENSIONS = {
    ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp4", ".webm", ".mp3",
    ".ogg", ".wav", ".map", ".swf", ".webp", ".avif", ".bmp"
}

# Static documents that are files, not pages: an SPA route, child path or form query on them
# is a crawler artefact (e.g. a headless browser left on /sitemap.xml then followed in-app links)
STATIC_DOCUMENT_EXTENSIONS = {".xml", ".txt", ".md", ".pdf", ".csv", ".yml", ".yaml", ".log", ".bak",
                              ".kdbx", ".pyc", ".gg", ".zip", ".gz"}

# HTML tags erroneously extracted as links by regex-based crawlers
JUNK_TAGS = {
    "a", "b", "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "button", "div", "label", "span", "small", "g,", "_", "i", "pl"
}

# Server filesystem prefixes leaked from stack traces or comments
FS_PREFIXES = {"/var", "/etc", "/usr", "/home", "/opt", "/proc", "/tmp"}

# Regex for unrendered client-side template variables: {fooLink}, %{foo}%, ${foo}, {{foo}}
UNRENDERED_VARS_REGEX = re.compile(
    r"(%7B|\{)[^/\}]*Link(%7D|\})|%\{[a-zA-Z0-9_]+\}%|\$\{[a-zA-Z0-9_.]+\}|\{\{[^}]*\}\}",
    re.IGNORECASE
)

# Broken JS string concatenation markers
JS_CONCAT_MARKERS = ["item.", "row.", "%20+%20", '"', "'", "<", ">", "+"]

# Path segment that is a dynamic identifier, regardless of the application
NUMERIC_SEGMENT = re.compile(r"^\d+$")
# Numeric resource with a document extension: "42.html", "7.json" -> "{id}.html"
NUMERIC_WITH_EXT_SEGMENT = re.compile(r"^\d+(\.[A-Za-z0-9]{1,5})$")
UUID_SEGMENT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
HEX_HASH_SEGMENT = re.compile(r"^[0-9a-fA-F]{16,}$")
# Mongo ObjectId-like / long opaque tokens mixing letters and digits (e.g. slugs ending in ids)
OPAQUE_TOKEN_SEGMENT = re.compile(r"^(?=.*\d)(?=.*[a-zA-Z])[A-Za-z0-9_-]{24,}$")
# Values leaked by SPAs building URLs from unset JS variables
JS_NULLISH_SEGMENT = {"undefined", "null", "NaN"}
# Express/Angular route placeholders surfaced in JS bundles (":id")
ROUTE_PLACEHOLDER_SEGMENT = re.compile(r"^:[A-Za-z_]\w*$")


def is_static_asset(path: str) -> bool:
    """Returns True if the path points to a static media or stylesheet asset."""
    path_lower = path.lower()
    return any(path_lower.endswith(ext) for ext in STATIC_EXTENSIONS)
