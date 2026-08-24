"""Прогон стадий пайплайна: план → поиск → скоринг → карточки → сводка.

Вынесено из `run_flow.py` (ревью Д-1 Э2-З, цикл 1, совет 1): файл дробления
`orchestrator.py` (РЕШ-151) упёрся в лимит 500 строк ровно на границе, и
следующая же правка начиналась бы с дробления, а не с задачи. `_run_stages` —
прежнее тело `ScoutPipeline.run()` до Т-19/Т-20 (запись `stage_log`, `plan_log`
РЕШ-83 и `candidate_log` РЕШ-84 появилась там же, но саму прогонку стадий не
переносила); `_empty_report` — его хвост на пустой выдаче. Оба остаются
методами `ScoutPipeline` через миксин, тем же приёмом, что и остальные куски
дробления оркестратора: `RunStagesMixin` подмешивается в `run_flow.ScoutPipeline`
рядом с `SearchMixin`, `EnrichmentMixin` и прочими, и обращается к `self._router`,
`self._settings`, `self._notify` и другим атрибутам, которые собирает
`PipelineContextMixin.__init__`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.llm import CostLimitExceededError
from app.pipeline import dedup_judge, diversity, history, stages
from app.pipeline.persistence import CandidateRecord, PlanRecord, StageRecord
from app.pipeline.pipeline_selection_flow import CONTEST_CARDS_TARGET, candidate_records, split_contest_candidates
from app.pipeline.run_journal import cost_delta
from app.schemas import Brief, Report, ReportSummary, ScoredCandidate, SolutionCard

#: Логгер намеренно не по `__name__` — см. пояснение в `pipeline_context.py`:
#: код до дробления жил в `app.pipeline.orchestrator`, и caplog-фильтр
#: `tests/test_fallback_without_guesses.py` ловит запись об отказе сводки
#: ровно по этой строке.
log = logging.getLogger("app.pipeline.orchestrator")


class RunStagesMixin:
    """Прогон стадий одного запроса и сборка `Report` — `_run_stages`/`_empty_report`."""

    async def _run_stages(
        self,
        brief: Brief,
        request_id: int,
        stage_log: list[StageRecord],
        plan_log: list[PlanRecord],
        candidate_log: list[CandidateRecord],
        history_pool: history.HistoryPool | None = None,
    ) -> Report:
        """Прежнее тело `run` — прогон стадий, теперь ещё собирает `stage_log`,
        планы проходов в `plan_log` (РЕШ-83) и кандидатов с оценками в
        `candidate_log` (РЕШ-84). `history_pool` — накопительный поиск (РЕШ-163,
        `app.pipeline.history`); умолчание `None` — ради прямых вызовов в тестах.

        Оба журнала **обязательны** (РЕШ-89). Пока `plan_log` был необязательным,
        ветка «списка не передали» переживала мутацию молча: следующий
        вызывающий — второй проход поиска (Т-21) — потерял бы ровно ту запись,
        ради которой затеян РЕШ-83, и ни один тест этого не заметил бы. Вызовам
        из тестов, которым нужен только журнал стадий, дешевле передать пустой
        список, чем оставлять в коде необязательную ветку.
        """
        history_pool = history_pool or history.HistoryPool()
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

        if not unique and not history_pool.scored:
            return self._empty_report(brief, request_id, outcome.unavailable)

        new_for_scoring = history.exclude_known(unique, history_pool.scored)  # РЕШ-163, пункт 4

        # Между дедупликацией и скорингом (РЕШ-52.3): обогащать до склейки
        # дублей — платить лишние запросы за кандидатов, которые всё равно
        # схлопнутся, а после скоринга цитируемость уже никому не поможет.
        new_for_scoring, citations = await self.enrich_candidates(new_for_scoring, stage_log=stage_log)
        if citations:
            await self._notify("enrich", f"Цитируемость известна у кандидатов: {len(citations)}", 0.47)

        await self._notify("scorer", "Оцениваю кандидатов", 0.5)
        started = datetime.now(UTC)
        cost_before = dict(self._router.cost.by_provider)
        scored = await stages.score_candidates(self._router, brief, new_for_scoring, citations=citations)
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
        # Пункт 5: объединённый пул для отбора и гейта второго прохода.
        scored_pool = [*history_pool.scored, *scored]
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
        main_first_pass, _ = split_contest_candidates(scored_pool)
        if stages.needs_second_pass(main_first_pass, threshold=self._settings.second_pass_relevance_max):
            scored_second, second_unavailable = await self._second_pass(
                brief, plan, unique, scored_pool, stage_log=stage_log, plan_log=plan_log, history=history_pool.scored
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
        main_scored, contest_scored = split_contest_candidates([*scored_pool, *scored_second])
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
        if history_pool.scored:
            # Пункт 6: свой вызов с pass_no=0, судьба — по ЭТОМУ прогону.
            candidate_log.extend(candidate_records(history_pool.scored, picked, pass_no=history.HISTORY_PASS_NO))
        # Семантический судья дублей (Д-6 Э2-З, РЕШ-152): лексика заголовков
        # дубли не разделяет (расчёт по обеим отраслям —
        # `.planning/evidence/E2G-З6-similarity.md`), поэтому перед группировкой
        # ОСНОВНОЙ очереди спрашивается модель — один вызов на прогон, только по
        # уже отобранным кандидатам. Конкурсная очередь этим признаком не
        # проверяется — «+1 вызов модели на прогон», а не «+2» (граница задачи).
        # Меньше двух кандидатов — дублировать нечего, вызов модели того не стоит
        # (та же причина, что у пропуска строки журнала конкурсной секции ниже).
        judge_groups: list[list[int]] | None = None
        if len(selected) >= 2:
            await self._notify("dedup_judge", "Спрашиваю судью дублей по отобранным кандидатам", 0.58)
            started = datetime.now(UTC)
            cost_before = dict(self._router.cost.by_provider)
            judge_groups = await dedup_judge.judge_duplicate_groups(self._router, selected)
            stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
            judge_ok = judge_groups is not None
            stage_log.append(
                StageRecord(
                    stage="dedup_judge",
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    provider=provider,
                    cost_rub=stage_cost,
                    error=None if judge_ok else "судья дублей недоступен, группировка идёт без семантической склейки",
                )
            )
            await self._notify(
                "dedup_judge",
                f"Судья дублей: групп найдено {len(judge_groups)}"
                if judge_ok
                else "Судья дублей недоступен, иду без семантической склейки",
                0.585,
            )

        groups_raw = stages.group_for_cards(selected, target=target)
        if judge_groups:
            # Вердикт судьи скармливается той же машинерии склейки, что и
            # лексический порог (РЕШ-132 п. 2, граница задачи Д-6: порог 0.65 не
            # трогать) — она остаётся страховкой ПОСЛЕ семантической склейки.
            judge_input = selected[: dedup_judge.JUDGE_CANDIDATE_LIMIT]
            groups_raw = dedup_judge.apply_judge_groups(groups_raw, judge_groups, judge_input)
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
