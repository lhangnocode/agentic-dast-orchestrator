# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Research prototype for adaptive endpoint discovery in DAST. Crawl-only tools (OWASP ZAP spider and Ajax spider, Wapiti with `-m ""`, Firecrawl Cloud agent) are orchestrated by an LLM (Qwen via Alibaba DashScope). Results are normalized into canonical endpoints and stored in SQLite. The code is target-agnostic: everything specific to one target lives in `configs/app_config.json` (the current target is `http://localhost:3000/`).

The current branch implements **strategy 2**: classic crawlers (ZAP traditional + Wapiti) first, then adaptive tool choice. Strategy 1 (Firecrawl first) is commit `b705786`.

## Commands

All `make` targets use `.venv/bin/python3` / `.venv/bin/pytest` with `PYTHONPATH=.`, so the venv must exist (`uv venv .venv && uv pip install -r requirements.txt`).

```bash
make test                     # pytest -v tests/
PYTHONPATH=. .venv/bin/pytest tests/test_tools.py::test_zap_host_alias_mapping -v   # single test
make up / make down           # start/stop ZAP daemon (docker, exposed on 127.0.0.1:18080)
make run                      # run_mvp.py --mode adaptive
make zap-only | wapiti-only | firecrawl-only   # single-tool baselines -> output/experiments/<name>/
make evaluate                 # score output/normalized_endpoints.json vs ground truth
make clean                    # removes output/temp, output/logs, storage/*.db
python3 run_mvp.py --mode adaptive --max-iter 6 --config configs/app_config.json
```

No linter/formatter is configured.

## Architecture

`AdaptiveOrchestrator.run` (`orchestrator/loop.py`) runs in two phases:

1. **Bootstrap.** This phase is deterministic and makes no LLM call.
   - **Round 1:** the `orchestrator.bootstrap.tools` (default: ZAP traditional, ZAP Ajax, Wapiti) crawl `base_url` plus `target.seed_urls`.
   - **Round 2, exchange (`exchange_round`):** every tool is re-seeded with ALL URLs found by the other engine(s) that it hasn't visited (ZAP ← Wapiti, Wapiti ← ZAP). There is no seed cap, and the seeds are computed before the round runs.
   - **Lanes (`_run_plan`):** tools of the same engine (`engine_of`) run sequentially in one lane, because they share ZAP's message history. With `parallel: true`, the different engines' lanes run concurrently.
   - Unavailable tools are skipped.
2. **Adaptive rounds.** `_decide` builds the YAML context (`orchestrator/compactor.py`) and asks `QwenClient` (`llm/qwen_client.py`). The loop uses `HeuristicPolicy.decide` (`orchestrator/policy.py`) instead when the LLM is not configured, the call fails, or the LLM picks a tool that isn't available. `SafetyFirewall` filters the seeds. `ToolRegistry.run` executes the tool. `_ingest` normalizes the results and marks the coverage matrix. The loop stops on `terminate`, or after `early_stop_patience` consecutive adaptive rounds below `marginal_gain_threshold` (bootstrap rounds don't count).

Key boundaries:
- **`tools/registry.py`** is the only place that builds tool wrappers from config, for both the loop and the baselines in `run_mvp.py`. `available_tools()` reports what can actually run: ZAP alive, the Wapiti binary found (also next to the venv interpreter), Firecrawl with an API key that can reach the target. Every wrapper returns a list of `{url, method, status_code, source}` dicts. `source` is the tool name, and `status_code: 0` means unknown. Each wrapper exposes `last_request_count`.
- **`orchestrator/policy.py`**: `classify_surface` derives `spa_likelihood` from generic signals: hash routes, API-heavy surface, few HTML pages. The policy order is: Ajax Spider for SPAs → Firecrawl on auth-walled (3xx/401/403) seeds → Wapiti/ZAP on frontier found by other tools → one Ajax pass for non-SPAs → terminate. Each tool is capped at `max_runs_per_tool`.
- **Tool names** (`ToolActionEnum` values) are used for the coverage matrix, history rows, and result `source` labels. `SOURCE_TOOL_ALIASES` in `storage/state_store.py` maps the legacy short names.

### ZAP / Wapiti / Firecrawl specifics

