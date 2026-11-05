"""Мост управления Local AI Manager для ИИ-агентов и автотестов.

Работает внутри процесса приложения: локальный TCP-сервер на 127.0.0.1 с
построчным JSON-протоколом. Все обращения к Tk выполняются в главном потоке
через очередь, поэтому гонок с интерфейсом нет.

Подключение в приложении (одна строка после создания главного окна):

    import ai_bridge
    ai_bridge.start(app)

Включить только осознанно: LAM_AI_BRIDGE=1. По умолчанию мост выключен.
Порт: LAM_AI_BRIDGE_PORT (по умолчанию 8791, при занятости берётся следующий).

Файл состояния ~/.local_ai_manager_bridge.json содержит порт, токен и pid —
его читает ai_bridge_mcp.py, чтобы найти работающее приложение.
"""

import atexit
import json
import os
import queue
import secrets
import socket
import socketserver
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback

DEFAULT_PORT = 8791
STATE_PATH = os.path.join(os.path.expanduser("~"), ".local_ai_manager_bridge.json")

_app = None
_server = None
_pending = queue.Queue()
_refs = {}
_ref_by_path = {}
_ref_counter = [0]
_lock = threading.Lock()


# --- инфраструктура ---------------------------------------------------------

def enabled():
    """Мост удалённого управления выключен по умолчанию для безопасного релиза."""
    return os.environ.get("LAM_AI_BRIDGE", "0").strip().lower() in ("1", "true", "on", "yes")


def _eval_enabled():
    """Произвольное выполнение Python доступно только при отдельном явном opt-in."""
    return os.environ.get("LAM_AI_BRIDGE_ALLOW_EVAL", "0").strip().lower() in ("1", "true", "on", "yes")


def _pump():
    """Крутится в главном потоке: выполняет накопленные задачи Tk."""
    while True:
        try:
            task, slot = _pending.get_nowait()
        except queue.Empty:
            break
        try:
            slot["value"] = task()
        except BaseException as exc:  # noqa: BLE001 - ошибку отдаём клиенту
            slot["error"] = exc
        finally:
            slot["event"].set()
    try:
        _app.after(20, _pump)
    except Exception:  # окно закрывается
        pass


def _run_on_ui(task, timeout=25.0):
    if threading.current_thread() is threading.main_thread():
        return task()
    slot = {"event": threading.Event(), "value": None, "error": None}
    _pending.put((task, slot))
    if not slot["event"].wait(timeout):
        raise TimeoutError("интерфейс не ответил за %.0f с (открыт модальный диалог?)" % timeout)
    if slot["error"] is not None:
        raise slot["error"]
    return slot["value"]


# --- описание виджетов ------------------------------------------------------

def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _ref_for(widget):
    path = str(widget)
    with _lock:
        ref = _ref_by_path.get(path)
        if ref:
            return ref
        _ref_counter[0] += 1
        ref = "w%d" % _ref_counter[0]
        _ref_by_path[path] = ref
        _refs[ref] = widget
        return ref


def _resolve(ref):
    """Находит виджет по ref, tk-пути или по точному тексту."""
    if not ref:
        raise ValueError("не указан ref")
    ref = str(ref).strip()
    widget = _refs.get(ref)
    if widget is None and ("." == ref[:1]):
        widget = _safe(lambda: _app.nametowidget(ref))
    if widget is None:
        matches = [w for w in _walk(_app) if _text_of(w) == ref]
        if len(matches) == 1:
            widget = matches[0]
        elif len(matches) > 1:
            raise ValueError("несколько виджетов с текстом %r, уточни ref" % ref)
    if widget is None:
        raise LookupError("виджет не найден: %r" % ref)
    if not _safe(lambda: bool(widget.winfo_exists()), False):
        raise LookupError("STALE_REF: виджет %r уже уничтожен" % ref)
    return widget


def _walk(widget, out=None):
    out = [] if out is None else out
    out.append(widget)
    for child in _safe(lambda: list(widget.winfo_children()), []) or []:
        _walk(child, out)
    return out


