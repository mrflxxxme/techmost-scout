"""Запись прогона в БД: запросы, план поиска, кандидаты с оценками, карточки,
журнал стадий (Ф-8, РЕШ-40, РЕШ-83, РЕШ-84).

Минимально достаточный набор — пять таблиц из восьми существующих в
`app.models`: `requests`, `search_plans`, `candidates`, `solution_cards`,
`pipeline_logs`. Полная персистентность (брифы, вердикты, витрина) — задача
этапа 3.

Кандидаты с оценками добавлены сюда в Т-20 (РЕШ-84): оценки скоринга жили
только в строке журнала калибровки, то есть умирали вместе с прогоном, и
числовой критерий приёмки подэтапа (РЕШ-86) подтвердить было нечем. Считают по
этим строкам чистые функции `app.metrics`.

План поиска добавлен сюда же (РЕШ-83): пока он строится моделью заново каждый
раз, эффект правок поиска неотличим от разброса между прогонами. Обратная
операция — `load_search_plan`: прогон по сохранённому плану (`scout run
--plan-id N`) не зовёт планировщик вовсе.

Работа с базой живёт вне стадий (CLAUDE.md, `docs/team/CONVENTIONS.md`):
стадии остаются чистыми функциями, а запись вызывает только оркестратор через
переданную фабрику сессий — той же формы, что и `app.db.session_scope`.

Ценность прогона выше ценности строки в таблице: любой отказ записи (файла
базы нет, файл занят другим процессом) не должен ронять прогон — markdown-
отчёт первичен. Поэтому `persist_run` сама гасит исключения записи и кладёт
факт отказа в лог, а не пробрасывает его наружу.

Миграций в MVP нет (`init_db` — это `create_all`): новые колонки появляются
только на вновь созданном файле базы, существующий файл нужно пересоздать.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Candidate as CandidateRow
from app.models import PipelineLog as PipelineLogRow
from app.models import Request as RequestRow
from app.models import SearchPlan as SearchPlanRow
from app.models import SolutionCard as SolutionCardRow
from app.schemas import Candidate, SearchPlan, SolutionCard

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


@dataclass(slots=True)
class PlanRecord:
    """План одного прохода поиска — что именно спрашивали у источников (РЕШ-83).

    `pass_no` едет рядом с планом, а не выводится из порядка записей: второй
    проход (РЕШ-81) появится в Т-21, и до него в списке всегда ровно одна
    запись с единицей. Порядковый номер в списке молча превратил бы повтор
    первого прохода во «второй».
    """

    plan: SearchPlan
    pass_no: int = 1


@dataclass(slots=True)
class CandidateRecord:
    """Один кандидат прогона вместе с оценками и судьбой (РЕШ-84).

    `score` — готовый словарь `app.metrics.score_payload`, а не пять отдельных
    полей: формат оценок задаётся в одном месте вместе с признаком нейтральной
    оценки, и измеритель читает ровно то, что записал прогон.

    `status` — `selected` для тех, кто прошёл отсечку по `relevance` и попал в
    отбор top-N (`select_top`), `rejected` для остальных. Это **не** «дошёл до
    карточки»: `group_for_cards` режет отобранных дальше, до `target` групп, и
    примерно половина строк со статусом `selected` в карточки не попадает —
    считать по этому полю «сколько дошло до клиента» значит завысить ответ.
    Разделение при этом обязательно: без него по базе не отличить «кандидат был
    плох» от «кандидат не дошёл из-за лимита на один источник», а вся таблица
    превращается в список найденного.

    `pass_no` — из какого прохода поиска пришёл кандидат (РЕШ-81); до Т-21
    всегда единица.
    """

    candidate: Candidate
    score: dict
    status: str
    pass_no: int = 1


class SearchPlanNotFoundError(Exception):
    """Сохранённого плана нет, он не читается схемой или база недоступна.

    Отдельный тип, а не общий `Exception`: прогон по сохранённому плану обязан
    отказать понятно и ДО первого платного запроса, а не наполовину. Текст
    исключения печатается оператору как есть, поэтому он на русском и называет
    причину.
    """


async def load_search_plan(session_factory: SessionFactory | None, plan_id: int) -> SearchPlan:
    """Прочитать сохранённый план по номеру строки `search_plans`.

    В отличие от записи, чтение отказ НЕ гасит: запись вторична по отношению к
    отчёту (инвариант №5 в редакции РЕШ-43), а вот прогон по несуществующему
    плану продолжать нечем — источники опрашивать не по чему. Любая причина
    (нет базы, нет строки, битый JSON) превращается в `SearchPlanNotFoundError` с
    человекочитаемой причиной, и решение о коде возврата принимает CLI.
    """
    if session_factory is None:
        raise SearchPlanNotFoundError(
            f"база недоступна, прочитать план №{plan_id} нечем — прогон по сохранённому плану невозможен"
        )
    try:
        async with session_factory() as session:
            found = await session.execute(select(SearchPlanRow).where(SearchPlanRow.id == plan_id))
            row = found.scalars().one_or_none()
            stored = dict(row.queries or {}) if row is not None else None
    except Exception as exc:
        raise SearchPlanNotFoundError(f"план №{plan_id} не удалось прочитать из базы: {exc}") from exc

    if stored is None:
        raise SearchPlanNotFoundError(f"плана №{plan_id} нет в базе — проверьте номер в таблице search_plans")
    try:
        return SearchPlan.model_validate(stored)
    except ValidationError as exc:
        raise SearchPlanNotFoundError(f"план №{plan_id} в базе не читается схемой SearchPlan: {exc}") from exc


async def persist_run(
    session_factory: SessionFactory | None,
    *,
    raw_text: str,
    status: str,
    cost_rub: float,
    cards: list[SolutionCard],
    stages: list[StageRecord],
    plans: list[PlanRecord] | None = None,
    candidates: list[CandidateRecord] | None = None,
    error: str | None = None,
) -> int | None:
    """Записать прогон одной транзакцией: запрос, кандидаты, карточки, журнал стадий.

    Возвращает `requests.id` записанной строки — оркестратор ставит его в
    `Report.request_id`, и экспорт печатает «Запрос №N» вместо прочерка (Т-7).
    До этого номер прогона существовал только в БД, и связать выданный клиенту
    отчёт со строкой журнала было нечем.

    Фабрика не передана (тесты, веб до Э3) — запись не выполняется, прогон не
    меняется ни на шаг, номера нет: возвращается `None`. Любой отказ самой
    записи (открытия сессии, вставки) гасится и уходит в лог, наружу тоже
    уходит `None`: он не должен уничтожить уже готовый отчёт (инвариант №5 в
    редакции РЕШ-43 — отказ не роняет прогон, но и не выдаётся за успех).
    """
    if session_factory is None:
        return None
    try:
        async with session_factory() as session:
            return await _write(
                session,
                raw_text=raw_text,
                status=status,
                cost_rub=cost_rub,
                cards=cards,
                stages=stages,
                plans=plans or [],
                candidates=candidates or [],
                error=error,
            )
    except Exception:
        log.warning("запись прогона в БД не удалась — отчёт сохранён без неё", exc_info=True)
        return None


async def _write(
    session: AsyncSession,
    *,
    raw_text: str,
    status: str,
    cost_rub: float,
    cards: list[SolutionCard],
    stages: list[StageRecord],
    plans: list[PlanRecord],
    candidates: list[CandidateRecord],
    error: str | None,
) -> int:
    request_row = RequestRow(raw_text=raw_text, status=status, cost_rub=cost_rub, error=error)
    session.add(request_row)
    await session.flush()  # нужен id запроса для внешних ключей карточек и журнала
    # Снимаем номер сразу значением: после выхода из `session_scope` сессия
    # закрыта, и обращение к атрибуту ORM-объекта уже полезло бы в базу.
    request_id = int(request_row.id)

    # План поиска — в той же транзакции, что и строка запроса (РЕШ-83): план,
    # записанный отдельно от прогона, не с чем сопоставить.
    for record in plans:
        session.add(
            SearchPlanRow(
                request_id=request_id,
                pass_no=record.pass_no,
                queries=record.plan.model_dump(mode="json"),
            )
        )

    # Кандидаты с оценками — той же транзакцией (РЕШ-84). Пишутся все, кто дошёл
    # до скоринга, а не только отобранные: сравнивать прогоны между собой можно
    # лишь по полной выдаче, отобранные же и так видны в карточках.
    for record in candidates:
        session.add(
            CandidateRow(
                request_id=request_id,
                pass_no=record.pass_no,
                source_type=record.candidate.source_type.value,
                provider=record.candidate.provider,
                title=record.candidate.title,
                url=str(record.candidate.url),
                org=record.candidate.org,
                authors=list(record.candidate.authors),
                year=record.candidate.year,
                snippet=record.candidate.snippet,
                raw_id=record.candidate.raw_id,
                dedup_key=record.candidate.dedup_key(),
                score=dict(record.score),
                status=record.status,
            )
        )

    for position, card in enumerate(cards):
        session.add(
            SolutionCardRow(
                request_id=request_id,
                position=position,
                title=card.title,
                essence=card.essence,
                maturity_stage=card.maturity_stage.value,
                trl_estimate=card.trl_estimate,
                ip_status=card.ip_status.value,
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
                requirement_checks=[check.model_dump(mode="json") for check in card.requirement_checks],
                unsourced_claims=list(card.unsourced_claims),
                pdf_urls=[str(url) for url in card.pdf_urls],
            )
        )

    for record in stages:
        session.add(
            PipelineLogRow(
                request_id=request_id,
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

    return request_id
