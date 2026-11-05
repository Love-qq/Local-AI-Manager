"""Регрессия: локальные запросы идут мимо прокси, внешние — через прокси.

Ни один локальный адрес не должен попадать в `urllib.request.urlopen` или в
`build_opener(...).open` напрямую: `urllib` по умолчанию уводит запрос через
HTTP_PROXY/HTTPS_PROXY (или системный прокси Windows), а прокси не может
достучаться до 127.0.0.1. Локальные запросы обязаны идти через
`lamcore.localnet.open_local` / `local_opener`, внешние — через системный прокси.

Тесты не требуют Tkinter: только AST-анализ исходников и mock-серверы.
"""

import ast
import http.server
import threading
import urllib.request
from pathlib import Path
from typing import ClassVar

import pytest

from lamcore.localnet import is_local_url, local_opener, open_local

ROOT = Path(__file__).resolve().parent
LOCAL_MARKERS = ("127.0.0.1", "localhost", "[::1]", "::1")

# Файлы, где локальная сеть вообще может использоваться.
NETWORK_SOURCES = ["local_ai_manager.py", "ai_bridge.py"] + sorted(
    str(path.relative_to(ROOT)) for path in (ROOT / "lamcore").glob("*.py")
)


# --- статический анализ -----------------------------------------------------

