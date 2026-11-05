import unittest

import local_ai_manager as lam


class WheelScrollTests(unittest.TestCase):
    def test_wheel_up_scrolls_up(self):
        self.assertEqual(lam.wheel_scroll_units(120), -60)

    def test_wheel_down_scrolls_down(self):
        self.assertEqual(lam.wheel_scroll_units(-120), 60)

    def test_wheel_large_delta_keeps_one_step(self):
        self.assertEqual(lam.wheel_scroll_units(-360), 60)

    def test_wheel_zero_delta_does_nothing(self):
        self.assertEqual(lam.wheel_scroll_units(0), 0)

    def test_wheel_custom_step(self):
        self.assertEqual(lam.wheel_scroll_units(120, step=120), -120)


if __name__ == "__main__":
    unittest.main()
