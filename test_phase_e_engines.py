"""Phase E: адаптеры, совместимость, запрет запуска и подтверждение установки."""

import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from lamcore.engines import (CompatibilityStatus, ConfirmationRequired, EngineManager,
                             IncompatibleModelError, LaunchError, LaunchParams,
                             LlamaCppAdapter, plan_llama_launch, stream_chat_completion)
from lamcore.modelinfo import ModelInfo
from lamcore.paths import AppPaths


def gguf_info(**overrides):
    data = dict(path="model.gguf", format="gguf", architecture="llama", quantization="Q4_K_M",
                max_context=8192, size_bytes=2 * 1024 ** 3, completeness="complete",
                layers=32, heads=32, kv_heads=8, head_dim=128)
    data.update(overrides)
    return ModelInfo(**data)


class CompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.adapter = LlamaCppAdapter("C:/no/llama-server.exe")

    def test_gguf_is_compatible(self):
        verdict = self.adapter.can_run(gguf_info())
        self.assertEqual(verdict.status, CompatibilityStatus.COMPATIBLE)
        self.assertTrue(verdict)

    def test_safetensors_is_unsupported_not_ok(self):
        verdict = self.adapter.can_run(ModelInfo(path="m.safetensors", format="safetensors",
                                                 completeness="complete"))
        self.assertEqual(verdict.status, CompatibilityStatus.UNSUPPORTED)
        self.assertTrue(verdict.is_blocking)
        self.assertNotEqual(verdict.status, CompatibilityStatus.COMPATIBLE)

    def test_mmproj_is_unsupported(self):
        verdict = self.adapter.can_run(ModelInfo(path="mmproj-x.gguf", format="gguf",
                                                 architecture="clip-projector",
                                                 is_supporting=True))
        self.assertEqual(verdict.status, CompatibilityStatus.UNSUPPORTED)
        self.assertIn("mmproj", verdict.detail)

    def test_incomplete_gguf_is_incompatible(self):
        verdict = self.adapter.can_run(gguf_info(completeness="incomplete",
                                                 missing_files=("part-00002.gguf",)))
        self.assertEqual(verdict.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertIn("part-00002.gguf", verdict.detail)

    def test_unreadable_header_is_unverified_and_not_blocking(self):
        verdict = self.adapter.can_run(gguf_info(completeness="unknown"))
        self.assertEqual(verdict.status, CompatibilityStatus.UNVERIFIED)
        self.assertFalse(verdict.is_blocking)

    def test_unknown_format_is_unsupported(self):
        self.assertEqual(self.adapter.can_run(ModelInfo(path="x.bin")).status,
                         CompatibilityStatus.UNSUPPORTED)


class LaunchBlockingTest(unittest.TestCase):
    def test_incompatible_model_never_reaches_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Path(tmp) / "llama-server.exe"
            server.write_bytes(b"MZ")
            model = Path(tmp) / "model.safetensors"
            model.write_bytes(b"w")
            adapter = LlamaCppAdapter(str(server))
            spawned = []
            params = LaunchParams(model_path=str(model))
            info = ModelInfo(path=str(model), format="safetensors", completeness="complete")
            with self.assertRaises(IncompatibleModelError):
                adapter.launch(params, info, spawn=lambda plan: spawned.append(plan))
            self.assertEqual(spawned, [])

    def test_manager_require_launchable_raises_for_onnx(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = EngineManager(AppPaths(tmp))
            with self.assertRaises(IncompatibleModelError):
                manager.require_launchable(ModelInfo(path="x.onnx", format="onnx"),
                                           engine_id="llama.cpp", server_path="")

    def test_plan_builds_expected_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Path(tmp) / "llama-server.exe"
            server.write_bytes(b"MZ")
            model = Path(tmp) / "model.gguf"
            model.write_bytes(b"GGUF")
            plan = plan_llama_launch(str(server), LaunchParams(
                model_path=str(model), ctx=8192, gpu_layers=20, threads=6, port=9090,
                alias="qwen"))
            argv = list(plan.argv)
            self.assertEqual(argv[0], str(server))
            self.assertIn("-m", argv)
            self.assertEqual(argv[argv.index("-c") + 1], "8192")
            self.assertEqual(argv[argv.index("-ngl") + 1], "20")
            self.assertEqual(argv[argv.index("--port") + 1], "9090")
            self.assertEqual(argv[argv.index("-a") + 1], "qwen")

    def test_plan_rejects_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Path(tmp) / "llama-server.exe"
            server.write_bytes(b"MZ")
            model = Path(tmp) / "model.gguf"
            model.write_bytes(b"G")
            for kwargs in ({"ctx": 1}, {"ctx": 4096, "threads": 0}, {"ctx": 4096, "port": 70000}):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(LaunchError):
                        LaunchEngine(**kwargs)
            with self.assertRaises(LaunchError):
                LaunchEngine(ctx=4096, server_missing=True)


def LaunchEngine(*, ctx=4096, threads=4, port=8080, server_missing=False):
    """Проверка валидации параметров без реального запуска."""
    import tempfile
    from pathlib import Path as _Path
    folder = tempfile.mkdtemp()
    server = _Path(folder) / ("missing.exe" if server_missing else "llama-server.exe")
    if not server_missing:
        server.write_bytes(b"MZ")
    model = _Path(folder) / "model.gguf"
    model.write_bytes(b"G")
    return plan_llama_launch(str(server), LaunchParams(
        model_path=str(model), ctx=ctx, threads=threads, port=port))


class InstallConfirmationTest(unittest.TestCase):
    def test_install_without_confirmation_does_nothing(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            manager = EngineManager(AppPaths(tmp))
            with self.assertRaises(ConfirmationRequired):
                manager.install("llama.cpp", downloader=lambda: calls.append("download"))
            self.assertEqual(calls, [])

    def test_install_with_confirmation_runs_downloader(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            manager = EngineManager(AppPaths(tmp))
            detail, status = manager.install("llama.cpp", confirm=True,
                                             downloader=lambda: calls.append("download") or "ok",
                                             verify=False)
            self.assertEqual(calls, ["download"])
            self.assertEqual(detail, "ok")
            self.assertIsNone(status)

    def test_remove_requires_confirmation_and_spares_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp) / "root").ensure()
            engine = paths.engines / "llama.cpp"
            engine.mkdir()
            (engine / "llama-server.exe").write_bytes(b"MZ")
            model = paths.models / "org" / "repo" / "model.gguf"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"GGUF")
            manager = EngineManager(paths)
            with self.assertRaises(ConfirmationRequired):
                manager.remove("llama.cpp")
            self.assertTrue(engine.exists())
            self.assertTrue(manager.remove("llama.cpp", confirm=True))
            self.assertFalse(engine.exists())
            self.assertTrue(model.exists(), "удаление движка не должно трогать модели")

    def test_remove_refuses_unknown_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp) / "root").ensure()
            manager = EngineManager(paths)
            with self.assertRaises(LaunchError):
                manager.remove("Ollama", confirm=True)


