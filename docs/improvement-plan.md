# Improvement plan: runtime reporting, Firecrawl evidence, crawl storms, seed selection

Status: **proposal, nothing implemented yet** (2026-10-30). It compiles the findings and proposals discussed
after the Crawl Maze and Crawlground series. Decisions still open are listed at the end.
The algorithm the plan changes is described in `docs/tool-selection.md`.

## 1. Baseline these changes are measured against

Every change goes into **new benchmark labels**; the existing runs stay as they are.

| Benchmark | Configuration (runs) | Full-pipeline recall | Summary |
|---|---|---|---|
| Crawl Maze (91 expected resources) | ZAP only, Wapiti only, ZAP + Wapiti, Firecrawl only, Full pipeline (2 each) | 0.901 / 0.956 | `output/benchmarks/crawl-maze/series-summary.md` |
| Crawlground (59 markers) | ZAP only 4, Wapiti only 4, ZAP + Wapiti 3, Firecrawl only 4, Full pipeline 3 | 1.000 | `output/benchmarks/crawlground/series-summary.md` |

Crawl Maze full pipeline: 12 and 18 iterations, 25 and 37 tool runs, 69.3 and 68.1 min, saturation in both.
The last expected resource was found in iteration 3 both times.

## 2. Findings

### 2.1 Runtime reporting

- "Time to saturation" (run start → stop) already includes the orchestrator's own work: 5.6 s and 6.4 s
  in the two Crawl Maze full-pipeline runs.
- Not shown anywhere:
  - environment reset (target restart about 13 s; ZAP restart about 19.5 s is a separate row);
  - orchestrator start-up before the `run_started` event (unmeasured, under 1 s);
  - post-run metrics (about 1 s);
  - an end-to-end total (run-02: about 4,193 s from the benchmark log vs. 4,160 s reported).
- Tool time is a sum over tool runs, so tools that ran in parallel are counted twice (run-02: 4,642 s of
  tool time inside a 4,160 s run).
- Some wrapper work is counted as crawl time: Firecrawl polling delay, ZAP result collection and Client Map
  export, Wapiti report parsing.
- **Memory is not recorded at all.**
- **Firecrawl tokens are not available:** the API returns only `creditsUsed`. That field is missing or 0 for
  some jobs (run-02: 21 of 30 jobs report it, sum 673; run-04: 28 of 34, sum 782).

### 2.2 Firecrawl reports what it saw, not what it did

| Crawl Maze run-02, all Firecrawl runs | Paths |
|---|---:|
| Reported in `links` | 329 |
| Requested by its browser (tunnel log) | 249 |
| Both | 213 |
| Reported but never requested | 116 |
| Requested but never reported | 36 |

- Of its 24 first finds in run-02, its browser requested only 9; the other 15 were read from page content.
- Crawlground Firecrawl-only run 4 failed the other way: the browser requested 7 markers and reported none.
- `actions_taken` claimed clicks the server never received.

**Agreed:** URLs seen only in the tunnel traffic do **not** count as Firecrawl discoveries. The traffic is
used only to verify what Firecrawl actually did.

### 2.3 "Visited" is not truthful

- A tool's seeds are its `PENDING` URLs. After a run, every seed and every reported URL is marked `PROCESSED`
  for that tool. Seeds are therefore never repeated, except by the R1 retry.
- `PROCESSED` is set without proof of a visit:
  - Firecrawl stage 2: 230 seeds handed over, only 159 (run-02) and 155 (run-04) requested, all 230 marked.
  - Seeds of failed or timed-out runs are also marked.
  - Firecrawl's `links` and Client Map nodes that ZAP never visited (the parser ignores the `visited` flag)
    are marked as processed. In Client Spider run 1, 364 of 763 results had no status.
- Seeds are not repeated, but the whole site is:
  - Wapiti always starts at `-u base_url` with `--scope domain` and a fresh session.
  - The ZAP traditional spider runs one recursive scan from the site root over every known node.
  - Each of their re-runs therefore re-crawls the whole site, however few new seeds there are.

### 2.4 Crawling storms

Existing protection:

| Mechanism | Stops |
|---|---|
| Saturation: a tool runs again only if its pending list changed | static surfaces |
| Per-tool coverage | identical re-seeding |
| `max_iterations` 30, `max_runs_per_tool` 10, `max_retries` 1 | unbounded loops (hard ceiling) |
| Normalizer: `{id}`/`{hash}`/`{token}`, query values → `{key}`, URL ≤ 2,048 chars, ≤ 15 path segments, repeated path segments | ID/parameter explosions, path recursion |
| Frontier skips 404/405/410/5xx and non-GET | dead ends |

