"""Маршруты веб-интерфейса.

Готово: главная страница, история запросов, витрина. Полный интерактивный
проход (уточнения → живой прогресс → отчёт → ревью эксперта) — этап 3
спецификации, заготовки помечены и содержат контракт.

Соглашение: страницы отдаются как HTML через Jinja, обновления внутри страницы
приходят фрагментами HTML для HTMX. JSON наружу отдаём только там, где это
действительно нужно машине.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Request as RequestRow
from app.models import RequestStatus, ShowcaseItem

log = logging.getLogger(__name__)
router = APIRouter()


def _templates(request: Request):
    """Шаблоны берём из приложения, чтобы не плодить второй экземпляр Jinja."""
    from app.main import templates

    return templates


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session)):
    """Главная: постановка новой задачи и последние запросы."""

    recent = (await session.execute(select(RequestRow).order_by(desc(RequestRow.created_at)).limit(5))).scalars().all()
    return _templates(request).TemplateResponse(
        request=request,
        name="index.html",
        context={"recent": recent},
    )


@router.post("/requests", response_class=HTMLResponse)
async def create_request(
    request: Request,
    raw_text: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Создать запрос и перейти к уточняющим вопросам.

    Этап 3 спецификации. Контракт:
    1. Сохранить RequestRow(raw_text, status=RequestStatus.INTAKE).
    2. Вызвать stages.run_intake, отдать фрагмент с вопросами (шаблон
       _intake_questions.html) для HTMX-подстановки.
    3. Ответы принять в POST /requests/{id}/brief, собрать Brief через
       refine_brief, показать его на подтверждение.
    """
    row = RequestRow(raw_text=raw_text.strip(), status=RequestStatus.INTAKE)
    session.add(row)
    await session.flush()
    log.info("создан запрос №%s", row.id)
    return _templates(request).TemplateResponse(
        request=request,
        name="_stage_todo.html",
        context={
            "request_id": row.id,
            "stage": "Уточняющие вопросы",
            "spec_ref": "docs/SPEC.md → Э3-2",
        },
    )


@router.get("/requests", response_class=HTMLResponse)
async def list_requests(request: Request, session: AsyncSession = Depends(get_session)):
    """История запросов со статусами."""
    rows = (await session.execute(select(RequestRow).order_by(desc(RequestRow.created_at)).limit(100))).scalars().all()
    return _templates(request).TemplateResponse(
        request=request,
        name="history.html",
        context={"rows": rows},
    )


@router.get("/showcase", response_class=HTMLResponse)
async def showcase(request: Request, session: AsyncSession = Depends(get_session)):
    """Витрина разработок партнёров — приоритетный слой выдачи."""
    items = (
        (await session.execute(select(ShowcaseItem).order_by(desc(ShowcaseItem.created_at)).limit(200))).scalars().all()
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="showcase.html",
        context={"items": items},
    )


# ── Заготовки этапа 3–4 (контракты описаны в docs/SPEC.md) ──────────────────


@router.get("/requests/{request_id}/progress")
async def progress_stream(request_id: int):
    """Живой прогресс поиска через Server-Sent Events.

    Этап 3, задача Э3-4. Контракт: text/event-stream, события вида
    {"stage": "search", "message": "Публикации: 14", "fraction": 0.4}.
    Источник событий — ProgressHook пайплайна. Это витринный экран демо,
    поэтому важна плавность: события не реже одного в 2 секунды.
    """
    raise NotImplementedError("этап 3, задача Э3-4 — см. docs/SPEC.md")


@router.get("/requests/{request_id}/report", response_class=HTMLResponse)
async def show_report(request_id: int):
    """Отчёт с карточками в трёх режимах: просмотр, правка, ревью эксперта.

    Этап 3, задача Э3-5; режим ревью — этап 4, задача Э4-1.
    """
    raise NotImplementedError("этап 3, задача Э3-5 — см. docs/SPEC.md")


@router.post("/cards/{card_id}/verdict")
async def submit_verdict(card_id: int):
    """Вердикт эксперта: approve | reject | rework, оценка 1–5, комментарий.

    Этап 4, задача Э4-1. Пишет строку в verdicts; когда по всем карточкам
    запроса есть вердикт, статус запроса становится validated.
    """
    raise NotImplementedError("этап 4, задача Э4-1 — см. docs/SPEC.md")
