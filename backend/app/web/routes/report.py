"""Отчёт с карточками и правкой оператора (SPEC Э3-5, задачи И-12, Э4-И2).

Страница строится из `export.view.build_export_view` — единственного
сборщика данных (РЕШ-196 п. 8): свой разбор `report_json`, своё наложение
`editor_overrides` или свой фильтр reject-карточек этот файл больше не ведёт.

Вердикт эксперта (`POST /cards/{card_id}/verdict`) переезжает в
`web/routes/review.py` отдельным экраном (Э4-И3) — здесь стаба больше нет
(РЕШ-143 п. 5, контракт волны 0 плана этапа 4).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.export.markdown import CONFIDENCE_RU, CONTEST_SECTION_HEADING, MATURITY_RU, limitations_lines
from app.export.view import build_export_view
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.pipeline.persistence import ReportNotFoundError
from app.verdicts import signature_line

log = logging.getLogger(__name__)
router = APIRouter()


def _templates(request: Request):
    """Шаблоны берём из приложения, чтобы не плодить второй экземпляр Jinja."""
    from app.main import templates

    return templates


@router.get("/requests/{request_id}/report", response_class=HTMLResponse)
async def show_report(request_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    """Отчёт: пометка «ЧЕРНОВИК» до валидации, подпись эксперта после неё.

    `is_draft` и подпись приходят из `ExportView` (`build_export_view`) — не
    из статуса запроса напрямую: единственное место, где решается, снята ли
    пометка, — слой вердиктов (`app.verdicts`, инвариант №2).
    """
    row = (await session.execute(select(RequestRow).where(RequestRow.id == request_id))).scalars().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"запрос №{request_id} не найден")

    try:
        view = await build_export_view(session, request_id)
    except ReportNotFoundError as exc:
        # Текст исключения — только в лог; клиенту уходит человеческая фраза
        # без внутренностей (замечание ревью цикла 2, РЕШ-143 п. 7).
        log.info("отчёт запроса №%s недоступен: %s", request_id, exc)
        return _templates(request).TemplateResponse(
            request=request,
            name="report.html",
            context={"row": row, "report": None},
            status_code=404,
        )

    report = view.report
    # Номер карточки передаётся явно, а не через `loop.index` шаблона: Jinja
    # не прокидывает `loop` во `{% include %}`, даже с контекстом по умолчанию.
    cards_with_ids = [
        (index + 1, card, view.card_ids.get((False, index)), (False, index) in view.rework_keys)
        for index, card in enumerate(report.cards)
    ]
    contest_with_ids = [
        (index + 1, card, view.card_ids.get((True, index)), (True, index) in view.rework_keys)
        for index, card in enumerate(report.contest_cards)
    ]
    signature = (
        signature_line(view.reviewer_name, view.validated_at)
        if not view.is_draft and view.validated_at is not None
        else None
    )

    return _templates(request).TemplateResponse(
        request=request,
        name="report.html",
        context={
            "row": row,
            "report": report,
            "is_draft": view.is_draft,
            "signature": signature,
            "cards_with_ids": cards_with_ids,
            "contest_with_ids": contest_with_ids,
            "contest_heading": CONTEST_SECTION_HEADING.removeprefix("## "),
            "limitation_lines": limitations_lines(report),
            "maturity_ru": MATURITY_RU,
            "confidence_ru": CONFIDENCE_RU,
        },
    )


@router.post("/cards/{card_id}/edit")
async def edit_card(
    card_id: int,
    title: str | None = Form(default=None),
    essence: str | None = Form(default=None),
    applicability: str | None = Form(default=None),
    next_step: str | None = Form(default=None),
    risks: str | None = Form(default=None),
    contact_leads: str | None = Form(default=None),
    developer_org: str | None = Form(default=None),
    developer_team: str | None = Form(default=None),
    evidence_removed: list[str] = Form(default_factory=list),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Правка текстовых полей карточки строго по белому списку (РЕШ-148).

    `confidence`, `requirement_checks`, `unsourced_claims` среди параметров
    обработчика нет вообще — этим маршрутом их не прислать даже случайно,
    лишние поля формы FastAPI молча отбрасывает; у обработчика нет и
    параметра для нового URL `evidence` — единственная операция над
    подтверждениями здесь — удаление уже показанных на странице. Что именно
    это гарантирует независимо от параметров обработчика (белый список полей,
    невозможность добавить `evidence`) — свойство самого наложения, оно
    держится в `app.report_overrides.apply_overrides`, не здесь.

    Правка разрешена и после `validated` (РЕШ-196 п. 6): `requests.status`
    здесь не трогается ни строкой — статус не откатывается и не сбрасывается.

    `apply_overrides` не проверяет типы значений (он только накладывает уже
    готовый словарь) — проверка входа формы здесь: значение, пришедшее не
    строкой, отбрасывается молча, а не роняет обработчик 500-й (замечание
    ревью И-7). `risks` и `contact_leads` в схеме — списки строк, а не текст:
    форма собирает их textarea по строке на пункт, здесь текст построчно
    разбирается в список до записи в `editor_overrides`.
    """
    card_row = await session.get(SolutionCardRow, card_id)
    if card_row is None:
        raise HTTPException(status_code=404, detail=f"карточка №{card_id} не найдена")

    updates: dict[str, object] = {}
    for field, value in (
        ("title", title),
        ("essence", essence),
        ("applicability", applicability),
        ("next_step", next_step),
        ("developer_org", developer_org),
        ("developer_team", developer_team),
    ):
        if isinstance(value, str):
            updates[field] = value
    for field, value in (("risks", risks), ("contact_leads", contact_leads)):
        if isinstance(value, str):
            updates[field] = [line.strip() for line in value.splitlines() if line.strip()]

    existing = dict(card_row.editor_overrides or {})
    #: Удаление по URL, а не по позиции строки: если в `evidence` карточки
    #: оказались две записи с одинаковым URL (дубль источника), одна отметка
    #: оператора уберёт обе — так уже ведёт себя `apply_overrides` (И-7), и
    #: здесь это поведение не меняется, а не «чинится» тихой доработкой.
    already_removed = {str(u) for u in existing.get("evidence_removed", []) if isinstance(u, str)}
    newly_removed = {u for u in evidence_removed if isinstance(u, str) and u}
    removed = already_removed | newly_removed
    if removed:
        updates["evidence_removed"] = sorted(removed)

    card_row.editor_overrides = {**existing, **updates}
    return RedirectResponse(url=f"/requests/{card_row.request_id}/report", status_code=303)
