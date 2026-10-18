"""Роутинг по Host (Э6-6а): публичный хост пускает только белый список путей.

Приложение собирается настоящим `main.build_app`, сети и базы нет: ответы,
которые проверяются, даёт сама прослойка или проверка ключа до обращения к
базе. Маршрутов `/scan` в приложении пока нет (Э6-6в), поэтому в собранное
приложение добавляется пробный `/scan`, чтобы доказать, что 404 на рабочем хосте
и при пустой настройке даёт прослойка, а не отсутствие маршрута.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.config import Settings, get_settings
from app.web.host_routing import is_public_path, normalize_host

PUBLIC = "scan.example.ru"
WORK = "scout.example.ru"
KEY = "test-key-alpha-0000"


@pytest.fixture(autouse=True)
def _api_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCOUT_API_KEYS", f"alpha:{KEY}")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _build(public_host: str):
    app = main.build_app("test", public_scan_host=public_host)

    @app.get("/scan")
    async def _probe() -> dict[str, str]:
        return {"page": "scan"}

    @app.get("/scan/{token}.json")
    async def _probe_json(token: str) -> dict[str, str]:
        return {"token": token}

    return app


def _client(app, host: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=f"http://{host}")


# ── Публичный хост ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/request/1", "/history", "/requests/1/report", "/api/docs", "/api/openapi.json"])
async def test_публичный_хост_закрывает_рабочие_пути_404_без_пароля(path: str):
    async with _client(_build(PUBLIC), PUBLIC) as client:
        response = await client.get(path)  # заголовка Authorization нет

    assert response.status_code == 404
    assert "www-authenticate" not in response.headers


async def test_публичный_хост_закрывает_рабочие_пути_и_для_post():
    async with _client(_build(PUBLIC), PUBLIC) as client:
        response = await client.post("/run", data={"task": "x"})

    assert response.status_code == 404


async def test_корень_публичного_хоста_ведёт_на_scan():
    async with _client(_build(PUBLIC), PUBLIC) as client:
        response = await client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/scan"


async def test_публичный_хост_пускает_scan_health_static_и_api():
    async with _client(_build(PUBLIC), PUBLIC) as client:
        scan = await client.get("/scan")
        result = await client.get("/scan/abc.json")
        health = await client.get("/health")
        api = await client.get("/api/v1/requests/1")  # без ключа: 401 самого API, не 404
        static = await client.get("/static/нет-такого-файла.css")

    assert scan.status_code == 200
    assert result.json() == {"token": "abc"}
    assert health.status_code == 200
    assert api.status_code == 401
    # Файла нет, но 404 отдаёт StaticFiles текстом, а не прослойка своим JSON.
    assert static.status_code == 404
    assert static.headers["content-type"].startswith("text/plain")


async def test_api_v1_на_публичном_хосте_отвечает_как_на_рабочем():
    app = _build(PUBLIC)
    async with _client(app, PUBLIC) as public, _client(app, WORK) as work:
        on_public = await public.get("/api/v1/requests/1")
        on_work = await work.get("/api/v1/requests/1")

    assert on_public.status_code == on_work.status_code == 401


@pytest.mark.parametrize("host", ["SCAN.Example.RU", "scan.example.ru:8443", "Scan.Example.Ru.:443"])
async def test_хост_с_портом_и_в_верхнем_регистре_распознан(host: str):
    app = _build(PUBLIC)
    async with _client(app, WORK) as client:
        response = await client.get("/history", headers={"Host": host})

    assert response.status_code == 404
    assert "www-authenticate" not in response.headers


async def test_префикс_scan_не_цепляет_чужие_пути():
    async with _client(_build(PUBLIC), PUBLIC) as client:
        response = await client.get("/scanner")

    assert response.status_code == 404
    assert not is_public_path("/scanner")
    assert not is_public_path("/health/x")


# ── Рабочий хост ──────────────────────────────────────────────────────────────
async def test_рабочий_хост_не_отдаёт_scan_и_оставляет_прежние_пути():
    app = _build(PUBLIC)
    async with _client(app, WORK) as client:
        scan = await client.get("/scan")
        result = await client.get("/scan/abc.json")
        history = await client.get("/history")  # прежний путь: общий пароль, а не 404
        health = await client.get("/health")
        root = await client.get("/", follow_redirects=False)

    assert scan.status_code == 404
    assert result.status_code == 404
    assert history.status_code == 401
    assert health.status_code == 200
    assert root.status_code == 401


# ── PUBLIC_SCAN_HOST пуст ─────────────────────────────────────────────────────
@pytest.mark.parametrize("host", [PUBLIC, WORK, "localhost:8000"])
async def test_без_настройки_scan_закрыт_везде_а_рабочие_пути_прежние(host: str):
    app = _build("")
    async with _client(app, host) as client:
        scan = await client.get("/scan")
        history = await client.get("/history")
        root = await client.get("/", follow_redirects=False)

    assert scan.status_code == 404
    assert history.status_code == 401
    assert root.status_code == 401


# ── Мелочи ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Scan.Example.RU:8443", "scan.example.ru"),
        ("scan.example.ru.", "scan.example.ru"),
        ("[::1]:8000", "::1"),
        ("  ", ""),
    ],
)
def test_normalize_host(raw: str, expected: str):
    assert normalize_host(raw) == expected


def test_умолчания_настроек_скана_по_РЕШ_258():
    settings = Settings(_env_file=None)

    assert settings.scan_limit_per_ip_per_day == 2
    assert settings.scan_daily_budget_rub == 300
    assert settings.scan_cost_limit_rub == 10
    assert settings.public_scan_host == ""
    assert settings.scan_order_telegram_url == ""
    assert settings.scan_order_email == ""
    assert settings.yandex_search_rub_per_query == 0.48
