"""Согласованность версии 48.1.4 во всех файлах проекта.

Тест не требует Tkinter: версия берётся из `lamcore.__version__`, а строки
User-Agent, `version_info.txt`, `installer.iss`, README и CHANGELOG читаются как
текст или разбираются через AST. Тест падает, если версия предыдущих
релизов (48.1.3, 48.1.2) вернётся в любой из этих файлов.
"""

import ast
import re
from pathlib import Path

import pytest

import lamcore
import lamcore.huggingface as hf

ROOT = Path(__file__).resolve().parent
EXPECTED = "48.1.4"
UA_PREFIX = "Local-AI-Manager/"
# Файлы, где не должно остаться прошлой версии. CHANGELOG хранит историю
# релизов и проверяется отдельно.
VERSION_FILES = [
    "lamcore/__init__.py",
    "local_ai_manager.py",
    "lamcore/engines.py",
    "lamcore/huggingface.py",
    "version_info.txt",
    "installer.iss",
    "README.md",
    "README_RU.md",
]


def _read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _evaluate_joined(node, values):
    """Собирает строку из Constant/FormattedValue(Name) без выполнения кода."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(str(part.value))
            elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
                name = part.value.id
                assert name in values, f"User-Agent собран из неизвестного имени {name}"
                parts.append(str(values[name]))
            else:
                pytest.fail("неожиданное выражение в строке User-Agent")
        return "".join(parts)
    pytest.fail(f"неожиданный узел в User-Agent: {ast.dump(node)}")


def _module_assignments(tree):
    return {node.targets[0].id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)}


def _imports_version_from_lamcore(tree):
    """`from lamcore import __version__` или `from . import __version__`."""
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module not in (None, "lamcore") or node.level not in (0, 1):
            continue
        if any(alias.name == "__version__" for alias in node.names):
            return True
    return False


def _string_structs(tree):
    """Значения StringStruct('Имя', 'значение') из version_info.txt."""
    values = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "StringStruct" and len(node.args) == 2:
            values[ast.literal_eval(node.args[0])] = ast.literal_eval(node.args[1])
    return values


def test_lamcore_version_is_expected():
    assert lamcore.__version__ == EXPECTED


def test_huggingface_user_agent_uses_package_version():
    assert hf.USER_AGENT == UA_PREFIX + lamcore.__version__
    assert hf.USER_AGENT == UA_PREFIX + EXPECTED


def test_app_user_agent_is_generated_from_package_version():
    tree = ast.parse(_read("local_ai_manager.py"), filename="local_ai_manager.py")
    assert _imports_version_from_lamcore(tree), \
        "APP_USER_AGENT должен брать версию из lamcore.__version__"
    assignments = _module_assignments(tree)
    assert "APP_USER_AGENT" in assignments, "APP_USER_AGENT не найден"
    value = _evaluate_joined(assignments["APP_USER_AGENT"], {"__version__": lamcore.__version__})
    assert value == UA_PREFIX + EXPECTED


def test_engines_user_agent_is_generated_from_package_version():
    tree = ast.parse(_read("lamcore/engines.py"), filename="lamcore/engines.py")
    assert _imports_version_from_lamcore(tree), \
        "User-Agent движков должен брать версию из lamcore.__version__"
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "User-Agent":
                found.append(_evaluate_joined(value, {"__version__": lamcore.__version__}))
    assert found, "в lamcore/engines.py нет заголовка User-Agent"
    assert all(item == UA_PREFIX + EXPECTED for item in found), found


def test_no_hardcoded_user_agent_version_in_sources():
    """User-Agent собирается из версии, а не из отдельной строки."""
    offenders = []
    for path in [ROOT / "local_ai_manager.py", *sorted((ROOT / "lamcore").glob("*.py"))]:
        text = path.read_text(encoding="utf-8")
        if re.search(re.escape(UA_PREFIX) + r"\d", text):
            offenders.append(path.name)
    assert offenders == [], f"жёстко зашитая версия в User-Agent: {offenders}"


def test_version_info_file():
    tree = ast.parse(_read("version_info.txt"), filename="version_info.txt")
    numbers = [int(part) for part in EXPECTED.split(".")] + [0]
    tuples = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "FixedFileInfo":
            for keyword in node.keywords:
                if keyword.arg in ("filevers", "prodvers"):
                    tuples[keyword.arg] = ast.literal_eval(keyword.value)
    assert tuples.get("filevers") == tuple(numbers), tuples
    assert tuples.get("prodvers") == tuple(numbers), tuples
    strings = _string_structs(tree)
    assert strings.get("FileVersion") == EXPECTED, strings
    assert strings.get("ProductVersion") == EXPECTED, strings


def test_installer_app_version():
    values = re.findall(r'^#define\s+AppVersion\s+"([^"]+)"', _read("installer.iss"),
                        flags=re.MULTILINE)
    assert values == [EXPECTED], values


def test_readme_versions():
    for name in ("README.md", "README_RU.md"):
        text = _read(name)
        assert EXPECTED in text, f"{name} не содержит версию {EXPECTED}"


def test_latest_changelog_entry_is_the_release():
    text = _read("CHANGELOG.md")
    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    assert headings, "в CHANGELOG нет ни одного раздела"
    first = headings[0]
    assert first.startswith(EXPECTED), f"последний раздел CHANGELOG: {first}"
    assert re.search(r"\(?\d{4}-\d{2}-\d{2}\)?", first), \
        f"у релиза {EXPECTED} нет даты: {first}"
    assert "Unreleased" not in text, "черновой раздел Unreleased не переименован"


def test_changelog_has_single_section_per_version():
    headings = re.findall(r"^## (.+)$", _read("CHANGELOG.md"), flags=re.MULTILINE)
    versions = [re.match(r"(\d+\.\d+\.\d+)", heading).group(1) for heading in headings]
    duplicates = {version for version in versions if versions.count(version) > 1}
    assert duplicates == set(), f"версия описана несколькими разделами: {duplicates}"


def test_no_previous_version_left_in_release_files():
    """Случайный возврат версии прошлого релиза должен ломать тест.

    48.1.3 уже опубликован: бинарники под этим номером существуют, поэтому
    версия не должна появиться ни в одном файле релиза (CHANGELOG хранит её
    как историю и проверяется отдельно).
    """
    previous = ("48.1.3", "48.1.2")
    offenders = {}
    for name in VERSION_FILES:
        text = _read(name)
        stale = [version for version in previous if version in text]
        if stale:
            offenders[name] = stale
    assert offenders == {}, f"в файлах осталась версия прошлого релиза: {offenders}"
    assert not any(version in lamcore.__version__ for version in previous)
