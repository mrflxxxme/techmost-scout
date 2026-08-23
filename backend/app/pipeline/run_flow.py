"""Полный проход `ScoutPipeline` и запись прогона в БД (РЕШ-151, дробление
`orchestrator.py`).

Здесь собирается финальный класс `ScoutPipeline` — из контекста
(`PipelineContextMixin`), поиска (`SearchMixin`), обогащения
(`EnrichmentMixin`), отбора (`SelectionFlowMixin`) и сборки карточек
(`CardAssemblyMixin`) — и здесь же держится главное правило устойчивости:
отказ любого отдельного источника не роняет запрос, он попадает в
`sources_unavailable` и виден в отчёте. То же правило (Ф-7, инвариант №5)
распространяется на запись прогона в БД (Ф-8, РЕШ-40): отказ записи не роняет
прогон, markdown-отчёт первичен.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.llm import CostLimitExceededError
from app.pipeline import diversity, stages
from app.pipeline.persistence import CandidateRecord, PlanRecord, StageRecord
from app.pipeline.pipeline_context import PipelineContextMixin
from app.pipeline.pipeline_enrichment import EnrichmentMixin
from app.pipeline.pipeline_search import SearchMixin
from app.pipeline.pipeline_selection_flow import (
    CONTEST_CARDS_TARGET,
    SelectionFlowMixin,
    candidate_records,
    split_contest_candidates,
)
from app.pipeline.run_cards import CardAssemblyMixin
from app.pipeline.run_journal import cost_delta, format_active_run_params
from app.schemas import Brief, Report, ScoredCandidate, SolutionCard

#: Логгер намеренно не по `__name__` — см. пояснение в `pipeline_context.py`:
#: код до дробления жил в `app.pipeline.orchestrator`.
log = logging.getLogger("app.pipeline.orchestrator")


class ScoutPipeline(PipelineContextMixin, SearchMixin, EnrichmentMixin, SelectionFlowMixin, CardAssemblyMixin):
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
        # Печатается первой строкой прогона, до вызова стадий (критерий 1 З-7):
        # обязана попасть в лог и тогда, когда первая же платная стадия падает,
        # — именно тогда фактическое окружение прогона важнее всего разобрать,
        # а восстановить его после падения больше неоткуда.
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
        try:
            report = await self._run_stages(brief, request_id, stage_log, plan_log, candidate_log)
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
            # Обе секции пишутся из отчёта, а не из локальных списков стадии:
            # источник карточек один и тот же для клиента и для базы (РЕШ-92).
            contest_cards=report.contest_cards,
            stages=stage_log,
            plans=plan_log,
            candidates=candidate_log,
            error=None,
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

    async def _run_stages(
        self,
        brief: Brief,
        request_id: int,
        stage_log: list[StageRecord],
        plan_log: list[PlanRecord],
        candidate_log: list[CandidateRecord],
    ) -> Report:
        """Прежнее тело `run` — прогон стадий, теперь ещё собирает `stage_log`,
        планы проходов в `plan_log` (РЕШ-83) и кандидатов с оценками в
        `candidate_log` (РЕШ-84).

        Оба журнала **обязательны** (РЕШ-89). Пока `plan_log` был необязательным,
        ветка «списка не передали» переживала мутацию молча: следующий
        вызывающий — второй проход поиска (Т-21) — потерял бы ровно ту запись,
        ради которой затеян РЕШ-83, и ни один тест этого не заметил бы. Вызовам
        из тестов, которым нужен только журнал стадий, дешевле передать пустой
        список, чем оставлять в коде необязательную ветку.
        """
        started = datetime.now(UTC)
        cost_before = dict(self._router.cost.by_provider)
        if self._preset_plan is not None:
            # Прогон по сохранённому плану: модель не спрашиваем вовсе, значит
            # и снимок счётчика вокруг стадии брать не у чего — стоимость
            # планировщика ровно ноль, и это видно в журнале стадий.
            await self._notify("planner", "Беру сохранённый план поиска, планировщик не вызывается", 0.05)
            plan = self._preset_plan
            stage_cost, provider = 0.0, None
        else:
            await self._notify("planner", "Составляю план поиска", 0.05)
            plan = await stages.plan_search(self._router, brief)
            stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
        stage_log.append(
            StageRecord(
                stage="planner",
                started_at=started,
                finished_at=datetime.now(UTC),
                provider=provider,
                cost_rub=stage_cost,
            )
        )
        # План прогона сохраняется независимо от того, построен он моделью или
        # взят из базы: прогон по чужому плану — тоже прогон, и восстановить
        # его формулировки потом больше неоткуда.
        plan_log.append(PlanRecord(plan=plan, pass_no=1))
        await self._notify("planner", f"Формулировок в плане: {len(plan.queries)}", 0.1)

        await self._notify("search", "Опрашиваю источники", 0.15)
        started = datetime.now(UTC)
        # Снимок счётчика вокруг поиска: до Т-6 стадия поиска не звала модель
        # ни разу, и пустые `provider`/`cost_rub` были безобидны по построению.
        # Стадия extractor сделала поиск платным — трата обязана быть видна в
        # журнале стадий тем же способом, что у остальных стадий (П-2 аудита).
        cost_before = dict(self._router.cost.by_provider)
        outcome = await self.gather_candidates(plan)
        stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
        stage_log.append(
            StageRecord(
                stage="search",
                started_at=started,
                finished_at=datetime.now(UTC),
                provider=provider,
                cost_rub=stage_cost,
                sources_hit=dict(outcome.hits_by_provider),
                error="; ".join(outcome.problems) or None,
            )
        )
        found_summary = ", ".join(f"{name}: {count}" for name, count in sorted(outcome.hits_by_provider.items()))
        await self._notify("search", f"Найдено кандидатов — {found_summary or 'ничего'}", 0.4)
        if outcome.unavailable:
            await self._notify("search", f"Не ответили: {', '.join(outcome.unavailable)}", 0.42)

        unique = stages.deduplicate(outcome.candidates)
        await self._notify("dedup", f"После склейки дублей осталось {len(unique)}", 0.45)

        if not unique:
            return self._empty_report(brief, request_id, outcome.unavailable)

        # Между дедупликацией и скорингом (РЕШ-52.3): обогащать до склейки
        # дублей — платить лишние запросы за кандидатов, которые всё равно
        # схлопнутся, а после скоринга цитируемость уже никому не поможет.
        unique, citations = await self.enrich_candidates(unique, stage_log=stage_log)
        if citations:
            await self._notify("enrich", f"Цитируемость известна у кандидатов: {len(citations)}", 0.47)

        await self._notify("scorer", "Оцениваю кандидатов", 0.5)
        started = datetime.now(UTC)
        cost_before = dict(self._router.cost.by_provider)
        scored = await stages.score_candidates(self._router, brief, unique, citations=citations)
        stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
        stage_log.append(
            StageRecord(
                stage="scorer",
                started_at=started,
                finished_at=datetime.now(UTC),
                provider=provider,
                cost_rub=stage_cost,
            )
        )
        # Условный второй проход (РЕШ-81). Решение печатается ВСЕГДА, одной из
        # двух дословных строк, в том числе при выключенном пороге: иначе
        # «проход не сработал» неотличимо от «код до проверки не дошёл».
        scored_second: list[ScoredCandidate] = []
        # Решение принимается по ОСНОВНОЙ выдаче — той, что идёт клиенту (Т-34,
        # находка К-1 пост-аудита Э2-Б). `needs_second_pass` берёт максимум
        # `relevance`, а конкурсный класс выведен из общего отбора (РЕШ-92): его
        # кандидат в обещанные клиенту 5–7 карточек не попадает по построению, и
        # его высокая оценка ничего не говорит о том, закрыта ли задача. Пока
        # гейт смотрел на весь список, один хакатонный прототип с relevance 9
        # гасил проход на выдаче из двоек и троек — ровно на той задаче, ради
        # которой проход затеян (РЕШ-81).
        main_first_pass, _ = split_contest_candidates(scored)
        if stages.needs_second_pass(main_first_pass, threshold=self._settings.second_pass_relevance_max):
            scored_second, second_unavailable = await self._second_pass(
                brief, plan, unique, scored, stage_log=stage_log, plan_log=plan_log
            )
            if second_unavailable:
                # Источник, не ответивший хотя бы одному проходу, честно едет в
                # отчёт: клиент читает список недоступных источников целиком по
                # прогону, а не по его половине.
                outcome.unavailable = sorted(set(outcome.unavailable) | set(second_unavailable))

        target = self._settings.target_cards
        # Отбор идёт по обоим проходам сразу: второй проход затевался ради того,
        # чтобы в карточки попало найденное им, а не ради отдельного списка.
        # Но конкурсные кандидаты в общий отбор не идут (РЕШ-85 п. 1, РЕШ-92):
        # они не конкурируют с публикациями по общему скорингу и никого из
        # обещанных клиенту 5–7 вариантов не вытесняют.
        main_scored, contest_scored = split_contest_candidates([*scored, *scored_second])
        selected = stages.select_top(main_scored, limit=target * 2)
        # Своя очередь конкурсного класса: группировка по организации та же, но
        # с собственной верхней границей, и `select_top` здесь не зовётся —
        # иначе прогон печатал бы вторую строку калибровки `relevance` по
        # десятку конкурсных кандидатов, а её читают как строку всего прогона
        # (воротной критерий 3 подэтапа Э2-А).
        #
        # Отсечка по предметному соответствию (РЕШ-48) при этом обязана работать
        # и здесь: `select_top` фильтрует внутри себя, `group_for_cards` не
        # фильтрует ничем, и конкурсный кандидат с `relevance` 1 доезжал до
        # карточки и до клиента (Т-34, находка К-2 пост-аудита Э2-Б). Берётся
        # ровно отсечка, а не весь `select_top`, — вторая строка калибровки
        # осталась бы тем же испорченным воротным критерием.
        contest_eligible = self._cut_by_relevance(contest_scored)
        contest_groups_raw = stages.group_for_cards(contest_eligible, target=CONTEST_CARDS_TARGET)
        # Склейка заготовок до сборки карточек (З-6, РЕШ-132 п. 2): дублирующую
        # заготовку нет смысла оплачивать отдельным вызовом модели. Порог до
        # сборки, а не после — карточка стоит платного вызова.
        contest_groups = diversity.merge_duplicate_groups(contest_groups_raw)
        await self._notify(
            "scorer",
            f"Разнообразие (конкурсная очередь): заготовок до склейки {len(contest_groups_raw)}, "
            f"после {len(contest_groups)}, схлопнуто {len(contest_groups_raw) - len(contest_groups)}",
            0.63,
        )
        contest_picked = [item for group in contest_groups for item in group]
        # Оценки уезжают в БД сразу после отбора, а не в конце прогона: отказ
        # сборки карточек или сводки не должен уносить с собой единственное
        # числовое свидетельство того, что нашёл и как оценил этот прогон.
        # Проходы записываются по отдельности, каждый со своим `pass_no`: судьба
        # кандидата берётся по ключу склейки, и склеенный список пометил бы
        # `selected` обе строки общего ключа (см. `candidate_records`).
        #
        # Судьба конкурсного кандидата считается по его собственной очереди:
        # `selected` для него означает «дошёл до своей секции». Списки не
        # пересекаются по построению — кандидат либо конкурсный, либо нет, —
        # поэтому дважды `selected` он не получит, а конкурсный, не попавший в
        # свою секцию, останется `rejected` (предупреждение ревью Т-20).
        picked = [*selected, *contest_picked]
        candidate_log.extend(candidate_records(scored, picked))
        if scored_second:
            candidate_log.extend(candidate_records(scored_second, picked, pass_no=2))
        groups_raw = stages.group_for_cards(selected, target=target)
        # Склейка заготовок до сборки карточек (З-6, РЕШ-132 п. 2) — та же
        # причина, что у конкурсной очереди: дублирующая заготовка не должна
        # доезжать до платного вызова модели.
        groups = diversity.merge_duplicate_groups(groups_raw)
        await self._notify("scorer", f"Отобрано вариантов для карточек: {len(groups)}", 0.6)
        await self._notify(
            "scorer",
            f"Разнообразие (основная очередь): заготовок до склейки {len(groups_raw)}, "
            f"после {len(groups)}, схлопнуто {len(groups_raw) - len(groups)}",
            0.61,
        )
        # Строка печатается ВСЕГДА, в том числе с нулями: «конкурсных кандидатов
        # не было» и «конкурсные были, но до карточек не дошли» — разные события,
        # и молчание сделало бы их неотличимыми. Именно на этом «подключили и не
        # заметили» держался дефект, ради которого заведена Т-33.
        await self._notify(
            "scorer",
            f"Конкурсный класс: кандидатов {len(contest_scored)}, "
            f"заготовок карточек {len(contest_groups)} (секция вне лимита 5–7)",
            0.62,
        )

        await self._notify("cards", "Собираю карточки решений", 0.65)
        started = datetime.now(UTC)
        cost_before = dict(self._router.cost.by_provider)
        cards = await self._author_cards(brief, groups)
        stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
        stage_log.append(
            StageRecord(
                stage="cards", started_at=started, finished_at=datetime.now(UTC), provider=provider, cost_rub=stage_cost
            )
        )
        published = stages.select_publishable_cards(cards)
        dropped = len(cards) - len(published)
        await self._notify("cards", f"Готово карточек: {len(published)}, отброшено пустых: {dropped}", 0.8)

        await self._notify("summary", "Составляю сводку и рекомендацию", 0.85)
        started = datetime.now(UTC)
        cost_before = dict(self._router.cost.by_provider)
        summary_error: str | None = None
        try:
            summary = await stages.summarize(self._router, brief, published)
        except CostLimitExceededError:
            # Стоп-кран (РЕШ-45): исчерпанный лимит — не отказ модели, а
            # команда «стоп тратить». Запасная сводка НЕ строится — она сама
            # честный сценарий для отказа модели, а не для решения владельца
            # остановить трату; прогон обязан упасть.
            raise
        except Exception as exc:
            # Отказ стадии summary не роняет прогон с уже готовыми карточками
            # (Ф-7, инвариант №5 распространяется и на отказ модели): запасная
            # сводка собирается без вызова модели, факт отказа виден клиенту.
            log.warning("сводка не собралась (%s), собираю запасную без вызова модели", exc)
            summary = stages.fallback_summary(published)
            summary_error = str(exc)
        stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
        stage_log.append(
            StageRecord(
                stage="summary",
                started_at=started,
                finished_at=datetime.now(UTC),
                provider=provider,
                cost_rub=stage_cost,
                error=summary_error,
            )
        )

        # Конкурсная секция собирается ПОСЛЕДНЕЙ — после основных карточек и
        # сводки (РЕШ-93). Порядок не косметический: секция — приложение к
        # отчёту, а `CostLimitExceededError` из сборки карточек пробрасывается
        # по РЕШ-45 и роняет прогон. Стоя перед сводкой, приложение из трёх
        # карточек распоряжалось судьбой основной выдачи, за которую уже
        # заплачено. Отдельного стоп-крана на секцию при этом не заводится —
        # владелец отверг ещё один рычаг в конфигурации.
        contest_authored: list[SolutionCard] = []
        contest_published: list[SolutionCard] = []
        if contest_groups:
            # Своя строка журнала стадий и своё имя: разделённые сводкой снимки
            # счётчика в одну строку не складываются, а одинаковое имя оставило
            # бы трату приложения отличимой только по порядку записей — ровно то,
            # на что аудит указал у второго прохода (Ц-2).
            started = datetime.now(UTC)
            cost_before = dict(self._router.cost.by_provider)
            contest_authored = await self._author_cards(brief, contest_groups)
            stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
            stage_log.append(
                StageRecord(
                    stage="contest_cards",
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    provider=provider,
                    cost_rub=stage_cost,
                )
            )
            # Тот же отсев пустых карточек (инвариант №1: без подтверждений
            # карточка клиенту не показывается), но со своей верхней границей —
            # лимит 5–7 к этой секции не применяется ни в какую сторону.
            contest_published = stages.select_publishable_cards(contest_authored, limit=CONTEST_CARDS_TARGET)
        await self._notify(
            "cards",
            f"Карточек конкурсного класса: {len(contest_published)}, "
            f"отброшено пустых: {len(contest_authored) - len(contest_published)}",
            0.95,
        )

        report = Report(
            request_id=request_id,
            brief=brief,
            cards=published,
            # Сводка и сравнение вариантов остаются по основной выдаче: рекомендация
            # клиенту — про решения, прошедшие общий отбор, а конкурсная секция
            # идёт к ней приложением со своим предупреждением о статусе прав.
            contest_cards=contest_published,
            summary=summary,
            sources_unavailable=outcome.unavailable,
            generated_at=datetime.now(UTC),
            cost_rub=round(self._router.cost.spent_rub, 4),
        )
        await self._notify("done", f"Отчёт готов. Стоимость запроса: {report.cost_rub:.2f} ₽", 1.0)
        return report

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
