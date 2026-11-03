# Improvement plan: runtime reporting, Firecrawl evidence, crawl storms, seed selection

Status (2026-11-03): **partly implemented.** Phase 0 and a simplified P2 (Firecrawl schema and prompt) are
done; every rule (A0, A–F), runtime reporting (T) and the offline replay (P0) are still open. See
*Implementation status* below. The plan compiles the findings and proposals discussed after the Crawl Maze and
Crawlground series. Decisions still open are listed at the end. The algorithm the plan changes is described in
`docs/tool-selection.md`.

## Implementation status

| Item | Status | Commit / note |
|---|---|---|
| Phase 0: finish the Crawl Maze series | done 2026-10-27 | `5ba0da1` |
| P2: Firecrawl schema and prompt | done 2026-10-31, **simplified** (three flat lists, no `from_page`, no action log; context files removed; `batch_size` 10) | `caf212b` |
| P2: accuracy metric, `Sec-Fetch-*` in the proxy log, Referer check | open | |
| T: runtime, memory and credit reporting | open | |
| P0a: data capture (G1–G5) | done 2026-11-03: Client Map tree parser, ZAP Referer, Wapiti referer/depth, `endpoint_links`, per-run copies of the Client Map and Wapiti DB | |
| A0, rules A, B, C, D, E, F | open (C: design agreed, see 3.6) | |
| P0: offline replay | open; only Crawl Maze run-04 has partial data for it (see 3.6) | |
| Eval: new labels | open; needs new Firecrawl credits | |

Decisions of 2026-11-03 that change the plan: the tunnel log is offline-only (3.2, 3.3, F5); Wapiti always
runs headless and keeps `#` seeds (3.4, 3.5, 3.7).

Changes made outside this plan:

- **R2 "first run" removed** (2026-11-02, `36bd93c`). It fired in none of the 30 recorded runs, because the
  fixed stages run every tool once. A tool that has never run now counts as "frontier changed", so R3–R6 start
  a tool the stages leave out. Rule numbers are kept so old logs stay comparable.
- **Early Crawlground test runs deleted** (2026-10-31): `smoke`, `no-firecrawl`, `firecrawl-smoke`,
  `with-firecrawl` (one run each, 2026-10-12/13, made before ZAP was restarted between runs; none was part of
  a series summary).
- **AJAX Spider removed** (2026-11-03): wrapper code, the `ajax_spider` and `orchestrator.rules.browser_spider`
  settings, the tool name, its docker-compose option, docs and tests. The Client Spider is the only browser
  spider. It was not part of any benchmark series.

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
  a trap URL is the same request as the page it already crawled. (Corrected 2026-11-03: headless Wapiti does
  load the fragment, see 3.4; its trap re-runs are for Rule B to stop.)
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
`batch_size` stays 10. Each ingested result carries `firecrawl_kind`. The traffic-based accuracy metric below
is still to do. The original proposal, kept for reference:

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
- **Role (decided 2026-11-03):** the tunnel log is an **offline audit only** (this accuracy metric, the P0
  replay). It never decides a coverage state at runtime, because production and external scans have no
  logging proxy. See A0.

### 3.3 A0: truthful "visited" (prerequisite for C and F)

- Coverage states per tool:
  - `PENDING`: not yet given to the tool;
  - `SEEDED`: handed over, no evidence of a visit in the tool's output;
  - `VISITED`: the tool's own output shows a visit;
  - `SEEN`: only extracted.
- **Evidence per tool (runtime, tool output only):**

  | Tool | `VISITED` when | `SEEN` when |
  |---|---|---|
  | ZAP traditional spider, HTTP probe | a message/response with a status | – |
  | ZAP Client Spider | ZAP history message with a status; Client Map node with `visited` (absent flag = visited, to verify) | Client Map node with `visited: false`, component `href`s |
  | Wapiti (headless) | `responses.status_code` in its session DB. A `#` seed: its fragment-less page has a response in that run (Wapiti does not store routes, see 3.4; that the route rendered is assumed, not shown) | – |
  | Firecrawl | listed in `visited_urls`, or in `interaction_urls` (with `interaction = true`) | listed in `seen_urls`; old `links` (`unspecified`) |

