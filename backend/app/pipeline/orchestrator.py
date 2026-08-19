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
from app.metrics import score_payload
from app.pipeline import stages
from app.pipeline.persistence import CandidateRecord, PlanRecord, SessionFactory, StageRecord, persist_run
from app.schemas import Brief, Candidate, Report, ScoredCandidate, SearchPlan, SolutionCard, SourceType
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


def candidate_records(
    scored: list[ScoredCandidate], selected: list[ScoredCandidate], *, pass_no: int = 1
) -> list[CandidateRecord]:
    """Строки таблицы `candidates` по итогам скоринга и отбора (РЕШ-84).

    Судьба кандидата берётся по ключу склейки, а не по совпадению объектов: до
    записи список успевает пройти сортировку и потолок на источник, и сравнение
    по `id()` держалось бы на том, что отбор не пересобирает элементы.

    Признак нейтральной оценки спрашивается у стадии скоринга
    (`stages.has_neutral_score`), а не вычисляется здесь заново: вторая копия
    этого знания молча разошлась бы с первой, и в базе следы отказа батча стали
    бы неотличимы от оценок модели — ровно то, что ловит измеритель
    `app.metrics`.

    Вызывать функцию нужно **отдельно на каждый проход** со своим `pass_no`, а
    не на склеенном списке двух проходов (Т-21): судьба берётся по ключу
    склейки, и общий для двух проходов ключ пометил бы `selected` обе строки,
    хотя отбор взял одну. Внутри одного прохода список приходит из
    `deduplicate`, поэтому там повтор ключа невозможен по построению.
    """
    picked = {item.candidate.dedup_key() for item in selected}
    return [
        CandidateRecord(
            candidate=item.candidate,
            score=score_payload(item, neutral=stages.has_neutral_score(item)),
            status="selected" if item.candidate.dedup_key() in picked else "rejected",
            pass_no=pass_no,
        )
        for item in scored
    ]


#: Сколько карточек собирается для секции «Ранние разработки и команды»
#: (РЕШ-85 п. 1). Лимит 5–7 (FR-9) секции не касается — это её собственная
#: верхняя граница, и она нужна по двум причинам. Первая: каждая карточка стоит
#: отдельного вызова модели, а конкурсных кандидатов платный контур Яндекса
#: может принести десятками. Вторая: секция дополнительная, её читает эксперт
#: поверх основной выдачи, и десять ранних прототипов в довесок к семи решениям
#: он разбирать не станет. Число не выводится из `target_cards`: оно про другое
#: — про объём работы эксперта, а не про обещанные клиенту 5–7 вариантов.
CONTEST_CARDS_TARGET = 3

#: Номер прохода у стадий условного второго поиска (РЕШ-81, РЕШ-82). Именованная
#: величина, а не литерал в каждой записи: `replan`, вторые `search`, `enrich` и
#: `scorer` обязаны получить одну и ту же двойку, а пропущенная в одном месте
#: пометка хуже отсутствующего поля — доклад владельцу о расходе второго прохода
#: строится фильтром `pass_no == 2` и припишет трату второго прохода первому
#: (Ц-2 пост-аудита Э2-Б).
SECOND_PASS_NO = 2


def split_contest_candidates(
    scored: list[ScoredCandidate],
) -> tuple[list[ScoredCandidate], list[ScoredCandidate]]:
    """Разделить оценённых кандидатов на обычных и конкурсных (РЕШ-92).

    Возвращает пару «обычные, конкурсные». Деление идёт по `source_type`
    кандидата, а не по площадке в адресе: тип проставляет адаптер по типу
    формулировки плана (Т-23), и он же управляет потолком зрелости (Т-22) — два
    разных способа опознать конкурсный класс молча разошлись бы.

    Смысл разделения — в арифметике весов (РЕШ-85): хакатонный прототип с
    потолком зрелости `prototype` проигрывает статье с промышленной апробацией
    0,8–1,0 балла **только на зрелости**. В общем `select_top` он почти никогда
    не попал бы в выдачу, а попав — вытеснил бы зрелое решение из обещанных
    клиенту 5–7. Поэтому конкурсные кандидаты не участвуют в общем отборе
    вовсе, а их карточки печатаются отдельной секцией вне лимита.
    """
    main: list[ScoredCandidate] = []
    contest: list[ScoredCandidate] = []
    for item in scored:
        if item.candidate.source_type is SourceType.CONTEST:
            contest.append(item)
        else:
            main.append(item)
    return main, contest


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

    # ── Вспомогательное ─────────────────────────────────────────────────────
    async def _notify(self, stage: str, message: str, fraction: float) -> None:
        log.info("[%s] %s", stage, message)
        if self._progress is None:
            return
        result = self._progress(stage, message, fraction)
        if asyncio.iscoroutine(result):
            await result

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
        for adapter in adapters:
            drain = getattr(adapter, "drain_limit_problems", None)
            if drain is not None:
                problems.extend(drain())

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
        self, candidates: list[Candidate], *, stage_log: list[StageRecord] | None = None, pass_no: int = 1
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

        `pass_no` — проход, которому принадлежит запись: обогащение зовётся и во
        втором проходе (РЕШ-81), и без явного номера его строка приписалась бы
        первому (Ц-2 пост-аудита Э2-Б).
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
                    StageRecord(
                        stage="enrich",
                        started_at=started,
                        finished_at=datetime.now(UTC),
                        pass_no=pass_no,
                        error=problem,
                    )
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
        contest_groups = stages.group_for_cards(contest_eligible, target=CONTEST_CARDS_TARGET)
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
        groups = stages.group_for_cards(selected, target=target)
        await self._notify("scorer", f"Отобрано вариантов для карточек: {len(groups)}", 0.6)
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

    def _cut_by_relevance(self, scored: list[ScoredCandidate]) -> list[ScoredCandidate]:
        """Отсечка по предметному соответствию (РЕШ-48) вне `select_top`.

        Нужна очереди конкурсного класса: `group_for_cards` не фильтрует ничем,
        и кандидат с `relevance` ниже порога доезжал до карточки и до клиента
        (Т-34, находка К-2 пост-аудита Э2-Б). Правило то же, что внутри
        `select_top`, включая исключение для нейтральных оценок: их проставил
        код после отказа батча скоринга, и один такой отказ иначе тихо выкосил
        бы всю секцию, а причина была бы неотличима от «все нерелевантны».
        """
        threshold = self._settings.relevance_min
        return [item for item in scored if stages.has_neutral_score(item) or item.relevance >= threshold]

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
