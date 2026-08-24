"""Интейк и подтверждение брифа.

Перенесено из `web/routes.py` без изменения поведения (РЕШ-143 п. 5). Живой
поток уточняющих вопросов и подтверждения брифа — задача И-10 (SPEC Э3-2);
здесь пока только создание запроса, как было в исходном файле.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Request as RequestRow
from app.models import RequestStatus

log = logging.getLogger(__name__)
router = APIRouter()


def _templates(request: Request):
    """Шаблоны берём из приложения, чтобы не плодить второй экземпляр Jinja."""
    from app.main import templates

    return templates


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
