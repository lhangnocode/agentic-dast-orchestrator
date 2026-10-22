"""
Relational SQLite State Store for the Orchestrator.
Maintains the Canonical Attack Surface, Tool Coverage Matrix (U x T),
Provenance, and Iterative Execution History.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Tool names of the coverage matrix (mirror orchestrator.decision_schema.ToolActionEnum)
CRAWL_TOOLS = ("zap_traditional_spider", "zap_ajax_spider", "zap_client_spider", "wapiti_form_crawl",
               "firecrawl_navigate")

# Legacy / short source labels mapped onto coverage-matrix tool names
SOURCE_TOOL_ALIASES = {
    "zap": "zap_traditional_spider",
    "wapiti": "wapiti_form_crawl",
    "firecrawl": "firecrawl_navigate",
}

# Path segments that mark an endpoint as a machine API rather than a rendered page
API_SEGMENTS = {"api", "rest", "graphql", "rpc", "ajax", "json", "v1", "v2", "v3"}


class StateStore:
    def __init__(self, db_path: str = "storage/orchestrator_state.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. Canonical Endpoints table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS endpoints (
                    canonical_hash TEXT PRIMARY KEY,
                    canonical_key TEXT UNIQUE NOT NULL,
                    method TEXT NOT NULL,
                    path_template TEXT NOT NULL,
                    query_params TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    status_code INTEGER DEFAULT 200,
                    first_seen_iteration INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 2. Multi-source Provenance table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS endpoint_sources (
                    canonical_hash TEXT NOT NULL,
                    source_tool TEXT NOT NULL,
                    raw_sample TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (canonical_hash, source_tool),
                    FOREIGN KEY (canonical_hash) REFERENCES endpoints(canonical_hash) ON DELETE CASCADE
                );
            """)

            # 3. Tool Coverage Matrix (U x T)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tool_coverage (
                    canonical_hash TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'PROCESSED', 'FAILED')),
                    last_attempt_iteration INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (canonical_hash, tool_name),
                    FOREIGN KEY (canonical_hash) REFERENCES endpoints(canonical_hash) ON DELETE CASCADE
                );
            """)

            # 4. Iteration and Decision History
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS execution_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    iteration INTEGER NOT NULL,
                    chosen_tool TEXT NOT NULL,
                    reasoning TEXT,
                    target_seeds TEXT,
                    endpoints_before INTEGER NOT NULL,
                    endpoints_after INTEGER NOT NULL,
                    new_endpoints_discovered INTEGER NOT NULL,
                    marginal_gain REAL NOT NULL,
                    tool_duration_seconds REAL DEFAULT 0.0,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Migration check: if execution_history has iteration as primary key, migrate it
            cursor.execute("PRAGMA table_info(execution_history);")
            cols = {row[1]: row for row in cursor.fetchall()}
            if "id" not in cols and "iteration" in cols and cols["iteration"][5] == 1:
                cursor.execute("ALTER TABLE execution_history RENAME TO execution_history_old;")
                cursor.execute("""
                    CREATE TABLE execution_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        iteration INTEGER NOT NULL,
                        chosen_tool TEXT NOT NULL,
                        reasoning TEXT,
                        target_seeds TEXT,
                        endpoints_before INTEGER NOT NULL,
                        endpoints_after INTEGER NOT NULL,
                        new_endpoints_discovered INTEGER NOT NULL,
                        marginal_gain REAL NOT NULL,
                        tool_duration_seconds REAL DEFAULT 0.0,
                        llm_prompt_tokens INTEGER DEFAULT 0,
                        llm_completion_tokens INTEGER DEFAULT 0,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cursor.execute("""
                    INSERT INTO execution_history (
                        iteration, chosen_tool, reasoning, target_seeds,
                        endpoints_before, endpoints_after, new_endpoints_discovered,
                        marginal_gain, tool_duration_seconds, llm_prompt_tokens, llm_completion_tokens, timestamp
                    ) SELECT iteration, chosen_tool, reasoning, target_seeds,
                        endpoints_before, endpoints_after, new_endpoints_discovered,
                        marginal_gain, tool_duration_seconds, llm_prompt_tokens, llm_completion_tokens, timestamp
                    FROM execution_history_old;
                """)
                cursor.execute("DROP TABLE execution_history_old;")

            # Migrations: columns added after the first schema version
            self._add_missing_columns(cursor, "endpoints", {
                "content_type": "TEXT DEFAULT ''",
            })
            self._add_missing_columns(cursor, "execution_history", {
                "requests_sent": "INTEGER DEFAULT 0",
                "raw_results": "INTEGER DEFAULT 0",
                "seeds_count": "INTEGER DEFAULT 0",
                "status": "TEXT DEFAULT 'ok'",          # ok | failed | timeout
                "error": "TEXT",
                "rule_id": "TEXT",                     # which stage/rule triggered the run
                "frontier_fingerprint": "TEXT",        # tool's pending frontier right after the run
            })

            # Tools added later get PENDING coverage rows for endpoints that already exist
            for tool in CRAWL_TOOLS:
                cursor.execute("""
                    INSERT OR IGNORE INTO tool_coverage (canonical_hash, tool_name, status, last_attempt_iteration)
                    SELECT canonical_hash, ?, 'PENDING', 0 FROM endpoints
                """, (tool,))

            # Indices for rapid querying
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_endpoints_path ON endpoints(path_template);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tool_coverage_lookup ON tool_coverage(tool_name, status);")
            conn.commit()

    @staticmethod
    def _add_missing_columns(cursor: sqlite3.Cursor, table: str, columns: Dict[str, str]) -> None:
        cursor.execute(f"PRAGMA table_info({table});")
        existing = {row[1] for row in cursor.fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl};")

    def insert_or_update_endpoint(self, endpoint: Dict[str, Any], current_iteration: int) -> bool:
        """
        Inserts a new canonical endpoint or updates existing provenance/status.
        Returns True if the canonical endpoint was freshly discovered (net new).
        """
        canonical_hash = endpoint["canonical_hash"]
        canonical_key = endpoint["canonical_key"]
        method = endpoint.get("method", "GET")
        path_template = endpoint.get("path_template", "/")
        query_params = json.dumps(endpoint.get("query_params", []))
        normalized_url = endpoint.get("normalized_url", "")
        status_code = endpoint.get("status_code", 200)
        source_tool = endpoint.get("source_tool", "seed")
        raw_sample = endpoint.get("raw_sample", "")
        content_type = (endpoint.get("content_type") or "").split(";")[0].strip().lower()

        is_new = False
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Check existence
            cursor.execute("SELECT canonical_hash, status_code, content_type FROM endpoints WHERE canonical_hash = ?", (canonical_hash,))
            row = cursor.fetchone()

            if row is None:
                is_new = True
                cursor.execute("""
                    INSERT INTO endpoints (
                        canonical_hash, canonical_key, method, path_template,
                        query_params, normalized_url, status_code, first_seen_iteration, content_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (canonical_hash, canonical_key, method, path_template, query_params, normalized_url,
                      status_code, current_iteration, content_type))

                # Initialize coverage matrix: the discovering tool already saw it, the others are PENDING
                mapped_tool = SOURCE_TOOL_ALIASES.get(source_tool, source_tool)
                for t in CRAWL_TOOLS:
                    init_status = "PROCESSED" if t == mapped_tool else "PENDING"
                    cursor.execute("""
                        INSERT INTO tool_coverage (canonical_hash, tool_name, status, last_attempt_iteration)
                        VALUES (?, ?, ?, ?)
                    """, (canonical_hash, t, init_status, current_iteration if t == mapped_tool else 0))
            else:
                # Keep the most informative status: a verified 2xx wins, and any real status beats unknown (0)
                old_status = row["status_code"] or 0
                if (200 <= status_code < 300 and not 200 <= old_status < 300) or (old_status == 0 and status_code):
                    cursor.execute("UPDATE endpoints SET status_code = ? WHERE canonical_hash = ?", (status_code, canonical_hash))
                if content_type and not row["content_type"]:
                    cursor.execute("UPDATE endpoints SET content_type = ? WHERE canonical_hash = ?", (content_type, canonical_hash))

            # Record provenance
            cursor.execute("""
                INSERT OR IGNORE INTO endpoint_sources (canonical_hash, source_tool, raw_sample)
                VALUES (?, ?, ?)
            """, (canonical_hash, source_tool, raw_sample))

            conn.commit()

        return is_new

    def mark_tool_coverage(self, canonical_hash: str, tool_name: str, status: str, iteration: int) -> None:
        """Updates the status of an endpoint for a specific tool in the coverage matrix."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tool_coverage (canonical_hash, tool_name, status, last_attempt_iteration, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(canonical_hash, tool_name) DO UPDATE SET
                    status = excluded.status,
                    last_attempt_iteration = excluded.last_attempt_iteration,
                    updated_at = CURRENT_TIMESTAMP
            """, (canonical_hash, tool_name, status, iteration))
            conn.commit()

    def mark_hashes_processed(self, canonical_hashes: Iterable[str], tool_name: str, iteration: int,
                              status: str = "PROCESSED") -> int:
        """Marks known endpoints as visited by tool_name. Unknown hashes are ignored. Returns rows updated."""
        hashes = list(dict.fromkeys(canonical_hashes))
        if not hashes:
            return 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany("""
                UPDATE tool_coverage
                SET status = ?, last_attempt_iteration = ?, updated_at = CURRENT_TIMESTAMP
                WHERE canonical_hash = ? AND tool_name = ?
            """, [(status, iteration, h, tool_name) for h in hashes])
            conn.commit()
            return cursor.rowcount

    def get_pending_frontier(self, tool_name: str, limit: Optional[int] = 30,
                             statuses: Optional[Iterable[int]] = None,
                             sources: Optional[Iterable[str]] = None,
                             page_like: bool = False) -> List[str]:
        """
        Retrieves GET seed URLs that tool_name has not visited yet.
        - limit: maximum number of seeds; None = no limit.
        - statuses: only endpoints with these HTTP status codes.
        - sources: only endpoints discovered by these tools (provenance).
        - page_like: only URLs that can be pages: HTML or not-yet-known content type
          (excludes JSON/API responses, files, scripts), plus all client-side `#/` routes.
        Dead ends (404/405/410 and 5xx) are skipped; 2xx come first, then auth walls/redirects.
        Returns a concrete URL observed by a crawler (raw sample), not the {placeholder} template,
        so the seed can actually be requested.
        """
        filters = ""
        params: List[Any] = [tool_name]
        if statuses:
            status_list = list(statuses)
            filters += f" AND e.status_code IN ({','.join('?' * len(status_list))})"
            params.extend(status_list)
        if sources:
            source_list = list(sources)
            filters += (" AND EXISTS (SELECT 1 FROM endpoint_sources src WHERE src.canonical_hash = e.canonical_hash"
                        f" AND src.source_tool IN ({','.join('?' * len(source_list))}))")
            params.extend(source_list)
        if page_like:
            filters += (" AND (e.path_template LIKE '%#%' OR COALESCE(e.content_type, '') IN "
                        "('', 'text/html', 'application/xhtml+xml'))")
        params.append(-1 if limit is None else limit)  # SQLite: LIMIT -1 = unlimited
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT e.normalized_url,
                       (SELECT es.raw_sample FROM endpoint_sources es
                        WHERE es.canonical_hash = e.canonical_hash AND es.raw_sample LIKE 'http%'
                        ORDER BY es.raw_sample LIKE '%{{%' ASC, length(es.raw_sample) ASC
                        LIMIT 1) AS sample_url
                FROM endpoints e
                JOIN tool_coverage tc ON e.canonical_hash = tc.canonical_hash
                WHERE tc.tool_name = ? AND tc.status = 'PENDING'
                  AND e.method = 'GET'
                  AND e.status_code NOT IN (404, 405, 410) AND e.status_code < 500
                  {filters}
                ORDER BY (e.status_code BETWEEN 200 AND 299) DESC, e.first_seen_iteration ASC
                LIMIT ?
            """, params)
            return list(dict.fromkeys(row["sample_url"] or row["normalized_url"] for row in cursor.fetchall()))

    def get_unknown_status_urls(self, limit: Optional[int] = None) -> List[str]:
        """
        GET endpoints whose HTTP status is still unknown (0) and that live on the server
        (no `#/` client route): the HTTP probe's frontier. Returns concrete observed URLs.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.normalized_url,
                       (SELECT es.raw_sample FROM endpoint_sources es
                        WHERE es.canonical_hash = e.canonical_hash AND es.raw_sample LIKE 'http%'
                        ORDER BY length(es.raw_sample) ASC LIMIT 1) AS sample_url
                FROM endpoints e
                WHERE e.method = 'GET' AND COALESCE(e.status_code, 0) = 0 AND e.path_template NOT LIKE '%#%'
                ORDER BY e.first_seen_iteration ASC, e.path_template ASC
                LIMIT ?
            """, (-1 if limit is None else limit,))
            return list(dict.fromkeys(row["sample_url"] or row["normalized_url"] for row in cursor.fetchall()))

    def get_known_routes_summary(self, max_chars: int = 4000) -> str:
        """
        Compact, deterministic text listing what is already discovered (for the Firecrawl prompt):
        client-side routes first, then server paths grouped by their first segment.
        Cut at a line boundary to stay within max_chars.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT path_template FROM endpoints WHERE status_code NOT IN (404, 410)")
            paths = sorted({row["path_template"] for row in cursor.fetchall()})
        routes = sorted({p[p.index("#"):] for p in paths if "#" in p})
        groups: Dict[str, List[str]] = {}
        for p in paths:
            if "#" in p or p == "/":
                continue
            head, _, rest = p.lstrip("/").partition("/")
            groups.setdefault("/" + head, [])
            if rest:
                groups["/" + head].append(rest)
        lines = []
        if routes:
            lines.append(f"Client-side routes ({len(routes)}): " + ", ".join(routes))
        if groups:
            lines.append(f"Server paths ({sum(max(1, len(v)) for v in groups.values())}):")
            for head in sorted(groups):
                children = sorted(set(groups[head]))
                lines.append(f"- {head}" + (f": {', '.join(children)}" if children else ""))
        text = ""
        for line in lines:
            if len(text) + len(line) + 1 > max_chars:
                text += "(... more not listed)"
                break
            text += line + "\n"
        return text.strip()

    def frontier_fingerprint(self, tool_name: str) -> str:
        """
        Compact identity of tool_name's whole pending frontier ("<count>:<hash>").
        Stored after each run; a different value later means the frontier changed.
        The HTTP probe's frontier is the set of URLs with unknown status.
        """
        if tool_name == "http_probe":
            frontier = sorted(self.get_unknown_status_urls())
        else:
            frontier = sorted(self.get_pending_frontier(tool_name, limit=None))
        digest = hashlib.sha1("\n".join(frontier).encode("utf-8")).hexdigest()[:12]
        return f"{len(frontier)}:{digest}"

    def get_last_runs(self) -> Dict[str, Dict[str, Any]]:
        """Most recent execution_history row per tool."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT h.* FROM execution_history h
                JOIN (SELECT chosen_tool, MAX(id) AS max_id FROM execution_history GROUP BY chosen_tool) last
                  ON h.id = last.max_id
            """)
            return {row["chosen_tool"]: dict(row) for row in cursor.fetchall()}

    def get_consecutive_failures(self, tool_name: str) -> int:
        """How many of tool_name's most recent runs in a row ended in failed/timeout."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM execution_history WHERE chosen_tool = ? ORDER BY id DESC",
                           (tool_name,))
            count = 0
            for row in cursor.fetchall():
                if (row["status"] or "ok") == "ok":
                    break
                count += 1
            return count

    def get_tool_run_counts(self) -> Dict[str, int]:
        """How many times each tool has been executed (bootstrap + adaptive iterations)."""
        counts = {t: 0 for t in CRAWL_TOOLS}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chosen_tool, COUNT(*) AS cnt FROM execution_history GROUP BY chosen_tool")
            for row in cursor.fetchall():
                counts[row["chosen_tool"]] = row["cnt"]
        return counts

    def get_surface_profile(self) -> Dict[str, int]:
        """
        Raw shape of the discovered surface, used to detect SPAs and auth walls without
        any knowledge of the target application. Page/JSON counts use the observed
        Content-Type, so files (markdown, archives, …) are not mistaken for pages.
        """
        profile = {
            "total": 0, "get": 0, "non_get": 0, "html_pages": 0, "json_responses": 0, "api_like": 0,
            "hash_routes": 0, "parameterized": 0, "auth_walls": 0, "redirects": 0, "errors": 0,
            "unknown_status": 0,
        }
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT method, path_template, query_params, status_code, content_type FROM endpoints")
            for row in cursor.fetchall():
                path = row["path_template"] or "/"
                status = row["status_code"] or 0
                ctype = (row["content_type"] or "").lower()
                segments = {s.lower() for s in path.split("#")[0].split("/") if s}
                profile["total"] += 1
                profile["get" if row["method"] == "GET" else "non_get"] += 1
                if "#" in path:
                    profile["hash_routes"] += 1
                if bool(segments & API_SEGMENTS) or "json" in ctype:
                    profile["api_like"] += 1
                if "json" in ctype:
                    profile["json_responses"] += 1
                if "html" in ctype and "#" not in path and 200 <= status < 300:
                    profile["html_pages"] += 1
                if row["query_params"] not in (None, "", "[]"):
                    profile["parameterized"] += 1
                if status == 0:
                    profile["unknown_status"] += 1
                elif status in (401, 403):
                    profile["auth_walls"] += 1
                elif 300 <= status < 400:
                    profile["redirects"] += 1
                elif status >= 400:
                    profile["errors"] += 1
        return profile

    def get_total_endpoints_count(self) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM endpoints")
            return cursor.fetchone()["cnt"]

    def get_all_endpoints(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM endpoints ORDER BY path_template ASC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_tool_stats(self) -> Dict[str, Dict[str, int]]:
        """Returns counts of PENDING / PROCESSED / FAILED for each tool."""
        stats = {}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT tool_name, status, COUNT(*) as cnt
                FROM tool_coverage
                GROUP BY tool_name, status
            """)
            for row in cursor.fetchall():
                t = row["tool_name"]
                s = row["status"]
                cnt = row["cnt"]
                if t not in stats:
                    stats[t] = {"PENDING": 0, "PROCESSED": 0, "FAILED": 0}
                stats[t][s] = cnt
        return stats

    def get_provenance_summary(self) -> Dict[str, int]:
        """Returns count of unique endpoints contributed by each tool."""
        summary = {}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT source_tool, COUNT(DISTINCT canonical_hash) as cnt
                FROM endpoint_sources
                GROUP BY source_tool
            """)
            for row in cursor.fetchall():
                summary[row["source_tool"]] = row["cnt"]
        return summary

    def get_compact_summary(self, max_sample_endpoints: int = 40) -> Dict[str, Any]:
        """
        Generates an information-dense, lightweight summary representation for the LLM context.
        Strictly stays within ~800 tokens.
        """
        total_eps = self.get_total_endpoints_count()
        tool_stats = self.get_tool_stats()
        provenance = self.get_provenance_summary()

        # Get high-value pending endpoints for each tool
        pending_samples = {}
        for tool in CRAWL_TOOLS:
            frontier = self.get_pending_frontier(tool, limit=5)
            pending_samples[tool] = frontier

        # Get recent discoveries sample
        recent_endpoints = []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT canonical_key, status_code, first_seen_iteration
                FROM endpoints
                ORDER BY first_seen_iteration DESC, path_template ASC
                LIMIT ?
            """, (max_sample_endpoints,))
            recent_endpoints = [
                f"{r['canonical_key']} (iter {r['first_seen_iteration']}, status {r['status_code']})"
                for r in cursor.fetchall()
            ]

        # Latest runs (several rows can share an iteration when tools ran in parallel)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM execution_history ORDER BY id DESC LIMIT 3")
            recent_runs = [dict(r) for r in cursor.fetchall()]

        return {
            "total_canonical_endpoints": total_eps,
            "tool_coverage_matrix": tool_stats,
            "tool_provenance_contributions": provenance,
            "pending_frontier_samples": pending_samples,
            "latest_run": recent_runs[0] if recent_runs else None,
            "recent_runs": recent_runs,
            "tool_run_counts": self.get_tool_run_counts(),
            "surface_profile": self.get_surface_profile(),
            "recent_canonical_endpoints_sample": recent_endpoints[:max_sample_endpoints],
        }

    def record_iteration(self, record: Dict[str, Any]) -> None:
        """Records the outcome of one tool run."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO execution_history (
                    iteration, chosen_tool, reasoning, target_seeds,
                    endpoints_before, endpoints_after, new_endpoints_discovered,
                    marginal_gain, tool_duration_seconds, requests_sent,
                    raw_results, seeds_count, status, error, rule_id, frontier_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["iteration"],
                record["chosen_tool"],
                record.get("reasoning", ""),
                json.dumps(record.get("target_seeds", [])),
                record["endpoints_before"],
                record["endpoints_after"],
                record["new_endpoints_discovered"],
                record["marginal_gain"],
                record.get("tool_duration_seconds", 0.0),
                record.get("requests_sent", 0),
                record.get("raw_results", 0),
                record.get("seeds_count", len(record.get("target_seeds", []))),
                record.get("status", "ok"),
                record.get("error"),
                record.get("rule_id"),
                record.get("frontier_fingerprint"),
            ))
            conn.commit()
