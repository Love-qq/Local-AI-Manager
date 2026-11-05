import unittest

from local_ai_manager import assess_model_requirements


class SystemRequirementsTests(unittest.TestCase):
    def test_model_with_free_ram_is_compatible(self):
        result = assess_model_requirements(3.0, 4096, 16.0, 12.0)
        self.assertEqual(result["verdict"], "ok")

    def test_low_free_ram_is_limited(self):
        result = assess_model_requirements(6.0, 4096, 16.0, 7.5)
        self.assertEqual(result["verdict"], "limited")
        self.assertIn("проблемами", result["message"])

    def test_insufficient_total_ram_is_not_recommended(self):
        result = assess_model_requirements(12.0, 8192, 8.0, 6.0)
        self.assertEqual(result["verdict"], "fail")

    def test_gpu_offload_reduces_ram_requirement(self):
        cpu = assess_model_requirements(8.0, 4096, 16.0, 8.0)
        gpu = assess_model_requirements(8.0, 4096, 16.0, 8.0, 12.0, True, 99)
        self.assertLess(gpu["required_gb"], cpu["required_gb"])


if __name__ == "__main__":
    unittest.main()
