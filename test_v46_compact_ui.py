"""Компактный интерфейс v46: панели, кнопки, автоперенос, заголовки диалогов."""
import inspect
import tkinter as tk
import unittest

import local_ai_manager as lam


class CompactUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"нет дисплея для Tk: {exc}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def test_rounded_panel_shorter_than_60px_is_not_clipped(self):
        panel = lam.RoundedPanel(self.root, page="#212337", fill="#272942", shadow="#151726",
                                 radius=16, padding=6, lateral=8)
        panel.pack(fill="x")
        tk.Label(panel.inner, text="x").pack()
        self.root.deiconify()
        self.root.geometry("400x200+0+0")
        self.root.update()
        panel._redraw()
        _x0, _y0, _x1, y1 = panel.bbox("deco")
        self.assertLess(panel.winfo_height(), 60)
        self.assertLessEqual(y1, panel.winfo_height(), "фон панели не должен выходить за её границы")
        panel.destroy()

    def test_button_hover_is_animated_not_instant(self):
        button = lam.RoundedButton(self.root, "ОК", lambda: None,
                                   {"card": "#272942", "card_alt": "#2d2f4a", "field": "#1f2134",
                                    "text": "#ffffff", "accent": "#a653f5", "accent_hover": "#bd79ff",
                                    "accent_deep": "#8f49e8", "accent_pressed": "#8640d5",
                                    "shadow": "#151726", "border": "#343757"})
        button.pack()
        self.root.update()
        button._btn_enter()
        self.root.update()
        self.assertTrue(0.0 <= button._btn_anim < 1.0 or button._btn_anim_job is not None)
        for _ in range(40):
            self.root.update()
            self.root.after(16)
        self.assertAlmostEqual(button._btn_anim, 1.0, delta=0.05)
        button._btn_leave()
        for _ in range(40):
            self.root.update()
            self.root.after(16)
        self.assertAlmostEqual(button._btn_anim, 0.0, delta=0.05)
        button.destroy()

    def test_autowrap_follows_label_width(self):
        label = tk.Label(self.root, text="слово " * 80, justify="left")
        lam.LocalAIManager._autowrap(label)
        label.pack(fill="x")
        self.root.deiconify()
        self.root.geometry("500x300+0+0")
        self.root.update()
        self.root.update()
        first = int(float(label.cget("wraplength")))
        self.root.geometry("900x300+0+0")
        self.root.update()
        self.root.update()
        second = int(float(label.cget("wraplength")))
        self.assertTrue(second > first > 100)
        label.destroy()

    def test_fade_in_ends_fully_visible(self):
        top = tk.Toplevel(self.root)
        lam.fade_in(top, duration=60, steps=4)
        for _ in range(30):
            self.root.update()
            self.root.after(10)
        try:
            self.assertAlmostEqual(float(top.attributes("-alpha")), 1.0, delta=0.01)
        except tk.TclError:
            self.skipTest("прозрачность не поддерживается")
        finally:
            top.destroy()

    def test_chat_and_agent_settings_use_frameless_dark_titlebar(self):
        source = inspect.getsource(lam.LocalChatWindow.__init__)
        settings = inspect.getsource(lam.AgentSkillManager.__init__)
        # Заголовок теперь берётся из словаря (i18n), поэтому проверяем вызов и ключ.
        self.assertIn("_build_frameless_dialog_titlebar(self, app,", source)
        self.assertIn('self.t("chat.title")', source)
        self.assertIn('_build_frameless_dialog_titlebar(self, app,', settings)

    def test_engine_download_button_uses_short_label(self):
        source = inspect.getsource(lam.LocalAIManager._build)
        # Текст кнопки берётся из словаря; ключ lib.engine_download — короткий.
        self.assertIn('engine_actions, self.t("lib.engine_download")', source)
        self.assertNotIn("Скачать выбранный движок", source)


class MainWindowFitTest(unittest.TestCase):
    """Штатное окно 1440x900 должно вмещать все панели без прокрутки."""

    SCRIPT = r"""
import json, tempfile
from pathlib import Path
import local_ai_manager as lam

tmp = Path(tempfile.mkdtemp())
lam.APP_DIR = tmp
lam.CONFIG_FILE = tmp / "config.json"
app = lam.LocalAIManager()
app.geometry("1440x900")
for job in app.tk.call("after", "info"):
    app.after_cancel(job)
app.update()
height = app.scroll_canvas.winfo_height()
y0, y1 = app.scroll_canvas.bbox("all")[1::2]
print(json.dumps({"content": y1 - y0, "view": height, "needed": bool(app._scroll_needed)}))
"""

    def test_content_fits_default_size(self):
        """До правки содержимое было 880 px при видимой высоте 862 px:
        полоса журнала уезжала за нижний край и появлялся скроллбар.

        Окно строится в отдельном процессе: несколько Tk-интерпретаторов в одном
        процессе после destroy() иногда не пересоздаются ("invalid command name
        tcl_findLibrary"), и тест становился плавающим.
        """
        import json
        import subprocess
        import sys
        from pathlib import Path

        result = subprocess.run(
            [sys.executable, "-c", self.SCRIPT],
            cwd=str(Path(lam.__file__).resolve().parent),
            capture_output=True, text=True, timeout=180)
        if "no display" in (result.stderr or "").lower():
            raise unittest.SkipTest("нет дисплея для Tk")
        self.assertEqual(result.returncode, 0, result.stderr)
        metrics = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertLessEqual(metrics["content"], metrics["view"],
                             "панели должны помещаться в окно 1440x900 без прокрутки")
        self.assertFalse(metrics["needed"])


class TitlebarRestoreTest(unittest.TestCase):
    """Главное окно остаётся обычным taskbar-окном без мигания стилей."""

    def test_root_window_keeps_native_taskbar_identity(self):
        init_source = inspect.getsource(lam.LocalAIManager.__init__)
        minimize_source = inspect.getsource(lam.LocalAIManager.minimize_window)
        style_source = inspect.getsource(lam.LocalAIManager._apply_windows_window_style)

        self.assertIn("self.overrideredirect(True)", init_source)
        self.assertIn("self.iconify()", minimize_source)
        self.assertIn("self.overrideredirect(False)", minimize_source)
        self.assertIn("_restore_frameless_after_map", init_source)
        self.assertIn("WS_EX_APPWINDOW", style_source)
        self.assertIn("WS_EX_TOOLWINDOW", style_source)


if __name__ == "__main__":
    unittest.main()
