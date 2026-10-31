#!/usr/bin/env bash
# 2026-10-23 (second batch): ZAP only and Wapiti only, runs 3-4 each, alternating, after fixing the startup
# measurement (ZAP: stale message from the previous run; Wapiti: crawl lines go to its --log file).
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=.
PY=.venv/bin/python3
ZAP_ONLY=(--set tools.wapiti.enabled=false --set tools.firecrawl.enabled=false --set tools.http_probe.enabled=false)
WAPITI_ONLY=(--set tools.zap.enabled=false --set tools.firecrawl.enabled=false --set tools.http_probe.enabled=false)
run() { echo "=== $(date -Is) $1 ==="; $PY -m evaluation.run_benchmark --benchmark crawlground --runs 1 --label "$@"; echo "=== exit $? ==="; }
run zap-only "${ZAP_ONLY[@]}"
run wapiti-only "${WAPITI_ONLY[@]}"
run zap-only "${ZAP_ONLY[@]}"
run wapiti-only "${WAPITI_ONLY[@]}"
echo "=== $(date -Is) done ==="
