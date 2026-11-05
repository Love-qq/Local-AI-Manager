"""Регрессия: отложенные лямбды не захватывают имена из `except`.

В Python 3 имя исключения (`except Exception as exc:`) удаляется при выходе из
блока. Лямбда, которую выполнят позже (через очередь UI, `after(...)`, поток),
падала с `NameError` вместо текста ошибки. Текст нужно вычислять до выхода из
`except`. Тест читает исходники через AST и не требует Tkinter.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE_FILES = sorted(
    [path for path in ROOT.glob("*.py") if not path.name.startswith("test_")]
    + list((ROOT / "lamcore").glob("*.py"))
)


def lambdas_capturing_exception_names(tree):
    """Список (строка, имя) для лямбд, замыкающих имя из `except`."""
    found = []

    class LambdaFinder(ast.NodeVisitor):
        def __init__(self, bound_name):
            self.bound_name = bound_name
            self.hits = []

        def visit_Lambda(self, node):
            names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
            if self.bound_name in names:
                self.hits.append(node.lineno)
            self.generic_visit(node)

        # Вложенные функции разбираются отдельно: они не часть тела handler'а.
        def visit_FunctionDef(self, node):
            pass

        def visit_AsyncFunctionDef(self, node):
            pass

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.name:
            continue
        finder = LambdaFinder(node.name)
        for statement in node.body:
            finder.visit(statement)
        for lineno in finder.hits:
            found.append((lineno, node.name))
    return found


def test_detector_catches_deferred_lambda_name_error():
    """Самопроверка: шаблон с ошибкой детектируется, исправленный — нет."""
    broken = ast.parse(
        "def worker(queue):\n"
        "    try:\n"
        "        raise RuntimeError('boom')\n"
        "    except Exception as exc:\n"
        "        queue.put(lambda: print(str(exc)))\n"
    )
    assert lambdas_capturing_exception_names(broken) == [(5, "exc")]

    fixed = ast.parse(
        "def worker(queue):\n"
        "    try:\n"
        "        raise RuntimeError('boom')\n"
        "    except Exception as exc:\n"
        "        error = str(exc)\n"
        "        queue.put(lambda: print(error))\n"
    )
    assert lambdas_capturing_exception_names(fixed) == []


def test_no_deferred_lambda_captures_exception():
    """Во всём проекте отложенные лямбды не замыкают имена из `except`."""
    offenders = {}
    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = lambdas_capturing_exception_names(tree)
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"лямбда замыкает имя из except (NameError): {offenders}"


def test_fixed_call_sites_keep_error_text():
    """Проверка, что исправленные места сохранили текст ошибки в сообщении."""
    source = (ROOT / "local_ai_manager.py").read_text(encoding="utf-8")
    assert "Не удалось прочитать характеристики: {error}" in source
    assert 'messagebox.showerror("Ошибка проверки GPU", error)' in source
    assert '{"User-Agent": "Local-AI-Manager"}' in source  # проверка API не задета
