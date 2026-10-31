#!/usr/bin/env bash
# PART 4 (to run after adding a Firecrawl API key with credits): the two Crawl Maze runs that still need
# Firecrawl, so that every configuration has exactly 2 complete runs.
#   - firecrawl-only  run 2   (becomes firecrawl-only/run-02)
#   - full-pipeline   1 run   (becomes full-pipeline/run-04; run-01 and run-03 were aborted and are kept)
# Same overrides, tunnel proxy and checks as bench_series_maze.sh. The script starts its own Cloudflare
# quick tunnel, writes its URL to .env (PUBLIC_TUNNEL_URL), verifies the target through it, runs, then
# stops the tunnel and rebuilds the summary.
#
#   ./output/bench_series_maze_part4.sh > output/bench_series_maze_part4.out 2>&1
#
# Expected Firecrawl cost: roughly 1,000+ credits (full-pipeline/run-02 reported >= 673, not all jobs report it).
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH=.
PY=.venv/bin/python3
FC_ONLY=(--set tools.zap.enabled=false --set tools.wapiti.enabled=false --set tools.http_probe.enabled=false
         --set 'orchestrator.stages=[["firecrawl_navigate"]]')
FULL=()

# 0. Preconditions: containers, API key, credits
docker compose -f benchmarks/docker-compose.yml up -d crawl-maze >/dev/null 2>&1
docker compose up -d zap >/dev/null 2>&1
KEY=$(grep '^FIRECRAWL_API_KEY=' .env | cut -d= -f2-)
echo "credits $(date -Is): $(curl -s -H "Authorization: Bearer $KEY" https://api.firecrawl.dev/v2/team/credit-usage)"

# 1. New quick tunnel -> logging proxy port 3457; its URL goes into .env
pkill -x cloudflared 2>/dev/null
: > output/cloudflared.log
setsid nohup cloudflared tunnel --no-autoupdate --url http://localhost:3457 > output/cloudflared.log 2>&1 &
TUNNEL=""
for _ in $(seq 1 60); do
  TUNNEL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' output/cloudflared.log | head -1)
  [ -n "$TUNNEL" ] && break; sleep 1
done
[ -z "$TUNNEL" ] && { echo "tunnel did not start"; exit 1; }
sed -i "s#^PUBLIC_TUNNEL_URL=.*#PUBLIC_TUNNEL_URL=$TUNNEL#" .env
trap 'pkill -x cloudflared; echo "tunnel stopped $(date -Is)"' EXIT
sleep 10   # DNS for a new quick tunnel takes a few seconds

check_tunnel() {
  $PY -m tools.traffic_proxy --listen 3457 --upstream http://localhost:8090 --log /dev/null >/dev/null 2>&1 &
  local pid=$!; sleep 3
  local code; code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$TUNNEL/")
  echo "tunnel check $(date -Is): cloudflared=$(pgrep -c cloudflared) $TUNNEL/ -> $code"
  kill $pid; wait $pid 2>/dev/null
  [ "$code" = "200" ]
}
run() {
  local label=$1; shift
  check_tunnel || { echo "tunnel not reachable, not starting $label"; exit 1; }
  echo "=== $(date -Is) $label ==="
  $PY -m evaluation.run_benchmark --benchmark crawl-maze --runs 1 --tunnel-proxy 3457 --label "$label" "$@"
  echo "=== exit $? $(date -Is) ==="
}
run firecrawl-only "${FC_ONLY[@]}"
run full-pipeline "${FULL[@]}"
echo "credits $(date -Is): $(curl -s -H "Authorization: Bearer $KEY" https://api.firecrawl.dev/v2/team/credit-usage)"
$PY output/consolidate_maze.py
echo "=== $(date -Is) done ==="
