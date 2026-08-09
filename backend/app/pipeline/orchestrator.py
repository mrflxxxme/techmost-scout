"""Оркестратор: ведёт запрос от текста до отчёта и пишет журнал стадий.

Здесь собрано всё, что стадии знать не должны: работа с базой, параллельный
опрос источников, отчёт о прогрессе для интерфейса, честная пометка
недоступных источников.

Главное правило устойчивости: отказ любого отдельного источника не роняет
запрос — он попадает в список sources_unavailable и виден в отчёте.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.config import Settings, get_settings
from app.llm import LLMRouter
from app.pipeline import stages
from app.schemas import Brief, Candidate, Report, SearchPlan, SolutionCard
from app.search import build_adapters
from app.search.base import SearchAdapter

log = logging.getLogger(__name__)

#: Обработчик прогресса: (стадия, человекочитаемое сообщение, доля 0..1)
ProgressHook = Callable[[str, str, float], Awaitable[None] | None]


@dataclass(slots=True)
class SearchOutcome:
    candidates: list[Candidate]
    unavailable: list[str]
    hits_by_provider: dict[str, int]


class ScoutPipeline:
    """Полный проход подбора. Один экземпляр — один запрос."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        router: LLMRouter | None = None,
        adapters: list[SearchAdapter] | None = None,
        progress: ProgressHook | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._router = router or LLMRouter(self._settings)
        self._adapters = adapters
        self._progress = progress
        self._client: httpx.AsyncClient | None = None

    # ── Вспомогательное ─────────────────────────────────────────────────────
    async def _notify(self, stage: str, message: str, fraction: float) -> None:
        log.info("[%s] %s", stage, message)
        if self._progress is None:
            return
        result = self._progress(stage, message, fraction)
        if asyncio.iscoroutine(result):
            await result

    async def _get_adapters(self) -> list[SearchAdapter]:
        if self._adapters is not None:
            return self._adapters
        self._client = httpx.AsyncClient(
            timeout=self._settings.search_timeout_seconds,
            headers={"User-Agent": f"TechMostScout/1.0 (mailto:{self._settings.crossref_mailto})"},
            follow_redirects=True,
        )
        self._adapters = build_adapters(self._client, self._settings)
        log.info("подключено источников: %s", ", ".join(a.name for a in self._adapters))
        return self._adapters

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Поиск по всем источникам ────────────────────────────────────────────
    async def gather_candidates(self, plan: SearchPlan) -> SearchOutcome:
        """Опросить источники параллельно, каждому — только подходящие запросы."""
        adapters = await self._get_adapters()
        tasks: list[tuple[str, asyncio.Task[list[Candidate]]]] = []

        async with asyncio.TaskGroup() as group:
            for adapter in adapters:
                for query in plan.queries:
                    if adapter.source_types and query.source_type not in adapter.source_types:
                        continue
                    task = group.create_task(
                        self._safe_search(adapter, query),
                        name=f"{adapter.name}:{query.source_type.value}",
                    )
                    tasks.append((adapter.name, task))

        candidates: list[Candidate] = []
        hits: dict[str, int] = {}
        failed: dict[str, int] = {}
        for name, task in tasks:
            found = task.result()
            if found is None:  # адаптер сообщил об отказе источника
                failed[name] = failed.get(name, 0) + 1
                continue
            candidates.extend(found)
            hits[name] = hits.get(name, 0) + len(found)

        # Источник считаем недоступным, если он не отдал вообще ничего и при этом падал.
        unavailable = sorted(name for name in failed if hits.get(name, 0) == 0)
        return SearchOutcome(candidates=candidates, unavailable=unavailable, hits_by_provider=hits)

    async def _safe_search(self, adapter: SearchAdapter, query) -> list[Candidate] | None:
        """Обёртка: любая ошибка адаптера превращается в None, а не в падение запроса."""
        try:
            return await adapter.search(query, limit=self._settings.max_candidates_per_query)
        except NotImplementedError:
            return None
        except Exception as exc:
            log.warning("источник %s не смог выполнить поиск «%s»: %s", adapter.name, query.text, exc)
            return None

    # ── Полный проход ───────────────────────────────────────────────────────
    async def run(self, brief: Brief, *, request_id: int = 0) -> Report:
        """Бриф → отчёт. Интейк выполняется отдельно, до этого вызова."""
        await self._notify("planner", "Составляю план поиска", 0.05)
        plan = await stages.plan_search(self._router, brief)
        await self._notify("planner", f"Формулировок в плане: {len(plan.queries)}", 0.1)

        await self._notify("search", "Опрашиваю источники", 0.15)
        outcome = await self.gather_candidates(plan)
        found_summary = ", ".join(f"{name}: {count}" for name, count in sorted(outcome.hits_by_provider.items()))
        await self._notify("search", f"Найдено кандидатов — {found_summary or 'ничего'}", 0.4)
        if outcome.unavailable:
            await self._notify("search", f"Не ответили: {', '.join(outcome.unavailable)}", 0.42)

        unique = stages.deduplicate(outcome.candidates)
        await self._notify("dedup", f"После склейки дублей осталось {len(unique)}", 0.45)

        if not unique:
            return self._empty_report(brief, request_id, outcome.unavailable)

        await self._notify("scorer", "Оцениваю кандидатов", 0.5)
        scored = await stages.score_candidates(self._router, brief, unique)
        target = self._settings.target_cards
        selected = stages.select_top(scored, limit=target * 2)
        groups = stages.group_for_cards(selected, target=target)
        await self._notify("scorer", f"Отобрано вариантов для карточек: {len(groups)}", 0.6)

        await self._notify("cards", "Собираю карточки решений", 0.65)
        cards = await self._author_cards(brief, groups)
        await self._notify("cards", f"Готово карточек: {len(cards)}", 0.85)

        await self._notify("summary", "Составляю сводку и рекомендацию", 0.9)
        summary = await stages.summarize(self._router, brief, cards)

        report = Report(
            request_id=request_id,
            brief=brief,
            cards=cards,
            summary=summary,
            sources_unavailable=outcome.unavailable,
            generated_at=datetime.now(UTC),
            cost_rub=round(self._router.cost.spent_rub, 4),
        )
        await self._notify("done", f"Отчёт готов. Стоимость запроса: {report.cost_rub:.2f} ₽", 1.0)
        return report

    async def _author_cards(self, brief: Brief, groups: list[list]) -> list[SolutionCard]:
        """Собрать и проверить карточки. Параллельно, но не более трёх сразу —
        дешёвые модели плохо переносят десяток одновременных длинных вызовов."""
        semaphore = asyncio.Semaphore(3)

        async def one(group: list) -> SolutionCard | None:
            async with semaphore:
                try:
                    card = await stages.author_card(self._router, brief, group)
                except Exception as exc:
                    log.warning("карточка не собралась: %s", exc)
                    return None
                try:
                    return await stages.verify_card(self._router, card)
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

    def _empty_report(self, brief: Brief, request_id: int, unavailable: list[str]) -> Report:
        """Пустая выдача — тоже результат: честно говорим, что ничего не нашли."""
        from app.schemas import ReportSummary

        return Report(
            request_id=request_id,
            brief=brief,
            cards=[],
            summary=ReportSummary(
                comparison_rows=[],
                recommendation=(
                    "По этой формулировке задачи ничего пригодного не найдено. "
                    "Стоит расширить ключевые слова, снять часть ограничений или "
                    "проверить задачу вручную по отраслевым институтам."
                ),
                caveats=["Автоматический поиск не дал кандидатов"],
            ),
            sources_unavailable=unavailable,
            generated_at=datetime.now(UTC),
            cost_rub=round(self._router.cost.spent_rub, 4),
        )
