import logging
import os
import tempfile
from pathlib import Path

from normalization.normalizer import normalize_endpoint
from storage.state_store import StateStore
from tools.firecrawl_client import FirecrawlClient
from tools.firecrawl_prompt import (MAX_PROMPT_CHARS, SAFETY_CONSTRAINTS, PromptParts, read_text_file,
                                    render_prompt, save_prompt)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "configs" / "prompts" / "firecrawl_agent.md").read_text()


def _parts(**kw):
    base = dict(target_url="http://localhost:3000/", seeds=["http://localhost:3000/", "http://localhost:3000/#/login"],
                known_routes="Client-side routes (1): #/login",
                focus="new pages", auth_instructions="Sign in as the test user.")
    base.update(kw)
    return PromptParts(**base)


def test_template_renders_every_section_in_order():
    text = render_prompt(TEMPLATE, _parts()).text
    for value in ("http://localhost:3000/#/login", "#/login", "new pages",
                  "Sign in as the test user.", SAFETY_CONSTRAINTS):
        assert value in text
    headings = [line for line in text.splitlines() if line.startswith("# ")]
    assert [h.split(".")[0] for h in headings] == [f"# {i}" for i in range(1, 9)]
    assert "${" not in text
    for field in ("visited_urls", "interaction_urls", "seen_urls"):   # matches the response schema
        assert field in text


def test_prompt_holds_no_application_context():
    text = render_prompt(TEMPLATE, _parts()).text
    assert "app_context" not in text and "About this application" not in text and "Workflows" not in text
    assert not (ROOT / "configs" / "contexts").exists()


def test_user_text_with_braces_and_dollars_is_safe():
    focus = "Prices like $5 and ${not_a_var} and {curly} stay as written."
    assert focus in render_prompt(TEMPLATE, _parts(focus=focus)).text


def test_safety_rules_cannot_be_removed_by_editing_the_template():
    text = render_prompt("Explore ${target_url}. No rules here.", _parts()).text
    assert SAFETY_CONSTRAINTS in text


def test_missing_template_file_gives_empty_text_and_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert read_text_file("/nonexistent/template.md", "Prompt template") == ""
    assert "not found" in caplog.text


def test_long_inputs_are_cut_in_priority_order():
    known = "\n".join(f"- /api/resource{i}: a, b, c" for i in range(2000))
    many_seeds = [f"http://localhost:3000/page{i}" for i in range(400)]
    rendered = render_prompt(TEMPLATE, _parts(known_routes=known, seeds=many_seeds))
    assert len(rendered.text) <= MAX_PROMPT_CHARS
    assert rendered.truncated == ["known_routes", "seeds"]
    assert SAFETY_CONSTRAINTS in rendered.text


def test_prompt_is_saved_by_hash(tmp_path):
    rendered = render_prompt(TEMPLATE, _parts())
    path = save_prompt(rendered, str(tmp_path))
    assert path.name.startswith(rendered.sha256[:16]) and path.read_text() == rendered.text


def test_template_holds_no_credentials():
    secrets = [v for k, v in os.environ.items() if k in ("AUTH_USER", "AUTH_PASSWORD") and v]
    content = (ROOT / "configs/prompts/firecrawl_agent.md").read_text()
    assert not any(sec in content for sec in secrets)


def test_known_routes_summary_is_bounded_and_grouped():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = StateStore(db_path=f"{tmpdir}/s.db")
        for url in ["http://localhost:3000/#/login", "http://localhost:3000/#/basket",
                    "http://localhost:3000/api/Users", "http://localhost:3000/api/Feedbacks",
                    "http://localhost:3000/rest/user/whoami", "http://localhost:3000/ftp"]:
            store.insert_or_update_endpoint(normalize_endpoint(url, source_tool="zap_client_spider", status_code=200), 1)
        summary = store.get_known_routes_summary()
        assert summary.splitlines()[0] == "Client-side routes (2): #/basket, #/login"
        assert "- /api: Feedbacks, Users" in summary and "- /ftp" in summary
        assert len(store.get_known_routes_summary(max_chars=60)) <= 60 + len("(... more not listed)")


