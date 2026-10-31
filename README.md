# Agentic DAST Orchestrator

> **Research Prototype**: Adaptive Web Crawling and Endpoint Discovery for Dynamic Application Security Testing (DAST)

## 1. Overview & Architecture

The **Agentic DAST Orchestrator** is an autonomous crawler orchestration framework designed to address the coverage vs. redundancy dilemma in modern web security testing. Rather than executing crawlers in isolated silos or static pipelines, this architecture runs a **local deterministic state machine**: fixed stages, then rules that select the next crawler from observed crawl state (no LLM in tool selection):

```text
                        ┌────────────────────────┐
                        │      User / CLI        │
                        └───────────┬────────────┘
                                    │
                                    ▼
                        ┌────────────────────────┐
                        │ Local Python           │ ◄───────► Relational SQLite
                        │ Orchestrator           │           State Store (U x T)
                        └───────────┬────────────┘
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          │                         │                         │
          ▼                         ▼                         ▼
  ┌───────────────┐         ┌───────────────┐         ┌───────────────┐
  │ Firecrawl     │         │ OWASP ZAP     │         │ Wapiti        │
  │ Cloud Agent   │         │ Spider & Ajax │         │ Form Crawler  │
  └───────────────┘         └───────────────┘         └───────────────┘
          │                         │                         │
          └─────────────────────────┼─────────────────────────┘
                                    │
                                    ▼
                      ┌───────────────────────────┐
                      │ Normalization Engine      │ (Rule 1: Junk filtering)
                      │ & Canonical Hash Registry │ (Rule 2: Multi-source dedup)
                      └─────────────┬─────────────┘ (Rule 3: Path generalization)
                                    │
                                    ▼
                      ┌───────────────────────────┐
                      │ Deterministic tool policy │ (observed signals → next tool)
                      └───────────────────────────┘
```

### Key Capabilities
- **Staged pipeline + deterministic rules (no LLM)**: stage 1 runs ZAP traditional spider → ZAP Client Spider in parallel with Wapiti; stage 2 runs Firecrawl on everything stage 1 found; afterwards a rule engine (`orchestrator/rules.py`) re-runs a tool whenever its unvisited frontier changed, retries failed runs, probes unknown statuses, and stops at saturation. See `docs/tool-capabilities.md`.
- **SPA routes via the ZAP Client Spider**: the Client Map export (parsed robustly, ZAP can emit invalid YAML) yields `#/` routes that HTTP-level crawlers never see; ZAP browser-based login lets it crawl logged-in views.
- **Firecrawl prompt templates**: `configs/prompts/firecrawl_agent.md` (no per-target hints) + a summary of what is already known; Firecrawl reports visited / reached-by-interaction / only-seen URLs separately; safety rules are enforced by code.
- **Parameter & Form Specialist (Wapiti)**: forms and parameters, crawl-only (`-m ""`), headless for SPAs.
- **Strict Normalization**: junk/trap filtering, multi-source dedup, canonical templates (`SHA256(Method + Path_Template + Query_Keys)`), SPA hash routes as `GET #/route`.
- **Benchmarks**: pinned Juice Shop, Crawlground and Security Crawl Maze with ground truth built from source, repeated isolated runs, recall per kind with 95% CIs (`docs/benchmarks.md`).

---

## 2. Directory Structure

