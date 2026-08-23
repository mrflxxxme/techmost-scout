"""Поиск и сбор кандидатов (РЕШ-151, дробление `orchestrator.py`).

Опрос источников параллельно, извлечение сырых записей платного поиска
моделью (стадия `extractor`) и условный второй проход при слабой первой
выдаче — три вещи, которые вместе отвечают на один вопрос: что удалось найти
и чем это далось. `_second_pass` лежит здесь же, а не в отдельном файле: это
повторный поиск с повторным скорингом новых кандидатов, а не самостоятельная
тема.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from app.llm import CostLimitExceededError
from app.pipeline import stages
from app.pipeline.persistence import PlanRecord, StageRecord
from app.pipeline.pipeline_selection_flow import SECOND_PASS_NO
from app.pipeline.run_journal import cost_delta
from app.schemas import Brief, Candidate, ScoredCandidate, SearchPlan
from app.search.base import SearchAdapter

#: Логгер намеренно не по `__name__` — см. пояснение в `pipeline_context.py`:
#: код до дробления жил в `app.pipeline.orchestrator`, и caplog-фильтры
#: существующих тестов (`tests/test_stages.py`,
#: `tests/test_fallback_without_guesses.py`) ловят записи ровно по этой строке.
log = logging.getLogger("app.pipeline.orchestrator")


def _unwrap_group(wrapped: BaseExceptionGroup) -> BaseException:
    """Достать из обёртки `asyncio.TaskGroup` исходное исключение.

    Группа задач заворачивает падение любой из них, и наружу вместо причины
    уходит «unhandled errors in a TaskGroup (1 sub-exception)». Это ломает
    контракт РЕШ-45 сразу в двух местах: `cli.py` ловит стоп-кран **по типу**,
    а `persist_run` пишет в `requests.error` текст исключения — три теста
    персистентности требуют в нём слова «лимит». До Т-6 внутри группы работали
    только адаптеры, и `_safe_search` гасил любое их исключение; стадия
    extractor сделала группу первым местом, где стоп-кран возникает внутри
    `TaskGroup` (П-1 аудита).

    Стоп-кран выбирается из листьев первым: если лимит выбрали сразу несколько
    формулировок, причина остановки прогона — именно он, а не соседний отказ.
    """
    leaves: list[BaseException] = []
    queue: list[BaseException] = list(wrapped.exceptions)
    while queue:
        current = queue.pop(0)
        if isinstance(current, BaseExceptionGroup):
            queue = [*current.exceptions, *queue]
        else:
            leaves.append(current)
    for leaf in leaves:
        if isinstance(leaf, CostLimitExceededError):
            return leaf
    return leaves[0] if leaves else wrapped


@dataclass(slots=True)
class SearchOutcome:
    candidates: list[Candidate]
    unavailable: list[str]
    #: Метка контура (РЕШ-31) — на нём строится строка «Найдено кандидатов»
    hits_by_provider: dict[str, int]
    #: Имя адаптера — тот же ключ, что и в `unavailable`; без него вклад
    #: адаптера с несколькими контурами (Яндекс) было нечем восстановить
    hits_by_name: dict[str, int]
    #: Что деградировало внутри поиска, не сделав источник недоступным:
    #: отказ стадии extractor и её пустой результат при непустой выдаче, а
    #: также транспортный отказ на ОТДЕЛЬНОЙ формулировке, когда тот же адаптер
    #: ответил на другие (З-6, находка В-3 аудита Э2-Г): источник не попадает в
    #: `unavailable`, потому что отдал что-то по другим формулировкам, а
    #: формулировка, на которой он упал, — единственная про свою тему — иначе
    #: пропадала бы бесследно. Отсюда строка попадает в `error` записи стадии
    #: `search` (Н-1 аудита) — иначе пробел не виден ни в отчёте, ни в журнале,
    #: ни в списке недоступных источников.
    problems: list[str] = field(default_factory=list)


class SearchMixin:
    """Опрос источников, извлечение кандидатов и повторный проход поиска."""

    async def _get_adapters(self) -> list[SearchAdapter]:
        """Адаптеры прогона. Список собирается один раз и кэшируется: первым
        его просит поиск (`gather_candidates`), обогащение работает уже на
        готовом списке.
        """
        if self._adapters is not None:
            return self._adapters
        self._client = httpx.AsyncClient(
            timeout=self._settings.search_timeout_seconds,
            headers={"User-Agent": f"TechMostScout/1.0 (mailto:{self._settings.crossref_mailto})"},
            follow_redirects=True,
        )
        # Отложенный импорт из фасада, а не `from app.search import
        # build_adapters` в лоб: тесты подменяют `build_adapters` атрибутом
        # именно на `app.pipeline.orchestrator` (`tests/test_cli_run.py`), и
        # прямой импорт здесь эту подмену не увидел бы — тот же приём, что и
        # с `get_settings` в `selection.py` (грабля коммита 3891b08).
        from app.pipeline.orchestrator import build_adapters

        self._adapters = build_adapters(self._client, self._settings)
        log.info("подключено источников: %s", ", ".join(a.name for a in self._adapters))
        return self._adapters

    @property
    def adapters(self) -> list[SearchAdapter]:
        """Источники этого прогона — для тех, кому нужны их счётчики после
        прогона (Т-18: расход платного поиска OpenAlex печатается в конце
        `scout run`). Пустой список означает «поиск ни разу не запускался»:
        список собирается лениво, на первом обращении к источникам.
        """
        return list(self._adapters or [])

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Поиск по всем источникам ────────────────────────────────────────────
    async def gather_candidates(self, plan: SearchPlan) -> SearchOutcome:
        """Опросить источники параллельно, каждому — только подходящие запросы.

        Счётчик `hits_by_provider` ключуется меткой контура (`contour_label`),
        а не именем адаптера: источник вроде Яндекса отдаёт разные метки для
        разных формулировок (РЕШ-31). `sources_unavailable` при этом остаётся
        по имени адаптера — этот список печатается в клиентском отчёте, и его
        состав задачей учёта не меняется.

        `SearchOutcome.problems` собирает всё, что прошло тихо: деградации
        стадии extractor (`_extract`) и формулировки, не отправленные из-за
        исчерпанного платного предела источника. Оттуда список уезжает в
        `error` записи стадии `search` — без него оба события в журнале прогона
        неотличимы от «источник ответил и ничего не нашёл».
        """
        adapters = await self._get_adapters()
        tasks: list[tuple[str, str, asyncio.Task[list[Candidate]]]] = []
        problems: list[str] = []

        try:
            async with asyncio.TaskGroup() as group:
                for adapter in adapters:
                    for query in plan.queries:
                        if adapter.source_types and query.source_type not in adapter.source_types:
                            continue
                        task = group.create_task(
                            self._safe_search(adapter, query, problems),
                            name=f"{adapter.name}:{query.source_type.value}",
                        )
                        tasks.append((adapter.name, adapter.contour_label(query), task))
        except BaseExceptionGroup as wrapped:
            raise _unwrap_group(wrapped) from wrapped

        # Пропуски по исчерпанному платному пределу забираются у источников
        # здесь, а не внутри `_safe_search`: предел кончается в самом адаптере,
        # до обращения к источнику, и о пропущенной формулировке знает только
        # он. Первый цикл правок завёл у Яндекса `drain_limit_problems`, но не
        # позвал его ни разу — записи оставались в адаптере, и исчерпанный
        # платный предел в отчёте по-прежнему выглядел как «источник ответил и
        # ничего не нашёл» (Ц-1 пост-аудита Э2-Б, второй цикл).
        #
        # Метод спрашивается у объекта, а не объявлен в `SearchAdapter`: платный
        # предел на прогон есть ровно у одного источника, и общий контракт под
        # частный случай не расширяется — так же оркестратор находит
        # `enrich_by_doi`.
        #
        # Забирается ровно один раз на проход: `problems` живёт внутри
        # `gather_candidates`, и записи первого прохода, приехавшие во второй,
        # читались бы как удвоенный недобор.
        limit_skipped = 0
        for adapter in adapters:
            drain = getattr(adapter, "drain_limit_problems", None)
            if drain is not None:
                drained = drain()
                limit_skipped += len(drained)
                problems.extend(drained)
        # Счётчик копится по всем проходам прогона: оговорка клиенту ставится
        # один раз, а исчерпать предел может как первый проход, так и второй.
        self._limit_skipped_total += limit_skipped

        candidates: list[Candidate] = []
        hits_by_label: dict[str, int] = {}
        hits_by_name: dict[str, int] = {}
        failed: dict[str, int] = {}
        for name, label, task in tasks:
            found = task.result()
            if found is None:  # адаптер сообщил об отказе источника
                failed[name] = failed.get(name, 0) + 1
                continue
            candidates.extend(found)
            hits_by_label[label] = hits_by_label.get(label, 0) + len(found)
            hits_by_name[name] = hits_by_name.get(name, 0) + len(found)

        # Источник считаем недоступным, если он не отдал вообще ничего и при этом
        # падал — проверяем по имени адаптера, не по метке контура.
        unavailable = sorted(name for name in failed if hits_by_name.get(name, 0) == 0)
        return SearchOutcome(
            candidates=candidates,
            unavailable=unavailable,
            hits_by_provider=hits_by_label,
            hits_by_name=hits_by_name,
            problems=problems,
        )

    async def _safe_search(self, adapter: SearchAdapter, query, problems: list[str]) -> list[Candidate] | None:
        """Обёртка: любая ошибка адаптера превращается в None, а не в падение запроса.

        `problems` — общий на прогон список деградаций поиска: сюда же попадает
        отказ ОТДЕЛЬНОЙ формулировки (см. ниже), а не только деградации стадии
        extractor, см. `SearchOutcome.problems`.
        """
        raw: list[dict] = []
        try:
            if getattr(adapter, "needs_extraction", False):
                # Кандидаты и сырые записи одним ответом источника: спрашивать
                # ту же выдачу второй раз ради стадии extractor — двойной счёт
                # у платного источника (Э2-5, Р-8 плана).
                found, raw = await adapter.search_raw(query, limit=self._settings.max_candidates_per_query)
            else:
                # Структурированный источник: разбор кодом точнее и бесплатен,
                # модель ему не нужна.
                found = await adapter.search(query, limit=self._settings.max_candidates_per_query)
        except NotImplementedError:
            return None
        except Exception as exc:
            log.warning("источник %s не смог выполнить поиск «%s»: %s", adapter.name, query.text, exc)
            # Отказ конкретной формулировки записывается в problems, даже если
            # адаптер в целом отдал что-то по другим формулировкам и потому не
            # попадёт в `sources_unavailable` (З-6, находка В-3 аудита Э2-Г):
            # без этой строки единственная формулировка про свою тему пропадала
            # бы бесследно — виден был бы только `log.warning`, а клиент отчёт
            # читает, а не лог. Счётчик — по образцу `_limit_skipped_total`,
            # чтобы оговорка клиенту ставилась по факту, а не по подстроке.
            problems.append(f"источник {adapter.name} не смог выполнить поиск «{query.text}»: {exc}")
            self._partial_failure_total += 1
            return None
        if not raw:
            return found
        return await self._extract(adapter, query, found, raw, problems)

    async def _extract(
        self, adapter: SearchAdapter, query, parsed: list[Candidate], raw: list[dict], problems: list[str]
    ) -> list[Candidate]:
        """Нормализовать сырую выдачу моделью (Э2-5).

        Результат стадии заменяет собственный разбор адаптера, а не дополняет
        его: это одни и те же записи выдачи, и складывать их значило бы
        протащить каждую страницу в отбор дважды — ключи склейки у них разные
        (у разбора адаптера это адрес, у стадии — DOI либо название).

        Отказ стадии прогон не роняет и источник недоступным не делает
        (инвариант №5): выдача уже оплачена и разобрана адаптером, поэтому
        берём его разбор и пишем причину в лог.

        Пустой результат при непустых сырых записях лечится тем же откатом, и
        это осознанный выбор, а не симметрия ради симметрии (Н-1 аудита). До
        Т-6 весь веб приходил в прогон именно разбором адаптера, поэтому откат
        не может выдать ничего хуже прежнего состояния; ссылки в нём взяты
        кодом из ответа источника, а не написаны моделью. Обратный вариант —
        отдать пусто — стирал бы оплаченную выдачу целиком ради случая, когда
        модель честно сочла все двадцать записей мусором (правило 11 промпта);
        случай редкий, а цена ошибки в нём — вклад единственного платного
        источника прогона. Отбор мусора и без того делает скоринг.

        В обоих случаях причина уходит в `problems` — оттуда она попадает в
        `error` записи стадии `search`, потому что лога прогона снаружи не
        видно, а «источник ответил и ничего не нашёл» выглядит так же.
        """
        try:
            found = await stages.extract_candidates(query.source_type, adapter.name, raw, self._router)
        except CostLimitExceededError:
            # Стоп-кран (РЕШ-45): исчерпанный лимит — не отказ модели, а
            # команда «стоп тратить». Продолжить на разборе адаптера значило бы
            # оставить остальные формулировки платить дальше.
            raise
        except Exception as exc:
            log.warning(
                "extractor: стадия не удалась для %s («%s»): %s — беру разбор адаптера",
                adapter.name,
                query.text,
                exc,
            )
            problems.append(f"extractor ({adapter.name}): стадия не удалась — {exc}; взят разбор адаптера")
            return parsed
        if not found:
            log.warning(
                "extractor: %s («%s») вернул ноль кандидатов из %s сырых записей — беру разбор адаптера",
                adapter.name,
                query.text,
                len(raw),
            )
            problems.append(
                f"extractor ({adapter.name}): ноль кандидатов из {len(raw)} сырых записей; взят разбор адаптера"
            )
            return parsed
        return found

    async def _second_pass(
        self,
        brief: Brief,
        plan: SearchPlan,
        first_pass: list[Candidate],
        scored: list[ScoredCandidate],
        *,
        stage_log: list[StageRecord],
        plan_log: list[PlanRecord],
    ) -> tuple[list[ScoredCandidate], list[str]]:
        """Второй проход поиска: новый план, повторный поиск, скоринг НОВЫХ кандидатов (РЕШ-81).

        Возвращает пару «оценки второго прохода, источники, не ответившие во
        втором проходе». Пустой первый элемент означает, что прохода фактически
        не случилось — отказала стадия `replan` либо второй поиск не принёс ни
        одного кандидата сверх первой выдачи.

        Три решения, каждое из которых стоит денег или времени прогона:

        * **Отказ `replan` прогон не роняет** (инвариант №5): отчёт собирается
          по первой выдаче, а факт отказа виден в журнале стадий строкой с
          непустым `error` — тем же способом, каким видно отказ сводки и
          интейка. Молчаливый отказ был бы неотличим от «порог не сработал».
          Стоп-кран по стоимости (РЕШ-45) при этом пробрасывается: исчерпанный
          лимит — не отказ модели, а команда «стоп тратить».
        * **В скоринг уходят только новые кандидаты.** Повторно оценивать уже
          оценённых — платить второй раз за тот же ответ и удваивать 25–30 минут
          прогона, не получив ни одного нового кандидата. Новизна считается по
          ключу склейки через существующий `deduplicate`, которому отдаются обе
          выдачи сразу: так же схлопываются и дубли внутри второго прохода.
        * **Свои строки журнала стадий** — `replan`, вторые `search`, `enrich` и
          `scorer`: трата второго прохода обязана быть видна отдельно, а не
          растворяться в строках первого. Отдельность даёт поле `pass_no`
          (`SECOND_PASS_NO`), а не порядок записей: доклад о расходе (РЕШ-82)
          строится фильтром по полю. Первый цикл правок завёл поле, но ни одной
          двойки в него не положил — заполненное единицей, оно отвечало на
          вопрос «сколько стоил второй проход» нулём и приписывало его трату
          первому проходу (Ц-2 пост-аудита Э2-Б).
        """
        started = datetime.now(UTC)
        cost_before = dict(self._router.cost.by_provider)
        await self._notify("replan", "Выдача мимо задачи — перестраиваю план поиска", 0.55)
        replan_error: str | None = None
        new_plan: SearchPlan | None = None
        try:
            new_plan = await stages.replan_search(self._router, brief, plan, scored)
        except CostLimitExceededError:
            raise
        except Exception as exc:
            log.warning("стадия replan не удалась (%s), остаюсь на выдаче первого прохода", exc)
            replan_error = f"стадия replan не удалась: {exc}"
        stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
        stage_log.append(
            StageRecord(
                stage="replan",
                started_at=started,
                finished_at=datetime.now(UTC),
                pass_no=SECOND_PASS_NO,
                provider=provider,
                cost_rub=stage_cost,
                error=replan_error,
            )
        )
        if new_plan is None:
            return [], []

        plan_log.append(PlanRecord(plan=new_plan, pass_no=2))
        await self._notify("replan", f"Формулировок во втором плане: {len(new_plan.queries)}", 0.56)

        started = datetime.now(UTC)
        cost_before = dict(self._router.cost.by_provider)
        outcome = await self.gather_candidates(new_plan)
        stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
        stage_log.append(
            StageRecord(
                stage="search",
                started_at=started,
                finished_at=datetime.now(UTC),
                pass_no=SECOND_PASS_NO,
                provider=provider,
                cost_rub=stage_cost,
                sources_hit=dict(outcome.hits_by_provider),
                error="; ".join(outcome.problems) or None,
            )
        )

        known = {candidate.dedup_key() for candidate in first_pass}
        merged = stages.deduplicate([*first_pass, *outcome.candidates])
        fresh = [candidate for candidate in merged if candidate.dedup_key() not in known]
        await self._notify(
            "search",
            f"Второй проход: найдено {len(outcome.candidates)}, из них новых после склейки {len(fresh)}",
            0.58,
        )
        if not fresh:
            return [], outcome.unavailable

        fresh, citations = await self.enrich_candidates(fresh, stage_log=stage_log, pass_no=SECOND_PASS_NO)

        await self._notify("scorer", f"Оцениваю новых кандидатов второго прохода: {len(fresh)}", 0.59)
        started = datetime.now(UTC)
        cost_before = dict(self._router.cost.by_provider)
        scored_second = await stages.score_candidates(self._router, brief, fresh, citations=citations)
        stage_cost, provider = cost_delta(cost_before, dict(self._router.cost.by_provider))
        stage_log.append(
            StageRecord(
                stage="scorer",
                started_at=started,
                finished_at=datetime.now(UTC),
                pass_no=SECOND_PASS_NO,
                provider=provider,
                cost_rub=stage_cost,
            )
        )
        return scored_second, outcome.unavailable
