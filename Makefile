.PHONY: test run evaluate zap-only wapiti-only firecrawl-only up down clean

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
	docker compose up -d zap

down:
	docker compose down

clean:
	rm -rf output/temp output/logs storage/*.db .pytest_cache
	find . -type d -name "__pycache__" -exec rm -rf {} +
