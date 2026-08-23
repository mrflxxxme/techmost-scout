"""Сборка карточек решений на уровне прогона (РЕШ-151, дробление
`orchestrator.py`).

Ограниченный параллелизм и стоп-кран стоимости — то, что оркестратор
добавляет вокруг чистых функций `stages.author_card` и `stages.verify_card`.
"""

from __future__ import annotations

import asyncio
import logging

from app.llm import CostLimitExceededError
from app.pipeline import stages
from app.schemas import Brief, SolutionCard

#: Логгер намеренно не по `__name__` — см. пояснение в `pipeline_context.py`:
#: код до дробления жил в `app.pipeline.orchestrator`.
log = logging.getLogger("app.pipeline.orchestrator")


class CardAssemblyMixin:
    """Сборка и проверка карточек решений с ограниченным параллелизмом."""

    async def _author_cards(self, brief: Brief, groups: list[list]) -> list[SolutionCard]:
        """Собрать и проверить карточки. Параллельно, но не более трёх сразу —
        дешёвые модели плохо переносят десяток одновременных длинных вызовов."""
        semaphore = asyncio.Semaphore(3)

        async def one(group: list) -> SolutionCard | None:
            async with semaphore:
                try:
                    card = await stages.author_card(self._router, brief, group)
                except CostLimitExceededError:
                    # Стоп-кран (РЕШ-45): исчерпанный лимит — не отказ модели,
                    # а команда «стоп тратить». Молчаливая потеря карточки
                    # позволила бы остальным группам продолжать платить дальше
                    # (asyncio.gather не отменяет соседей на первый же None) —
                    # прогон обязан упасть целиком, а не тихо обеднеть.
                    raise
                except Exception as exc:
                    log.warning("карточка не собралась: %s", exc)
                    return None
                try:
                    return await stages.verify_card(self._router, card, group)
                except Exception as exc:
                    # Собранную карточку из-за сбоя проверки не выбрасываем:
                    # честно понижаем уверенность и отдаём решение эксперту.
                    log.warning("проверка карточки не удалась (%s), понижаю уверенность", exc)
                    from app.schemas import Confidence

                    card.confidence = Confidence.LOW
                    card.confidence_note = (
                        card.confidence_note + " Автопроверка не выполнена — проверить вручную."
                    ).strip()
                    return card

        results = await asyncio.gather(*(one(group) for group in groups))
        return [card for card in results if card is not None]
