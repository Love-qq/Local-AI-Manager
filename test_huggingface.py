import unittest

import local_ai_manager as lam


class HuggingFaceUrlTests(unittest.TestCase):
    def test_api_url_for_repo(self):
        self.assertEqual(
            lam.hf_api_url("bartowski/Qwen2.5-3B-Instruct-GGUF"),
            "https://huggingface.co/api/models/bartowski/Qwen2.5-3B-Instruct-GGUF")

    def test_api_url_strips_slashes(self):
        self.assertEqual(
            lam.hf_api_url("/bartowski/Model-GGUF/"),
            "https://huggingface.co/api/models/bartowski/Model-GGUF")

    def test_api_url_requires_repo(self):
        with self.assertRaises(ValueError):
            lam.hf_api_url("   ")

    def test_resolve_url(self):
        self.assertEqual(
            lam.hf_resolve_url("bartowski/Model-GGUF", "model-Q4_K_M.gguf"),
            "https://huggingface.co/bartowski/Model-GGUF/resolve/main/model-Q4_K_M.gguf")

    def test_resolve_url_subfolder_and_revision(self):
        self.assertEqual(
            lam.hf_resolve_url("org/repo", "/gguf/model.gguf", revision="v2"),
            "https://huggingface.co/org/repo/resolve/v2/gguf/model.gguf")

    def test_resolve_url_requires_filename(self):
        with self.assertRaises(ValueError):
            lam.hf_resolve_url("org/repo", "")


class GgufSiblingTests(unittest.TestCase):
    def test_only_gguf_files_sorted(self):
        payload = {"siblings": [
            {"rfilename": "README.md"},
            {"rfilename": "model-Q8_0.gguf"},
            {"rfilename": "model-Q4_K_M.gguf"},
            {"rfilename": "tokenizer.json"},
        ]}
        self.assertEqual(lam.gguf_siblings(payload),
                         ["model-Q4_K_M.gguf", "model-Q8_0.gguf"])

    def test_empty_payload(self):
        self.assertEqual(lam.gguf_siblings({}), [])
        self.assertEqual(lam.gguf_siblings(None), [])

    def test_ignores_broken_entries(self):
        payload = {"siblings": ["model.gguf", {"rfilename": None}, {"x": 1}]}
        self.assertEqual(lam.gguf_siblings(payload), [])

    def test_ignores_mmproj(self):
        payload = {"siblings": [
            {"rfilename": "mmproj-Qwen3.gguf"},
            {"rfilename": "model-Q4_K_M.gguf"},
        ]}
        self.assertEqual(lam.gguf_siblings(payload), ["model-Q4_K_M.gguf"])
        self.assertTrue(lam.is_mmproj_gguf("mmproj-Qwen3.gguf"))


class SizeTests(unittest.TestCase):
    def test_bytes_to_gb(self):
        self.assertAlmostEqual(lam.bytes_to_gb(1024 ** 3), 1.0)
        self.assertEqual(lam.bytes_to_gb(None), 0.0)

    def test_format_bytes(self):
        self.assertEqual(lam.format_bytes(1024 ** 3 * 2.5), "2.50 GB")
        self.assertEqual(lam.format_bytes(1024 ** 2 * 812), "812 MB")
        self.assertEqual(lam.format_bytes(2048), "2 KB")


if __name__ == "__main__":
    unittest.main()
