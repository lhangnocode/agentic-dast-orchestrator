# Writes full-pipeline/run-NN/new-urls.md: tools per iteration and every new endpoint (★ first find of an expected resource).
# Usage: PYTHONPATH=. .venv/bin/python3 output/new_urls_maze.py run-02 run-04
import json,glob,sys,sqlite3
from collections import defaultdict
gt={e['canonical_key'].split(' ',1)[1].split('?')[0] for e in json.load(open('evaluation/ground_truth/crawl-maze.json'))['entries']}
SHORT={'zap_traditional_spider':'ZAP trad.','zap_client_spider':'ZAP Client','wapiti_form_crawl':'Wapiti','firecrawl_navigate':'Firecrawl','http_probe':'Probe'}
for run in sys.argv[1:]:
    B=f'output/benchmarks/crawl-maze/full-pipeline/{run}/'
    traces=[json.load(open(f)) for f in glob.glob(B+'tool_runs/*.json')]
    db=sqlite3.connect(B+'state.db'); order=[r[0] for r in db.execute('select chosen_tool from execution_history order by id')]
    hist=list(db.execute('select iteration, chosen_tool from execution_history order by id'))
    traces.sort(key=lambda t: hist.index((t['iteration'],t['tool'])))
    by=defaultdict(list)
    for t in traces: by[t['iteration']].append(t)
    out=[f'# Crawl Maze / full-pipeline / {run}: tools and new URLs per iteration','',
         'Each iteration lists the tools the orchestrator ran (rule, number of seeds) and every endpoint that was new to the store after that tool. ★ = expected resource (ground truth) found for the first time; ☆ = another endpoint on an expected-resource path that was already found (other method or query). Generated from `tool_runs/*.json`.','']
    total=0; gtot=0; seen=set()
    for it in sorted(by):
        out.append(f'## Iteration {it}')
        for t in by[it]:
            new=t['new_endpoints']; P=lambda k:k.split(' ',1)[1].split('?')[0]
            g=[]
            for k in new:
                if P(k) in gt and P(k) not in seen: g.append(k); seen.add(P(k))
            total+=len(new); gtot+=len(g)
            out.append(f"- **{SHORT[t['tool']]}** ({t['rule_id']}, {len(t['seeds'])} seed(s), {t['duration_seconds']:.0f} s): {len(new)} new endpoint(s), {len(g)} expected resource(s) found first")
            for k in new:
                out.append(f"  - {'★ ' if k in g else ('☆ ' if P(k) in gt else '')}`{k}`")
        out.append('')
    out.append(f'Total: {total} new endpoints, {gtot} expected resources.')
    open(B+'new-urls.md','w').write('\n'.join(out)+'\n')
    print(run, total, gtot)
