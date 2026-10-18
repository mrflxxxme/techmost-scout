"""Точка входа веб-приложения.

Один процесс: FastAPI отдаёт и API, и страницы на Jinja. Отдельного
фронтенд-сервиса нет — меньше движущихся частей, проще деплой, и агентам
негде рассинхронизировать типы между двумя проектами.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.db import init_db
from app.export.markdown import TEMPLATE_FILTERS
from app.web.host_routing import HostRoutingMiddleware

log = logging.getLogger(__name__)
settings = get_settings()


def schema_urls(app_env: str) -> tuple[str | None, str | None]:
    """Адреса схемы API. Открыты только при `app_env`, буквально равном `dev` или `test`.

    Единственная точка правды: до этой задачи `docs_url` был зашит в
    `FastAPI(...)`, а вход по паролю навешен только на роутер интерфейса
    (`app.web.routes`) — схема API оставалась за одним рубежом Caddy вместо
    двух. Правило закрыто по умолчанию: сравнение регистрозависимое —
    `.lower()` не вызывается, — поэтому любое значение, кроме буквально
    `dev` или `test` в нижнем регистре (включая `Dev`, `PROD`, пустую
    строку и опечатку), даёт `(None, None)`. Второй признак «мы в бою»,
    помимо `app_env`, этот этап не заводит.
    """
    if app_env in ("dev", "test"):
        return "/api/docs", "/api/openapi.json"
    return None, None


WEB_DIR = Path(__file__).parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters.update(TEMPLATE_FILTERS)  # общий фильтр developer_line (Э4-И1)
security = HTTPBasic(auto_error=True)


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Один общий вход. Личных кабинетов в MVP нет — роли разделены организационно."""
    expected_user = settings.web_auth_user
    expected_password = settings.web_auth_password.get_secret_value()
    # Сравниваются байты: compare_digest на строках падает TypeError, если в
    # пароле есть не-ASCII символы — вместо 401 получался бы 500.
    user_ok = secrets.compare_digest(credentials.username.encode("utf-8"), expected_user.encode("utf-8"))
    password_ok = secrets.compare_digest(credentials.password.encode("utf-8"), expected_password.encode("utf-8"))
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    await init_db()
    log.info("ТехМост Скаут запущен, окружение %s", settings.app_env)
    yield


def build_app(app_env: str, *, public_scan_host: str = "") -> FastAPI:
    """Собирает приложение целиком для заданного окружения.

    `public_scan_host` включает роутинг по Host (`app.web.host_routing`): пусто —
    публичного режима нет и `/scan*` недоступен на любом хосте.

    Единственное место, где результат `schema_urls` довозится и до объекта
    `FastAPI` (`docs_url`/`openapi_url`), и до `templates.env.globals`
    (`api_docs_url`, читает `base.html`). Тесты Э5-И4 вызывают эту функцию
    напрямую с `"prod"` и `"dev"`, чтобы проверять настоящий код сборки
    приложения, а не отдельно пересобранную копию мимо него (ревью цикла 1:
    мутация, зашивающая `openapi_url` прямо в вызов `FastAPI(...)` в обход
    `schema_urls`, проходила незамеченной, пока приложение строилось не
    здесь).
    """
    docs_url, openapi_url = schema_urls(app_env)
    app = FastAPI(
        title="ТехМост Скаут",
        description="Подбор научно-технических решений под задачу бизнеса",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=docs_url,
        openapi_url=openapi_url,
    )
    templates.env.globals["api_docs_url"] = docs_url

    static_dir = WEB_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Проверка живости для прокси и мониторинга. Без авторизации."""
        return {"status": "ok", "env": app_env}

    # Маршруты интерфейса и API живут в web/routes.py, чтобы main.py оставался
    # точкой сборки, а не свалкой обработчиков.
    from app.web.routes import router as web_router

    # Один общий вход на всех маршрутах, кроме /health (РЕШ-16): защита живёт в
    # приложении, пароль на Caddy при деплое — второй рубеж, а не единственный.
    app.include_router(web_router, dependencies=[Depends(require_auth)])

    # JSON-API экосистемы (РЕШ-250) — не под общим паролем интерфейса, а под
    # ключом приложения: зависимость `require_api_client` стоит на самих
    # маршрутах роутера. В `web_router` он не входит намеренно.
    from app.web.routes.api_v1 import router as api_v1_router

    app.include_router(api_v1_router)

    # Прослойка ставится последней и потому оказывается самой внешней: на
    # публичном хосте чужой путь получает 404 до любой проверки пароля.
    app.add_middleware(HostRoutingMiddleware, public_scan_host=public_scan_host)
    return app


app = build_app(settings.app_env, public_scan_host=settings.public_scan_host)
