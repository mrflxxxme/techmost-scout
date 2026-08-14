"""Командная строка: `scout doctor`, `scout run`, `scout serve`, `scout showcase`.

Команда doctor — главный инструмент первого дня: показывает по каждому ключу и
каждому источнику, работает он или нет и почему. С неё начинается разбор любой
проблемы, и ей проверяется, что перенос ключей прошёл успешно.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.llm import CostLimitExceededError, LLMRouter
from app.schemas import Brief, MaturityStage

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


async def _doctor() -> None:
    settings = get_settings()

    # Таблицы создаём до проверок: иначе витрина честно, но бесполезно
    # сообщает «нет таблицы» на чистой машине.
    from app.db import init_db

    await init_db()

    secrets_table = Table(title="Ключи", show_lines=False)
    secrets_table.add_column("Переменная")
    secrets_table.add_column("Статус")
    secrets_table.add_column("Зачем")
    checks = [
        ("GIGACHAT_AUTH_KEY", "gigachat_auth_key", "основная модель, бесплатный тариф"),
        ("DEEPSEEK_API_KEY", "deepseek_api_key", "качественные стадии: карточки, проверка"),
        ("YANDEX_API_KEY", "yandex_api_key", "аварийный резерв модели"),
        ("YANDEX_CATALOG_ID", "yandex_catalog_id", "нужен вместе с ключом Яндекса"),
        ("YANDEX_SEARCH_API_KEY", "yandex_search_api_key", "поиск по вебу РФ (платно)"),
        ("GOOGLE_API_KEY", "google_api_key", "поиск по мировому вебу (100 запросов в сутки бесплатно)"),
        ("GOOGLE_CSE_ID", "google_cse_id", "нужен вместе с ключом Google"),
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
    industry: str = typer.Option(
        None, "--industry", "-i", help="Отрасль заказчика; если не задана, определяется разбором текста"
    ),
    maturity: str = typer.Option(None, "--maturity", "-m", help="lab|prototype|pilot|series"),
    out: Path = typer.Option(None, "--out", "-o", help="Куда положить отчёт (по умолчанию out/)"),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive", help="Задавать уточняющие вопросы"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Прогнать запрос через пайплайн и сохранить отчёт в markdown."""
    _setup_logging(verbose)
    asyncio.run(_run(task, industry, maturity, out, interactive))


async def _run(task: str, industry: str | None, maturity: str | None, out: Path | None, interactive: bool) -> None:
    from app.db import init_db, session_scope
    from app.pipeline import ScoutPipeline
    from app.pipeline.persistence import StageRecord
    from app.pipeline.stages import brief_from_raw_text, refine_brief, run_intake

    settings = get_settings()
    router = LLMRouter(settings)

    # Таблицы для минимальной записи прогона (Ф-8, РЕШ-40); фабрика сессий —
    # `session_scope`, тот же контракт, что и у `app.db.get_session`. Отказ
    # подготовки схемы (файл базы залочен другим процессом и т. п.) не должен
    # ронять прогон ДО построения отчёта: тот же принцип, что и в
    # `persistence.persist_run` — markdown-отчёт первичен, запись в БД
    # вторична. Без готовой схемы запись просто не делаем (session_factory=None).
    db_session_factory = session_scope
    try:
        await init_db()
    except Exception:
        log.warning("не удалось подготовить БД, прогон продолжается без записи", exc_info=True)
        db_session_factory = None

    # Разбор текста нужен в обоих режимах: без него `constraints` брифа
    # остаются пустыми, а именно они дают карточкам сверку с техтребованиями
    # (РЕШ-38). Стадия дешёвая — маршрут `intake` идёт на бесплатный GigaChat.
    #
    # Устойчивость интейка (Т-2): раньше отказ `run_intake`/`refine_brief`
    # убивал прогон целиком ДО пайплайна и не оставлял в БД даже строки
    # `failed` (аудит Ф-10, дважды воспроизведено на GigaChat). Теперь отказ —
    # предупреждение оператору и честный след в `pipeline_logs`, а прогон
    # продолжается до отчёта. Исчерпание лимита стоимости (РЕШ-45) —
    # исключение: это команда «стоп тратить», не отказ модели, и она
    # пробрасывается наружу как есть.
    industry_hint = industry or "не указана"
    if interactive:
        console.print("[bold]Уточняю задачу…[/bold]")

    intake_stage_log: list[StageRecord] = []
    intake_started = datetime.now(UTC)
    try:
        intake = await run_intake(router, task, industry_hint=industry_hint)
    except CostLimitExceededError:
        raise
    except Exception as exc:
        log.warning("интейк не удался, бриф собран из текста задачи без уточнений", exc_info=True)
        console.print("[yellow]Предупреждение:[/yellow] интейк не удался, бриф собран из текста задачи без уточнений")
        intake_stage_log.append(
            StageRecord(stage="intake", started_at=intake_started, finished_at=datetime.now(UTC), error=str(exc))
        )
        intake = None

    brief: Brief
    if intake is None:
        # Черновика нет вовсе — уточняющий диалог провести не из чего,
        # бриф собирается кодом из сырого текста задачи (инвариант №1).
        brief = brief_from_raw_text(task, industry_hint=industry_hint)
    elif interactive:
        answers: dict[str, str] = {}
        for question in intake.questions:
            console.print(f"\n[dim]{question.why}[/dim]")
            answers[question.question] = typer.prompt(question.question, default="не знаю")
        if answers:
            refine_started = datetime.now(UTC)
            try:
                brief = await refine_brief(router, task, answers, industry_hint=industry_hint)
            except CostLimitExceededError:
                raise
            except Exception as exc:
                log.warning(
                    "уточнение брифа не удалось, использую черновик интейка с ответами владельца", exc_info=True
                )
                console.print(
                    "[yellow]Предупреждение:[/yellow] уточнение брифа не удалось, "
                    "использую черновик интейка с вашими ответами"
                )
                # Ответы владельца не теряются: дописываются в constraints
                # черновика в виде «вопрос — ответ» (Т-2).
                qa_notes = [f"{question} — {answer}" for question, answer in answers.items()]
                brief = intake.draft_brief.model_copy(
                    update={"constraints": [*intake.draft_brief.constraints, *qa_notes]}
                )
                intake_stage_log.append(
                    StageRecord(
                        stage="intake", started_at=refine_started, finished_at=datetime.now(UTC), error=str(exc)
                    )
                )
        else:
            brief = intake.draft_brief
    else:
        # Немой режим: вопросы не задаются и не печатаются, бриф берётся как
        # есть из разбора текста.
        brief = intake.draft_brief

    # Явно заданный флаг перекрывает то, что определила модель при разборе.
    if industry:
        brief = brief.model_copy(update={"industry": industry})
    if maturity:
        brief = brief.model_copy(update={"target_maturity": MaturityStage(maturity)})

    console.print(f"\n[bold]Бриф:[/bold] {brief.task_summary} [dim]({brief.industry})[/dim]\n")

    def show(stage: str, message: str, fraction: float) -> None:
        console.print(f"[dim]{int(fraction * 100):3d}%[/dim] [cyan]{stage:8s}[/cyan] {message}")

    pipeline = ScoutPipeline(settings=settings, router=router, progress=show, session_factory=db_session_factory)
    try:
        report = await pipeline.run(brief, raw_text=task, initial_stage_log=intake_stage_log)
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
