"""Запуск прогона и живой прогресс.

Перенесено из `web/routes.py` без изменения поведения (РЕШ-143 п. 5). Стаб
заготовки — задача И-11 (SPEC Э3-3, Э3-4) реализует настоящий поток.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/requests/{request_id}/progress")
async def progress_stream(request_id: int):
    """Живой прогресс поиска через Server-Sent Events.

    Этап 3, задача Э3-4. Контракт: text/event-stream, события вида
    {"stage": "search", "message": "Публикации: 14", "fraction": 0.4}.
    Источник событий — ProgressHook пайплайна. Это витринный экран демо,
    поэтому важна плавность: события не реже одного в 2 секунды.
    """
    raise NotImplementedError("этап 3, задача Э3-4 — см. docs/SPEC.md")
