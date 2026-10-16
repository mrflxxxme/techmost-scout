"""Подключение к базе.

MVP живёт на SQLite: одного файла достаточно для десятков запросов в месяц и
он тривиально копируется в бэкап. Переход на Postgres — замена DATABASE_URL,
код менять не придётся.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base

log = logging.getLogger(__name__)
_settings = get_settings()

# Для SQLite убеждаемся, что каталог существует, иначе engine упадёт при первой записи.
if _settings.database_url.startswith("sqlite"):
    db_path = _settings.database_url.split("///", 1)[-1]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(_settings.database_url, echo=False, future=True)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

#: Колонки, добавленные в уже существующие таблицы после первого боевого
#: развёртывания (РЕШ-250). `create_all` новую таблицу заведёт, а колонку в
#: существующую таблицу не допишет (грабля Э2-А) — без этого списка боевая
#: база падала бы на первом `SELECT` по модели. Список явный, а не «всё, чего
#: нет в базе»: дописываются только nullable-колонки, заведённые осознанно;
#: удаление и смена типа колонок по-прежнему ручная работа с копией базы.
ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("briefs", "region_code"),
    ("briefs", "need_id"),
    ("briefs", "mode"),
    ("requests", "api_client"),
)


def add_missing_columns(conn: Connection) -> list[str]:
    """Дописать в существующие таблицы колонки из `ADDED_COLUMNS`, которых там нет.

    Возвращает имена дописанных колонок вида `таблица.колонка` — пустой
    список, если схема уже догнана. Колонка обязана быть nullable: `ALTER
    TABLE ADD COLUMN` без умолчания на непустой таблице иначе невозможен.
    """
    inspector = inspect(conn)
    added: list[str] = []
    for table_name, column_name in ADDED_COLUMNS:
        present = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name in present:
            continue
        column = Base.metadata.tables[table_name].c[column_name]
        if not column.nullable:
            raise RuntimeError(f"{table_name}.{column_name}: дописать можно только nullable-колонку")
        ddl_type = column.type.compile(dialect=conn.dialect)
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type}"))
        added.append(f"{table_name}.{column_name}")
    return added


async def init_db() -> None:
    """Создать таблицы и догнать колонки `ADDED_COLUMNS` в уже существующей базе."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(add_missing_columns)
    if added:
        log.info("схема базы догнана: дописаны колонки %s", ", ".join(added))


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
