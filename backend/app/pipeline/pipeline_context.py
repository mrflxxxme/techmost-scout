"""Общий контекст прогона `ScoutPipeline` (РЕШ-151, дробление `orchestrator.py`).

Конструктор и уведомление о прогрессе нужны всем остальным кускам пайплайна —
поиску, обогащению, отбору, сборке карточек и сборке отчёта, — а не только
одному из них, поэтому вынесены в отдельный модуль, а не спрятаны в одном из
тематических файлов. `ScoutPipeline` собирается в `app.pipeline.run_flow` из
этого класса и остальных подмешиваемых частей: класс здесь не финальный, а
подмешиваемый (`PipelineContextMixin`), и `self` в его методах — экземпляр уже
собранного `ScoutPipeline`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from app.config import Settings, get_settings
from app.llm import LLMRouter
from app.pipeline.persistence import SessionFactory
from app.schemas import SearchPlan
from app.search.base import SearchAdapter

#: Логгер намеренно не по `__name__`: код до дробления жил в
#: `app.pipeline.orchestrator`, и caplog-фильтры существующих тестов
#: (`tests/test_stages.py`, `tests/test_fallback_without_guesses.py`) ловят
#: записи ровно по этой строке (грабля коммита 3891b08, тот же приём).
log = logging.getLogger("app.pipeline.orchestrator")

#: Обработчик прогресса: (стадия, человекочитаемое сообщение, доля 0..1)
ProgressHook = Callable[[str, str, float], Awaitable[None] | None]


class PipelineContextMixin:
    """Инициализация экземпляра прогона и общий помощник уведомлений."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        router: LLMRouter | None = None,
        adapters: list[SearchAdapter] | None = None,
        progress: ProgressHook | None = None,
        session_factory: SessionFactory | None = None,
        preset_plan: SearchPlan | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._router = router or LLMRouter(self._settings)
        self._adapters = adapters
        self._progress = progress
        #: Готовый план поиска (РЕШ-83, `scout run --plan-id N`): при нём стадия
        #: планировщика не обращается к модели вовсе, и её строка в журнале
        #: стоит с нулевой стоимостью. Читает план из БД вызывающий код —
        #: оркестратор получает уже разобранную схему, чтобы отказ чтения
        #: случался до первого платного запроса, а не посреди прогона.
        self._preset_plan = preset_plan
        #: Фабрика сессий БД (Ф-8, РЕШ-40) — не передана, значит запись в БД
        #: не выполняется вовсе (тесты, веб до Э3): прогон не меняется ни на шаг.
        self._session_factory = session_factory
        self._client: httpx.AsyncClient | None = None
        #: Сколько формулировок за весь прогон не доехало до источника из-за
        #: исчерпанного платного предела (РЕШ-127 п. 2). Копится по обоим
        #: проходам: один экземпляр — один запрос, поэтому обнуления по ходу
        #: не требуется.
        self._limit_skipped_total = 0
        #: Сколько отдельных формулировок за весь прогон транспортно отказали
        #: у источника, который при этом ответил на другие формулировки (З-6,
        #: находка В-3 аудита Э2-Г). По тому же образцу и по той же причине,
        #: что `_limit_skipped_total`: копится по обоим проходам, потому что
        #: отказать может и второй.
        self._partial_failure_total = 0

    # ── Вспомогательное ─────────────────────────────────────────────────────
    async def _notify(self, stage: str, message: str, fraction: float) -> None:
        log.info("[%s] %s", stage, message)
        if self._progress is None:
            return
        result = self._progress(stage, message, fraction)
        if asyncio.iscoroutine(result):
            await result