class _FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(json)
        return type("R", (), {"status_code": 200, "json": lambda self: {"id": f"job{len(outer.posts)}"},
                              "text": ""})()

    def get(self, url, headers=None, timeout=None):
        return type("R", (), {"status_code": 200, "json": lambda self: {
            "status": "completed", "data": {"links": ["https://abc.trycloudflare.com/#/new"]}}})()


outer = None


def test_client_batches_all_seeds_and_passes_credit_limit(tmp_path):
    global outer
    client = FirecrawlClient(api_key="key", target_base="http://localhost:3000/",
                             public_tunnel_url="https://abc.trycloudflare.com", batch_size=10,
                             max_credits=5000, prompts_dir=str(tmp_path), poll_interval_seconds=0,
                             prompt_template_file=str(ROOT / "configs/prompts/firecrawl_agent.md"))
    client.session = outer = _FakeSession()
    client.known_routes_provider = lambda max_chars: "Client-side routes (1): #/login"
    seeds = [f"http://localhost:3000/page{i}" for i in range(25)]
    results = client.run_agent_discovery(seeds, focus="new pages")

    assert [len(p["urls"]) for p in outer.posts] == [10, 10, 5]           # every seed, in batches
    assert all(p["maxCredits"] == 5000 for p in outer.posts)
    assert outer.posts[0]["urls"][0] == "https://abc.trycloudflare.com/page0"
    assert "#/login" in outer.posts[0]["prompt"] and "new pages" in outer.posts[0]["prompt"]
    assert {r["url"] for r in results} == {"http://localhost:3000/#/new"}  # mapped back from the tunnel
    assert len(client.last_prompt_files) == 3 and client.last_status == "ok"


def test_client_saves_full_job_response(tmp_path):
    global outer
    client = FirecrawlClient(api_key="key", target_base="http://localhost:3000/",
                             public_tunnel_url="https://abc.trycloudflare.com", batch_size=10,
                             prompts_dir=str(tmp_path / "prompts"), responses_dir=str(tmp_path / "responses"),
                             poll_interval_seconds=0,
                             prompt_template_file=str(ROOT / "configs/prompts/firecrawl_agent.md"))
    client.session = outer = _FakeSession()
    client.run_agent_discovery(["http://localhost:3000/a", "http://localhost:3000/b"])

    assert len(client.last_response_files) == 1
    import json
    saved = json.loads(Path(client.last_response_files[0]).read_text())
    assert saved["job_id"] == "job1" and saved["outcome"] == "completed"
    assert saved["seeds"] == ["http://localhost:3000/a", "http://localhost:3000/b"]
    assert len(saved["prompt_sha256"]) == 64
    assert saved["response"]["data"]["links"] == ["https://abc.trycloudflare.com/#/new"]  # raw API answer kept


def test_client_reads_job_startup_from_traffic_log(tmp_path):
    global outer
    import json
    import time
    from datetime import datetime, timezone
    log = tmp_path / "traffic.jsonl"
    soon = time.time() + 30   # a request the proxy logged after the job was submitted
    log.write_text("\n".join(json.dumps({"ts": datetime.fromtimestamp(t, timezone.utc).isoformat(), "path": "/"})
                             for t in (time.time() - 600, soon, soon + 5)) + "\n")
    client = FirecrawlClient(api_key="key", target_base="http://localhost:3000/",
                             public_tunnel_url="https://abc.trycloudflare.com", prompts_dir=str(tmp_path / "p"),
                             responses_dir=str(tmp_path / "r"), traffic_log=str(log), poll_interval_seconds=0,
                             prompt_template_file=str(ROOT / "configs/prompts/firecrawl_agent.md"))
    client.session = outer = _FakeSession()
    client.run_agent_discovery(["http://localhost:3000/a"])
    saved = json.loads(Path(client.last_response_files[0]).read_text())
    assert saved["requests_observed"] == 2                       # the old request is not this job's
    assert abs(client.last_first_request_at - soon) < 0.01
    assert 29 < saved["startup_seconds"] < 31


