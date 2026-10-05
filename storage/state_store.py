"""
Relational SQLite State Store for the Orchestrator.
Maintains the Canonical Attack Surface, Tool Coverage Matrix (U x T),
Provenance, and Iterative Execution History.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
                    llm_prompt_tokens INTEGER DEFAULT 0,
                    llm_completion_tokens INTEGER DEFAULT 0,
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

            # Indices for rapid querying
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_endpoints_path ON endpoints(path_template);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tool_coverage_lookup ON tool_coverage(tool_name, status);")
            conn.commit()

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

        is_new = False
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Check existence
            cursor.execute("SELECT canonical_hash, status_code FROM endpoints WHERE canonical_hash = ?", (canonical_hash,))
            row = cursor.fetchone()

            if row is None:
                is_new = True
                cursor.execute("""
                    INSERT INTO endpoints (
                        canonical_hash, canonical_key, method, path_template,
                        query_params, normalized_url, status_code, first_seen_iteration
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (canonical_hash, canonical_key, method, path_template, query_params, normalized_url, status_code, current_iteration))

                # Initialize coverage matrix for standard tools as PENDING
                mapped_tool = source_tool
                if source_tool == "firecrawl":
                    mapped_tool = "firecrawl_navigate"
                elif source_tool == "zap":
                    mapped_tool = "zap_traditional_spider"
                elif source_tool == "wapiti":
                    mapped_tool = "wapiti_form_crawl"

                for t in ["zap_traditional_spider", "zap_ajax_spider", "wapiti_form_crawl", "firecrawl_navigate"]:
                    init_status = "PROCESSED" if t == mapped_tool else "PENDING"
                    cursor.execute("""
                        INSERT INTO tool_coverage (canonical_hash, tool_name, status, last_attempt_iteration)
                        VALUES (?, ?, ?, ?)
                    """, (canonical_hash, t, init_status, current_iteration if t == mapped_tool else 0))
            else:
                # Update status code if transitioning to 200 OK
                if status_code == 200 and row["status_code"] != 200:
                    cursor.execute("UPDATE endpoints SET status_code = 200 WHERE canonical_hash = ?", (canonical_hash,))

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

    def get_pending_frontier(self, tool_name: str, limit: int = 30) -> List[str]:
        """
        Retrieves normalized seed URLs that have not yet been processed by tool_name.
        Prioritizes endpoints with HTTP 200 status.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.normalized_url
                FROM endpoints e
                JOIN tool_coverage tc ON e.canonical_hash = tc.canonical_hash
                WHERE tc.tool_name = ? AND tc.status = 'PENDING'
                ORDER BY e.status_code = 200 DESC, e.first_seen_iteration ASC
                LIMIT ?
            """, (tool_name, limit))
            rows = cursor.fetchall()
            return [row["normalized_url"] for row in rows]

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
        for tool in ["zap_traditional_spider", "zap_ajax_spider", "wapiti_form_crawl", "firecrawl_navigate"]:
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

        # Get latest iteration history
        latest_history = None
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM execution_history ORDER BY iteration DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                latest_history = dict(row)

        return {
            "total_canonical_endpoints": total_eps,
            "tool_coverage_matrix": tool_stats,
            "tool_provenance_contributions": provenance,
            "pending_frontier_samples": pending_samples,
            "latest_run": latest_history,
            "recent_canonical_endpoints_sample": recent_endpoints[:max_sample_endpoints],
        }

    def record_iteration(self, record: Dict[str, Any]) -> None:
        """Records the outcome of an orchestration iteration."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO execution_history (
                    iteration, chosen_tool, reasoning, target_seeds,
                    endpoints_before, endpoints_after, new_endpoints_discovered,
                    marginal_gain, tool_duration_seconds, llm_prompt_tokens, llm_completion_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                record.get("llm_prompt_tokens", 0),
                record.get("llm_completion_tokens", 0),
            ))
            conn.commit()
