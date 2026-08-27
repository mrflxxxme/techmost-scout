"""Вход по паролю: один общий basic auth на всех маршрутах, кроме /health.

РЕШ-16: защита живёт в приложении, Caddy при деплое — второй рубеж.
Тесты не трогают базу: 401 отдаётся зависимостью до вызова обработчика,
поэтому TestClient используется без контекстного менеджера (lifespan и
init_db не запускаются).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import main


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.mark.parametrize("path", ["/", "/requests", "/showcase", "/requests/1/review"])
def test_routes_require_auth(client: TestClient, path: str):
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Basic"


def test_health_is_open(client: TestClient):
    assert client.get("/health").status_code == 200


def test_require_auth_verdicts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(main.settings, "web_auth_user", "scout")
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr("верный"))
    ok = main.require_auth(HTTPBasicCredentials(username="scout", password="верный"))
    assert ok == "scout"
    with pytest.raises(HTTPException):
        main.require_auth(HTTPBasicCredentials(username="scout", password="неверный"))
