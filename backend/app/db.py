"""Подключение к базе.

MVP живёт на SQLite: одного файла достаточно для десятков запросов в месяц и
он тривиально копируется в бэкап. Переход на Postgres — замена DATABASE_URL,
код менять не придётся.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base

_settings = get_settings()

# Для SQLite убеждаемся, что каталог существует, иначе engine упадёт при первой записи.
if _settings.database_url.startswith("sqlite"):
    db_path = _settings.database_url.split("///", 1)[-1]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(_settings.database_url, echo=False, future=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Создать таблицы. Миграции в MVP не нужны — схема ещё меняется свободно."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия с автоматическим коммитом и откатом при ошибке."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI."""
    async with session_scope() as session:
        yield session
