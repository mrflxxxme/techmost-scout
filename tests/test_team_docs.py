"""Проверки процессных документов как контракта.

Урок, взятый из проекта, откуда перенесена методика: формат, который ничем не
проверяется, дрейфует за несколько фаз. Там шаблон отчёта аудита разошёлся с
реальными отчётами по шести секциям, а два файла состояния за пять фаз остались
пустыми — просто потому, что никто этого не проверял.

Поэтому набор конвенций проверяется автоматически: определения ролей обязаны
иметь корректную шапку, команды — описание, ссылки между документами — вести на
существующие файлы, а инварианты — иметь способ проверки у каждого пункта.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"
TEAM_DIR = REPO_ROOT / "docs" / "team"

EXPECTED_ROLES = {"interviewer", "planner", "implementer", "reviewer", "verifier", "auditor"}
EXPECTED_COMMANDS = {"team-start", "team-grill", "team-plan", "team-build", "team-gate", "team-status"}

#: Роли, которые физически не должны уметь править то, что проверяют (§3 конвенций).
CHECKING_ROLES = {"reviewer", "verifier", "auditor"}

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
#: Ссылки вида [текст](путь) — без внешних адресов и якорей
MD_LINK = re.compile(r"\[[^\]]+\]\((?!https?://|#)([^)\s]+)\)")


def _frontmatter(path: Path) -> dict[str, object]:
    """Разобрать шапку markdown-файла настоящим YAML-парсером.

    Именно настоящим, а не разбором по двоеточию: наивный разбор пропускает
    невалидный YAML вида `description: Шаг 0: вызывать...` — двоеточие внутри
    незакавыченного значения ломает шапку, но при разборе по первому двоеточию
    выглядит нормально. Эта грабля в проекте уже случалась дважды: в справочнике
    господдержки и в шапках команд.
    """
    import yaml

    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError(f"{path.name}: нет шапки между --- ---")
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise AssertionError(f"{path.name}: шапка не разбирается как YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise AssertionError(f"{path.name}: шапка не является набором полей")
    return data


# ── Роли ────────────────────────────────────────────────────────────────────
def test_all_roles_present():
    """Шесть ролей из конвенций существуют, лишних нет."""
    found = {path.stem for path in AGENTS_DIR.glob("*.md")}
    assert found == EXPECTED_ROLES


@pytest.mark.parametrize("role", sorted(EXPECTED_ROLES))
def test_role_frontmatter(role: str):
    """У роли есть имя, описание, список инструментов и модель.

    Описание — не украшение: по нему оркестратор выбирает, кого вызвать.
    """
    fields = _frontmatter(AGENTS_DIR / f"{role}.md")
    for key in ("name", "description", "tools", "model"):
        assert fields.get(key), f"роль {role}: не заполнено поле {key}"
    assert fields["name"] == role, f"роль {role}: имя в шапке не совпадает с именем файла"
    assert fields["model"] in {"opus", "sonnet", "haiku"}, f"роль {role}: неизвестная модель"
    assert len(str(fields["description"])) > 40, f"роль {role}: описание слишком общее, оркестратор не выберет"


@pytest.mark.parametrize("role", sorted(CHECKING_ROLES))
def test_checking_roles_cannot_write(role: str):
    """Проверяющий не может править найденное — это инструментальный запрет.

    Половина ценности системы держится на одной строке в шапке роли: если
    ревьюер способен сам починить дефект, независимость проверки исчезает.
    """
    tools = str(_frontmatter(AGENTS_DIR / f"{role}.md")["tools"])
    granted = {tool.strip() for tool in tools.split(",")}
    assert "Write" not in granted, f"роль {role}: Write должен быть запрещён (§3 конвенций)"
    assert "Edit" not in granted, f"роль {role}: Edit должен быть запрещён (§3 конвенций)"


@pytest.mark.parametrize("role", sorted(EXPECTED_ROLES))
def test_role_declares_boundaries(role: str):
    """У роли есть разделы о запретах, выходе и самопроверке."""
    text = (AGENTS_DIR / f"{role}.md").read_text(encoding="utf-8")
    for section in ("Чего НЕ делаешь", "Выход", "Handoff", "самопроверки"):
        assert section in text, f"роль {role}: нет раздела «{section}»"


def test_planner_checklist_covers_measurable_event_reachability():
    """Чек-лист самопроверки планировщика содержит пункт о достижимости порога.

    Урок Э1-2: порог «числа по контурам веб-поиска» был запланирован при
    промпте, который вообще не производил веб-запросов, — событие, на которое
    рассчитывал план, не могло произойти по контрактам кода. Пункт 9 чек-листа
    закрывает именно это: планировщик обязан свериться, что тип запроса
    обслуживается рабочим адаптером и поле доходит до печати, прежде чем ставить
    порог, требующий живого замера. Ищем содержательные слова по корню
    («достижим», «измеря», «событ»), а не точную фразу целиком: безобидная
    правка формулировки (порядок слов, падеж, синоним рядом) не должна тихо
    убить проверку, но пункт, который перестал говорить о достижимости
    измеряемого события, обязан провалить тест.
    """
    text = (AGENTS_DIR / "planner.md").read_text(encoding="utf-8")
    match = re.search(r"## Чек-лист самопроверки\n(.*)\Z", text, re.DOTALL)
    assert match, "planner.md: нет раздела «Чек-лист самопроверки»"
    checklist = match.group(1)
    # Пункты чек-листа — многострочные абзацы, начинающиеся с "N. " в начале строки.
    items = re.split(r"^\d+\.\s+", checklist, flags=re.MULTILINE)[1:]
    assert items, "planner.md: чек-лист самопроверки пуст"

    def has_stems(item: str) -> bool:
        return bool(
            re.search(r"достижим", item, re.IGNORECASE)
            and re.search(r"измеря", item, re.IGNORECASE)
            and re.search(r"событ", item, re.IGNORECASE)
        )

    assert any(has_stems(item) for item in items), (
        "planner.md: чек-лист самопроверки не содержит пункта о достижимости "
        "измеряемого события (см. урок Э1-2 — порог, чьё событие код не мог "
        "произвести)"
    )


# ── Команды ─────────────────────────────────────────────────────────────────
def test_all_commands_present():
    found = {path.stem for path in COMMANDS_DIR.glob("*.md")}
    assert found == EXPECTED_COMMANDS


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_command_frontmatter(command: str):
    """У команды есть описание и подсказка аргумента, шапка — валидный YAML."""
    fields = _frontmatter(COMMANDS_DIR / f"{command}.md")
    assert fields.get("description"), f"команда {command}: нет описания"
    if command != "team-status":  # единственная команда без аргумента
        assert fields.get("argument-hint"), f"команда {command}: нет подсказки аргумента"


# ── Ссылки и состояние ──────────────────────────────────────────────────────
DOCS_WITH_LINKS = [
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "README.md",
    TEAM_DIR / "CONVENTIONS.md",
    TEAM_DIR / "INVARIANTS.md",
]


@pytest.mark.parametrize("doc", DOCS_WITH_LINKS, ids=lambda p: p.name)
def test_internal_links_resolve(doc: Path):
    """Ссылка на несуществующий файл — обычная причина, по которой агент
    уходит не туда и додумывает содержание."""
    broken = [
        target
        for target in MD_LINK.findall(doc.read_text(encoding="utf-8"))
        if not (doc.parent / target.split("#")[0]).resolve().exists()
    ]
    assert not broken, f"{doc.name}: битые ссылки {broken}"


def test_state_files_exist_and_are_filled():
    """Файлы состояния существуют и не пустые.

    Пустой файл состояния хуже отсутствующего: он создаёт видимость учёта.
    """
    for name in ("STATE.md", "JOURNAL.md", "DECISIONS.md", "PLAN.md"):
        path = REPO_ROOT / ".planning" / name
        assert path.exists(), f"нет файла состояния {name}"
        assert len(path.read_text(encoding="utf-8").strip()) > 200, f"{name}: файл почти пуст"


def test_state_stays_short():
    """STATE.md — снимок на две страницы, а не архив: иначе его перестают читать."""
    size = (REPO_ROOT / ".planning" / "STATE.md").stat().st_size
    assert size < 4096, f"STATE.md разросся до {size} байт — вынесите историю в JOURNAL.md"


def test_invariants_have_verification_column():
    """У каждого инварианта заполнен способ проверки.

    Инвариант без операционализации — пожелание: проверить его никто не сможет,
    и он тихо перестанет соблюдаться.
    """
    lines = (TEAM_DIR / "INVARIANTS.md").read_text(encoding="utf-8").splitlines()
    rows = [line for line in lines if line.startswith("| ") and re.match(r"^\|\s*\d+\s*\|", line)]
    assert len(rows) >= 5, "инвариантов меньше пяти — список выглядит недоработанным"
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert len(cells) >= 4, f"строка инварианта без всех колонок: {row[:60]}"
        assert len(cells[-1]) > 15, f"инвариант без способа проверки: {cells[1][:60]}"


#: Ссылка на файл теста внутри колонки «как проверять», например tests/test_x.py
TEST_FILE_REF = re.compile(r"tests/[\w./-]+\.py")


@pytest.mark.parametrize("invariant_number", [1, 5])
def test_invariant_verification_files_exist(invariant_number: int):
    """Файл теста, названный как способ проверки инварианта, существует на диске.

    Урок Э1-2: нарушение (факт без источника в карточке, скрытый отказ
    источника) прошло все гейты именно потому, что способ проверки в таблице
    был декларацией, а не работающей ссылкой — реального теста, который ловит
    именно это нарушение, либо не было, либо он проверял не то поле. Пункты 1
    и 5 таблицы INVARIANTS.md теперь называют конкретные файлы тестов; если
    файл переименуют или удалят, а строку таблицы не поправят, инвариант снова
    станет пожеланием на бумаге. Строка разбирается прицельно — по началу
    "| N |" у конкретной строки, а не поиском по всему тексту документа, чтобы
    не перепутать номер пункта со случайным числом в других местах файла.
    """
    lines = (TEAM_DIR / "INVARIANTS.md").read_text(encoding="utf-8").splitlines()
    prefix = f"| {invariant_number} |"
    rows = [line for line in lines if line.strip().startswith(prefix)]
    assert len(rows) == 1, f"строка инварианта №{invariant_number} не найдена однозначно: {rows}"
    cells = [cell.strip() for cell in rows[0].strip("|").split("|")]
    verification_column = cells[-1]
    refs = TEST_FILE_REF.findall(verification_column)
    assert refs, f"инвариант №{invariant_number}: в колонке «как проверять» не назван ни один файл теста"
    for ref in refs:
        path = REPO_ROOT / ref
        assert path.exists(), f"инвариант №{invariant_number}: ссылка на несуществующий файл теста {ref}"


def test_no_byte_order_mark():
    """Ни в одном текстовом файле нет метки порядка байтов.

    PowerShell 5.1 при записи в utf8 добавляет BOM, а три байта перед первым
    символом делают шапку `---` невидимой для разбора: файл выглядит правильным,
    а парсер сообщает «нет шапки». Ровно так один раз и сломались пять ролей.
    Убрать разом: `python scripts/strip_bom.py --write`.
    """
    skip_dirs = {".git", ".venv", "node_modules", "out", "__pycache__", ".ruff_cache", ".pytest_cache"}
    offenders: list[str] = []
    for pattern in ("*.md", "*.json", "*.yaml", "*.yml", "*.py"):
        for path in REPO_ROOT.rglob(pattern):
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.read_bytes().startswith(b"\xef\xbb\xbf"):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, f"файлы с BOM: {sorted(offenders)}"


def test_settings_json_is_valid():
    """Разрешения и хуки читаются, секреты закрыты от чтения."""
    path = REPO_ROOT / ".claude" / "settings.json"
    assert path.exists(), "нет .claude/settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    deny = json.dumps(data.get("permissions", {}).get("deny", []), ensure_ascii=False)
    assert ".env" in deny, "чтение .env должно быть запрещено явно"
