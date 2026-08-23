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
from typing import TYPE_CHECKING

import httpx
import typer
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.llm import CostLimitExceededError, LLMRouter
from app.schemas import Brief, MaturityStage, OwnerAnswer, SearchPlan

if TYPE_CHECKING:  # только для аннотаций: тяжёлые модули грузятся лениво, внутри команд
    from app.pipeline.persistence import SessionFactory, StageRecord

app = typer.Typer(add_completion=False, help="ТехМост Скаут — подбор научных решений под задачу бизнеса")
console = Console()
log = logging.getLogger(__name__)

#: Умолчание уточняющего диалога: оператор, нажавший Enter, отвечает именно так.
UNKNOWN_ANSWER = "не знаю"

#: Как «не знаю» записывается в бриф (распоряжение владельца, РЕШ-58). Дословное
#: «не знаю» читается как потерянные данные, хотя по смыслу это отсутствие
#: требования к решению; ответ уходит в модель именно в этой формулировке.
UNSPECIFIED_REQUIREMENT = "требование не специализировано"


#: Оговорка клиенту, когда уточняющий диалог не состоялся и бриф собран кодом
#: из текста задачи (РЕШ-58, П-1 аудита). Отказ интейка виден оператору в
#: консоли, в логе и в `pipeline_logs`, но до эксперта и клиента доживает
#: только отчёт — и он об этом молчал, хотя обеднённый бриф объясняет и
#: широкую выдачу, и пустую сверку с требованиями в карточках.
INTAKE_FALLBACK_CAVEAT = (
    "Уточнения по задаче не собраны: бриф составлен из текста задачи. "
    "Выдача может быть шире и менее точной, чем при уточнённой постановке."
)

#: Оговорка клиенту, когда уточняющий диалог состоялся, а свести ответы в
#: постановку не удалось (доводка П-1). Своя формулировка, а не
#: `INTAKE_FALLBACK_CAVEAT`: тот говорит «бриф составлен из текста задачи», а
#: здесь бриф взят из черновика интейка — было бы неправдой. О качестве выдачи
#: не заявляется ничего: ответы владельца доезжают до модели через
#: `owner_answers`, и утверждать, что выдача от этого хуже, нечем.
REFINE_FALLBACK_CAVEAT = (
    "Постановка задачи осталась черновой: ответы на уточняющие вопросы получены "
    "и учтены при подборе, но в постановку не сведены."
)

#: Имена стадий интейка в журнале `pipeline_logs`. Разведены (совет С-2
#: аудита): отказ разбора текста и отказ уточнения брифа — разные инциденты с
#: разной починкой, а одинаковая строка `intake` их не различала. Маршрут
#: модели у обеих стадий один и тот же — `intake` (РЕШ-46), меняются только
#: имена в журнале.
STAGE_INTAKE = "intake"
STAGE_INTAKE_REFINE = "intake_refine"

#: Таблица ключей доктора: (переменная окружения, поле настроек, зачем нужен).
#: Отдельной константой, а не списком внутри `_doctor`: состав таблицы —
#: проверяемое обещание (РЕШ-52.2), а спрятанный в теле функции список
#: проверить нечем, кроме живого запуска.
SECRET_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("GIGACHAT_AUTH_KEY", "gigachat_auth_key", "основная модель, бесплатный тариф"),
    ("DEEPSEEK_API_KEY", "deepseek_api_key", "качественные стадии: карточки, проверка"),
    ("YANDEX_API_KEY", "yandex_api_key", "аварийный резерв модели"),
    ("YANDEX_CATALOG_ID", "yandex_catalog_id", "нужен вместе с ключом Яндекса"),
    ("YANDEX_SEARCH_API_KEY", "yandex_search_api_key", "поиск по вебу РФ (платно)"),
    # Google больше не источник, а закрытая дверь: Custom Search JSON API закрыт
    # для новых клиентов, сам API отключается 01.01.2027 (РЕШ-21), значения в
    # `.env` очищены (РЕШ-32). Прежнее «100 запросов в сутки бесплатно» читалось
    # как рабочая возможность, которой просто не воспользовались, — оператор мог
    # часами добывать ключ, который всё равно отдаёт 403. Код адаптера остаётся
    # (РЕШ-52.4), в таблице источников он виден выключенным.
    ("GOOGLE_API_KEY", "google_api_key", "не используется: доступ закрыт Google, API отключается 01.01.2027"),
    ("GOOGLE_CSE_ID", "google_cse_id", "не используется: шёл в паре с ключом Google, значение очищено"),
    ("OPENALEX_API_KEY", "openalex_api_key", "необязателен: поиск работает без ключа, нужен обогащению по DOI"),
    ("SEMANTICSCHOLAR_API_KEY", "semanticscholar_api_key", "необязателен, повышает лимиты"),
    ("ROSPATENT_API_KEY", "rospatent_api_key", "поисковая платформа Роспатента (Э2-2)"),
    ("PATENTSVIEW_API_KEY", "patentsview_api_key", "патенты США, ключ обязателен с 05.2025"),
)


