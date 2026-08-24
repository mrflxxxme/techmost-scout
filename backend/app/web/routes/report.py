"""Отчёт с карточками и правкой оператора (SPEC Э3-5, задача И-12).

Страница читает отчёт из `requests.report_json` через `persistence.load_report`
(РЕШ-189 п. 4) — не из `solution_cards`: таблица карточек используется здесь
только чтобы найти `id` строки под форму правки и достать уже сохранённые
`editor_overrides` (наложение — модуль `app.report_overrides`, задача И-7).
Отсутствие строк `solution_cards` не мешает открыть страницу — правок тогда
просто нет, что отдельно проверяет тест.

Вердикт эксперта (`POST /cards/{card_id}/verdict`) — стаб этапа 4, этот файл
его не трогает (РЕШ-143 п. 5).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.export.markdown import CONFIDENCE_RU, CONTEST_SECTION_HEADING, MATURITY_RU, limitations_lines
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.models import SolutionCard as SolutionCardRow
from app.pipeline.persistence import ReportNotFoundError, load_report
from app.report_overrides import OverrideMap, apply_overrides

router = APIRouter()

#: Ключ строки правок (И-7) — секция карточки и её позиция внутри секции.
_OverrideKey = tuple[bool, int]


def _templates(request: Request):
    """Шаблоны берём из приложения, чтобы не плодить второй экземпляр Jinja."""
    from app.main import templates

    return templates


@asynccontextmanager
async def _keep_session_open(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Оборачивает уже открытую сессию запроса под форму `load_report`.

    `load_report` принимает фабрику сессий той же формы, что `app.db.session_scope`
    (нулевая функция, отдающая асинхронный контекстный менеджер) — маршрут же
    получает сессию через `Depends(get_session)` один раз на запрос, и жизненным
    циклом этой сессии должен продолжать управлять FastAPI, а не эта обёртка.
    """
    yield session


async def _card_overrides_and_ids(
    session: AsyncSession, request_id: int
) -> tuple[OverrideMap, dict[_OverrideKey, int]]:
    """Правки и id строк `solution_cards` запроса по ключу (секция, позиция).

    Пустой результат — законный случай: отчёт лежит в `report_json`, а строк
    `solution_cards` может не быть (критерий приёмки И-12 проверяет это
    отдельно) — тогда правок нет и форма правки на странице не показывается.
    """
    rows = (
        (await session.execute(select(SolutionCardRow).where(SolutionCardRow.request_id == request_id))).scalars().all()
    )
    overrides: OverrideMap = {}
    ids: dict[_OverrideKey, int] = {}
    for row in rows:
        key = (row.is_contest, row.position)
        overrides[key] = dict(row.editor_overrides or {})
        ids[key] = row.id
    return overrides, ids


@router.get("/requests/{request_id}/report", response_class=HTMLResponse)
async def show_report(request_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    """Отчёт: пометка «ЧЕРНОВИК» до валидации, сводка, сравнение, карточки.

    До валидации экспертом (этап 4, задача Э4-1) статус запроса не бывает
    `validated`/`exported` в этой волне, поэтому пометка «черновик» видна
    всегда — сохранение правки её не снимает (РЕШ-189 п. 6): наложение
    `editor_overrides` не трогает `row.status`.
    """
    row = (await session.execute(select(RequestRow).where(RequestRow.id == request_id))).scalars().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"запрос №{request_id} не найден")

    try:
        raw_report = await load_report(lambda: _keep_session_open(session), request_id)
    except ReportNotFoundError as exc:
        return _templates(request).TemplateResponse(
            request=request,
            name="report.html",
            context={"row": row, "report": None, "error": str(exc)},
            status_code=404,
        )

    overrides, card_ids = await _card_overrides_and_ids(session, request_id)
    report = apply_overrides(raw_report, overrides)

    # Номер карточки передаётся явно, а не через `loop.index` шаблона: Jinja
    # не прокидывает `loop` во `{% include %}`, даже с контекстом по умолчанию.
    cards_with_ids = [(index + 1, card, card_ids.get((False, index))) for index, card in enumerate(report.cards)]
    contest_with_ids = [
        (index + 1, card, card_ids.get((True, index))) for index, card in enumerate(report.contest_cards)
    ]

    return _templates(request).TemplateResponse(
        request=request,
        name="report.html",
        context={
            "row": row,
            "report": report,
            "error": None,
            "is_draft": row.status not in (RequestStatus.VALIDATED, RequestStatus.EXPORTED),
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
    лишние поля формы FastAPI молча отбрасывает. Добавить новую строку
    `evidence` тем же путём нельзя: единственная операция над подтверждениями
    здесь — удаление уже показанных на странице URL.

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


@router.post("/cards/{card_id}/verdict")
async def submit_verdict(card_id: int):
    """Вердикт эксперта: approve | reject | rework, оценка 1–5, комментарий.

    Этап 4, задача Э4-1. Пишет строку в verdicts; когда по всем карточкам
    запроса есть вердикт, статус запроса становится validated.
    """
    raise NotImplementedError("этап 4, задача Э4-1 — см. docs/SPEC.md")
