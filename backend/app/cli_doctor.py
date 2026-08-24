"""Реализация команды `scout doctor` (Д-4 волны Э2-З, дробление `cli.py`).

Точка входа typer (`app.command()`) осталась в `cli.py` — здесь только тяжёлая
часть: таблица проверяемых ключей, сверка схемы живой базы с моделями и сам
асинхронный обход ключей/моделей/источников.

Три имени — `console`, `get_settings`, `LLMRouter` — намеренно НЕ импортируются
на уровне модуля, а читаются ОТЛОЖЕННЫМ импортом внутри `_doctor` (`from
app.cli import ...`). Причина та же, что в `pipeline/orchestrator.py` (РЕШ-151,
дробление З-10): существующие тесты (`tests/test_cli_doctor_schema_guard.py`,
`tests/test_openalex.py`, `tests/test_secret_leak_guard.py`) подменяют их через
`monkeypatch.setattr(cli, "LLMRouter", ...)` и т. п. — то есть патчат атрибут
модуля `app.cli`, а не `app.cli_doctor`. Импорт внутри функции подхватывает
подменённое значение в момент вызова; импорт на уровне модуля закрепил бы
исходную ссылку намертво и подмена тестов не сработала бы.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
from rich.table import Table

if TYPE_CHECKING:  # только для аннотаций, реального импорта на старте не требуется
    from sqlalchemy.ext.asyncio import AsyncEngine

#: Таблица ключей доктора: (переменная окружения, поле настроек, зачем нужен).
#: Отдельной константой, а не списком внутри `_doctor`: состав таблицы —
#: проверяемое обещание (РЕШ-52.2), а спрятанный в теле функции список
#: проверить нечем, кроме живого запуска.
SECRET_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("DEEPSEEK_API_KEY", "deepseek_api_key", "основная модель для всех стадий (РЕШ-171)"),
    ("YANDEX_API_KEY", "yandex_api_key", "аварийный резерв модели"),
    ("YANDEX_CATALOG_ID", "yandex_catalog_id", "нужен вместе с ключом Яндекса"),
    ("YANDEX_SEARCH_API_KEY", "yandex_search_api_key", "поиск по вебу РФ (платно)"),
    # Google удалён из списка вместе с адаптером (РЕШ-174): Custom Search JSON
    # API закрыт для новых клиентов, доступ владельца получить не удалось,
    # держать в докторе строку про несуществующие настройки незачем — подробности
    # в `docs/sources.md`.
    ("OPENALEX_API_KEY", "openalex_api_key", "необязателен: поиск работает без ключа, нужен обогащению по DOI"),
    ("SEMANTICSCHOLAR_API_KEY", "semanticscholar_api_key", "необязателен, повышает лимиты"),
    ("ROSPATENT_API_KEY", "rospatent_api_key", "поисковая платформа Роспатента (Э2-2)"),
    ("PATENTSVIEW_API_KEY", "patentsview_api_key", "патенты США, ключ обязателен с 05.2025"),
)


async def _schema_drift(engine: AsyncEngine) -> list[str]:
    """Сверить `pragma table_info` живой базы с моделями SQLAlchemy — имена
    колонок и признак `NOT NULL`. Список пуст — схема совпадает.

    Страж закрывает инцидент прогона 1 ворот Э2-Ж: `init_db` — это
    `create_all`, а он не видит расхождений в уже существующей таблице.
    Удаление колонок `tokens_in`/`tokens_out` из модели `pipeline_logs`
    оставило их в файле боевой базы как `NOT NULL` без серверного умолчания —
    каждый `INSERT` без значения для них валил `IntegrityError` и откатывал
    всю транзакцию, а прогон терялся целиком. До этой проверки расхождение
    между файлом базы и моделями не видел никто, включая `doctor`.
    """
    from sqlalchemy import text

    from app.models import Base

    problems: list[str] = []
    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            result = await conn.execute(text(f'PRAGMA table_info("{table.name}")'))
            rows = result.mappings().all()
            if not rows:
                problems.append(f"таблица {table.name}: объявлена в моделях, отсутствует в базе")
                continue
            db_columns = {row["name"]: bool(row["notnull"]) for row in rows}
            model_columns = {column.name: (not column.nullable) for column in table.columns}
            for name in sorted(set(model_columns) - set(db_columns)):
                problems.append(f"{table.name}.{name}: есть в модели, нет в базе")
            for name in sorted(set(db_columns) - set(model_columns)):
                mark = " NOT NULL" if db_columns[name] else ""
                problems.append(f"{table.name}.{name}: есть в базе{mark}, нет в модели")
            for name in sorted(set(model_columns) & set(db_columns)):
                if model_columns[name] != db_columns[name]:
                    problems.append(
                        f"{table.name}.{name}: NOT NULL расходится "
                        f"(модель={'да' if model_columns[name] else 'нет'}, "
                        f"база={'да' if db_columns[name] else 'нет'})"
                    )
    return problems


async def _doctor() -> None:
    """Проверить ключи, схему базы, модели и источники — ничего не меняет.

    `console`, `get_settings`, `LLMRouter` читаются отложенным импортом из
    `app.cli` (см. докстроку модуля) — так тестовые подмены `cli.console` и
    `cli.LLMRouter` доезжают сюда без изменений в самих тестах.
    """
    from app.cli import LLMRouter, console, get_settings

    settings = get_settings()

    # Таблицы создаём до проверок: иначе витрина честно, но бесполезно
    # сообщает «нет таблицы» на чистой машине.
    from app.db import engine as db_engine
    from app.db import init_db

    await init_db()

    console.print("[bold]Проверяю схему базы…[/bold]")
    schema_drift = await _schema_drift(db_engine)
    if schema_drift:
        for problem in schema_drift:
            console.print(f"[red]Схема базы разошлась с моделями:[/red] {problem}")
    else:
        console.print("[green]Схема базы совпадает с моделями.[/green]")

    secrets_table = Table(title="Ключи", show_lines=False)
    secrets_table.add_column("Переменная")
    secrets_table.add_column("Статус")
    secrets_table.add_column("Зачем")
    for env_name, field_name, purpose in SECRET_CHECKS:
        ok = settings.has_secret(field_name)
        secrets_table.add_row(env_name, "[green]есть[/green]" if ok else "[yellow]нет[/yellow]", purpose)
    console.print(secrets_table)

    console.print("\n[bold]Проверяю модели…[/bold]")
    router = LLMRouter(settings)
    llm_report = await router.health()
    llm_table = Table(show_lines=False)
    llm_table.add_column("Провайдер")
    llm_table.add_column("Статус")
    llm_table.add_column("Подробности")
    for name, (ok, detail) in llm_report.items():
        llm_table.add_row(name, "[green]ок[/green]" if ok else "[red]нет[/red]", detail)
    console.print(llm_table)

    routes = settings.stage_routes()
    console.print(f"\n[bold]Маршруты стадий:[/bold] {json.dumps(routes, ensure_ascii=False)}")
    console.print(f"[bold]Резервный порядок:[/bold] {', '.join(settings.fallback_order())}")

    console.print("\n[bold]Проверяю источники…[/bold]")
    from app.search import adapter_status, build_adapters

    # include_stubs=True: доктор показывает ВСЕ источники из реестра (РЕШ-52.1).
    # Источника, которого нет в таблице, нельзя ни проверить, ни починить —
    # именно так заготовки и выключенные без ключа источники были невидимы.
    async with httpx.AsyncClient(
        timeout=settings.search_timeout_seconds,
        headers={"User-Agent": f"TechMostScout/1.0 (mailto:{settings.crossref_mailto})"},
        follow_redirects=True,
    ) as client:
        adapters = build_adapters(client, settings, include_stubs=True)
        # Здоровье спрашиваем только у рабочих: у заготовки спрашивать нечего,
        # а у источника без ключа проверка всё равно упрётся в его отсутствие.
        working = [a for a in adapters if adapter_status(a) is None]
        results = await asyncio.gather(*(a.health() for a in working), return_exceptions=True)

    health_by_name = dict(zip((a.name for a in working), results, strict=True))
    src_table = Table(show_lines=False)
    src_table.add_column("Источник")
    src_table.add_column("Статус")
    src_table.add_column("Подробности")
    for adapter in adapters:
        status = adapter_status(adapter)
        if status is not None:
            label, detail = status
            src_table.add_row(adapter.name, f"[yellow]{label}[/yellow]", detail)
            continue
        outcome = health_by_name[adapter.name]
        if isinstance(outcome, BaseException):
            src_table.add_row(adapter.name, "[red]ошибка[/red]", f"{type(outcome).__name__}: {outcome}")
        else:
            ok, detail = outcome
            src_table.add_row(adapter.name, "[green]ок[/green]" if ok else "[yellow]нет[/yellow]", detail)
    console.print(src_table)

    healthy_llm = [name for name, (ok, _) in llm_report.items() if ok]
    if healthy_llm:
        console.print(f"\n[green]Готово к работе.[/green] Рабочие модели: {', '.join(healthy_llm)}")
    else:
        console.print(
            "\n[red]Ни одна модель не отвечает.[/red] Начните с DEEPSEEK_API_KEY — "
            "основной провайдер (РЕШ-171). См. docs/deps-manual.md"
        )
