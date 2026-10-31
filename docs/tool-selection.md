# Tool-selection algorithm

How the orchestrator decides which crawler runs next, written as a procedure. It describes the code as of
2026-10-27 (`orchestrator/loop.py`, `orchestrator/rules.py`, `storage/state_store.py`). No LLM is involved:
every decision is a deterministic function of what is stored in the SQLite state store.

## Terms

| Term | Meaning |
|---|---|
| **Tool** | One crawler action: `zap_traditional_spider`, `zap_client_spider` (or `zap_ajax_spider`), `wapiti_form_crawl`, `firecrawl_navigate`, `http_probe`. |
| **Engine** | The program behind a tool: `zap`, `wapiti`, `firecrawl`, `http` (the part of the tool name before the first `_`). Tools of one engine share state, so they never run at the same time. |
| **Iteration** | One round of the loop. It runs **at most one tool per engine**, the engines in parallel. One iteration therefore contains 1–4 **tool runs**. |
| **Endpoint** | A normalized URL (`canonical_key`, e.g. `GET /test/html/body/a/href.found`). IDs, hashes and query values are generalized, so `/item/1` and `/item/2` are one endpoint. |
| **Coverage** | For every endpoint and every tool: `PENDING` (not yet given to / reported by that tool) or `PROCESSED`. |
| **Frontier of a tool** | The seeds that tool would get now (definition in step 3.2). |
| **Fingerprint** | `"<count>:<sha1 of the sorted pending URLs>"` of a tool's pending list, stored after each of its runs. |

## Procedure

### Step 1. Initialise

1. If the store is empty, insert the base URL and the configured `target.seed_urls` as endpoints with
   status 0 (unknown).
2. Determine the **available tools**: ZAP's three spiders if the ZAP daemon answers; Wapiti if its binary
   exists; Firecrawl if an API key is set and the target is reachable from the cloud (public tunnel for a
   localhost target); the HTTP probe always. Tools disabled in the config are removed.
3. `base_seeds` = base URL + `seed_urls`, filtered by the safety firewall (allowed hosts only, no logout
   paths, no cloud-metadata hosts).
4. `iteration = 0`, `staged = {}`.

### Step 2. Fixed stages

For each stage in `orchestrator.stages` (default: stage 1 = `[zap_traditional_spider, zap_client_spider,
wapiti_form_crawl]`, stage 2 = `[firecrawl_navigate]`):

1. Keep the stage's tools that are available. If none are left, or `iteration ≥ max_iterations`, skip the stage.
2. `iteration += 1`.
3. Seeds per tool: stage 1 → `base_seeds`; later stages → `base_seeds` + the tool's frontier (step 3.2),
   in scope only.
4. Run the tools (step 5) and add them to `staged`.

Stage 1 therefore runs the ZAP lane (traditional spider, then Client Spider) in parallel with Wapiti;
stage 2 runs Firecrawl on every page-like URL found in stage 1.

### Step 3. Rule round: take a snapshot

Repeat steps 3–5 until a stop condition holds.

1. If `iteration ≥ max_iterations` (default 30): **stop, termination = `max_iterations`**.
2. For each selectable tool that is available (in this order: ZAP traditional spider, browser spider
   [`zap_client_spider` by default], Wapiti, Firecrawl, HTTP probe), read from the store:
   - `runs`: how many times it ran in this crawl;
   - `last_status`: `ok` / `failed` / `timeout` of its last run, and `last_seeds`;
   - `consecutive_failures`: failed or timed-out runs in a row, counted back from its last run;
   - `stored_fingerprint`: the fingerprint saved right after its last run;
   - `current_fingerprint`: the fingerprint of its pending list now;
   - `frontier`: its seeds now:
     - **HTTP probe:** GET endpoints with unknown status (0) and no `#` route.
     - **Every other tool:** endpoints whose coverage for this tool is `PENDING`, method GET, status not
       404/405/410 and below 500. For the browser spider, Wapiti and Firecrawl ("page-like" tools) also:
       content type HTML/XHTML/unknown, or a `#/` client route. Ordered 2xx first, then by the iteration
       that found them. Each endpoint is given as a concrete URL a tool actually observed, not as the
       generalized template.
   - `frontier_changed` = `runs > 0` **and** `current_fingerprint ≠ stored_fingerprint` **and** the frontier
     is not empty.

   Note: the fingerprint covers the tool's whole pending list, while the frontier of a page-like tool is the
   page-like part of it. A new non-page URL can therefore change the fingerprint, and the tool then runs on
   its (page-like) frontier.

### Step 4. Rule round: select the actions

