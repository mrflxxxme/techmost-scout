"""Командная строка: `scout doctor`, `scout run`, `scout serve`, `scout showcase`.

Команда doctor — главный инструмент первого дня: показывает по каждому ключу и
каждому источнику, работает он или нет и почему. С неё начинается разбор любой
проблемы, и ей проверяется, что перенос ключей прошёл успешно.

Дробление (Д-4 волны Э2-З, по образцу `pipeline/orchestrator.py`, РЕШ-151):
этот файл остаётся точкой входа typer — здесь декларации команд и разделяемые
`app`/`console`/`log`. Тяжёлая реализация уехала рядом: `cli_doctor.py`
(`_doctor`, `_schema_drift`, таблица ключей `SECRET_CHECKS`) и `cli_run.py`
(`_run`, интейк до пайплайна, оговорки клиенту). Оба модуля читают `console`,
`get_settings`, `LLMRouter` ОТЛОЖЕННЫМ импортом (`from app.cli import ...`
внутри своих функций), а не берут их напрямую при загрузке модуля: так
тестовые подмены `monkeypatch.setattr(cli, "LLMRouter", ...)` и подобные
доезжают до места использования, а не остаются висеть на неиспользуемой
ссылке — тот же приём, каким коммит 3891b08 (З-2 Э2-Г) сохранил подмену
`get_settings` при дроблении `stages.py`.

Имена `_doctor`, `_schema_drift`, `_run`, `SECRET_CHECKS`,
`INTAKE_FALLBACK_CAVEAT`, `REFINE_FALLBACK_CAVEAT` импортированы сюда не для
красоты: существующие тесты (`tests/test_cli_run.py`,
`tests/test_cli_doctor_schema_guard.py`, `tests/test_openalex.py`,
`tests/test_pipeline_resilience.py`, `tests/test_run_accounting.py`,
`tests/test_secret_leak_guard.py`) обращаются к ним как к атрибутам модуля
`app.cli` (`cli._run(...)`, `cli._doctor()`, `cli.INTAKE_FALLBACK_CAVEAT`) и
переписывать их при дроблении не входило в границы задачи.
"""

from __future__ import annotations

import asyncio
import csv as csv_module
import logging
from pathlib import Path

import typer
from rich.console import Console

from app.cli_doctor import SECRET_CHECKS, _doctor, _schema_drift
from app.cli_export import _export
from app.cli_run import (
    INTAKE_FALLBACK_CAVEAT,
    REFINE_FALLBACK_CAVEAT,
    STAGE_INTAKE,
    STAGE_INTAKE_REFINE,
    UNKNOWN_ANSWER,
    UNSPECIFIED_REQUIREMENT,
    _run,
)
from app.config import get_settings
from app.llm import LLMRouter

# Реэкспорт без использования ниже — часть публичной поверхности модуля
# (см. докстроку выше и §6 разбора подмен): __all__ фиксирует эти имена как
# сознательно оставленные, а не забытые импорты.
__all__ = [
    "INTAKE_FALLBACK_CAVEAT",
    "REFINE_FALLBACK_CAVEAT",
    "SECRET_CHECKS",
    "STAGE_INTAKE",
    "STAGE_INTAKE_REFINE",
    "UNKNOWN_ANSWER",
    "UNSPECIFIED_REQUIREMENT",
    "LLMRouter",
    "_doctor",
    "_export",
    "_run",
    "_schema_drift",
    "app",
    "console",
    "doctor",
    "export",
    "run",
    "serve",
    "showcase",
]

app = typer.Typer(add_completion=False, help="ТехМост Скаут — подбор научных решений под задачу бизнеса")
console = Console()
log = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, get_settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


# ── doctor ──────────────────────────────────────────────────────────────────
@app.command()
def doctor(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Проверить ключи, модели и источники. Ничего не меняет."""
    _setup_logging(verbose)
    asyncio.run(_doctor())


# ── run ─────────────────────────────────────────────────────────────────────
@app.command()
def run(
    task: str = typer.Argument(..., help="Описание технологической задачи (обезличенное)"),
    industry: str = typer.Option(
        None, "--industry", "-i", help="Отрасль заказчика; если не задана, определяется разбором текста"
    ),
    maturity: str = typer.Option(None, "--maturity", "-m", help="lab|prototype|pilot|series"),
    out: Path = typer.Option(None, "--out", "-o", help="Куда положить отчёт (по умолчанию out/)"),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Задавать уточняющие вопросы"),
    plan_id: int = typer.Option(
        None, "--plan-id", help="Прогнать по сохранённому плану поиска (номер строки search_plans), не строя новый"
    ),
    history_request_id: int = typer.Option(
        None,
        "--history-request-id",
        help=(
            "Учесть кандидатов прошлого прогона (номер строки requests) в накопительном поиске (РЕШ-163) — "
            "явный путь признака «та же задача», когда её пересказали другими словами (РЕШ-166)"
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Прогнать запрос через пайплайн и сохранить отчёт в markdown."""
    _setup_logging(verbose)
    asyncio.run(_run(task, industry, maturity, out, interactive, plan_id, history_request_id))


# ── export ──────────────────────────────────────────────────────────────────
@app.command()
def export(
    request_id: int = typer.Argument(..., help="Номер запроса (requests.id)"),
    pdf: bool = typer.Option(False, "--pdf", help="Выгрузить в PDF"),
    docx: bool = typer.Option(False, "--docx", help="Выгрузить в DOCX"),
    draft: bool = typer.Option(
        False, "--draft", help="Разрешить выгрузку черновика — без валидации эксперта (РЕШ-199)"
    ),
    out: Path = typer.Option(None, "--out", "-o", help="Каталог для файла выгрузки (по умолчанию out/)"),
) -> None:
    """Выгрузить отчёт запроса в PDF или DOCX — ровно один формат за раз."""
    asyncio.run(_export(request_id, pdf=pdf, docx=docx, draft=draft, out=out))


# ── serve ───────────────────────────────────────────────────────────────────
@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Запустить веб-интерфейс."""
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=host, port=port or settings.backend_port, reload=reload)


# ── showcase ────────────────────────────────────────────────────────────────
@app.command()
def showcase(
    csv_path: Path = typer.Argument(..., help="CSV витрины: title,org,contact,maturity_stage,description,tags,url"),
) -> None:
    """Загрузить витрину разработок партнёров из CSV."""
    asyncio.run(_import_showcase(csv_path))


async def _import_showcase(csv_path: Path) -> None:
    from app.db import init_db, session_scope
    from app.models import ShowcaseItem

    await init_db()
    added = 0
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv_module.DictReader(handle))
    async with session_scope() as session:
        for row in rows:
            tags = [t.strip() for t in (row.get("tags") or "").split(";") if t.strip()]
            session.add(
                ShowcaseItem(
                    title=(row.get("title") or "").strip(),
                    org=(row.get("org") or "").strip(),
                    contact=(row.get("contact") or "").strip(),
                    maturity_stage=(row.get("maturity_stage") or "lab").strip(),
                    description=(row.get("description") or "").strip(),
                    tags=tags,
                    url=(row.get("url") or "").strip() or None,
                    source_note=(row.get("source_note") or "").strip(),
                    added_by="cli",
                )
            )
            added += 1
    console.print(f"[green]Добавлено в витрину:[/green] {added}")


if __name__ == "__main__":
    app()
