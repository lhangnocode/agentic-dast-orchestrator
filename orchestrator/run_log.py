"""
Structured run log: one JSON object per line (JSONL), appended to a file.

Every crawl gets a `run_id`; each event carries a UTC timestamp, the run id and an `event`
name (run_started, decision, tool_run, run_finished). The file is meant for reproducing and
diagnosing experiments and as input for evaluation scripts; the human-readable log stays in
output/logs/orchestrator.log.
"""

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("orchestrator.run_log")


class RunLog:
    def __init__(self, path: Optional[str], run_id: Optional[str] = None):
        """path=None disables the log (e.g. in unit tests)."""
        self.path = Path(path) if path else None
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, event: str, **fields: Any) -> None:
        if not self.path:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "event": event,
            **fields,
        }
        try:
            line = json.dumps(record, default=str, ensure_ascii=False)
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # logging must never break a crawl
            logger.warning(f"[RUN_LOG] Could not write event '{event}': {e}")