```
actions = {}                                   # at most one per engine
for rule in [R1, R2, R3, R4, R5, R6, R7]:      # priority order; rules in rules.disabled_rules are skipped
    for tool in snapshot.tools:                # order of step 3.2
        engine = engine_of(tool)
        if engine in actions:                  continue   # this engine already has an action this round
        if tool.runs >= max_runs_per_tool:     continue   # safety ceiling (default 10)
        action = rule(tool)
        if action: actions[engine] = action
if actions is empty: stop, termination = "saturation"
```

| Rule | Fires for a tool when | Seeds |
|---|---|---|
| **R1 retry** | its last run ended `failed` or `timeout`, `consecutive_failures ≤ max_retries` (default 1), and it had seeds | the same seeds again |
| **R2 first run** | it never ran, no fixed stage covers it, and it is not the probe | `base_seeds` |
| **R3 browser spider** | it is the configured browser spider and `frontier_changed` | its frontier |
| **R4 link spider** | it is the ZAP traditional spider and `frontier_changed` | its frontier |
| **R5 form crawler** | it is Wapiti and `frontier_changed` | its frontier |
| **R6 UI agent** | it is Firecrawl and `frontier_changed` | its frontier (focus text: "pages and client-side views discovered since the previous exploration") |
| **R7 probe** | it is the HTTP probe, its frontier is not empty and its fingerprint changed (no previous run needed) | its frontier |

Consequences of the "one action per engine" limit and the rule order:

- The ZAP traditional spider and the Client Spider are both engine `zap`. When both frontiers changed, R3
  (Client Spider) wins, and the traditional spider waits until a round in which the Client Spider has
  nothing new.
- Different engines are independent: in one round ZAP, Wapiti, Firecrawl and the probe can all run.

### Step 5. Run the round and update the store

1. `iteration += 1`; log one `decision` event per action (rule, tool, number of seeds, reason).
2. Firewall the seeds of every action again (an empty list becomes `[base URL]`).
3. Group the actions by engine ("lanes"). Inside a lane the tools run one after another; the lanes run in
   parallel when `orchestrator.parallel` is true.
4. After all lanes finish, ingest each tool run in plan order:
   1. Normalize every reported URL; drop junk, traps the normalizer recognizes and other hosts.
   2. Insert endpoints that are new. A new endpoint gets coverage `PROCESSED` for the tool that found it and
      `PENDING` for every other tool, so it enters their frontiers.
   3. Mark every result **and every seed** of the run as `PROCESSED` for this tool.
   4. Write a history row: tool, rule, seeds, status, requests, duration, startup, and the tool's
      **fingerprint after the run** (compared in the next rounds by `frontier_changed`).
5. Go back to step 3.

### Step 6. Finish

Export the endpoints (`normalized_discovered_urls.txt`, `normalized_endpoints.json`) and log `run_finished`
with the termination reason, the number of iterations and the rule counts.

## Why the crawl stops

- **Saturation:** a tool is chosen again only if its pending list changed since its last run. When no tool
  reports anything new, no fingerprint changes and no rule fires.
- **Ceilings:** `max_iterations` (30) and `max_runs_per_tool` (10) bound every crawl, even when a trap keeps
  producing new URLs; `max_retries` (1) bounds R1.

## Iterations vs. tool runs: Crawl Maze full pipeline

| Run | Iterations | Tool runs | Tool runs per iteration |
|---|---:|---:|---|
| run-02 | 12 | 25 | 3, 1, 3, 2, 2, 2, 3, 2, 2, 2, 2, 1 |
| run-04 | 18 | 37 | 3, 1, 3, then 2 in each of iterations 4–18 |

Run-04 step by step (rule, seeds):

| Iter | ZAP engine | Wapiti | Firecrawl | Probe |
|---:|---|---|---|---|
| 1 | stage 1: traditional (1) → Client Spider (1) | stage 1 (1) | – | – |
| 2 | – | – | stage 2 (230) | – |
| 3 | R3 Client Spider (60) | R5 (96) | – | R7 (77) |
| 4 | R3 Client Spider (2) | – | R6 (32) | – |
| 5 | R3 Client Spider (2) | R5 (30) | – | – |
| 6 | R4 traditional (131) | – | R6 (2) | – |
| 7 | R3 Client Spider (4) | R5 (6) | – | – |
| 8 | R4 traditional (6) | – | R6 (2) | – |
| 9–18 | alternating R3 Client Spider / R4 traditional (1–4) | R5 in odd rounds (2–5) | R6 in even rounds (1–2) | – |

The alternation comes from the data flow: Firecrawl's new URLs in an even round change the frontiers of the
Client Spider and Wapiti, whose new URLs in the next (odd) round change the frontiers of Firecrawl and the
traditional spider. From iteration 4 on, all new URLs were AngularJS hash-route trap variants
(`/javascript/frameworks/angularjs#/!/#%2F!%2F…`), so each round was triggered by 1–6 trap URLs; the crawl
saturated when Firecrawl in iteration 18 reported no new variant.
