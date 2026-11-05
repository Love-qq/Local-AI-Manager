"""Единая таблица таймеров и правило «спит, когда окно не видно».

Раньше опрос был размазан по классам: у монитора 1800 мс, у журнала 150 мс,
у панели состояния 2500 мс, у чата 80 мс, у проверки процесса 1500 мс — и все
работали, даже когда окно свёрнуто. Здесь таблица служит документацией и
источником интервалов, а should_pause() выключает опрос у скрытых окон.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PollSpec:
    name: str
    interval_ms: int
    owner: str
    purpose: str
    pauses_when_hidden: bool = True


# Интервалы подобраны так, чтобы 1.8-2.5 с хватало для телеметрии и не было
# двух таймеров с одной задачей. Очередь журнала — единственный «быстрый» опрос.
TIMER_TABLE = (
    PollSpec("log_queue", 150, "LocalAIManager", "забрать строки вывода сервера и события фоновых задач"),
    PollSpec("process_state", 2000, "LocalAIManager", "состояние процесса llama-server"),
    PollSpec("monitor", 2000, "MainMonitorPanel", "RAM/VRAM/API и время работы"),
    PollSpec("chat_events", 100, "LocalChatWindow", "куски потокового ответа модели"),
    PollSpec("dashboard", 2500, "RuntimeDashboard", "телеметрия окна мониторинга"),
    PollSpec("hf_events", 150, "HuggingFaceDialog", "очередь загрузки/списка файлов HF"),
    PollSpec("toast", 16, "show_notification", "анимация уведомления"),
)

HIDDEN_STATES = ("iconic", "withdrawn")


def should_pause(window, *, visible=None) -> bool:
    """True, если окно скрыто и опрос нужно приостановить.

    visible можно передать явно (например, активная вкладка), в тестах вместо
    окна достаточно объекта с методами winfo_ismapped()/state().
    """
    if visible is False:
        return True
    try:
        if not window.winfo_exists():
            return True
        if not window.winfo_ismapped():
            return True
        if str(window.state()).lower() in HIDDEN_STATES:
            return True
    except Exception:
        # Окно уже уничтожено — опрос не нужен.
        return True
    return False


def next_delay(name, *, hidden=False) -> int:
    """Интервал таймера по имени. У скрытого окна опрос реже, а не отключается."""
    for spec in TIMER_TABLE:
        if spec.name == name:
            return spec.interval_ms * (4 if hidden and spec.pauses_when_hidden else 1)
    raise KeyError(name)


def spec(name) -> PollSpec:
    for item in TIMER_TABLE:
        if item.name == name:
            return item
    raise KeyError(name)


def duplicates(table=TIMER_TABLE):
    names = [item.name for item in table]
    return sorted({name for name in names if names.count(name) > 1})
