#!/usr/bin/env bash
# 2026-10-23 23:30: PART 3. full-pipeline/run-03 was aborted at 23:29 because Firecrawl ran out of
# credits (402, balance 11). firecrawl-only run 2 and one full-pipeline run need new credits.
# This runs the three remaining runs that do not use Firecrawl, in the planned order.
# Earlier: RESUME of bench_series_maze.sh: full-pipeline/run-01 was aborted at 21:57 (Firecrawl client
# KeyError 'first_request_at' on a job with no tunnel traffic; fixed). Remaining 6 runs, same order:
# full-pipeline x2, then E D C B A as planned. Original header:
# Security Crawl Maze, 5 configurations x 2 runs. Order: A B C D E, then E D C B A (balanced).
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
run zap-wapiti "${ZAP_WAPITI[@]}"
run wapiti-only "${WAPITI_ONLY[@]}"
run zap-only "${ZAP_ONLY[@]}"
echo "=== $(date -Is) done ==="
