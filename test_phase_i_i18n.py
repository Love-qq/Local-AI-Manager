"""Фаза I: словари ru/en, t(), переключение языка и отсутствие "сырых" строк."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from lamcore import i18n


PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)")


def placeholders(text):
    return set(PLACEHOLDER.findall(text))


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ru = i18n.load_catalog("ru")
        cls.en = i18n.load_catalog("en")

    def test_key_sets_match(self):
        self.assertEqual(i18n.keys(self.ru), i18n.keys(self.en))

    def test_no_empty_values(self):
        self.assertEqual(i18n.empty_values(self.ru), [])
        self.assertEqual(i18n.empty_values(self.en), [])

    def test_validate_catalogs_is_clean(self):
        self.assertEqual(i18n.validate_catalogs(self.ru, self.en), [])

    def test_placeholders_match_per_key(self):
        mismatched = {key: (placeholders(self.ru[key]), placeholders(self.en[key]))
                      for key in self.ru
                      if placeholders(self.ru[key]) != placeholders(self.en[key])}
        self.assertEqual(mismatched, {})

    def test_both_languages_are_present(self):
        self.assertIn("Русский", i18n.LOCALES["ru"])
        self.assertIn("English", i18n.LOCALES["en"])

    def test_english_catalog_is_not_russian(self):
        russian = re.compile(r"[А-Яа-яЁё]")
        untranslated = [key for key, value in self.en.items()
                        if key != "lang.ru" and russian.search(PLACEHOLDER.sub("", value))]
        self.assertEqual(untranslated, [])


class TranslatorTest(unittest.TestCase):
    def setUp(self):
        self.trans = i18n.Translator("ru")

    def t(self, key, **params):
        return self.trans.t(key, **params)

    def test_translates_and_formats(self):
        text = self.t("hf.file_count", count=5)
        self.assertIn("5", text)
        self.assertIn("Файл", text)

    def test_switch_language(self):
        self.assertIn("ЗАПУСТИТЬ", self.t("app.start"))
        self.trans.set_locale("en")
        self.assertIn("START", self.t("app.start"))
        self.assertNotIn("ЗАПУСТИТЬ", self.t("app.start"))

    def test_unknown_key_returns_key(self):
        self.assertEqual(self.t("no.such.key"), "no.such.key")

    def test_missing_parameter_does_not_crash(self):
        self.assertIn("{count}", self.t("hf.file_count"))

    def test_listener_called_once_per_change(self):
        calls = []
        self.trans.on_change(calls.append)
        self.trans.on_change(calls.append)  # повторная подписка не дублируется
        self.trans.set_locale("en")
        self.assertEqual(calls, ["en"])

    def test_bad_locale_rejected(self):
        with self.assertRaises(ValueError):
            self.trans.set_locale("de")


class UiLiteralsTest(unittest.TestCase):
    """Строки интерфейса берутся из t(); исключения перечислены явно."""

    SOURCE = Path(__file__).with_name("local_ai_manager.py")

    ALLOWED = {
        "brand",              # логотип и фирменное имя не переводятся
        "technical",          # имена параметров CLI (-ngl, -t)
        "glyph",              # символы кнопок (×, —, ▶)
        "engine_name",        # llama.cpp / ONNX Runtime — имена продуктов
        "format_name",        # GGUF / safetensors
    }

    def test_migrated_screens_have_no_hardcoded_labels(self):
        """В отмеченных функциях нет русских литералов вне t()."""
        import inspect
        import local_ai_manager as lam

        migrated = [
            lam.LocalChatWindow._build,
            lam.MainMonitorPanel._build,
            lam.LocalAIManager._create_custom_titlebar,
            lam.HuggingFaceDialog._build,
        ]

        russian = re.compile(r"[А-Яа-яЁё]")
        problems = []
        for function in migrated:
            for line in inspect.getsource(function).splitlines():
                code = line.split("#", 1)[0]
                for literal in re.findall(r'"([^"\n]*)"|\'([^\'\n]*)\'', code):
                    text = literal[0] or literal[1]
                    if russian.search(text) and 't("' not in code and "t('" not in code:
                        problems.append(f"{function.__name__}: {text}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