```text
agentic-dast-orchestrator/
├── benchmarks/                  # docker-compose (pinned targets) + per-benchmark profiles
├── configs/
│   ├── app_config.json          # Target, auth, stages/rules, normalization, tool options
│   └── prompts/firecrawl_agent.md  # Firecrawl prompt template
├── docs/                        # tool-capabilities.md (rule derivation), benchmarks.md (protocol)
├── evaluation/
│   ├── ground_truth.py          # Ground-truth builders (pinned sources)
│   ├── ground_truth/            # Generated ground-truth files
│   ├── crawl_metrics.py         # Run metrics + aggregation (mean, SD, 95% CI)
│   ├── run_benchmark.py         # Repeated, isolated benchmark runs
│   ├── benchmark_runner.py      # Legacy AVideo evaluator
│   └── metrics.py               # Legacy recall/precision helpers
├── normalization/               # Normalizer + generic patterns
├── orchestrator/
│   ├── auth.py                  # Login + header/cookie distribution
│   ├── loop.py                  # Stages, rule rounds, ingest, export
│   ├── rules.py                 # Snapshot + rule engine (R1–R7)
│   ├── run_log.py               # JSONL run events
│   ├── policy.py                # Surface signals (SPA likelihood) for logging
│   ├── decision_schema.py       # Tool names
│   └── safety_firewall.py       # Scope / logout / SSRF filtering of seeds
├── storage/state_store.py       # SQLite: endpoints, provenance, coverage matrix, history
├── tools/
│   ├── zap_wrapper.py           # ZAP traditional / Client / AJAX spiders, browser login
│   ├── client_map.py            # Client Map export parser
│   ├── wapiti_wrapper.py        # Wapiti crawl-only
│   ├── firecrawl_client.py      # Firecrawl Agent (batched jobs)
│   ├── firecrawl_prompt.py      # Prompt rendering, limits, safety section
│   ├── http_probe.py            # Status/content-type probe
│   └── registry.py              # Builds tools from config, uniform run()
├── tests/                       # Unit + end-to-end tests (fake tools)
├── docker-compose.yml           # ZAP daemon (+ orchestrator image)
├── Makefile
└── run_mvp.py                   # CLI entrypoint
```

---

## 3. Quickstart

### Option A: Local Virtual Environment (Recommended)

1. **Activate Environment & Install Dependencies**:
   ```bash
   uv venv .venv
   source .venv/bin/activate
   uv pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   Copy `.env.example` to `.env` and configure your API keys:
   ```bash
   cp .env.example .env
   ```
   - `FIRECRAWL_API_KEY`: Your Firecrawl Cloud API key (optional; without it Firecrawl is simply skipped. For a localhost target it also needs `PUBLIC_TUNNEL_URL`).
   - `ZAP_API_URL`: `http://127.0.0.1:18080`.

3. **Start Local ZAP Daemon**:
   ```bash
   make up
   ```

4. **Run Unit Tests**:
   ```bash
   make test
   ```

5. **Run Adaptive Crawl Loop**:
   ```bash
   make run
   ```

6. **Evaluate Results Against Ground Truth**:
   ```bash
   make evaluate
   ```

---

## 4. CLI Execution Modes

The `run_mvp.py` CLI supports both the full adaptive orchestrator and isolated baseline scans for empirical comparison:

```bash
# 1. Full Adaptive Orchestration (ZAP + Wapiti + Firecrawl, deterministic tool selection)
python3 run_mvp.py --mode adaptive --max-iter 6

# 2. Standalone ZAP Baseline (Traditional + Client Spider)
python3 run_mvp.py --mode zap-only

# 3. Standalone Wapiti Baseline (Crawl-only -m "")
python3 run_mvp.py --mode wapiti-only

# 4. Standalone Firecrawl Baseline (Agent Navigation)
python3 run_mvp.py --mode firecrawl-only
```

---

## 5. Scientific Benchmark Metrics

Evaluation uses the mathematical formulas established in the research methodology:

- **Endpoint Recall**:
  $$R_{EP} = \frac{|E_{discovered} \cap E_{ground\_truth}|}{|E_{ground\_truth}|}$$
- **Endpoint Precision**:
  $$P_{EP} = \frac{|E_{discovered} \cap E_{ground\_truth}|}{|E_{discovered}|}$$
- **F1 Score**:
  $$F_1 = 2 \cdot \frac{P_{EP} \cdot R_{EP}}{P_{EP} + R_{EP}}$$
- **Request Efficiency**:
  $$E_{REQ} = \frac{|E_{discovered}|}{N_{requests}}$$
- **Redundancy Ratio**:
  $$RR = 1 - \frac{|E_{discovered}|}{N_{requests}}$$
