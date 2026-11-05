"""Phase C: разбор ссылок HF, список любых файлов, коды ошибок, докачка."""

import hashlib
import http.server
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

from lamcore import huggingface as hf


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        data = self.server.payload
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            self.server.requests.append(("range", start))
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.server.requests.append(("full", 0))
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        limit = getattr(self.server, "fail_after", None)
        if limit is not None:
            self.wfile.write(body[:limit])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)


class _Server:
    def __init__(self, payload, fail_after=None):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.payload = payload
        self.httpd.requests = []
        self.httpd.fail_after = fail_after
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/model.gguf"

    @property
    def requests(self):
        return self.httpd.requests


class RepoUrlTest(unittest.TestCase):
    def test_accepts_web_url(self):
        ref = hf.parse_repo_url("https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF")
        self.assertEqual(ref.repo_id, "Qwen/Qwen2.5-3B-Instruct-GGUF")
        self.assertEqual(ref.revision, "main")

    def test_accepts_plain_id_and_slash(self):
        self.assertEqual(hf.parse_repo_url("org/repo").repo_id, "org/repo")
        self.assertEqual(hf.parse_repo_url("  org/repo/  ").repo_id, "org/repo")

    def test_accepts_tree_and_blob(self):
        tree = hf.parse_repo_url("https://huggingface.co/org/repo/tree/v2")
        self.assertEqual((tree.repo_id, tree.revision), ("org/repo", "v2"))
        blob = hf.parse_repo_url("https://huggingface.co/org/repo/blob/main/gguf/model.gguf")
        self.assertEqual(blob.revision, "main")
        self.assertEqual(blob.file_path, "gguf/model.gguf")
        self.assertEqual(blob.resolve_url(),
                         "https://huggingface.co/org/repo/resolve/main/gguf/model.gguf")

    def test_accepts_query_string(self):
        ref = hf.parse_repo_url("https://huggingface.co/org/repo?x=1")
        self.assertEqual(ref.repo_id, "org/repo")

    def test_rejects_foreign_host_and_broken_input(self):
        for bad in ("https://evil.example/org/repo", "https://huggingface.co/onlyowner",
                    "ftp://huggingface.co/org/repo", "org/repo with space", "",
                    "org/../repo", "/org/repo", "org/repo/tree", "org/repo/blob/main",
                    "org/repo/something/else"):
            with self.assertRaises(hf.HFError, msg=bad):
                hf.parse_repo_url(bad)

    def test_owner_rejects_traversal(self):
        with self.assertRaises(hf.HFError):
            hf.parse_repo_url("..%2F/repo")


class FileListTest(unittest.TestCase):
    PAYLOAD = {"siblings": [
        {"rfilename": "README.md", "size": 10},
        {"rfilename": "config.json", "size": 20},
        {"rfilename": "model.safetensors", "size": 100,
         "lfs": {"size": 100, "oid": "a" * 64}},
        {"rfilename": "model.onnx", "size": 50},
        {"rfilename": "weights.onnx_data", "size": 5000},
        {"rfilename": "tokenizer.json", "size": 30},
        {"rfilename": "q4/model-Q4_K_M.gguf", "size": 1000},
        {"rfilename": "mmproj-vision.gguf", "size": 400},
        {"bad": "entry"},
    ]}

    def test_lists_every_extension_not_only_gguf(self):
        entries = hf.build_file_list(self.PAYLOAD)
        names = [entry.name for entry in entries]
        self.assertIn("model.safetensors", names)
        self.assertIn("model.onnx", names)
        self.assertIn("config.json", names)
        self.assertIn("q4/model-Q4_K_M.gguf", names)
        self.assertEqual(len(names), 8)

    def test_categories(self):
        by_name = {entry.name: entry for entry in hf.build_file_list(self.PAYLOAD)}
        self.assertEqual(by_name["q4/model-Q4_K_M.gguf"].category, "weights")
        self.assertEqual(by_name["model.safetensors"].category, "weights")
        self.assertEqual(by_name["mmproj-vision.gguf"].category, "mmproj")
        self.assertEqual(by_name["tokenizer.json"].category, "supporting")
        self.assertEqual(by_name["model.safetensors"].sha256, "a" * 64)

    def test_nested_names_and_filter(self):
        entries = hf.build_file_list(self.PAYLOAD)
        nested = [entry for entry in entries if "/" in entry.name]
        self.assertEqual(len(nested), 1)
        self.assertEqual([e.name for e in hf.filter_files(entries, "token")], ["tokenizer.json"])
        self.assertEqual([e.name for e in hf.filter_files(entries, extension="gguf")],
                         ["mmproj-vision.gguf", "q4/model-Q4_K_M.gguf"])
        self.assertEqual([e.name for e in hf.filter_files(entries, category="weights")],
                         ["model.onnx", "model.safetensors", "q4/model-Q4_K_M.gguf"])

    def test_empty_payload(self):
        self.assertEqual(hf.build_file_list({}), [])
        self.assertEqual(hf.build_file_list(None), [])


