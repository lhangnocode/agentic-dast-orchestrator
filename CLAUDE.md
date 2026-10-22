# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Research prototype for endpoint and route discovery in DAST. Crawl-only tools (OWASP ZAP traditional spider and Client Spider, Wapiti with `-m ""`, the Firecrawl Cloud agent, and a plain HTTP probe) are orchestrated by **deterministic rules**; no LLM chooses tools. Results are normalized into canonical endpoints and stored in SQLite. The code is target-agnostic: everything specific to one target lives in `configs/app_config.json`, `configs/contexts/<target>.md` and `benchmarks/<target>.json` (the current target is OWASP Juice Shop at `http://localhost:3000/`). The optimization target is maximum recall of SPA routes and server pages/endpoints, without a time budget.

## Commands

All `make` targets use `.venv/bin/python3` / `.venv/bin/pytest` with `PYTHONPATH=.`, so the venv must exist (`uv venv .venv && uv pip install -r requirements.txt`). Wapiti's headless mode needs `.venv/bin/playwright install firefox`.

```bash
make test                     # pytest -v tests/
PYTHONPATH=. .venv/bin/pytest tests/test_rules.py -v      # single test file (or file::test_name)
make up / make down           # ZAP daemon (docker, 127.0.0.1:18080; ./output/zap mounted at /zap/wrk)
make run                      # run_mvp.py --mode adaptive
make zap-only | wapiti-only | firecrawl-only   # single-tool baselines -> output/experiments/<name>/
make clean                    # removes output/temp, output/logs, storage/*.db
make bench-up                 # benchmark targets (benchmarks/docker-compose.yml, pinned versions)
make bench-gt BENCH=juice-shop                 # regenerate a ground truth (network)
make bench BENCH=juice-shop RUNS=5 LABEL=x     # repeated isolated runs + metrics (docs/benchmarks.md)
```

No linter/formatter is configured.

## Architecture

`AdaptiveOrchestrator.run` (`orchestrator/loop.py`):

1. **Fixed stages** (`orchestrator.stages`), default:
   - stage 1: ZAP traditional spider → ZAP Client Spider (one ZAP lane) ∥ Wapiti (own lane), from `base_url` + `target.seed_urls`
   - stage 2: Firecrawl, seeded with every page-like URL found so far
