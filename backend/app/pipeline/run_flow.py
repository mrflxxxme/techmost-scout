"""Полный проход `ScoutPipeline` и запись прогона в БД (РЕШ-151, дробление
`orchestrator.py`).

Здесь собирается финальный класс `ScoutPipeline` — из контекста
(`PipelineContextMixin`), поиска (`SearchMixin`), обогащения
(`EnrichmentMixin`), отбора (`SelectionFlowMixin`), сборки карточек
(`CardAssemblyMixin`) и прогона стадий (`RunStagesMixin`) — и здесь же держится
главное правило устойчивости: отказ любого отдельного источника не роняет
запрос, он попадает в `sources_unavailable` и виден в отчёте. То же правило
(Ф-7, инвариант №5) распространяется на запись прогона в БД (Ф-8, РЕШ-40):
отказ записи не роняет прогон, markdown-отчёт первичен.

Сам прогон стадий (`_run_stages`/`_empty_report`) живёт в `run_stages.py`
(ревью Д-1 Э2-З, цикл 1, совет 1): здесь остаётся только `run` — точка входа,
запись в БД до и после стадий, оговорки и передача номера прогона в отчёт.
"""

from __future__ import annotations

import logging

from app.pipeline import history
from app.pipeline.persistence import CandidateRecord, PlanRecord, StageRecord
from app.pipeline.pipeline_context import PipelineContextMixin
from app.pipeline.pipeline_enrichment import EnrichmentMixin
from app.pipeline.pipeline_search import SearchMixin
from app.pipeline.pipeline_selection_flow import SelectionFlowMixin
from app.pipeline.run_cards import CardAssemblyMixin
from app.pipeline.run_journal import format_active_run_params
from app.pipeline.run_stages import RunStagesMixin
from app.schemas import Brief, Report

#: Логгер намеренно не по `__name__` — см. пояснение в `pipeline_context.py`:
#: код до дробления жил в `app.pipeline.orchestrator`.
log = logging.getLogger("app.pipeline.orchestrator")


