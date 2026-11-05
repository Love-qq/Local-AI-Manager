"""Phase D: метаданные GGUF/ONNX/JSON и честная проверка полноты."""

import json
import struct
import tempfile
import unittest
from pathlib import Path

from lamcore.modelinfo import FILE_TYPES, inspect_path, read_gguf_metadata


def _kv_string(key, value):
    key_bytes = key.encode("utf-8")
    value_bytes = value.encode("utf-8")
    return (struct.pack("<Q", len(key_bytes)) + key_bytes + struct.pack("<I", 8)
            + struct.pack("<Q", len(value_bytes)) + value_bytes)


def _kv_u32(key, value):
    key_bytes = key.encode("utf-8")
    return (struct.pack("<Q", len(key_bytes)) + key_bytes + struct.pack("<I", 4)
            + struct.pack("<I", int(value)))


def build_gguf(path, *, arch="qwen2", context=32768, layers=28, heads=28,
               kv_heads=4, hidden=3584, file_type=15):
    entries = [
        _kv_string("general.architecture", arch),
        _kv_u32(arch + ".context_length", context),
        _kv_u32(arch + ".block_count", layers),
        _kv_u32(arch + ".attention.head_count", heads),
        _kv_u32(arch + ".attention.head_count_kv", kv_heads),
        _kv_u32(arch + ".attention.key_length", 128),
        _kv_u32(arch + ".embedding_length", hidden),
        _kv_u32("general.file_type", file_type),
    ]
    header = (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0)
              + struct.pack("<Q", len(entries)))
    path.write_bytes(header + b"".join(entries))
    return path


class GGUFTest(unittest.TestCase):
    def test_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_gguf(Path(tmp) / "model.gguf")
            info = inspect_path(path)
            self.assertEqual(info.format, "gguf")
            self.assertEqual(info.architecture, "qwen2")
            self.assertEqual(info.max_context, 32768)
            self.assertEqual(info.layers, 28)
            self.assertEqual(info.heads, 28)
            self.assertEqual(info.kv_heads, 4)
            self.assertEqual(info.head_dim, 128)
            self.assertEqual(info.quantization, FILE_TYPES[15])
            self.assertEqual(info.completeness, "complete")
            self.assertTrue(info.runnable)

    def test_metadata_reader_returns_raw_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_gguf(Path(tmp) / "m.gguf", arch="llama")
            meta = read_gguf_metadata(path)
            self.assertEqual(meta["general.architecture"], "llama")
            self.assertEqual(meta["llama.context_length"], 32768)

    def test_model_without_head_length_uses_embedding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.gguf"
            entries = [
                {"k": "general.architecture", "v": "llama"},
                {"k": "llama.context_length", "v": 4096},
                {"k": "llama.block_count", "v": 32},
                {"k": "llama.attention.head_count", "v": 32},
                {"k": "llama.embedding_length", "v": 4096},
            ]
            parts = []
            for entry in entries:
                if isinstance(entry["v"], str):
                    parts.append(_kv_string(entry["k"], entry["v"]))
                else:
                    parts.append(_kv_u32(entry["k"], entry["v"]))
            header = (b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0)
                      + struct.pack("<Q", len(parts)))
            path.write_bytes(header + b"".join(parts))
            info = inspect_path(path)
            self.assertEqual(info.head_dim, 128)
            self.assertEqual(info.kv_heads, None)

    def test_broken_file_is_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.gguf"
            path.write_bytes(b"GGUF\x03\x00\x00\x00garbage")
            info = inspect_path(path)
            self.assertEqual(info.format, "gguf")
            self.assertEqual(info.completeness, "unknown")
            self.assertTrue(info.notes)

    def test_wrong_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.gguf"
            path.write_bytes(b"NOPE" + b"\x00" * 64)
            info = inspect_path(path)
            self.assertEqual(info.completeness, "unknown")

    def test_mmproj_is_supporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mmproj-vision.gguf"
            path.write_bytes(b"GGUF")
            info = inspect_path(path)
            self.assertTrue(info.is_supporting)
            self.assertFalse(info.runnable)


