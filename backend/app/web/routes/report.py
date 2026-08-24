"""Отчёт с карточками и ревью эксперта.

Перенесено из `web/routes.py` без изменения поведения (РЕШ-143 п. 5). Стаб
заготовки страницы отчёта — задача И-12 (SPEC Э3-5); стаб вердикта эксперта —
этап 4, задача Э4-1, этой волны не касается.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


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
