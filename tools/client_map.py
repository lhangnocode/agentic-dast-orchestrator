"""
Reader for ZAP's Client Map export (`client/action/exportClientMap`).

The Client Map is the only place ZAP records client-side routes such as `/#/login`.
The export is a YAML tree: one `- node:` entry per origin, then one per path segment.
Segments starting with "/" are appended as-is ("/#" + "/login"); others get a "/" separator
("ftp" + "legal.md"). A segment like "redirect(to)" is ZAP's notation for query parameters.

The exporter (client add-on 0.33.0, alpha) can write invalid YAML, so the tree is rebuilt
from the indentation of the `- node:` lines instead of using a YAML loader.

`parse_client_map` returns the URLs only (what discovery uses). `parse_client_map_tree` also keeps, per node,
its tree parent, the `visited` flag, a `Redirect` target and the DOM components the browser saw on that page
(`tagName` A/AREA/BUTTON/FORM/INPUT, `href`, `formId`, ...): the evidence for the link graph and seed scoring.
Two export layouts are handled: client add-on 0.34 (`components:` list, components with `tagName`) and 0.33
(components directly under the node, often without `href` or `tagName`).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

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


COMPONENT_START = re.compile(r'^(\s*)- (nodeType|href|tagName): ?(.*?)\s*$')
KEY_LINE = re.compile(r'^(\s*)([A-Za-z]+): ?(.*?)\s*$')
COMPONENT_KEYS = {"nodeType": "node_type", "href": "href", "text": "text", "id": "element_id",
                  "tagName": "tag", "tagType": "tag_type", "formId": "form_id"}


@dataclass
class ClientMapNode:
    url: str                                   # absolute URL as exported (ZAP's host alias, if any)
    parent_url: Optional[str]                  # tree parent (path hierarchy, not a navigation link)
    visited: Optional[bool] = None             # None = flag absent in the export
    redirect: Optional[str] = None
    components: List[Dict[str, Any]] = field(default_factory=list)


def _value(raw: str) -> Any:
    value = _unquote(raw)
    if value in ("true", "false"):
        return value == "true"
    return value


def parse_client_map_tree(lines: Iterable[str]) -> List[ClientMapNode]:
    """Every node of the export, in file order, with parent, flags and components.

    Ownership is decided by indentation only: a component or a `visited:`/`Redirect:` line belongs to the
    closest open node indented less than it, which also places the components of a mis-indented
    `components:`/`children:` region (invalid YAML from the exporter) under the right node."""
    nodes: List[ClientMapNode] = []
    stack: List[Tuple[int, ClientMapNode]] = []
    component: Optional[Tuple[int, Dict[str, Any]]] = None   # (dash indent, fields) of the open component

    def owner(indent: int) -> Optional[ClientMapNode]:
        for node_indent, node in reversed(stack):
            if node_indent < indent:
                return node
        return None

    for line in lines:
        line = line.rstrip("\n")
        node_match = NODE_LINE.match(line)
        if node_match:
            component = None
            indent, name = len(node_match.group(1)), _unquote(node_match.group(2))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else None
            if "://" in name or parent is None:
                url = name
            else:
                base, query = _split_params(name)
                path_url = _join(parent.url.split("?", 1)[0], base) if base else parent.url.split("?", 1)[0]
                url = f"{path_url}?{query}" if query else path_url
            node = ClientMapNode(url=url, parent_url=parent.url if parent else None)
            nodes.append(node)
            stack.append((indent, node))
            continue

        start = COMPONENT_START.match(line)
        if start:
            indent = len(start.group(1))
            fields: Dict[str, Any] = {COMPONENT_KEYS[start.group(2)]: _value(start.group(3))}
            holder = owner(indent)
            if holder is not None:
                holder.components.append(fields)
            component = (indent, fields)
            continue

        key = KEY_LINE.match(line)
        if not key:
            continue
        indent, name, raw = len(key.group(1)), key.group(2), key.group(3)
        if component and indent == component[0] + 2 and name in COMPONENT_KEYS:
            value = _value(raw)
            if name == "formId":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    pass
            component[1][COMPONENT_KEYS[name]] = value
            continue
        if component and indent <= component[0] + 2:
            component = None
        if name in ("visited", "Redirect"):
            holder = next((n for i, n in reversed(stack) if i == indent - 2), None)
            if holder is not None:
                if name == "visited":
                    holder.visited = _value(raw) is True
                else:
                    holder.redirect = _value(raw)
    return nodes


def client_map_links(nodes: Iterable[ClientMapNode]) -> List[Dict[str, Any]]:
    """Link edges from the tree: page -> each component's href (kind `dom:<tag>`, `dom:unknown` without a
    tag) and page -> its redirect target (kind `redirect`). Tree parent/child pairs are path structure, not
    navigation, so they are not edges."""
    links: List[Dict[str, Any]] = []
    for node in nodes:
        if "://" not in node.url:
            continue
        for comp in node.components:
            href = comp.get("href")
            if isinstance(href, str) and "://" in href:
                tag = str(comp.get("tag") or "unknown").lower()
                links.append({"parent_url": node.url, "child_url": href, "kind": f"dom:{tag}"})
        if isinstance(node.redirect, str) and "://" in node.redirect:
            links.append({"parent_url": node.url, "child_url": node.redirect, "kind": "redirect"})
    return links
