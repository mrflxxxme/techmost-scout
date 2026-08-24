"""Готовые страницы веб-интерфейса: главная, история, витрина.

Перенесено из `web/routes.py` без изменения поведения (РЕШ-143 п. 5).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Request as RequestRow
from app.models import ShowcaseItem

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
