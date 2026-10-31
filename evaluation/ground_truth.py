"""
Ground-truth builders for the crawl benchmarks (pinned versions; see benchmarks/docker-compose.yml).

Every builder reads the benchmark's own source at a pinned tag/commit and normalizes the result with
the same NormalizationProfile the crawler uses, so canonical hashes are directly comparable.

  juice-shop   client routes from frontend/src/app/app.routing.ts (`useHash: true` -> GET #/route)
               + server endpoints: explicit app.get/post/put/patch/delete registrations in server.ts
               + the auto-generated REST resources (autoModels: /api/<Name>s and /api/<Name>s/:id).
               Entries carry `reachable: null` until a documented manual review decides which ones a
               crawler can reach without crafted input (see docs/benchmarks.md).
  crawlground  one marker per test file: /score/<category>/<test-id> (server-side hit records exist too)
  crawl-maze   blueprints/utils/resources/expected-results.json (paths ending in ".found")

Usage:  python -m evaluation.ground_truth juice-shop|crawlground|crawl-maze [--out FILE]
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from normalization.normalizer import NormalizationProfile, normalize_endpoint

BENCHMARKS = {
    "juice-shop": {
        "version": "v20.1.1",
        "base_url": "http://localhost:3000/",
        "raw": "https://raw.githubusercontent.com/juice-shop/juice-shop/{version}/{path}",
    },
    "crawlground": {
        "version": "7c7ba5931345bb8bd3a7cc2d5187f156ec0db512",
        "base_url": "http://localhost:3456/",
        "tree": "https://api.github.com/repos/zaproxy/crawlground/git/trees/{version}?recursive=1",
        # The server scores any GET or POST to /score/<category>/<test>, whatever the query string
        "match": "path",
    },
    "crawl-maze": {
        "version": "2a0dd0a75e39925ded887bcd25016e8b3ce0f2b3",
        "base_url": "http://localhost:8090/",
        "raw": "https://raw.githubusercontent.com/google/security-crawl-maze/{version}/{path}",
        # expected-results.json lists paths only: a resource counts as found when its path is requested
        # (e.g. action-post.found is reached by a POST, form actions may carry a query string)
        "match": "path",
    },
}

OUT_DIR = Path(__file__).parent / "ground_truth"


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:  # public GitHub content, pinned revision
        return resp.read().decode("utf-8")


# ---------------------------------------------------------------------------- Juice Shop parsers
ROUTE_TOKEN = re.compile(r"""children:\s*\[|\bpath:\s*['"]([^'"]*)['"]|\[|\]""")
SERVER_ROUTE = re.compile(r"""^\s*app\.(get|post|put|patch|delete)\(\s*['"](/[^'"]*)['"]""", re.MULTILINE)
AUTO_MODEL = re.compile(r"""\{\s*name:\s*'(\w+)'""")


def juice_shop_routes(routing_ts: str) -> List[str]:
    """
    Full client route paths ('' = root). Child routes (`children: [...]`) are prefixed with their
    parent's path, as Angular does. Wildcards and matcher-based routes have no fixed path.
    """
    code = "\n".join(line for line in routing_ts.splitlines() if not line.lstrip().startswith("//"))
    depth = 0
    last_path_at: Dict[int, str] = {}     # most recent `path:` seen at each array depth
    prefixes: List[tuple] = []            # (depth of the children array, full parent path)
    paths: List[str] = []
    for m in ROUTE_TOKEN.finditer(code):
        token = m.group(0)
        if token.startswith("children"):
            parent = last_path_at.get(depth, "")
            outer = prefixes[-1][1] if prefixes else ""
            depth += 1
            prefixes.append((depth, "/".join(x for x in (outer, parent) if x)))
        elif token == "[":
            depth += 1
        elif token == "]":
            if prefixes and prefixes[-1][0] == depth:
                prefixes.pop()
            depth -= 1
        else:
            path = m.group(1)
            last_path_at[depth] = path
            if path == "**" or "*" in path:
                continue
            outer = prefixes[-1][1] if prefixes else ""
            paths.append("/".join(x for x in (outer, path) if x))
    return list(dict.fromkeys(paths))


def juice_shop_server_endpoints(server_ts: str) -> List[tuple]:
    """(METHOD, path) from explicit registrations (commented-out lines are ignored) and autoModels."""
    code = "\n".join(line for line in server_ts.splitlines() if not line.lstrip().startswith("//"))
    endpoints = [(m.upper(), p) for m, p in SERVER_ROUTE.findall(code)]
    for name in AUTO_MODEL.findall(_bracket_block(code, "autoModels")):
        collection = f"/api/{name}s"
        endpoints += [("GET", collection), ("POST", collection),
                      ("GET", f"{collection}/:id"), ("PUT", f"{collection}/:id"), ("DELETE", f"{collection}/:id")]
    return list(dict.fromkeys(endpoints))


def _bracket_block(code: str, marker: str) -> str:
    """The `[...]` literal that follows `marker`, with nested brackets matched."""
    start = code.find("[", code.find(marker)) if marker in code else -1
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(code)):
        depth += {"[": 1, "]": -1}.get(code[i], 0)
        if depth == 0:
            return code[start:i + 1]
    return code[start:]


# ---------------------------------------------------------------------------- builders
def _entry(profile: NormalizationProfile, url: str, method: str, kind: str, origin: str, **extra) -> Optional[dict]:
    ep = normalize_endpoint(url, method=method, profile=profile)
    if not ep:
        return None
    return {"canonical_key": ep["canonical_key"], "canonical_hash": ep["canonical_hash"], "method": ep["method"],
            "path_template": ep["path_template"], "kind": kind, "origin": origin, **extra}


def build_juice_shop(spec: Dict) -> List[dict]:
    raw = lambda path: fetch(spec["raw"].format(version=spec["version"], path=path))  # noqa: E731
    profile = NormalizationProfile.from_config({"target": {"base_url": spec["base_url"]}})
    base = spec["base_url"].rstrip("/")
    entries = []
    for path in juice_shop_routes(raw("frontend/src/app/app.routing.ts")):
        entries.append(_entry(profile, f"{base}/#/{path}", "GET", "client_route", "app.routing.ts", reachable=None))
    for method, path in juice_shop_server_endpoints(raw("server.ts")):
        entries.append(_entry(profile, f"{base}{path}", method, "server_endpoint", "server.ts", reachable=None))
    return _dedupe(entries)


def build_crawlground(spec: Dict) -> List[dict]:
    tree = json.loads(fetch(spec["tree"].format(version=spec["version"])))["tree"]
    profile = NormalizationProfile.from_config({"target": {"base_url": spec["base_url"]}})
    base = spec["base_url"].rstrip("/")
    entries = []
    for item in tree:
        m = re.fullmatch(r"tests/([^/]+)/([^/]+)\.js", item["path"])
        if m:
            entries.append(_entry(profile, f"{base}/score/{m.group(1)}/{m.group(2)}", "GET", "marker",
                                  item["path"], category=m.group(1), reachable=True))
    return _dedupe(entries)


def build_crawl_maze(spec: Dict) -> List[dict]:
    paths = json.loads(fetch(spec["raw"].format(version=spec["version"],
                                                path="blueprints/utils/resources/expected-results.json")))
    profile = NormalizationProfile.from_config({"target": {"base_url": spec["base_url"]}})
    base = spec["base_url"].rstrip("/")
    return _dedupe([_entry(profile, f"{base}{p}", "GET", "expected_resource", "expected-results.json",
                           category=p.strip("/").split("/")[0], reachable=True) for p in paths])


def _dedupe(entries: Iterable[Optional[dict]]) -> List[dict]:
    seen, out = set(), []
    for e in entries:
        if e and e["canonical_hash"] not in seen:
            seen.add(e["canonical_hash"])
            out.append(e)
    return sorted(out, key=lambda e: (e["kind"], e["canonical_key"]))


BUILDERS = {"juice-shop": build_juice_shop, "crawlground": build_crawlground, "crawl-maze": build_crawl_maze}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build crawl ground truth for a pinned benchmark version.")
    parser.add_argument("benchmark", choices=sorted(BUILDERS))
    parser.add_argument("--out", help="output JSON file (default: evaluation/ground_truth/<benchmark>.json)")
    args = parser.parse_args(argv)
    spec = BENCHMARKS[args.benchmark]
    entries = BUILDERS[args.benchmark](spec)
    out = Path(args.out) if args.out else OUT_DIR / f"{args.benchmark}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {"benchmark": args.benchmark, "version": spec["version"], "base_url": spec["base_url"],
           "match": spec.get("match", "hash"), "entries": entries}
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    kinds = {}
    for e in entries:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"{args.benchmark} {spec['version']}: {len(entries)} entries {kinds} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
