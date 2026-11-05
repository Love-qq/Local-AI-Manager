"""Phase F: KV-кэш из метаданных, рекомендация контекста, ручные пределы."""

import unittest

from lamcore.hardware import (CONTEXT_STOPS, GpuInfo, HardwareInfo, kv_cache_bytes,
                              max_output_tokens, manual_context, recommend_context)
from lamcore.modelinfo import ModelInfo
from lamcore.uirules import (launch_params_visible, param_visible, relevant_params)


def model(**overrides):
    data = dict(path="model.gguf", format="gguf", architecture="llama", quantization="Q4_K_M",
                max_context=32768, size_bytes=2 * 1024 ** 3, completeness="complete",
                layers=28, heads=28, kv_heads=4, head_dim=128)
    data.update(overrides)
    return ModelInfo(**data)


def hardware(vram=24.0, free_ram=32.0, total_ram=32.0):
    return HardwareInfo(ram_total_gb=total_ram, ram_free_gb=free_ram,
                        gpus=(GpuInfo("Test GPU", vram),), cpu="Test CPU", cores=8)


class KvCacheTest(unittest.TestCase):
    def test_formula_uses_metadata(self):
        info = model()
        expected = 2 * 28 * 4096 * 4 * 128 * 2
        self.assertEqual(kv_cache_bytes(model(), 4096), expected)

    def test_missing_metadata_returns_none(self):
        self.assertIsNone(kv_cache_bytes(ModelInfo(path="x"), 4096))


class RecommendContextTest(unittest.TestCase):
    def test_never_exceeds_model_limit(self):
        result = recommend_context(model(max_context=4096), HardwareInfo(
            ram_total_gb=64, ram_free_gb=64, gpus=(GpuInfo("big", 48),)))
        self.assertEqual(recommend_context(model(max_context=4096), HardwareInfo(
            ram_total_gb=64, ram_free_gb=64, gpus=(GpuInfo("big", 48),))).context, 4096)
        self.assertEqual(result.limited_by, "model")

    def test_low_vram_lowers_recommendation(self):
        big = recommend_context(model(), HardwareInfo(ram_total_gb=64, ram_free_gb=64,
                                                      gpus=(GpuInfo("big", 24),)))
        small = recommend_context(model(), HardwareInfo(ram_total_gb=64, ram_free_gb=64,
                                                        gpus=(GpuInfo("small", 3),)))
        self.assertLess(small.context, big.context)
        self.assertEqual(big.context, 32768)
        self.assertEqual(small.limited_by, "vram")

    def test_low_ram_lowers_recommendation_without_gpu(self):
        plenty = recommend_context(model(), hardware(vram=0, free_ram=32), gpu_offload=False)
        tight = recommend_context(model(), hardware(vram=0, free_ram=4), gpu_offload=False)
        self.assertLess(tight.context, plenty.context)
        self.assertEqual(tight.limited_by, "ram")

    def test_context_is_a_known_stop(self):
        for vram in (2, 4, 8, 24, 80):
            result = recommend_context(model(), HardwareInfo(ram_free_gb=64,
                                                             gpus=(GpuInfo("g", vram),)))
            self.assertIn(result.context, CONTEXT_STOPS)

    def test_unknown_metadata_is_reported_as_unknown(self):
        info = ModelInfo(path="x.gguf", format="gguf", size_bytes=1)
        result = recommend_context(info, hardware())
        self.assertEqual(result.limited_by, "unknown")
        self.assertFalse(result.exact)

    def test_tiny_vram_does_not_overflow(self):
        result = recommend_context(model(), HardwareInfo(ram_free_gb=2,
                                                         gpus=(GpuInfo("g", 0.5),)))
        self.assertEqual(result.context, 2048)
        self.assertTrue(any("2048" in note for note in result.notes))


class ManualContextTest(unittest.TestCase):
    def test_clamps_to_model_limit(self):
        value, reason, changed = manual_context(999999, model(max_context=8192))
        self.assertEqual(value, 8192)
        self.assertTrue(changed)
        self.assertIn("8192", reason)

    def test_clamps_to_minimum(self):
        value, _reason, changed = manual_context(16, model())
        self.assertEqual(value, 512)
        self.assertTrue(changed)

    def test_valid_value_is_untouched(self):
        self.assertEqual(manual_context(4096, model()), (4096, "", False))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            manual_context("много", model())


class OutputTokensTest(unittest.TestCase):
    def test_limited_by_remaining_context(self):
        self.assertEqual(max_output_tokens(4096, requested=8192), 3584)

    def test_limited_by_engine(self):
        self.assertEqual(max_output_tokens(131072, requested=32768, engine_max=4096), 4096)

    def test_never_zero(self):
        self.assertEqual(max_output_tokens(512, requested=1024), 1)


class UiRulesTest(unittest.TestCase):
    def test_section_hidden_until_a_model_exists(self):
        self.assertFalse(launch_params_visible(0))
        self.assertTrue(launch_params_visible(1))

    def test_engine_switch_hides_inapplicable_fields(self):
        self.assertIn("gpu_layers", relevant_params("llama.cpp", "gguf"))
        self.assertNotIn("gpu_layers", relevant_params("ONNX Runtime", "onnx"))
        self.assertFalse(param_visible("threads", "Transformers + PyTorch", "safetensors"))
        self.assertTrue(param_visible("port", "ONNX Runtime", "onnx"))


if __name__ == "__main__":
    unittest.main()
