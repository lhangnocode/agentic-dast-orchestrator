.PHONY: test run evaluate zap-only wapiti-only firecrawl-only up down clean bench-up bench-down bench-gt bench

PYTHONPATH := .
VENV_PYTHON := .venv/bin/python3
VENV_PYTEST := .venv/bin/pytest

test:
	PYTHONPATH=$(PYTHONPATH) $(VENV_PYTEST) -v tests/

run:
	PYTHONPATH=$(PYTHONPATH) $(VENV_PYTHON) run_mvp.py --mode adaptive

zap-only:
	PYTHONPATH=$(PYTHONPATH) $(VENV_PYTHON) run_mvp.py --mode zap-only

wapiti-only:
	PYTHONPATH=$(PYTHONPATH) $(VENV_PYTHON) run_mvp.py --mode wapiti-only

firecrawl-only:
	PYTHONPATH=$(PYTHONPATH) $(VENV_PYTHON) run_mvp.py --mode firecrawl-only

evaluate:
	PYTHONPATH=$(PYTHONPATH) $(VENV_PYTHON) evaluation/benchmark_runner.py --input output/normalized_endpoints.json

up:
	mkdir -p output/zap
	docker compose up -d zap

down:
	docker compose down

clean:
	rm -rf output/temp output/logs storage/*.db .pytest_cache
	find . -type d -name "__pycache__" -exec rm -rf {} +

# Benchmarks (docs/benchmarks.md). Example: make bench BENCH=juice-shop RUNS=5 LABEL=client-spider
BENCH ?= juice-shop
RUNS ?= 5
LABEL ?= default

bench-up:
	docker compose -f benchmarks/docker-compose.yml up -d --build

bench-down:
	docker compose -f benchmarks/docker-compose.yml down

bench-gt:
	PYTHONPATH=$(PYTHONPATH) $(VENV_PYTHON) -m evaluation.ground_truth $(BENCH)

bench:
	PYTHONPATH=$(PYTHONPATH) $(VENV_PYTHON) -m evaluation.run_benchmark --benchmark $(BENCH) --runs $(RUNS) --label $(LABEL)
