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


class _AuthFakeZap:
    """zapv2 stand-in for browser-login setup; `accepted` decides what the login traffic looks like."""

    def __init__(self, accepted):
        self.calls = []
        status = "200 OK" if accepted else "401 Unauthorized"
        outer = self

        class Ns:
            def __init__(self, name, returns=None):
                self.name, self.returns = name, returns or {}

            def __getattr__(self, attr):
                def call(*a, **k):
                    outer.calls.append((f"{self.name}.{attr}", a))
                    return self.returns.get(attr, "1")
                return call

        self.context = Ns("context", {"new_context": "1"})
        self.context.context_list = []
        self.authentication = Ns("authentication")
        self.sessionManagement = Ns("sessionManagement")
        self.users = Ns("users", {"new_user": "7"})

        class Core:
            def number_of_messages(self, baseurl=None):
                return 0

            def messages(self, baseurl, start, count):
                return [{"requestHeader": "POST http://host.docker.internal:3000/rest/user/login HTTP/1.1",
                         "responseHeader": f"HTTP/1.1 {status}"}]
        self.core = Core()


def _zap_with(fake):
    zap = ZapWrapper(target_base="http://localhost:3000/", target_host_alias="host.docker.internal")
    zap._client = fake
    return zap


def test_browser_login_success_sets_crawl_user():
    fake = _AuthFakeZap(accepted=True)
    zap = _zap_with(fake)
    assert zap.setup_browser_login("http://localhost:3000/#/login", "u@example.test", "pw") is True
    assert zap.client_spider_user == ("dast-orchestrator", "crawler")
    method = next(a for name, a in fake.calls if name == "authentication.set_authentication_method")
    assert method[1] == "browserBasedAuthentication"
    assert "loginPageUrl=http%3A%2F%2Fhost.docker.internal%3A3000%2F%23%2Flogin" in method[2]
    assert ("sessionManagement.set_session_management_method", ("1", "autoDetectSessionManagement")) in fake.calls


def test_browser_login_failure_keeps_header_fallback():
    fake = _AuthFakeZap(accepted=False)
    zap = _zap_with(fake)
    assert zap.setup_browser_login("http://localhost:3000/#/login", "u@example.test", "bad") is False
    assert zap.client_spider_user is None
    assert any(name == "context.remove_context" for name, _ in fake.calls)


def test_browser_login_settings_from_config(monkeypatch):
    from tools.registry import _browser_login_settings
    monkeypatch.setenv("AUTH_USER", "u@example.test")
    monkeypatch.setenv("AUTH_PASSWORD", "pw")
    cfg = {"auth": {"enabled": True, "browser_login": {"enabled": True,
                                                        "login_page_url": "http://localhost:3000/#/login"}}}
    s = _browser_login_settings(cfg)
    assert s["username"] == "u@example.test" and s["password"] == "pw"
    monkeypatch.delenv("AUTH_USER")
    assert _browser_login_settings(cfg) is None                     # unresolved -> no browser login
    assert _browser_login_settings({"auth": {"enabled": False}}) is None
