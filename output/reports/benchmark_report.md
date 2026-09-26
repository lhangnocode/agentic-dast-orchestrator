# DAST Crawl Performance & Endpoint Discovery Benchmark

## 1. Executive Summary

- **Target Application**: AVideo CMS (v11.6)
- **Ground Truth Size**: 112 canonical endpoints

## 2. Quantitative Evaluation Table

| Experiment / Architecture | Total Discovered | True Positives | Recall ($R_{EP}$) | Precision ($P_{EP}$) | $F_1$ Score | Efficiency ($E_{REQ}$) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Adaptive Orchestrator Run** | 17 | 15 | 13.4% | 88.2% | 0.233 | 0.0000 |

## 3. Analysis & Key Insights

- **Recall ($R_{EP}$)** reflects attack surface coverage against verified application routes.
- **Precision ($P_{EP}$)** reflects filtering efficacy against crawler traps and regex noise.
- **Adaptive Orchestrator** leverages specialized tools (Firecrawl for UI navigation, ZAP for spidering, Wapiti for parameter mutation) guided by LLM cognitive reasoning.
