import inspect
import unittest

import local_ai_manager as lam


class WindowsFramelessRegressionTest(unittest.TestCase):
    """The native Windows title bar is removed with overrideredirect, not by
    stripping WS_CAPTION (which left black/duplicated regions)."""

    def test_root_window_is_borderless_on_windows(self):
        source = inspect.getsource(lam.LocalAIManager.__init__)
        self.assertIn('self.overrideredirect(True)', source)
        self.assertIn('_init_taskbar_button', source)
        self.assertIn('_create_resize_grips', source)

    def test_caption_and_frame_styles_are_not_touched(self):
        source = inspect.getsource(lam.LocalAIManager._apply_windows_window_style)
        self.assertNotIn('WS_CAPTION =', source)
        self.assertNotIn('WS_THICKFRAME', source)
        self.assertNotIn('SWP_FRAMECHANGED', source)

    def test_taskbar_button_and_minimize_support(self):
        source = inspect.getsource(lam.LocalAIManager._apply_windows_window_style)
        self.assertIn('WS_EX_APPWINDOW', source)
        self.assertIn('WS_EX_TOOLWINDOW', source)
        self.assertIn('WS_MINIMIZEBOX', source)
        self.assertIn('ShowWindow', inspect.getsource(lam.LocalAIManager.minimize_window))

    def test_maximize_respects_work_area(self):
        source = inspect.getsource(lam.LocalAIManager.toggle_maximize)
        self.assertIn('_work_area', source)


if __name__ == '__main__':
    unittest.main()
