import subprocess

from orchestrator.auth import AuthContext, expand_placeholders, prepare_auth
from tools.wapiti_wrapper import WapitiWrapper
from tools.zap_wrapper import ZapWrapper


class FakeResponse:
    def __init__(self, payload, cookies=None):
        self._payload = payload
        self.cookies = type("Jar", (), {"get_dict": lambda _self: dict(cookies or {})})()

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_expand_placeholders(monkeypatch):
    monkeypatch.setenv("AUTH_USER", "admin@example.test")
    data = {"a": "user=${AUTH_USER}", "b": ["${MISSING}"], "c": 3}
    assert expand_placeholders(data) == {"a": "user=admin@example.test", "b": ["${MISSING}"], "c": 3}
    assert expand_placeholders("Bearer ${T}", {"T": "abc"}) == "Bearer abc"


def test_prepare_auth_disabled():
    assert prepare_auth({}).active is False


def test_prepare_auth_token_login(monkeypatch):
    monkeypatch.setenv("AUTH_USER", "u@example.test")
    monkeypatch.setenv("AUTH_PASSWORD", "pw")
    seen = {}

    def fake_request(method, url, json=None, data=None, timeout=None):
        seen.update(method=method, url=url, json=json)
        return FakeResponse({"authentication": {"token": "jwt123"}}, cookies={"sid": "s1"})

    monkeypatch.setattr("orchestrator.auth.requests.request", fake_request)
    ctx = prepare_auth({"auth": {
        "enabled": True,
        "login": {"url": "http://localhost:3000/login", "json": {"email": "${AUTH_USER}", "password": "${AUTH_PASSWORD}"},
                  "token_path": "authentication.token"},
        "headers": {"Authorization": "Bearer ${AUTH_TOKEN}"},
        "cookies": {"token": "${AUTH_TOKEN}"},
    }})
    assert seen["json"] == {"email": "u@example.test", "password": "pw"}
    assert ctx.headers == {"Authorization": "Bearer jwt123"}
    assert ctx.cookies == {"sid": "s1", "token": "jwt123"}


def test_prepare_auth_unresolved_placeholder_disables_auth():
    ctx = prepare_auth({"auth": {"enabled": True, "headers": {"Authorization": "Bearer ${NOPE_NOT_SET}"}}})
    assert ctx.active is False


def test_wapiti_command_carries_auth(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("tools.wapiti_wrapper.subprocess.run", fake_run)
    wapiti = WapitiWrapper(session_db_path=str(tmp_path / "s.db"), report_path=str(tmp_path / "r.json"),
                           log_path=str(tmp_path / "w.log"),
                           headers={"Authorization": "Bearer jwt"}, cookies={"token": "jwt"})
    wapiti.run_crawl_only("http://localhost:3000/")
    cmd = captured["cmd"]
    assert cmd[cmd.index("-H") + 1] == "Authorization: Bearer jwt"
    assert cmd[cmd.index("-C") + 1] == "token=jwt"


def test_zap_auth_headers_include_cookie():
    zap = ZapWrapper()
    zap.set_auth({"Authorization": "Bearer jwt"}, {"token": "jwt", "lang": "en"})
    assert zap.auth_headers == {"Authorization": "Bearer jwt", "Cookie": "token=jwt; lang=en"}
    assert AuthContext(cookies={"a": "1"}).cookie_header() == "a=1"
