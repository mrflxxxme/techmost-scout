"""Фоновый экспресс-скан: интейк без вопросов и урезанный прогон (Э6-6в, РЕШ-254, РЕШ-258).

Устроен по образцу `app.web.api_run`: разбор текста без уточняющего диалога,
дозаполнение ограничений, затем `ScoutPipeline` через общий
`web.routes.run._run_pipeline` под тем же замком `_active_runs` (РЕШ-147).
Отличия скана:

- настройки прогона — копия с жёстким стоп-краном `SCAN_COST_LIMIT_RUB` и тремя
  карточками (`target_cards`); процессные настройки не меняются;
- режим подачи всегда `business`, вид решения выбирает посетитель («Не знаю» —
  решает интейк, `resolve_solution_kind`);
- причина отказа на страницу не попадает: в `requests.error` пишется
  безопасная фраза, текст исключения остаётся в журнале сервера.

Сессию БД задача берёт через `app.db.session_scope` отложенным импортом — как
`_run_pipeline` (РЕШ-189 п. 10), чтобы подмена в тестах долетала до фона.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.export.markdown import CONSTRAINTS_UNRESOLVED_NOTICE
from app.llm import CostLimitExceededError, LLMRouter
from app.models import Candidate as CandidateRow
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.pipeline.intake import SOLUTION_KIND_UNKNOWN
from app.pipeline.intake_flow import resolve_constraints_stage, run_intake_stage
from app.pipeline.persistence import ReportNotFoundError
from app.schemas import MARKET_KINDS, Brief, Report
from app.web.brief_store import save_brief
from app.web.scan_view import ScanView, build_scan_view

if TYPE_CHECKING:  # только для аннотаций
    from app.pipeline.persistence import StageRecord

log = logging.getLogger(__name__)

#: Сколько основных карточек собирает пайплайн скана; в страницу идёт до трёх
#: сверенных (`scan_view.MAX_SCAN_CARDS`).
SCAN_TARGET_CARDS = 3
#: Подсказка отрасли: посетитель её не указывает.
_INDUSTRY_HINT = "не указана"
#: Что пишется в `requests.error`; текст исключения посетителю не показывается.
SCAN_ERROR_STOP = "Скан остановлен: достигнут предел расходов на один скан."
SCAN_ERROR_FAILED = "Скан не удался."
#: Токен — `secrets.token_urlsafe(SCAN_TOKEN_BYTES)`: 32 случайных байта в base64url без «=» дают ровно 43 знака
#: алфавита `A-Za-z0-9_-`. Всё остальное в адресе страницы токеном быть не может.
SCAN_TOKEN_BYTES = 32
_SCAN_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
#: Статусы скана, при которых результат собран и страница его показывает.
READY_STATUSES = frozenset({RequestStatus.DRAFTED, RequestStatus.VALIDATED, RequestStatus.EXPORTED})


def is_scan_token(token: str) -> bool:
    """Похожа ли строка из адреса на токен скана: проверка формата без обращения к базе.

    Токен идёт в заголовок `HX-Redirect`, поэтому управляющие знаки и переводы строк
    должны отсекаться до любого использования значения.
    """
    return _SCAN_TOKEN_RE.fullmatch(token) is not None


def scan_settings(base: Settings | None = None) -> Settings:
    """Копия настроек для скана: стоп-кран `SCAN_COST_LIMIT_RUB` и три карточки."""
    cfg = base or get_settings()
    return cfg.model_copy(
        update={"cost_limit_rub_per_request": cfg.scan_cost_limit_rub, "target_cards": SCAN_TARGET_CARDS}
    )


async def create_scan_request(task: str) -> tuple[int, str]:
    """Завести строку скана с неугадываемым токеном и закоммитить её до запуска фона.

    Возвращает номер запроса (наружу не отдаётся) и токен для адреса страницы.
    """
    from app.db import session_scope

    token = secrets.token_urlsafe(SCAN_TOKEN_BYTES)
    async with session_scope() as session:
        row = RequestRow(raw_text=task, status=RequestStatus.INTAKE, scan_token=token)
        session.add(row)
        await session.flush()
        return row.id, token


async def find_scan(session: AsyncSession, token: str) -> RequestRow | None:
    """Строка скана по токену; запрос без токена (не скан) этим путём не открывается."""
    if not token:
        return None
    return (await session.execute(select(RequestRow).where(RequestRow.scan_token == token))).scalar_one_or_none()


async def load_scan_view(session: AsyncSession, row: RequestRow) -> ScanView:
    """Вид готового скана: отчёт из `report_json` и тексты источников из `candidates`.

    Автосверка идёт по тексту, сохранённому при прогоне, без обращения к сети.
    Отчёта в строке нет — `ReportNotFoundError`, страница скажет об ошибке скана.
    """
    if not row.report_json:
        raise ReportNotFoundError(f"у скана №{row.id} нет собранного отчёта")
    report = Report.model_validate_json(row.report_json)
    rows = (await session.execute(select(CandidateRow).where(CandidateRow.request_id == row.id))).scalars().all()
    return build_scan_view(report, rows)


async def _mark_failed(request_id: int, error: str, *, cost_rub: float | None = None) -> None:
    """Пометить скан отказом: статус `failed`, безопасная причина, потраченное."""
    from app.db import session_scope

    async with session_scope() as session:
        row = await session.get(RequestRow, request_id)
        if row is None:
            return
        row.status = RequestStatus.FAILED
        row.error = error
        if cost_rub is not None:
            row.cost_rub = cost_rub


def _scan_brief(brief: Brief, kind: str) -> Brief:
    """Бриф скана: режим `business`; вид — выбор посетителя, «Не знаю» — решение интейка."""
    if kind == SOLUTION_KIND_UNKNOWN:
        resolved = brief.solution_kind if brief.solution_kind in MARKET_KINDS else "product"
    else:
        resolved = kind
    return brief.model_copy(update={"mode": "business", "solution_kind": resolved})


async def _prepare_brief(
    request_id: int, task: str, kind: str, cfg: Settings
) -> tuple[Brief, dict[str, object], float] | None:
    """Интейк без вопросов: бриф, аргументы пайплайна и уже потраченное.

    `None` — сработал стоп-кран скана, запрос уже помечен `failed`.
    """
    from app.db import session_scope

    router = LLMRouter(cfg)
    stage_log: list[StageRecord] = []
    try:
        outcome = await run_intake_stage(
            router, task, industry_hint=_INDUSTRY_HINT, session_factory=None, stage_log=stage_log, solution_kind=kind
        )
        brief = outcome.brief if outcome.intake is None else outcome.intake.draft_brief
        caveats = list(outcome.caveats)
        brief, unresolved = await resolve_constraints_stage(
            router, brief, task, industry_hint=_INDUSTRY_HINT, session_factory=None, stage_log=stage_log
        )
    except CostLimitExceededError:
        log.warning("скан №%s остановлен стоп-краном на интейке", request_id)
        await _mark_failed(request_id, SCAN_ERROR_STOP, cost_rub=router.cost.spent_rub)
        return None

    if unresolved:
        caveats.append(CONSTRAINTS_UNRESOLVED_NOTICE)
    brief = _scan_brief(brief, kind)

    spent = router.cost.spent_rub
    async with session_scope() as session:
        row = await session.get(RequestRow, request_id)
        row.status = RequestStatus.SEARCHING
        row.cost_rub = spent
        await save_brief(session, request_id, brief, confirmed=False)

    # `raw_text` в пайплайн не передаётся — как у веба и API: накопительный поиск
    # (РЕШ-163) не должен подтягивать пул чужого запроса с тем же текстом.
    kwargs: dict[str, object] = {
        "initial_stage_log": stage_log,
        "initial_caveats": caveats,
        "constraints_unresolved": unresolved,
    }
    return brief, kwargs, spent


async def run_scan(request_id: int, task: str, kind: str) -> None:
    """Тело фоновой задачи скана: интейк, пайплайн, снятие замка.

    Замок (`_active_runs`) снимается в `finally` при любом исходе. Отказ
    пайплайна `_run_pipeline` разбирает сам; здесь ловится отказ до него.
    """
    from app.web.routes import run as web_run

    cfg = scan_settings()
    try:
        prepared = await _prepare_brief(request_id, task, kind, cfg)
        if prepared is None:
            return
        brief, kwargs, spent = prepared
        await web_run._run_pipeline(request_id, brief, initial_cost_rub=spent, pipeline_kwargs=kwargs, settings=cfg)
    except Exception:
        log.exception("скан №%s завершился отказом до пайплайна", request_id)
        await _mark_failed(request_id, SCAN_ERROR_FAILED)
    finally:
        web_run._active_runs.pop(request_id, None)
