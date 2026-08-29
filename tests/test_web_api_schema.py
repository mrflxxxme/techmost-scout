"""Схема API закрыта в боевом окружении (Э5-И4, РЕШ-224).

`schema_urls(app_env)` — единственная точка правды, где решается, отдаёт ли
приложение `/api/docs` и `/api/openapi.json`. Правило закрыто по умолчанию:
открыто только для `dev` и `test` в нижнем регистре. До этой задачи `docs_url`
был зашит прямо в `FastAPI(...)`, а вход по паролю навешен только на роутер
интерфейса — схема API оставалась за одним рубежом Caddy вместо двух.

Ревью цикла 1 (`.planning/evidence/Э5-И4-review.md`) нашло дыру: поведенческие
тесты собирали `FastAPI(docs_url=..., openapi_url=...)` заново из значений
`schema_urls(...)`, а тест шаблона подменял `templates.env.globals` вручную
через `monkeypatch.setitem` — обе проверки байпасили настоящий код разводки
в `main.build_app`, и мутация, зашивающая один из адресов прямо в вызов
`FastAPI(...)` в обход `schema_urls`, проходила весь набор незамеченной.
Поэтому здесь тесты вызывают `main.build_app(app_env)` напрямую — ту же
функцию, что строит боевой `main.app`, — а не пересобирают приложение мимо
неё.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import main


@pytest.fixture(autouse=True)
def _restore_api_docs_url() -> Iterator[None]:
    """`build_app` пишет в общий `templates.env.globals` — тесты не должны просачиваться друг в друга.

    `templates` — модульный синглтон (общий и для боевого `main.app`, и для
    приложений, собранных тестами через `main.build_app`), поэтому вызов
    `build_app("prod")` внутри теста меняет состояние, которое видят и
    остальные тестовые файлы, если их запустить в одном процессе. Фикстура
    возвращает значение, которое было до теста (для тестового окружения —
    выставленное импортом `main.app = main.build_app("test")`).
    """
    original = main.templates.env.globals.get("api_docs_url")
    yield
    main.templates.env.globals["api_docs_url"] = original


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_schema_urls_open_in_dev_environments(app_env: str):
    """В средах разработки адреса схемы отдаются."""
    assert main.schema_urls(app_env) == ("/api/docs", "/api/openapi.json")


@pytest.mark.parametrize("app_env", ["prod", "PROD", ""])
def test_schema_urls_closed_by_default(app_env: str):
    """Любое иное значение, включая боевое, пустое и опечатку, закрывает схему."""
    assert main.schema_urls(app_env) == (None, None)


@pytest.mark.parametrize("app_env", ["Dev", "DEV", "Test", "TEST"])
def test_schema_urls_comparison_is_case_sensitive(app_env: str):
    """Сравнение регистрозависимое: `.lower()` не вызывается — фиксирует докстроку функции.

    Отклонение безопасное (опечатка в регистре закрывает схему, а не
    открывает), но докстрока `schema_urls` обязана говорить об этом прямо,
    а не подразумевать нормализацию регистра, которой нет (совет ревью
    цикла 1).
    """
    assert main.schema_urls(app_env) == (None, None)


def test_prod_app_returns_404_on_schema_endpoints():
    """Настоящая сборка приложения (`main.build_app`) для `prod` не отдаёт ни документацию, ни openapi.json.

    Приложение собирается той же функцией, что и боевой `main.app`, — не
    отдельным вызовом `FastAPI(docs_url=..., openapi_url=...)` в тесте.
    Мутация, зашивающая один из адресов прямо в `FastAPI(...)` внутри
    `build_app` в обход `schema_urls`, красит именно этот тест или
    следующий — в зависимости от того, какой из двух адресов зашит.
    """
    app = main.build_app("prod")
    with TestClient(app) as client:
        assert client.get("/api/docs").status_code == 404
        assert client.get("/api/openapi.json").status_code == 404


def test_dev_app_returns_200_on_schema_endpoints():
    """Настоящая сборка приложения (`main.build_app`) для `dev` отдаёт и документацию, и openapi.json.

    Без этой половины тест на закрытие прошёл бы даже при полностью сломанной
    `schema_urls` — например, если бы она всегда возвращала `(None, None)`.
    """
    app = main.build_app("dev")
    with TestClient(app) as client:
        assert client.get("/api/docs").status_code == 200
        assert client.get("/api/openapi.json").status_code == 200


def _auth(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Настраивает пароль латиницей: `HTTPBasic` не декодирует заголовок как UTF-8."""
    monkeypatch.setattr(main.settings, "web_auth_user", "scout")
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr("parol123"))
    return "scout", "parol123"


def test_base_html_hides_api_link_when_schema_closed(monkeypatch: pytest.MonkeyPatch):
    """Приложение, собранное `build_app("prod")`, не печатает ссылку на схему API на странице `/`.

    `api_docs_url` в `templates.env.globals` выставляет сам `main.build_app`
    (не тест вручную) — так проверка бьёт по реальной разводке
    `schema_urls() -> templates.env.globals`, а не по подмене мимо неё.
    """
    credentials = _auth(monkeypatch)
    app = main.build_app("prod")
    with TestClient(app) as client:
        response = client.get("/", auth=credentials)
    assert response.status_code == 200
    assert "/api/docs" not in response.text


def test_base_html_shows_api_link_when_schema_open(monkeypatch: pytest.MonkeyPatch):
    """Приложение, собранное `build_app("dev")`, печатает ссылку именно на выданный адрес.

    Если строка `templates.env.globals["api_docs_url"] = docs_url` пропадёт
    из `build_app`, этот тест покраснеет: глобал не появится вовсе, и
    `base.html` не напечатает пункт «API» ни при каком окружении.
    """
    credentials = _auth(monkeypatch)
    app = main.build_app("dev")
    with TestClient(app) as client:
        response = client.get("/", auth=credentials)
    assert response.status_code == 200
    assert 'href="/api/docs"' in response.text
