"""Публичный экспресс-скан: форма, запуск, страница, JSON и заявка (Э6-6в, РЕШ-254, РЕШ-258).

Маршруты подключаются в `app.main` без общего пароля интерфейса: их защищает
роутинг по Host (`app.web.host_routing`), лимит по адресу и суточный потолок
расходов (`app.web.scan_limits`). `PUBLIC_SCAN_HOST` пуст — прослойка отвечает
404 на любой `/scan*`.

- `GET /scan` — форма: текст задачи и вид решения; контактов нет;
- `POST /scan` — допуск, запуск фоновой задачи, 303 на страницу по токену;
- `GET /scan/{token}` — прогресс, результат или честный отказ;
- `GET /scan/{token}/status` — фрагмент прогресса для HTMX;
- `GET /scan/{token}.json` — тот же результат машинным видом.

Результат строит только `ScanView` (`build_scan_view`); JSON — только
`scan_payload`. Номер запроса наружу не отдаётся, адрес посетителя нигде не
записывается и в журнал не пишется. Единственный «следующий шаг» страницы —
кнопка заявки (РЕШ-254): куда она ведёт, решают `SCAN_ORDER_*`; оплаты,
хранения заявок и событий для CRM здесь нет.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.anonymize import find_deidentification_risks
from app.config import Settings, get_settings
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.pipeline.persistence import ReportNotFoundError
from app.web import scan_run
from app.web.routes import run as web_run
from app.web.scan_limits import ScanAdmission, admit_scan, client_ip, ip_counter
from app.web.scan_view import SCAN_BANNER, scan_payload

log = logging.getLogger(__name__)
router = APIRouter(tags=["scan"])

#: Предел длины текста: защита бюджета, а не правило содержания. Короче
#: минимума модель описывать нечего, а запуск стоит денег.
TASK_MIN_LENGTH = 20
TASK_MAX_LENGTH = 3000

#: Виды решения на форме: значение → подпись. «Науки» среди них нет намеренно —
#: научный поиск в публичный скан не входит, присланное руками `research` — 422.
KIND_CHOICES: tuple[tuple[str, str], ...] = (
    ("product", "Продукт"),
    ("digital", "Цифровое решение"),
    ("business_model", "Бизнес-модель"),
    ("unknown", "Не знаю"),
)
DEFAULT_KIND = "unknown"
_ALLOWED_KINDS = frozenset(value for value, _ in KIND_CHOICES)

#: Тексты клиенту (РЕШ-275: в редакции исполнителя, владелец правит после показа).
MSG_TASK_LENGTH = f"Опишите задачу подробнее: от {TASK_MIN_LENGTH} до {TASK_MAX_LENGTH} знаков."
MSG_KIND = "Выберите вид решения из списка."
MSG_DEIDENTIFICATION = "В тексте похоже на название компании или бренда. Замените его обезличенной формулировкой."
MSG_BUSY = "Сейчас готовится другой обзор. Повторите через несколько минут: ваша попытка не потрачена."
MSG_IP_LIMIT = "С вашего адреса сегодня уже запущено максимальное число обзоров. Попробуйте завтра."
MSG_BUDGET = "Бесплатный обзор недоступен: лимит на сегодня исчерпан. Попробуйте завтра."
MSG_FAILED = "Обзор не получился. Повторите попытку позже или опишите задачу иначе."
MSG_STOPPED = "Обзор остановлен: достигнут предел расходов на один обзор. Опишите задачу короче и точнее."
MSG_INTERRUPTED = "Подготовка обзора прервана перезапуском сервера. Запустите обзор заново."
MSG_NOT_READY = "Обзор ещё готовится."
MSG_NOT_FOUND = "Обзор не найден. Проверьте адрес страницы."

#: Сколько знаков токена служит номером обзора для заявки: хватает, чтобы
#: найти скан, и не раскрывает адрес страницы.
SCAN_NUMBER_LENGTH = 8
_TELEGRAM_RE = re.compile(r"^https://t\.me/[A-Za-z0-9_+/\-]+$")
_EMAIL_RE = re.compile(r"^[^@\s<>\"',;?&]+@[^@\s<>\"',;?&]+\.[^@\s<>\"',;?&]+$")
_PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    # Ссылки на источники ведут наружу: без этого токен страницы уходил бы им в Referer.
    "Referrer-Policy": "no-referrer",
}


@dataclass(frozen=True)
class OrderLinks:
    """Куда ведёт кнопка «Заказать полный отчёт»: основной адрес и необязательный запасной."""

    primary_url: str
    secondary_url: str = ""
    number: str = ""


def scan_number(token: str) -> str:
    """Номер обзора для заявки — начало токена."""
    return token[:SCAN_NUMBER_LENGTH]


def order_links(settings: Settings, token: str) -> OrderLinks | None:
    """Адреса заявки по `SCAN_ORDER_*`; оба пусты или негодны — `None`, кнопки нет.

    Telegram принимается только вида `https://t.me/…`; почта — как адрес без
    пробелов и служебных знаков. Номер обзора идёт в тему письма; в ссылку
    Telegram его не вставить, поэтому страница показывает его текстом.
    """
    number = scan_number(token)
    telegram = settings.scan_order_telegram_url.strip()
    email = settings.scan_order_email.strip()
    telegram_url = telegram if _TELEGRAM_RE.match(telegram) else ""
    mail_url = ""
    if _EMAIL_RE.match(email):
        mail_url = f"mailto:{email}?subject={quote(f'Заказ полного отчёта, обзор {number}')}"
    if telegram_url:
        return OrderLinks(primary_url=telegram_url, secondary_url=mail_url, number=number)
    if mail_url:
        return OrderLinks(primary_url=mail_url, number=number)
    return None


def _templates():
    from app.main import templates

    return templates


def _page(request: Request, name: str, context: dict[str, object], *, status_code: int = 200) -> HTMLResponse:
    """Страница скана: плашка идёт в каждый шаблон, закрытые от индексации заголовки — в каждый ответ."""
    response = _templates().TemplateResponse(
        request=request, name=name, context={"banner": SCAN_BANNER, **context}, status_code=status_code
    )
    response.headers.update(_PRIVATE_HEADERS)
    return response


def _form_page(request: Request, *, task: str = "", kind: str = DEFAULT_KIND, error: str = "", status_code: int = 200):
    return _page(
        request,
        "scan_form.html",
        {
            "task": task,
            "kind": kind,
            "kinds": KIND_CHOICES,
            "error": error,
            "task_min": TASK_MIN_LENGTH,
            "task_max": TASK_MAX_LENGTH,
        },
        status_code=status_code,
    )


def _form_problem(task: str, kind: str) -> str:
    """Причина отказа по самой форме или пустая строка; платных вызовов тут нет."""
    if kind not in _ALLOWED_KINDS:
        return MSG_KIND
    if not TASK_MIN_LENGTH <= len(task) <= TASK_MAX_LENGTH:
        return MSG_TASK_LENGTH
    if find_deidentification_risks(task):
        # Найденные фрагменты не возвращаются: эхо чужого названия на странице — лишнее.
        return MSG_DEIDENTIFICATION
    return ""


@router.get("/scan", response_class=HTMLResponse)
async def scan_form(request: Request) -> HTMLResponse:
    """Форма запуска экспресс-скана."""
    return _form_page(request)


@router.post("/scan")
async def scan_start(request: Request, task: str = Form(""), kind: str = Form(DEFAULT_KIND)) -> Response:
    """Допустить скан и запустить его в фоне; успех — 303 на страницу по токену.

    Порядок отказов: сама форма (422), занятая система (503, лимит не тратится),
    лимит адреса и суточный потолок (429). Допуск и запись попытки адреса идут
    подряд, без `await` между ними (РЕШ-281 п. 4), и всё — под системным замком
    `_start_lock`, как у `/api/v1`: два параллельных POST не пройдут проверку оба.
    """
    from app.db import session_scope

    text = task.strip()
    problem = _form_problem(text, kind)
    if problem:
        return _form_page(request, task=text, kind=kind, error=problem, status_code=422)

    settings = get_settings()
    ip = client_ip(request)
    now = datetime.now(UTC)
    async with web_run._start_lock:
        web_run._forget_finished_runs()
        if web_run._active_runs:
            response = _form_page(request, task=text, kind=kind, error=MSG_BUSY, status_code=503)
            response.headers["Retry-After"] = "120"
            return response
        async with session_scope() as session:
            admission = await admit_scan(session, ip, now, settings=settings)
        if admission is not ScanAdmission.OK:
            message = MSG_IP_LIMIT if admission is ScanAdmission.IP_LIMIT else MSG_BUDGET
            return _form_page(request, task=text, kind=kind, error=message, status_code=429)
        ip_counter.record(ip, now)
        request_id, token = await scan_run.create_scan_request(text)
        web_run._active_runs[request_id] = asyncio.create_task(scan_run.run_scan(request_id, text, kind))

    log.info("экспресс-скан №%s запущен", request_id)
    return RedirectResponse(f"/scan/{token}", status_code=303, headers=_PRIVATE_HEADERS)


def _state(row: RequestRow) -> str:
    """Состояние скана: `ready`, `failed`, `running` или `interrupted` (прогона нет, статус не конечный)."""
    if row.status in scan_run.READY_STATUSES:
        return "ready"
    if row.status == RequestStatus.FAILED:
        return "failed"
    return "running" if web_run.is_run_active(row.id) else "interrupted"


def _failure_text(row: RequestRow, state: str) -> str:
    if state == "interrupted":
        return MSG_INTERRUPTED
    return MSG_STOPPED if row.error == scan_run.SCAN_ERROR_STOP else MSG_FAILED


def _json_error(status_code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"detail": {"error": error, "message": message}}, headers=_PRIVATE_HEADERS
    )


# Маршрут `.json` объявлен раньше `/scan/{token}`: иначе токен с суффиксом читался бы как токен.
@router.get("/scan/{token}.json")
async def scan_json(token: str) -> JSONResponse:
    """JSON готового скана; несуществующий токен — 404, неготовый или отказавший — 409."""
    from app.db import session_scope

    async with session_scope() as session:
        row = await scan_run.find_scan(session, token)
        if row is None:
            return _json_error(404, "not_found", MSG_NOT_FOUND)
        state = _state(row)
        if state != "ready":
            if state == "running":
                return _json_error(409, "not_ready", MSG_NOT_READY)
            return _json_error(409, "scan_failed", _failure_text(row, state))
        try:
            view = await scan_run.load_scan_view(session, row)
        except ReportNotFoundError:
            log.warning("у готового скана нет отчёта")
            return _json_error(409, "scan_failed", MSG_FAILED)
    return JSONResponse(content=scan_payload(view), headers=_PRIVATE_HEADERS)


@router.get("/scan/{token}/status", response_class=HTMLResponse)
async def scan_status(request: Request, token: str) -> Response:
    """Фрагмент прогресса для HTMX; когда скан закончен или отказал, отправляет на страницу."""
    from app.db import session_scope

    async with session_scope() as session:
        row = await scan_run.find_scan(session, token)
        state = _state(row) if row is not None else ""
    if state == "running":
        return _page(request, "_scan_progress.html", {"token": token})
    # Неизвестный токен тоже ведёт на страницу: она ответит 404 человеческим текстом.
    response = Response(status_code=200)
    response.headers.update(_PRIVATE_HEADERS)
    response.headers["HX-Redirect"] = f"/scan/{token}"
    return response


@router.get("/scan/{token}", response_class=HTMLResponse)
async def scan_page(request: Request, token: str) -> HTMLResponse:
    """Страница скана: прогресс, результат с кнопкой заявки или честный отказ."""
    from app.db import session_scope

    async with session_scope() as session:
        row = await scan_run.find_scan(session, token)
        if row is None:
            return _page(request, "scan_result.html", {"state": "missing", "text": MSG_NOT_FOUND}, status_code=404)
        state = _state(row)
        if state == "running":
            return _page(request, "scan_result.html", {"state": "running", "token": token})
        if state == "ready":
            try:
                view = await scan_run.load_scan_view(session, row)
            except ReportNotFoundError:
                log.warning("у готового скана нет отчёта")
                return _page(request, "scan_result.html", {"state": "failed", "text": MSG_FAILED})
            context = {
                "state": "ready",
                "view": view,
                "order": order_links(get_settings(), token),
                "token": token,
            }
            return _page(request, "scan_result.html", context)
        return _page(request, "scan_result.html", {"state": state, "text": _failure_text(row, state)})
