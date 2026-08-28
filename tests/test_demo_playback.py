"""Демо-воспроизведение журнала стадий (Э4-И7, SPEC Э4-3, РЕШ-200/РЕШ-201).

Чистые функции над вручную сконструированными строками журнала (структурный
протокол `PipelineLogEntry` — без ORM и без базы, инвариант №7). Проверяется
пропорциональное сжатие задержек («Темп воспроизведения»), неубывающие доли,
честный финальный шаг без суммы стоимости (РЕШ-188) и устойчивость сборки к
вырожденным журналам (одна строка, нулевые длительности, пустой журнал).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.pipeline.demo import DONE_MESSAGE, PlaybackStep, build_playback, play
from app.web.progress import ProgressBroker

_START = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)


@dataclass(slots=True)
class _LogRow:
    """Заглушка строки `pipeline_logs` — только поля, нужные воспроизведению
    (структурно совпадает с протоколом `PipelineLogEntry`, без ORM)."""

    stage: str
    started_at: datetime
    finished_at: datetime | None
    sources_hit: dict[str, int]


def _row(
    stage: str,
    *,
    start_offset: float,
    duration: float | None,
    sources_hit: dict[str, int] | None = None,
) -> _LogRow:
    started = _START + timedelta(seconds=start_offset)
    finished = started + timedelta(seconds=duration) if duration is not None else None
    return _LogRow(stage=stage, started_at=started, finished_at=finished, sources_hit=sources_hit or {})


# ── Критерий 1: пропорции реальных длительностей, сумма задержек, финал ──────


def test_delays_scaled_proportionally_to_real_durations():
    """10/20/70 с при total_seconds=150 → задержки 15/30/105 с (±0.5 с)."""
    logs = [
        _row("planner", start_offset=0, duration=10),
        _row("search", start_offset=10, duration=20, sources_hit={"crossref": 5, "arxiv": 3}),
        _row("scorer", start_offset=30, duration=70),
    ]

    steps = build_playback(logs, total_seconds=150.0)

    real_steps = steps[:-1]
    assert [s.stage for s in real_steps] == ["planner", "search", "scorer"]
    assert real_steps[0].delay_seconds == pytest.approx(15.0, abs=0.5)
    assert real_steps[1].delay_seconds == pytest.approx(30.0, abs=0.5)
    assert real_steps[2].delay_seconds == pytest.approx(105.0, abs=0.5)

    total_delay = sum(s.delay_seconds for s in steps)
    assert total_delay == pytest.approx(150.0, abs=1.0)


def test_fractions_do_not_decrease_and_last_step_is_done_without_cost():
    logs = [
        _row("planner", start_offset=0, duration=10),
        _row("search", start_offset=10, duration=20),
        _row("scorer", start_offset=30, duration=70),
    ]

    steps = build_playback(logs, total_seconds=150.0)

    fractions = [s.fraction for s in steps]
    assert fractions == sorted(fractions)
    assert steps[-1] == PlaybackStep(stage="done", message=DONE_MESSAGE, fraction=1.0, delay_seconds=0.0)
    assert "стоимост" not in steps[-1].message.lower()
    assert "₽" not in steps[-1].message


def test_only_final_done_step_reaches_full_fraction():
    """`ProgressBroker.subscribe` закрывает поток на первом `fraction >= 1.0`
    (`app/web/progress.py`) — если бы это была последняя реальная стадия, а
    не `done`, клиент не увидел бы финального события вовсе."""
    logs = [
        _row("planner", start_offset=0, duration=10),
        _row("search", start_offset=10, duration=20),
        _row("scorer", start_offset=30, duration=70),
    ]

    steps = build_playback(logs, total_seconds=150.0)

    assert all(step.fraction < 1.0 for step in steps[:-1])
    assert steps[-1].fraction == 1.0


def test_search_message_uses_real_sources_hit_numbers_not_invented():
    """Инвариант №1 распространяется на показ: число — сумма `sources_hit`."""
    logs = [_row("search", start_offset=0, duration=10, sources_hit={"crossref": 4, "arxiv": 2})]

    steps = build_playback(logs, total_seconds=30.0)

    assert "6" in steps[0].message


# ── Критерий 1: вырожденные журналы не роняют сборку ─────────────────────────


def test_single_row_journal_does_not_break_build():
    logs = [_row("cards", start_offset=0, duration=42)]

    steps = build_playback(logs, total_seconds=60.0)

    assert len(steps) == 2  # сама стадия + done
    assert steps[0].delay_seconds == pytest.approx(60.0, abs=0.5)
    assert steps[-1].stage == "done"


def test_zero_duration_journal_does_not_break_build():
    """Стадии длились ноль секунд (или не завершились) — время делится поровну."""
    logs = [
        _row("planner", start_offset=0, duration=0),
        _row("search", start_offset=0, duration=None),
    ]

    steps = build_playback(logs, total_seconds=90.0)

    assert len(steps) == 3
    assert steps[0].delay_seconds == pytest.approx(45.0, abs=0.5)
    assert steps[1].delay_seconds == pytest.approx(45.0, abs=0.5)
    total_delay = sum(s.delay_seconds for s in steps)
    assert total_delay == pytest.approx(90.0, abs=1.0)


def test_empty_journal_returns_empty_list():
    """Пустой журнал — забота вызывающего маршрута (честное сообщение), не этой функции."""
    assert build_playback([], total_seconds=150.0) == []


def test_unknown_stage_gets_fallback_message_without_crash():
    logs = [_row("future_stage", start_offset=0, duration=5)]

    steps = build_playback(logs, total_seconds=10.0)

    assert "future_stage" in steps[0].message


def test_logs_are_ordered_by_started_at_regardless_of_input_order():
    """Порядок в списке не гарантирован вызывающим кодом — функция сортирует сама."""
    logs = [
        _row("scorer", start_offset=30, duration=70),
        _row("planner", start_offset=0, duration=10),
        _row("search", start_offset=10, duration=20),
    ]

    steps = build_playback(logs, total_seconds=150.0)

    assert [s.stage for s in steps] == ["planner", "search", "scorer", "done"]


# ── Критерий 2 (часть): play публикует шаги и ждёт между ними ────────────────


async def test_play_publishes_all_steps_in_order():
    broker = ProgressBroker()
    steps = [
        PlaybackStep(stage="planner", message="Планирую", fraction=0.3, delay_seconds=1.5),
        PlaybackStep(stage="done", message=DONE_MESSAGE, fraction=1.0, delay_seconds=0.0),
    ]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    await play(777, steps, broker=broker, sleep=fake_sleep)

    events = [event async for event in broker.subscribe(777)]
    assert [e.stage for e in events] == ["planner", "done"]
    assert events[-1].fraction == 1.0
    assert sleeps == [1.5]  # у done задержка нулевая — sleep на неё не вызывается


async def test_play_does_not_sleep_when_all_delays_zero():
    broker = ProgressBroker()
    steps = [PlaybackStep(stage="done", message=DONE_MESSAGE, fraction=1.0, delay_seconds=0.0)]
    calls = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1

    await play(778, steps, broker=broker, sleep=fake_sleep)

    assert calls == 0
