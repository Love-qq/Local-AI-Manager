"""Состояние чата и потокового ответа — без Tkinter.

Раньше текст ответа собирался в двух местах сразу: виджет получал первый кусок
отдельно от служебной подписи, а готовая строка ещё раз добавлялась в историю.
Здесь единственный источник истины — StreamAssembler: экран показывает ровно
то, что пришло от API, каждый кусок ровно один раз. Дублирование содержимого
не «лечится» постобработкой: если модель прислала «hh», так и будет «hh».
"""

from __future__ import annotations

from dataclasses import dataclass, field


class StreamStateError(RuntimeError):
    pass


@dataclass
class ChatMessage:
    role: str
    content: str


class StreamAssembler:
    """Складывает куски ответа. Одинаковые куски остаются разными кусками."""

    def __init__(self):
        self._parts = []
        self.closed = False
        self.chunks = 0

    def feed(self, chunk) -> str:
        if self.closed:
            raise StreamStateError("поток уже завершён")
        text = "" if chunk is None else str(chunk)
        if text:
            self._parts.append(text)
            self.chunks += 1
        return text

    def text(self) -> str:
        return "".join(self._parts)

    def close(self) -> str:
        self.closed = True
        return self.text()


class ChatTranscript:
    """История чата и текущий поток в одном месте (UI только рисует blocks())."""

    PLACEHOLDER = "…"

    def __init__(self):
        self.messages = []            # list[ChatMessage] — завершённые реплики
        self.stream = None            # StreamAssembler | None
        self.pending_placeholder = False
        self.last_error = ""

    # --- история ----------------------------------------------------------
    def add_user(self, text):
        self.messages.append(ChatMessage("user", str(text)))

    def add_assistant(self, text):
        self.messages.append(ChatMessage("assistant", str(text)))

    def add_error(self, text):
        self.last_error = str(text)
        self.messages.append(ChatMessage("error", str(text)))

    # --- поток ------------------------------------------------------------
    def begin_stream(self):
        if self.stream is not None:
            raise StreamStateError("поток уже идёт")
        self.stream = StreamAssembler()
        self.pending_placeholder = True
        return self.stream

    def feed(self, chunk):
        if self.stream is None:
            raise StreamStateError("поток не начат")
        text = self.stream.feed(chunk)
        if text:
            self.pending_placeholder = False
        return text

    @property
    def streaming(self) -> bool:
        return self.stream is not None

    def finish(self) -> str:
        if self.stream is None:
            raise StreamStateError("поток не начат")
        text = self.stream.close()
        self.stream = None
        self.pending_placeholder = False
        self.messages.append(ChatMessage("assistant", text))
        return text

    def cancel(self, reason="Генерация остановлена."):
        """Останавливает поток, не записывая пустой ответ в историю."""
        if self.stream is None:
            return ""
        partial = self.stream.close()
        self.stream = None
        self.pending_placeholder = False
        if partial:
            self.messages.append(ChatMessage("assistant", partial))
        self.add_error(reason)
        return partial

    # --- представление ----------------------------------------------------
    def blocks(self):
        """То, что должно быть видно: [(role, text)] без служебных строк."""
        blocks = [(message.role, message.content) for message in self.messages]
        if self.stream is not None:
            text = self.stream.text()
            if text:
                blocks.append(("assistant", text))
            elif self.pending_placeholder:
                blocks.append(("assistant", self.PLACEHOLDER))
        return blocks

    def history_for_api(self, *, system="", max_messages=24):
        """Сообщения для API: без ошибок и без пустых ответов."""
        usable = [m for m in self.messages if m.role in ("user", "assistant") and m.content.strip()]
        trimmed = usable[-max_messages:]
        while trimmed and trimmed[0].role != "user":
            trimmed.pop(0)
        payload = [{"role": m.role, "content": m.content} for m in trimmed]
        if system:
            payload.insert(0, {"role": "system", "content": system})
        return payload


class EventSubscription:
    """Не даёт навесить один и тот же обработчик дважды (утечка при переоткрытии)."""

    def __init__(self):
        self._bound = set()

    def bind(self, widget, sequence, callback, *, key=None):
        ident = (id(widget), sequence, key or getattr(callback, "__name__", str(callback)))
        if ident in self._bound:
            return False
        self._bound.add(ident)
        widget.bind(sequence, callback, add="+")
        return True

    def forget_widget(self, widget):
        self._bound = {item for item in self._bound if item[0] != id(widget)}

    def __len__(self):
        return len(self._bound)


def split_stream_lines(buffer: bytes):
    """Режет SSE-байты на строки, оставляя неполный хвост в буфере.

    Нужен для тестов и для ручных реализаций чтения: неполная строка не должна
    разбираться дважды при следующем чтении.
    """
    text = buffer.decode("utf-8", errors="replace")
    lines = text.split("\n")
    return lines[:-1], lines[-1].encode("utf-8")
