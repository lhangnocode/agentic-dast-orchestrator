#!/bin/bash
# Crawlground series: ZAP+Wapiti only, Firecrawl only, full pipeline (3 runs each, ZAP restarted per run)
set -x
B="env PYTHONPATH=. .venv/bin/python3 -m evaluation.run_benchmark --benchmark crawlground --runs 3"
$B --label zap-wapiti --set tools.firecrawl.enabled=false --set tools.http_probe.enabled=false
$B --label firecrawl-only --set tools.zap.enabled=false --set tools.wapiti.enabled=false \
   --set tools.http_probe.enabled=false --set 'orchestrator.stages=[["firecrawl_navigate"]]'
$B --label full-pipeline
echo SERIES_DONE