class DetectTest(unittest.TestCase):
    def test_detects_engine_inside_engines_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp) / "root").ensure()
            server = paths.engines / "llama.cpp" / "build" / "bin" / "llama-server.exe"
            server.parent.mkdir(parents=True)
            server.write_bytes(b"MZ")
            manager = EngineManager(paths)
            self.assertEqual(manager.llama_server(), str(server))
            status = manager.detect_installed()["llama.cpp"]
            self.assertTrue(status.installed)

    def test_unsupported_engines_are_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = EngineManager(AppPaths(tmp))
            for engine in ("ONNX Runtime", "Transformers + PyTorch"):
                verdict = manager.adapter(engine).can_run(ModelInfo(path="x", format="onnx"))
                self.assertEqual(verdict.status, CompatibilityStatus.UNSUPPORTED)
                self.assertTrue(verdict.detail)


class StreamTest(unittest.TestCase):
    PAYLOAD = (b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
               b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
               b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
               b'data: [DONE]\n\n')

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(StreamTest.PAYLOAD)

    def test_identical_chunks_stay_two_chunks(self):
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions"
            seen = []
            text = stream_chat_completion(url, {"messages": []}, on_chunk=seen.append)
            self.assertEqual(text, "aab")
            self.assertEqual(seen, ["a", "a", "b"])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
