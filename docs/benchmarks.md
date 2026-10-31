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
   (`state.db`, `run_events.jsonl`, `orchestrator.log`, `prompts/`, `firecrawl_responses/`, endpoint exports,
   the resolved `config.json`, and `tool_runs/<iteration>-<tool>.json`: one trace per tool run with its rule
   and reason, input URLs, raw output, new endpoints, timing/startup and every tool's pending-frontier size and
   visited count afterwards),
   and, for a profile with `access_log_container` (Crawl Maze), saves the target's own access log for the run
   to `target_access.log`,
5. writes `metrics.json` and `run_meta.json` (overrides, ZAP restart time, ZAP add-on versions); after all runs
   `summary.json` and `summary.md` (mean, SD, 95% t-interval) over **every** run of the label.

A label is one configuration. Running a label again **appends** runs (numbering continues after the
highest `run-NN`; existing runs are never overwritten), and the runner refuses a label whose earlier runs
used different `--set` overrides.

Firecrawl needs `FIRECRAWL_API_KEY` and a running tunnel (`PUBLIC_TUNNEL_URL`) pointing at the
benchmark's port. Rendered prompts are stored per run with their hash, and each Firecrawl job's full
API response (URL lists, status, seeds, prompt hash) in `firecrawl_responses/<job id>.json`. Responses
before 2026-10-31 have `links` + `actions_taken`; later ones have `visited_urls`, `interaction_urls` and
`seen_urls` (see *Threats to validity*).

**Observing Firecrawl's traffic.** Only Firecrawl reaches the target through the tunnel, so a logging
proxy between the tunnel and the target records exactly its requests:

```bash
cloudflared tunnel --url http://localhost:3457          # tunnel -> proxy port, not the target port
python -m evaluation.run_benchmark --benchmark crawlground --runs 5 --label x --tunnel-proxy 3457
```

Each run then has `tunnel_traffic.jsonl` (one line per request: time, method, path, status, bytes,
user agent) and the metrics `firecrawl_requests_observed` / `firecrawl_paths_observed`. The proxy runs only
while a run is in progress; outside runs the tunnel gets no answer. For a manual run, start it on its own:
`python -m tools.traffic_proxy --listen 3457 --upstream http://localhost:3456 --log output/tunnel_traffic.jsonl`.

## Metrics

TP = ground-truth entries matched by at least one discovered endpoint. Matching is by canonical hash,
unless the ground-truth file sets `"match": "path"`: then an endpoint matches an entry with the same
path, whatever its method and query string. Crawlground uses path matching, because its server scores a
marker on any GET or POST to the marker path (a submitted form such as
`/score/forms/04-select-field?colour=green` normalizes to a different key but still scores). Crawl Maze
also uses path matching (since 2026-10-23, before its first scored runs): `expected-results.json` lists
paths only, and some resources are reached by a POST (`form/action-post.found`) or with a query string.

| Metric | Definition | Notes |
|---|---|---|
| `recall` | TP / \|GT\| | Primary metric |
| `recall_client_route`, `recall_server_endpoint` | Recall per ground-truth kind (Juice Shop) | Route coverage vs. server coverage are reported separately |
| `recall_marker`, `recall_expected_resource` | Recall for Crawlground / Crawl Maze | |
| `recall_reviewed` | TP among reviewed-reachable entries / their count | Only after the manual review |
| `server_markers_scored` | Markers Crawlground's server saw being hit | Independent of the crawler's own records |
| `server_resources_requested` | Crawl Maze expected-resource paths in the target's access log (any method/query) | Same idea for Crawl Maze; `target_requests_logged` = all logged requests, from every tool incl. Firecrawl |
| `precision` | Discovered endpoints that match a GT entry / \|discovered\| | A source-built GT can be incomplete, so a low value is not automatically noise |
| `unique_valid` | Discovered endpoints with status 2xx/3xx/401/403 | |
| `duplicate_rate` | 1 − \|discovered\| / raw tool results | Redundant tool output |
| `time_to_saturation_s` | Run start → run end | |
| `t50_s`, `t90_s` | Seconds until 50% / 90% of the run's final TP | From `endpoints.created_at` (first matching endpoint per entry) |
| `discovery_curve` | `[seconds, cumulative TP]` per TP | Per run in `metrics.json` |
| `requests`, `tool_runs`, `failed_runs`, `timeout_runs` | From the execution history | Firecrawl's requests are not included (the API does not report them) |
| `startup_overhead_s` | Σ over tool runs of (first request at the target − tool invoked) | See "Tool startup overhead" below |
| `crawl_time_s` | Σ over the same tool runs of (duration − startup) | |
| `startup_share` | startup / (startup + crawl time) | |
| `zap_restart_s` | Runner's ZAP container restart before the run | Environment reset, not part of any tool run |
| `firecrawl_requests_observed`, `firecrawl_paths_observed` | Requests / distinct paths in `tunnel_traffic.jsonl` | Only for runs with `--tunnel-proxy` |

