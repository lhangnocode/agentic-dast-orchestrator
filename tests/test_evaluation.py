import tempfile
from pathlib import Path
from evaluation.metrics import calculate_benchmark_metrics
from evaluation.benchmark_runner import BenchmarkRunner


def test_metrics_calculation():
    gt = [
        {"canonical_hash": "hash1", "method": "GET"},
        {"canonical_hash": "hash2", "method": "GET"},
        {"canonical_hash": "hash3", "method": "POST"},
        {"canonical_hash": "hash4", "method": "GET"},
    ]

    discovered = [
        {"canonical_hash": "hash1", "method": "GET"},
        {"canonical_hash": "hash2", "method": "GET"},
        {"canonical_hash": "hash5", "method": "GET"},  # False positive
    ]

    metrics = calculate_benchmark_metrics(discovered, gt, total_requests=10)

    assert metrics["ground_truth_total"] == 4
    assert metrics["discovered_total"] == 3
    assert metrics["true_positives"] == 2
    assert metrics["false_positives"] == 1
    assert metrics["false_negatives"] == 2
    assert metrics["recall_R_ep"] == 0.5
    assert round(metrics["precision_P_ep"], 2) == 0.67
    assert metrics["request_efficiency_E_req"] == 0.3
    assert metrics["redundancy_ratio_RR"] == 0.7


def test_benchmark_runner_report():
    with tempfile.TemporaryDirectory() as tmpdir:
        gt_file = Path(tmpdir) / "gt.json"
        gt_file.write_text('[{"canonical_hash": "h1"}]')

        runner = BenchmarkRunner(ground_truth_file=str(gt_file))
        assert len(runner.ground_truth) == 1

        report_file = Path(tmpdir) / "report.md"
        report_md = runner.generate_markdown_report(
            {"Test Exp": {"recall_R_ep": 0.8, "precision_P_ep": 0.9, "f1_score": 0.85, "discovered_total": 10, "true_positives": 8}},
            output_file=str(report_file)
        )
        assert "# DAST Crawl Performance" in report_md
        assert report_file.exists()
