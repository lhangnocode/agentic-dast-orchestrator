"""
Scientific Evaluation Metrics for DAST Crawl Performance.
Implements Recall (R_ep), Precision (P_ep), F1 Score,
Request Efficiency (E_req), and Redundancy Ratio (RR).
"""

from typing import Any, Dict, List, Set


def calculate_benchmark_metrics(
    discovered_endpoints: List[Dict[str, Any]],
    ground_truth_endpoints: List[Dict[str, Any]],
    total_requests: int = 0
) -> Dict[str, Any]:
    """
    Computes rigorous academic benchmark metrics comparing discovered canonical
    endpoints against an independent ground truth.
    """
    disc_hashes: Set[str] = {ep["canonical_hash"] for ep in discovered_endpoints if "canonical_hash" in ep}
    gt_hashes: Set[str] = {ep["canonical_hash"] for ep in ground_truth_endpoints if "canonical_hash" in ep}

    tp_hashes = disc_hashes.intersection(gt_hashes)
    fp_hashes = disc_hashes.difference(gt_hashes)
    fn_hashes = gt_hashes.difference(disc_hashes)

    tp = len(tp_hashes)
    fp = len(fp_hashes)
    fn = len(fn_hashes)

    recall = tp / len(gt_hashes) if gt_hashes else 0.0
    precision = tp / len(disc_hashes) if disc_hashes else 0.0
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

    # Request efficiency metrics
    req_efficiency = (len(disc_hashes) / total_requests) if total_requests > 0 else 0.0
    redundancy_ratio = (1.0 - (len(disc_hashes) / total_requests)) if total_requests > 0 else 0.0

    # Breakdown by HTTP Method
    method_dist: Dict[str, int] = {}
    for ep in discovered_endpoints:
        m = ep.get("method", "GET").upper()
        method_dist[m] = method_dist.get(m, 0) + 1

    return {
        "ground_truth_total": len(gt_hashes),
        "discovered_total": len(disc_hashes),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "recall_R_ep": round(recall, 4),
        "precision_P_ep": round(precision, 4),
        "f1_score": round(f1, 4),
        "total_requests": total_requests,
        "request_efficiency_E_req": round(req_efficiency, 4),
        "redundancy_ratio_RR": round(redundancy_ratio, 4),
        "method_distribution": method_dist
    }