def test_client_keeps_links_of_a_job_that_sent_no_request(tmp_path):
    """A job served without any request through the tunnel (e.g. from Firecrawl's cache) must still return
    its links once, not re-poll until the timeout (regression: KeyError 'first_request_at')."""
    global outer
    import json
    import time
    from datetime import datetime, timezone
    log = tmp_path / "traffic.jsonl"
    log.write_text(json.dumps({"ts": datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat(),
                               "path": "/"}) + "\n")
    client = FirecrawlClient(api_key="key", target_base="http://localhost:3000/",
                             public_tunnel_url="https://abc.trycloudflare.com", prompts_dir=str(tmp_path / "p"),
                             responses_dir=str(tmp_path / "r"), traffic_log=str(log), poll_interval_seconds=0,
                             prompt_template_file=str(ROOT / "configs/prompts/firecrawl_agent.md"))
    client.session = outer = _FakeSession()
    results = client.run_agent_discovery(["http://localhost:3000/a"])
    assert results and client.last_status == "ok"
    saved = json.loads(Path(client.last_response_files[0]).read_text())
    assert saved["requests_observed"] == 0 and "startup_seconds" not in saved
    assert client.last_first_request_at is None


def test_response_lists_are_ingested_once_with_their_kind():
    from tools.firecrawl_client import DiscoveredLinksSchema, extract_urls
    assert set(DiscoveredLinksSchema.model_json_schema()["properties"]) == {"visited_urls", "interaction_urls", "seen_urls"}
    data = {"visited_urls": ["https://t/a", "https://t/b"], "interaction_urls": ["https://t/b", "https://t/c"],
            "seen_urls": ["https://t/d", "https://t/a"], "links": ["https://t/e"]}
    assert extract_urls(data) == [("https://t/b", "interaction"), ("https://t/c", "interaction"),
                                  ("https://t/a", "visited"), ("https://t/d", "seen"), ("https://t/e", "unspecified")]
    assert extract_urls(None) == [] and extract_urls({"seen_urls": None}) == []


def test_client_returns_every_list_and_partial_results(tmp_path):
    global outer

    class _Session(_FakeSession):
        def __init__(self, answer):
            super().__init__()
            self.answer = answer

        def get(self, url, headers=None, timeout=None):
            return type("R", (), {"status_code": 200, "json": lambda s: self.answer})()

    tun = "https://abc.trycloudflare.com"
    for answer, status in (
        ({"status": "completed", "data": {"visited_urls": [tun + "/a"], "interaction_urls": [tun + "/b"],
                                          "seen_urls": [tun + "/c"]}}, "ok"),
        ({"status": "failed", "stopReason": "credits", "partial": {"interaction_urls": [tun + "/b"]}}, "failed"),
    ):
        client = FirecrawlClient(api_key="key", target_base="http://localhost:3000/", public_tunnel_url=tun,
                                 prompts_dir=str(tmp_path), poll_interval_seconds=0,
                                 prompt_template_file=str(ROOT / "configs/prompts/firecrawl_agent.md"))
        client.session = outer = _Session(answer)
        results = client.run_agent_discovery(["http://localhost:3000/"])
        assert client.last_status == status
        kinds = {r["url"]: r["firecrawl_kind"] for r in results}
        if status == "ok":
            assert kinds == {"http://localhost:3000/a": "visited", "http://localhost:3000/b": "interaction",
                             "http://localhost:3000/c": "seen"}
        else:
            assert kinds == {"http://localhost:3000/b": "interaction"}