- **Firecrawl is trusted at runtime (decided 2026-11-03):** its payload sets the state directly; the tunnel
  log does not. Risk: Firecrawl's lists are self-reported (in the old format, 116 of 329 reported paths were
  never requested). A false `VISITED` keeps a seed from being offered again; a false interaction raises a seed
  to level 1. The P2 accuracy metric measures both per benchmark and is reported as a threat to validity.
- A Firecrawl seed that appears in none of its three lists stays `SEEDED`. Seeds of failed runs stay `SEEDED`
  (R1 still retries them).
- The frontier keeps today's behaviour (`PENDING` only), so this is mostly bookkeeping; C and F read the new
  states.

### 3.4 Rule A: same request for this tool

- `request_key(url, tool)`:
  - drop the fragment **only for tools that never load it**: the ZAP traditional spider and the HTTP probe
    (the probe already skips `#` routes);
  - **keep the fragment** for the Client Spider, Firecrawl and Wapiti (decided 2026-11-03);
  - resolve `/./` and `/../`;
  - drop default ports.
- A pending seed whose key is already done for that tool is skipped. The fingerprint is computed on the same
  filtered list, so a skipped seed cannot trigger a re-run.
- **Wapiti runs headless, always (decided 2026-11-03):** `tools.wapiti.headless` is fixed to `hidden` (it is
  already the default; the option `no` is to be removed, together with the wrapper branch that drops `#` seeds
  when headless is off). Verified in Wapiti 3.3.2's code:
  - `-s <url#route>` keeps the fragment (`Request(start_url)`), and the headless explorer navigates to
    `url_with_fragment` and parses links with `allow_fragments=True`, so Wapiti does explore hash routes;
  - its session DB stores only the `path`, so **routes never come back in Wapiti's output**: in Crawl Maze
    run-04 its 8 runs got 35 `#` seeds and returned 0 `#` URLs (247 DB paths, none with `#`). What a route
    seed yields is the server traffic it triggers (pages, XHR, forms);
  - `-u` must not contain a fragment (`is_valid_endpoint`), so `#` seeds always go in `-s`.
- Expected effect: the ZAP traditional re-runs triggered by trap variants (section 2.4) do not fire. **Wapiti's
  trap re-runs are no longer stopped by A** (run-04: 7 re-runs, 694 s, 0 new); Rule B must stop them, so B is
  a prerequisite for Wapiti's savings.
- Risk inside one Wapiti run: its explorer treats fragment variants as different requests, so a hash trap can
  also grow inside Wapiti, bounded by its `depth`. Not observed so far (re-runs took about 97 s each).
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
- **B is the only guard against fragment traps** for the tools that keep fragments (Client Spider, Wapiti,
  Firecrawl); A no longer strips fragments for Wapiti.
- Expected effect on Crawl Maze: the AngularJS line stops after its first generation, so saturation around
  iteration 5 instead of 12 or 18. The P0 replay confirms this before implementation.
- Risk: low.

### 3.6 Rule C: seed value score

- **Approach:** deterministic and explainable. Each seed gets a level 1–4 plus a list of reasons; no learned
  weights.
- **Levels (agreed 2026-11-02):**

  | Level | When |
  |---|---|
  | Excluded | trap (B) or same request as a done URL for this tool (A) |
  | 1 – high | first URL of a new page group, **or** a page with forms/buttons no browser tool explored, **or** a URL Firecrawl reached only by interaction (`firecrawl_kind = interaction`) |
  | 2 | handled only by tools without JavaScript (ZAP traditional, probe); JavaScript tools are the Client Spider, headless Wapiti and Firecrawl |
  | 3 | opened by a browser tool, has outgoing links |
  | 4 – low | leaf (opened by a browser tool, no outgoing links) or most of its links already known |

  Level 1 takes precedence over level 4: a page with no links but a button is not a leaf.
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
- **Where the evidence comes from today** (checked 2026-11-02 on Crawl Maze run-04):

  | Signal | ZAP traditional | ZAP Client Spider | Wapiti | Firecrawl |
  |---|---|---|---|---|
  | Page group | request URL | node URL | `paths.path` + `params` | the three lists |
  | Forms/buttons | no (bodies not kept) | Client Map `components.tagName` BUTTON/INPUT/FORM, `formId` (**dropped by the parser**) | `paths.method`/`enctype`, `params`, `responses.body` | no |
  | Reached by interaction | – | derivable from Client Map BUTTON components | – | `interaction_urls` (new runs only) |
  | Visited by a JavaScript tool | – | ZAP history status; Client Map `visited` (ignored) | – | `visited_urls` (self-reported) |
  | Outgoing links / leaf | `Referer` in `requestHeader` (**dropped**) | component `href`s per node (**parent relation dropped**) | `paths.referer`, `depth` | no `from_page` |