def _text_of(widget):
    cls = _safe(lambda: widget.winfo_class(), "")
    if cls in ("Button", "TButton", "Label", "TLabel", "Checkbutton", "TCheckbutton",
               "Radiobutton", "TRadiobutton", "LabelFrame", "TLabelframe", "TLabel"):
        return _safe(lambda: str(widget.cget("text")), "") or ""
    if cls in ("Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Combobox"):
        return _safe(lambda: str(widget.get()), "") or ""
    if cls in ("Text", "ScrolledText"):
        return (_safe(lambda: str(widget.get("1.0", "end-1c")), "") or "")[:2000]
    if cls == "Canvas":
        texts = []
        for item in _safe(lambda: widget.find_all(), []) or []:
            if _safe(lambda: widget.type(item), "") == "text":
                value = _safe(lambda: str(widget.itemcget(item, "text")), "")
                if value:
                    texts.append(value)
        return " | ".join(texts)
    return ""


def _value_of(widget):
    variable = getattr(widget, "variable", None)
    if variable is not None and hasattr(variable, "get"):
        return _safe(lambda: variable.get())
    cls = _safe(lambda: widget.winfo_class(), "")
    if cls in ("Listbox",):
        return _safe(lambda: [widget.get(i) for i in widget.curselection()], [])
    return None


def _bounds(widget):
    ax = _safe(lambda: int(widget.winfo_rootx()), 0)
    ay = _safe(lambda: int(widget.winfo_rooty()), 0)
    width = _safe(lambda: int(widget.winfo_width()), 0)
    height = _safe(lambda: int(widget.winfo_height()), 0)
    root_x = _safe(lambda: int(_app.winfo_rootx()), 0)
    root_y = _safe(lambda: int(_app.winfo_rooty()), 0)
    return {"xy": [ax, ay], "size": [width, height], "rel": [ax - root_x, ay - root_y]}


def _states(widget):
    states = {}
    mapped = _safe(lambda: bool(widget.winfo_ismapped()))
    if mapped is not None:
        states["mapped"] = mapped
    viewable = _safe(lambda: bool(widget.winfo_viewable()))
    if viewable is not None:
        states["visible"] = viewable
    disabled = _safe(lambda: bool(widget.instate(["disabled"])))
    if disabled is None:
        disabled = _safe(lambda: str(widget.cget("state")) == "disabled")
    if disabled is None:
        disabled = getattr(widget, "state", None) == "disabled"
    if disabled is not None:
        states["disabled"] = bool(disabled)
    focus = _safe(lambda: str(widget.focus_get()) == str(widget), False)
    if focus:
        states["focused"] = True
    return states


def _info(widget):
    item = {"ref": _ref_for(widget), "cls": _safe(lambda: widget.winfo_class(), "?"),
            "path": _safe(lambda: str(widget), "")}
    text = _text_of(widget)
    if text:
        item["text"] = text[:400]
    value = _value_of(widget)
    if value not in (None, "", []):
        try:
            json.dumps(value)
            item["value"] = value
        except (TypeError, ValueError):
            item["value"] = str(value)
    state = _states(widget)
    if state:
        item["state"] = state
    item.update(_bounds(widget))
    return item


def _children(widget):
    return _safe(lambda: list(widget.winfo_children()), []) or []


def _is_interactive(widget):
    cls = _safe(lambda: widget.winfo_class(), "")
    if cls in ("Button", "TButton", "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton",
               "Entry", "TEntry", "Text", "TCombobox", "Combobox", "Listbox", "Scale", "TScale",
               "Spinbox", "TSpinbox", "Menubutton", "TMenubutton"):
        return True
    if cls == "Canvas":
        return bool(_safe(lambda: widget.bind("<Button-1>"), "") or _safe(lambda: widget.bind("<ButtonPress-1>"), ""))
    return hasattr(widget, "command") or hasattr(widget, "_command") or hasattr(widget, "variable")


# --- команды ----------------------------------------------------------------

