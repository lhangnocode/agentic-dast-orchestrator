"""
Tool names used across the orchestrator (coverage matrix, history rows, result `source` labels).
"""

from enum import Enum


class ToolActionEnum(str, Enum):
    ZAP_TRADITIONAL_SPIDER = "zap_traditional_spider"
    ZAP_AJAX_SPIDER = "zap_ajax_spider"
    ZAP_CLIENT_SPIDER = "zap_client_spider"
    WAPITI_FORM_CRAWL = "wapiti_form_crawl"
    FIRECRAWL_NAVIGATE = "firecrawl_navigate"
    HTTP_PROBE = "http_probe"
