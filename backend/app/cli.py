"""Командная строка: `scout doctor`, `scout run`, `scout serve`, `scout showcase`.

Команда doctor — главный инструмент первого дня: показывает по каждому ключу и
каждому источнику, работает он или нет и почему. С неё начинается разбор любой
проблемы, и ей проверяется, что перенос ключей прошёл успешно.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.llm import LLMRouter
from app.schemas import Brief, MaturityStage

app = typer.Typer(add_completion=False, help="ТехМост Скаут — подбор научных решений под задачу бизнеса")
console = Console()


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


async def _doctor() -> None:
    settings = get_settings()

    secrets_table = Table(title="Ключи", show_lines=False)
    secrets_table.add_column("Переменная")
    secrets_table.add_column("Статус")
    secrets_table.add_column("Зачем")
    checks = [
        ("GIGACHAT_AUTH_KEY", "gigachat_auth_key", "основная модель, бесплатный тариф"),
        ("DEEPSEEK_API_KEY", "deepseek_api_key", "качественные стадии: карточки, проверка"),
        ("YANDEX_API_KEY", "yandex_api_key", "аварийный резерв модели"),
        ("YANDEX_CATALOG_ID", "yandex_catalog_id", "нужен вместе с ключом Яндекса"),
        ("YANDEX_SEARCH_API_KEY", "yandex_search_api_key", "поиск по открытому вебу (платно)"),
        ("OPENALEX_API_KEY", "openalex_api_key", "обогащение по DOI (с 02.2026 ключ обязателен)"),
        ("SEMANTICSCHOLAR_API_KEY", "semanticscholar_api_key", "необязателен, повышает лимиты"),
    ]
    for env_name, field_name, purpose in checks:
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
    from app.search import build_adapters

    async with httpx.AsyncClient(
        timeout=settings.search_timeout_seconds,
        headers={"User-Agent": f"TechMostScout/1.0 (mailto:{settings.crossref_mailto})"},
        follow_redirects=True,
    ) as client:
        adapters = build_adapters(client, settings)
        results = await asyncio.gather(*(a.health() for a in adapters), return_exceptions=True)

    src_table = Table(show_lines=False)
    src_table.add_column("Источник")
    src_table.add_column("Статус")
    src_table.add_column("Подробности")
    for adapter, outcome in zip(adapters, results, strict=True):
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
            "\n[red]Ни одна модель не отвечает.[/red] Начните с GIGACHAT_AUTH_KEY — "
            "у него бесплатный тариф. См. docs/deps-manual.md"
        )


# ── run ─────────────────────────────────────────────────────────────────────
@app.command()
def run(
    task: str = typer.Argument(..., help="Описание технологической задачи (обезличенное)"),
    industry: str = typer.Option("промышленность", "--industry", "-i"),
    maturity: str = typer.Option(None, "--maturity", "-m", help="lab|prototype|pilot|series"),
    out: Path = typer.Option(None, "--out", "-o", help="Куда положить отчёт (по умолчанию out/)"),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Задавать уточняющие вопросы"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Прогнать запрос через пайплайн и сохранить отчёт в markdown."""
    _setup_logging(verbose)
    asyncio.run(_run(task, industry, maturity, out, interactive))


async def _run(task: str, industry: str, maturity: str | None, out: Path | None, interactive: bool) -> None:
    from app.pipeline import ScoutPipeline
    from app.pipeline.stages import refine_brief, run_intake

    settings = get_settings()
    router = LLMRouter(settings)

    brief: Brief
    if interactive:
        console.print("[bold]Уточняю задачу…[/bold]")
        intake = await run_intake(router, task)
        answers: dict[str, str] = {}
        for question in intake.questions:
            console.print(f"\n[dim]{question.why}[/dim]")
            answers[question.question] = typer.prompt(question.question, default="не знаю")
        brief = await refine_brief(router, task, answers) if answers else intake.draft_brief
    else:
        brief = Brief(
            industry=industry,
            task_summary=task,
            target_maturity=MaturityStage(maturity) if maturity else None,
        )

    console.print(f"\n[bold]Бриф:[/bold] {brief.task_summary} [dim]({brief.industry})[/dim]\n")

    def show(stage: str, message: str, fraction: float) -> None:
        console.print(f"[dim]{int(fraction * 100):3d}%[/dim] [cyan]{stage:8s}[/cyan] {message}")

    pipeline = ScoutPipeline(settings=settings, router=router, progress=show)
    try:
        report = await pipeline.run(brief)
    finally:
        await pipeline.aclose()

    from app.export.markdown import render_markdown

    target = out or settings.out_dir / f"report-{report.generated_at:%Y%m%d-%H%M%S}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(report), encoding="utf-8")
    console.print(f"\n[green]Отчёт сохранён:[/green] {target}")
    console.print(f"[bold]Стоимость запроса:[/bold] {report.cost_rub:.2f} ₽ за {router.cost.calls} вызовов модели")


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
    import csv as csv_module

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
