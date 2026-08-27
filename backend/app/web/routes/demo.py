"""Демо-режим: список кейсов и запуск воспроизведения (SPEC Э4-3, РЕШ-196
п. 10–11, 14; РЕШ-200/РЕШ-201).

Показ проигрывает уже сохранённый журнал стадий (`pipeline_logs`) выбранного
запроса в тот же брокер прогресса `app.web.progress`, каким пользуется живой
прогон (`web/routes/run.py`): SSE-маршрут `GET /requests/{id}/progress` не
меняется и не знает, живой это прогон или демо-показ («Контракты» → «Формат
события демо»). Ответ старта — тот же фрагмент `_progress.html`, что и у
живого прогона: контейнер прогресса не различает источник событий.

Разметку демо-кейсов (`requests.demo_flag`) расставляет оркестратор на
воротах этапа одним `UPDATE` («Контракты» → «Разметка демо-кейсов», РЕШ-200)
— этот модуль только читает флаг из базы, номеров кейсов не знает.

Модуль не обращается ни к слою языковых моделей, ни к настоящему пайплайну
поиска (инвариант №6, РЕШ-196 п. 10) — показ не тратит ни рубля: вся динамика
построена из уже записанных времён стадий (`app.pipeline.demo.build_playback`).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Brief as BriefRow
from app.models import PipelineLog as PipelineLogRow
from app.models import Request as RequestRow
from app.pipeline import demo
from app.web import progress

log = logging.getLogger(__name__)
router = APIRouter()

#: Фоновые задачи показа — по `request_id`, тем же приёмом, что реестр
#: активных прогонов в `run.py` (РЕШ-147): повторный старт активного показа
#: не заводит вторую задачу, а отдаёт текущий прогресс.
_active_demos: dict[int, asyncio.Task] = {}

#: Новые клиентские фразы задачи — см. handoff для визы владельца.
REQUEST_NOT_FOUND = "Запрос №{request_id} не найден."
REFUSAL_NO_LOG = "Журнал прогона не сохранён — показать демо нечем."


def _templates(request: Request):
    """Шаблоны берём из приложения, чтобы не плодить второй экземпляр Jinja."""
    from app.main import templates

    return templates


def _notice(text: str, *, status_code: int) -> HTMLResponse:
    """Фрагмент-отказ: `.notice.danger` из `theme.css` (та же форма, что в `run.py`)."""
    return HTMLResponse(content=f'<div class="notice danger">{text}</div>', status_code=status_code)


def _progress_fragment(request: Request, request_id: int) -> HTMLResponse:
    """Тот же фрагмент прогресса, что у живого прогона: `_progress.html` не
    знает, живой это прогон или демо-показ (контейнер связан только с
    `request_id`, источник событий — брокер `app.web.progress`)."""
    return _templates(request).TemplateResponse(
        request=request,
        name="_progress.html",
        context={"request_id": request_id},
    )


def _forget_if_finished(request_id: int) -> None:
    """Страховка на случай гонки между самоочисткой задачи и повторным запросом."""
    task = _active_demos.get(request_id)
    if task is not None and task.done():
        _active_demos.pop(request_id, None)


async def _sleep(seconds: float) -> None:
    """Обёртка над `asyncio.sleep` — подменяется тестами тем же приёмом, что
    `run._KEEPALIVE_INTERVAL`: показ не должен ждать настоящих секунд
    (инвариант №7). `app.pipeline.demo.play` берёт эту функцию параметром, а
    не своим умолчанием `asyncio.sleep`, — умолчание уже связано с реальной
    функцией на момент импорта модуля и монкипатчу не поддаётся."""
    await asyncio.sleep(seconds)


@router.get("/demo", response_class=HTMLResponse)
async def demo_index(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Список запросов, размеченных под демо-показ (`demo_flag = true`)."""
    rows = (
        (await session.execute(select(RequestRow).where(RequestRow.demo_flag.is_(True)).order_by(RequestRow.id)))
        .scalars()
        .all()
    )
    request_ids = [row.id for row in rows]
    briefs: dict[int, BriefRow] = {}
    if request_ids:
        found = (await session.execute(select(BriefRow).where(BriefRow.request_id.in_(request_ids)))).scalars().all()
        briefs = {brief.request_id: brief for brief in found}

    cases = [(row, briefs.get(row.id)) for row in rows]
    return _templates(request).TemplateResponse(
        request=request,
        name="demo.html",
        context={"cases": cases},
    )


@router.post("/demo/{request_id}/start", response_class=HTMLResponse)
async def start_demo(request: Request, request_id: int, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Запустить фоновое воспроизведение журнала запроса — без пайплайна и без трат.

    Тот же контракт, что у `run.start_run` (РЕШ-147): тот же `request_id` при
    уже идущем показе не заводит вторую задачу, а отдаёт текущий прогресс тем
    же фрагментом, каким отвечает и старт показа. Пустой журнал (прогон не
    сохранил ни одной строки `pipeline_logs`) — честный отказ, а не падение
    страницы (инвариант №5).
    """
    row = await session.get(RequestRow, request_id)
    if row is None:
        return _notice(REQUEST_NOT_FOUND.format(request_id=request_id), status_code=404)

    _forget_if_finished(request_id)
    if request_id in _active_demos:
        return _progress_fragment(request, request_id)

    logs = (
        (
            await session.execute(
                select(PipelineLogRow)
                .where(PipelineLogRow.request_id == request_id)
                .order_by(PipelineLogRow.started_at)
            )
        )
        .scalars()
        .all()
    )
    if not logs:
        return _notice(REFUSAL_NO_LOG, status_code=409)

    steps = demo.build_playback(logs, total_seconds=get_settings().demo_duration_seconds)

    task = asyncio.create_task(demo.play(request_id, steps, broker=progress.get_broker(), sleep=_sleep))
    task.add_done_callback(lambda _task, rid=request_id: _active_demos.pop(rid, None))
    _active_demos[request_id] = task

    return _progress_fragment(request, request_id)