- **Data gaps to close before scoring:**

  | # | Gap | Fix |
  |---|---|---|
  | G1 | Client Map parser keeps hrefs only | return per node: URL, parent, `visited`, components (`tagName`, `href`, `formId`) |
  | G2 | ZAP `Referer` dropped in `_parse_message` | keep it as a link edge |
  | G3 | no link table | `endpoint_links`, filled from G1, G2 and Wapiti `paths.referer` |
  | G4 | Client Map export overwritten every run (`output/zap/client_map.yaml`) | copy it into the run folder after each Client Spider run |
  | G5 | Wapiti session DB flushed on every Wapiti run | copy `session.db` per Wapiti run |
  | G6 | PROCESSED ≠ visited | A0 |
  | G7 | path templates too fine to group pages (readable slugs are kept) | page group = (method, parent folder of the path template, query keys) – **decided 2026-11-03** |

- **P0a findings (2026-11-03):**
  - Client Map, run-04 export: 354 edges, 348 kept after normalization (204 `dom:a`, 84 `redirect`,
    30 `dom:input`, 18 `dom:button`, 12 `dom:form`). Nodes with components (pages) never carry a `visited`
    flag and path-only nodes carry `visited: false`, which supports "absent = visited".
  - Wapiti's `paths.referer` is mostly empty in headless mode: 21 of 247 paths in run-04's last Wapiti run.
    Wapiti contributes few edges; leaf detection relies on the Client Map and ZAP Referers.
  - The 0.33 layout (Juice Shop fixture) has components without `tagName`, so its edges are `dom:unknown`
    and carry no interaction evidence.
  - Edges never add endpoints: P0a does not change what a run discovers.
- **Edge cases:** browsers never send the fragment in `Referer`, so links between `#/` routes come only from
  the Client Map tree; "no links" seen only by the traditional spider is not a leaf (JavaScript links);
  the Client Map `visited` flag appears only as `false` (251 nodes) and is absent on 247 nodes, which probably
  means visited (to verify); redirects (Client Map `Redirect`, 3xx `Location`) are link edges too;
  `interaction_urls` is Firecrawl's own claim (the P2 accuracy metric measures how reliable it is).
- **Level-4 seeds for Firecrawl (agreed 2026-11-02):** compare in the replay
  - **Run A:** skip level 4 for good;
  - **Run B:** defer level 4 and send it in one final batch before termination;

  on expected resources lost vs. jobs, credits and time. Replay limits: only Crawl Maze run-04 kept its Client
  Map; no old run has ZAP Referers or Firecrawl kinds; attribution of a find to a seed uses the tunnel Referer
  (offline analysis only); the replay cannot show how Firecrawl behaves with a different seed mix, so live runs
  confirm the result.
- **Use:**
  - Cheap tools (ZAP traditional, probe) are only ranked, never filtered.
  - Expensive tools use the level for selection (D, F).
- **Code:** `orchestrator/seed_scoring.py` (pure functions), features built in `build_snapshot`, settings
  in `orchestrator.seed_scoring`.
- **Scope (agreed 2026-11-03):** coarse tiers first, then ordering inside a tier in steps. With maximum recall
  and no budget, ordering inside a tier changes *when* resources are found (t50/t90), not *whether*, unless
  something cuts the list (level-4 skip, F4 stop, a credit cap). All ordering is a fixed sort on readable keys,
  no weights; each step sits behind a config switch.

  | Step | Content | Measured by |
  |---|---|---|
  | 1 | P0a data capture (G1–G5) | tests |
  | 2 | Coarse tiers + fixed tie-break: level → page group (round-robin) → status class → discovery order; replay: baseline, Run A, Run B | recall lost, Firecrawl jobs/credits/time |
  | 3 | Parent yield as a tie-break key (needs G3) | t50/t90, recall under a credit cap |
  | 4 | Structural de-duplication (page shape from Client Map components or Wapiti bodies) as an optional filter, off by default | recall lost vs. seeds removed; high risk on Crawl Maze, whose test pages look alike |

  HTTP feedback needs no new work: status is the tie-break, content type selects page-like seeds, form
  density is level 1; error rate stays out on purpose.