Why this was not enough (AngularJS trap `/javascript/frameworks/angularjs#/!/#%2F!%2F%23%2F!%2F…`):

- **The trap grows inside the URL fragment.** The repetition checks look only at the path, and the fragment
  uses encoded separators. At one level per round it would take about 200 rounds to reach 2,048 characters.
- **Re-run cost doesn't depend on how many new seeds there are:**

  | Run | Iterations after the last expected resource | Time | Wapiti re-runs (0 new) | ZAP traditional re-runs (0 new) |
  |---|---|---:|---|---|
  | run-02 | 4–12 | 1,019 s (17 min) | 4, 404 s | 4 |
  | run-04 | 4–18 | 1,547 s (26 min) | 7, 694 s | 7 |

- **Seeds a tool can't use:** a `#…` fragment is never sent to the server, so for the ZAP traditional spider
  and Wapiti a trap URL is the same request as the page they already crawled.
- **No unproductive-streak check:** tools with several zero-yield runs in a row still run on the next change.
- **The stop was luck:** run-02 ended when the trap moved to `/#/…` and stopped producing variants, not
  because a guard caught it.

### 2.5 Firecrawl stage 2 is the largest cost

Stage 2 gets every page-like URL from stage 1 (230 seeds, 23 jobs of 10). Expected resources found first,
per job:

```
run-02  jobs 1-6: 1 0 0 0 0 1   jobs 7-14: 5 5 1 5 1 0 3 2   jobs 15-23: 0 0 0 0 0 0 0 0 0
run-04  jobs 1-6: 2 0 0 0 0 2   jobs 7-14: 8 2 7 0 2 4 0 2   jobs 15-23: 0 0 0 0 0 0 0 0 0
```

- **Jobs 15–23 found nothing in either run:** 923 s (run-02) and 664 s (run-04). They are dominated by
  already-found leaf pages (59 of the 230 seeds are `.found` pages).
- **Jobs 2–5 also found nothing**, but the productive jobs come after them. A Maelstrom-style "stop when
  recent yield drops" rule over the job order would have stopped early and lost most of Firecrawl's finds.
  Seeds must be ordered by value before any yield window is used.

### 2.6 Lessons from Maelstrom (feedback-driven crawling)

| Maelstrom | Our counterpart | Use |
|---|---|---|
| Fresh GET (unvisited first) | `PENDING` frontier | only meaningful once "visited" is truthful (A0) |
| Growth | new endpoints per run | yield signal (E) |
| Duplicate candidates | results that were already known | waste signal (C, E) |
| URL diversity (templates ÷ URLs) | normalizer template families | novelty feature (C, F) |
| Novel endpoints (black-box fallback for novel code) | new endpoints, trap URLs excluded | main yield signal |
| Error rate | 4xx/5xx, JavaScript errors | **left out**: error-heavy areas often hold vulnerabilities |
| Window C = 5 vs baseline B = 25, majority vote, cooldown 20, warm-up 100 steps | at most 10 runs per tool | per-tool, short windows: latest run vs. earlier runs; a tool needs ≥ 2 runs before it can be judged |

## 3. Proposals

All rules are generic and set in config. Each one logs the reason it applied and can be switched off, so its
effect is measured by turning it off and on. `.found` is Crawl Maze naming; no rule may depend on it (leaf
pages are detected from the link graph).

### 3.1 Runtime reporting (item T)

- Record phase timestamps per run: environment reset, orchestrator start-up, each iteration, rule selection,
  ingest, export, metrics.
- Report: **end-to-end = environment reset + pipeline wall clock + post-processing**, and pipeline wall clock
  = Σ iteration wall times + orchestrator time, measured directly instead of as a difference.
- Record each tool's tail (last request → tool returns).
- Existing runs: fill what the logs allow (1 s precision) without re-running.
- **Memory:** sample during each tool run: the ZAP container (`docker stats`), the Wapiti process and the
  orchestrator's own process (`psutil`). Firecrawl runs in Firecrawl's cloud and can never be measured.
- **Firecrawl cost:** keep `creditsUsed` for every job and the credit balance before and after each run;
  report credits, not tokens.

### 3.2 Firecrawl reporting (item P2)

**Done 2026-10-31 (simplified):** context files removed; schema = three flat lists `visited_urls`,
`interaction_urls`, `seen_urls`, without `{action, from_page}` details or an action log, to save credits;
`batch_size` stays 10. The traffic-based accuracy metric below is still to do.

