# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Research prototype for adaptive endpoint discovery in DAST: an LLM (Qwen via Alibaba DashScope) picks which crawler (Firecrawl Cloud agent, OWASP ZAP spider/Ajax spider, Wapiti crawl-only) to run next, and results are normalized into canonical endpoints stored in SQLite. The benchmark target is a local AVideo CMS instance at `http://localhost/`.

## Commands

All `make` targets use `.venv/bin/python3` / `.venv/bin/pytest` with `PYTHONPATH=.`, so the venv must exist (`uv venv .venv && uv pip install -r requirements.txt`).

```bash
make test                     # pytest -v tests/
PYTHONPATH=. .venv/bin/pytest tests/test_tools.py::test_name -v   # single test
make up / make down           # start/stop ZAP daemon (docker, exposed on 127.0.0.1:18080)
make run                      # run_mvp.py --mode adaptive
make zap-only | wapiti-only | firecrawl-only   # single-tool baselines
make evaluate                 # score output/normalized_endpoints.json vs ground truth
make clean                    # removes output/temp, output/logs, storage/*.db
python3 run_mvp.py --mode adaptive --max-iter 6 --config configs/app_config.json
```

No linter/formatter is configured.

## Architecture

The loop in `orchestrator/loop.py` (`AdaptiveOrchestrator.run`) is the center; each iteration:

1. `StateCompactor` (`orchestrator/compactor.py`) builds a small YAML summary of the SQLite state for the prompt.
2. `QwenClient.get_decision` (`llm/qwen_client.py`) returns an `OrchestratorDecision` (`orchestrator/decision_schema.py`) via OpenAI-compatible structured parsing. The `ToolActionEnum` values (`firecrawl_navigate`, `zap_traditional_spider`, `zap_ajax_spider`, `wapiti_form_crawl`, `terminate`) are also the tool names used in the coverage matrix.
3. `SafetyFirewall.sanitize_seeds` drops LLM-proposed seeds outside `allowed_hosts` (SSRF guard).
4. `_dispatch_tool` calls the wrapper in `tools/`; every wrapper returns a list of `{url, method, status_code, source}` dicts and swallows errors (returns `[]`).
5. Each result goes through `normalization.normalizer.normalize_endpoint` and `StateStore.insert_or_update_endpoint`.
6. Marginal gain = new endpoints / endpoints before. The loop stops early after 2 consecutive rounds below `marginal_gain_threshold` (only from iteration 3 on), or when the LLM returns `terminate`.
7. At the end it exports `output/normalized_endpoints.json` and `output/normalized_discovered_urls.txt`. Baseline modes write to `output/experiments/<name>/` instead.

### Canonical endpoint identity

Endpoint identity is `canonical_hash = SHA256("METHOD /generalized/path?key1={key1}&...")` from `compute_canonical_key`. Path generalization and junk/trap filtering use the regexes in `normalization/patterns.py`. Hosts `127.0.0.1` and `host.docker.internal` are rewritten to `localhost`, and `normalized_url` is always rebuilt on `http://localhost`. Evaluation (`evaluation/metrics.py`) only compares hashes against `ground_truth/avideo_routes.json`. **Any change to normalization rules changes hashes, so check that ground-truth entries still match** (recall/precision depend on it).

### State store (`storage/state_store.py`)

SQLite tables: `endpoints`, `endpoint_sources` (provenance per tool), `tool_coverage` (endpoint × tool matrix, `PENDING`/`PROCESSED`), and `execution_history`. When a new endpoint is inserted, a row is created for each of the 4 tools; the discovering tool is marked `PROCESSED` (short source names like `zap`/`firecrawl` are mapped to the enum names). `get_pending_frontier(tool)` gives the next seeds for that tool. `_init_db` includes an in-place migration for `execution_history`. The DB lives at `storage/orchestrator_state.db` and **persists between runs**; if it is not empty, the root URL is not seeded again. Use `make clean` for a fresh run.

### Offline / fallback behavior (important for tests)

The system is built to run without any external services:
- No `DASHSCOPE_API_KEY` (or the placeholder value), or an API error → `QwenClient._heuristic_decision`, a fixed schedule: Firecrawl → ZAP spider → Wapiti → ZAP Ajax → terminate.
- No `FIRECRAWL_API_KEY`, or a localhost target without `PUBLIC_TUNNEL_URL` → `FirecrawlClient._get_fallback_routes()` returns hardcoded AVideo routes (`mock_fallback=True` is always set by callers). With `PUBLIC_TUNNEL_URL`, localhost URLs are rewritten to the tunnel, and the tunnel host is added to `allowed_hosts`.
- ZAP unreachable → spiders return `[]` (the e2e test points ZAP at an unused port on purpose).

### Configuration

Runtime parameters come from `configs/app_config.json` (target, budgets, tool options, DB path). From `.env` (loaded in `run_mvp.py`), only these are actually read: `DASHSCOPE_API_KEY`, `DASHSCOPE_BASE_URL`, `QWEN_MODEL`, `FIRECRAWL_API_KEY`, and `PUBLIC_TUNNEL_URL`. Other variables in `.env.example` (`MAX_ITERATIONS`, `TARGET_URL`, `ZAP_API_URL`, …) are not read by Python code. `configs/login_config.json` is not referenced by any Python code right now.
