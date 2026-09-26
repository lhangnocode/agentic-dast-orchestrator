# Agentic DAST Orchestrator

> **Research Prototype**: Adaptive Web Crawling and Endpoint Discovery for Dynamic Application Security Testing (DAST)

## 1. Overview & Architecture

The **Agentic DAST Orchestrator** is an autonomous crawler orchestration framework designed to address the coverage vs. redundancy dilemma in modern web security testing. Rather than executing crawlers in isolated silos or static pipelines, this architecture pairs a **Local Deterministic State Machine** with a **Cloud Cognitive Decision Layer** (Alibaba Cloud Model Studio / Qwen):

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
                      │ Context Compactor (<1k tok│
                      └─────────────┬─────────────┘
                                    │
                                    ▼
                      ┌───────────────────────────┐
                      │ Alibaba Cloud / Qwen-Plus │
                      └───────────────────────────┘
```

### Key Capabilities
- **Semantic UI Pathfinder (Firecrawl Cloud)**: Autonomously navigates authenticated dropdowns, avatars, administrative links, and client-side routes.
- **High-Speed Link Spider (OWASP ZAP)**: Crawls traditional anchor structures and handles dynamic JavaScript DOM events with Ajax Spider.
- **Parameter & Form Specialist (Wapiti)**: Extracts complex multi-field forms and parameter mutations strictly in crawl-only mode (`-m ""`).
- **Cognitive Decision Layer (Qwen-Plus)**: Evaluates marginal gains ($MG$), state saturation, and dynamically steers seed frontiers to the optimal tool.
- **Strict Normalization (Rules 1–3)**: Filters traps/wildcards, deduplicates multi-source HTTP messages, and clusters endpoints into canonical path templates (`SHA256(Method + Path_Template + Query_Keys)`).
- **Independent Benchmark Evaluator**: Evaluates Recall ($R_{EP}$), Precision ($P_{EP}$), $F_1$, and Request Efficiency ($E_{REQ}$) against an independent ground truth.

---

## 2. Directory Structure

```text
agentic-dast-orchestrator/
├── configs/
│   ├── app_config.json          # Target parameters, timeouts, and budget limits
│   └── login_config.json        # AVideo CMS credentials and Playwright selectors
├── evaluation/
│   ├── benchmark_runner.py      # Experiment comparative evaluation runner
│   └── metrics.py               # Recall, Precision, F1, Request Efficiency metrics
├── ground_truth/
│   └── avideo_routes.json       # 112 canonical verified AVideo attack surfaces
├── llm/
│   └── qwen_client.py           # DashScope OpenAI-compatible client & heuristic fallback
├── normalization/
│   ├── normalizer.py            # Rules 1-3 implementation & canonical hashing
│   └── patterns.py              # Junk/trap regexes and generalization templates
├── orchestrator/
│   ├── compactor.py             # YAML state abstract builder (<1,000 tokens)
│   ├── decision_schema.py       # Pydantic schemas for structured LLM decisions
│   ├── loop.py                  # Main adaptive orchestration state machine
│   └── safety_firewall.py       # SSRF prevention and seed whitelist sanitization
├── storage/
│   └── state_store.py           # Relational SQLite state store (Endpoints, Matrix, History)
├── tools/
│   ├── firecrawl_client.py      # Firecrawl /v2/agent polling wrapper
│   ├── wapiti_wrapper.py        # Wapiti crawl-only CLI & SQLite extractor
│   └── zap_wrapper.py           # ZAPv2 Traditional & Ajax Spider API wrapper
├── tests/                       # Complete unit and integration test suite
├── docker-compose.yml           # Containerized ZAP and Orchestrator
├── Dockerfile                   # Orchestrator container image
├── Makefile                     # Make targets for development and execution
├── requirements.txt             # Lightweight Python dependencies
└── run_mvp.py                   # Top-level CLI entrypoint
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
   - `DASHSCOPE_API_KEY`: Your Alibaba Cloud Model Studio API key.
   - `FIRECRAWL_API_KEY`: Your Firecrawl Cloud API key (optional; built-in simulation fallback available).
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
# 1. Full Adaptive Orchestration (Qwen + Firecrawl + ZAP + Wapiti)
python3 run_mvp.py --mode adaptive --max-iter 6

# 2. Standalone ZAP Baseline (Traditional + Ajax Spider)
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