class ScoutPipeline(
    PipelineContextMixin, SearchMixin, EnrichmentMixin, SelectionFlowMixin, CardAssemblyMixin, RunStagesMixin
):
    """Полный проход подбора. Один экземпляр — один запрос."""

    async def run(
        self,
        brief: Brief,
        *,
        request_id: int = 0,
        raw_text: str = "",
        initial_stage_log: list[StageRecord] | None = None,
        initial_caveats: list[str] | None = None,
        constraints_unresolved: bool = False,
        history_request_id: int | None = None,  # РЕШ-166: явный номер прошлого прогона
    ) -> Report:
        """Бриф → отчёт. Интейк выполняется отдельно, до этого вызова.

        Запись прогона в БД (Ф-8, РЕШ-40) происходит здесь, после сборки
        отчёта либо после отказа стадии: работа с базой держится в
        оркестраторе, стадии остаются чистыми функциями (CLAUDE.md).
        `raw_text` — исходный текст задачи, для строки `requests`; отчёту он
        не нужен, поэтому передаётся отдельно от `brief`.

        `initial_stage_log` — записи стадий, случившихся до вызова `run`
        (интейк живёт в `cli._run`, до пайплайна): их отказ иначе не оставил
        бы следа в `pipeline_logs`, хотя прогон честно продолжается (Т-2).

        `initial_caveats` — оговорки о том, что случилось до вызова `run`, тем
        же путём и по той же причине (РЕШ-58, П-1 аудита). Журнал стадий и
        консоль читает оператор, а до эксперта и клиента доживает только
        отчёт: оговорка встаёт в начало `ReportSummary.caveats` — туда же,
        куда `fallback_summary` кладёт оговорку об отказе сводки, — и
        печатается экспортом в блоке «Ограничения этого поиска».

        `constraints_unresolved` — тем же путём, что и `initial_caveats`
        (РЕШ-115): ограничения задачи не собрались даже после повторного
        адресного вызова интейка в `cli._run`. Флаг ложится в `Report` здесь,
        а не в `_run_stages`: стадии карточек к этому моменту уже отработали
        по тому `brief`, что был передан на вход, — флаг только про то, что
        клиент увидит в отчёте.
        """
        # Печатается первой строкой прогона, до вызова его стадий (критерий 1
        # З-7): обязана попасть в лог и тогда, когда первая же стадия прогона
        # падает, — именно тогда фактическое окружение важнее всего разобрать,
        # а восстановить его после падения больше неоткуда.
        #
        # Граница честно: у команды `scout run` интейк живёт РАНЬШЕ, в
        # `cli._run`, и если прогон умрёт на нём (например, на стоп-кране),
        # этой строки в логе не будет вовсе. Найдено пост-аудитом Э2-Д, вынесено
        # долгом волны: печать нужно поднять в `cli._run` до `run_intake`.
        log.info(format_active_run_params(self._settings))
        # `persist_run` читается отложенным импортом из фасада, а не
        # `from app.pipeline.persistence import persist_run` в лоб:
        # `tests/test_openalex.py` подменяет функцию атрибутом на
        # `app.pipeline.orchestrator`, и только этот путь видит подмену (тот
        # же приём, что для `build_adapters` в `pipeline_search.py`).
        from app.pipeline.orchestrator import persist_run

        stage_log: list[StageRecord] = list(initial_stage_log or [])
        #: Планы проходов этого прогона (РЕШ-83): собираются по ходу стадий и
        #: пишутся в `search_plans` той же транзакцией, что и строка запроса.
        #: Отдельным списком, а не полем отчёта: клиенту план не показывают,
        #: он нужен для повторяемого прогона и сравнения выдачи.
        plan_log: list[PlanRecord] = []
        #: Кандидаты прогона с оценками (РЕШ-84) — тем же способом и по той же
        #: причине, что и планы: без сохранённых оценок числовой критерий
        #: приёмки (РЕШ-86) не с чем сравнивать.
        candidate_log: list[CandidateRecord] = []
        # Накопительный поиск (РЕШ-163) — чтение, лог и запись отказа в
        # журнал стадий собраны в history.load_and_log (инвариант №5).
        history_pool = await history.load_and_log(
            self._session_factory, raw_text=raw_text, explicit_request_id=history_request_id, stage_log=stage_log
        )
        try:
            report = await self._run_stages(brief, request_id, stage_log, plan_log, candidate_log, history_pool)
        except Exception as exc:
            await persist_run(
                self._session_factory,
                raw_text=raw_text,
                status="failed",
                cost_rub=round(self._router.cost.spent_rub, 4),
                cards=[],
                contest_cards=[],
                stages=stage_log,
                plans=plan_log,
                candidates=candidate_log,
                error=str(exc),
                brief=brief,  # РЕШ-146/156: briefs пишется и на отказавшем прогоне
            )
            raise
        if initial_caveats:
            # В начало: оговорка о том, из чего собран бриф, объясняет качество
            # всей выдачи, а оговорки стадий — только своей части.
            report.summary.caveats = [*initial_caveats, *report.summary.caveats]
        if self._limit_skipped_total or self._partial_failure_total:
            # Признак — счётчик, а не подстрока в `problems` (РЕШ-127 п. 2,
            # З-6): до этой правки ни исчерпанный платный предел, ни отказ
            # отдельной формулировки не доезжали дальше `pipeline_logs`, и
            # клиент видел недобор источника как «источник ответил и ничего не
            # нашёл». Инвариант №5 требует обратного. Оговорка ставится ровно
            # один раз, даже если сработали оба признака сразу, — условие
            # общее на оба счётчика, а не два отдельных append.
            #
            # `PAID_LIMIT_NOTICE` читается отложенным импортом из фасада, а не
            # `from app.export.markdown import PAID_LIMIT_NOTICE` в лоб: тест
            # `tests/test_partial_source_failure.py` подменяет константу
            # атрибутом на `app.pipeline.orchestrator`, и только этот путь
            # видит подмену (тот же приём, что для `build_adapters`).
            from app.pipeline.orchestrator import PAID_LIMIT_NOTICE

            report.summary.caveats = [*report.summary.caveats, PAID_LIMIT_NOTICE]
        report.constraints_unresolved = constraints_unresolved
        persisted_id = await persist_run(
            self._session_factory,
            raw_text=raw_text,
            status="drafted",
            cost_rub=report.cost_rub,
            cards=report.cards,
            # Обе секции пишутся из отчёта: источник карточек один и тот же для клиента и для базы (РЕШ-92).
            contest_cards=report.contest_cards,
            stages=stage_log,
            plans=plan_log,
            candidates=candidate_log,
            error=None,
            brief=brief,
            report=report,  # РЕШ-146/156: briefs и report_json — для экрана Э3-5
        )
        if persisted_id is not None:
            # Номер прогона появляется только в момент записи: до неё строки
            # `requests` не существует. Экспорт печатает «Запрос №N» — по нему
            # выданный клиенту отчёт связывается со строкой журнала и с
            # карточками в БД (Т-7). Записи не было или она отказала — номера
            # нет, `request_id` остаётся нулём, и экспорт печатает прочерк:
            # отказ БД не роняет прогон, но и не выдаётся за успешную запись
            # (инвариант №5 в редакции РЕШ-43).
            report.request_id = persisted_id
        return report
