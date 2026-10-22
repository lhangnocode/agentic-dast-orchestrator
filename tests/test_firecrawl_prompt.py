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
                app_context="Shop app. Workflow: basket -> checkout.", known_routes="Client-side routes (1): #/login",
                focus="new pages", auth_instructions="Sign in as the test user.")
    base.update(kw)
    return PromptParts(**base)


def test_template_renders_every_section_in_order():
    text = render_prompt(TEMPLATE, _parts()).text
    for value in ("http://localhost:3000/#/login", "basket -> checkout", "#/login", "new pages",
                  "Sign in as the test user.", SAFETY_CONSTRAINTS):
        assert value in text
    headings = [line for line in text.splitlines() if line.startswith("# ")]
    assert [h.split(".")[0] for h in headings] == [f"# {i}" for i in range(1, 11)]
    assert "${" not in text


def test_user_text_with_braces_and_dollars_is_safe():
    ctx = "Prices like $5 and ${not_a_var} and {curly} stay as written."
    assert ctx in render_prompt(TEMPLATE, _parts(app_context=ctx)).text


def test_safety_rules_cannot_be_removed_by_editing_the_template():
    text = render_prompt("Explore ${target_url}. No rules here.", _parts()).text
    assert SAFETY_CONSTRAINTS in text


def test_missing_context_file_gives_empty_section_and_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert read_text_file("/nonexistent/context.md", "Application context") == ""
    assert "not found" in caplog.text
    assert "(no application context provided)" in render_prompt(TEMPLATE, _parts(app_context="")).text


def test_long_inputs_are_cut_in_priority_order():
    known = "\n".join(f"- /api/resource{i}: a, b, c" for i in range(2000))
    many_seeds = [f"http://localhost:3000/page{i}" for i in range(400)]
    context = "Important workflow description. " * 20
    rendered = render_prompt(TEMPLATE, _parts(known_routes=known, seeds=many_seeds, app_context=context))
    assert len(rendered.text) <= MAX_PROMPT_CHARS
    assert rendered.truncated[0] == "known_routes"
    assert "app_context" not in rendered.truncated        # context survives intact
    assert context.strip() in rendered.text


def test_prompt_is_saved_by_hash(tmp_path):
    rendered = render_prompt(TEMPLATE, _parts())
    path = save_prompt(rendered, str(tmp_path))
    assert path.name.startswith(rendered.sha256[:16]) and path.read_text() == rendered.text


def test_template_and_context_files_hold_no_credentials():
    secrets = [v for k, v in os.environ.items() if k in ("AUTH_USER", "AUTH_PASSWORD") and v]
    for f in ("configs/prompts/firecrawl_agent.md", "configs/contexts/juice-shop.md"):
        content = (ROOT / f).read_text()
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
