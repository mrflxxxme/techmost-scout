"""Оркестратор — фасад (РЕШ-151, дробление): реэкспорт из модулей рядом.

Реальный код живёт в `pipeline_context.py` (конструктор и уведомления),
`pipeline_search.py` (поиск, извлечение, второй проход), `pipeline_enrichment.py`
(обогащение по DOI), `pipeline_selection_flow.py` (деление на обычных и
конкурсных кандидатов, строки таблицы `candidates`), `run_cards.py` (сборка
карточек) и `run_flow.py` (сам класс `ScoutPipeline`, метод `run`). Здесь — ни
одного `def` и `class`, только реэкспорт: модуль остаётся точкой импорта,
которую не тронуло дробление.

Три имени — `build_adapters`, `persist_run`, `PAID_LIMIT_NOTICE` — реэкспортированы
не для красоты: подмодули читают их ОТЛОЖЕННЫМ импортом отсюда же
(`from app.pipeline.orchestrator import …` внутри вызываемой функции), а не
берут напрямую из `app.search`/`app.pipeline.persistence`/`app.export.markdown`.
Так тестовые подмены (`tests/test_cli_run.py`, `tests/test_openalex.py`,
`tests/test_partial_source_failure.py`), бьющие по атрибуту фасада, долетают до
мест, где эти имена реально используются, — тот же приём, каким коммит
3891b08 (З-2 Э2-Г) сохранил подмену `get_settings` при дроблении `stages.py`.
"""

from __future__ import annotations

from app.export.markdown import PAID_LIMIT_NOTICE
from app.pipeline.persistence import persist_run
from app.pipeline.pipeline_context import ProgressHook
from app.pipeline.pipeline_search import SearchOutcome
from app.pipeline.pipeline_selection_flow import (
    CONTEST_CARDS_TARGET,
    SECOND_PASS_NO,
    candidate_records,
    split_contest_candidates,
)
from app.pipeline.run_flow import ScoutPipeline
from app.pipeline.run_journal import cost_delta, format_active_run_params
from app.search import build_adapters, normalize_doi

__all__ = [
    "CONTEST_CARDS_TARGET",
    "PAID_LIMIT_NOTICE",
    "SECOND_PASS_NO",
    "ProgressHook",
    "ScoutPipeline",
    "SearchOutcome",
    "build_adapters",
    "candidate_records",
    "cost_delta",
    "format_active_run_params",
    "normalize_doi",
    "persist_run",
    "split_contest_candidates",
]