### 3.7 Rule D: re-runs sized to the new seeds

- A first run keeps scope `domain`.
- Re-runs triggered by R4 and R5:
  - ZAP: `spider.scan(seed, recurse=False, subtreeOnly=True)` per seed, with a small depth;
  - Wapiti: `-u <seed> --scope folder` (or `page`) per seed group instead of `-u base_url`. `-u` cannot hold a
    fragment, so a `#` seed keeps `-u` = its fragment-less page and goes in `-s`.
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
5. **F5 unvisited seeds (revised 2026-11-03, no tunnel log):** a seed that appears in none of Firecrawl's
   three lists stays `SEEDED` and is offered once more, then given up. The tunnel log only audits this offline.

Trade-off: with max recall and no budget, F saves time only if Firecrawl may skip low-value seeds for good.
If deferred seeds always get a final run, the savings disappear.

## 4. Phases

| Phase | Content | Runs / credits |
|---|---|---|
| 0 | Finish the paused Crawl Maze runs on the current code | **done** 2026-10-27 |
| P0a | Data capture for scoring: G4, G5 (keep raw data per run), G1, G2, G3 (components, link graph). **Done** 2026-11-03 | none |
| P0 | Offline replay on existing runs: rebuild each run's frontiers, apply A, B, C, E, F step by step, and report per rule: seeds removed, tool runs avoided, estimated time saved, expected resources lost. Seeds are linked to what they produced through traces, the ZAP history and (offline only) tunnel Referers | none |
| P1 | T (runtime and memory reporting), A0, A, B, Wapiti headless fixed, with tests and docs; regenerate ground truth if the normalizer changes | none until evaluation |
| P2 | Firecrawl schema and prompt (**done**, simplified), offline accuracy metric (tunnel log) | evaluation only |
| P3 | Link graph, C, then D, E, F behind config flags | evaluation only |
| Eval | New labels (`full-pipeline-v2`, then one-rule-off variants) on Crawl Maze, Crawlground and Juice Shop; old runs kept | yes, ask first |

Credits: the current Firecrawl key has about 188 left; full-pipeline/run-04 reported at least 782
(part 4, both remaining runs together, used 837).

## 5. Decisions still open (recommendation first)

1. ~~**A0 and F5:** use the tunnel log to decide whether Firecrawl visited a seed?~~ **Decided 2026-11-03:**
   no. Firecrawl's payload sets the state at runtime; the tunnel log is an offline audit only (3.2, 3.3).
2. **Rule A:** resolve `/./` in the normalizer (ground truth regenerated) or only in `request_key`? In the
   normalizer.
3. **Rule B:** keep trap URLs flagged, or drop them? Keep them flagged and out of every frontier.
4. ~~**Rule C:** fixed levels or a weighted sum?~~ **Decided:** fixed levels (definitions in 3.6).
5. **Rule E:** allow it despite the max-recall goal? Only if the P0 replay shows no loss of expected
   resources.
6. **Rule F:** may Firecrawl permanently skip low-value seeds? Decide after the P0 Run A vs. Run B comparison.
7. **Memory and credit logging (T):** add it before the next benchmark round? Yes.
8. ~~**Page group (G7)**~~ **Decided 2026-11-03:** (method, parent folder of the path template, query keys).
9. ~~**Order**~~ **Decided 2026-11-03:** P0a data capture and link-graph ingestion before the replay.
10. ~~**Rule A for Wapiti:** drop fragments?~~ **Decided 2026-11-03:** no. Wapiti always runs headless and
    keeps `#` seeds; fragment traps are Rule B's job (3.4).
11. ~~**Wapiti `#` seed → VISITED**~~ **Decided 2026-11-03:** visited when its fragment-less page got a
    response in that Wapiti run, documented as an assumption (Wapiti cannot report the route).

## 6. Other open items

- Juice Shop benchmark and the manual `reachable` review of its ground truth.
- `client_spider.per_seed_max_duration_min` for runs with many seeds.
- Pin ZAP add-on versions (they are recorded per run in `run_meta.json`).
- `.env` `PUBLIC_TUNNEL_URL` points to a dead quick-tunnel URL; benchmark scripts create a new one.