- **Schema:** replace `links` with
  - `visited_urls`: pages actually loaded;
  - `interaction_urls`: `{url, action, from_page}` for URLs reached by clicking, submitting, scrolling or a
    redirect;
  - `discovered_urls`: seen in links or source but not opened;
  - structured `actions_taken`: `{page, action, element, result_url}`.
- **Prompt section 10:** strict definitions of the three lists ("list a URL under visited only if you loaded
  it").
- **Verification (traffic only, no discovery):**
  - accuracy metric: share of `visited_urls` and `interaction_urls` that appear in the tunnel log;
  - add `Sec-Fetch-Dest` / `Sec-Fetch-Mode` to the proxy log to tell page loads from background requests;
  - use the Referer to check `from_page` of interaction URLs.
- **Limitation:** verification exists only for localhost targets behind the logging proxy.

### 3.3 A0: truthful "visited" (prerequisite for C and F)

- Coverage states per tool:
  - `PENDING`: not yet given to the tool;
  - `SEEDED`: handed over, no proof of a visit;
  - `VISITED`: the tool returned a status for it, or the tunnel log proves a Firecrawl visit;
  - `SEEN`: only extracted.
- Client Map: use ZAP's `visited` flag. Firecrawl `links` become `SEEN`. Seeds of failed runs stay `SEEDED`
  (R1 still retries them).
- The frontier keeps today's behaviour (`PENDING` only), so this is mostly bookkeeping; C and F read the new
  states.

### 3.4 Rule A: same request for this tool

- `request_key(url, tool)`:
  - drop the fragment for tools that never send it (ZAP traditional, Wapiti, probe);
  - resolve `/./` and `/../`;
  - drop default ports.
- A pending seed whose key is already done for that tool is skipped. The fingerprint is computed on the same
  filtered list, so a skipped seed cannot trigger a re-run.
- Expected effect: the ZAP traditional and Wapiti re-runs triggered by trap variants (section 2.4) do not fire.
- Open choice: resolve `/./` in the normalizer (correct identity, ground truth regenerated) or only in
  `request_key`.
- Risk: low.

### 3.5 Rule B: trap detection

- Decode the fragment up to 3 times (`%23 %2F %21`) and split it on `/ # !`.
- A URL is a trap if any of these hold:
  - a unit of 1–4 tokens repeats at least twice in a row;
  - more than one `#` after decoding;
  - the route is deeper than `D_max` (8) segments;
  - self-similar growth: the URL is a known URL plus another copy of a unit that URL already contains, two
    generations in a row.
- Trap URLs are stored with `trap_reason` (not deleted), kept out of every frontier and fingerprint, and
  precision is reported with and without them.
- Expected effect on Crawl Maze: the AngularJS line stops after its first generation, so saturation around
  iteration 5 instead of 12 or 18. The P0 replay confirms this before implementation.
- Risk: low.

### 3.6 Rule C: seed value score

- **Approach:** deterministic and explainable. Each seed gets a level 1–4 plus a list of reasons; no learned
  weights.
- **Features** (from tool output only, never from tunnel traffic):

  | Feature | Signal | Data | Effect |
  |---|---|---|---|
  | Family novelty | first one or two URLs of a path template or set of query keys | normalizer templates | raises |
  | Interaction evidence | buttons, inputs and forms on the page | Client Map component types; Wapiti forms | raises for browser tools and Firecrawl |
  | Gap for this tool | processed only by tools without this tool's capability (e.g. only the link spider, no JavaScript) | per-tool coverage | raises |
  | Leaf page | visited by a browser tool, 0 outgoing links | new link graph | lowers |
  | Duplicate rate | share of the page's links that were already known | link graph | lowers |
  | Parent yield | the page that linked to it produced new endpoints | link graph + traces | small raise |
  | Status | 2xx > 401/403 > 3xx | endpoints | tie-break |
  | Trap risk | fragment depth, similarity to B's traps | B | excludes |

- **New data:** `endpoint_links(parent_hash, child_hash, tool, kind)`, filled from the ZAP history Referer,
  the Client Map tree and Wapiti's crawl data.
- **Use:**
  - Cheap tools (ZAP traditional, probe) are only ranked, never filtered.
  - Expensive tools use the level for selection (D, F).
- **Code:** `orchestrator/seed_scoring.py` (pure functions), features built in `build_snapshot`, settings
  in `orchestrator.seed_scoring`.

### 3.7 Rule D: re-runs sized to the new seeds

- A first run keeps scope `domain`.
- Re-runs triggered by R4 and R5:
  - ZAP: `spider.scan(seed, recurse=False, subtreeOnly=True)` per seed, with a small depth;
  - Wapiti: `-u <seed> --scope folder` (or `page`) per seed group instead of `-u base_url`.
- Needs a short spike first: how Wapiti scopes extra `-s` seeds relative to `-u`, and ZAP's `subtreeOnly`.
- Risk: medium. A full re-crawl could reach pages only through new server state; check Crawlground and Juice
  Shop runs for re-runs that found something new.

### 3.8 Rule E: unproductive-streak gate (Maelstrom-style, per tool)

- **Signals per run:** novel endpoints (traps excluded), growth, duplicate rate, template diversity. Error
  rate is left out.
- **Vote:** from the tool's 2nd run on, each signal compares the latest run with the mean of the tool's
  earlier runs. A majority below baseline puts the tool on cooldown.
- **On cooldown:** the tool fires only on a material change (at least M seeds left after A and B, or a seed
  from a new template family). One productive run lifts the cooldown; the tool is never disabled for good.
- **Role:** safety net for traps B does not recognise. If A and B already remove the waste, E may stay off
  by default.
- Risk: medium (slight conflict with max recall).

### 3.9 Rule F: Firecrawl seed selection

1. **F1 eligibility:** remove already-visited leaf pages (C), traps (B) and seeds that are the same request
   as a done URL (A).
2. **F2 one per family:** at most k seeds per template family or folder (k = 1–2, configurable).
3. **F3 order by value:** batch by level, so high-value jobs run first.
4. **F4 window stop, only after ordering:** after a warm-up of 3 jobs, stop submitting when the last 2 jobs
   found nothing new while earlier jobs did, and all remaining seeds are level 3 or lower. Skipped seeds are
   marked *deferred*, not done.
5. **F5 visit check (traffic used for verification only):** a seed counts as visited by Firecrawl only if
   the tunnel log shows it was requested; unvisited seeds are offered once more, then given up.

Trade-off: with max recall and no budget, F saves time only if Firecrawl may skip low-value seeds for good.
If deferred seeds always get a final run, the savings disappear.

## 4. Phases

| Phase | Content | Runs / credits |
|---|---|---|
| 0 | Finish the paused Crawl Maze runs on the current code | **done** 2026-10-27 |
| P0 | Offline replay on existing runs: rebuild each run's frontiers, apply A, B, C, E, F step by step, and report per rule: seeds removed, tool runs avoided, estimated time saved, expected resources lost. Seeds are linked to what they produced through traces, the ZAP history and (offline only) tunnel Referers | none |
| P1 | T (runtime and memory reporting), A0, A, B, with tests and docs; regenerate ground truth if the normalizer changes | none until evaluation |
| P2 | Firecrawl schema and prompt, F5 visit checks, accuracy metric | evaluation only |
| P3 | Link graph, C, then D, E, F behind config flags | evaluation only |
| Eval | New labels (`full-pipeline-v2`, then one-rule-off variants) on Crawl Maze, Crawlground and Juice Shop; old runs kept | yes, ask first |

Credits: the current Firecrawl key has about 188 left; full-pipeline/run-04 reported at least 782
(part 4, both remaining runs together, used 837).

## 5. Decisions still open (recommendation first)

1. **A0 and F5:** use the tunnel log to decide whether Firecrawl visited a seed, and offer unvisited seeds
   once more? Yes. This is verification, consistent with "traffic is not discovery".
2. **Rule A:** resolve `/./` in the normalizer (ground truth regenerated) or only in `request_key`? In the
   normalizer.
3. **Rule B:** keep trap URLs flagged, or drop them? Keep them flagged and out of every frontier.
4. **Rule C:** fixed levels or a weighted sum? Fixed levels.
5. **Rule E:** allow it despite the max-recall goal? Only if the P0 replay shows no loss of expected
   resources.
6. **Rule F:** may Firecrawl permanently skip low-value seeds? Decide after the P0 numbers.
7. **Memory and credit logging (T):** add it before the next benchmark round? Yes.

## 6. Other open items

- Commit the uncommitted work (tool-run traces, access-log metric, Crawl Maze path matching, Firecrawl
  zero-traffic fix, summary scripts, docs). Ask before committing.
- Juice Shop benchmark and the manual `reachable` review of its ground truth.
- `client_spider.per_seed_max_duration_min` for runs with many seeds.
- Pin ZAP add-on versions (they are recorded per run in `run_meta.json`).
- `.env` `PUBLIC_TUNNEL_URL` points to a dead quick-tunnel URL; benchmark scripts create a new one.