def _intake_stage_record(
    stage: str,
    *,
    started_at: datetime,
    cost_before: dict[str, float],
    router: LLMRouter,
    error: BaseException | None = None,
) -> StageRecord:
    """Строка журнала стадии интейка — со снимком счётчика.

    Интейк выполняется в `cli._run`, до пайплайна, поэтому снимок счётчика
    вокруг вызова снимается здесь — тем же способом и той же функцией, что и в
    `orchestrator._run_stages` (П-4 аудита). Стадия платная (`intake:deepseek`
    в других конфигурациях маршрутов, РЕШ-46), и её расход — что на отказе,
    что на успехе — обязан попасть в журнал: `complete_json` делает оплаченный
    вызов, затем оплаченный ремонтный при несходимости со схемой, и раньше эта
    трата на удачном пути нигде не оседала (долг 8 аудита Э2-Г: 0,51 ₽,
    5,6% прогона, не были отнесены ни к какой стадии).

    `error=None` — стадия завершилась успешно, запись всё равно нужна: иначе
    построчный расход по журналу не сходится с фактической стоимостью прогона.

    `tokens_in`/`tokens_out` остаются нулями: `CostTracker` копит только рубли,
    вызовы и разбивку по провайдерам — токенов в счётчике нет вовсе, поэтому
    нулевые они и у всех остальных стадий журнала.
    """
    from app.pipeline.orchestrator import cost_delta
    from app.pipeline.persistence import StageRecord

    spent, provider = cost_delta(cost_before, dict(router.cost.by_provider))
    return StageRecord(
        stage=stage,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        provider=provider,
        cost_rub=spent,
        error=str(error) if error is not None else None,
    )


async def _persist_intake_stop(
    session_factory: SessionFactory | None,
    *,
    raw_text: str,
    router: LLMRouter,
    stage_log: list[StageRecord],
    record: StageRecord | None,
    error: BaseException,
) -> None:
    """Записать прогон, оборванный стоп-краном (РЕШ-45) на стадии интейка.

    Стоп-кран не смягчается: исключение уходит наружу, прогон обязан упасть.
    Но след обязан остаться (П-5 аудита) — деньги на неудавшийся вызов уже
    потрачены, а до пайплайна прогон не дошёл, значит `persist_run` иначе не
    вызовет никто: `requests` не получал даже строки `failed`, и трата
    исчезала. Тот же `CostLimitExceededError` внутри `pipeline.run` пишется со
    статусом `failed` и фактической стоимостью — здесь ровно та же запись.

    `record=None` — вызывающий уже дописал свою строку в `stage_log` сам (так
    делает `retry_constraints_if_missing`, у которого свой снимок счётчика
    стоимости вокруг вызова): повторный `append` завёл бы дубль записи об
    одном и том же отказе (ревью, цикл 1, совет 2).
    """
    from app.pipeline.persistence import persist_run

    if record is not None:
        stage_log.append(record)
    await persist_run(
        session_factory,
        raw_text=raw_text,
        status="failed",
        cost_rub=round(router.cost.spent_rub, 4),
        cards=[],
        # Прогон оборвался на интейке — карточек нет ни в одной секции.
        contest_cards=[],
        stages=stage_log,
        error=str(error),
    )


