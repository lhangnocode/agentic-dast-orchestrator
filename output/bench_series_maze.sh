#!/usr/bin/env bash
# 2026-10-23: Security Crawl Maze, 5 configurations x 2 runs. Order: A B C D E, then E D C B A (balanced).
# Every run uses the tunnel logging proxy on :3457 (only Firecrawl reaches the tunnel), so the setup is
# identical for all configurations. Before each Firecrawl run the tunnel is checked end to end.
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=.
PY=.venv/bin/python3
ZAP_ONLY=(--set tools.wapiti.enabled=false --set tools.firecrawl.enabled=false --set tools.http_probe.enabled=false)
WAPITI_ONLY=(--set tools.zap.enabled=false --set tools.firecrawl.enabled=false --set tools.http_probe.enabled=false)
ZAP_WAPITI=(--set tools.firecrawl.enabled=false --set tools.http_probe.enabled=false)
FC_ONLY=(--set tools.zap.enabled=false --set tools.wapiti.enabled=false --set tools.http_probe.enabled=false
         --set 'orchestrator.stages=[["firecrawl_navigate"]]')
FULL=()
TUNNEL=$(grep '^PUBLIC_TUNNEL_URL=' .env | cut -d= -f2-)

check_tunnel() {
  $PY -m tools.traffic_proxy --listen 3457 --upstream http://localhost:8090 --log /dev/null >/dev/null 2>&1 &
  local pid=$!; sleep 3
  echo "tunnel check $(date -Is): cloudflared=$(pgrep -c cloudflared) $TUNNEL/ -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$TUNNEL/")"
  kill $pid; wait $pid 2>/dev/null
}
run() {
  local label=$1; shift
  case $label in firecrawl-only|full-pipeline) check_tunnel;; esac
  echo "=== $(date -Is) $label ==="
  $PY -m evaluation.run_benchmark --benchmark crawl-maze --runs 1 --tunnel-proxy 3457 --label "$label" "$@"
  echo "=== exit $? $(date -Is) ==="
}
run zap-only "${ZAP_ONLY[@]}"
run wapiti-only "${WAPITI_ONLY[@]}"
run zap-wapiti "${ZAP_WAPITI[@]}"
run firecrawl-only "${FC_ONLY[@]}"
run full-pipeline "${FULL[@]}"
run full-pipeline "${FULL[@]}"
run firecrawl-only "${FC_ONLY[@]}"
run zap-wapiti "${ZAP_WAPITI[@]}"
run wapiti-only "${WAPITI_ONLY[@]}"
run zap-only "${ZAP_ONLY[@]}"
echo "=== $(date -Is) done ==="
