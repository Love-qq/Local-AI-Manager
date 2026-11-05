import unittest

import local_ai_manager as lam


class PcReadyFilterTests(unittest.TestCase):
    def test_ok_fits(self):
        self.assertTrue(lam.verdict_fits("ok"))

    def test_limited_fits(self):
        self.assertTrue(lam.verdict_fits("limited"))

    def test_fail_does_not_fit(self):
        self.assertFalse(lam.verdict_fits("fail"))

    def test_unknown_does_not_fit(self):
        self.assertFalse(lam.verdict_fits("неизвестно"))

    def test_verdict_from_assessment_fits_for_roomy_pc(self):
        assessment = lam.assess_model_requirements(3.0, 4096, 32.0, 24.0)
        self.assertTrue(lam.verdict_fits(assessment["verdict"]))

    def test_verdict_from_assessment_rejects_small_pc(self):
        assessment = lam.assess_model_requirements(24.0, 8192, 8.0, 2.0)
        self.assertFalse(lam.verdict_fits(assessment["verdict"]))


if __name__ == "__main__":
    unittest.main()
