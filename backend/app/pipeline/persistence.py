"""Запись прогона в БД: запросы, карточки, журнал стадий (Ф-8, РЕШ-40).

Минимально достаточный набор — три таблицы из семи существующих в
`app.models`: `requests`, `solution_cards`, `pipeline_logs`. Полная
персистентность (брифы, кандидаты, вердикты, витрина) — задача этапа 3.

Работа с базой живёт вне стадий (CLAUDE.md, `docs/team/CONVENTIONS.md`):
стадии остаются чистыми функциями, а запись вызывает только оркестратор через
переданную фабрику сессий — той же формы, что и `app.db.session_scope`.

Ценность прогона выше ценности строки в таблице: любой отказ записи (файла
базы нет, файл занят другим процессом) не должен ронять прогон — markdown-
отчёт первичен. Поэтому `persist_run` сама гасит исключения записи и кладёт
факт отказа в лог, а не пробрасывает его наружу.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PipelineLog as PipelineLogRow
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.schemas import SolutionCard

log = logging.getLogger(__name__)

#: Фабрика сессий: вызывается без аргументов, отдаёт асинхронный контекстный
#: менеджер с сессией внутри. По форме совпадает с `app.db.session_scope`,
#: которую и передаёт `scout run`; тесты подставляют свою на временной базе.
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(slots=True)
class StageRecord:
    """Один элемент журнала стадий — оркестратор собирает их по ходу прогона.

    Роутер моделей не считает стоимость и токены по стадиям, только по
    запросу целиком, поэтому `provider`/`tokens_in`/`tokens_out`/`cost_rub`
    заполняются оркестратором приблизительно, по снимкам счётчика до и после
    вызова стадии; `sources_hit` — только для стадии поиска, из
    `SearchOutcome.hits_by_provider`.
    """

    stage: str
    started_at: datetime
    finished_at: datetime
    provider: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_rub: float = 0.0
    sources_hit: dict[str, int] = field(default_factory=dict)
    error: str | None = None


async def persist_run(
    session_factory: SessionFactory | None,
    *,
    raw_text: str,
    status: str,
    cost_rub: float,
    cards: list[SolutionCard],
    stages: list[StageRecord],
    error: str | None = None,
) -> None:
    """Записать прогон одной транзакцией: запрос, карточки, журнал стадий.

    Фабрика не передана (тесты, веб до Э3) — запись не выполняется, прогон не
    меняется ни на шаг. Любой отказ самой записи (открытия сессии, вставки)
    гасится и уходит в лог: он не должен уничтожить уже готовый отчёт.
    """
    if session_factory is None:
        return
    try:
        async with session_factory() as session:
            await _write(
                session, raw_text=raw_text, status=status, cost_rub=cost_rub, cards=cards, stages=stages, error=error
            )
    except Exception:
        log.warning("запись прогона в БД не удалась — отчёт сохранён без неё", exc_info=True)


async def _write(
    session: AsyncSession,
    *,
    raw_text: str,
    status: str,
    cost_rub: float,
    cards: list[SolutionCard],
    stages: list[StageRecord],
    error: str | None,
) -> None:
    request_row = RequestRow(raw_text=raw_text, status=status, cost_rub=cost_rub, error=error)
    session.add(request_row)
    await session.flush()  # нужен id запроса для внешних ключей карточек и журнала

    for position, card in enumerate(cards):
        session.add(
            SolutionCardRow(
                request_id=request_row.id,
                position=position,
                title=card.title,
                essence=card.essence,
                maturity_stage=card.maturity_stage.value,
                trl_estimate=card.trl_estimate,
                developer_org=card.developer_org,
                developer_team=card.developer_team,
                contact_leads=list(card.contact_leads),
                evidence=[item.model_dump(mode="json") for item in card.evidence],
                applicability=card.applicability,
                risks=list(card.risks),
                gov_support=[item.model_dump(mode="json") for item in card.gov_support],
                next_step=card.next_step,
                confidence=card.confidence.value,
                confidence_note=card.confidence_note,
                candidate_urls=[str(url) for url in card.candidate_urls],
            )
        )

    for record in stages:
        session.add(
            PipelineLogRow(
                request_id=request_row.id,
                stage=record.stage,
                started_at=record.started_at,
                finished_at=record.finished_at,
                provider=record.provider,
                tokens_in=record.tokens_in,
                tokens_out=record.tokens_out,
                cost_rub=record.cost_rub,
                sources_hit=dict(record.sources_hit),
                error=record.error,
            )
        )
