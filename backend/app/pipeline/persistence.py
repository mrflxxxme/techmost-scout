"""Запись прогона в БД: запросы, брифы, план поиска, кандидаты с оценками,
карточки, журнал стадий (Ф-8, РЕШ-40, РЕШ-83, РЕШ-84, РЕШ-146/156).

Шесть таблиц из восьми существующих в `app.models`: `requests`, `briefs`,
`search_plans`, `candidates`, `solution_cards`, `pipeline_logs` — и сам отчёт
целиком, дампом в `requests.report_json`. Вердикты и витрина партнёров —
задача этапа 3–4, им запись здесь не нужна.

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

from app.models import Brief as BriefRow
from app.models import Candidate as CandidateRow
from app.models import PipelineLog as PipelineLogRow
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.models import SearchPlan as SearchPlanRow
from app.models import SolutionCard as SolutionCardRow
from app.pipeline.history import HistoryPool, HistoryRow, build_history_pool, normalize_task_text
from app.schemas import Brief, Candidate, Report, SearchPlan, SolutionCard

log = logging.getLogger(__name__)

#: Фабрика сессий: вызывается без аргументов, отдаёт асинхронный контекстный
#: менеджер с сессией внутри. По форме совпадает с `app.db.session_scope`,
#: которую и передаёт `scout run`; тесты подставляют свою на временной базе.
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(slots=True)
class StageRecord:
    """Один элемент журнала стадий — оркестратор собирает их по ходу прогона.

    Роутер моделей не считает стоимость по стадиям, только по запросу целиком,
    поэтому `provider`/`cost_rub` заполняются оркестратором приблизительно, по
    снимкам счётчика до и после вызова стадии; `sources_hit` — только для
    стадии поиска, из `SearchOutcome.hits_by_provider`.

    Полей `tokens_in`/`tokens_out` здесь больше нет (РЕШ-174): счётчик
    стоимости считает только рубли, токены никто не читал.

    `pass_no` — проход поиска, к которому относится стадия (РЕШ-81). Едет рядом
    со стадией по тому же доводу, что у планов (Т-19) и кандидатов (Т-20):
    второй проход повторяет стадии `search` и `scorer` под теми же именами, и
    без поля расход второго прохода (доклад по РЕШ-82) отделялся бы только
    порядком строк — упорядочить по времени и отсечь всё после `replan`.
    Умолчание — единица: однопроходных стадий большинство (интейк, карточки,
    проверка, сводка), и требовать от каждого места записи явной единицы значило
    бы завести источник расхождений на ровном месте.
    """

    stage: str
    started_at: datetime
    finished_at: datetime
    pass_no: int = 1
    provider: str | None = None
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


class ReportNotFoundError(Exception):
    """Отчёт запроса не нашёлся, не читается схемой `Report` или база недоступна.

    Отдельный тип, а не общий `Exception`, по тому же доводу, что у
    `SearchPlanNotFoundError`: экран отчёта (Э3-5) отказывает оператору понятно
    и один раз, а не наполовину отрисованной страницей. Текст исключения на
    русском и называет причину — печатается как есть.
    """


async def load_report(session_factory: SessionFactory | None, request_id: int) -> Report:
    """Прочитать отчёт запроса №`request_id` из `requests.report_json` (РЕШ-146/156).

    Обратная операция для записи `report_json` в `_write`: JSON, сложенный туда
    `Report.model_dump_json()`, читается назад той же схемой одним вызовом
    `Report.model_validate_json`, без собственного разбора полей — тем же
    приёмом, что и `load_search_plan` для `search_plans.queries`.

    Отказ здесь НЕ гасится (в отличие от `persist_run`): чтение — по прямому
    запросу экрана отчёта, продолжать без него нечем, а не необязательное
    усиление обычного прогона.
    """
    if session_factory is None:
        raise ReportNotFoundError(f"база недоступна, прочитать отчёт запроса №{request_id} нечем")
    try:
        async with session_factory() as session:
            found = await session.execute(select(RequestRow).where(RequestRow.id == request_id))
            row = found.scalars().one_or_none()
            stored = row.report_json if row is not None else None
    except Exception as exc:
        raise ReportNotFoundError(f"отчёт запроса №{request_id} не удалось прочитать из базы: {exc}") from exc

    if not stored:
        raise ReportNotFoundError(
            f"отчёта запроса №{request_id} в базе нет — report_json пуст (прогон не дошёл до сборки отчёта)"
        )
    try:
        return Report.model_validate_json(stored)
    except ValidationError as exc:
        raise ReportNotFoundError(f"отчёт запроса №{request_id} в базе не читается схемой Report: {exc}") from exc


class HistoryReadError(Exception):
    """История прошлых прогонов не прочиталась.

    Отдельный тип по образцу `SearchPlanNotFoundError`, но поведение
    противоположное: `--plan-id` — явная команда оператора, и её отказ обязан
    остановить прогон до первого платного вызова. Накопительный поиск —
    необязательное усиление обычного прогона (инвариант №5), поэтому это
    исключение никогда не покидает `load_history_pool` — вызывающий код в
    `run_flow.py` гасит его, кладёт причину в лог и в журнал стадий, и прогон
    продолжается без истории.
    """


async def load_history_pool(
    session_factory: SessionFactory | None,
    *,
    raw_text: str,
    explicit_request_id: int | None = None,
) -> HistoryPool:
    """Кандидаты прошлых прогонов ЭТОЙ ЖЕ задачи (РЕШ-163, РЕШ-166).

    Признак «та же задача» — оба пути решения Р-1: явный номер прогона важнее
    текста (оператор указал его руками, потому что задачу пересказали другими
    словами и автоматическое опознание не сработает), иначе — совпадение
    нормализованного текста среди прогонов, не закончившихся отказом
    (`status != RequestStatus.FAILED` — из упавшего прогона брать нечего, кроме случайных
    строк с середины). Без фабрики сессий и без единого совпадения возвращается
    пустой пул, а не исключение — это тот же обычный случай, что и «БД не
    настроена» у `persist_run`, а не отказ.

    Восстановление строк — чистая функция `app.pipeline.history.build_history_pool`,
    здесь только чтение и сборка плоских `HistoryRow`.
    """
    if session_factory is None:
        return HistoryPool()
    try:
        async with session_factory() as session:
            if explicit_request_id is not None:
                request_ids = [explicit_request_id]
            else:
                target = normalize_task_text(raw_text)
                found = await session.execute(
                    select(RequestRow.id, RequestRow.raw_text).where(RequestRow.status != RequestStatus.FAILED)
                )
                request_ids = [rid for rid, text in found.all() if normalize_task_text(text) == target]
            if not request_ids:
                return HistoryPool()
            rows = await session.execute(select(CandidateRow).where(CandidateRow.request_id.in_(request_ids)))
            history_rows = [
                HistoryRow(
                    source_type=row.source_type,
                    provider=row.provider,
                    title=row.title,
                    url=row.url,
                    org=row.org,
                    authors=list(row.authors or []),
                    year=row.year,
                    snippet=row.snippet,
                    raw_id=row.raw_id,
                    dedup_key=row.dedup_key,
                    score=dict(row.score or {}),
                    request_id=row.request_id,
                )
                for row in rows.scalars().all()
            ]
    except Exception as exc:
        raise HistoryReadError(f"история прошлых прогонов не прочиталась: {exc}") from exc
    return build_history_pool(history_rows)


async def persist_run(
    session_factory: SessionFactory | None,
    *,
    raw_text: str,
    status: str,
    cost_rub: float,
    cards: list[SolutionCard],
    contest_cards: list[SolutionCard],
    stages: list[StageRecord],
    plans: list[PlanRecord] | None = None,
    candidates: list[CandidateRecord] | None = None,
    error: str | None = None,
    brief: Brief | None = None,
    report: Report | None = None,
) -> int | None:
    """Записать прогон одной транзакцией: запрос, кандидаты, карточки, журнал стадий.

    Возвращает `requests.id` записанной строки — оркестратор ставит его в
    `Report.request_id`, и экспорт печатает «Запрос №N» вместо прочерка (Т-7).
    До этого номер прогона существовал только в БД, и связать выданный клиенту
    отчёт со строкой журнала было нечем.

    `contest_cards` — карточки секции «Ранние разработки и команды» (РЕШ-85
    п. 1). Аргумент **обязательный**, без умолчания, по тому же уроку, что и
    журналы проходов (РЕШ-89): умолчание сделало бы потерю целой секции
    незаметной для тестов, а это ровно тот класс дефекта, ради которого
    заведена Т-33. В базе секции различаются колонкой `solution_cards.is_contest`.

    `brief` — структурированная постановка задачи (РЕШ-146/156): строка
    `briefs` пишется на КАЖДОМ прогоне, где бриф уже собран, — успешном и
    упавшем на любой стадии после интейка, — а не только на успешном. Таблица
    была мертва (РЕШ-156), ничего в неё не писало ни разу; ответы оператора
    (`Brief.owner_answers`) пусты в авто-режиме, но строка всё равно есть.

    `report` — готовый отчёт (РЕШ-146): дампится в `requests.report_json`
    целиком через `Report.model_dump_json()`. Передаётся только на успешном
    прогоне (на отказе стадии отчёта ещё не существует). `report.request_id`
    на момент вызова ещё не исправлен на настоящий номер — `_write` кладёт в
    базу копию с уже подставленным `request_id`, а сам переданный объект не
    трогает: вызывающий код (`run_flow.ScoutPipeline.run`) сам присваивает
    `report.request_id` после этого вызова.

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
                contest_cards=contest_cards,
                stages=stages,
                plans=plans or [],
                candidates=candidates or [],
                error=error,
                brief=brief,
                report=report,
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
    contest_cards: list[SolutionCard],
    stages: list[StageRecord],
    plans: list[PlanRecord],
    candidates: list[CandidateRecord],
    error: str | None,
    brief: Brief | None = None,
    report: Report | None = None,
) -> int:
    request_row = RequestRow(raw_text=raw_text, status=status, cost_rub=cost_rub, error=error)
    session.add(request_row)
    await session.flush()  # нужен id запроса для внешних ключей карточек и журнала
    # Снимаем номер сразу значением: после выхода из `session_scope` сессия
    # закрыта, и обращение к атрибуту ORM-объекта уже полезло бы в базу.
    request_id = int(request_row.id)

    if report is not None:
        # Копия с исправленным request_id, а не мутация переданного объекта:
        # на момент этого вызова `report.request_id` у вызывающего кода ещё
        # нулевой (номер появляется только сейчас) — в базу должен попасть
        # отчёт с настоящим номером, а не с нулём (см. докстроку persist_run).
        request_row.report_json = report.model_copy(update={"request_id": request_id}).model_dump_json()

    if brief is not None:
        # Бриф пишется независимо от report: он существует уже к моменту
        # отказа любой стадии после интейка, а отчёт — только на успехе.
        session.add(
            BriefRow(
                request_id=request_id,
                industry=brief.industry,
                task_summary=brief.task_summary,
                constraints=list(brief.constraints),
                owner_answers=[item.model_dump(mode="json") for item in brief.owner_answers],
                target_maturity=brief.target_maturity.value if brief.target_maturity is not None else None,
                geography=brief.geography,
                budget_hint=brief.budget_hint,
                timeline_hint=brief.timeline_hint,
                keywords_ru=list(brief.keywords_ru),
                keywords_en=list(brief.keywords_en),
            )
        )

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

    # Обе секции отчёта ложатся в одну таблицу и различаются колонкой
    # `is_contest` (РЕШ-92, критерий 3). Нумерация позиций своя у каждой секции:
    # `position` повторяет порядковый номер карточки в отчёте, а конкурсные там
    # нумеруются заново, с единицы.
    for position, card in enumerate(cards):
        session.add(_card_row(request_id, position, card, is_contest=False))
    for position, card in enumerate(contest_cards):
        session.add(_card_row(request_id, position, card, is_contest=True))

    for record in stages:
        session.add(
            PipelineLogRow(
                request_id=request_id,
                pass_no=record.pass_no,
                stage=record.stage,
                started_at=record.started_at,
                finished_at=record.finished_at,
                provider=record.provider,
                cost_rub=record.cost_rub,
                sources_hit=dict(record.sources_hit),
                error=record.error,
            )
        )

    return request_id


def _card_row(request_id: int, position: int, card: SolutionCard, *, is_contest: bool) -> SolutionCardRow:
    """Строка `solution_cards` из карточки. Тело общее для обеих секций.

    Общее сознательно: отдельная сборка строки для конкурсной секции — тот
    самый путь, которым однажды разошлись бы состав полей отчёта и состав полей
    базы, и карточка в базе снова стала бы «увереннее» своей markdown-версии
    (урок Т-5). Секция задаётся одним флагом, а не своим набором полей.
    """
    return SolutionCardRow(
        request_id=request_id,
        position=position,
        is_contest=is_contest,
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
