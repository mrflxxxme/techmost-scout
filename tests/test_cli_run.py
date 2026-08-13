"""Проверки `scout run`: интейк вызывается в обоих режимах, ограничения из
разбора доходят до брифа, `--industry` перекрывает то, что определила модель.

Эти три дефекта нашёл четвёртый живой прогон: в немом режиме бриф собирался
без разбора текста (`constraints` оставались пустыми — они дают карточкам
сверку с техтребованиями, РЕШ-38), `--industry` в интерактивной ветке не
использовался вовсе, а нулевой список вопросов диалог не запускал.

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
        del settings, router, adapters, progress, session_factory

    async def run(self, brief: Brief, *, request_id: int = 0, raw_text: str = "") -> Report:
        _RecordingPipeline.captured.append(brief)
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
    monkeypatch.setattr(pipeline_pkg, "ScoutPipeline", _RecordingPipeline)
    return _RecordingPipeline


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch, settings):
    """`_run` берёт настройки через `get_settings()` — отдаём фикстуру без сети и без .env."""
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


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
