"""Реализация команды `scout run` (Д-4 волны Э2-З, дробление `cli.py`).

Точка входа typer (`app.command()`) осталась в `cli.py` — здесь только тяжёлая
часть: интерактивный диалог поверх общего сценария интейка и сам асинхронный
`_run`.

Сам сценарий интейка (разбор текста, уточнение, дозаполнение ограничений, след
трат стоп-крана) уехал в `app.pipeline.intake_flow` (РЕШ-184, задача И-4) —
общий модуль для CLI и веба. Здесь остаётся только то, что специфично именно
для командной строки: вопросы оператору через `typer.prompt` и печать в
`console`. Константы и имена стадий (`INTAKE_FALLBACK_CAVEAT`,
`REFINE_FALLBACK_CAVEAT`, `STAGE_INTAKE`, `STAGE_INTAKE_REFINE`) реэкспортированы
из `intake_flow` — `cli.py` читает их как атрибуты `app.cli_run` (см. докстроку
`app.cli`), и переносить эти обращения при дроблении не входило в границы
задачи.

`console`, `get_settings`, `LLMRouter` намеренно НЕ импортируются на уровне
модуля, а читаются ОТЛОЖЕННЫМ импортом внутри `_run` (`from app.cli import
...`) — тем же приёмом, каким `pipeline/orchestrator.py` реэкспортирует имена
для подмодулей (РЕШ-151, дробление З-10). Существующие тесты
(`tests/test_cli_run.py`, `tests/test_openalex.py`,
`tests/test_pipeline_resilience.py`, `tests/test_run_accounting.py`,
`tests/test_secret_leak_guard.py`) подменяют их через
`monkeypatch.setattr(cli, "get_settings", ...)` и т. п. — то есть патчат
атрибут модуля `app.cli`, а не `app.cli_run`. Импорт внутри функции
подхватывает подменённое значение в момент вызова; импорт на уровне модуля
закрепил бы исходную ссылку намертво и подмена тестов не сработала бы.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from app.pipeline.intake_flow import (
    INTAKE_FALLBACK_CAVEAT,
    REFINE_FALLBACK_CAVEAT,
    STAGE_INTAKE,
    STAGE_INTAKE_REFINE,
    refine_brief_stage,
    resolve_constraints_stage,
    run_intake_stage,
)
from app.schemas import Brief, MaturityStage, SearchPlan

if TYPE_CHECKING:  # только для аннотаций: тяжёлые модули грузятся лениво, внутри функций
    from app.pipeline.persistence import StageRecord

log = logging.getLogger(__name__)

# Реэкспорт: `app.cli` импортирует эти имена из `app.cli_run` как часть своей
# публичной поверхности (см. докстроку модуля выше и `app.cli.__all__`) — сами
# значения теперь определены в `intake_flow`, здесь только алиасы.
__all__ = [
    "INTAKE_FALLBACK_CAVEAT",
    "REFINE_FALLBACK_CAVEAT",
    "STAGE_INTAKE",
    "STAGE_INTAKE_REFINE",
    "UNKNOWN_ANSWER",
    "UNSPECIFIED_REQUIREMENT",
    "_run",
]

#: Умолчание уточняющего диалога: оператор, нажавший Enter, отвечает именно так.
UNKNOWN_ANSWER = "не знаю"

#: Как «не знаю» записывается в бриф (распоряжение владельца, РЕШ-58). Дословное
#: «не знаю» читается как потерянные данные, хотя по смыслу это отсутствие
#: требования к решению; ответ уходит в модель именно в этой формулировке.
UNSPECIFIED_REQUIREMENT = "требование не специализировано"


def _normalize_owner_answer(answer: str) -> str:
    """Ответ оператора в том виде, в каком он уходит в бриф и в модель.

    Сравнение по очищенному значению и без учёта регистра: умолчание
    `typer.prompt` оператор может и дописать руками — «Не знаю», «не знаю  ».
    """
    cleaned = answer.strip()
    if cleaned.casefold() == UNKNOWN_ANSWER.casefold():
        return UNSPECIFIED_REQUIREMENT
    return cleaned


async def _run(
    task: str,
    industry: str | None,
    maturity: str | None,
    out: Path | None,
    interactive: bool,
    plan_id: int | None = None,
    history_request_id: int | None = None,
) -> None:
    """Прогнать запрос через пайплайн и сохранить отчёт в markdown.

    `console`, `get_settings`, `LLMRouter` читаются отложенным импортом из
    `app.cli` (см. докстроку модуля) — так тестовые подмены `cli.get_settings`,
    `cli.LLMRouter` и `cli.console` доезжают сюда без изменений в самих тестах.
    """
    from app.cli import LLMRouter, console, get_settings
    from app.db import init_db, session_scope
    from app.export.markdown import CONSTRAINTS_UNRESOLVED_NOTICE
    from app.pipeline import ScoutPipeline
    from app.pipeline.stages import neutralize_placeholders

    settings = get_settings()
    router = LLMRouter(settings)

    # Строка активных параметров печатается здесь, а не только в
    # `ScoutPipeline.run` (З-5, долг ворот Э2-Д): у команды `scout run` интейк
    # живёт РАНЬШЕ пайплайна, и если прогон умрёт на нём (например, на
    # стоп-кране), строка из `run_flow.py` в лог не попадёт вовсе — а разбирать
    # окружение важнее всего именно на упавшем прогоне. Печать в `run_flow.py`
    # намеренно не убрана: тот путь нужен веб-прогону и прямым вызовам
    # пайплайна (тесты, `scout serve`), и туда эта функция не поднимается.
    # Задваивание строки в логе `scout run` — сознательная цена: пропущенная
    # строка стоит разбора вслепую, лишняя не стоит ничего.
    from app.pipeline.orchestrator import format_active_run_params

    log.info(format_active_run_params(settings))

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
    # (РЕШ-38). Маршрут `intake` идёт на DeepSeek (РЕШ-171).
    #
    # Сценарий интейка (разбор текста, уточнение, дозаполнение ограничений,
    # след трат стоп-крана) — общий с вебом модуль `app.pipeline.intake_flow`
    # (РЕШ-184). Здесь остаётся только то, что специфично для CLI: вопросы
    # оператору через `typer.prompt` и печать предупреждений в консоль —
    # сам модуль ни того, ни другого не делает.
    industry_hint = industry or "не указана"
    if interactive:
        console.print("[bold]Уточняю задачу…[/bold]")

    intake_stage_log: list[StageRecord] = []
    intake_outcome = await run_intake_stage(
        router, task, industry_hint=industry_hint, session_factory=db_session_factory, stage_log=intake_stage_log
    )
    intake_caveats: list[str] = list(intake_outcome.caveats)
    if intake_outcome.warning:
        console.print(f"[yellow]Предупреждение:[/yellow] {intake_outcome.warning}")

    brief: Brief
    if intake_outcome.intake is None:
        # Черновика нет вовсе — уточняющий диалог провести не из чего,
        # бриф уже собран `run_intake_stage` кодом из сырого текста задачи
        # (инвариант №1). Клиент узнаёт об этом из отчёта, а не только
        # оператор из консоли.
        brief = intake_outcome.brief
    elif interactive:
        intake = intake_outcome.intake
        answers: dict[str, str] = {}
        for question in intake.questions:
            console.print(f"\n[dim]{question.why}[/dim]")
            answers[question.question] = _normalize_owner_answer(
                typer.prompt(question.question, default=UNKNOWN_ANSWER)
            )
        if answers:
            refine_outcome = await refine_brief_stage(
                router,
                task,
                intake,
                answers,
                industry_hint=industry_hint,
                session_factory=db_session_factory,
                stage_log=intake_stage_log,
            )
            brief = refine_outcome.brief
            intake_caveats.extend(refine_outcome.caveats)
            if refine_outcome.warning:
                console.print(f"[yellow]Предупреждение:[/yellow] {refine_outcome.warning}")
        else:
            brief = intake.draft_brief
    else:
        # Немой режим: вопросы не задаются и не печатаются, бриф берётся как
        # есть из разбора текста.
        brief = intake_outcome.intake.draft_brief

    # РЕШ-115: непустой текст задачи с признаками требований влечёт непустой
    # constraints либо явную строку клиенту - молчаливого третьего исхода нет.
    # Единственный лишний вызов за прогон, и только если все пути выше
    # оставили constraints пустым (проверка - внутри retry_constraints_if_missing).
    brief, constraints_unresolved = await resolve_constraints_stage(
        router, brief, task, industry_hint=industry_hint, session_factory=db_session_factory, stage_log=intake_stage_log
    )
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
    # Именованный аргумент добавляется, только если флаг реально задан: так
    # заглушка пайплайна в тестах, не знающая про накопительный поиск, не
    # ловит TypeError на незнакомом ей keyword-параметре.
    run_kwargs = {"history_request_id": history_request_id} if history_request_id is not None else {}
    try:
        report = await pipeline.run(
            brief,
            raw_text=task,
            initial_stage_log=intake_stage_log,
            initial_caveats=intake_caveats,
            constraints_unresolved=constraints_unresolved,
            **run_kwargs,
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
    # DeepSeek (поле `prompt_cache_hit_tokens` в ответе). У Yandex
    # `Usage.cached` всегда False, поэтому на прогоне через резерв строка
    # покажет ноль — и без оговорки читатель решит, что кэш не работает, хотя
    # его просто не измеряют.
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
