import os
"""Регрессии v47.3: обрыв загрузки, битый конфиг, дубли имён, alias, URL HF."""
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

import local_ai_manager as lam


class _TruncatedHandler(http.server.BaseHTTPRequestHandler):
    """Обещает 1000 байт, отдаёт 10 и закрывает соединение."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        self.wfile.write(b"x" * 10)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args):
        pass


class _WrongRangeHandler(http.server.BaseHTTPRequestHandler):
    """Игнорирует запрошенное смещение и отдаёт начало файла как 206."""
    def do_GET(self):
        self.send_response(206)
        self.send_header("Content-Length", "10")
        self.send_header("Content-Range", "bytes 0-9/20")
        self.end_headers()
        self.wfile.write(b"abcdefghij")

    def log_message(self, *args):
        pass


class _MissingRangeHandler(http.server.BaseHTTPRequestHandler):
    """Отдаёт 206 без обязательного Content-Range."""
    def do_GET(self):
        self.send_response(206)
        self.send_header("Content-Length", "10")
        self.end_headers()
        self.wfile.write(b"klmnopqrst")

    def log_message(self, *args):
        pass


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _DownloadDialog:
    """Двойник окна HF для проверки независимой отмены без Tk."""
    cancel_download = lam.HuggingFaceDialog.cancel_download

    def __init__(self, engine_cancel_event):
        self.app = type("App", (), {"download_cancel_event": engine_cancel_event})()
        self.download_cancel_event = threading.Event()
        self.status_var = _Var()


class _Library:
    """Двойник LocalAIManager: выбор модели без Tk."""

    def __init__(self, models, selected=None):
        self.models = models
        self.model_var = _Var()
        self.model_combo = {"values": lam.LocalAIManager.model_display_names(self)}
        self.model_var.set(selected if selected is not None else self.model_combo["values"][-1])

    model_display_name = staticmethod(lam.LocalAIManager.model_display_name)
    model_display_names = lam.LocalAIManager.model_display_names
    selected_path = lam.LocalAIManager.selected_path
    model_alias = lam.LocalAIManager.model_alias


class DownloadIntegrityTest(unittest.TestCase):
    def test_truncated_download_raises_and_keeps_part(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TruncatedHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "model.gguf"
                url = f"http://127.0.0.1:{server.server_port}/model.gguf"
                with self.assertRaises(IOError):
                    lam.stream_download_resumable(url, target, timeout=10)
                self.assertFalse(target.exists())
                self.assertTrue(Path(str(target) + ".part").exists())
        finally:
            server.shutdown()
            server.server_close()

    def test_hf_cancel_does_not_cancel_engine_download(self):
        engine_cancel_event = threading.Event()
        dialog = _DownloadDialog(engine_cancel_event)
        dialog.cancel_download()
        self.assertTrue(dialog.download_cancel_event.is_set())
        self.assertFalse(engine_cancel_event.is_set())

    def test_wrong_content_range_never_creates_a_completed_file(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WrongRangeHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "model.gguf"
                part = Path(str(target) + ".part")
                part.write_bytes(b"abcdefghij")
                url = f"http://127.0.0.1:{server.server_port}/model.gguf"
                with self.assertRaises(IOError):
                    lam.stream_download_resumable(url, target, timeout=10)
                self.assertFalse(target.exists())
                self.assertEqual(part.read_bytes(), b"abcdefghij")
        finally:
            server.shutdown()
            server.server_close()

    def test_missing_content_range_never_creates_a_completed_file(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MissingRangeHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "model.gguf"
                part = Path(str(target) + ".part")
                part.write_bytes(b"abcdefghij")
                url = f"http://127.0.0.1:{server.server_port}/model.gguf"
                with self.assertRaises(IOError):
                    lam.stream_download_resumable(url, target, timeout=10)
                self.assertFalse(target.exists())
                self.assertEqual(part.read_bytes(), b"abcdefghij")
        finally:
            server.shutdown()
            server.server_close()


class ConfigRecoveryTest(unittest.TestCase):
    def _load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            old_dir, old_file = lam.APP_DIR, lam.CONFIG_FILE
            lam.APP_DIR, lam.CONFIG_FILE = Path(tmp), Path(tmp) / "config.json"
            lam.CONFIG_FILE.write_text(text, encoding="utf-8")
            try:
                config = lam.LocalAIManager.load_config(object())
                broken = list(Path(tmp).glob("config.json.broken-*"))
            finally:
                lam.APP_DIR, lam.CONFIG_FILE = old_dir, old_file
        return config, broken

    def test_broken_json_is_kept_as_backup(self):
        config, broken = self._load("{не json")
        self.assertEqual(config["port"], 8080)
        self.assertEqual(config["models"], [])
        self.assertEqual(len(broken), 1)

    def test_wrong_types_fall_back_to_defaults(self):
        config, broken = self._load(json.dumps(
            {"models": "not-a-list", "port": "8080", "server": 5, "skills": ["ok"]}))
        self.assertEqual(broken, [])
        self.assertEqual(config["models"], [])
        self.assertEqual(config["port"], 8080)
        self.assertEqual(config["server"], "")
        self.assertEqual(config["skills"], ["ok"])


class ApiCheckTest(unittest.TestCase):
    """Ошибка проверки API должна доходить до пользователя своим текстом."""

    def test_error_report_keeps_exception_text(self):
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # порт свободен: подключение будет отвергнуто

        events = []

        class _Stub:
            def _ui_event(self, kind, payload):
                events.append((kind, payload))

            def _show_api_result(self, url, text, error):
                events.append(("result", url, text, error))

        stub = _Stub()
        lam.LocalAIManager._check_api_worker(stub, f"http://127.0.0.1:{port}/v1/models")

        self.assertEqual(events[0][0], "call")
        events[0][1]()  # до правки здесь был NameError про свободную переменную exc
        self.assertEqual(events[1][0], "result")
        error = events[1][3]
        self.assertTrue(error, "текст ошибки не должен теряться")
        self.assertNotIn("free variable", error)
        self.assertNotIn("NameError", error)


class ReloadOpenCodeMessageTest(unittest.TestCase):
    """При выключенном OpenCode журнал не должен обещать «модели обновились»."""

    def test_closed_opencode_message_is_honest(self):
        logs = []

        class _Stub:
            def opencode_cli_path(self):
                return r"C:\fake\opencode-cli.exe"

            def opencode_is_running(self):
                return False

            def write_log(self, text):
                logs.append(text)

            def _reload_opencode_worker(self, exe):
                raise AssertionError("фоновый перезапуск не должен запускаться")

        lam.LocalAIManager.reload_opencode(_Stub())

        self.assertTrue(logs, "журнал должен получить хотя бы одну строку")
        self.assertIn("следующем старте", logs[-1])
        self.assertNotIn("обновились", logs[-1])


class AutoFindServerErrorTest(unittest.TestCase):
    """Сбой обхода папки при автопоиске должен показывать причину, а не NameError."""

    def test_walk_error_is_reported_with_text(self):
        from pathlib import Path

        events = []

        class _Stub:
            def _ui_event(self, kind, payload):
                events.append((kind, payload))

        stub = _Stub()
        shown = []
        original_walk = lam.os.walk
        original_error = lam.messagebox.showerror
        lam.os.walk = lambda *_a, **_k: (_ for _ in ()).throw(OSError("нет доступа"))
        lam.messagebox.showerror = lambda title, message, **kwargs: shown.append((title, message))
        try:
            lam.LocalAIManager._find_server_worker(stub, Path("."))
            events[0][1]()
        finally:
            lam.os.walk = original_walk
            lam.messagebox.showerror = original_error

        self.assertEqual(shown[0][0], "Автопоиск")
        self.assertIn("нет доступа", shown[0][1])


class ModelSelectionTest(unittest.TestCase):
    def test_duplicate_names_are_distinguished(self):
        library = _Library([r"C:\one\model.gguf", r"C:\two\model.gguf"])
        names = library.model_combo["values"]
        self.assertEqual(len(set(names)), 2)
        library.model_var.set(names[1])
        self.assertEqual(library.selected_path(), r"C:\two\model.gguf")

    def test_same_file_name_in_two_folders_gets_two_aliases(self):
        models = [r"C:\one\model.gguf", r"C:\two\model.gguf"]
        first_lib = _Library(models)
        names = first_lib.model_combo["values"]
        first_lib.model_var.set(names[0])
        first = first_lib.model_alias()
        second_lib = _Library(models)
        second_lib.model_var.set(names[1])
        second = second_lib.model_alias()
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(second), 80)

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_cyrillic_alias_is_unique_and_ascii(self):
        cyrillic = _Library([r"C:\m\Модель-Q4.gguf"]).model_alias()
        latin = _Library([r"C:\m\Q4.gguf"]).model_alias()
        self.assertRegex(cyrillic, r"^[A-Za-z0-9._-]{1,80}$")
        self.assertNotEqual(cyrillic, latin)
        self.assertTrue(cyrillic.startswith("Q4-"))

    def test_long_ascii_aliases_are_unique(self):
        first = _Library([r"C:\\m\\" + "a" * 80 + "1.gguf"]).model_alias()
        second = _Library([r"C:\\m\\" + "a" * 80 + "2.gguf"]).model_alias()
        self.assertLessEqual(len(first), 80)
        self.assertNotEqual(first, second)


class ResolveUrlTest(unittest.TestCase):
    def test_names_with_spaces_are_quoted(self):
        url = lam.hf_resolve_url("org/repo", "папка/файл имя.gguf")
        self.assertNotIn(" ", url)
        self.assertIn("/%D0%BF%D0%B0%D0%BF%D0%BA%D0%B0/", url)
        self.assertIn("%20", url)
        self.assertTrue(url.endswith(".gguf"))


class AssetPickTest(unittest.TestCase):
    """Имена взяты из реального релиза llama.cpp (b12098)."""
    ASSETS = [{"name": n} for n in (
        "cudart-llama-bin-win-cuda-12.4-x64.zip",
        "cudart-llama-bin-win-cuda-13.4-x64.zip",
        "llama-b12098-bin-win-cpu-arm64.zip",
        "llama-b12098-bin-win-cpu-x64.zip",
        "llama-b12098-bin-win-cuda-12.4-x64.zip",
        "llama-b12098-bin-win-cuda-13.4-x64.zip",
        "llama-b12098-bin-win-vulkan-arm64.zip",
        "llama-b12098-bin-win-vulkan-x64.zip",
        "llama-b12098-bin-win-rocm-10.0-x64.zip",
    )]

    def test_nvidia_gets_cuda_12_4_build_not_cudart(self):
        self.assertEqual(lam.pick_llama_cpp_asset(self.ASSETS, "NVIDIA GeForce RTX 4060")["name"],
                         "llama-b12098-bin-win-cuda-12.4-x64.zip")

    def test_amd_gets_vulkan_build(self):
        self.assertEqual(lam.pick_llama_cpp_asset(self.ASSETS, "AMD Radeon RX 6700")["name"],
                         "llama-b12098-bin-win-vulkan-x64.zip")

    def test_no_gpu_gets_cpu_build(self):
        self.assertEqual(lam.pick_llama_cpp_asset(self.ASSETS, "")["name"],
                         "llama-b12098-bin-win-cpu-x64.zip")

    def test_release_without_windows_build_returns_none(self):
        self.assertIsNone(lam.pick_llama_cpp_asset([{"name": "nightly-tag.txt"}]))


if __name__ == "__main__":
    unittest.main()
