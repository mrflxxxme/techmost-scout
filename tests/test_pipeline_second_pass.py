"""Условный второй проход поиска (Т-21, РЕШ-81).

Провал критерия «карточки по делу» на воротах Э2-А случился не в отборе: у
агропрома максимум `relevance` по всем 72 кандидатам равен 4, и при любом
пороге отбирать было не из чего. Второй проход даёт системе увидеть собственный
промах: модель смотрит на первую выдачу, называет, чего не нашли, строит новый
план, и поиск идёт второй раз.

Что проверяется здесь:

* условие входа — максимум по оценкам МОДЕЛИ **строго ниже** порога (три случая:
  4 при пороге 6 — проход есть, 6 при 6 и 9 при 6 — прохода нет);
* нейтральные оценки отказавшего батча в максимум не входят: ровно пятёрка,
  которую ставит код, не имеет права погасить проход там, где модель не
  поставила выше четырёх;
* при выключенном пороге (значение 0, порог 6 — умолчание с РЕШ-103) прогон
  совпадает с прежним: одна стадия поиска, один план, ни одного лишнего
  вызова модели;
* отказ стадии `replan` прогон не роняет — отчёт собирается по первой выдаче, а
  отказ виден в журнале стадий;
* в скоринг второго прохода уходят ТОЛЬКО новые кандидаты (повтор оценки — это
  вторая оплата того же ответа и удвоение 25–30 минут прогона);
* одна из двух дословных строк решения печатается ВСЕГДА.

Сети нет нигде (инвариант №7): модель — провайдер-заглушка, отвечающий по
ключевой фразе роли в системном промпте, источники — синхронные адаптеры, как в
tests/test_pipeline_counters.py. База — временный SQLite в `tmp_path`, боевой
`data/scout.db` не задевается.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.llm import CostLimitExceededError, LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage
from app.models import Base, PipelineLog
from app.models import Candidate as CandidateRow
from app.models import Request as RequestRow
from app.models import SearchPlan as SearchPlanRow
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import Candidate, SourceType
from app.search.base import SearchAdapter, SearchError

# Ключевые фразы из "## Роль" промптов — по ним заглушка узнаёт стадию, не
# завися от порядка вызовов. Фраза replanner не пересекается с фразой планировщика:
# иначе один промпт узнавался бы как другой, и тест мерил бы не то.
_PLANNER_KEY = "специалист по научно-техническому поиску"
_REPLAN_KEY = "аналитик повторного поиска"
_SCORER_KEY = "аналитик технологического скаутинга"
_CARD_AUTHOR_KEY = "технический писатель службы подбора"
_VERIFIER_KEY = "научный редактор-контролёр"
_SUMMARY_KEY = "руководитель проекта подбора"

#: Пометка в тексте формулировки, по которой поддельный источник узнаёт запрос
#: ВТОРОГО плана: выдача второго прохода обязана отличаться от первой.
_SECOND_PASS_MARK = "повторно"


class _ScriptedProvider(LLMProvider):
    """Провайдер-заглушка: ответ выбирается по ключевой фразе системного
    сообщения, ответы на одну фразу выдаются по очереди, последний — повторно.

    Элемент `None` в очереди означает отказ модели на этом вызове; отказ
    объявлен неповторяемым, чтобы роутер не спал между попытками — тесту нужен
    отказ стадии, а не замер задержек.
    """

    name = "deepseek"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    def __init__(self, routes: dict[str, list[str | None]]) -> None:
        self._routes = {keyword: list(responses) for keyword, responses in routes.items()}
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
        self.calls.append(messages)
        system_text = messages[0].content if messages else ""
        for keyword, queue in self._routes.items():
            if keyword not in system_text:
                continue
            if not queue:
                raise LLMError(self.name, f"заготовленные ответы для «{keyword}» закончились", retryable=False)
            text = queue.pop(0) if len(queue) > 1 else queue[0]
            if text is None:
                raise LLMError(self.name, f"тестовый отказ стадии «{keyword}»", retryable=False)
            return LLMResult(
                text=text,
                usage=Usage(tokens_in=10, tokens_out=10, cost_rub=0.001, provider=self.name, model="fake"),
            )
        raise AssertionError(f"тест не подготовил ответ для запроса: {system_text[:120]}")

    async def health(self) -> tuple[bool, str]:
        return True, "заглушка"


def _candidate(number: int) -> Candidate:
    """Кандидат с устойчивым ключом склейки: по нему считается новизна выдачи."""
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=f"Кандидат {number}",
        url=f"https://doi.org/10.1/c{number}",
        # Организация у всех одна: `group_for_cards` группирует по ней, и одна
        # группа означает одну карточку — тест меряет проходы поиска, а не
        # число вызовов сборки карточек.
        org="Институт катализа",
        year=2025,
        snippet="Фрагмент описания кандидата.",
        raw_id=f"10.1/c{number}",
        provider="crossref",
    )


class _TwoPassAdapter(SearchAdapter):
    """Источник, чья выдача зависит от того, чей план его спрашивает.

    Формулировка первого плана даёт одну выдачу, формулировка второго (её видно
    по пометке в тексте) — другую. Так проверяется главное свойство второго
    прохода: он ищет ДРУГИМИ словами и приносит то, чего не было.

    `fail_second` — источник ответил первому проходу и отказал второму (429,
    таймаут, нечитаемое тело): адаптер честно поднимает `SearchError`, как
    предписывает инвариант №5 в редакции РЕШ-43.
    """

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    def __init__(
        self, client: httpx.AsyncClient, *, first: list[int], second: list[int], fail_second: bool = False
    ) -> None:
        super().__init__(client)
        self._first = first
        self._second = second
        self._fail_second = fail_second

    async def search(self, query, *, limit: int = 20) -> list[Candidate]:
        second_pass = _SECOND_PASS_MARK in query.text
        if second_pass and self._fail_second:
            raise SearchError(self.name, "источник ответил 429 на формулировку второго прохода")
        numbers = self._second if second_pass else self._first
        return [_candidate(number) for number in numbers]


def _plan_json(text: str) -> str:
    return json.dumps(
        {"queries": [{"text": text, "source_type": "publication", "lang": "ru", "rationale": "формулировка теста"}]},
        ensure_ascii=False,
    )


_PLAN_FIRST = _plan_json("катализатор гидроочистки сернистое сырьё")
_PLAN_SECOND = _plan_json(f"регенерация катализатора {_SECOND_PASS_MARK} другими словами")


def _scores_json(count: int, relevance: int) -> str:
    """Ответ скоринга на батч из `count` кандидатов с заданной релевантностью."""
    return json.dumps(
        {
            "scores": [
                {
                    "index": index,
                    "relevance": relevance,
                    "maturity": 5,
                    "accessibility": 5,
                    "freshness": 5,
                    "completeness": 5,
                    "notes": "оценка теста",
                }
                for index in range(count)
            ]
        },
        ensure_ascii=False,
    )


_CARD_JSON = json.dumps(
    {
        "title": "Катализатор с присадками",
        "essence": "Присадка продлевает срок службы катализатора на сернистом сырье.",
        "maturity_stage": "pilot",
        "trl_estimate": None,
        "developer_org": "Институт катализа",
        "developer_team": None,
        "contact_leads": [],
        "evidence": [{"claim": "Испытан на установке", "url": "https://doi.org/10.1/c1"}],
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "risks": [],
        "gov_support": [],
        "next_step": "Запросить протокол испытаний.",
        "confidence": "medium",
        "confidence_note": "Один источник",
        "candidate_urls": ["https://doi.org/10.1/c1"],
        "requirement_checks": [],
        "unsourced_claims": [],
        "pdf_urls": [],
    },
    ensure_ascii=False,
)

_VERIFY_JSON = json.dumps(
    {"needs_rework": False, "confidence": "medium", "confidence_note": "ок", "unsourced_claims": [], "notes": "ок"},
    ensure_ascii=False,
)

_SUMMARY_JSON = json.dumps(
    {"comparison_rows": [], "recommendation": "Рассмотреть вариант №1.", "caveats": []}, ensure_ascii=False
)


def _routes(
    *, first_relevance: int, second_relevance: int = 8, scorer_extra: list[str | None] | None = None
) -> dict[str, list[str | None]]:
    """Заготовленные ответы полного прогона.

    `scorer_extra` — что отвечать на батчи после первого: так собирается прогон
    с отказавшим батчем скоринга.
    """
    scorer: list[str | None] = [_scores_json(10, first_relevance)]
    scorer.extend(scorer_extra or [_scores_json(4, second_relevance)])
    return {
        _PLANNER_KEY: [_PLAN_FIRST],
        _REPLAN_KEY: [_PLAN_SECOND],
        _SCORER_KEY: scorer,
        _CARD_AUTHOR_KEY: [_CARD_JSON],
        _VERIFIER_KEY: [_VERIFY_JSON],
        _SUMMARY_KEY: [_SUMMARY_JSON],
    }


@pytest.fixture
async def db_session_factory(tmp_path):
    """Фабрика сессий на временном SQLite — той же формы, что `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'second-pass-test.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    yield factory
    await engine.dispose()


class _LimitReportingAdapter(_TwoPassAdapter):
    """Тот же источник, но с исчерпанным платным пределом на одну формулировку.

    Нужен для РЕШ-127 п. 2: до правки пропуск по платному пределу доезжал
    только до `pipeline_logs`, и клиент видел недобор источника неотличимо от
    «источник ответил и ничего не нашёл» — ровно то, что запрещает инвариант
    №5. Метод повторяет контракт `YandexSearchAdapter.drain_limit_problems`:
    отдаёт накопленное и обнуляет счёт, чтобы записи первого прохода не
    приехали во второй и не задвоили недобор.
    """

    def __init__(self, *args, skipped: int = 1, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pending = [f"формулировка №{n} не отправлена: исчерпан платный предел" for n in range(skipped)]

    def drain_limit_problems(self) -> list[str]:
        drained, self._pending = self._pending, []
        return drained


class _LimitReportingBothPassesAdapter(_TwoPassAdapter):
    """Платный предел исчерпан на КАЖДОМ проходе отдельно (пост-аудит РЕШ-126/127,
    правка 2): первый вызов ``drain_limit_problems`` отдаёт пропуск первого
    прохода, второй вызов — пропуск второго. `_LimitReportingAdapter` выше
    умеет отдавать пропуск только один раз (на том проходе, что спросит
    первым) — этой формы достаточно для дыры в стороже накопления
    (`_limit_skipped_total`), но не для обратной проверки «предел исчерпан на
    обоих проходах — оговорка не задваивается»."""

    def __init__(self, *args, skipped_by_pass: list[int], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._skipped_by_pass = list(skipped_by_pass)
        self._drain_calls = 0

    def drain_limit_problems(self) -> list[str]:
        if self._drain_calls >= len(self._skipped_by_pass):
            return []
        count = self._skipped_by_pass[self._drain_calls]
        self._drain_calls += 1
        return [f"формулировка №{n} не отправлена: исчерпан платный предел" for n in range(count)]


async def _run_pipeline(
    settings,
    brief,
    provider: _ScriptedProvider,
    session_factory,
    *,
    threshold: int,
    first: list[int],
    second: list[int],
    fail_second: bool = False,
    limit_skipped: int | None = None,
    adapter_factory: Callable[[httpx.AsyncClient], SearchAdapter] | None = None,
):
    """Полный прогон с заданным порогом второго прохода и двумя выдачами.

    `fail_second` — источник отказывает на формулировке второго плана.
    `limit_skipped` — источник сообщает о стольких формулировках, не
    отправленных из-за исчерпанного платного предела (РЕШ-127 п. 2), один раз,
    на том проходе, что спросит первым.
    `adapter_factory` — свой адаптер вместо трёх встроенных вариантов (нужен
    `_LimitReportingBothPassesAdapter`, у которого пропуск свой на каждом
    проходе).
    """
    tuned = settings.model_copy(update={"second_pass_relevance_max": threshold})
    router = LLMRouter(tuned, providers={"deepseek": provider})
    async with httpx.AsyncClient() as client:
        if adapter_factory is not None:
            adapter = adapter_factory(client)
        elif limit_skipped is None:
            adapter = _TwoPassAdapter(client, first=first, second=second, fail_second=fail_second)
        else:
            adapter = _LimitReportingAdapter(
                client, first=first, second=second, fail_second=fail_second, skipped=limit_skipped
            )
        pipeline = ScoutPipeline(
            settings=tuned,
            router=router,
            adapters=[adapter],
            session_factory=session_factory,
        )
        return await pipeline.run(brief, request_id=1, raw_text="задача теста")


async def _rows(session_factory, model):
    async with session_factory() as session:
        return (await session.execute(select(model))).scalars().all()


# ── Критерий 1: условие входа — строго ниже порога ──────────────────────────
@pytest.mark.parametrize(
    ("relevance", "expected_second_pass"),
    [(4, True), (6, False), (9, False)],
    ids=["максимум 4 при пороге 6", "максимум 6 при пороге 6", "максимум 9 при пороге 6"],
)
async def test_второй_проход_только_при_максимуме_строго_ниже_порога(
    settings, brief, db_session_factory, relevance, expected_second_pass
):
    """Три случая порога 6. Максимум 4 — проход есть, максимум 6 — прохода нет.

    Граница проверяется именно случаем «максимум равен порогу»: замена условия
    со «строго ниже» на «не выше» пройдёт мимо случаев 4 и 9 и убьётся только
    здесь. Смысл границы: порог — уровень годной выдачи, и выдача, дотянувшая
    до него, годной уже считается.
    """
    provider = _ScriptedProvider(_routes(first_relevance=relevance))
    report = await _run_pipeline(
        settings,
        brief,
        provider,
        db_session_factory,
        threshold=6,
        first=list(range(1, 11)),
        second=[1, 2, 3, 4, 5, 6, 11, 12, 13, 14],
    )

    assert report.cards, "прогон обязан дойти до карточек в любом из трёх случаев"
    logs = await _rows(db_session_factory, PipelineLog)
    plans = await _rows(db_session_factory, SearchPlanRow)
    replan_logs = [row for row in logs if row.stage == "replan"]
    searches = [row for row in logs if row.stage == "search"]

    if expected_second_pass:
        assert len(replan_logs) == 1, "второй проход обязан оставить в журнале стадию replan"
        assert len(searches) == 2, "второй проход поиска обязан быть отдельной строкой журнала"
        assert sorted(row.pass_no for row in plans) == [1, 2], "план второго прохода пишется с pass_no = 2"
    else:
        assert replan_logs == [], "выдача не ниже порога — стадии replan быть не должно"
        assert len(searches) == 1
        assert [row.pass_no for row in plans] == [1]


# ── Критерий 1 (продолжение): нейтральные оценки в максимум не входят ───────
async def test_нейтральные_оценки_отказавшего_батча_не_гасят_второй_проход(settings, brief, db_session_factory, caplog):
    """Отказавший батч скоринга получает от кода ровно пятёрки, и они НЕ имеют
    права считаться максимумом выдачи.

    Набор подобран так, что ошибка видна: модель поставила максимум 4, порог
    равен 5. Учтись нейтральные пятёрки в максимуме — он стал бы 5, условие
    «строго ниже 5» перестало бы выполняться, и второй проход не запустился бы
    ровно на той выдаче, ради которой он затеян (агропром, максимум 4 при
    отказавших батчах). На наборе с высоким максимумом эта подмена не видна
    вовсе — поэтому здесь и максимум ниже пяти, и настоящий отказ батча.
    """
    caplog.set_level(logging.INFO, logger="app.pipeline.stages")
    # 13 кандидатов: первый батч (10) оценён моделью, второй (3) отказал.
    provider = _ScriptedProvider(_routes(first_relevance=4, scorer_extra=[None]))
    await _run_pipeline(
        settings,
        brief,
        provider,
        db_session_factory,
        threshold=5,
        first=list(range(1, 14)),
        second=[1, 2, 3, 20, 21],
    )

    assert "скоринг батча не удался" in caplog.text, "тест обязан идти по настоящему отказу батча, иначе он слеп"
    candidates = await _rows(db_session_factory, CandidateRow)
    neutral_first_pass = [row for row in candidates if row.pass_no == 1 and row.score.get("neutral")]
    assert len(neutral_first_pass) == 3, "нейтральные оценки обязаны быть в первой выдаче — иначе проверять нечего"

    assert "второй проход: максимум relevance 4 ниже порога 5 — иду повторно" in caplog.text
    logs = await _rows(db_session_factory, PipelineLog)
    assert [row.stage for row in logs].count("replan") == 1


# ── Критерий 2: выключенный порог не меняет прогон ──────────────────────────
async def test_при_выключенном_пороге_прогон_совпадает_с_прежним(
    settings, brief, db_session_factory, caplog, monkeypatch
):
    """`SECOND_PASS_RELEVANCE_MAX = 0` (проход явно выключен, а не умолчание —
    оно с РЕШ-103 равно рабочему значению 6) — один план, одна стадия поиска
    и ровно шесть вызовов модели, как до задачи плюс один судья дублей.

    Шесть — это эталон прогона без второго прохода: планировщик, скоринг
    одного батча, судья дублей (Д-6 Э2-З, РЕШ-152 — десять кандидатов первой
    выдачи, вызов один на прогон), сборка карточки, её проверка, сводка.
    Безусловный второй проход добавил бы к ним минимум вызов `replan` и
    второй скоринг.

    Отсечка `relevance_min` здесь выключена явно через окружение: умолчание
    сменилось на 3 (РЕШ-174), а условие теста — оценка 1 у всех кандидатов
    первой выдачи — завязано на прежнее поведение отбора, а не на второй
    проход, который здесь и проверяется. `select_top` без явного аргумента
    читает порог из закэшированного `get_settings()`, а не из объекта
    `settings`, поэтому отключать нужно через переменную окружения — так же,
    как это делает рабочий код.
    """
    from app.config import get_settings

    caplog.set_level(logging.INFO, logger="app.pipeline.stages")
    monkeypatch.setenv("RELEVANCE_MIN", "0")
    get_settings.cache_clear()
    tuned = settings.model_copy(update={"relevance_min": 0})
    provider = _ScriptedProvider(_routes(first_relevance=1))
    report = await _run_pipeline(
        tuned,
        brief,
        provider,
        db_session_factory,
        threshold=0,
        first=list(range(1, 11)),
        second=[1, 2, 3, 11, 12],
    )

    # Судья дублей отвечает произвольным JSON, который его системная фраза не
    # ловит ни одним ключевым словом `_ScriptedProvider` (справедливо: у него
    # свой промпт) — деградирует до `None` (см. `dedup_judge.judge_duplicate_groups`),
    # но вызов всё равно случился и посчитан. Шесть, а не пять, — это ожидаемая
    # цена подключения судьи (Д-6 Э2-З), а не лишний вызов сверх эталона.
    assert len(provider.calls) == 6, "при выключенном пороге второго прохода лишних вызовов сверх судьи дублей нет"
    assert report.cards
    logs = await _rows(db_session_factory, PipelineLog)
    assert [row.stage for row in logs] == ["planner", "search", "scorer", "dedup_judge", "cards", "summary"]
    plans = await _rows(db_session_factory, SearchPlanRow)
    assert [row.pass_no for row in plans] == [1]
    candidates = await _rows(db_session_factory, CandidateRow)
    assert {row.pass_no for row in candidates} == {1}


# ── Критерий 3: отказ стадии replan не роняет прогон ────────────────────────
async def test_отказ_стадии_replan_не_роняет_прогон(settings, brief, db_session_factory):
    """Модель отказала на `replan` — отчёт всё равно собирается по первой
    выдаче, а факт отказа виден в журнале стадий строкой с непустым `error`
    (инвариант №5: отказ стадии не роняет прогон, но и не прячется)."""
    routes = _routes(first_relevance=4)
    routes[_REPLAN_KEY] = [None]
    provider = _ScriptedProvider(routes)
    report = await _run_pipeline(
        settings,
        brief,
        provider,
        db_session_factory,
        threshold=6,
        first=list(range(1, 11)),
        second=[1, 2, 3, 11, 12],
    )

    assert report.cards, "отказ replan обязан оставить прогон с карточками первой выдачи"
    logs = await _rows(db_session_factory, PipelineLog)
    replan_logs = [row for row in logs if row.stage == "replan"]
    assert len(replan_logs) == 1
    assert replan_logs[0].error, "молчаливый отказ стадии неотличим от «порог не сработал»"
    assert [row.stage for row in logs].count("search") == 1, "после отказа replan второго поиска быть не может"
    plans = await _rows(db_session_factory, SearchPlanRow)
    assert [row.pass_no for row in plans] == [1], "плана второго прохода не существует — писать его нечем"


# ── Критерий 4: в скоринг второго прохода уходят только новые кандидаты ─────
async def test_в_скоринг_второго_прохода_уходят_только_новые_кандидаты(
    settings, brief, db_session_factory, monkeypatch
):
    """Первая выдача — 10 кандидатов, вторая — 6 тех же и 4 новых. В скоринг
    второго прохода уходит ровно 4.

    Повторная оценка уже оценённых — это вторая оплата того же ответа и
    удвоение 25–30 минут прогона без единого нового кандидата.
    """
    seen: list[list[Candidate]] = []
    original = stages.score_candidates

    async def recording(router, task_brief, candidates, **kwargs):
        seen.append(list(candidates))
        return await original(router, task_brief, candidates, **kwargs)

    monkeypatch.setattr(stages, "score_candidates", recording)

    provider = _ScriptedProvider(_routes(first_relevance=4))
    await _run_pipeline(
        settings,
        brief,
        provider,
        db_session_factory,
        threshold=6,
        first=list(range(1, 11)),
        second=[1, 2, 3, 4, 5, 6, 11, 12, 13, 14],
    )

    assert [len(batch) for batch in seen] == [10, 4], "во второй скоринг обязаны уйти только 4 новых кандидата"
    assert {candidate.raw_id for candidate in seen[1]} == {"10.1/c11", "10.1/c12", "10.1/c13", "10.1/c14"}

    rows = await _rows(db_session_factory, CandidateRow)
    by_pass: dict[int, list[CandidateRow]] = {}
    for row in rows:
        by_pass.setdefault(row.pass_no, []).append(row)
    assert sorted(by_pass) == [1, 2]
    assert len(by_pass[1]) == 10
    assert len(by_pass[2]) == 4
    # Судьба кандидата пишется по своему проходу: строк со статусом `selected`
    # не может быть больше, чем кандидатов в отборе (target_cards * 2 = 12).
    selected = [row for row in rows if row.status == "selected"]
    assert len(selected) <= settings.target_cards * 2
    assert len({row.url for row in selected}) == len(selected), "один ключ склейки не может быть отобран дважды"


# ── Критерий 5: одна из двух строк решения печатается всегда ────────────────
@pytest.mark.parametrize(
    ("threshold", "relevance", "expected_line"),
    [
        (0, 4, "второй проход не нужен: максимум relevance 4 не ниже порога 0"),
        (6, 9, "второй проход не нужен: максимум relevance 9 не ниже порога 6"),
        (6, 4, "второй проход: максимум relevance 4 ниже порога 6 — иду повторно"),
    ],
    ids=["порог выключен", "выдача годная", "выдача мимо"],
)
async def test_решение_о_втором_проходе_печатается_всегда(
    settings, brief, db_session_factory, caplog, threshold, relevance, expected_line
):
    """Печатается ровно одна из двух дословных строк — в том числе при
    выключенном пороге.

    Молчание в ветке «проход не нужен» сделало бы «порог не сработал»
    неотличимым от «код до проверки не дошёл»: тот же урок, ради которого
    всегда печатается строка калибровки `relevance`.
    """
    caplog.set_level(logging.INFO, logger="app.pipeline.stages")
    provider = _ScriptedProvider(_routes(first_relevance=relevance))
    await _run_pipeline(
        settings,
        brief,
        provider,
        db_session_factory,
        threshold=threshold,
        first=list(range(1, 11)),
        second=[1, 2, 3, 11, 12, 13, 14],
    )

    decisions = [line for line in caplog.text.splitlines() if "второй проход" in line]
    assert len(decisions) == 1, f"строка решения обязана быть ровно одна, найдено: {decisions}"
    assert expected_line in decisions[0]


# ── Инвариант №5: отказ источника во втором проходе виден в отчёте ──────────
async def test_отказ_источника_во_втором_проходе_доезжает_до_отчёта(settings, brief, db_session_factory):
    """Источник ответил первому проходу и отказал второму — его имя обязано
    попасть в `sources_unavailable` отчёта, а прогон — дойти до карточек.

    Это дословно инвариант №5: скрытый отказ хуже видимого. На Э1-2 источник
    отвечал 429 три прогона подряд, и ни один отчёт об этом не сказал, потому
    что отказ гасился в пустой список. Второй проход завёл вторую, отдельную
    выдачу со своим списком недоступных: не слей его с первым — и отчёт объявит
    полное покрытие источников там, где половина запросов не дозвалась. Разбор
    выдачи на живом прогоне (Т-29) при этом не отличит «источник ничего не
    нашёл» от «источник не ответил».
    """
    provider = _ScriptedProvider(_routes(first_relevance=4))
    report = await _run_pipeline(
        settings,
        brief,
        provider,
        db_session_factory,
        threshold=6,
        first=list(range(1, 11)),
        second=[11, 12, 13, 14],
        fail_second=True,
    )

    assert report.sources_unavailable == ["crossref"], (
        "источник, не ответивший второму проходу, обязан быть назван в отчёте — "
        "молчание здесь неотличимо от полного покрытия источников"
    )
    assert report.cards, "отказ источника во втором проходе прогон не роняет: карточки первой выдачи на месте"

    logs = await _rows(db_session_factory, PipelineLog)
    assert [row.stage for row in logs].count("search") == 2, "второй поиск состоялся и отказал именно в нём"
    candidates = await _rows(db_session_factory, CandidateRow)
    assert {row.pass_no for row in candidates} == {1}, "второй проход не принёс ни одного кандидата — их и нет в базе"


# ── РЕШ-45: стоп-кран по стоимости не смягчается новой стадией ──────────────
async def test_стоп_кран_на_стадии_replan_роняет_прогон(settings, brief, db_session_factory, monkeypatch):
    """`CostLimitExceededError` на `replan` — команда владельца «стоп тратить»,
    а не отказ модели: она обязана уйти наружу, а не попасть в широкий
    `except Exception` рядом.

    Разница дорогая. Отказ стадии оставляет строку журнала с `error`, и прогон
    честно идёт дальше на первой выдаче — за карточки и сводку платится ещё
    раз. Исчерпанный лимит означает обратное: платить больше нечем. Проект
    закрыл этот риск для интейка, уточнения брифа, скоринга, extractor, сборки
    карточек и сводки; `replan` — последняя стадия конвейера, у которой такого
    замка не было.
    """

    async def broken_replan(router, task_brief, plan, scored):
        raise CostLimitExceededError("запрос израсходовал 10.00 ₽ при лимите 10.00 ₽")

    monkeypatch.setattr(stages, "replan_search", broken_replan)
    provider = _ScriptedProvider(_routes(first_relevance=4))

    with pytest.raises(CostLimitExceededError):
        await _run_pipeline(
            settings,
            brief,
            provider,
            db_session_factory,
            threshold=6,
            first=list(range(1, 11)),
            second=[11, 12, 13, 14],
        )

    # Оборванный стоп-краном прогон обязан оставить след: статус `failed` и уже
    # потраченные деньги (РЕШ-40, П-5 аудита) — иначе трата была бы, а записи о
    # ней нет.
    requests = await _rows(db_session_factory, RequestRow)
    assert [row.status for row in requests] == ["failed"]
    assert requests[0].error
    logs = await _rows(db_session_factory, PipelineLog)
    assert "cards" not in [row.stage for row in logs], "после стоп-крана прогон не имеет права платить дальше"


# ── РЕШ-127 п. 2: исчерпанный платный предел доезжает до клиентского отчёта ──
async def test_исчерпанный_платный_предел_доезжает_до_отчёта(settings, brief, db_session_factory):
    """Пропуск формулировки по платному пределу виден клиенту, а не только в журнале.

    Инвариант №5 требует, чтобы недобор источника был виден в ОТЧЁТЕ. До правки
    пропуск уезжал в `SearchOutcome.problems`, оттуда в `StageRecord.error`
    стадии `search` — то есть в `pipeline_logs`, и дальше никуда. Клиент видел
    ровно то же, что при «источник ответил и ничего не нашёл».

    Признак — счётчик пропущенных формулировок, а НЕ подстрока в тексте
    сообщения источника: текст поменяется, и проверка молча исчезнет.
    """
    provider = _ScriptedProvider(_routes(first_relevance=9))
    report = await _run_pipeline(
        settings,
        brief,
        provider,
        db_session_factory,
        threshold=0,
        first=list(range(1, 11)),
        second=[],
        limit_skipped=2,
    )

    from app.export.markdown import PAID_LIMIT_NOTICE, render_markdown

    assert PAID_LIMIT_NOTICE in report.summary.caveats, (
        "исчерпанный платный предел обязан доезжать до оговорок отчёта, а не оставаться в журнале"
    )
    assert PAID_LIMIT_NOTICE in render_markdown(report), "оговорка не дошла до собранного markdown"


async def test_без_исчерпания_предела_оговорки_нет(settings, brief, db_session_factory):
    """Обратная сторона: оговорка не печатается там, где предел не исчерпан.

    Без этой проверки оговорка, печатаемая всегда, прошла бы предыдущий тест и
    сделала бы недобор неотличимым от нормы — уже в другую сторону.
    """
    provider = _ScriptedProvider(_routes(first_relevance=9))
    report = await _run_pipeline(
        settings, brief, provider, db_session_factory, threshold=0, first=list(range(1, 11)), second=[]
    )

    from app.export.markdown import PAID_LIMIT_NOTICE, render_markdown

    assert PAID_LIMIT_NOTICE not in report.summary.caveats
    assert PAID_LIMIT_NOTICE not in render_markdown(report)


# ── Пост-аудит РЕШ-126/127 (правка 2): накопление счётчика по проходам ──────
async def test_платный_предел_исчерпан_только_на_первом_проходе_оговорка_видна(settings, brief, db_session_factory):
    """`_limit_skipped_total` копится по обоим проходам через `+=` — до этого
    теста мутация `+=` -> `=` (обнуление на каждом проходе) переживала весь
    набор: 639 passed. Здесь предел исчерпан только на ПЕРВОМ проходе
    (`_LimitReportingAdapter` отдаёт пропуск ровно один раз, тому проходу, что
    спросит первым — здесь это первый), второй проход чистый.

    При `=` вместо `+=` второй, чистый вызов `gather_candidates`
    (`limit_skipped = 0`) стирает накопленное на первом проходе — оговорка
    исчезает из отчёта именно там, где предел действительно был исчерпан.
    """
    provider = _ScriptedProvider(_routes(first_relevance=4))
    report = await _run_pipeline(
        settings,
        brief,
        provider,
        db_session_factory,
        threshold=6,
        first=list(range(1, 11)),
        second=[1, 2, 3, 4, 5, 6, 11, 12, 13, 14],
        limit_skipped=2,
    )

    from app.export.markdown import PAID_LIMIT_NOTICE

    assert report.summary.caveats.count(PAID_LIMIT_NOTICE) == 1, (
        "предел исчерпан на первом проходе — оговорка обязана дойти до отчёта, "
        "а второй, чистый проход не имеет права её стереть"
    )


async def test_платный_предел_исчерпан_на_обоих_проходах_оговорка_не_задваивается(settings, brief, db_session_factory):
    """Обратная сторона предыдущего теста: предел исчерпан на ОБОИХ проходах —
    оговорка обязана остаться ровно одной записью в `caveats`, а не появиться
    дважды (по разу на проход)."""
    provider = _ScriptedProvider(_routes(first_relevance=4))

    def factory(client: httpx.AsyncClient) -> SearchAdapter:
        return _LimitReportingBothPassesAdapter(
            client,
            first=list(range(1, 11)),
            second=[1, 2, 3, 4, 5, 6, 11, 12, 13, 14],
            skipped_by_pass=[2, 1],
        )

    report = await _run_pipeline(
        settings,
        brief,
        provider,
        db_session_factory,
        threshold=6,
        first=list(range(1, 11)),
        second=[1, 2, 3, 4, 5, 6, 11, 12, 13, 14],
        adapter_factory=factory,
    )

    from app.export.markdown import PAID_LIMIT_NOTICE

    assert report.summary.caveats.count(PAID_LIMIT_NOTICE) == 1, "оговорка не имеет права появиться дважды"
