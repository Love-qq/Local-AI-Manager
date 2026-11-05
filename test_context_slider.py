import unittest

import local_ai_manager as lam


class ContextSliderTests(unittest.TestCase):
    def test_first_step(self):
        self.assertEqual(lam.context_for_index(0), "2048")

    def test_last_step(self):
        self.assertEqual(lam.context_for_index(len(lam.DEFAULT_CONTEXTS) - 1), "131072")

    def test_float_value_from_scale(self):
        self.assertEqual(lam.context_for_index(2.4), "8192")
        self.assertEqual(lam.context_for_index(2.6), "16384")

    def test_out_of_range_is_clamped(self):
        self.assertEqual(lam.context_for_index(-5), "2048")
        self.assertEqual(lam.context_for_index(99), "131072")

    def test_bad_value_falls_back_to_first(self):
        self.assertEqual(lam.context_for_index("abc"), "2048")
        self.assertEqual(lam.context_for_index(None), "2048")

    def test_unchanged_range_and_value_do_not_reset_thumb(self):
        self.assertFalse(lam.context_slider_needs_reset(6, 6, 3, 3))

    def test_changed_value_resets_thumb(self):
        self.assertTrue(lam.context_slider_needs_reset(6, 6, 3, 4))

    def test_changed_range_resets_thumb(self):
        self.assertTrue(lam.context_slider_needs_reset(6, 5, 3, 3))

    def test_missing_target_only_resets_when_range_changes(self):
        self.assertFalse(lam.context_slider_needs_reset(6, 6, 3, None))
        self.assertTrue(lam.context_slider_needs_reset(6, 5, 3, None))

    def test_end_labels_follow_the_current_stops(self):
        fake = type("Slider", (), {"stops": [2048, 4096, 32768]})()
        self.assertEqual(lam.ContextSlider._stop_label(fake, 2), "32K")
        self.assertEqual(lam.ContextSlider._stop_label(fake, 9), "")

    def test_all_steps_are_valid_numbers(self):
        for index in range(len(lam.DEFAULT_CONTEXTS)):
            value = lam.context_for_index(index)
            self.assertTrue(value.isdigit())
            self.assertGreaterEqual(int(value), 2048)


if __name__ == "__main__":
    unittest.main()
