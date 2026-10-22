# Benchmarks and evaluation protocol

Optimization target: **maximum recall** of SPA routes and server pages/endpoints, without a time or
request budget. Each crawl runs until saturation; time, requests and failures are recorded, not limited.

## Targets (pinned)

| Benchmark | Version | Port | Ground truth | What it measures |
|---|---|---|---|---|
| OWASP Juice Shop | `bkimminich/juice-shop:v20.1.1` | 3000 | built from source (`app.routing.ts`, `server.ts`) | Realistic Angular SPA with hash routes, login, REST API |
| Crawlground | `zaproxy/crawlground@7c7ba59` | 3456 | one `/score/<category>/<test>` marker per test file; the server also records hits | Whether each kind of control (buttons, forms, JS events, dynamic content, frames, navigation) is exercised |
| Security Crawl Maze | `google/security-crawl-maze@2a0dd0a` | 8090 | `expected-results.json` (91 resources) | Breadth of link extraction (HTML, CSS, JS, Angular/Polymer/React) |

```bash
docker compose -f benchmarks/docker-compose.yml up -d --build   # all three (Crawl Maze builds its SPAs; slow)
python -m evaluation.ground_truth juice-shop                     # regenerate a ground truth (needs network)
```

The ground-truth files in `evaluation/ground_truth/` are normalized with the same rules as the
crawler, so canonical hashes compare directly. **Changing normalization rules requires regenerating
them** (`python -m evaluation.ground_truth <name>`).

### Juice Shop reachability review (to do by hand)

