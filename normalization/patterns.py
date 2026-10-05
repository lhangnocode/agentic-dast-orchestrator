"""
Rule-based patterns for URL normalization, crawler-trap filtering, and path generalization.
Implements Rule 1 (Junk Filtering) and Rule 3 (Path Generalization & Canonicalization).
"""

import re
import urllib.parse as up

# Static extension noise that should not be indexed as API/web endpoints
STATIC_EXTENSIONS = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm", ".mp3",
    ".ogg", ".wav", ".map", ".swf", ".webp"
}

# HTML tags erroneously extracted as links by regex-based crawlers
JUNK_TAGS = {
    "a", "b", "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "button", "div", "label", "span", "small", "g,", "_", "i", "pl"
}

# Server filesystem leak prefixes erroneously extracted from stack traces or comments
FS_PREFIXES = {
    "/var", "/var/www", "/var/www/html", "/var/www/html/AVideo", "/etc", "/usr", "/home"
}

# Known directory endpoints requiring standardized trailing slashes
DIR_ENDPOINTS = {
    "/admin", "/objects", "/plugin", "/locale", "/view",
    "/plugin/Live", "/plugin/LiveLink", "/view/mini-upload-form",
    "/audio_wave"
}

# Regex for unrendered client-side JS template variables
UNRENDERED_VARS_REGEX = re.compile(
    r"(%7B|\{)(videoLink|photo|placeholder|channelLink|[^/\}]+Link)(%7D|\})|%\{[a-zA-Z0-9_]+\}%|\$\{[a-zA-Z0-9_]+\}",
    re.IGNORECASE
)

# Broken JS concatenation markers
JS_CONCAT_MARKERS = [
    "webSiteRootURL", "item.", "item.id", "row.", "downloadhighest",
    "%20+%20", '"', "'", "<", ">", "+"
]


def is_static_asset(path: str) -> bool:
    """Returns True if the path points to a static media or stylesheet asset."""
    path_lower = path.lower()
    return any(path_lower.endswith(ext) for ext in STATIC_EXTENSIONS)
