"""Схема API закрыта в боевом окружении (Э5-И4, РЕШ-224).

`schema_urls(app_env)` — единственная точка правды, где решается, отдаёт ли
приложение `/api/docs` и `/api/openapi.json`. Правило закрыто по умолчанию:
открыто только для `dev` и `test` в нижнем регистре. До этой задачи `docs_url`
был зашит прямо в `FastAPI(...)`, а вход по паролю навешен только на роутер
интерфейса — схема API оставалась за одним рубежом Caddy вместо двух.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import main


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_schema_urls_open_in_dev_environments(app_env: str):
    """В средах разработки адреса схемы отдаются."""
    assert main.schema_urls(app_env) == ("/api/docs", "/api/openapi.json")


@pytest.mark.parametrize("app_env", ["prod", "PROD", ""])
def test_schema_urls_closed_by_default(app_env: str):
    """Любое иное значение, включая боевое, пустое и опечатку, закрывает схему."""
    assert main.schema_urls(app_env) == (None, None)


def test_prod_app_returns_404_on_schema_endpoints():
    """Приложение, собранное для `prod`, не отдаёт ни документацию, ни openapi.json."""
    docs_url, openapi_url = main.schema_urls("prod")
    app = FastAPI(docs_url=docs_url, openapi_url=openapi_url)
    with TestClient(app) as client:
        assert client.get("/api/docs").status_code == 404
        assert client.get("/api/openapi.json").status_code == 404


def test_dev_app_returns_200_on_schema_endpoints():
    """Приложение, собранное для `dev`, отдаёт и документацию, и openapi.json.

    Без этой половины тест на закрытие прошёл бы даже при полностью сломанной
    `schema_urls` — например, если бы она всегда возвращала `(None, None)`.
    """
    docs_url, openapi_url = main.schema_urls("dev")
    app = FastAPI(docs_url=docs_url, openapi_url=openapi_url)
    with TestClient(app) as client:
        assert client.get("/api/docs").status_code == 200
        assert client.get("/api/openapi.json").status_code == 200


def test_base_html_hides_api_link_when_schema_closed(monkeypatch: pytest.MonkeyPatch):
    """При `api_docs_url=None` страница не печатает ссылку на схему API."""
    monkeypatch.setattr(main.settings, "web_auth_user", "scout")
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr("parol123"))
    monkeypatch.setitem(main.templates.env.globals, "api_docs_url", None)
    with TestClient(main.app) as client:
        response = client.get("/", auth=("scout", "parol123"))
    assert response.status_code == 200
    assert "/api/docs" not in response.text


def test_base_html_shows_api_link_when_schema_open(monkeypatch: pytest.MonkeyPatch):
    """При заданном `api_docs_url` страница печатает ссылку именно на него, а не на зашитый адрес."""
    monkeypatch.setattr(main.settings, "web_auth_user", "scout")
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr("parol123"))
    monkeypatch.setitem(main.templates.env.globals, "api_docs_url", "/api/docs")
    with TestClient(main.app) as client:
        response = client.get("/", auth=("scout", "parol123"))
    assert response.status_code == 200
    assert 'href="/api/docs"' in response.text
