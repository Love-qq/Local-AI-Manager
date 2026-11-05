"""Запросы к локальному серверу: без системного и env-прокси.

`urllib` по умолчанию поднимает ProxyHandler и уводит *любой* запрос через
прокси из переменных окружения (HTTP_PROXY/HTTPS_PROXY) или системных настроек
Windows. Прокси не может достучаться до 127.0.0.1 пользователя, поэтому
локальный API llama.cpp при включённом прокси отвечал ошибкой (а внешний прокси
получал адрес локального сервера). Здесь — единственная точка для локальных
запросов: прокси отключён, TLS-контекст и проверка сертификатов сохраняются.

Модуль не импортирует Tkinter и ничего не знает про UI.
"""

from __future__ import annotations

import urllib.parse
import urllib.request

from .tls import https_handler

LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_OPENER = None


def is_local_url(url: str) -> bool:
    """True для адресов локального сервера (127.0.0.1, ::1, localhost)."""
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        host = (parsed.hostname or "").strip().lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS


def local_opener() -> urllib.request.OpenerDirector:
    """Opener с отключённым прокси и проверенным TLS-контекстом."""
    global _OPENER
    if _OPENER is None:
        # Пустой ProxyHandler перекрывает системные/env-прокси: для 127.0.0.1
        # прокси не нужен и только ломает запрос.
        _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                              https_handler())
    return _OPENER


def open_local(url, *, timeout=30, data=None, headers=None, method=None):
    """Открыть локальный URL (или Request) в обход прокси.

    Принимает `urllib.request.Request` либо строку-адрес. Внешние адреса
    запрещены намеренно: они обязаны идти через системный прокси, и обходить
    его можно только для локального сервера.
    """
    if isinstance(url, urllib.request.Request):
        request = url
    else:
        request = urllib.request.Request(url, data=data, headers=dict(headers or {}),
                                         method=method)
    if not is_local_url(request.full_url):
        raise ValueError(f"open_local() принимает только локальные адреса: {request.full_url}")
    return local_opener().open(request, timeout=timeout)