def _cmd_ping(req):
    return {
        "pid": os.getpid(),
        "title": _safe(lambda: _app.title(), ""),
        "geometry": _safe(lambda: _app.winfo_geometry(), ""),
        "screen": _safe(lambda: [_app.winfo_screenwidth(), _app.winfo_screenheight()], []),
        "python": sys.version.split()[0],
        "widgets": len(_walk(_app)),
        "commands": sorted(_COMMANDS),
    }


def _cmd_tree(req):
    depth = int(req.get("depth") or 4)
    only = (req.get("only") or "all").lower()
    root = _resolve(req["root"]) if req.get("root") else _app

    def build(widget, level):
        node = _info(widget)
        if level < depth:
            kids = []
            for child in _children(widget):
                if only == "interactive" and not _is_interactive(child) and not _children(child):
                    continue
                if only == "text" and not _text_of(child) and not _children(child):
                    continue
                kids.append(build(child, level + 1))
            if kids:
                node["children"] = kids
        return node

    return build(root, 0)


def _cmd_find(req):
    query = str(req.get("query") or "").strip().lower()
    cls_filter = str(req.get("cls") or "").strip().lower()
    interactive_only = bool(req.get("interactive"))
    limit = int(req.get("limit") or 30)
    hits = []
    for widget in _walk(_app):
        if interactive_only and not _is_interactive(widget):
            continue
        cls = _safe(lambda: widget.winfo_class(), "").lower()
        if cls_filter and cls_filter not in cls:
            continue
        haystack = " ".join([cls, str(widget), _text_of(widget),
                             str(_value_of(widget) or "")]).lower()
        if query and query not in haystack:
            continue
        hits.append(_info(widget))
        if len(hits) >= limit:
            break
    return {"count": len(hits), "items": hits}


def _cmd_get(req):
    return _info(_resolve(req["ref"]))


def _cmd_invoke(req):
    widget = _resolve(req["ref"])
    for name in ("invoke", "_btn_invoke", "_click", "_toggle", "command"):
        action = getattr(widget, name, None)
        if callable(action):
            action()
            return {"invoked": name, "widget": _info(widget)}
    return _cmd_click(req)


