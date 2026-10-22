"""
Reader for ZAP's Client Map export (`client/action/exportClientMap`).

The Client Map is the only place ZAP records client-side routes such as `/#/login`.
The export is a YAML tree: one `- node:` entry per origin, then one per path segment.
Segments starting with "/" are appended as-is ("/#" + "/login"); others get a "/" separator
("ftp" + "legal.md"). A segment like "redirect(to)" is ZAP's notation for query parameters.

The exporter (client add-on 0.33.0, alpha) can write invalid YAML, so the tree is rebuilt
from the indentation of the `- node:` lines instead of using a YAML loader.
"""

import json
import re
from typing import Iterable, List, Tuple

NODE_LINE = re.compile(r'^(\s*)- node: (.+?)\s*$')
HREF_LINE = re.compile(r'^\s*(?:- )?href: (.+?)\s*$')
PARAM_SEGMENT = re.compile(r'^(.*)\(([^()]*)\)$')


def _unquote(raw: str) -> str:
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')
    return raw


def _join(parent: str, name: str) -> str:
    if name.startswith("/") or parent.endswith("/"):
        return parent + name
    return parent + "/" + name


def _split_params(name: str) -> Tuple[str, str]:
    """'redirect(to)' -> ('redirect', 'to=') ; '(email,password)' -> ('', 'email=&password=')."""
    match = PARAM_SEGMENT.match(name)
    if not match:
        return name, ""
    params = [p.strip() for p in match.group(2).split(",") if p.strip()]
    return match.group(1), "&".join(f"{p}=" for p in params)


def parse_client_map(lines: Iterable[str], include_hrefs: bool = True) -> List[str]:
    """Absolute URLs from the export, in first-seen order. Scope filtering is the caller's job."""
    found: List[str] = []
    seen = set()
    stack: List[Tuple[int, str]] = []  # (indent, url) of the open ancestors

    def keep(url: str) -> None:
        if "://" in url and url not in seen:
            seen.add(url)
            found.append(url)

    for line in lines:
        node = NODE_LINE.match(line)
        if node:
            indent, name = len(node.group(1)), _unquote(node.group(2))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else ""
            if "://" in name or not parent:
                path_url, query = name, ""
            else:
                base, query = _split_params(name)
                # a params-only node ("(email,password)") is the parent page itself with a query;
                # its children (e.g. "/#" routes) belong to that page, not to a "(…)" segment
                path_url = _join(parent, base) if base else parent
            stack.append((indent, path_url))
            keep(f"{path_url}?{query}" if query else path_url)
            continue
        if include_hrefs:
            href = HREF_LINE.match(line)
            if href:
                keep(_unquote(href.group(1)))
    return found
