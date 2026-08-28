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

log = logging.getLogger(__name__)
settings = get_settings()

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


app = FastAPI(
    title="ТехМост Скаут",
    description="Подбор научно-технических решений под задачу бизнеса",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

static_dir = WEB_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Проверка живости для прокси и мониторинга. Без авторизации."""
    return {"status": "ok", "env": settings.app_env}


# Маршруты интерфейса и API живут в web/routes.py, чтобы main.py оставался
# точкой сборки, а не свалкой обработчиков.
from app.web.routes import router as web_router  # noqa: E402 — после создания app

# Один общий вход на всех маршрутах, кроме /health (РЕШ-16): защита живёт в
# приложении, пароль на Caddy при деплое — второй рубеж, а не единственный.
app.include_router(web_router, dependencies=[Depends(require_auth)])