def _cmd_click(req):
    widget = _resolve(req["ref"])
    x = max(1, _safe(lambda: int(widget.winfo_width()), 10) // 2)
    y = max(1, _safe(lambda: int(widget.winfo_height()), 10) // 2)
    widget.event_generate("<ButtonPress-1>", x=x, y=y)
    widget.event_generate("<ButtonRelease-1>", x=x, y=y)
    return {"clicked": x, "y": y, "widget": _info(widget)}


def _cmd_focus(req):
    widget = _resolve(req["ref"])
    widget.focus_force()
    return {"focused": _ref_for(widget)}


def _cmd_set(req):
    widget = _resolve(req["ref"])
    value = req.get("value", "")
    cls = _safe(lambda: widget.winfo_class(), "")
    if cls in ("Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Combobox"):
        widget.delete(0, "end")
        widget.insert(0, str(value))
    elif cls in ("Text", "ScrolledText"):
        widget.delete("1.0", "end")
        widget.insert("1.0", str(value))
    elif getattr(widget, "variable", None) is not None and hasattr(widget.variable, "set"):
        current = _safe(lambda: widget.variable.get())
        if isinstance(current, bool):
            widget.variable.set(str(value).strip().lower() in ("1", "true", "yes", "on", "да"))
        elif isinstance(current, int):
            widget.variable.set(int(float(value)))
        elif isinstance(current, float):
            widget.variable.set(float(value))
        else:
            widget.variable.set(str(value))
    else:
        raise ValueError("виджет %s (%s) не поддерживает set" % (req["ref"], cls))
    return _info(widget)


def _cmd_type(req):
    widget = _resolve(req["ref"])
    text = str(req.get("text", ""))
    cls = _safe(lambda: widget.winfo_class(), "")
    if cls in ("Entry", "TEntry", "Spinbox", "TSpinbox", "TCombobox", "Combobox"):
        widget.insert("insert", text)
    elif cls in ("Text", "ScrolledText"):
        widget.insert("insert", text)
    else:
        raise ValueError("виджет %s (%s) не принимает ввод" % (req["ref"], cls))
    return _info(widget)


def _cmd_key(req):
    widget = _resolve(req["ref"])
    keys = str(req.get("keys") or "Return")
    sequence = keys if keys.startswith("<") else "<%s>" % keys
    widget.focus_force()
    widget.event_generate(sequence)
    return {"sent": sequence, "widget": _info(widget)}


def _cmd_call(req):
    target = _app if req.get("target") in (None, "", "app") else _resolve(req.get("target"))
    method = req.get("method")
    if not method:
        raise ValueError("не указан method")
    args = req.get("args") or []
    kwargs = req.get("kwargs") or {}
    result = getattr(target, method)(*args, **kwargs)
    return {"result": _jsonable(result)}


def _cmd_attrs(req):
    limit = int(req.get("limit") or 200)
    items = {}
    for name, value in vars(_app).items():
        if name.startswith("__") or len(items) >= limit:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            items[name] = _jsonable(value)
        elif isinstance(value, tk.Variable):
            items[name] = {"variable": _safe(lambda v=value: _jsonable(v.get()))}
        elif isinstance(value, (list, tuple, dict)) and len(value) <= 40:
            items[name] = _jsonable(value)
        else:
            items[name] = _brief(value)
    return items


def _cmd_eval(req):
    expression = req.get("expr") or req.get("expression")
    if not expression:
        raise ValueError("не указан expr")
    namespace = _namespace()
    try:
        return {"result": _jsonable(eval(expression, namespace))}
    except SyntaxError:
        exec(expression, namespace)
        return {"executed": True}


def _cmd_toplevels(req):
    import tkinter as tk
    windows = []
    for widget in _walk(_app):
        if isinstance(widget, (tk.Toplevel, tk.Tk)):
            windows.append({
                "ref": _ref_for(widget),
                "title": _safe(lambda w=widget: w.title(), ""),
                "geometry": _safe(lambda w=widget: w.winfo_geometry(), ""),
                "visible": _safe(lambda w=widget: bool(w.winfo_viewable()), False),
                "widgets": len(_walk(widget)),
            })
    return {"count": len(windows), "windows": windows}


def _cmd_screenshot(req):
    """Снимок окна. mode=window (по умолчанию) снимает само окно даже под другими
    окнами, mode=screen — то, что реально видно на экране."""
    from PIL import Image
    target = _resolve(req["target"]) if req.get("target") and req["target"] != "app" else _app
    mode = (req.get("mode") or "window").lower()
    x = _safe(lambda: int(target.winfo_rootx()), 0)
    y = _safe(lambda: int(target.winfo_rooty()), 0)
    width = _safe(lambda: int(target.winfo_width()), 0)
    height = _safe(lambda: int(target.winfo_height()), 0)
    if width <= 1 or height <= 1:
        raise ValueError("окно не отрисовано: %sx%s" % (width, height))

    folder = os.path.join(tempfile.gettempdir(), "lam_ai_bridge")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "screen_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    method = "screen"

    if mode == "window":
        image = _grab_window(target, width, height)
        if image is not None:
            image.save(path)
            method = "printwindow"

    if method == "screen":
        from PIL import ImageGrab
        ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True).save(path)

    return {"path": path, "size": [width, height], "xy": [x, y], "method": method}


def _grab_window(target, width, height):
    """Снимок окна через PrintWindow: не зависит от перекрытия другими окнами."""
    try:
        import ctypes

        import win32gui
        import win32ui
        from PIL import Image

        hwnd = int(target.wm_frame(), 16)
        window_dc = win32gui.GetWindowDC(hwnd)
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)
        ctypes.windll.user32.PrintWindow(hwnd, memory_dc.GetSafeHdc(), 2)
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits,
                                 "raw", "BGRX", 0, 1)
        win32gui.DeleteObject(bitmap.GetHandle())
        memory_dc.DeleteDC()
        source_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)
        return image
    except Exception:
        return None


