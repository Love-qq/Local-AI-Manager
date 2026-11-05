"""Phase B: каталоги, safe_join, классификация моделей, отсутствие перезаписи."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from lamcore.paths import AppPaths, UnsafePathError, safe_join
from lamcore.storage import (ModelCatalog, ModelKind, classify_directory, classify_file,
                             discover_models, ensure_new_file, merge_user_models,
                             unique_path)


class SafeJoinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "model"
        self.base.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_parent_traversal(self):
        for bad in ("../x", "a/../../x", "a\\..\\..\\x", "./../x"):
            with self.assertRaises(UnsafePathError, msg=bad):
                safe_join(self.base, bad)

    def test_rejects_absolute_paths(self):
        for bad in ("/etc/x", "C:\\x", "C:x", "\\\\server\\share\\x", "//server/x"):
            with self.assertRaises(UnsafePathError, msg=bad):
                safe_join(self.base, bad)

    def test_rejects_empty_and_streams(self):
        for bad in ("", "   ", None, "a:b", "a\x00b"):
            with self.assertRaises(UnsafePathError, msg=repr(bad)):
                safe_join(self.base, bad)

    def test_accepts_nested_relative(self):
        target = safe_join(self.base, "owner/repo/sub/model.gguf")
        self.assertTrue(str(target).endswith(os.path.join("owner", "repo", "sub", "model.gguf")))
        self.assertEqual(target, target.resolve())

    def test_rejects_symlink_escape(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        link = self.base / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("создание симлинков недоступно без прав")
        with self.assertRaises(UnsafePathError):
            safe_join(self.base, "link/secret.txt")

    def test_paths_follow_root(self):
        paths = AppPaths(Path(self.tmp.name) / "root")
        self.assertEqual(paths.models.parent, paths.root)
        self.assertEqual(paths.engines, paths.root / "engines")
        self.assertEqual(paths.logs.name, "logs")
        self.assertEqual(paths.tmp.name, "tmp")
        paths.ensure()
        for folder in paths.all_dirs():
            self.assertTrue(folder.is_dir())
        target = paths.model_target("org/repo", "sub/model.gguf")
        self.assertEqual(target, (paths.models / "org" / "repo" / "sub" / "model.gguf").resolve())


class ClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "model"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, relative, payload=b"x" * 16):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_supporting_file_is_never_a_model(self):
        self.assertEqual(classify_file("tokenizer.json"), ModelKind.SUPPORTING_FILE)
        self.assertEqual(classify_file("README.md"), ModelKind.SUPPORTING_FILE)
        self.assertEqual(classify_file("mmproj-model.gguf"), ModelKind.SUPPORTING_FILE)
        self.assertEqual(classify_file("model.gguf.part"), ModelKind.SUPPORTING_FILE)

    def test_complete_package(self):
        self._write("org/repo/config.json", b"{}")
        self._write("org/repo/model.safetensors")
        self._write("org/repo/tokenizer.json", b"{}")
        self.assertEqual(classify_directory(self.root / "org" / "repo"), ModelKind.COMPLETE_PACKAGE)
        records = discover_models(self.root)
        package = [r for r in records if r.kind == ModelKind.COMPLETE_PACKAGE]
        self.assertEqual(len(package), 1)
        self.assertTrue(package[0].is_runnable)
        self.assertEqual(package[0].repo, "org/repo")

    def test_multi_file_repo_keeps_structure(self):
        self._write("org/repo/gguf/part-00001-of-00002.gguf")
        self._write("org/repo/gguf/part-00002-of-00002.gguf")
        records = discover_models(self.root)
        multi = [r for r in records if r.kind == ModelKind.MULTI_FILE_REPO]
        self.assertEqual(len(multi), 1)
        self.assertTrue(multi[0].is_runnable)
        self.assertEqual(sorted(multi[0].files), ["part-00001-of-00002.gguf", "part-00002-of-00002.gguf"])

    def test_single_shard_is_not_a_model(self):
        self._write("org/repo/model-00002-of-00003.gguf")
        records = discover_models(self.root)
        shard = [r for r in records if r.note == "incomplete_shard_set"]
        self.assertEqual(len(shard), 1)
        self.assertFalse(shard[0].is_runnable)

    def test_tokenizer_json_alone_gives_no_models(self):
        self._write("org/repo/tokenizer.json", b"{}")
        self.assertEqual([r for r in discover_models(self.root) if r.is_runnable], [])


class NoOverwriteTest(unittest.TestCase):
    def test_unique_path_never_reuses_a_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model.gguf"
            target.write_bytes(b"old")
            fresh = unique_path(target)
            self.assertNotEqual(fresh, target)
            self.assertFalse(fresh.exists())
            self.assertEqual(target.read_bytes(), b"old")

    def test_ensure_new_file_refuses_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model.gguf"
            target.write_bytes(b"old")
            with self.assertRaises(FileExistsError):
                ensure_new_file(target)

    def test_download_refuses_to_overwrite(self):
        from lamcore import huggingface
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "model.gguf"
            target.write_bytes(b"old")
            with self.assertRaises(FileExistsError):
                huggingface.download_file("http://127.0.0.1:1/never", target)
            self.assertEqual(target.read_bytes(), b"old")


class DiscoveryAcrossRestartsTest(unittest.TestCase):
    def test_second_instance_finds_models_of_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp) / "root").ensure()
            model = paths.models / "org" / "repo" / "model.gguf"
            model.parent.mkdir(parents=True)
            model.write_bytes(b"GGUF" + b"\0" * 60)

            # Первый запуск: каталог создан, модель записана.
            catalog = ModelCatalog(paths.root / ModelCatalog.FILENAME)
            catalog.sync(paths.models)
            self.assertTrue(catalog.get(str(model)))

            # Второй запуск: свежий объект читает каталог с диска и находит файл.
            fresh = ModelCatalog(paths.root / ModelCatalog.FILENAME)
            self.assertTrue(fresh.get(str(model)))
            self.assertEqual(len(fresh.runnable()), 1)

    def test_catalog_sync_does_not_lose_user_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root / "model"
            models.mkdir()
            outside = root / "elsewhere" / "Qwen.gguf"
            outside.parent.mkdir()
            outside.write_bytes(b"GGUF")
            discovered = discover_models(models)
            merged, added = merge_user_models([str(outside)], discovered, models)
            self.assertIn(str(outside), merged)
            self.assertEqual(added, [])
            self.assertTrue(outside.exists(), "миграция не должна перемещать файлы")

    def test_catalog_persists_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / ModelCatalog.FILENAME
            catalog = ModelCatalog(catalog_path)
            from lamcore.storage import ModelRecord
            catalog.add(ModelRecord(path=str(Path(tmp) / "a.gguf"), kind=ModelKind.SINGLE_FILE,
                                    size_bytes=10, source="user"))
            payload = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(len(payload["models"]), 1)
            self.assertEqual(ModelCatalog(catalog_path).records[0].size_bytes, 10)


if __name__ == "__main__":
    unittest.main()
