import inspect
import json
import unittest
from pathlib import Path

import local_ai_manager as lam


class UiFixesV481Test(unittest.TestCase):
    def test_main_monitor_body_uses_its_own_queue(self):
        source = inspect.getsource(lam.MainMonitorPanel._refresh_body)
        self.assertIn("_worker_queue", source)
        self.assertNotIn("_gpu_queue", source)
        self.assertNotIn("self.metrics", source)

    def test_stop_button_follows_process_state(self):
        self.assertIn("_sync_header_buttons", inspect.getsource(lam.LocalAIManager.set_status))
        source = inspect.getsource(lam.LocalAIManager._sync_header_buttons)
        self.assertIn("header_stop_btn", source)
        self.assertIn('"normal"', source)

    def test_stretch_panels_keep_natural_height(self):
        source = inspect.getsource(lam.RoundedPanel._fit_height)
        self.assertNotIn("if self.stretch:\n            return", source)

    def test_dialogs_fit_content_and_have_border(self):
        source = inspect.getsource(lam._build_frameless_dialog_titlebar)
        self.assertIn("fit_toplevel_to_content", source)
        self.assertIn("highlightthickness=1", source)

    def test_chat_empty_hint_is_translated(self):
        base = Path(lam.__file__).resolve().parent / "lamcore" / "i18n"
        for code in ("ru", "en"):
            data = json.loads((base / f"{code}.json").read_text(encoding="utf-8"))
            self.assertTrue(data.get("chat.empty"))


if __name__ == "__main__":
    unittest.main()
