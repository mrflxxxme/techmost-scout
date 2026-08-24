"""Запись прогона в БД: вставка новой строки `requests` либо обновление
существующей (задача И-5, долг DLG-3 грилла РЕШ-145/189).

Вынесено из `persistence.py`: тот файл держал вместе чтение (`load_search_plan`,
`load_report`, `load_history_pool`) и запись, и правка записи под обновление
строки по `request_id` перевалила лимит 500 строк (CLAUDE.md). Типы записей
журнала (`StageRecord`, `PlanRecord`, `CandidateRecord`) и `SessionFactory`
остались в `persistence.py` — этот модуль берёт их оттуда же, а не заводит
копии: два по-разному собранных `StageRecord` в разных модулях быстро
разошлись бы полями.

`persist_run` без `request_id` работает как раньше: одна вставка — одна новая
строка `requests` на вызов. С `request_id` (РЕШ-145) — обновляет существующую
строку вместо второй вставки: перезаписывает карточки, журнал стадий,
кандидатов и планы этого запроса, обновляет (не дублирует) строку `briefs` —
у неё `unique(request_id)`, и веб пишет бриф раньше прогона (задача И-9).
Номер, которому в базе не нашлось строки (опечатка, гонка с чисткой), не
роняет запись: `_write` откатывается к обычной вставке новой строки и
предупреждает в лог — тот же принцип устойчивости, что у отказа самой записи
(инвариант №5 в редакции РЕШ-43): молчаливая потеря прогона недопустима, но и
падать из-за неверного номера запись не должна, отчёт первичен.

Ценность прогона выше ценности строки в таблице: любой отказ записи (файла
базы нет, файл занят другим процессом) не должен ронять прогон — markdown-
отчёт первичен. Поэтому `persist_run` сама гасит исключения записи и кладёт
факт отказа в лог, а не пробрасывает его наружу.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Brief as BriefRow
from app.models import Candidate as CandidateRow
from app.models import PipelineLog as PipelineLogRow
from app.models import Request as RequestRow
from app.models import SearchPlan as SearchPlanRow
from app.models import SolutionCard as SolutionCardRow
from app.pipeline.persistence import CandidateRecord, PlanRecord, SessionFactory, StageRecord
from app.schemas import Brief, Report, SolutionCard

log = logging.getLogger(__name__)


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
    request_id: int | None = None,
) -> int | None:
    """Записать прогон одной транзакцией: запрос, кандидаты, карточки, журнал стадий.

    Возвращает `requests.id` записанной строки — оркестратор ставит его в
    `Report.request_id`, и экспорт печатает «Запрос №N» вместо прочерка (Т-7).

    `request_id` — номер уже существующей строки `requests` (РЕШ-145, долг
    DLG-3): если она есть, `persist_run` обновляет её (статус, стоимость,
    ошибку, `report_json`) вместо повторной вставки и **перезаписывает**
    карточки, журнал стадий, кандидатов и планы этого запроса — старые строки
    прошлой попытки удаляются, новые пишутся заново.

    Стоимость (`cost_rub`) на этой ветке — тоже **присваивание**, не
    сложение (РЕШ-194, вариант А ревью цикла 1 правки аудита Э3): перенос
    трат прошлых фаз веб-пути в счёт уже сделан на уровне `LLMRouter`
    (`initial_spent_rub=row.cost_rub`, РЕШ-193 п. 1) — значение, которое сюда
    приходит параметром `cost_rub`, уже кумулятивно за весь запрос. Сложение
    здесь удвоило бы перенесённую сумму — оба места решали бы одну и ту же
    задачу «не терять деньги предыдущих фаз» двумя разными механизмами.
    Единственный перенос — тот же приём, что уже работает в `intake.py`.

    Так повторный прогон того же запроса (веб перезапускает отказавший
    запрос той же строкой — задача И-9/И-10) не копит историю прошлых
    попыток рядом с текущей. Строка
    `briefs` обновляется, а не дублируется: у неё `unique(request_id)`, второй
    `INSERT` на тот же номер уронил бы транзакцию. Умолчание `None` — прежнее
    поведение: новая строка на каждый вызов, как до этой задачи. Номер, для
    которого строки в базе не нашлось, не считается отказом — `_write` пишет
    новую строку и предупреждает в лог: неверный номер не должен стоить
    прогона.

    `contest_cards` — карточки секции «Ранние разработки и команды» (РЕШ-85
    п. 1). Аргумент **обязательный**, без умолчания, по тому же уроку, что и
    журналы проходов (РЕШ-89): умолчание сделало бы потерю целой секции
    незаметной для тестов, а это ровно тот класс дефекта, ради которого
    заведена Т-33. В базе секции различаются колонкой `solution_cards.is_contest`.

    `brief` — структурированная постановка задачи (РЕШ-146/156): строка
    `briefs` пишется на КАЖДОМ прогоне, где бриф уже собран, — успешном и
    упавшем на любой стадии после интейка, — а не только на успешном.

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
                request_id=request_id,
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
    request_id: int | None = None,
) -> int:
    request_row = await session.get(RequestRow, request_id) if request_id is not None else None
    if request_id is not None and request_row is None:
        log.warning(
            "persist_run: запроса №%s нет в базе — записана новая строка, номер аргумента проигнорирован",
            request_id,
        )

    if request_row is not None:
        # Обновление существующей строки (РЕШ-145): один запрос — одна строка
        # `requests` за весь его жизненный цикл, вместо второй строки на
        # каждый повтор. `raw_text` не трогаем — он уже стоит с момента
        # создания строки (веб пишет бриф и запрос раньше прогона, И-9), и
        # переданный текст здесь ровно тот же.
        resolved_id = int(request_row.id)
        request_row.status = status
        # Присваивание, не сложение (РЕШ-194, вариант А ревью цикла 1):
        # `cost_rub` уже кумулятивен на входе этой функции — `LLMRouter`
        # каждой фазы веб-пути создаётся с `initial_spent_rub=row.cost_rub`
        # (РЕШ-193 п. 1), поэтому и `router.cost.spent_rub`, и `report.cost_rub`
        # на выходе — это сумма за ВЕСЬ запрос, а не дельта одной фазы. Сложение
        # здесь удваивало бы перенесённую сумму (11,46 → 11,64 на первом
        # прогоне, компаунд на повторных — двойной перенос уже переносится
        # роутером, `_write` его дублировать не должен). Перенос — один
        # механизм, тем же приёмом, что уже работает в `intake.py`
        # (`row.cost_rub = llm_router.cost.spent_rub`).
        request_row.cost_rub = cost_rub
        request_row.error = error
        if report is not None:
            request_row.report_json = report.model_copy(update={"request_id": resolved_id}).model_dump_json()
        # Перезаписываем карточки, журнал стадий, кандидатов и планы этого
        # запроса: без чистки повторный прогон того же request_id (отказ и
        # перезапуск, повтор из веба) копил бы строки прошлой попытки рядом с
        # новыми, и на одну строку requests пришлось бы несколько наборов
        # карточек — инвариант №1 требует, чтобы карточка в БД отражала
        # ровно последний прогон, а не архив всех попыток.
        await session.execute(delete(SolutionCardRow).where(SolutionCardRow.request_id == resolved_id))
        await session.execute(delete(PipelineLogRow).where(PipelineLogRow.request_id == resolved_id))
        await session.execute(delete(CandidateRow).where(CandidateRow.request_id == resolved_id))
        await session.execute(delete(SearchPlanRow).where(SearchPlanRow.request_id == resolved_id))
        if brief is not None:
            found = await session.execute(select(BriefRow).where(BriefRow.request_id == resolved_id))
            brief_row = found.scalars().one_or_none()
            if brief_row is None:
                session.add(_brief_row(resolved_id, brief))
            else:
                # Обновление, а не повторная вставка: у `briefs.request_id`
                # ограничение `unique`, второй `INSERT` уронил бы транзакцию.
                _apply_brief(brief_row, brief)
    else:
        request_row = RequestRow(raw_text=raw_text, status=status, cost_rub=cost_rub, error=error)
        session.add(request_row)
        await session.flush()  # нужен id запроса для внешних ключей карточек и журнала
        # Снимаем номер сразу значением: после выхода из `session_scope` сессия
        # закрыта, и обращение к атрибуту ORM-объекта уже полезло бы в базу.
        resolved_id = int(request_row.id)
        if report is not None:
            # Копия с исправленным request_id, а не мутация переданного объекта:
            # на момент этого вызова `report.request_id` у вызывающего кода ещё
            # нулевой (номер появляется только сейчас) — в базу должен попасть
            # отчёт с настоящим номером, а не с нулём (см. докстроку persist_run).
            request_row.report_json = report.model_copy(update={"request_id": resolved_id}).model_dump_json()
        if brief is not None:
            # Бриф пишется независимо от report: он существует уже к моменту
            # отказа любой стадии после интейка, а отчёт — только на успехе.
            session.add(_brief_row(resolved_id, brief))

    # План поиска — в той же транзакции, что и строка запроса (РЕШ-83): план,
    # записанный отдельно от прогона, не с чем сопоставить.
    for record in plans:
        session.add(
            SearchPlanRow(
                request_id=resolved_id,
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
                request_id=resolved_id,
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
        session.add(_card_row(resolved_id, position, card, is_contest=False))
    for position, card in enumerate(contest_cards):
        session.add(_card_row(resolved_id, position, card, is_contest=True))

    for record in stages:
        session.add(
            PipelineLogRow(
                request_id=resolved_id,
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

    return resolved_id


def _brief_row(request_id: int, brief: Brief) -> BriefRow:
    """Новая строка `briefs` из схемы `Brief` — тело общее для вставки и первого
    обновления (когда строки под этот `request_id` ещё не было)."""
    return BriefRow(
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


def _apply_brief(row: BriefRow, brief: Brief) -> None:
    """Переносит поля схемы `Brief` в уже существующую строку `briefs`
    (РЕШ-145): обновление на месте, а не повторная вставка — у колонки
    `request_id` ограничение `unique`."""
    row.industry = brief.industry
    row.task_summary = brief.task_summary
    row.constraints = list(brief.constraints)
    row.owner_answers = [item.model_dump(mode="json") for item in brief.owner_answers]
    row.target_maturity = brief.target_maturity.value if brief.target_maturity is not None else None
    row.geography = brief.geography
    row.budget_hint = brief.budget_hint
    row.timeline_hint = brief.timeline_hint
    row.keywords_ru = list(brief.keywords_ru)
    row.keywords_en = list(brief.keywords_en)


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
