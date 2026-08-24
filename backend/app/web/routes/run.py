"""Запуск прогона в фоне, замок, живой прогресс, страница-хаб запроса
(SPEC Э3-3, Э3-4; РЕШ-147, РЕШ-183, РЕШ-188, РЕШ-189 п. 9–12).

`POST /requests/{id}/start` берёт подтверждённый бриф (`brief_store`, И-9),
ставит статус `searching` и запускает `ScoutPipeline.run` фоновой задачей
`asyncio.create_task`. Реестр задач `_active_runs` — простой словарь в памяти
процесса, без очереди задач и без брокера сообщений (SPEC Э3-3: на масштабе
«десятки запросов в месяц» это лишние движущиеся части); он же держит
системный замок (РЕШ-147): активный прогон один на систему, повторный
запуск того же запроса
не заводит второй прогон, а отдаёт текущий прогресс, запуск другого запроса
при активном отказывает человеческим текстом. Перезапуск процесса реестр не
переживает — запрос со статусом `searching` без задачи в реестре получает на
хабе и в списке запросов плашку «прогон прерван перезапуском» (это ожидаемо,
SPEC Э3-3, а не дефект).

Фоновая задача берёт сессию БД через `app.db.session_scope` отложенным
импортом внутри функции (РЕШ-189 п. 10, тот же приём, что `cli_run.py`
использует для `session_scope`/`init_db`): так тесты подменяют
`app.db.session_scope` и подмена долетает до задачи, запущенной уже после
подмены, а не только до кода, вызванного напрямую из теста.

`GET /requests/{id}/progress` отдаёт `text/event-stream` из брокера
`app.web.progress` (И-2): тело события — JSON `{"stage", "message",
"fraction"}`, повтор последнего события при молчании источника помечается
`"keepalive": true` (по совпадению ссылки на объект события — брокер отдаёт
один и тот же `ProgressEvent` дважды, ничего не зная о HTTP-слое). В
демо-режиме (`settings.demo_mode_default`, FR-16, РЕШ-188) сумма стоимости
из финального сообщения `done` вырезается на этом же слое — сериализации в
поток, — чтобы демонстрационный источник события не имел значения: и
подмена в тесте, и настоящий пайплайн проходят один и тот же фильтр.

Известный долг (не наш файл, не правим): `app.web.progress.ProgressBroker`
не освобождает `_channels[request_id]` после события `done` — буфер и список
подписчиков запроса остаются в памяти процесса до его перезапуска. На
масштабе «десятки запросов в месяц» это не течёт ощутимо, но при появлении
публичного метода очистки в `progress.py` (задача И-13 или отдельный долг)
этот маршрут обязан его вызвать после `done`.

Замок атомарен намеренно (ревью цикла 1, находка 1): проверка «активный
прогон уже есть» и его регистрация в `_active_runs` разнесены двумя await'ами
(`brief_store.load_brief`, `session.flush()`) — без общего примитива
атомарности два одновременных `POST /start` (двойной клик по кнопке) успевали
пройти проверку раньше, чем первый регистрировал свою задачу, и запускали
пайплайн дважды. `_start_lock` держит всю критическую секцию «проверить и
занять» одним куском: пока один запрос ждёт `load_brief`/`flush`, второй
блокируется на входе в `async with` и видит уже зарегистрированную задачу
первого, а не пустой реестр.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.llm import LLMRouter
from app.models import Brief as BriefRow
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import Brief
from app.web import brief_store, progress

log = logging.getLogger(__name__)
router = APIRouter()

#: Реестр фоновых задач прогона — весь замок держится на этом словаре
#: (РЕШ-147): непустой реестр значит «в системе уже идёт прогон».
_active_runs: dict[int, asyncio.Task] = {}

#: Атомарность «проверить замок и занять его» в `start_run` (ревью цикла 1,
#: находка 1) — без него между проверкой и регистрацией задачи лежат два
#: await'а, и двойной клик по кнопке запускает пайплайн дважды.
_start_lock = asyncio.Lock()

#: Не реже раза в 2 секунды (SPEC Э3-4). Вынесено константой модуля, а не
#: литералом внутри маршрута, — тем же приёмом, что и параметр
#: `keepalive_interval` у `ProgressBroker.subscribe` (И-2): тест подменяет имя
#: и не ждёт реальных секунд.
_KEEPALIVE_INTERVAL = 2.0

#: Новые клиентские фразы этой задачи (см. handoff для визы владельца).
REQUEST_NOT_FOUND = "Запрос №{request_id} не найден."
REFUSAL_BRIEF_NOT_CONFIRMED = (
    "Бриф ещё не подтверждён. Вернитесь к шагу подтверждения и нажмите «Подтвердить и продолжить»."
)
REFUSAL_ANOTHER_RUN_ACTIVE = (
    "Сейчас выполняется другой запрос (№{other_id}). Дождитесь его завершения и запустите этот запрос заново."
)
#: П-3 аудита Э3: повторный старт по запросу, у которого отчёт уже собран
#: (`drafted`/`validated`/`exported`), раньше заводил второй платный прогон и
#: перезаписывал отчёт тем же `request_id`. Новая клиентская фраза — на визу
#: владельца (см. handoff).
REFUSAL_ALREADY_DONE = 'Отчёт по этому запросу уже готов. <a href="/requests/{request_id}/report">Открыть отчёт</a>.'
#: Статусы, для которых `start_run` отказывает `REFUSAL_ALREADY_DONE`: отчёт
#: уже существует и повторный старт перезаписал бы его. `failed` и
#: `searching` без зарегистрированной задачи (прогон прерван перезапуском
#: процесса) сюда сознательно не входят — их можно перезапускать («Запустить
#: заново»).
_ALREADY_DONE_STATUSES = frozenset({RequestStatus.DRAFTED, RequestStatus.VALIDATED, RequestStatus.EXPORTED})

#: Хвост финального сообщения пайплайна («Отчёт готов. Стоимость запроса: N
#: ₽») — в демо-режиме вырезается, текст «Отчёт готов» остаётся (РЕШ-188).
_COST_SUFFIX_RE = re.compile(r"\.\s*Стоимость.*$")


def _templates(request: Request):
    """Шаблоны берём из приложения, чтобы не плодить второй экземпляр Jinja."""
    from app.main import templates

    return templates


def _without_cost(message: str) -> str:
    """Убирает сумму из сообщения `done` для демо-показа (РЕШ-188)."""
    return _COST_SUFFIX_RE.sub(".", message)


def _forget_finished_runs() -> None:
    """Чистит реестр от задач, уже снявших себя (страховка на случай гонки
    между самоочисткой в `finally` фоновой задачи и повторным запросом)."""
    finished = [rid for rid, task in _active_runs.items() if task.done()]
    for rid in finished:
        _active_runs.pop(rid, None)


def is_run_active(request_id: int) -> bool:
    """Есть ли в реестре живая фоновая задача этого запроса.

    Читают хаб (плашка «прогон прерван перезапуском») и список запросов
    (`web/routes/pages.py`) — оба должны видеть один и тот же реестр.
    """
    task = _active_runs.get(request_id)
    return task is not None and not task.done()


def _notice(text: str, *, status_code: int) -> HTMLResponse:
    """Короткий фрагмент-отказ: `.notice.danger` из `theme.css`, без своего
    шаблона — текст фиксированный, собирать его через Jinja незачем."""
    return HTMLResponse(content=f'<div class="notice danger">{text}</div>', status_code=status_code)


def _progress_fragment(request: Request, request_id: int) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request=request,
        name="_progress.html",
        context={"request_id": request_id},
    )


async def _run_pipeline(request_id: int, brief: Brief, *, initial_cost_rub: float = 0.0) -> None:
    """Тело фоновой задачи: пайплайн, публикация прогресса, снятие замка.

    Замок снимается в `finally` при любом исходе — успехе, отказе пайплайна
    (`ScoutPipeline.run` сам пишет статус `failed` и пробрасывает исключение
    дальше, РЕШ-40/РЕШ-145) и пустой выдаче (`_empty_report` не шлёт событие
    `done` — это долг пайплайна, не этой задачи, поэтому здесь и стоит
    страховка: без неё открытая вкладка прогресса зависла бы до перезапуска
    процесса).

    `initial_cost_rub` — сколько уже потрачено по этой строке `requests` до
    старта прогона (интейк, сбор брифа — РЕШ-193 п. 1, П-1 аудита Э3):
    роутер этой фазы стартует с этой суммой, а не с нуля, иначе стоп-кран
    `COST_LIMIT_RUB_PER_REQUEST` действовал бы отдельно на каждую фазу
    веб-пути вместо запроса целиком.
    """
    from app.db import session_scope

    broker_hook = progress.hook_for(request_id)
    state = {"done": False}

    def hook(stage: str, message: str, fraction: float) -> None:
        if stage == "done":
            state["done"] = True
        broker_hook(stage, message, fraction)

    router = LLMRouter(get_settings(), initial_spent_rub=initial_cost_rub)
    pipeline = ScoutPipeline(session_factory=session_scope, progress=hook, router=router)
    failed = False
    try:
        await pipeline.run(brief, request_id=request_id)
    except Exception:
        failed = True
        log.exception("прогон запроса №%s завершился отказом пайплайна", request_id)
    finally:
        if not state["done"]:
            # Пайплайн не прислал ни одного события (отказ до первого вызова
            # хука) — живой лог обязан честно показать, что это был отказ, а
            # не тихое завершение (ревью цикла 1, совет 2).
            message = "Прогон завершился отказом." if failed else "Прогон завершён."
            hook("done", message, 1.0)
        _active_runs.pop(request_id, None)


@router.post("/requests/{request_id}/start", response_class=HTMLResponse)
async def start_run(
    request: Request,
    request_id: int,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Запустить фоновый прогон подтверждённого брифа (SPEC Э3-3).

    Контракт: без подтверждённого брифа — отказ человеческим текстом; запрос,
    у которого отчёт уже собран (`drafted`/`validated`/`exported`), — отказ
    со ссылкой на готовый отчёт, второй платный прогон не заводится (П-3
    аудита Э3); тот же запрос при уже идущей своей задаче — фрагмент
    текущего прогресса без второго прогона; другой запрос при активном
    где-либо прогоне — отказ (РЕШ-147, системный замок один на процесс).
    `failed` и `searching` без зарегистрированной задачи (прогон прерван
    перезапуском процесса) перезапускать можно — кнопка «Запустить заново».
    Проверка замка и его регистрация — атомарны через `_start_lock` (ревью
    цикла 1, находка 1): без этого два одновременных запроса (двойной клик)
    успевали пройти проверку раньше, чем первый регистрировал свою задачу.
    """
    row = await session.get(RequestRow, request_id)
    if row is None:
        return _notice(REQUEST_NOT_FOUND.format(request_id=request_id), status_code=404)

    if row.status in _ALREADY_DONE_STATUSES:
        return _notice(REFUSAL_ALREADY_DONE.format(request_id=request_id), status_code=409)

    confirmed_at = (
        await session.execute(select(BriefRow.confirmed_at).where(BriefRow.request_id == request_id))
    ).scalar_one_or_none()
    if confirmed_at is None:
        return _notice(REFUSAL_BRIEF_NOT_CONFIRMED, status_code=400)

    async with _start_lock:
        _forget_finished_runs()

        if request_id in _active_runs:
            # Тот же запрос уже идёт — второй прогон не заводим, отдаём
            # текущий прогресс тем же фрагментом, что и на старте (РЕШ-147).
            return _progress_fragment(request, request_id)

        if _active_runs:
            other_id = next(iter(_active_runs))
            return _notice(REFUSAL_ANOTHER_RUN_ACTIVE.format(other_id=other_id), status_code=409)

        # Бриф грузится и статус пишется ВНУТРИ замка: пока эти await'ы не
        # завершились, второй конкурентный запрос ждёт на входе в
        # `async with` и не может проскочить с пустым реестром на руках.
        brief = await brief_store.load_brief(session, request_id)
        row.status = RequestStatus.SEARCHING
        await session.flush()

        task = asyncio.create_task(_run_pipeline(request_id, brief, initial_cost_rub=row.cost_rub or 0.0))
        _active_runs[request_id] = task

    return _progress_fragment(request, request_id)


