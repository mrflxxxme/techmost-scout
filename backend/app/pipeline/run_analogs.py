"""Секция «Аналоги из мира» на уровне прогона (Э6-2а, дробление `run_stages.py`).

Чистые функции отбора и сборки — в `analogs.py`; здесь то, что оркестратор делает
вокруг них: выбор заготовок из очереди аналогов, ограниченный параллелизм вызовов
модели, строка журнала стадий `analog_cards` и строки прогресса.

Секция строится ПОСЛЕДНЕЙ, после конкурсной (как и конкурсная — после сводки):
это приложение к отчёту, и `CostLimitExceededError` из её сборки пробрасывается по
РЕШ-45 — стоя раньше, приложение распоряжалось бы судьбой основной выдачи, за
которую уже заплачено. При виде решения «Наука» (`research`) секция не строится
вовсе: ни вызова модели, ни строки журнала, ни строки прогресса.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.llm import CostLimitExceededError
from app.pipeline import analogs, stages
from app.pipeline.persistence import StageRecord
from app.pipeline.pipeline_selection_flow import ANALOG_CARDS_TARGET
from app.pipeline.run_journal import cost_delta
from app.schemas import AnalogCard, Brief, ScoredCandidate

#: Логгер намеренно не по `__name__` — см. пояснение в `pipeline_context.py`.
log = logging.getLogger("app.pipeline.orchestrator")

#: Не более трёх одновременных вызовов — тот же расчёт, что у карточек решений
#: (`CardAssemblyMixin._author_cards`): дешёвые модели плохо переносят десяток
#: длинных вызовов разом.
_PARALLEL_CALLS = 3


class AnalogSectionMixin:
    """Отбор заготовок и сборка секции аналогов — `_select_analog_groups`/`_build_analog_section`."""

    def _select_analog_groups(self, brief: Brief, analog_scored: list[ScoredCandidate]) -> list[list[ScoredCandidate]]:
        """Заготовки карточек аналогов из очереди аналогов.

        Порядок важен: сначала снимаются кандидаты исключённой страны, и только
        потом идёт отсечка по релевантности и группировка со срезом — иначе
        заготовка с запрещённой страной занимала бы место допустимой.
        """
        if brief.solution_kind == "research":
            return []
        allowed = analogs.prefilter_analog_candidates(analog_scored, brief.mode)
        eligible = self._cut_by_relevance(allowed)
        return stages.group_for_cards(eligible, target=ANALOG_CARDS_TARGET)

    async def _build_analog_section(
        self,
        brief: Brief,
        groups: list[list[ScoredCandidate]],
        *,
        found: int,
        stage_log: list[StageRecord],
    ) -> list[AnalogCard]:
        """Собрать карточки аналогов и отобрать публикуемые; `[]` при виде «Наука».

        `found` — сколько кандидатов-аналогов дошло до отбора (для строки прогресса).
        Строка печатается ВСЕГДА при виде «Рынок», в том числе с нулями: «аналогов
        не нашли» и «нашли, но до карточек не дошли» — разные события.
        """
        if brief.solution_kind == "research":
            return []
        await self._notify(
            "cards",
            f"Аналоги из мира: кандидатов {found}, заготовок карточек {len(groups)} (секция вне лимита 5–7)",
            0.955,
        )
        authored: list[AnalogCard] = []
        if groups:
            started = datetime.now(UTC)
            cost_before = dict(self._router.cost.by_provider)
            authored = await self._author_analog_cards(brief, groups)
            stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
            stage_log.append(
                StageRecord(
                    stage="analog_cards",
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    provider=provider,
                    cost_rub=stage_cost,
                )
            )
        published = analogs.select_publishable_analogs(authored, brief.mode, ANALOG_CARDS_TARGET)
        await self._notify(
            "cards",
            f"Аналогов из мира: {len(published)}, отброшено: {len(groups) - len(published)}",
            0.97,
        )
        return published

    async def _author_analog_cards(self, brief: Brief, groups: list[list[ScoredCandidate]]) -> list[AnalogCard]:
        """Собрать карточки аналогов параллельно; отказ одной не роняет остальные.

        Исчерпанный лимит стоимости (РЕШ-45) — не отказ модели, а команда «стоп
        тратить»: пробрасывается, чтобы соседние группы не продолжали платить.
        """
        semaphore = asyncio.Semaphore(_PARALLEL_CALLS)

        async def one(group: list[ScoredCandidate]) -> AnalogCard | None:
            async with semaphore:
                try:
                    return await analogs.author_analog_card(self._router, brief, group)
                except CostLimitExceededError:
                    raise
                except Exception as exc:
                    log.warning("карточка аналога не собралась: %s", exc)
                    return None

        results = await asyncio.gather(*(one(group) for group in groups))
        return [card for card in results if card is not None]
