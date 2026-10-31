#!/usr/bin/env bash
# 2026-10-23: single-tool baselines (ZAP only, Wapiti only; 2 runs each, alternating) + Firecrawl-only run 4
# with the tunnel logging proxy. Runs are appended to their labels; nothing existing is overwritten.
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=.
PY=.venv/bin/python3
ZAP_ONLY=(--set tools.wapiti.enabled=false --set tools.firecrawl.enabled=false --set tools.http_probe.enabled=false)
WAPITI_ONLY=(--set tools.zap.enabled=false --set tools.firecrawl.enabled=false --set tools.http_probe.enabled=false)
FC_ONLY=(--set tools.zap.enabled=false --set tools.wapiti.enabled=false --set tools.http_probe.enabled=false
         --set 'orchestrator.stages=[["firecrawl_navigate"]]')
run() { echo "=== $(date -Is) $1 ==="; $PY -m evaluation.run_benchmark --benchmark crawlground --runs 1 --label "$@"; echo "=== exit $? ==="; }
run zap-only "${ZAP_ONLY[@]}"
run wapiti-only "${WAPITI_ONLY[@]}"
run zap-only "${ZAP_ONLY[@]}"
run wapiti-only "${WAPITI_ONLY[@]}"
run firecrawl-only "${FC_ONLY[@]}" --tunnel-proxy 3457
echo "=== $(date -Is) done ==="
