"""Страж схемы в `scout doctor` (Д-3 волны Э2-З).

Замок на инцидент прогона 1 ворот Э2-Ж: `init_db` — это `create_all`, а он не
видит расхождений в уже существующей таблице. Удаление колонок
`tokens_in`/`tokens_out` из модели `pipeline_logs` оставило их в файле боевой
базы как `NOT NULL` без серверного умолчания — каждый `INSERT` без значения
для них валил `IntegrityError` и откатывал всю транзакцию, а прогон терялся
целиком. До этой задачи расхождение между файлом базы и моделями не видел
никто, включая `doctor`.

Сети нет (инвариант №7): проверка идёт на временном файле SQLite, ничего из
боевой `data/scout.db` не задевается.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import cli
from app.models import Base


@pytest.fixture
async def clean_engine(tmp_path):
    """Временный файл SQLite со схемой ровно такой, какую строят модели."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'schema-guard.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_чистая_схема_не_даёт_расхождений(clean_engine):
    """Свежесозданная схема — ровно та, что строят модели: расхождений нет."""
    problems = await cli._schema_drift(clean_engine)

    assert problems == []


async def test_лишняя_not_null_колонка_без_соответствия_в_модели_ловится(clean_engine):
    """Инцидент дословно: колонка `NOT NULL` есть в файле базы, а в модели её
    уже нет — каждый `INSERT` без значения для неё валит `IntegrityError`."""
    async with clean_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE pipeline_logs ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0"))

    problems = await cli._schema_drift(clean_engine)

    assert any("pipeline_logs.tokens_in" in p and "NOT NULL" in p for p in problems), problems


async def test_отсутствующая_в_базе_колонка_модели_ловится(clean_engine):
    """Обратный случай: колонка объявлена в модели, но в файле базы её нет —
    например, модель обзавелась новой колонкой, а `create_all` в уже
    существующую таблицу её не дописал (та же болезнь, что и с лишней)."""
    async with clean_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE candidates DROP COLUMN snippet"))

    problems = await cli._schema_drift(clean_engine)

    assert any("candidates.snippet" in p and "нет в базе" in p for p in problems), problems


async def test_doctor_на_битой_схеме_печатает_внятную_красную_строку(monkeypatch, clean_engine):
    """Критерий задачи: `scout doctor` на битой схеме говорит внятно, а не
    молчит. Роутер моделей и реестр источников подменены заглушками — цель
    проверки только строка о схеме (тот же приём, что и в
    `tests/test_secret_leak_guard.py::_run_doctor_with_fake_keys`)."""
    async with clean_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE pipeline_logs ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0"))

    import app.db

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(app.db, "init_db", fake_init_db)
    monkeypatch.setattr(app.db, "engine", clean_engine)

    class _FakeRouter:
        def __init__(self, settings) -> None:
            del settings

        async def health(self) -> dict[str, tuple[bool, str]]:
            return {"deepseek": (True, "заглушка")}

    monkeypatch.setattr(cli, "LLMRouter", _FakeRouter)
    monkeypatch.setattr("app.search.build_adapters", lambda client, settings, **kwargs: [])

    recorder = Console(file=io.StringIO(), width=200, record=True)
    monkeypatch.setattr(cli, "console", recorder)

    await cli._doctor()

    printed = recorder.export_text()
    assert "Схема базы разошлась с моделями" in printed, "доктор обязан назвать расхождение внятной строкой"
    assert "pipeline_logs.tokens_in" in printed, "строка обязана называть конкретную таблицу и колонку"


async def test_doctor_на_целой_схеме_подтверждает_совпадение(monkeypatch, clean_engine):
    """Обратная сторона: без расхождений `doctor` говорит об этом явно, а не
    молчит — иначе оператор не может отличить «проверено и всё хорошо» от
    «проверка не запустилась»."""
    import app.db

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(app.db, "init_db", fake_init_db)
    monkeypatch.setattr(app.db, "engine", clean_engine)

    class _FakeRouter:
        def __init__(self, settings) -> None:
            del settings

        async def health(self) -> dict[str, tuple[bool, str]]:
            return {"deepseek": (True, "заглушка")}

    monkeypatch.setattr(cli, "LLMRouter", _FakeRouter)
    monkeypatch.setattr("app.search.build_adapters", lambda client, settings, **kwargs: [])

    recorder = Console(file=io.StringIO(), width=200, record=True)
    monkeypatch.setattr(cli, "console", recorder)

    await cli._doctor()

    printed = recorder.export_text()
    assert "Схема базы совпадает с моделями" in printed
    assert "разошлась" not in printed
