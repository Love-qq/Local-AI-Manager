"""Release security regression tests that do not require a GUI display."""
import os
import unittest
from unittest.mock import patch

import ai_bridge


class BridgeReleaseSecurityTests(unittest.TestCase):
    def test_bridge_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ai_bridge.enabled())

    def test_bridge_requires_explicit_opt_in(self):
        with patch.dict(os.environ, {"LAM_AI_BRIDGE": "1"}, clear=True):
            self.assertTrue(ai_bridge.enabled())

    def test_eval_requires_separate_explicit_opt_in(self):
        with patch.dict(os.environ, {"LAM_AI_BRIDGE": "1"}, clear=True):
            self.assertFalse(ai_bridge._eval_enabled())
        with patch.dict(os.environ, {
            "LAM_AI_BRIDGE": "1",
            "LAM_AI_BRIDGE_ALLOW_EVAL": "1",
        }, clear=True):
            self.assertTrue(ai_bridge._eval_enabled())

    def test_windows_app_user_model_id_is_consistent_and_not_misattributed(self):
        from pathlib import Path
        source = Path(__file__).parent
        app = (source / "local_ai_manager.py").read_text(encoding="utf-8")
        installer = (source / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("LocalAIManager.Desktop", app)
        self.assertIn('AppUserModelID: "LocalAIManager.Desktop"', installer)
        self.assertNotIn("OpenAI.LocalAIManager.Desktop", app + installer)

    def test_eval_command_is_rejected_without_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            previous = ai_bridge._TOKEN[0]
            ai_bridge._TOKEN[0] = None
            try:
                result = ai_bridge._serve({"cmd": "eval", "expr": "1 + 1"})
            finally:
                ai_bridge._TOKEN[0] = previous
        self.assertFalse(result["ok"])
        self.assertIn("eval отключена", result["error"])


if __name__ == "__main__":
    unittest.main()
