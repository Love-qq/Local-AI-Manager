import urllib.request
from local_ai_manager import HFTokenRedirectHandler, APP_USER_AGENT


def test_hf_redirect_handler_removes_authorization_cross_host(monkeypatch):
    handler = HFTokenRedirectHandler()
    req = urllib.request.Request(
        "https://huggingface.co/repo/resolve/main/model.gguf",
        headers={"Authorization": "Bearer secret"}
    )
    # Simulate the base handler's redirected request while preserving headers.
    def fake_super(self, req, fp, code, msg, headers, newurl):
        return urllib.request.Request(newurl, headers=dict(req.headers))
    monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", fake_super)
    redirected = handler.redirect_request(req, None, 302, "Found", {}, "https://cdn-lfs.huggingface.co/file")
    assert redirected.get_header("Authorization") is None


def test_hf_redirect_handler_keeps_authorization_same_host(monkeypatch):
    handler = HFTokenRedirectHandler()
    req = urllib.request.Request("https://huggingface.co/a", headers={"Authorization": "Bearer secret"})
    def fake_super(self, req, fp, code, msg, headers, newurl):
        return urllib.request.Request(newurl, headers=dict(req.headers))
    monkeypatch.setattr(urllib.request.HTTPRedirectHandler, "redirect_request", fake_super)
    redirected = handler.redirect_request(req, None, 302, "Found", {}, "https://huggingface.co/b")
    assert redirected.get_header("Authorization") == "Bearer secret"


def test_single_user_agent_version():
    # Версия поднята в v2 (фазы B-I): старый номер оставлять нельзя.
    assert APP_USER_AGENT == "Local-AI-Manager/48.0"