- ZAP runs in Docker. `tools.zap.target_host_alias` (`host.docker.internal`) rewrites localhost seeds for ZAP, and results are mapped back. `collect_messages` only reads history after the run's start offset, filtered to the target origin. Traditional spider: all seeds are primed into the Sites tree (`core.access_url`), then ONE recursive scan per origin root runs, so seeds are cheap and unlimited. `HTTP/1.0 0` placeholder messages (Sites-tree parent nodes) are ignored. `post_form` is false, so the spider never submits POST forms. The Ajax Spider options (browsers, event wait, duration) are in `tools.zap.ajax_spider`.
- Wapiti: `headless: "hidden"` (the default) uses Playwright Firefox, which requires `.venv/bin/playwright install firefox`. It executes the SPA, and without it Wapiti finds almost nothing on JS apps. Hash-route seeds are dropped when headless is off.
- Time limits: ZAP `spider.max_duration_min` / `ajax_spider.max_duration_min` set to `0`/`null`, or Wapiti `max_scan_time: null`, mean no limit (the tool runs until its crawl finishes). `cookie_file` is the hook for a manually prepared authenticated session.
- Firecrawl: the prompt is a generic template (`build_agent_prompt`) built from seeds plus `focus`, with an optional `auth_instructions` string from config. No credentials or routes are hardcoded. Without a key, or for a localhost target without `PUBLIC_TUNNEL_URL`, it is unavailable. `fallback_routes_file` optionally replays a recorded URL list for offline experiments.

### Canonical endpoint identity

`normalize_endpoint` (`normalization/normalizer.py`) takes a `NormalizationProfile` built from config via `NormalizationProfile.from_config`. The generic rules: numeric, UUID, hex-hash and opaque-token segments, plus JS `undefined`/`null`, become `{id}`/`{hash}`/`{token}`. Query values become `{key}`. URLs on other hosts are dropped, and alias hosts are mapped to `base_url`'s origin. SPA hash routes (`/#/x`, `/#!/x`) become `GET #/x`. A plain `#anchor` is ignored. Target-specific rewrites go in the config's `normalization.path_rules`, `query_aliases`, `trailing_slash_dirs` and `junk_substrings`, never in code. `canonical_hash = SHA256(canonical_key)`. Evaluation compares only these hashes, so **changing rules or profiles changes hashes**. `ground_truth/avideo_routes.json` was generated with the old AVideo-specific rules.

### State store (`storage/state_store.py`)

Tables:
- `endpoints`
- `endpoint_sources` (provenance)
- `tool_coverage` (endpoint × tool, `PENDING`/`PROCESSED`)
- `execution_history` (one row per tool run, including `requests_sent`; bootstrap rows share an iteration number)

`get_pending_frontier(tool, limit=None|n, statuses, sources)` returns a concrete raw URL observed by a crawler, never the `{id}` template. It returns only GET endpoints, skips 404/405/410/5xx, and lists 2xx first. `sources` filters by provenance; this is used by the exchange round. `seed_limit: null` in config means no limit. `get_surface_profile` feeds SPA detection. The DB persists between runs, so use `make clean` for a fresh run.

### Configuration

Runtime parameters come from `configs/app_config.json`. `.env` (loaded by `run_mvp.py`) provides `DASHSCOPE_API_KEY`, `DASHSCOPE_BASE_URL`, `QWEN_MODEL`, `FIRECRAWL_API_KEY`, `PUBLIC_TUNNEL_URL` and the target credentials `AUTH_USER`/`AUTH_PASSWORD`. Other variables in `.env.example` are not read.

### Authentication (`orchestrator/auth.py`)

`prepare_auth` runs once when `ToolRegistry.from_config` is called. It runs only if the `auth` section has `enabled: true`. It can perform a login request (JSON or form) and extract a token by `token_path`. Cookies from the response's Set-Cookie are kept. It returns an `AuthContext` with the headers and cookies to send. `${VAR}` placeholders resolve from `.env` and the fetched token, so secrets never go into the tracked config. If login fails or a placeholder is unresolved, the crawl continues unauthenticated (an error is logged).

How each tool receives auth:
- ZAP: Replacer rules (`dast-orchestrator-auth:*`) limited to the target origin. They cover both spiders, including the Ajax Spider's browser traffic.
- Wapiti: `-H` / `-C`.
- Firecrawl: `tools.firecrawl.auth_instructions`, with placeholders expanded.

ZAP and Wapiti can't set the SPA's `localStorage`. API calls are authenticated, but menus that the UI shows only after login may not render.
