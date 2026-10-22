"""Публичный экспресс-скан: экранирование ввода, формат токена, заголовки и граничные значения (Э6-6в, цикл 2).

Закрывает блокирующие замечания ревью цикла 1 и выжившие мутации: отражённый
XSS на форме (текст посетителя и текст отказа), токен вне формата в пути
(заголовок `HX-Redirect`), `X-Robots-Tag`, длина номера обзора, набор готовых
статусов, схема ссылки источника и нижняя граница длины текста. Приложение,
база и заглушки пайплайна — те же, что в `test_scan_routes.py`.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import unquote

import pytest
from httpx import AsyncClient

import test_scan_routes as base
from app import main
from app.config import get_settings
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.web import scan_run
from app.web.routes import run
from app.web.routes import scan as scan_routes
from app.web.scan_view import SCAN_BANNER

# Фикстуры общие с test_scan_routes: окружение, база и клиент публичного хоста.
_scan_env = base._scan_env
db = base.db
client = base.client

EVIL_TASK = '</textarea><script>alert(1)</script>"><img src=x onerror=alert(2)>'
EVIL_SHORT = "</textarea><script>"  # 19 знаков: короче нижней границы
EVIL_ERROR = "<script>alert(3)</script>"
ESCAPED_ERROR = "&lt;script&gt;alert(3)&lt;/script&gt;"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&#34;")


@pytest.mark.parametrize("case", ["вид", "длина", "название", "ip", "потолок", "занято"])
async def test_ввод_и_текст_отказа_экранируются_на_каждом_отказе_формы(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, case: str
):
    task = EVIL_TASK
    if case == "вид":
        monkeypatch.setattr(scan_routes, "MSG_KIND", EVIL_ERROR)
        response = await base._post(client, task=task, kind="research")
        status = 422
    elif case == "длина":
        monkeypatch.setattr(scan_routes, "MSG_TASK_LENGTH", EVIL_ERROR)
        task = EVIL_SHORT
        response = await base._post(client, task=task)
        status = 422
    elif case == "название":
        monkeypatch.setattr(scan_routes, "MSG_DEIDENTIFICATION", EVIL_ERROR)
        task = f"{EVIL_TASK} ООО «Ромашка»"
        response = await base._post(client, task=task)
        status = 422
    elif case == "ip":
        monkeypatch.setattr(scan_routes, "MSG_IP_LIMIT", EVIL_ERROR)
        for _ in range(2):
            base._token(await base._post(client))
            await base._finish()
        response = await base._post(client, task=task)
        status = 429
    elif case == "потолок":
        monkeypatch.setattr(scan_routes, "MSG_BUDGET", EVIL_ERROR)
        monkeypatch.setenv("SCAN_DAILY_BUDGET_RUB", "5")
        get_settings.cache_clear()
        response = await base._post(client, task=task)
        status = 429
    else:
        monkeypatch.setattr(scan_routes, "MSG_BUSY", EVIL_ERROR)
        base._FakePipeline.gate = asyncio.Event()
        base._token(await base._post(client))
        await asyncio.sleep(0)
        response = await base._post(client, task=task)
        status = 503
    assert response.status_code == status
    html = response.text
    # Сырых вставок нет: единственный <script> страницы — htmx из основы.
    assert "<script>alert" not in html
    assert len(re.findall(r"<script", html, re.I)) == 1
    assert "<img" not in html and html.count("</textarea>") == 1
    assert "&lt;script&gt;" in html
    assert _escape(task.strip()) in html  # введённое возвращается в поле целиком, но безопасным текстом
    assert ESCAPED_ERROR in html  # текст отказа тоже экранирован
    if base._FakePipeline.gate is not None:
        base._FakePipeline.gate.set()
        await base._finish()


async def test_текст_отказа_скана_и_ненайденной_страницы_экранируется(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(scan_routes, "MSG_FAILED", EVIL_ERROR)
    monkeypatch.setattr(scan_routes, "MSG_NOT_FOUND", EVIL_ERROR)
    base._FakePipeline.fail = True
    token = base._token(await base._post(client))
    await base._finish()
    for path in (f"/scan/{token}", f"/scan/{'A' * 43}", "/scan/мусор"):
        html = (await client.get(path)).text
        assert "<script>alert(3)" not in html
        assert ESCAPED_ERROR in html


# --- Формат токена и заголовок HX-Redirect ---------------------------------------------------------------------

BAD_TOKENS = [
    "a%0d%0aX-Injected:%20yes",
    "a%0aX-Injected:%20yes",
    "A" * 42,
    "A" * 44,
    "A" * 43 + "%0a",
    "A" * 42 + "=",
    "A" * 42 + ".",
    "A" * 42 + "~",
    "A" * 42 + "%D0%B0",
    "1",
    "None",
]


@pytest.mark.parametrize("token", BAD_TOKENS)
async def test_токен_не_того_вида_404_без_базы_и_без_заголовков_из_пути(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, token: str
):
    async def _no_db(*_args, **_kwargs):
        raise AssertionError("токен не того вида дошёл до базы")

    monkeypatch.setattr(scan_run, "find_scan", _no_db)
    status = await client.get(f"/scan/{token}/status")
    page = await client.get(f"/scan/{token}")
    payload = await client.get(f"/scan/{token}.json")
    assert (status.status_code, page.status_code, payload.status_code) == (404, 404, 404)
    for response in (status, page, payload):
        assert "hx-redirect" not in response.headers and "x-injected" not in response.headers
        assert not any(("\r" in value or "\n" in value) for value in response.headers.values())
        assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert payload.json()["detail"]["error"] == "not_found"
    assert SCAN_BANNER in page.text and "не найден" in page.text


async def test_hx_redirect_строится_только_из_проверенного_токена(client: AsyncClient):
    token = await base._ready(client)
    done = await client.get(f"/scan/{token}/status")
    assert done.status_code == 200
    assert done.headers["hx-redirect"] == f"/scan/{token}"
    assert scan_run.is_scan_token(token)
    unknown = "B" * 43  # вид верный, такого обзора нет: страница сама ответит 404
    assert (await client.get(f"/scan/{unknown}/status")).headers["hx-redirect"] == f"/scan/{unknown}"


# --- X-Robots-Tag на страницах токена и формы -------------------------------------------------------------------


async def test_x_robots_tag_на_форме_редиректе_странице_статусе_и_json(client: AsyncClient):
    expected = "noindex, nofollow"
    assert (await client.get("/scan")).headers["x-robots-tag"] == expected
    redirect = await base._post(client)
    assert redirect.headers["x-robots-tag"] == expected
    token = base._token(redirect)
    await base._finish()
    for path in (f"/scan/{token}", f"/scan/{token}/status", f"/scan/{token}.json", f"/scan/{'C' * 43}"):
        assert (await client.get(path)).headers["x-robots-tag"] == expected, path
    refused = await base._post(client, kind="research")
    assert refused.headers["x-robots-tag"] == expected


# --- Номер обзора в заявке ------------------------------------------------------------------------------------


async def test_номер_обзора_в_теме_письма_ровно_восемь_знаков_и_токена_целиком_нет(client: AsyncClient):
    assert scan_routes.SCAN_NUMBER_LENGTH == 8
    token = await base._ready(client)
    html = (await client.get(f"/scan/{token}")).text
    assert token not in html and token[:9] not in html
    subject = unquote(re.search(r"mailto:[^?]+\?subject=([^\"]+)\"", html).group(1))
    number = re.fullmatch(r"Заказ полного отчёта, обзор (\S+)", subject).group(1)
    assert number == token[:8] and len(number) == 8
    assert scan_routes.scan_number(token) == token[:8]


# --- Незавершённый статус не показывает результат ----------------------------------------------------------------


@pytest.mark.parametrize("status", [RequestStatus.INTAKE, RequestStatus.BRIEFING, RequestStatus.SEARCHING])
async def test_незавершённый_статус_не_показывает_результат(client: AsyncClient, db, status: RequestStatus):
    request_id, token = await scan_run.create_scan_request(base.TASK)
    brief = base.Brief(industry="не указана", task_summary="тест", solution_kind="digital")
    async with db() as session:
        row = await session.get(RequestRow, request_id)
        row.status = status
        row.report_json = base._report(request_id, brief).model_dump_json()  # отчёт уже есть, статус ещё нет
    gate = asyncio.get_running_loop().create_future()
    run._active_runs[request_id] = gate  # живая задача: скан идёт
    try:
        page = await client.get(f"/scan/{token}")
        assert page.status_code == 200 and "Готовим обзор" in page.text
        assert "Катализатор с добавкой церия" not in page.text and "Заказать полный отчёт" not in page.text
        assert "hx-redirect" not in (await client.get(f"/scan/{token}/status")).headers
        payload = await client.get(f"/scan/{token}.json")
        assert payload.status_code == 409 and payload.json()["detail"]["error"] == "not_ready"
    finally:
        gate.cancel()
        run._active_runs.pop(request_id, None)


def test_готовые_статусы_только_после_сборки_отчёта():
    assert {RequestStatus.DRAFTED, RequestStatus.VALIDATED, RequestStatus.EXPORTED} == scan_run.READY_STATUSES


# --- Схема ссылки источника ----------------------------------------------------------------------------------

_LINK = '{% from "scan_result.html" import source_link %}{{ source_link(url) }}'


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>1</script>",
        "//evil.example/x",
        "ftp://x/y",
        "",
    ],
)
def test_ссылка_источника_с_чужой_схемой_не_попадает_в_href(url: str):
    html = main.templates.env.from_string(_LINK).render(url=url)
    assert "href" not in html and "источник не указан" in html


@pytest.mark.parametrize("url", ["https://source.example.org/a?b=1&c=2", "http://source.example.org/"])
def test_ссылка_источника_http_и_https_попадает_в_href(url: str):
    html = main.templates.env.from_string(_LINK).render(url=url)
    assert 'href="' in html and 'rel="noopener noreferrer nofollow"' in html
    assert "<script" not in html


# --- Границы длины текста -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("length", "status"), [(19, 422), (20, 303), (3000, 303), (3001, 422)])
async def test_границы_длины_текста(client: AsyncClient, length: int, status: int):
    assert scan_routes.TASK_MIN_LENGTH == 20 and scan_routes.TASK_MAX_LENGTH == 3000
    response = await base._post(client, task="а" * length)
    assert response.status_code == status
    await base._finish()
