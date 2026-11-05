"""Тесты моста ai_bridge.py: протокол, обход виджетов, действия."""

import json
import os
import queue
import tkinter as tk
import unittest

import ai_bridge


class BridgeProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.title("Bridge Test")
        cls.root.geometry("320x240+0+0")
        frame = tk.Frame(cls.root)
        frame.pack()
        cls.button_hits = []
        cls.button = tk.Button(frame, text="Тест", command=lambda: cls.button_hits.append(1))
        cls.button.pack()
        cls.entry = tk.Entry(frame)
        cls.entry.insert(0, "начало")
        cls.entry.pack()
        cls.canvas = tk.Canvas(frame, width=60, height=24)
        cls.canvas.create_text(30, 12, text="Кнопка-канвас")
        cls.canvas.pack()
        cls.root.update()
        ai_bridge._app = cls.root
        ai_bridge._TOKEN[0] = "test-token"

    @classmethod
    def tearDownClass(cls):
        ai_bridge._app = None
        cls.root.destroy()

    def serve(self, payload):
        return ai_bridge._serve(dict(payload, token="test-token"))

    def test_ping(self):
        result = self.serve({"cmd": "ping"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["title"], "Bridge Test")
        self.assertIn("tree", result["data"]["commands"])

    def test_bad_token(self):
        result = ai_bridge._serve({"cmd": "ping", "token": "wrong"})
        self.assertFalse(result["ok"])
        self.assertIn("токен", result["error"])

    def test_unknown_command(self):
        result = self.serve({"cmd": "нет-такой"})
        self.assertFalse(result["ok"])
        self.assertIn("неизвестная команда", result["error"])

    def test_tree_contains_widgets(self):
        result = self.serve({"cmd": "tree", "depth": 5})
        self.assertTrue(result["ok"])
        text = json.dumps(result["data"], ensure_ascii=False)
        self.assertIn("Button", text)
        self.assertIn("Entry", text)
        self.assertIn("Кнопка-канвас", text)

    def test_find_by_text(self):
        result = self.serve({"cmd": "find", "query": "Тест"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["items"][0]["cls"], "Button")

    def test_find_canvas_text(self):
        result = self.serve({"cmd": "find", "query": "канвас"})
        self.assertEqual(result["data"]["count"], 1)

    def test_set_and_get_entry(self):
        ref = self.serve({"cmd": "find", "cls": "entry"})["data"]["items"][0]["ref"]
        self.serve({"cmd": "set", "ref": ref, "value": "новое"})
        self.assertEqual(self.serve({"cmd": "get", "ref": ref})["data"]["text"], "новое")

    def test_invoke_button(self):
        ref = self.serve({"cmd": "find", "query": "Тест"})["data"]["items"][0]["ref"]
        self.button_hits.clear()
        self.serve({"cmd": "invoke", "ref": ref})
        self.assertEqual(self.button_hits, [1])

    def test_stale_ref(self):
        result = self.serve({"cmd": "get", "ref": "w9999"})
        self.assertFalse(result["ok"])
        self.assertIn("не найден", result["error"])

    def test_screenshot(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is not installed")
        result = self.serve({"cmd": "screenshot"})
        self.assertTrue(result["ok"])
        path = result["data"]["path"]
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 1000)

    def test_jsonable_does_not_block_on_queue(self):
        """Объекты с блокирующим get() не должны попадать в ответ (был дедлок)."""
        described = ai_bridge._jsonable(queue.Queue())
        self.assertIsInstance(described, dict)
        self.assertEqual(described["type"], "Queue")

    def test_attrs(self):
        self.root.custom_value = 42
        result = self.serve({"cmd": "attrs"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["custom_value"], 42)


if __name__ == "__main__":
    unittest.main()