class SafetensorsTest(unittest.TestCase):
    def _package(self, root, *, index=True, missing=None, config=True):
        folder = Path(root) / "org" / "repo"
        folder.mkdir(parents=True)
        if config:
            (folder / "config.json").write_text(json.dumps({
                "architectures": ["LlamaForCausalLM"],
                "max_position_embeddings": 32768,
                "num_hidden_layers": 32,
                "num_attention_heads": 32,
                "num_key_value_heads": 8,
                "hidden_size": 4096,
                "torch_dtype": "bfloat16",
            }), encoding="utf-8")
        (folder / "tokenizer.json").write_text("{}", encoding="utf-8")
        shards = ["model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"]
        for name in shards:
            if name != missing:
                (folder / name).write_bytes(b"x" * 32)
        if index:
            weight_map = {"layer.0": shards[0], "layer.1": shards[1]}
            (folder / "model.safetensors.index.json").write_text(
                json.dumps({"weight_map": weight_map}), encoding="utf-8")
        return folder

    def test_complete_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._package(tmp)
            info = inspect_path(folder)
            self.assertEqual(info.format, "safetensors")
            self.assertEqual(info.architecture, "LlamaForCausalLM")
            self.assertEqual(info.max_context, 32768)
            self.assertEqual(info.dtype, "bfloat16")
            self.assertEqual(info.completeness, "complete")
            self.assertEqual(len(info.shards), 2)
            self.assertIn("tokenizer.json", info.tokenizer_files)

    def test_missing_shard_returns_incomplete_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._package(tmp, missing="model-00002-of-00002.safetensors")
            info = inspect_path(folder)
            self.assertEqual(info.completeness, "incomplete")
            self.assertEqual(info.missing_files, ("model-00002-of-00002.safetensors",))
            self.assertFalse(info.runnable)

    def test_lone_shard_is_supporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model-00001-of-00002.safetensors"
            path.write_bytes(b"x" * 16)
            info = inspect_path(path)
            self.assertEqual(info.format, "safetensors")
            self.assertTrue(info.is_supporting)
            self.assertEqual(info.completeness, "incomplete")

    def test_broken_config_is_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._package(tmp)
            (folder / "config.json").write_text("{not json", encoding="utf-8")
            info = inspect_path(folder)
            self.assertEqual(info.architecture, "unknown")
            self.assertTrue(any("config.json" in note for note in info.notes))

    def test_single_model_file_without_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "single.safetensors"
            path.write_bytes(b"x" * 24)
            info = inspect_path(path)
            self.assertEqual(info.size_bytes, 24)
            self.assertEqual(info.completeness, "incomplete")


class OnnxTest(unittest.TestCase):
    def test_external_data_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "onnx"
            folder.mkdir()
            graph = folder / "model.onnx"
            graph.write_bytes(b"\x08\x07external_data\x12\x10model.onnx_data" + b"\x00" * 32)
            info = inspect_path(graph)
            self.assertEqual(info.format, "onnx")
            self.assertEqual(info.completeness, "incomplete")
            self.assertEqual(info.missing_files, ("model.onnx_data",))
            (folder / "model.onnx_data").write_bytes(b"\x00" * 64)
            info = inspect_path(folder)
            self.assertEqual(info.completeness, "complete")
            self.assertGreater(info.size_bytes, 64)


class UnknownTest(unittest.TestCase):
    def test_unknown_extension_is_supporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("hi", encoding="utf-8")
            info = inspect_path(path)
            self.assertTrue(info.is_supporting)
            self.assertEqual(info.kind, "supporting_file")

    def test_missing_path(self):
        info = inspect_path("C:/definitely/not/here.gguf")
        self.assertEqual(info.completeness, "unknown")


if __name__ == "__main__":
    unittest.main()