class ErrorCodeTest(unittest.TestCase):
    def _http(self, code, headers=None):
        return urllib.error.HTTPError("https://huggingface.co/x", code, "err",
                                      headers or {}, None)

    def test_each_status_has_its_own_message(self):
        messages = {}
        for code in (401, 403, 404, 429, 500):
            error = hf.error_from_http(self._http(code))
            messages[code] = (error.kind, error.message())
        self.assertEqual(messages[404][0], "not_found")
        self.assertEqual(messages[401][0], "auth")
        self.assertEqual(messages[403][0], "auth")
        self.assertEqual(messages[429][0], "rate_limit")
        self.assertEqual(messages[500][0], "http")
        texts = {text for _kind, text in messages.values()}
        self.assertEqual(len(texts), len(messages))

    def test_retry_after_is_reported(self):
        error = hf.error_from_http(self._http(429, {"Retry-After": "17"}))
        self.assertEqual(error.retry_after, 17)
        self.assertIn("17", error.message())

    def test_all_declared_kinds_have_distinct_templates(self):
        templates = {kind: hf.ERROR_MESSAGES[kind] for kind in hf.ERROR_MESSAGES}
        self.assertEqual(len(set(templates.values())), len(templates))
        for kind, template in hf.ERROR_MESSAGES.items():
            rendered = hf.HFError(kind).message()
            self.assertTrue(rendered.strip(), kind)


class LocalPathTest(unittest.TestCase):
    def test_preserves_nested_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = hf.local_path_for(tmp, "org/repo", "gguf/sub/model.gguf")
            self.assertTrue(str(path).endswith(os.path.join("org", "repo", "gguf", "sub", "model.gguf")))

    def test_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("../model.gguf", "/etc/passwd", "a/../../b.gguf", "C:/x.gguf"):
                with self.assertRaises(hf.HFError, msg=bad):
                    hf.local_path_for(tmp, "org/repo", bad)

    def test_plan_avoids_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = hf.local_path_for(tmp, "org/repo", "model.gguf")
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            plan = hf.plan_download(tmp, hf.parse_repo_url("org/repo"), ["model.gguf"])
            self.assertNotEqual(plan[0][1], target)
            self.assertEqual(target.read_bytes(), b"old")


class DownloadTest(unittest.TestCase):
    PAYLOAD = bytes(range(256)) * 400   # 102 400 байт

    def test_fresh_download_checks_size_and_sha(self):
        digest = hashlib.sha256(self.PAYLOAD).hexdigest()
        with _Server(self.PAYLOAD) as server, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model.gguf"
            done = hf.download_file(server.url, target, expected_size=len(self.PAYLOAD),
                                    expected_sha256=digest)
            self.assertEqual(Path(done).read_bytes(), self.PAYLOAD)
            self.assertFalse(Path(str(target) + ".part").exists())

    def test_wrong_sha_is_rejected_and_file_not_created(self):
        with _Server(self.PAYLOAD) as server, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model.gguf"
            with self.assertRaises(hf.HFError) as ctx:
                hf.download_file(server.url, target, expected_size=len(self.PAYLOAD),
                                 expected_sha256="0" * 64)
            self.assertEqual(ctx.exception.kind, "integrity")
            self.assertFalse(target.exists())

    def test_interrupted_download_resumes_with_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            part_dir = Path(tmp) / "tmp"
            part_dir.mkdir()
            target = Path(tmp) / "model.gguf"
            with _Server(self.PAYLOAD, fail_after=1000) as broken:
                with self.assertRaises(hf.HFError) as ctx:
                    hf.download_file(broken.url, target, expected_size=len(self.PAYLOAD),
                                     part_dir=part_dir)
                self.assertEqual(ctx.exception.kind, "interrupted")
            part = part_dir / "model.gguf.part"
            self.assertTrue(part.exists())
            self.assertEqual(part.stat().st_size, 1000)
            self.assertFalse(target.exists())

            with _Server(self.PAYLOAD) as server:
                hf.download_file(server.url, target, expected_size=len(self.PAYLOAD),
                                 part_dir=part_dir)
                self.assertIn(("range", 1000), server.requests)
            self.assertEqual(target.read_bytes(), self.PAYLOAD)

    def test_cancel_keeps_part_and_raises(self):
        with _Server(self.PAYLOAD) as server, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model.gguf"
            event = threading.Event()
            event.set()
            with self.assertRaises(hf.DownloadCancelled):
                hf.download_file(server.url, target, cancel_event=event)
            self.assertFalse(target.exists())

    def test_http_404_becomes_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(hf.HFError) as ctx:
                hf.download_file("http://127.0.0.1:1/x", Path(tmp) / "a.bin")
            self.assertIn(ctx.exception.kind, ("network",))

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"abc")
            self.assertEqual(hf.sha256_file(path),
                             hashlib.sha256(b"abc").hexdigest())


if __name__ == "__main__":
    unittest.main()
