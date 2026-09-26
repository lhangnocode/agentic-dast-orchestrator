import json
import tempfile
from pathlib import Path
import pytest

from orchestrator.loop import AdaptiveOrchestrator


def test_adaptive_orchestrator_end_to_end():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = str(tmp_path / "test_orchestrator.db")

        test_config = {
            "target": {
                "base_url": "http://localhost/",
                "allowed_hosts": ["localhost", "127.0.0.1"]
            },
            "orchestrator": {
                "max_iterations": 2,
                "marginal_gain_threshold": 0.01,
                "compactor_max_endpoints": 20
            },
            "tools": {
                "zap": {
                    "api_url": "http://127.0.0.1:59999",  # Safe mock/unreachable
                    "api_key": "dummy"
                },
                "wapiti": {
                    "binary": "wapiti",
                    "scope": "domain",
                    "session_file": str(tmp_path / "session.db"),
                    "report_file": str(tmp_path / "report.json"),
                    "log_file": str(tmp_path / "wapiti.log")
                },
                "firecrawl": {
                    "api_url": "https://api.firecrawl.dev/v2",
                    "model": "spark-1-pro"
                }
            },
            "storage": {
                "sqlite_db": db_path
            }
        }

        orchestrator = AdaptiveOrchestrator(test_config)
        summary = orchestrator.run()

        assert summary["total_canonical_endpoints"] > 1
        assert "firecrawl" in summary["tool_provenance"] or "initial_seed" in summary["tool_provenance"]
        assert Path(summary["output_txt"]).exists()
        assert Path(summary["output_json"]).exists()