`juice-shop.json` lists everything defined in the source (43 client routes, 136 server endpoints),
including endpoints a crawler can only reach with crafted input (e.g. `DELETE /api/Users/{id}`,
`PUT /api/Products/{id}`). Every entry has `"reachable": null`. Set `reachable` to `true`/`false`
after reviewing it, write down the criterion used (e.g. "reachable from the UI by a logged-in
admin without crafted requests"), and keep the reviewed file under version control.
`recall_reviewed` is reported once at least one entry is marked `true`; until then use `recall`
(against everything in the source) and the per-kind recalls.

## Running

```bash
# one configuration, 5 isolated repetitions
python -m evaluation.run_benchmark --benchmark juice-shop --runs 5 --label client-spider

# the same with the AJAX Spider as browser spider (A/B)
python -m evaluation.run_benchmark --benchmark juice-shop --runs 5 --label ajax-spider \
  --set orchestrator.rules.browser_spider=zap_ajax_spider \
  --set 'orchestrator.stages=[["zap_traditional_spider","zap_ajax_spider","wapiti_form_crawl"],["firecrawl_navigate"]]'

# without Firecrawl
python -m evaluation.run_benchmark --benchmark crawlground --runs 5 --label no-firecrawl \
  --set tools.firecrawl.enabled=false
```

Per run the runner:
1. restarts the target container (fresh application state; Juice Shop resets its database on start),
2. restarts the ZAP container (`docker compose restart zap`) and waits for its API. A new ZAP session
   alone is not enough: the Client Spider's Client Map appeared to carry over between runs, inflating
   later runs' results. `--no-zap-restart` falls back to a new session (faster, for quick tests only),
3. for Crawlground, registers a new tool name so its server-side scores start empty,
4. runs the orchestrator with every output in `output/benchmarks/<benchmark>/<label>/run-NN/`
   (`state.db`, `run_events.jsonl`, `orchestrator.log`, `prompts/`, endpoint exports),
5. writes `metrics.json`; after all runs `summary.json` and `summary.md` (mean, SD, 95% t-interval).

Firecrawl needs `FIRECRAWL_API_KEY` and a running tunnel (`PUBLIC_TUNNEL_URL`) pointing at the
benchmark's port. Rendered prompts are stored per run with their hash.

## Metrics

TP = discovered canonical hashes that are in the ground truth.

| Metric | Definition | Notes |
|---|---|---|
| `recall` | TP / \|GT\| | Primary metric |
| `recall_client_route`, `recall_server_endpoint` | Recall per ground-truth kind (Juice Shop) | Route coverage vs. server coverage are reported separately |
| `recall_marker`, `recall_expected_resource` | Recall for Crawlground / Crawl Maze | |
| `recall_reviewed` | TP among reviewed-reachable entries / their count | Only after the manual review |
| `server_markers_scored` | Markers Crawlground's server saw being hit | Independent of the crawler's own records |
| `precision` | TP / \|discovered\| | A source-built GT can be incomplete, so a low value is not automatically noise |
| `unique_valid` | Discovered endpoints with status 2xx/3xx/401/403 | |
| `duplicate_rate` | 1 − \|discovered\| / raw tool results | Redundant tool output |
| `time_to_saturation_s` | Run start → run end | |
| `t50_s`, `t90_s` | Seconds until 50% / 90% of the run's final TP | From `endpoints.created_at` |
| `discovery_curve` | `[seconds, cumulative TP]` per TP | Per run in `metrics.json` |
| `requests`, `tool_runs`, `failed_runs`, `timeout_runs` | From the execution history | |

URL/route recall is **not** application-state coverage: reaching `#/basket` does not mean every
action in the basket view was exercised. Anything beyond URL discovery should be reported qualitatively.

**Crawlground markers measure URL discovery, not interaction.** Crawlground scores a marker on any
GET or POST to `/score/<category>/<test>`, however it was made (`handleScore` in its `server.js`; no test
in the pinned version defines a `scoreWhen` condition). A direct request by the HTTP probe scores like a
real click. In addition, 56 of the 59 test pages contain their marker URL literally in the initial HTML
or an inline script (all but `frames/03-iframe-src`, `navigation/06-http-redirect` and
`navigation/07-base-href`), so a tool that reads the page source can report a marker without hovering,
scrolling or clicking anything. Consequences:
- "Markers reported" (`recall_marker`) = the URL was found by some means.
- `server_markers_scored` = the URL was requested by some means, not necessarily by the interaction the
  test is about.
- Neither shows that a tool performed the interaction. Reporting that needs the tool's own action trace
  (e.g. Firecrawl's `actions_taken`) or a per-test check of how the request was made.

## Protocol

- Same image digests, configuration file, seeds and credentials for every configuration compared.
- ≥ 5 repetitions per configuration (Firecrawl and browser spiders are nondeterministic); alternate the
  order of configurations between repetitions (e.g. A, B, B, A, …) to spread host-load drift.
- Report mean ± 95% CI and the per-run values; do not compare single runs.
- Record the host (CPU, RAM) and ZAP/add-on versions with the results.

## Threats to validity

- **Application state**: crawlers create data (feedback, basket items) as they go; the target is
  restarted before every run to remove it.
- **Ground truth from source**: `juice-shop.json` comes from routing/server code; endpoints created
  dynamically, or only reachable with crafted input, need the manual review above.
- **Normalization** decides what counts as one endpoint; ground truth and results use the same rules.
  For Crawlground this undercounts: a marker reported only with a query string (a submitted form, e.g.
  `/score/forms/04-select-field?colour=green`) normalizes to `GET /score/forms/04-select-field?colour={colour}`,
  which does not match the marker's key, so it is not counted.
- **Crawlground markers are discovery, not interaction** (see Metrics): a marker counts on any request to
  its URL, and most marker URLs are visible in the page source.
- **Firecrawl's own traffic is not observed**: its requests are not counted, and only its returned
  `links` are kept (its `actions_taken` are discarded), so which pages it actually loaded is unknown.
- **Nondeterminism**: browser timing and the LLM-driven Firecrawl agent vary between runs.
- **Tunnel and cloud latency** affect Firecrawl timings, not its correctness.
- **Version drift**: results are only comparable for the pinned versions above.
- **Tool state between runs**: ZAP is restarted before every run (see above). Results produced
  before this change (Crawlground `firecrawl-smoke`, `with-firecrawl` on 2026-10-13) may contain
  URLs carried over from earlier runs and should not be used.