@router.get("/requests/{request_id}", response_class=HTMLResponse)
async def request_hub(
    request: Request,
    request_id: int,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Хаб запроса: бриф, живой прогресс или плашка прерванного прогона,
    ссылка на отчёт при готовности, стоимость (РЕШ-189 п. 12).

    Несуществующий `request_id` отдаёт человекочитаемый HTML тем же способом,
    что и `start_run` (`_notice`), а не голый JSON `HTTPException`: у маршрута
    указан `response_class=HTMLResponse`, но это только метаданные OpenAPI —
    `HTTPException` всё равно уходит в стандартный обработчик Starlette и
    превращается в `application/json` (ревью цикла 1, находка 2).
    """
    row = await session.get(RequestRow, request_id)
    if row is None:
        return _notice(REQUEST_NOT_FOUND.format(request_id=request_id), status_code=404)

    brief = await brief_store.load_brief(session, request_id)
    confirmed_at = (
        await session.execute(select(BriefRow.confirmed_at).where(BriefRow.request_id == request_id))
    ).scalar_one_or_none()

    return _templates(request).TemplateResponse(
        request=request,
        name="request.html",
        context={
            "row": row,
            "brief": brief,
            "brief_confirmed": confirmed_at is not None,
            "task_active": is_run_active(request_id),
        },
    )


@router.get("/requests/{request_id}/progress")
async def progress_stream(request_id: int) -> StreamingResponse:
    """Живой прогресс поиска через Server-Sent Events (SPEC Э3-4).

    Источник — брокер `app.web.progress` (И-2), сериализация в SSE и
    демо-фильтр стоимости (РЕШ-188) — здесь.
    """
    demo_mode = get_settings().demo_mode_default

    async def event_source():
        previous: progress.ProgressEvent | None = None
        async for event in progress.get_broker().subscribe(request_id, keepalive_interval=_KEEPALIVE_INTERVAL):
            message = event.message
            if demo_mode and event.stage == "done":
                message = _without_cost(message)
            payload: dict[str, object] = {"stage": event.stage, "message": message, "fraction": event.fraction}
            if event is previous:
                # Брокер отдаёт один и тот же объект события повторно при
                # молчании источника — совпадение ссылки и есть keepalive,
                # без правки протокола брокера.
                payload["keepalive"] = True
            previous = event
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
