"""Страж структуры после дробления `stages.py` (З-2, РЕШ-94) и `orchestrator.py`
(З-10, РЕШ-151).

Проверяет ровно то, что обещано критериями задач: файлы не длиннее лимита,
фасады держат прежнюю публичную поверхность и не прячут в себе ни одного
определения.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.pipeline import orchestrator, stages

_PIPELINE_DIR = Path(__file__).resolve().parents[1] / "backend" / "app" / "pipeline"

#: Модули, созданные или изменённые задачей дробления. `orchestrator.py`,
#: `persistence.py` и `constraints_guard.py` этой задачей не тронуты и под
#: лимит не подпадают (границы задачи, `.planning/PLAN.md`, раздел З-2).
_MODULES_UNDER_LIMIT = (
    "prompting.py",
    "intake.py",
    "planning.py",
    "extraction.py",
    "scoring.py",
    "selection.py",
    "cards.py",
    "summary.py",
)

_GENERAL_LIMIT = 500
_STAGES_LIMIT = 120

#: Модули, созданные или изменённые задачей З-10 при дроблении `orchestrator.py`.
_ORCH_MODULES_UNDER_LIMIT = (
    "pipeline_context.py",
    "pipeline_search.py",
    "pipeline_enrichment.py",
    "pipeline_selection_flow.py",
    "run_cards.py",
    "run_flow.py",
    "run_journal.py",
)

_ORCH_FACADE_LIMIT = 120

#: Минимальная публичная поверхность фасада `app.pipeline.orchestrator` — имена,
#: по которым существующий код и тесты подменяют или зовут части оркестратора
#: (`.planning/PLAN.md`, критерий 14 ворот Э2-Д).
_ORCH_REQUIRED_FACADE_NAMES = (
    "ScoutPipeline",
    "ProgressHook",
    "SearchOutcome",
    "CONTEST_CARDS_TARGET",
    "SECOND_PASS_NO",
    "split_contest_candidates",
    "candidate_records",
    "cost_delta",
    "format_active_run_params",
    "build_adapters",
    "persist_run",
    "PAID_LIMIT_NOTICE",
    "normalize_doi",
)

#: Минимальная публичная поверхность фасада — имена, по которым существующие
#: тесты и другие модули подменяют или зовут стадии (`.planning/PLAN.md`,
#: критерий 4 задачи З-2).
_REQUIRED_FACADE_NAMES = (
    "run_intake",
    "refine_brief",
    "brief_from_raw_text",
    "neutralize_placeholders",
    "plan_search",
    "replan_search",
    "needs_second_pass",
    "extract_candidates",
    "deduplicate",
    "score_candidates",
    "select_top",
    "group_for_cards",
    "author_card",
    "verify_card",
    "summarize",
    "get_settings",
)

_DEFINITION_LINE = re.compile(r"^(async def |def |class )", re.MULTILINE)


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_pipeline_modules_stay_within_line_limit():
    """Ни один из восьми новых модулей не длиннее 500 строк, `stages.py` — не длиннее 120."""
    over_limit = []
    for name in _MODULES_UNDER_LIMIT:
        path = _PIPELINE_DIR / name
        count = _line_count(path)
        if count > _GENERAL_LIMIT:
            over_limit.append(f"{name}: {count} строк")
    stages_count = _line_count(_PIPELINE_DIR / "stages.py")
    if stages_count > _STAGES_LIMIT:
        over_limit.append(f"stages.py: {stages_count} строк (лимит {_STAGES_LIMIT})")
    assert not over_limit, "превышен лимит строк: " + "; ".join(over_limit)


def test_facade_reexports_required_names():
    """Фасад `app.pipeline.stages` держит минимальный набор публичных имён,
    на которые опираются существующие подмены в тестах."""
    missing = [name for name in _REQUIRED_FACADE_NAMES if not hasattr(stages, name)]
    assert not missing, f"фасад не реэкспортирует: {missing}"


def test_stages_module_has_no_definitions():
    """В `stages.py` нет ни одного `def`, `async def` или `class` — только реэкспорт."""
    text = (_PIPELINE_DIR / "stages.py").read_text(encoding="utf-8")
    found = _DEFINITION_LINE.findall(text)
    assert not found, f"в фасаде найдены определения: {found}"


# ── Дробление `orchestrator.py` (З-10, РЕШ-151) ──────────────────────────────


def test_orchestrator_modules_stay_within_line_limit():
    """Ни один из модулей дробления `orchestrator.py` не длиннее 500 строк,
    сам фасад — не длиннее 120."""
    over_limit = []
    for name in _ORCH_MODULES_UNDER_LIMIT:
        path = _PIPELINE_DIR / name
        count = _line_count(path)
        if count > _GENERAL_LIMIT:
            over_limit.append(f"{name}: {count} строк")
    facade_count = _line_count(_PIPELINE_DIR / "orchestrator.py")
    if facade_count > _ORCH_FACADE_LIMIT:
        over_limit.append(f"orchestrator.py: {facade_count} строк (лимит {_ORCH_FACADE_LIMIT})")
    assert not over_limit, "превышен лимит строк: " + "; ".join(over_limit)


def test_orchestrator_facade_reexports_required_names():
    """Фасад `app.pipeline.orchestrator` держит минимальный набор публичных
    имён, на которые опираются существующий код и подмены в тестах."""
    missing = [name for name in _ORCH_REQUIRED_FACADE_NAMES if not hasattr(orchestrator, name)]
    assert not missing, f"фасад не реэкспортирует: {missing}"


def test_orchestrator_module_has_no_definitions():
    """В `orchestrator.py` нет ни одного `def`, `async def` или `class` —
    только реэкспорт."""
    text = (_PIPELINE_DIR / "orchestrator.py").read_text(encoding="utf-8")
    found = _DEFINITION_LINE.findall(text)
    assert not found, f"в фасаде найдены определения: {found}"
