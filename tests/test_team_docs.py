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


def _frontmatter(path: Path) -> dict[str, str]:
    """Разобрать шапку markdown-файла в словарь. Вложенности здесь не бывает."""
    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if match is None:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.startswith((" ", "-", "#")):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


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
    assert len(fields["description"]) > 40, f"роль {role}: описание слишком общее, оркестратор не выберет"


@pytest.mark.parametrize("role", sorted(CHECKING_ROLES))
def test_checking_roles_cannot_write(role: str):
    """Проверяющий не может править найденное — это инструментальный запрет.

    Половина ценности системы держится на одной строке в шапке роли: если
    ревьюер способен сам починить дефект, независимость проверки исчезает.
    """
    tools = _frontmatter(AGENTS_DIR / f"{role}.md")["tools"]
    granted = {tool.strip() for tool in tools.split(",")}
    assert "Write" not in granted, f"роль {role}: Write должен быть запрещён (§3 конвенций)"
    assert "Edit" not in granted, f"роль {role}: Edit должен быть запрещён (§3 конвенций)"


@pytest.mark.parametrize("role", sorted(EXPECTED_ROLES))
def test_role_declares_boundaries(role: str):
    """У роли есть разделы о запретах, выходе и самопроверке."""
    text = (AGENTS_DIR / f"{role}.md").read_text(encoding="utf-8")
    for section in ("Чего НЕ делаешь", "Выход", "Handoff", "самопроверки"):
        assert section in text, f"роль {role}: нет раздела «{section}»"


# ── Команды ─────────────────────────────────────────────────────────────────
def test_all_commands_present():
    found = {path.stem for path in COMMANDS_DIR.glob("*.md")}
    assert found == EXPECTED_COMMANDS


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_command_has_description(command: str):
    fields = _frontmatter(COMMANDS_DIR / f"{command}.md")
    assert fields.get("description"), f"команда {command}: нет описания"


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


def test_settings_json_is_valid():
    """Разрешения и хуки читаются, секреты закрыты от чтения."""
    path = REPO_ROOT / ".claude" / "settings.json"
    assert path.exists(), "нет .claude/settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    deny = json.dumps(data.get("permissions", {}).get("deny", []), ensure_ascii=False)
    assert ".env" in deny, "чтение .env должно быть запрещено явно"
