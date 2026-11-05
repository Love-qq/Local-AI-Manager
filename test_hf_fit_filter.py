import unittest

import local_ai_manager as lam


class GgufFilesWithSizeTests(unittest.TestCase):
    def test_sizes_from_siblings(self):
        payload = {"siblings": [
            {"rfilename": "README.md", "size": 100},
            {"rfilename": "model-Q4_K_M.gguf", "size": 1900 * 1024 ** 2},
            {"rfilename": "model-Q8_0.gguf", "size": 3200 * 1024 ** 2},
        ]}
        files = lam.gguf_files(payload)
        self.assertEqual([f["name"] for f in files],
                         ["model-Q4_K_M.gguf", "model-Q8_0.gguf"])
        self.assertEqual(files[0]["size"], 1900 * 1024 ** 2)

    def test_lfs_size_fallback(self):
        payload = {"siblings": [
            {"rfilename": "big.gguf", "lfs": {"size": 5 * 1024 ** 3}},
        ]}
        self.assertEqual(lam.gguf_files(payload)[0]["size"], 5 * 1024 ** 3)

    def test_missing_size_is_zero(self):
        payload = {"siblings": [{"rfilename": "unknown.gguf"}]}
        self.assertEqual(lam.gguf_files(payload)[0]["size"], 0)

    def test_empty_payload(self):
        self.assertEqual(lam.gguf_files({}), [])
        self.assertEqual(lam.gguf_files(None), [])

    def test_only_gguf_kept(self):
        payload = {"siblings": [
            {"rfilename": "a.safetensors", "size": 10},
            {"rfilename": "b.gguf", "size": 20},
        ]}
        self.assertEqual([f["name"] for f in lam.gguf_files(payload)], ["b.gguf"])

    def test_mmproj_is_not_offered_as_model(self):
        payload = {"siblings": [
            {"rfilename": "mmproj-Ornith.gguf", "size": 20},
            {"rfilename": "Ornith-Q4_K_M.gguf", "size": 100},
        ]}
        self.assertEqual([f["name"] for f in lam.gguf_files(payload)], ["Ornith-Q4_K_M.gguf"])


class FitsDecisionTests(unittest.TestCase):
    def test_small_model_fits_roomy_pc(self):
        gb = lam.bytes_to_gb(2 * 1024 ** 3)
        assessment = lam.assess_model_requirements(gb, 4096, 32.0, 24.0, 8.0, True, 99)
        self.assertTrue(lam.verdict_fits(assessment["verdict"]))

    def test_huge_model_does_not_fit_small_pc(self):
        gb = lam.bytes_to_gb(40 * 1024 ** 3)
        assessment = lam.assess_model_requirements(gb, 4096, 8.0, 3.0, 4.0, True, 99)
        self.assertFalse(lam.verdict_fits(assessment["verdict"]))

    def test_gpu_fit_uses_vram(self):
        gb = lam.bytes_to_gb(6 * 1024 ** 3)
        with_gpu = lam.assess_model_requirements(gb, 2048, 16.0, 10.0, 12.0, True, 99)
        without_gpu = lam.assess_model_requirements(gb, 2048, 16.0, 10.0, 12.0, False, 0)
        self.assertLess(with_gpu["required_gb"], without_gpu["required_gb"])


if __name__ == "__main__":
    unittest.main()