_COMMANDS = {
    "ping": _cmd_ping,
    "tree": _cmd_tree,
    "find": _cmd_find,
    "get": _cmd_get,
    "invoke": _cmd_invoke,
    "click": _cmd_click,
    "focus": _cmd_focus,
    "set": _cmd_set,
    "type": _cmd_type,
    "key": _cmd_key,
    "call": _cmd_call,
    "attrs": _cmd_attrs,
    "eval": _cmd_eval,
    "toplevels": _cmd_toplevels,
    "screenshot": _cmd_screenshot,
}


def _namespace():
    module = sys.modules.get(type(_app).__module__)
    namespace = dict(getattr(module, "__dict__", {}))
    namespace.update({"app": _app, "bridge": sys.modules.get(__name__)})
    return namespace


def _jsonable(value, depth=0):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:400]
    if depth > 2:
        return _brief(value)
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, depth + 1) for item in list(value)[:50]]
    if isinstance(value, dict):
        return {str(key): _jsonable(item, depth + 1) for key, item in list(value.items())[:50]}
    if isinstance(value, tk.Variable):
        return {"variable": _safe(lambda: _jsonable(value.get(), depth + 1))}
    return _brief(value)


def _brief(value, limit=160):
    """Короткое описание без вызова чужих методов (не блокирует интерфейс)."""
    text = _safe(lambda: repr(value), "")
    text = (text or "").replace("\n", " ")[:limit]
    return {"type": type(value).__name__, "repr": text}


# --- сервер -----------------------------------------------------------------

class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(900)
        while True:
            try:
                raw = self.rfile.readline()
            except (OSError, socket.timeout):
                return
            if not raw:
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except Exception as exc:
                self._reply({"ok": False, "error": "некорректный JSON: %s" % exc})
                continue
            self._reply(_serve(request))

    def _reply(self, payload):
        try:
            self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass


def _serve(request):
    token = request.get("token")
    expected = _TOKEN[0]
    if expected and token != expected:
        return {"ok": False, "error": "неверный токен доступа"}
    command = str(request.get("cmd") or "").strip()
    if command == "eval" and not _eval_enabled():
        return {"ok": False, "error": "команда eval отключена; задайте LAM_AI_BRIDGE_ALLOW_EVAL=1 только в доверенной среде"}
    handler = _COMMANDS.get(command)
    if handler is None:
        return {"ok": False, "error": "неизвестная команда: %r" % command,
                "commands": sorted(_COMMANDS)}
    if command == "ping":
        try:
            return {"ok": True, "data": _cmd_ping(request)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    try:
        return {"ok": True, "data": _run_on_ui(lambda: handler(request))}
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "traceback": traceback.format_exc(limit=3)}


_TOKEN = [None]


def _write_state(port):
    data = {"port": port, "token": _TOKEN[0], "pid": os.getpid(),
            "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, STATE_PATH)
    return data


def _clear_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("pid") == os.getpid():
            os.remove(STATE_PATH)
    except Exception:
        pass


def stop():
    global _server
    if _server is not None:
        _safe(_server.shutdown)
        _safe(_server.server_close)
        _server = None
    _clear_state()


def start(app, port=None):
    """Запускает мост. Возвращает словарь состояния или None, если выключен."""
    global _app, _server
    if not enabled():
        return None
    if _server is not None:
        return {"port": getattr(_server, "server_address", (None, None))[1], "already": True}
    _app = app
    _TOKEN[0] = secrets.token_hex(16)
    app.after(20, _pump)

    base = int(port or os.environ.get("LAM_AI_BRIDGE_PORT") or DEFAULT_PORT)
    server = None
    for candidate in range(base, base + 20):
        try:
            server = socketserver.ThreadingTCPServer(("127.0.0.1", candidate), _Handler)
            break
        except OSError:
            continue
    if server is None:
        return None
    server.daemon_threads = True
    _server = server
    threading.Thread(target=server.serve_forever, name="lam-ai-bridge", daemon=True).start()
    state = _write_state(server.server_address[1])

    atexit.register(stop)

    def _on_destroy(event):
        if event.widget is app:
            stop()

    app.bind("<Destroy>", _on_destroy, add="+")
    return state
