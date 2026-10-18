"""Демо-воспроизведение сохранённого прогона (SPEC Э4-3, РЕШ-196 п. 10–11, 14;
РЕШ-200/РЕШ-201).

Показ не запускает пайплайн заново и не тратит ни рубля: он проигрывает уже
записанный журнал стадий (`pipeline_logs`) в тот же брокер прогресса, каким
пользуется живой прогон (`app.web.progress.ProgressBroker`), — тем же
контрактом события `{stage, message, fraction}` («Контракты» → «Формат
события демо» в `.planning/PLAN.md`). Доли стадий берутся из настоящих
`started_at`/`finished_at` (РЕШ-201): пропорции реальных длительностей
масштабируются на заданную общую длительность показа («Темп воспроизведения»),
а не повторяют исходные задержки секунда в секунду.

Модуль ничего не знает о базе, о выборе демо-кейсов (разметку читает и
проверяет только вызывающий маршрут, «Разметка демо-кейсов») и о живом
пайплайне (инвариант №6): на входе — уже прочитанные строки журнала, на
выходе — план воспроизведения. `play` умеет публиковать этот план в брокер,
ничего сверх `ProgressBroker.publish` и переданной функции ожидания не зная.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.web.progress import ProgressBroker

#: Сообщение финального шага показа — без суммы стоимости (РЕШ-188): демо не
#: должно намекать на цену настоящего прогона. Новая клиентская фраза — на
#: визу владельца, см. handoff задачи Э4-И7.
DONE_MESSAGE = "Показ демо-прогона завершён."


class PipelineLogEntry(Protocol):
    """Минимальный контракт строки `pipeline_logs`, нужный воспроизведению.

    Протокол, а не импорт ORM-модели: стадии пайплайна — чистые функции
    (CLAUDE.md), сессия базы сюда не долетает и не нужна — вызывающий
    маршрут передаёт уже прочитанные строки.
    """

    stage: str
    started_at: datetime
    finished_at: datetime | None
    sources_hit: dict[str, int]


@dataclass(frozen=True, slots=True)
class PlaybackStep:
    """Один шаг демо-показа — то, что уходит в `ProgressBroker.publish`."""

    stage: str
    message: str
    fraction: float
    delay_seconds: float


def _stage_duration(entry: PipelineLogEntry) -> float:
    """Настоящая длительность стадии в секундах; без `finished_at` — ноль
    (стадия не завершилась — прогон оборвался на ней)."""
    if entry.finished_at is None:
        return 0.0
    return max(0.0, (entry.finished_at - entry.started_at).total_seconds())


def _search_message(entry: PipelineLogEntry) -> str:
    """Число кандидатов — из `sources_hit` журнала, а не выдумано (инвариант №1)."""
    found = sum(entry.sources_hit.values()) if entry.sources_hit else 0
    return f"Поиск по источникам — найдено кандидатов: {found}."


#: Тексты стадий — словарь модуля (новые фразы на визу владельца, см.
#: handoff задачи Э4-И7). Числа в них берутся только из полей строки журнала
#: — ничего не выдумывается, инвариант №1 распространяется и на показ.
STAGE_MESSAGES: dict[str, Callable[[PipelineLogEntry], str]] = {
    "intake": lambda _e: "Разбираю формулировку задачи.",
    "intake_refine": lambda _e: "Уточняю бриф по ответам оператора.",
    "intake_constraints": lambda _e: "Проверяю формулировку на нерешаемые ограничения.",
    "planner": lambda _e: "Составляю план поиска.",
    "replan": lambda _e: "Перестраиваю план поиска по итогам первого прохода.",
    "search": _search_message,
    "dedup": lambda _e: "Склеиваю дубликаты кандидатов.",
    "enrich": lambda _e: "Обогащаю кандидатов данными о цитируемости.",
    "history": lambda _e: "Сверяюсь с историей прошлых прогонов.",
    "scorer": lambda _e: "Оцениваю кандидатов по релевантности и зрелости.",
    "dedup_judge": lambda _e: "Свожу похожие варианты в один.",
    "cards": lambda _e: "Собираю карточки решений.",
    "contest_cards": lambda _e: "Собираю карточки конкурсных решений.",
    "analog_cards": lambda _e: "Собираю карточки аналогов из мира.",
    "summary": lambda _e: "Составляю сводку и рекомендацию.",
}

#: Текст для стадии, отсутствующей в словаре (новая стадия пайплайна,
#: заведённая позже, — показ не должен падать на неизвестном имени).
_FALLBACK_MESSAGE = "Выполняю этап «{stage}»."


def _stage_message(entry: PipelineLogEntry) -> str:
    factory = STAGE_MESSAGES.get(entry.stage)
    if factory is not None:
        return factory(entry)
    return _FALLBACK_MESSAGE.format(stage=entry.stage)


def build_playback(logs: Sequence[PipelineLogEntry], *, total_seconds: float) -> list[PlaybackStep]:
    """Строит план воспроизведения журнала стадий (РЕШ-201).

    Реальная длительность каждой стадии превращается в долю от суммарной
    длительности журнала, а доля — в задержку, пропорционально
    масштабированную на `total_seconds` (пропорциональное сжатие, а не
    сохранение исходных секунд). Финальный шаг — `stage="done"`,
    `fraction=1.0`, без задержки и без суммы стоимости (РЕШ-188). `fraction`
    остальных шагов — нарастающая доля пройденной длительности, поэтому не
    убывает по ходу списка.

    Пустой журнал отдаёт пустой список — честное сообщение об этом показывает
    вызывающий маршрут, а не эта функция. Журнал из одной строки и журнал с
    нулевыми длительностями (стадии длились ноль секунд или не завершились)
    не роняют сборку: при нулевой суммарной длительности время
    распределяется поровну между стадиями.

    Доля ни одного из промежуточных шагов не достигает 1.0 — только у
    финального `done`: `ProgressBroker.subscribe` закрывает поток и на
    `stage == "done"`, и на `fraction >= 1.0` (`app/web/progress.py`), и
    если бы последняя реальная стадия уже несла долю 1.0, поток обрывался бы
    до публикации `done`, а клиент так и не увидел бы честного финала.
    """
    if not logs:
        return []

    ordered = sorted(logs, key=lambda entry: entry.started_at)
    durations = [_stage_duration(entry) for entry in ordered]
    total_duration = sum(durations)
    count = len(ordered)

    #: Потолок доли промежуточных шагов — см. докстроку выше про закрытие
    #: потока на `fraction >= 1.0`.
    intermediate_cap = 0.99

    steps: list[PlaybackStep] = []
    cumulative = 0.0
    for entry, duration in zip(ordered, durations, strict=True):
        share = duration / total_duration if total_duration > 0 else 1.0 / count
        cumulative = min(cumulative + share, 1.0)
        steps.append(
            PlaybackStep(
                stage=entry.stage,
                message=_stage_message(entry),
                fraction=min(cumulative, intermediate_cap),
                delay_seconds=total_seconds * share,
            )
        )

    steps.append(PlaybackStep(stage="done", message=DONE_MESSAGE, fraction=1.0, delay_seconds=0.0))
    return steps


async def play(
    request_id: int,
    steps: Sequence[PlaybackStep],
    *,
    broker: ProgressBroker,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Публикует шаги показа в брокер прогресса с паузами между ними.

    `sleep` — параметр, а не прямой вызов `asyncio.sleep`: тесты подставляют
    свою функцию и не ждут настоящих секунд (инвариант №7).
    """
    for step in steps:
        broker.publish(request_id, step.stage, step.message, step.fraction)
        if step.delay_seconds > 0:
            await sleep(step.delay_seconds)
