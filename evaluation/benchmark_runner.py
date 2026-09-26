"""
Benchmark Evaluation Runner.
Evaluates crawl experiments against Ground Truth and generates scientific comparison reports.
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from evaluation.metrics import calculate_benchmark_metrics

logger = logging.getLogger("orchestrator.benchmark")


class BenchmarkRunner:
    def __init__(self, ground_truth_file: str = "ground_truth/avideo_routes.json"):
        self.ground_truth_file = Path(ground_truth_file)
        self.ground_truth = self._load_json(self.ground_truth_file)

    def _load_json(self, path: Path) -> List[Dict[str, Any]]:
        if not path.is_file():
            logger.error(f"File not found: {path}")
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"Failed to read {path}: {e}")
            return []

    def evaluate_file(self, endpoints_file: str, total_requests: int = 0) -> Dict[str, Any]:
        """Evaluates a single discovered endpoints JSON file."""
        discovered = self._load_json(Path(endpoints_file))
        return calculate_benchmark_metrics(
            discovered_endpoints=discovered,
            ground_truth_endpoints=self.ground_truth,
            total_requests=total_requests
        )

    def generate_markdown_report(self, results: Dict[str, Dict[str, Any]], output_file: str = "output/reports/benchmark_report.md") -> str:
        """Generates an academic Markdown report comparing experiment results."""
        out_path = Path(output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# DAST Crawl Performance & Endpoint Discovery Benchmark",
            "",
            "## 1. Executive Summary",
            "",
            f"- **Target Application**: AVideo CMS (v11.6)",
            f"- **Ground Truth Size**: {len(self.ground_truth)} canonical endpoints",
            "",
            "## 2. Quantitative Evaluation Table",
            "",
            "| Experiment / Architecture | Total Discovered | True Positives | Recall ($R_{EP}$) | Precision ($P_{EP}$) | $F_1$ Score | Efficiency ($E_{REQ}$) |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
        ]

        for name, m in results.items():
            r = m.get("recall_R_ep", 0.0) * 100
            p = m.get("precision_P_ep", 0.0) * 100
            f1 = m.get("f1_score", 0.0)
            eff = m.get("request_efficiency_E_req", 0.0)
            lines.append(
                f"| **{name}** | {m.get('discovered_total', 0)} | {m.get('true_positives', 0)} | "
                f"{r:.1f}% | {p:.1f}% | {f1:.3f} | {eff:.4f} |"
            )

        lines.extend([
            "",
            "## 3. Analysis & Key Insights",
            "",
            "- **Recall ($R_{EP}$)** reflects attack surface coverage against verified application routes.",
            "- **Precision ($P_{EP}$)** reflects filtering efficacy against crawler traps and regex noise.",
            "- **Adaptive Orchestrator** leverages specialized tools (Firecrawl for UI navigation, ZAP for spidering, Wapiti for parameter mutation) guided by LLM cognitive reasoning.",
            ""
        ])

        report_content = "\n".join(lines)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report_content)

        logger.info(f"[REPORT] Saved benchmark evaluation report to {out_path}")
        return report_content


def main():
    parser = argparse.ArgumentParser(description="Evaluate DAST crawling experiments against ground truth")
    parser.add_argument(
        "--input",
        default="output/normalized_endpoints.json",
        help="Path to discovered endpoints JSON file"
    )
    parser.add_argument(
        "--ground-truth",
        default="ground_truth/avideo_routes.json",
        help="Path to ground truth JSON file"
    )
    parser.add_argument(
        "--report-out",
        default="output/reports/benchmark_report.md",
        help="Output markdown report path"
    )

    args = parser.parse_args()
    runner = BenchmarkRunner(ground_truth_file=args.ground_truth)
    metrics = runner.evaluate_file(args.input)

    print("\n" + "=" * 50)
    print("BENCHMARK EVALUATION METRICS:")
    print(json.dumps(metrics, indent=2))
    print("=" * 50 + "\n")

    runner.generate_markdown_report({"Adaptive Orchestrator Run": metrics}, output_file=args.report_out)


if __name__ == "__main__":
    main()
