# Tool capabilities and the rules derived from them

The rule engine (`orchestrator/rules.py`) picks tools by matching an **observable signal** to the
**cheapest tool able to discover that kind of content**. This page records each tool's capability
profile (from official documentation and from live runs against OWASP Juice Shop 20.1.1 with
ZAP 2.17.0) and maps every rule to the profile row it relies on.

## Capability profiles

| Tool (`name`) | Input | Discovers | Cannot / limits | Cost model | Failure modes |
|---|---|---|---|---|---|
| ZAP traditional spider (`zap_traditional_spider`) | Origin root; extra seeds are primed into the Sites tree and used by a recursive scan | Links in HTML, `robots.txt`, `sitemap.xml`, GET forms ([docs](https://www.zaproxy.org/docs/desktop/addons/spider/)) | No JavaScript execution; POST forms are not submitted (`post_form: false`) | One crawl per origin; seeds are nearly free | Timeout (`max_duration_min`) |
| ZAP Client Spider (`zap_client_spider`) | Start URL per seed; optional ZAP context + user | Browser with the ZAP extension: sees the DOM, clicks links/buttons, fills forms; the Client Map records `#/` routes ([docs](https://www.zaproxy.org/docs/desktop/addons/client-side-integration/spider/)) | "Only clicks on links and buttons" (not divs); add-on is alpha; the Client Map export can be invalid YAML (handled by `tools/client_map.py`) | Browser time per page/state; one scan per seed | Timeout; export not readable (volume missing) |
| ZAP AJAX Spider (`zap_ajax_spider`) | Start URL per seed | Crawljax browser; clicks elements, random inputs ([docs](https://www.zaproxy.org/docs/desktop/addons/ajax-spider/)) | Fragments (`#/` routes) are not available through the API; observed 223 s for 0 new endpoints | Browser time per seed | Timeout |
| Wapiti, headless (`wapiti_form_crawl`) | Base URL + `-s` seeds | Forms and their parameters (session DB `params`), JS-rendered links via Playwright Firefox | Fragments are lost in its session DB; produced artefacts on non-HTML files (`/sitemap.xml?email=…`), now filtered by the normalizer | One process for all seeds | Timeout (`max_scan_time`), process failure |
| Firecrawl Agent (`firecrawl_navigate`) | URLs (batched) + natural-language prompt + JSON schema ([docs](https://docs.firecrawl.dev/features/agent)) | LLM-driven navigation: menus, logins, multi-step business workflows | Returns links only (GET, no HTTP status); nondeterministic; prompt ≤ 10,000 characters; needs a public URL (tunnel) for a local target | Cloud job per batch (credits) | Job failed/cancelled (partial results kept), polling timeout |
| HTTP probe (`http_probe`) | URLs with unknown status | Real status code and content type | No discovery, no JavaScript, never follows redirects or submits forms | One GET per URL | Connection errors |

## Observable signals

All signals are computed from persisted data (`storage/state_store.py`); none needs a tool to run.

- **Frontier change per tool**: the tool's pending frontier fingerprint now vs. right after its last run.
- **Usable seeds per tool**: page-like URLs (HTML, unknown content type, or `#/` routes) for browser tools,
  Wapiti and Firecrawl; all pending URLs for the link spider; unknown-status server URLs for the probe.
- **Last run status**: `ok` / `failed` / `timeout`, and how many runs in a row failed.
- **Run counts** per tool.
- **Surface signals** (recorded with every decision, not used to gate tools): `#/` routes present,
  JSON responses with few HTML pages (`spa_html_page_max`), API-heavy surface, auth walls.

## Rules and the capabilities they rely on

Evaluated per engine (zap / wapiti / firecrawl / http) in this order; the first match wins.

| Rule | Condition | Action | Capability relied on |
|---|---|---|---|
| R1 retry | Last run `failed`/`timeout` and attempts ≤ `max_retries` | Same tool, same seeds | Failure modes above are mostly transient (timeouts, cloud jobs) |
| R2 first_run | Enabled tool not covered by the fixed stages has never run | Tool on base URL + `seed_urls` | Every tool's baseline from the entry point |
| R3 browser_spider | Browser spider frontier changed | Client Spider (or AJAX, `browser_spider`) on new page-like URLs | Only browser tools execute client-side routing and DOM events |
| R4 link_spider | Link spider frontier changed | ZAP traditional on new URLs | Cheapest way to follow server-rendered links from new pages |
| R5 form_crawler | Wapiti frontier changed | Wapiti on new page-like URLs | Only Wapiti extracts form parameters |
| R6 ui_agent | Firecrawl frontier changed | Firecrawl on new page-like URLs, batched | Only the agent performs multi-step workflows and semantic navigation |
| R7 probe | Unknown-status URLs changed | HTTP probe | Only the probe turns status 0 (Client Map, Firecrawl) into a real status/content type |
| — | No rule fires for any engine | Terminate (saturation) | — |

Ordering principle: retries first, then first runs, then capability rules; within the ZAP lane the
browser spider runs before the link spider. Without a budget, order changes when and with which input
a tool runs, not the final coverage.

Validation: every rule has a table-driven unit test (`tests/test_rules.py`); every round logs its
`rule_id` (history table and `run_events.jsonl`); per-rule ablations use `orchestrator.rules.disabled_rules`
in benchmark runs, and rules that never add recall should be removed.

## Configurable thresholds (calibrate on benchmarks)

| Parameter | Default | Meaning |
|---|---|---|
| `orchestrator.rules.max_retries` | 1 | Extra attempts after a failed/timed-out run |
| `orchestrator.rules.max_runs_per_tool` | 10 | Safety ceiling per tool |
| `orchestrator.max_iterations` | 30 | Safety ceiling for rounds |
| `orchestrator.rules.spa_html_page_max` | 3 | HTML-page count below which JSON answers suggest a JS shell (signal only) |