2. **Rule rounds** (`orchestrator/rules.py`). `build_snapshot` reads the store once per round. `select_actions` is a pure function that returns at most one `Action` per engine (`engine_of`: zap / wapiti / firecrawl / http). Rules in priority order: R1 retry (failed/timeout) → R2 first run of an unstaged tool → R3 browser spider → R4 link spider → R5 Wapiti → R6 Firecrawl → R7 HTTP probe. R3–R6 fire when the tool's pending-frontier fingerprint differs from the one stored after its last run. No action anywhere = **saturation** (stop). Ceilings: `max_iterations`, `rules.max_runs_per_tool`. `docs/tool-capabilities.md` maps every rule to the tool capability it relies on.
3. `_run_plan` runs the actions: same-engine tools run sequentially (they share ZAP's message history), and engines run in parallel when `orchestrator.parallel` is true. `SafetyFirewall` filters seeds (scope, logout, metadata hosts). `_ingest` normalizes the results and marks the coverage matrix. `_record` writes a history row with the post-run frontier fingerprint.

Key boundaries:
- **`tools/registry.py`** is the only place that builds tool wrappers from config, for both the loop and the baselines. `available_tools()` reports what can run. Every wrapper returns `{url, method, status_code, content_type, source}` dicts (`status_code: 0` = unknown) and sets `last_request_count` / `last_status` (`ok`/`failed`/`timeout`), which `ToolRun` carries.
- **Tool names** (`orchestrator/decision_schema.py: ToolActionEnum`) label coverage rows, history rows and result sources.
- `orchestrator/policy.py: classify_surface` only produces surface signals that are logged with decisions; it doesn't gate tools.

### Tool specifics

- **ZAP** runs in Docker. `tools.zap.target_host_alias` (`host.docker.internal`) rewrites localhost seeds for ZAP, and results are mapped back. Every run reads only the history after its own start offset, filtered to the target origin. `HTTP/1.0 0` placeholder messages are ignored.
  - Traditional spider: seeds are primed into the Sites tree, then one recursive scan per origin runs. `post_form: false`.
  - **Client Spider** (`client` add-on, alpha in ZAP 2.17.0): one scan per seed. After the run, the Client Map is exported to `/zap/wrk` (volume → `output/zap`) and parsed by `tools/client_map.py`. That parser is **line/indent-based** because ZAP can export invalid YAML; the fixture with the broken region is `tests/fixtures/client_map_juice_shop.yaml`. The Client Map is the only ZAP source of `#/` routes. `client_spider.per_seed_max_duration_min` caps each scan when a run has several seeds.
  - The AJAX Spider remains selectable (`orchestrator.rules.browser_spider`, plus the stage list).
- **Wapiti**: `headless: "hidden"` (Playwright Firefox) is the default. Hash-route seeds are dropped when headless is off.
- **Firecrawl**: the prompt is rendered by `tools/firecrawl_prompt.py` from `configs/prompts/firecrawl_agent.md` (sections 1–10, `string.Template.safe_substitute`) plus `tools.firecrawl.context_file` (`configs/contexts/<target>.md`) and the store's `get_known_routes_summary`.
  - The safety rules are owned by code and appended if the template drops `${constraints}`.
  - The prompt must stay ≤ 10,000 chars. Known routes are cut first, then the seed list; the context is cut only as a last resort.
  - Seeds are sent in jobs of `batch_size`. `max_credits` and `effort` go to the API when set, and `partial` results of credit-limited jobs are kept.
  - Rendered prompts are saved by hash in `prompts_dir`.
  - Without a key, or for a localhost target without `PUBLIC_TUNNEL_URL`, Firecrawl is unavailable.
- **HTTP probe** (`tools/http_probe.py`): one GET per unknown-status URL, with no redirects followed and no `#/` routes. It is always available.
- Time limits: `0`/`null` durations (`spider/ajax_spider/client_spider.max_duration_min`, Wapiti `max_scan_time`) mean no limit.

### Canonical endpoint identity

`normalize_endpoint` (`normalization/normalizer.py`) takes a `NormalizationProfile` from config.
- **Generic rules:** numeric, UUID, hex-hash and opaque-token segments become `{id}`/`{hash}`/`{token}`. Readable slugs such as `03-intersection-observer` are kept. Query values become `{key}`.
- **Hosts:** other hosts are dropped, and alias hosts map to `base_url`'s origin.
- **Routes:** `/#/x` and `/#!/x` become `GET #/x`; plain `#anchor`s are ignored.
- **Junk filters:** static assets, template residue, stack-trace frames, and artefacts around static documents (`/sitemap.xml#/…`, `/sitemap.xml/…`, `/robots.txt?x=`).
- **Target-specific rewrites** go in `normalization.*` in config, never in code.

**Changing rules changes `canonical_hash`**, so regenerate the ground truth (`make bench-gt`).

### State store (`storage/state_store.py`)

Tables:
- `endpoints` (with `content_type`, `created_at`)
- `endpoint_sources` (provenance + raw sample)
- `tool_coverage` (endpoint × tool PENDING/PROCESSED; rows for newly added tools are backfilled)
- `execution_history` (per tool run: `rule_id`, `status`, `error`, `seeds_count`, `raw_results`, `requests_sent`, `frontier_fingerprint`)

Frontier queries:
- `get_pending_frontier(tool, limit, statuses, sources, page_like)` returns concrete observed URLs, never `{id}` templates. It's GET-only, skips dead ends, and lists 2xx first.
- `get_unknown_status_urls` is the probe's frontier.

Every run appends JSONL events (`run_started`, `decision`, `tool_run`, `run_finished`, with `run_id`) to `logging.run_events_file`. `output.dir` sets where the endpoint exports go. The DB persists between runs, so use `make clean` for a fresh run.

### Authentication (`orchestrator/auth.py`, `ZapWrapper.setup_browser_login`)

- **`prepare_auth`** logs in once (JSON or form, optional `token_path`) when `auth.enabled`, and returns the headers/cookies for every tool. `${VAR}` placeholders resolve from `.env` (`AUTH_USER`, `AUTH_PASSWORD`) and the fetched token, so secrets never go into tracked files.
- **`auth.browser_login`** configures ZAP browser-based authentication through the API: a context, `browserBasedAuthentication` with an auto-detected form, auto-detected session management, and a user. The Client Spider then crawls logged in. On success the Replacer header rules are removed for that run, because they would overwrite the browser's cookies.
- **Fallback:** if the login submission isn't accepted, the Client Spider uses header/cookie injection (Replacer rules `dast-orchestrator-auth:*`). The API offers no login `steps`.
- **Other tools:** Wapiti gets `-H`/`-C`, the probe sends the same headers, and Firecrawl gets `auth_instructions`.

### Evaluation

- `evaluation/ground_truth.py`: builds pinned ground truth from each benchmark's source (Juice Shop routes with nesting plus server endpoints and auto-models; Crawlground markers; Crawl Maze expected results).
- `evaluation/crawl_metrics.py`: run metrics (recall per kind, precision, duplicates, t50/t90, saturation time) and the mean/SD/95% t-CI aggregation.
- `evaluation/run_benchmark.py`: repeated isolated runs. It restarts the target, **restarts the ZAP container** (a new ZAP session alone appeared to keep the Client Map, which leaked URLs between runs; `--no-zap-restart` skips this), sets a fresh Crawlground tool name, and gives each run its own output folder.

The Juice Shop ground truth still needs its manual `reachable` review. Protocol and threats to validity: `docs/benchmarks.md`. The old `evaluation/benchmark_runner.py` + `ground_truth/avideo_routes.json` (`make evaluate`) predate the current normalizer.

### Configuration

Runtime parameters come from `configs/app_config.json`. `.env` (loaded by `run_mvp.py` and the benchmark runner) provides `FIRECRAWL_API_KEY`, `PUBLIC_TUNNEL_URL`, `AUTH_USER` and `AUTH_PASSWORD`. Other variables in `.env.example` are not read.