def _normalize_owner_answer(answer: str) -> str:
    """Ответ оператора в том виде, в каком он уходит в бриф и в модель.

    Сравнение по очищенному значению и без учёта регистра: умолчание
    `typer.prompt` оператор может и дописать руками — «Не знаю», «не знаю  ».
    """
    cleaned = answer.strip()
    if cleaned.casefold() == UNKNOWN_ANSWER.casefold():
        return UNSPECIFIED_REQUIREMENT
    return cleaned


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
    plan_id: int = typer.Option(
        None, "--plan-id", help="Прогнать по сохранённому плану поиска (номер строки search_plans), не строя новый"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Прогнать запрос через пайплайн и сохранить отчёт в markdown."""
    _setup_logging(verbose)
    asyncio.run(_run(task, industry, maturity, out, interactive, plan_id))


async def _run(
    task: str,
    industry: str | None,
    maturity: str | None,
    out: Path | None,
    interactive: bool,
    plan_id: int | None = None,
) -> None:
    from app.db import init_db, session_scope
    from app.export.markdown import CONSTRAINTS_UNRESOLVED_NOTICE
    from app.pipeline import ScoutPipeline
    from app.pipeline.constraints_guard import retry_constraints_if_missing
    from app.pipeline.stages import brief_from_raw_text, neutralize_placeholders, refine_brief, run_intake

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

    # Прогон по сохранённому плану (РЕШ-83) — до интейка, то есть до первого
    # платного вызова: неверный номер обязан стоить ноль рублей и оборвать
    # прогон сразу, а не на середине, когда интейк уже оплачен. План читается
    # и валидируется схемой `SearchPlan` здесь же — пайплайн получает готовую
    # схему, а не номер строки.
    preset_plan: SearchPlan | None = None
    if plan_id is not None:
        from app.pipeline.persistence import SearchPlanNotFoundError, load_search_plan

        try:
            preset_plan = await load_search_plan(db_session_factory, plan_id)
        except SearchPlanNotFoundError as exc:
            console.print(f"[red]Прогон не начат:[/red] {exc}")
            raise typer.Exit(code=2) from exc
        console.print(
            f"[bold]План поиска взят из базы:[/bold] №{plan_id}, "
            f"формулировок {len(preset_plan.queries)} — планировщик не вызывается"
        )

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
    intake_cost_before = dict(router.cost.by_provider)
    try:
        intake = await run_intake(router, task, industry_hint=industry_hint)
    except CostLimitExceededError as exc:
        await _persist_intake_stop(
            db_session_factory,
            raw_text=task,
            router=router,
            stage_log=intake_stage_log,
            record=_intake_stage_record(
                STAGE_INTAKE, started_at=intake_started, cost_before=intake_cost_before, router=router, error=exc
            ),
            error=exc,
        )
        raise
    except Exception as exc:
        log.warning("интейк не удался, бриф собран из текста задачи без уточнений", exc_info=True)
        console.print("[yellow]Предупреждение:[/yellow] интейк не удался, бриф собран из текста задачи без уточнений")
        intake_stage_log.append(
            _intake_stage_record(
                STAGE_INTAKE,
                started_at=intake_started,
                cost_before=intake_cost_before,
                router=router,
                error=exc,
            )
        )
        intake = None
    else:
        # Успешный интейк — тоже платный вызов, и его расход обязан попасть
        # в журнал (долг 8 аудита Э2-Г), а не остаться только в счётчике
        # роутера. `else` выполняется исключительно при отсутствии исключения
        # — двойной записи вместе с веткой отказа не будет.
        intake_stage_log.append(
            _intake_stage_record(
                STAGE_INTAKE,
                started_at=intake_started,
                cost_before=intake_cost_before,
                router=router,
            )
        )

    brief: Brief
    intake_caveats: list[str] = []
    if intake is None:
        # Черновика нет вовсе — уточняющий диалог провести не из чего,
        # бриф собирается кодом из сырого текста задачи (инвариант №1).
        # Клиент узнаёт об этом из отчёта, а не только оператор из консоли.
        brief = brief_from_raw_text(task, industry_hint=industry_hint)
        intake_caveats.append(INTAKE_FALLBACK_CAVEAT)
    elif interactive:
        answers: dict[str, str] = {}
        for question in intake.questions:
            console.print(f"\n[dim]{question.why}[/dim]")
            answers[question.question] = _normalize_owner_answer(
                typer.prompt(question.question, default=UNKNOWN_ANSWER)
            )
        if answers:
            refine_started = datetime.now(UTC)
            refine_cost_before = dict(router.cost.by_provider)
            try:
                brief = await refine_brief(router, task, answers, industry_hint=industry_hint)
            except CostLimitExceededError as exc:
                await _persist_intake_stop(
                    db_session_factory,
                    raw_text=task,
                    router=router,
                    stage_log=intake_stage_log,
                    record=_intake_stage_record(
                        STAGE_INTAKE_REFINE,
                        started_at=refine_started,
                        cost_before=refine_cost_before,
                        router=router,
                        error=exc,
                    ),
                    error=exc,
                )
                raise
            except Exception as exc:
                log.warning(
                    "уточнение брифа не удалось, использую черновик интейка с ответами владельца", exc_info=True
                )
                console.print(
                    "[yellow]Предупреждение:[/yellow] уточнение брифа не удалось, "
                    "использую черновик интейка с вашими ответами"
                )
                # Ответы владельца не теряются, но и не выдаются клиенту за
                # ограничения задачи: у них своё поле брифа (РЕШ-58, П-2
                # аудита). `constraints` печатается строкой «Ограничения:» и
                # разворачивается в `requirement_checks` каждой карточки —
                # паре «вопрос — ответ» там не место. До модели ответы доходят
                # через `owner_answers` в BRIEF_JSON и через блок уточнений в
                # сообщении скоринга (`stages.owner_answers_block`).
                brief = intake.draft_brief.model_copy(
                    update={
                        "owner_answers": [
                            *intake.draft_brief.owner_answers,
                            *(OwnerAnswer(question=question, answer=answer) for question, answer in answers.items()),
                        ]
                    }
                )
                intake_stage_log.append(
                    _intake_stage_record(
                        STAGE_INTAKE_REFINE,
                        started_at=refine_started,
                        cost_before=refine_cost_before,
                        router=router,
                        error=exc,
                    )
                )
                # Отказ уточнения тоже доживает до отчёта, а не остаётся в
                # консоли и журнале: черновая постановка объясняет эксперту и
                # клиенту, почему бриф выглядит менее собранным, чем ожидалось.
                intake_caveats.append(REFINE_FALLBACK_CAVEAT)
            else:
                # Успешное уточнение — тоже платный вызов (долг 8 аудита
                # Э2-Г): без этой записи расход по журналу расходился с
                # фактической стоимостью прогона на сумму уточнения.
                intake_stage_log.append(
                    _intake_stage_record(
                        STAGE_INTAKE_REFINE,
                        started_at=refine_started,
                        cost_before=refine_cost_before,
                        router=router,
                    )
                )
        else:
            brief = intake.draft_brief
    else:
        # Немой режим: вопросы не задаются и не печатаются, бриф берётся как
        # есть из разбора текста.
        brief = intake.draft_brief

    # РЕШ-115: непустой текст задачи с признаками требований влечёт непустой
    # constraints либо явную строку клиенту - молчаливого третьего исхода нет.
    # Единственный лишний вызов за прогон, и только если все пути выше
    # оставили constraints пустым (проверка - внутри retry_constraints_if_missing).
    constraints_unresolved = False
    try:
        brief, constraints_unresolved = await retry_constraints_if_missing(
            router, brief, task, industry_hint=industry_hint, stage_log=intake_stage_log
        )
    except CostLimitExceededError as exc:
        # record=None: строка журнала уже дописана внутри
        # retry_constraints_if_missing своим снимком счётчика стоимости -
        # повторный append завёл бы дубль (ревью, цикл 1, совет 2).
        await _persist_intake_stop(
            db_session_factory,
            raw_text=task,
            router=router,
            stage_log=intake_stage_log,
            record=None,
            error=exc,
        )
        raise
    if constraints_unresolved:
        intake_caveats.append(CONSTRAINTS_UNRESOLVED_NOTICE)

    # Явно заданный флаг перекрывает то, что определила модель при разборе.
    # Значение флага — сырой ввод оператора и перекрывает в том числе уже
    # обезвреженную отрасль запасного брифа, поэтому метки `{{...}}` снимаются
    # и здесь: иначе `--industry "{{X}}"` ронял бы прогон на планировщике (П-6).
    if industry:
        brief = brief.model_copy(update={"industry": neutralize_placeholders(industry)})
    if maturity:
        brief = brief.model_copy(update={"target_maturity": MaturityStage(maturity)})

    console.print(f"\n[bold]Бриф:[/bold] {brief.task_summary} [dim]({brief.industry})[/dim]\n")

    def show(stage: str, message: str, fraction: float) -> None:
        console.print(f"[dim]{int(fraction * 100):3d}%[/dim] [cyan]{stage:8s}[/cyan] {message}")

    pipeline = ScoutPipeline(
        settings=settings,
        router=router,
        progress=show,
        session_factory=db_session_factory,
        preset_plan=preset_plan,
    )
    try:
        report = await pipeline.run(
            brief,
            raw_text=task,
            initial_stage_log=intake_stage_log,
            initial_caveats=intake_caveats,
            constraints_unresolved=constraints_unresolved,
        )
    finally:
        await pipeline.aclose()

    from app.export.markdown import render_markdown

    target = out or settings.out_dir / f"report-{report.generated_at:%Y%m%d-%H%M%S}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(report), encoding="utf-8")
    console.print(f"\n[green]Отчёт сохранён:[/green] {target}")
    # Счётчик кэша печатается рядом со стоимостью (Т-7): перенос блоков
    # подстановки в конец промптов (РЕШ-52.6) сделан ради попаданий в кэш
    # префикса, и без этой строки эффект правки на живом прогоне не виден.
    #
    # Оговорка в подписи не косметическая: попадание в кэш выставляет только
    # DeepSeek (поле `prompt_cache_hit_tokens` в ответе). У GigaChat и Yandex
    # `Usage.cached` всегда False, поэтому на прогоне через них строка покажет
    # ноль — и без оговорки читатель решит, что кэш не работает, хотя его просто
    # не измеряют.
    console.print(
        f"[bold]Стоимость запроса:[/bold] {report.cost_rub:.2f} ₽; "
        f"вызовов модели {router.cost.calls}, "
        f"из них с кэшем префикса {router.cost.cached_calls} "
        f"(кэш считается только у DeepSeek; у остальных провайдеров всегда 0)"
    )
    # Расход платных источников — отдельной строкой, а не слагаемым в
    # `report.cost_rub` (Т-18): там рубли моделей, и смешение двух валют в
    # одном поле хуже отсутствия счётчика. Строку собирает сам источник — он
    # один знает свою цену, норму и остаток.
    for note in _paid_source_notes(pipeline):
        console.print(f"[bold]Платные источники:[/bold] {note}")


def _paid_source_notes(pipeline: object) -> list[str]:
    """Строки расхода тех источников, которые умеют его посчитать.

    Утиная проверка, а не список имён: источник, научившийся считать себя,
    начинает печататься сам, и ещё одна правка CLI для этого не нужна.
    """
    notes: list[str] = []
    for adapter in getattr(pipeline, "adapters", []) or []:
        note = getattr(adapter, "search_usage_note", None)
        if callable(note):
            notes.append(str(note()))
    return notes


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
