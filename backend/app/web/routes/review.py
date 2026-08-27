"""Экран ревью эксперта (SPEC Э4-1, «Контракты» → «Вердикты», РЕШ-196–197).

`GET /requests/{id}/review` показывает **все** карточки запроса обеих секций
(включая уже отклонённые — эксперт видит их для контроля, в отличие от
клиентского вида, который их прячет). Поэтому здесь сознательно не
используется `export.view.build_export_view` — тот сборщик готовит вид ДЛЯ
КЛИЕНТА (фильтр reject, переиндексация позиций), а ревью работает с сырым
отчётом и исходными id строк `solution_cards`.

`POST /cards/{card_id}/verdict` пишет **новую** строку `verdicts` — ничего не
обновляется и не удаляется (РЕШ-196 п. 3). Когда после записи у каждой
строки `solution_cards` запроса есть хотя бы один вердикт
(`app.verdicts.all_cards_reviewed`), статус запроса становится `validated`;
`exported` этим не понижается.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.export.markdown import CONTEST_SECTION_HEADING
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.models import SolutionCard as SolutionCardRow
from app.models import Verdict as VerdictRow
from app.pipeline.persistence import ReportNotFoundError, load_report
from app.schemas import Verdict as VerdictDecision
from app.verdicts import CardKey, all_cards_reviewed, effective_verdicts

log = logging.getLogger(__name__)
router = APIRouter()

#: Новые клиентские фразы задачи — см. handoff для визы владельца.
REQUEST_NOT_FOUND = "Запрос №{request_id} не найден."
CARD_NOT_FOUND = "Карточка №{card_id} не найдена."
REFUSAL_EMPTY_NAME = "Укажите имя проверяющего — без имени вердикт не засчитывается."
REFUSAL_RATING_RANGE = "Оценка должна быть от 1 до 5."
REFUSAL_UNKNOWN_DECISION = "Неизвестное решение — выберите «Одобрить», «На доработку» или «Отклонить»."

#: Русские подписи решения — общие для формы и фрагмента с записанным вердиктом.
DECISION_LABELS: dict[VerdictDecision, str] = {
    VerdictDecision.APPROVE: "Одобрено",
    VerdictDecision.REWORK: "На доработку",
    VerdictDecision.REJECT: "Отклонено",
}


def _templates(request: Request):
    """Шаблоны берём из приложения, чтобы не плодить второй экземпляр Jinja."""
    from app.main import templates

    return templates


@asynccontextmanager
async def _session_scope(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Открытая сессия под форму `SessionFactory`, которую ждёт `load_report`."""
    yield session


def _notice(text: str, *, status_code: int) -> HTMLResponse:
    """Фрагмент-отказ: `.notice.danger` из `theme.css` (та же форма, что в `run.py`)."""
    return HTMLResponse(content=f'<div class="notice danger">{text}</div>', status_code=status_code)


async def _load_card_ids(session: AsyncSession, request_id: int) -> dict[CardKey, int]:
    """Id строк `solution_cards` запроса по ключу (секция, позиция) — без фильтра reject."""
    rows = (
        (await session.execute(select(SolutionCardRow).where(SolutionCardRow.request_id == request_id))).scalars().all()
    )
    return {(row.is_contest, row.position): row.id for row in rows}


async def _load_effective(session: AsyncSession, card_ids: dict[CardKey, int]) -> dict[int, VerdictRow]:
    """Действующие вердикты (последний по `created_at`, тай-брейк по `id`) по карточкам запроса."""
    if not card_ids:
        return {}
    found = await session.execute(select(VerdictRow).where(VerdictRow.card_id.in_(card_ids.values())))
    return effective_verdicts(found.scalars().all())


@router.get("/requests/{request_id}/review", response_class=HTMLResponse)
async def show_review(request_id: int, request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Экран ревью: карточки обеих секций в одну колонку, форма вердикта на каждую.

    В отличие от страницы отчёта, здесь показываются все карточки без фильтра
    reject — эксперт должен видеть отклонённые варианты, чтобы контролировать
    свои же прежние решения, а не только то, что уйдёт клиенту.
    """
    row = await session.get(RequestRow, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail=REQUEST_NOT_FOUND.format(request_id=request_id))

    try:
        report = await load_report(lambda: _session_scope(session), request_id)
    except ReportNotFoundError as exc:
        # Текст исключения — только в лог; на экране остаётся человеческая фраза.
        log.info("отчёт запроса №%s недоступен для ревью: %s", request_id, exc)
        return _templates(request).TemplateResponse(
            request=request,
            name="review.html",
            context={"row": row, "report": None},
            status_code=404,
        )

    card_ids = await _load_card_ids(session, request_id)
    effective = await _load_effective(session, card_ids)

    cards_with_ids = [(index + 1, card, card_ids.get((False, index))) for index, card in enumerate(report.cards)]
    contest_with_ids = [
        (index + 1, card, card_ids.get((True, index))) for index, card in enumerate(report.contest_cards)
    ]

    return _templates(request).TemplateResponse(
        request=request,
        name="review.html",
        context={
            "row": row,
            "report": report,
            "cards_with_ids": cards_with_ids,
            "contest_with_ids": contest_with_ids,
            "contest_heading": CONTEST_SECTION_HEADING.removeprefix("## "),
            "reviewer_default": get_settings().reviewer_name,
            "reviewed": len(effective),
            "total": len(card_ids),
        },
    )


@router.post("/cards/{card_id}/verdict", response_class=HTMLResponse)
async def submit_verdict(
    card_id: int,
    decision: str = Form(...),
    rating: int = Form(...),
    comment: str = Form(default=""),
    reviewer_name: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Записать новую строку `verdicts` и вернуть фрагмент со счётчиком «N из M».

    Отказы — пустое имя, оценка вне 1–5, неизвестное решение — не создают
    строку в `verdicts` (инвариант №2 держится на непустом имени: обходить
    его пустой строкой нельзя).
    """
    card_row = await session.get(SolutionCardRow, card_id)
    if card_row is None:
        return _notice(CARD_NOT_FOUND.format(card_id=card_id), status_code=404)

    name = reviewer_name.strip()
    if not name:
        return _notice(REFUSAL_EMPTY_NAME, status_code=400)

    if not 1 <= rating <= 5:
        return _notice(REFUSAL_RATING_RANGE, status_code=400)

    try:
        decision_value = VerdictDecision(decision)
    except ValueError:
        return _notice(REFUSAL_UNKNOWN_DECISION, status_code=400)

    comment_text = comment.strip()
    session.add(
        VerdictRow(
            card_id=card_id,
            reviewer_name=name,
            decision=decision_value.value,
            rating=rating,
            comment=comment_text,
        )
    )
    await session.flush()

    request_id = card_row.request_id
    card_ids = await _load_card_ids(session, request_id)
    effective = await _load_effective(session, card_ids)

    if all_cards_reviewed(card_ids, effective):
        request_row = await session.get(RequestRow, request_id)
        # `exported` не понижается до `validated` (РЕШ-196 п. 4).
        if request_row is not None and request_row.status != RequestStatus.EXPORTED:
            request_row.status = RequestStatus.VALIDATED

    label = DECISION_LABELS.get(decision_value, decision_value.value)
    comment_suffix = f" — {escape(comment_text)}" if comment_text else ""
    fragment = (
        '<div class="notice mb-sm">'
        f"Вердикт записан: {label}, оценка {rating}/5, проверяющий {escape(name)}{comment_suffix}. "
        f"Проверено {len(effective)} из {len(card_ids)}."
        "</div>"
    )
    return HTMLResponse(content=fragment, status_code=200)
