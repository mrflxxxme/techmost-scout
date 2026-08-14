"""Проверки `scout run`: интейк вызывается в обоих режимах, ограничения из
разбора доходят до брифа, `--industry` перекрывает то, что определила модель.

Эти три дефекта нашёл четвёртый живой прогон: в немом режиме бриф собирался
без разбора текста (`constraints` оставались пустыми — они дают карточкам
сверку с техтребованиями, РЕШ-38), `--industry` в интерактивной ветке не
использовался вовсе, а нулевой список вопросов диалог не запускал.

Отдельно (Т-2, устойчивость интейка): отказ `run_intake` не роняет прогон —
бриф собирается кодом из сырого текста; проверены обе границы этой сборки —
обрезка текста длиннее 500 знаков с многоточием без висящего пробела и
запасное «не указано» на пустом либо целиком пробельном тексте.

Сеть не используется: `run_intake`/`refine_brief` подменены заглушками,
`ScoutPipeline` — тоже, чтобы тест проверял только сборку брифа, а не весь
пайплайн поиска.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import app.cli as cli
import app.pipeline as pipeline_pkg
from app.llm.router import LLMRouter
from app.schemas import Brief, IntakeQuestion, IntakeResult, Report, ReportSummary


class _RecordingPipeline:
    """Пайплайн-заглушка: не ходит ни в источники, ни в модель — только
    запоминает бриф, с которым его вызвали, и отдаёт минимальный отчёт."""

    def __init__(self, *, settings=None, router=None, adapters=None, progress=None, session_factory=None) -> None:
        del settings, router, adapters, progress
        _RecordingPipeline.session_factories.append(session_factory)

    async def run(
        self,
        brief: Brief,
        *,
        request_id: int = 0,
        raw_text: str = "",
        initial_stage_log: list | None = None,
        initial_caveats: list | None = None,
    ) -> Report:
        _RecordingPipeline.captured.append(brief)
        _RecordingPipeline.initial_stage_logs.append(initial_stage_log)
        _RecordingPipeline.initial_caveats.append(initial_caveats)
        return Report(
            request_id=request_id,
            brief=brief,
            cards=[],
            summary=ReportSummary(recommendation="тест: пайплайн подменён заглушкой"),
            generated_at=datetime.now(UTC),
        )

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _patch_pipeline(monkeypatch):
    """Подменяет тяжёлый пайплайн на запись брифа: тест проверяет только интейк."""
    _RecordingPipeline.captured = []
    _RecordingPipeline.session_factories = []
    _RecordingPipeline.initial_stage_logs = []
    _RecordingPipeline.initial_caveats = []
    monkeypatch.setattr(pipeline_pkg, "ScoutPipeline", _RecordingPipeline)
    return _RecordingPipeline


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch, settings):
    """`_run` берёт настройки через `get_settings()` — отдаём фикстуру без сети и без .env."""
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


@pytest.fixture(autouse=True)
def _patch_init_db(monkeypatch):
    """`_run` готовит схему БД (Ф-8) перед пайплайном — здесь пайплайн подменён
    заглушкой, которая `session_factory` не использует, поэтому реальная схема
    не нужна вовсе. Заглушка не даёт тестам CLI трогать боевой data/scout.db
    рабочей копии (ревью, цикл 2: изоляция тестов)."""
    calls: list[None] = []

    async def fake_init_db() -> None:
        calls.append(None)

    monkeypatch.setattr("app.db.init_db", fake_init_db)
    return calls


def _router(settings, fake_provider_factory) -> LLMRouter:
    return LLMRouter(settings=settings, providers={"gigachat": fake_provider_factory("gigachat")})


def _draft_brief(*, industry: str, constraints: list[str] | None = None) -> Brief:
    return Brief(
        industry=industry,
        task_summary="Катализатор гидроочистки теряет активность раньше срока.",
        constraints=constraints or [],
    )


def _patch_llm_router(monkeypatch, settings, fake_provider_factory) -> None:
    router = _router(settings, fake_provider_factory)
    monkeypatch.setattr(cli, "LLMRouter", lambda _settings: router)


async def test_no_interactive_calls_run_intake_once_with_constraints(
    monkeypatch, settings, fake_provider_factory, tmp_path
):
    """Немой режим: `run_intake` вызывается ровно один раз, `constraints` из разбора доходят до брифа."""
    _patch_llm_router(monkeypatch, settings, fake_provider_factory)
    calls = []

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        calls.append((task, industry_hint))
        return IntakeResult(
            questions=[],
            draft_brief=_draft_brief(industry="нефтепереработка", constraints=["работа при 340 °C"]),
        )

    async def fail_refine_brief(*args, **kwargs):
        raise AssertionError("refine_brief не должен вызываться в немом режиме")

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fail_refine_brief)

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", False)

    assert len(calls) == 1, "run_intake обязан вызываться ровно один раз в немом режиме"
    assert len(_RecordingPipeline.captured) == 1
    assert _RecordingPipeline.captured[0].constraints == ["работа при 340 °C"]


@pytest.mark.parametrize("interactive", [True, False])
async def test_industry_flag_overrides_parsed_industry(
    monkeypatch, settings, fake_provider_factory, interactive, tmp_path
):
    """Явно заданный `--industry` перекрывает отрасль, которую определил разбор — в обоих режимах."""
    _patch_llm_router(monkeypatch, settings, fake_provider_factory)

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(questions=[], draft_brief=_draft_brief(industry="из разбора"))

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    await cli._run("сырой текст задачи", "автопром", None, tmp_path / "report.md", interactive)

    assert _RecordingPipeline.captured[0].industry == "автопром"


async def test_interactive_calls_refine_brief_when_questions_present(
    monkeypatch, settings, fake_provider_factory, tmp_path
):
    """Непустой список вопросов — интерактивная ветка обязана вызвать `refine_brief`."""
    _patch_llm_router(monkeypatch, settings, fake_provider_factory)
    refine_calls = []

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(
            questions=[IntakeQuestion(question="Какая температура?", why="нужна для отбора")],
            draft_brief=_draft_brief(industry="нефтепереработка"),
        )

    async def fake_refine_brief(router, task, answers, *, industry_hint="не указана"):
        refine_calls.append(answers)
        return _draft_brief(industry="нефтепереработка", constraints=["уточнено диалогом"])

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fake_refine_brief)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "380 °C")

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", True)

    assert len(refine_calls) == 1, "refine_brief обязан вызываться при непустом списке вопросов"
    assert _RecordingPipeline.captured[0].constraints == ["уточнено диалогом"]


async def test_interactive_skips_refine_brief_when_no_questions(monkeypatch, settings, fake_provider_factory, tmp_path):
    """Пустой список вопросов — интерактивная ветка не вызывает `refine_brief`, берёт черновик как есть."""
    _patch_llm_router(monkeypatch, settings, fake_provider_factory)

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(questions=[], draft_brief=_draft_brief(industry="нефтепереработка"))

    async def fail_refine_brief(*args, **kwargs):
        raise AssertionError("refine_brief не должен вызываться при пустом списке вопросов")

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fail_refine_brief)

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", True)

    assert _RecordingPipeline.captured[0].industry == "нефтепереработка"


# ── Ревью, цикл 2: отказ init_db не роняет прогон ────────────────────────────
async def test_init_db_failure_does_not_crash_run_and_report_is_built(
    monkeypatch, settings, fake_provider_factory, tmp_path
):
    """`init_db` бросает (типовой случай SQLite — база залочена другим
    процессом) → прогон всё равно завершается, отчёт строится и сохраняется,
    исключение наружу не летит; пайплайн получает `session_factory=None`
    (без готовой схемы запись не делаем — тот же принцип, что в
    `persistence.persist_run`: markdown-отчёт первичен)."""
    _patch_llm_router(monkeypatch, settings, fake_provider_factory)

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(questions=[], draft_brief=_draft_brief(industry="нефтепереработка"))

    async def broken_init_db() -> None:
        raise RuntimeError("база залокирована другим процессом")

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.db.init_db", broken_init_db)

    report_path = tmp_path / "report.md"
    await cli._run("сырой текст задачи", None, None, report_path, False)

    assert len(_RecordingPipeline.captured) == 1
    assert _RecordingPipeline.session_factories[-1] is None
    assert report_path.exists()


# ── Т-2: отказ run_intake не роняет прогон ───────────────────────────────────
async def test_run_intake_failure_falls_back_to_brief_from_raw_text_and_warns(
    monkeypatch, settings, fake_provider_factory, tmp_path, capsys
):
    """`run_intake` бросает исключение → прогон всё равно доходит до отчёта,
    бриф собирается кодом из сырого текста задачи (без вызова модели), а
    оператор видит предупреждение в консоли. `refine_brief` в этом сценарии
    вызываться не должен — черновика для диалога нет."""
    _patch_llm_router(monkeypatch, settings, fake_provider_factory)

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    async def fail_refine_brief(*args, **kwargs):
        raise AssertionError("refine_brief не должен вызываться, если run_intake отказал")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fail_refine_brief)

    report_path = tmp_path / "report.md"
    await cli._run("сырой текст задачи про катализатор", None, None, report_path, False)

    assert len(_RecordingPipeline.captured) == 1
    brief = _RecordingPipeline.captured[0]
    assert brief.task_summary == "сырой текст задачи про катализатор"
    assert brief.industry == "не указана"
    assert report_path.exists()

    stage_log = _RecordingPipeline.initial_stage_logs[-1]
    assert stage_log is not None and len(stage_log) == 1
    assert stage_log[0].stage == "intake"
    assert stage_log[0].error

    console_output = capsys.readouterr().out
    assert "интейк не удался" in console_output

    # П-1: консоль, лог и pipeline_logs видит только оператор — до эксперта и
    # клиента доживает отчёт, поэтому оговорка уходит и в него.
    assert _RecordingPipeline.initial_caveats[-1] == [cli.INTAKE_FALLBACK_CAVEAT]


async def test_successful_intake_adds_no_caveat_to_report(monkeypatch, settings, fake_provider_factory, tmp_path):
    """Обратная сторона П-1: когда интейк отработал, оговорке в отчёте взяться
    неоткуда — иначе клиент читал бы предупреждение о качестве выдачи на
    каждом здоровом прогоне."""
    _patch_llm_router(monkeypatch, settings, fake_provider_factory)

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return IntakeResult(questions=[], draft_brief=_draft_brief(industry="нефтепереработка"))

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    await cli._run("сырой текст задачи", None, None, tmp_path / "report.md", False)

    assert _RecordingPipeline.initial_caveats[-1] == []


# ── Т-2, цикл 2: границы запасного брифа ─────────────────────────────────────
#: Фраза ровно в 50 знаков, последний из них — пробел. При повторении знак с
#: индексом 499 (тот, на котором обрывается срез `[:500]`) всегда оказывается
#: пробелом, а знак 498 — точкой. Такая геометрия и удерживает `rstrip()`:
#: без него перед многоточием остался бы висящий пробел, а длина стала бы 501
#: вместо 500.
_PHRASE_50 = "Катализаторы гидроочистки рано теряют активность. "

#: Заведомо длиннее предела `_FALLBACK_SUMMARY_LIMIT = 500`: 600 знаков.
_LONG_TASK_TEXT = _PHRASE_50 * 12


async def test_long_raw_text_is_truncated_at_limit_without_dangling_space(
    monkeypatch, settings, fake_provider_factory, tmp_path
):
    """Отказ интейка на длинном тексте: запасной бриф обрезает сырой текст по
    пределу в 500 знаков и ставит многоточие, не оставляя перед ним висящего
    пробела.

    Вход — 600 знаков, у которых знак 499 (граница среза) пробельный, а 498 —
    точка. Ожидаемый `task_summary` — ровно 500 знаков: 499 знаков текста плюс
    многоточие. Пропавший `rstrip()` дал бы 501 знак с пробелом перед
    многоточием, сдвинутый срез — другую длину; обе регрессии тест ловит."""
    assert len(_PHRASE_50) == 50, "геометрия теста сломана: фраза обязана быть ровно 50 знаков"
    assert len(_LONG_TASK_TEXT) == 600
    assert _LONG_TASK_TEXT[499] == " ", "на границе среза обязан стоять пробел — иначе rstrip не проверяется"
    assert _LONG_TASK_TEXT[498] != " "

    _patch_llm_router(monkeypatch, settings, fake_provider_factory)

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)

    await cli._run(_LONG_TASK_TEXT, None, None, tmp_path / "report.md", False)

    summary = _RecordingPipeline.captured[0].task_summary
    assert summary.endswith("…"), "обрезанный текст обязан оканчиваться многоточием"
    assert len(summary) == 500, "499 знаков текста плюс многоточие"
    assert summary[-2] != " ", "перед многоточием не должно быть висящего пробела (за это отвечает rstrip)"
    assert summary[:-1] == _LONG_TASK_TEXT[:499], "обрезка обязана быть началом исходного текста"


@pytest.mark.parametrize("raw_text", ["", "   \n\t  "])
async def test_empty_raw_text_falls_back_to_placeholder_summary(
    monkeypatch, settings, fake_provider_factory, tmp_path, raw_text
):
    """Отказ интейка на пустом либо целиком пробельном тексте: `task_summary`
    получает запасное значение «не указано», а не пустую строку — пустая суть
    задачи в отчёте выглядит как потерянные данные (инвариант №1: код ничего
    не домысливает, но и не молчит)."""
    _patch_llm_router(monkeypatch, settings, fake_provider_factory)

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)

    await cli._run(raw_text, None, None, tmp_path / "report.md", False)

    assert _RecordingPipeline.captured[0].task_summary == "не указано"
