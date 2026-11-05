"""Регрессия: `DownloadCancelled` один на весь проект.

`local_ai_manager.py` определял собственный `class DownloadCancelled(Exception)`,
который перекрывал импорт из `lamcore.huggingface`. Из-за этого отмена загрузки
с Hugging Face (там исключение — наследник `HFError`) не попадала в
`except DownloadCancelled` и показывалась пользователю как ошибка загрузки.
Тест не требует Tkinter: AST-анализ плюс локальный mock-сервер.
"""

import ast
import http.server
import threading
from pathlib import Path

import pytest

import lamcore.huggingface as hf

ROOT = Path(__file__).resolve().parent
APP = ROOT / "local_ai_manager.py"


def _app_tree():
    return ast.parse(APP.read_text(encoding="utf-8"), filename="local_ai_manager.py")


def classes_named(tree, name):
    """Строки, где объявлен класс с таким именем (на любой глубине)."""
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == name]


def imported_names(tree):
    """Имена, импортированные через `from ... import ...`."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def handler_order(tree, marker_call):
    """Порядок обработчиков в функции, где встречается вызов marker_call."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [ast.unparse(call.func) for call in ast.walk(node)
                 if isinstance(call, ast.Call)]
        if not any(name.endswith(marker_call) for name in calls):
            continue
        order = []
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Try):
                continue
            for handler in inner.handlers:
                caught = handler.type
                names = [caught] if caught is not None else []
                if isinstance(caught, ast.Tuple):
                    names = list(caught.elts)
                for type_node in names:
                    order.append(ast.unparse(type_node))
        if order:
            return order
    return []


def test_detector_finds_local_class_definition():
    """Самопроверка: анализатор видит конфликтующее объявление класса."""
    tree = ast.parse(
        "from lamcore.huggingface import DownloadCancelled\n"
        "class DownloadCancelled(Exception):\n"
        "    pass\n"
    )
    assert classes_named(tree, "DownloadCancelled") == [2]
    assert "DownloadCancelled" in imported_names(tree)


def test_app_does_not_redefine_download_cancelled():
    tree = _app_tree()
    assert classes_named(tree, "DownloadCancelled") == [], (
        "в local_ai_manager.py снова объявлен собственный DownloadCancelled — "
        "он перекроет lamcore.huggingface.DownloadCancelled"
    )


def test_app_imports_download_cancelled_from_huggingface():
    tree = _app_tree()
    sources = {
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        and node.module.endswith("huggingface")
    }
    assert any("DownloadCancelled" in source for source in sources), sources


def test_hf_worker_reports_cancellation_before_generic_hf_error():
    order = handler_order(_app_tree(), "download_file")
    assert "DownloadCancelled" in order, order
    assert "HFError" in order, order
    assert order.index("DownloadCancelled") < order.index("HFError"), (
        "отмена должна обрабатываться раньше HFError, иначе она считается ошибкой"
    )


def test_download_cancelled_is_hf_error_with_cancelled_kind():
    assert issubclass(hf.DownloadCancelled, hf.HFError)
    error = hf.DownloadCancelled()
    assert error.kind == "cancelled"
    assert "остановлена" in str(error)


class _FileServer(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        body = b"0" * 1024
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_cancelled_download_raises_the_shared_exception(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FileServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cancel = threading.Event()
    cancel.set()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/model.gguf"
        destination = tmp_path / "model.gguf"
        with pytest.raises(hf.DownloadCancelled) as caught:
            hf.download_file(url, destination, cancel_event=cancel, timeout=5)
    finally:
        server.shutdown()
        server.server_close()

    assert caught.value.kind == "cancelled"
    assert not destination.exists(), "отменённая загрузка не должна создавать файл"
