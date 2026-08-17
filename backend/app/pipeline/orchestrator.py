"""Оркестратор: ведёт запрос от текста до отчёта и пишет журнал стадий.

Здесь собрано всё, что стадии знать не должны: работа с базой, параллельный
опрос источников, отчёт о прогрессе для интерфейса, честная пометка
недоступных источников.

Главное правило устойчивости: отказ любого отдельного источника не роняет
запрос — он попадает в список sources_unavailable и виден в отчёте. То же
правило (Ф-7, инвариант №5) распространяется на запись прогона в БД (Ф-8,
РЕШ-40): отказ записи не роняет прогон, markdown-отчёт первичен.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.llm import CostLimitExceededError, LLMRouter
from app.pipeline import stages
from app.pipeline.persistence import SessionFactory, StageRecord, persist_run
from app.schemas import Brief, Candidate, Report, SearchPlan, SolutionCard
from app.search import build_adapters, normalize_doi
from app.search.base import SearchAdapter

log = logging.getLogger(__name__)

#: Обработчик прогресса: (стадия, человекочитаемое сообщение, доля 0..1)
ProgressHook = Callable[[str, str, float], Awaitable[None] | None]


def cost_delta(before: dict[str, float], after: dict[str, float]) -> tuple[float, str | None]:
    """Прирост стоимости и провайдер(ы), которые её потратили, между двумя
    снимками `LLMRouter.cost.by_provider`.

    Роутер считает расход по запросу целиком, не по стадиям, поэтому для
    журнала стадий (Ф-8) берём разницу снимков вокруг вызова стадии —
    точность до провайдера, не до токена.

    Функция публичная, потому что тем же снимком пользуется `cli._run`: стадия
    интейка живёт до пайплайна, а её строка в журнале обязана нести
    фактическую стоимость, а не ноль (П-4 аудита Т-2).
    """
    changed: list[str] = []
    total = 0.0
    for name, value in after.items():
        diff = value - before.get(name, 0.0)
        if diff > 1e-9:
            total += diff
            changed.append(name)
    provider = ",".join(sorted(changed)) if changed else None
    return round(total, 6), provider


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


#: Поля кандидата, которые обогащение имеет право заполнить. Только пустые:
#: данные источника, нашедшего кандидата, точнее — он видел его в контексте
#: своей выдачи, а обогащение работает вслепую по DOI.
_PATCHABLE_FIELDS = ("org", "snippet", "pdf_url")


def _apply_patch(candidate: Candidate, patch: dict) -> tuple[Candidate, list[str]]:
    """Заполнить пустые поля кандидата патчем обогащения.

    Непустое значение не перезаписывается никогда — это суть правки, а не
    деталь: иначе аффилиация из Crossref молча заменялась бы на организацию
    первого автора из OpenAlex, а фрагмент из витрины партнёров — на
    академическую аннотацию.

    Поля проверяются по одному, и негодное отбрасывается в одиночку (П-2
    аудита). Раньше валидация шла одним куском: патч с годной организацией и
    мусорным `pdf_url` отбрасывался целиком, вместе с ссылкой пропадали
    организация и аннотация, а сообщение уходило на `DEBUG`, которого в прогоне
    не видно. Возвращает пару «кандидат, имена отброшенных полей»: молчаливое
    отбрасывание — это ровно тот отказ, который снаружи неотличим от работы.
    """
    updates = {field: patch[field] for field in _PATCHABLE_FIELDS if patch.get(field) and not getattr(candidate, field)}
    if not updates:
        return candidate, []
    # Через повторную валидацию, а не model_copy: в патче сырые строки, и
    # кривой pdf_url обязан отвалиться здесь, а не в экспорте отчёта.
    base = candidate.model_dump(mode="json")
    result = candidate
    accepted: dict = {}
    dropped: list[str] = []
    for field_name, value in updates.items():
        try:
            result = Candidate.model_validate({**base, **accepted, field_name: value})
        except ValidationError as exc:
            dropped.append(field_name)
            log.info("обогащение: поле %s отброшено для %s: %s", field_name, candidate.url, exc)
            continue
        accepted[field_name] = value
    return result, dropped


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
    #: отказ стадии extractor и её пустой результат при непустой выдаче.
    #: Отсюда строка попадает в `error` записи стадии `search` (Н-1 аудита) —
    #: иначе пробел не виден ни в отчёте, ни в журнале, ни в списке
    #: недоступных источников.
    problems: list[str] = field(default_factory=list)


class ScoutPipeline:
    """Полный проход подбора. Один экземпляр — один запрос."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        router: LLMRouter | None = None,
        adapters: list[SearchAdapter] | None = None,
        progress: ProgressHook | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._router = router or LLMRouter(self._settings)
        self._adapters = adapters
        self._progress = progress
        #: Фабрика сессий БД (Ф-8, РЕШ-40) — не передана, значит запись в БД
        #: не выполняется вовсе (тесты, веб до Э3): прогон не меняется ни на шаг.
        self._session_factory = session_factory
        self._client: httpx.AsyncClient | None = None

    # ── Вспомогательное ─────────────────────────────────────────────────────
    async def _notify(self, stage: str, message: str, fraction: float) -> None:
        log.info("[%s] %s", stage, message)
        if self._progress is None:
            return
        result = self._progress(stage, message, fraction)
        if asyncio.iscoroutine(result):
            await result

    async def _get_adapters(self, industry: str | None = None) -> list[SearchAdapter]:
        """Адаптеры прогона. `industry` — отрасль брифа (Т-4, РЕШ-51): по ней
        КиберЛенинка выбирает набор OAI.

        Список собирается один раз и кэшируется, поэтому отрасль важна на
        первом вызове — а он всегда идёт из поиска (`gather_candidates`),
        обогащение работает уже на готовом списке.
        """
        if self._adapters is not None:
            return self._adapters
        self._client = httpx.AsyncClient(
            timeout=self._settings.search_timeout_seconds,
            headers={"User-Agent": f"TechMostScout/1.0 (mailto:{self._settings.crossref_mailto})"},
            follow_redirects=True,
        )
        self._adapters = build_adapters(self._client, self._settings, industry=industry)
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
    async def gather_candidates(self, plan: SearchPlan, *, industry: str | None = None) -> SearchOutcome:
        """Опросить источники параллельно, каждому — только подходящие запросы.

        Счётчик `hits_by_provider` ключуется меткой контура (`contour_label`),
        а не именем адаптера: источник вроде Яндекса отдаёт разные метки для
        разных формулировок (РЕШ-31). `sources_unavailable` при этом остаётся
        по имени адаптера — этот список печатается в клиентском отчёте, и его
        состав задачей учёта не меняется.

        `industry` — отрасль брифа, её подставляет `_run_stages`. Она едет
        отдельным аргументом, а не полем `SearchPlan`: общий контракт данных
        (`schemas.py`) задача Т-4 не расширяет, меняется только внутренний
        контракт сборки адаптеров.
        """
        adapters = await self._get_adapters(industry)
        tasks: list[tuple[str, str, asyncio.Task[list[Candidate]]]] = []
        problems: list[str] = []

        try:
            async with asyncio.TaskGroup() as group:
                for adapter in adapters:
                    if getattr(adapter, "enrich_only", False):
                        # Источник обогащения (OpenAlex): поиска у него нет вовсе.
                        # Спросить его — значит получить NotImplementedError и
                        # записать в отчёт «не ответил» источник, который никто не
                        # спрашивал. Обогащение вызывается отдельно, после дедупа.
                        continue
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

    # ── Обогащение по DOI ───────────────────────────────────────────────────
    async def enrich_candidates(
        self, candidates: list[Candidate], *, stage_log: list[StageRecord] | None = None
    ) -> tuple[list[Candidate], dict[str, int]]:
        """Добрать метаданные по DOI у источника обогащения (РЕШ-52.3).

        Возвращает пару: кандидаты с заполненными ПУСТЫМИ полями и цитируемость
        по ключу склейки кандидата. Цитируемость не поле `Candidate` — она
        нужна ровно одной стадии, скорингу, и едет отдельным словарём, а не
        правкой общего контракта данных.

        Отказ обогащения прогон не роняет: адаптер честно поднимает
        `SearchError` (инвариант №5 в редакции РЕШ-43), а здесь это становится
        строкой в логе и продолжением работы — обогащение необязательно, без
        него кандидаты просто беднее.

        Но «не роняет» не значит «незаметно»: у стадии три независимых пути
        тихой деградации — отказ целиком, прерванный обход и отброшенные поля
        патча, — и ни один не оставлял следа в `pipeline_logs` (П-5 аудита).
        Обогащение, отказывающее в каждом прогоне месяц подряд, снаружи было
        неотличимо от работающего. Поэтому стадия пишет `StageRecord` с
        непустым `error` — тем же способом, каким Т-2 завела запись об отказе
        интейка.
        """
        adapters = await self._get_adapters()
        # Источник обогащения узнаётся по методу, а не по имени класса:
        # оркестратору незачем знать, что это именно OpenAlex.
        adapter = next((a for a in adapters if hasattr(a, "enrich_by_doi")), None)
        if adapter is None:
            return candidates, {}

        by_doi: dict[str, list[int]] = {}
        for index, candidate in enumerate(candidates):
            doi = normalize_doi(candidate.raw_id)
            if doi:
                by_doi.setdefault(doi, []).append(index)
        if not by_doi:
            return candidates, {}

        started = datetime.now(UTC)

        def write_stage(problem: str | None) -> None:
            """Строка журнала стадии обогащения; `problem` — что именно деградировало."""
            if stage_log is not None:
                stage_log.append(
                    StageRecord(stage="enrich", started_at=started, finished_at=datetime.now(UTC), error=problem)
                )

        try:
            patches = await adapter.enrich_by_doi(list(by_doi))
        except Exception as exc:
            log.warning("обогащение по DOI не удалось (%s), прогон продолжается без него", exc)
            write_stage(f"обогащение по DOI не удалось: {exc}")
            return candidates, {}

        enriched = list(candidates)
        citations: dict[str, int] = {}
        dropped_fields: list[str] = []
        filled_org = 0
        for doi, patch in patches.items():
            for index in by_doi.get(doi, ()):
                candidate = enriched[index]
                cited = patch.get("cited_by_count")
                if isinstance(cited, int) and not isinstance(cited, bool):
                    citations[candidate.dedup_key()] = cited
                enriched[index], dropped = _apply_patch(candidate, patch)
                dropped_fields.extend(dropped)
                if not candidate.org and enriched[index].org:
                    filled_org += 1

        # Счётчик заполненных организаций — не косметика (П-1 аудита):
        # `stages.group_for_cards` группирует отобранных кандидатов по ТОЧНОЙ
        # строке `org`, а OpenAlex часто отдаёт зонтичную организацию
        # («Russian Academy of Sciences») вместо лаборатории. Значит обогащение
        # способно схлопнуть две несвязанные работы в одну карточку и сдвинуть
        # число карточек независимо от порога отбора. Без этой строки эффект не
        # измерить ни в одном прогоне.
        log.info(
            "обогащение: заполнен пустой org у %s кандидатов из %s (карточки группируются по точной строке org)",
            filled_org,
            len(candidates),
        )

        problems: list[str] = []
        # Обход, прерванный отказом на середине: патчи собраны не по всем DOI.
        interrupted = getattr(adapter, "last_error", None)
        if interrupted:
            problems.append(str(interrupted))
        if dropped_fields:
            names = ", ".join(sorted(set(dropped_fields)))
            summary = f"отброшено негодных полей патча — {len(dropped_fields)} ({names})"
            log.warning("обогащение: %s", summary)
            problems.append(summary)
        write_stage("; ".join(problems) or None)
        return enriched, citations

    async def _safe_search(self, adapter: SearchAdapter, query, problems: list[str]) -> list[Candidate] | None:
        """Обёртка: любая ошибка адаптера превращается в None, а не в падение запроса.

        `problems` — общий на прогон список деградаций стадии extractor, см.
        `SearchOutcome.problems`.
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

    # ── Полный проход ───────────────────────────────────────────────────────
    async def run(
        self,
        brief: Brief,
        *,
        request_id: int = 0,
        raw_text: str = "",
        initial_stage_log: list[StageRecord] | None = None,
        initial_caveats: list[str] | None = None,
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
        """
        stage_log: list[StageRecord] = list(initial_stage_log or [])
        try:
            report = await self._run_stages(brief, request_id, stage_log)
        except Exception as exc:
            await persist_run(
                self._session_factory,
                raw_text=raw_text,
                status="failed",
                cost_rub=round(self._router.cost.spent_rub, 4),
                cards=[],
                stages=stage_log,
                error=str(exc),
            )
            raise
        if initial_caveats:
            # В начало: оговорка о том, из чего собран бриф, объясняет качество
            # всей выдачи, а оговорки стадий — только своей части.
            report.summary.caveats = [*initial_caveats, *report.summary.caveats]
        persisted_id = await persist_run(
            self._session_factory,
            raw_text=raw_text,
            status="drafted",
            cost_rub=report.cost_rub,
            cards=report.cards,
            stages=stage_log,
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

    async def _run_stages(self, brief: Brief, request_id: int, stage_log: list[StageRecord]) -> Report:
        """Прежнее тело `run` — прогон стадий, теперь ещё собирает `stage_log`."""
        await self._notify("planner", "Составляю план поиска", 0.05)
        started = datetime.now(UTC)
        cost_before = dict(self._router.cost.by_provider)
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
        await self._notify("planner", f"Формулировок в плане: {len(plan.queries)}", 0.1)

        await self._notify("search", "Опрашиваю источники", 0.15)
        started = datetime.now(UTC)
        # Снимок счётчика вокруг поиска: до Т-6 стадия поиска не звала модель
        # ни разу, и пустые `provider`/`cost_rub` были безобидны по построению.
        # Стадия extractor сделала поиск платным — трата обязана быть видна в
        # журнале стадий тем же способом, что у остальных стадий (П-2 аудита).
        cost_before = dict(self._router.cost.by_provider)
        outcome = await self.gather_candidates(plan, industry=brief.industry)
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
        target = self._settings.target_cards
        selected = stages.select_top(scored, limit=target * 2)
        groups = stages.group_for_cards(selected, target=target)
        await self._notify("scorer", f"Отобрано вариантов для карточек: {len(groups)}", 0.6)

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
        await self._notify("cards", f"Готово карточек: {len(published)}, отброшено пустых: {dropped}", 0.85)

        await self._notify("summary", "Составляю сводку и рекомендацию", 0.9)
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

        report = Report(
            request_id=request_id,
            brief=brief,
            cards=published,
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
