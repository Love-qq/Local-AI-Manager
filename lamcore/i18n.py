"""Локализация UI: стабильные ключи, два каталога, живое переключение.

Каталоги лежат в lamcore/i18n/<locale>.json, ключи плоские ("chat.send").
t() подставляет параметры {name}; отсутствующий ключ возвращается как есть,
чтобы ошибку было видно в интерфейсе, а не пряталось за английским текстом.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LOCALES = {"ru": "Русский", "en": "English"}
DEFAULT_LOCALE = "ru"
FALLBACK_LOCALE = "ru"


def catalog_dir() -> Path:
    # В EXE (PyInstaller) JSON лежат в _MEIPASS/lamcore/i18n.
    bundled = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", "") else None
    if bundled is not None:
        candidate = bundled / "lamcore" / "i18n"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent / "i18n"


def catalog_path(locale, folder=None) -> Path:
    return Path(folder or catalog_dir()) / f"{locale}.json"


def load_catalog(locale, folder=None) -> dict:
    path = catalog_path(locale, folder)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"каталог {path.name} не читается: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"каталог {path.name} должен быть JSON-объектом")
    return {str(key): str(value) for key, value in payload.items()}


class _Keep(dict):
    def __missing__(self, key):
        return "{" + key + "}"


class Translator:
    def __init__(self, locale=DEFAULT_LOCALE, *, folder=None, catalogs=None):
        self.folder = folder
        self._cache = dict(catalogs or {})
        self.locale = locale if locale in self._cache or self._exists(locale) else DEFAULT_LOCALE
        self._listeners = []

    def _exists(self, locale):
        try:
            return catalog_path(locale, self.folder).is_file()
        except OSError:
            return False

    def catalog(self, locale):
        if locale not in self._cache:
            self._cache[locale] = load_catalog(locale, self.folder)
        return self._cache[locale]

    def set_locale(self, locale):
        if locale not in LOCALES:
            raise ValueError(f"неизвестный язык: {locale}")
        self.locale = locale
        for callback in list(self._listeners):
            callback(locale)
        return locale

    def on_change(self, callback):
        """Подписка на смену языка. Повторная подписка того же объекта не дублируется."""
        if callback not in self._listeners:
            self._listeners.append(callback)
        return callback

    def t(self, key, **params):
        text = None
        try:
            text = self.catalog(self.locale).get(key)
        except ValueError:
            text = None
        if text is None and self.locale != FALLBACK_LOCALE:
            try:
                text = self.catalog(FALLBACK_LOCALE).get(key)
            except ValueError:
                text = None
        if text is None:
            return key
        if not params:
            return text
        try:
            return text.format_map(_Keep({k: v for k, v in params.items()}))
        except (IndexError, KeyError, ValueError):
            return text

    __call__ = t


def keys(catalog) -> set:
    return set(catalog.keys())


def empty_values(catalog) -> list:
    return sorted(key for key, value in catalog.items() if not str(value or "").strip())


def validate_catalogs(left, right, *, left_name="ru", right_name="en") -> list:
    """Возвращает список проблем: разные наборы ключей, пустые значения."""
    problems = []
    only_left = sorted(keys(left) - keys(right))
    only_right = sorted(keys(right) - keys(left))
    if only_left:
        problems.append(f"только в {left_name}: {', '.join(only_left[:12])}")
    if only_right:
        problems.append(f"только в {right_name}: {', '.join(only_right[:12])}")
    for name, catalog in ((left_name, left), (right_name, right)):
        empty = empty_values(catalog)
        if empty:
            problems.append(f"пустые значения в {name}: {', '.join(empty[:12])}")
    return problems


def check() -> list:
    return validate_catalogs(load_catalog("ru"), load_catalog("en"))