### Tool startup overhead

Measured per tool run, from the moment the orchestrator invokes the tool (`ToolRegistry.run`, the same
start as `tool_duration_seconds`) to the tool's **first request at the target**. Stored in
`execution_history.startup_seconds` (with `started_at`). The end point per tool:

| Tool | First request observed as |
|---|---|
| ZAP traditional spider / Client Spider / AJAX Spider | Earliest `timestamp` (time sent, ms) of the run's messages to the target in ZAP's history |
| Wapiti | First `[+] <METHOD> <url>` line of its output (printed after the fetch: + one local round trip) |
| Firecrawl | First request in the tunnel traffic log after the job was submitted (needs `--tunnel-proxy`) |
| HTTP probe | When the first GET is sent |

Startup therefore covers process/browser launch, scan configuration through the API and, for Firecrawl,
the cloud queue and cloud browser start. Each tool run is measured once: later browser launches within
the same run (one Client Spider scan per seed, one Firecrawl job per batch) count as crawl time. A tool
run that sends no request has no startup value and is left out of both sums. The ZAP daemon itself is
started (restarted) by the runner before each run; that time is `zap_restart_s`, reported separately.
Runs recorded before 2026-10-23 have no startup data (`None`).

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
  (Firecrawl's `actions_taken`, now saved per job) or a per-test check of how the request was made
  (`tunnel_traffic.jsonl` shows which marker URLs Firecrawl's browser requested).

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
  Crawlground is matched by path (see Metrics). Results scored before this (before 2026-10-22) matched by
  exact key and undercounted markers reported only with a query string or as POST; rescore old runs with
  `python -m evaluation.run_benchmark --benchmark <name> --label <label> --recompute`.
- **Crawlground markers are discovery, not interaction** (see Metrics): a marker counts on any request to
  its URL, and most marker URLs are visible in the page source.
- **Firecrawl's own traffic** is only observed in runs with `--tunnel-proxy`; runs before 2026-10-22 have
  neither the traffic log nor the saved responses (only the returned `links`), so which pages Firecrawl
  loaded in them is unknown. `actions_taken` is Firecrawl's own account and is not verified; in the
  first run with a traffic log (Crawlground `firecrawl-only/run-04`) it described clicking marker links and
  submitting forms that the server never received, so it is not evidence of interaction.
- **Crawler traps**: Crawl Maze's AngularJS app yields ever longer hash routes (`#/!/#%2F!%2F%23%2F!%2F…`);
  the normalizer has no guard against repeated route segments, so the full pipeline spends extra rounds on
  them before it saturates (2026-10-23 series: iterations 5–11, ~25 min, no expected resource gained).
- **Firecrawl jobs without traffic**: some jobs finish without any request through the tunnel (probably
  answered from Firecrawl's cache), so they have no startup value. Before 2026-10-23 23:00 such a job made the
  client re-poll until its timeout and drop the job's links (fixed).
- **Nondeterminism**: browser timing and the LLM-driven Firecrawl agent vary between runs.
- **Tunnel and cloud latency** affect Firecrawl timings, not its correctness.
- **Version drift**: results are only comparable for the pinned versions above. The ZAP image tag
  (`2.17.0`) does not pin its add-ons: ZAP installs add-on updates when its container is created. Runs
  from 2026-10-23 on record the versions in `run_meta.json` (`client` 0.34.0, released 2026-10-21); the
  2026-10-16 runs used an earlier, unrecorded `client` version.
- **Tool state between runs**: ZAP is restarted before every run (see above). The early Crawlground test
  runs made before this change (`smoke`, `no-firecrawl`, `firecrawl-smoke`, `with-firecrawl`, 2026-10-12/13,
  one run each) were deleted on 2026-10-31; none of them was part of a series summary.
- **Firecrawl prompt change (2026-10-31)**: every Firecrawl-only and full-pipeline run up to the 2026-10-27
  Crawl Maze series used a per-target context file (`configs/contexts/<target>.md`, rendered into prompt
  sections 3 and 7) with hand-written hints about each benchmark, e.g. Crawlground's "trigger each control
  once", which the other tools never got. The context files and those sections were removed, the "How to
  explore" section was kept unchanged, and the response schema changed from `links` + free-text
  `actions_taken` to three lists (`visited_urls`, `interaction_urls`, `seen_urls`; no action log, to save
  credits). Recall still counts every reported URL. Runs after this change go into **new labels** and are
  not pooled with earlier runs; the earlier runs stay as the baseline. Job size is unchanged
  (`batch_size` 10).