def _literal_text(node):
    """Текст строкового литерала или f-строки (без подстановок)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _mentions_local_address(text):
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in LOCAL_MARKERS)


def _call_name(node):
    """`urllib.request.urlopen` для Call-узла (или '' если не атрибут)."""
    try:
        return ast.unparse(node.func)
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - экзотический AST
        return ""


def _argument_is_local(node, local_names):
    """Аргумент urlopen/open ссылается на локальный адрес?"""
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in local_names
    if isinstance(node, ast.Call) and _call_name(node).endswith("Request"):
        return bool(node.args) and _argument_is_local(node.args[0], local_names)
    return _mentions_local_address(_literal_text(node))


def _local_names_in_scope(nodes):
    """Имена, которым присвоен локальный URL или Request с локальным URL."""
    local_names = set()
    for _ in range(4):  # фикспойнт: `req2 = req1`
        changed = False
        for node in nodes:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            is_local = _mentions_local_address(_literal_text(value))
            if isinstance(value, ast.Name):
                is_local = is_local or value.id in local_names
            if isinstance(value, ast.Call) and _call_name(value).endswith("Request"):
                is_local = is_local or (
                    bool(value.args)
                    and (_argument_is_local(value.args[0], local_names)
                         or _mentions_local_address(_literal_text(value.args[0])))
                )
            if not is_local:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in local_names:
                    local_names.add(target.id)
                    changed = True
        if not changed:
            break
    return local_names


def _own_nodes(scope):
    """Узлы тела scope без содержимого вложенных функций и классов."""
    collected = []
    stack = list(getattr(scope, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # вложенная область видимости разбирается отдельно
        collected.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return collected


def find_local_direct_requests(tree):
    """Список (строка, вызов) прямых urlopen/build_opener по локальным адресам.

    Области видимости разбираются по отдельности: имя `req` в разных функциях
    не смешивается, иначе внешние запросы получали бы ложные срабатывания.
    """
    violations = []
    scopes = [tree] + [node for node in ast.walk(tree)
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in scopes:
        nodes = _own_nodes(scope)
        local_names = _local_names_in_scope(
            [node for node in nodes if isinstance(node, ast.Assign)])
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            is_urlopen = name.endswith("urlopen")
            is_opener_open = name.endswith(".open") and "build_opener" in name
            if not (is_urlopen or is_opener_open):
                continue
            target = node.args[0] if node.args else None
            if _argument_is_local(target, local_names):
                violations.append((getattr(node, "lineno", 0), f"{name}(...)"))
    return violations


def _parse(relative_path):
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    return ast.parse(source, filename=relative_path), source


# --- статические проверки ---------------------------------------------------

def test_detector_catches_known_bad_and_good_patterns():
    """Самопроверка анализатора: без неё статический тест был бы пустым."""
    bad = ast.parse(
        "import urllib.request\n"
        "def worker(port):\n"
        "    req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/models')\n"
        "    return urllib.request.urlopen(req, timeout=1)\n"
    )
    assert len(find_local_direct_requests(bad)) == 1

    bad_opener = ast.parse(
        "import urllib.request\n"
        "def worker():\n"
        "    return urllib.request.build_opener().open('http://localhost:8080/v1')\n"
    )
    assert len(find_local_direct_requests(bad_opener)) == 1

    good = ast.parse(
        "import urllib.request\n"
        "from lamcore.localnet import open_local\n"
        "def worker(port):\n"
        "    req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/models')\n"
        "    return open_local(req, timeout=1)\n"
    )
    assert find_local_direct_requests(good) == []

    external = ast.parse(
        "import urllib.request\n"
        "def worker():\n"
        "    req = urllib.request.Request('https://api.github.com/repos/x')\n"
        "    return urllib.request.urlopen(req, timeout=15)\n"
    )
    assert find_local_direct_requests(external) == []


def test_no_direct_urlopen_for_local_urls():
    """Локальные адреса обслуживает только lamcore.localnet."""
    found = {}
    for relative_path in NETWORK_SOURCES:
        tree, _source = _parse(relative_path)
        violations = find_local_direct_requests(tree)
        if violations:
            found[relative_path] = violations
    assert found == {}, f"локальные запросы идут напрямую в urllib: {found}"


def test_local_helper_is_used_by_local_call_sites():
    """Хелпер не просто существует, а реально используется."""
    _tree, app_source = _parse("local_ai_manager.py")
    assert app_source.count("open_local(") >= 3
    assert "from lamcore.localnet import open_local" in app_source

    _tree, engines_source = _parse("lamcore/engines.py")
    assert "is_local_url(" in engines_source
    assert "local_opener()" in engines_source


def test_proxies_are_disabled_only_in_localnet():
    """Запрет прокси не должен распространяться на внешние запросы."""
    offenders = []
    for path in sorted(ROOT.glob("*.py")) + sorted((ROOT / "lamcore").glob("*.py")):
        if path.name.startswith("test_") or path.name == "localnet.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node).endswith("ProxyHandler") and node.args:
                literal = node.args[0]
                if isinstance(literal, ast.Dict) and not literal.keys:
                    offenders.append((path.name, node.lineno))
    assert offenders == [], f"прокси отключён вне localnet: {offenders}"


def test_external_urls_keep_proxy_support():
    """open_local не принимает внешние адреса — прокси для них сохраняется."""
    for url in ("https://github.com/ggml-org/llama.cpp",
                "https://huggingface.co/api/models",
                "https://cdn-lfs.huggingface.co/file"):
        with pytest.raises(ValueError):
            open_local(url, timeout=1)


def test_is_local_url_recognises_only_loopback():
    assert is_local_url("http://127.0.0.1:8080/v1/models")
    assert is_local_url("http://localhost:8080/")
    assert is_local_url("http://[::1]:8080/")
    assert not is_local_url("https://huggingface.co/api/models")
    assert not is_local_url("https://api.github.com/repos/ggml-org/llama.cpp")
    assert not is_local_url("")


# --- функциональные проверки на mock-серверах -------------------------------

class _RecordingProxy(http.server.BaseHTTPRequestHandler):
    """Прокси, который запоминает запросы и не ходит в сеть."""

    requests: ClassVar[list] = []

    def _handle(self):
        type(self).requests.append(self.path)
        body = b"proxied"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):  # тишина в выводе тестов
        pass


class _LocalApi(http.server.BaseHTTPRequestHandler):
    """Локальный «llama-server»: отвечает 200 и считает обращения."""

    hits = 0

    def do_GET(self):
        type(self).hits += 1
        body = b'{"data": [{"id": "local-model"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture()
def servers():
    _RecordingProxy.requests = []
    _LocalApi.hits = 0
    target = _serve(_LocalApi)
    proxy = _serve(_RecordingProxy)
    try:
        yield {
            "target_url": f"http://127.0.0.1:{target.server_address[1]}/v1/models",
            "proxy_url": f"http://127.0.0.1:{proxy.server_address[1]}",
        }
    finally:
        for server in (target, proxy):
            server.shutdown()
            server.server_close()


def test_open_local_ignores_http_and_https_proxy(servers, monkeypatch):
    """При настроенных HTTP_PROXY/HTTPS_PROXY локальный запрос идёт напрямую."""
    monkeypatch.setenv("HTTP_PROXY", servers["proxy_url"])
    monkeypatch.setenv("HTTPS_PROXY", servers["proxy_url"])
    monkeypatch.setenv("http_proxy", servers["proxy_url"])
    monkeypatch.setenv("https_proxy", servers["proxy_url"])
    for variable in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(variable, raising=False)

    # Окружение действительно видно urllib — иначе тест был бы пустым.
    assert urllib.request.getproxies().get("http") == servers["proxy_url"]

    request = urllib.request.Request(servers["target_url"], headers={"User-Agent": "test"})
    with open_local(request, timeout=5) as response:
        assert response.status == 200
        assert b"local-model" in response.read()

    assert _LocalApi.hits == 1
    assert _RecordingProxy.requests == [], "локальный запрос ушёл в прокси"


def test_proxy_is_bypassed_only_for_local_traffic(servers, monkeypatch):
    """Тот же прокси перехватывает запрос, если прокси не отключён явно."""
    proxied = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": servers["proxy_url"]}))
    with proxied.open(servers["target_url"], timeout=5) as response:
        assert response.read() == b"proxied"
    assert len(_RecordingProxy.requests) == 1, "mock-прокси не перехватил запрос"

    request = urllib.request.Request(servers["target_url"])
    with open_local(request, timeout=5) as response:
        assert response.status == 200
    assert len(_RecordingProxy.requests) == 1, "localnet не обошёл прокси"
    assert _LocalApi.hits == 1


def test_open_local_accepts_plain_url_and_headers(servers):
    with open_local(servers["target_url"], timeout=5,
                    headers={"User-Agent": "test"}) as response:
        assert response.status == 200


class _LocalChat(http.server.BaseHTTPRequestHandler):
    """Локальный «чат»: отдаёт SSE-поток, как llama-server."""

    hits = 0

    def do_POST(self):
        type(self).hits += 1
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in ("раз", "два"):
            payload = f'data: {{"choices": [{{"delta": {{"content": "{chunk}"}}}}]}}\n\n'
            self.wfile.write(payload.encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


def test_stream_chat_completion_bypasses_proxy(servers, monkeypatch):
    """Чат с локальным сервером не должен уходить в HTTP(S)_PROXY."""
    from lamcore.engines import stream_chat_completion

    monkeypatch.setenv("HTTP_PROXY", servers["proxy_url"])
    monkeypatch.setenv("HTTPS_PROXY", servers["proxy_url"])
    for variable in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(variable, raising=False)

    _LocalChat.hits = 0
    chat = _serve(_LocalChat)
    try:
        url = f"http://127.0.0.1:{chat.server_address[1]}/v1/chat/completions"
        chunks = []
        text = stream_chat_completion(url, {"messages": []}, on_chunk=chunks.append,
                                      timeout=5)
    finally:
        chat.shutdown()
        chat.server_close()

    assert text == "раздва"
    assert chunks == ["раз", "два"]
    assert _LocalChat.hits == 1
    assert _RecordingProxy.requests == [], "чат с локальным сервером ушёл в прокси"


def test_local_opener_is_shared_and_usable(servers):
    """local_opener() переиспользует opener и ходит напрямую на localhost."""
    opener = local_opener()
    assert opener is local_opener()
    with opener.open(servers["target_url"], timeout=5) as response:
        assert response.status == 200
