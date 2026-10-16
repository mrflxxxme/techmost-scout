"""JSON-API `/api/v1` для приложений экосистемы (SPEC Э6-5, РЕШ-250).

Роутер подключается в `app.main` отдельно от интерфейса: не под общим
паролем (`require_auth`), а под ключом приложения (`require_api_client`).
Caddy пропускает `/api/v1/*` мимо `basic_auth` — второй рубеж здесь ключ.

Три маршрута:
- `POST /api/v1/requests` — задача по контракту `LaunchContext` экосистемы;
  текст проверяется на обезличивание, запрос заводится, прогон идёт в фоне
  без вопросов (`app.web.api_run`), ответ 202;
- `GET /api/v1/requests/{id}` — машинный статус и подпись, признак проверки
  экспертом, готовность отчёта. Стоимости в ответе нет;
- `GET /api/v1/requests/{id}/report` — JSON-выгрузка (`app.export.json`)
  только после вердикта эксперта; до него 409 `awaiting_validation`.

Приложение видит только свои запросы: чужой и несуществующий номер дают один
и тот же 404, по ответу нельзя узнать, что номер занят. Все отказы — телом
`{"detail": {"error": код, "message": текст}}`; текст задачи и найденные
фрагменты обезличивания в ответ не возвращаются.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.anonymize import find_deidentification_risks
from app.config import get_settings
from app.db import get_session
from app.export.json import SCHEMA_VERSION, ReportNotValidatedError, export_report_json
from app.export.view import VALIDATED_STATUSES
from app.models import STATUS_LABELS, RequestStatus
from app.models import Request as RequestRow
from app.pipeline.persistence import ReportNotFoundError
from app.schemas import NEED_ID_PATTERN, REGION_CODE_PATTERN, BriefMode
from app.web import api_access, api_run
from app.web.routes import run as web_run

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["api-v1"])

#: Ограничение длины текста задачи: защита процесса и бюджета, а не правило
#: содержания — обычная постановка занимает одну-две тысячи знаков.
_TASK_MAX_LENGTH = 20_000


def _refusal(status_code: int, error: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": error, "message": message}, headers=headers)


def require_api_client(request: Request) -> str:
    """Имя приложения по ключу из заголовка; иначе отказ 503 или 401.

    `SCOUT_API_KEYS` пуст — API выключен, 503 на любой вызов. Ключа нет или
    он не совпал — 401. Ключ в лог и в ответ не попадает.
    """
    keys = api_access.parse_api_keys(get_settings().scout_api_keys.get_secret_value())
    if not keys:
        raise _refusal(503, "api_disabled", "JSON-API выключен: ключи приложений не заданы.")
    presented = api_access.presented_key(request.headers)
    client = api_access.match_api_key(presented, keys) if presented else None
    if client is None:
        raise _refusal(401, "unauthorized", "Нужен действующий ключ приложения.", {"WWW-Authenticate": "Bearer"})
    return client


def _rate_limit(client: str, kind: str, *, limit: int, window: float, record: bool = True) -> None:
    """Отказ 429 с `Retry-After`, если приложение исчерпало лимит частоты."""
    key = (client, kind)
    if record:
        retry_after = api_access.limiter.hit(key, limit=limit, window=window)
    else:
        retry_after = api_access.limiter.check(key, limit=limit, window=window)
    if retry_after is not None:
        raise _refusal(
            429, "rate_limited", "Превышен лимит частоты обращений, повторите позже.", {"Retry-After": str(retry_after)}
        )


def _read_limit(client: str = Depends(require_api_client)) -> str:
    _rate_limit(
        client,
        "reads",
        limit=get_settings().api_rate_limit_reads_per_minute,
        window=api_access.READS_WINDOW_SECONDS,
    )
    return client


# ── Тело запуска: подмножество `LaunchContext` версии 1.0 ─────────────────────
class _Region(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code_fns: str = Field(pattern=REGION_CODE_PATTERN)


class _Need(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=NEED_ID_PATTERN)


class _Source(BaseModel):
    model_config = ConfigDict(extra="ignore")
    product: Literal["site", "bot", "channel", "radar", "scout", "btb"]
    placement: str = Field(pattern=r"^[a-z0-9_]{1,40}$")


class _Context(BaseModel):
    """Поля `LaunchContext`, которые читает Скаут; остальные принимаются и игнорируются."""

    model_config = ConfigDict(extra="ignore")
    mode: BriefMode = "business"
    region: _Region | None = None
    need: _Need | None = None
    source: _Source | None = None


class _RunBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.0"]
    task: str = Field(min_length=1, max_length=_TASK_MAX_LENGTH)
    industry: str | None = Field(default=None, max_length=255)
    context: _Context | None = None


def _validation_refusal(exc: ValidationError) -> HTTPException:
    """422 со списком полей и причин, без эха присланных значений."""
    fields = [
        {"field": ".".join(str(part) for part in error["loc"]), "reason": error["type"]} for error in exc.errors()
    ]
    return HTTPException(
        status_code=422,
        detail={"error": "invalid_body", "message": "Тело запроса не соответствует контракту.", "fields": fields},
    )


def _links(request_id: int) -> dict[str, str]:
    return {"status": f"/api/v1/requests/{request_id}", "report": f"/api/v1/requests/{request_id}/report"}


def _not_found(request_id: int) -> HTTPException:
    return _refusal(404, "not_found", f"Запрос №{request_id} не найден.")


async def _own_row(session: AsyncSession, request_id: int, client: str) -> RequestRow:
    """Строка запроса этого приложения; чужая и несуществующая — один и тот же 404."""
    row = await session.get(RequestRow, request_id)
    if row is None or row.api_client != client:
        raise _not_found(request_id)
    return row


def _status_payload(row: RequestRow) -> dict[str, Any]:
    validated = row.status in VALIDATED_STATUSES
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": row.id,
        "status": str(row.status),
        "status_label": row.status_label,
        "validated": validated,
        "report_ready": validated and row.report_json is not None,
        "run_active": web_run.is_run_active(row.id),
        "links": _links(row.id),
    }


@router.post("/requests", status_code=202)
async def create_request(
    payload: Any = Body(...),
    client: str = Depends(require_api_client),
) -> JSONResponse:
    """Завести запрос по задаче приложения и запустить прогон в фоне (ответ 202).

    Тело принимается сырым и разбирается здесь: стандартный ответ 422 FastAPI
    возвращает присланные значения (`input`), а эхо текста задачи в ответе —
    лишнее раскрытие.
    """
    try:
        body = _RunBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_refusal(exc) from None

    task = body.task.strip()
    if not task:
        raise _refusal(422, "invalid_body", "Текст задачи пуст.")
    if find_deidentification_risks(task):
        # Найденные фрагменты не возвращаются: их эхо в ответе — то же раскрытие.
        raise _refusal(
            422,
            "deidentification_risk",
            "В тексте похоже на название компании или бренда. Замените обезличенной формулировкой.",
        )

    context = body.context or _Context()
    ecosystem: dict[str, Any] = {
        "region_code": context.region.code_fns if context.region else None,
        "need_id": context.need.id if context.need else None,
        "mode": context.mode,
    }
    settings = get_settings()

    async with web_run._start_lock:
        web_run._forget_finished_runs()
        _rate_limit(
            client,
            "runs",
            limit=settings.api_rate_limit_runs_per_hour,
            window=api_access.RUNS_WINDOW_SECONDS,
            record=False,
        )
        if web_run._active_runs:
            # Номер чужого прогона не называется: он может принадлежать другому приложению.
            raise _refusal(409, "busy", "Сейчас выполняется другой запрос. Повторите позже.")
        request_id = await api_run.create_api_request(task, client=client)
        api_access.limiter.record((client, "runs"))
        web_run._active_runs[request_id] = asyncio.create_task(
            api_run.run_api_request(request_id, task, industry=body.industry, ecosystem=ecosystem)
        )

    source = f"{context.source.product}/{context.source.placement}" if context.source else "не указан"
    log.info("запрос №%s заведён через API приложением %s, источник %s", request_id, client, source)
    response = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "status": str(RequestStatus.INTAKE),
        "status_label": STATUS_LABELS[RequestStatus.INTAKE],
        "links": _links(request_id),
    }
    return JSONResponse(status_code=202, content=response, headers={"Location": _links(request_id)["status"]})


@router.get("/requests/{request_id}")
async def request_status(
    request_id: int,
    client: str = Depends(_read_limit),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Статус запроса: машинный код, подпись, проверен ли экспертом, готов ли отчёт."""
    row = await _own_row(session, request_id, client)
    return _status_payload(row)


@router.get("/requests/{request_id}/report")
async def request_report(
    request_id: int,
    client: str = Depends(_read_limit),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """JSON-отчёт после вердикта эксперта; до него 409, черновик не отдаётся никогда."""
    row = await _own_row(session, request_id, client)
    if row.status == RequestStatus.FAILED:
        raise _refusal(409, "run_failed", "Прогон запроса завершился отказом, отчёта нет.")
    if row.status not in VALIDATED_STATUSES:
        error = "awaiting_validation" if row.status == RequestStatus.DRAFTED else "not_ready"
        raise _refusal(409, error, "Отчёт ещё не проверен экспертом.")
    try:
        return await export_report_json(session, request_id)
    except ReportNotValidatedError:
        raise _refusal(409, "awaiting_validation", "Отчёт ещё не проверен экспертом.") from None
    except ReportNotFoundError:
        log.info("отчёт запроса №%s недоступен для API", request_id)
        raise _refusal(409, "not_ready", "Отчёт ещё не готов.") from None
