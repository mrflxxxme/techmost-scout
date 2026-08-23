"""Накопительный поиск: кандидаты прошлых прогонов той же задачи (З-4, РЕШ-163).

Причина заведения — измеренная на пробе З-3: формулировки поиска придумывает
модель (РЕШ-109), и два прогона одной задачи ищут разное — пулы пересеклись на
6 кандидатах из 72, обе «семёрки» первого прогона во втором пуле отсутствовали.
Лечится не строгость скоринга, а память: однажды найденное и оценённое не
теряется, а накапливается прогон к прогону, и повторно за него не платят.

Проверяется по слоям, снизу вверх:

* чистые функции восстановления и учёта (`app.pipeline.history`) — без БД;
* чтение из базы (`app.pipeline.persistence.load_history_pool`) — признак
  «та же задача» обоими путями решения Р-1 (текст и явный номер), чужая
  задача не подмешивается (критерий 6);
* полный прогон `ScoutPipeline.run` на временном SQLite — критерии 1, 2, 3, 4.

Сети нет нигде (инвариант №7): модель — `FakeProvider`/провайдер-заглушка,
источник — синхронный адаптер поверх `httpx.AsyncClient` без реального запроса.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.llm import LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage
from app.models import Base
from app.models import Candidate as CandidateRow
from app.pipeline.history import (
    HistoryPool,
    HistoryRow,
    build_history_pool,
    exclude_known,
    format_history_summary,
    normalize_task_text,
    restore_history_candidate,
)
from app.pipeline.orchestrator import ScoutPipeline
from app.pipeline.persistence import CandidateRecord, HistoryReadError, load_history_pool, persist_run
from app.schemas import Candidate, ScoredCandidate, SourceType
from app.search.base import SearchAdapter

# ── Помощники, общие для всех уровней проверки ──────────────────────────────


def _score(relevance: int, *, neutral: bool = False) -> dict:
    return {
        "relevance": relevance,
        "maturity": 5,
        "accessibility": 5,
        "freshness": 5,
        "completeness": 5,
        "total": relevance * 0.35 + 5 * 0.65,
        "notes": "",
        "neutral": neutral,
    }


def _row(number: int, *, score: dict | None = None, dedup_key: str | None = None, request_id: int = 1) -> HistoryRow:
    key = dedup_key if dedup_key is not None else f"10.1/hist-{number}"
    return HistoryRow(
        source_type=SourceType.PUBLICATION.value,
        provider="crossref",
        title=f"Кандидат {number}",
        url=f"https://doi.org/10.1/hist-{number}",
        org="Институт катализа",
        authors=[],
        year=2025,
        snippet="Фрагмент описания.",
        raw_id=f"10.1/hist-{number}",
        dedup_key=key,
        score=score if score is not None else _score(8),
        request_id=request_id,
    )


# ── Уровень 1: чистые функции восстановления (app.pipeline.history) ────────


def test_normalize_task_text_гасит_регистр_и_пробелы():
    assert normalize_task_text("  Текст   Задачи\n") == normalize_task_text("текст задачи")
    assert normalize_task_text("Текст А") != normalize_task_text("Текст Б")


def test_restore_принимает_годную_строку():
    scored, reason = restore_history_candidate(_row(1))
    assert reason is None
    assert isinstance(scored, ScoredCandidate)
    assert scored.relevance == 8
    assert scored.candidate.dedup_key() == "10.1/hist-1"


def test_restore_отбрасывает_пустую_оценку():
    scored, reason = restore_history_candidate(_row(1, score={}))
    assert scored is None
    assert reason == "пустая оценка"


def test_restore_отбрасывает_нейтральную_оценку():
    scored, reason = restore_history_candidate(_row(1, score=_score(5, neutral=True)))
    assert scored is None
    assert reason == "нейтральная оценка"


def test_restore_отбрасывает_несовпавший_ключ_склейки():
    scored, reason = restore_history_candidate(_row(1, dedup_key="совсем другой ключ"))
    assert scored is None
    assert reason == "ключ склейки не совпал"


def test_build_history_pool_считает_взятое_и_отбраковку_по_причинам():
    rows = [
        _row(1),
        _row(2, score={}),
        _row(3, score=_score(5, neutral=True)),
        _row(4, dedup_key="испорченный ключ"),
    ]
    pool = build_history_pool(rows)
    assert pool.taken == 1
    assert pool.dropped == {
        "пустая оценка": 1,
        "нейтральная оценка": 1,
        "ключ склейки не совпал": 1,
    }


def test_build_history_pool_склеивает_дубли_по_dedup_key_и_берёт_лучший_total():
    """Пост-аудит Э2-Е, находка Б-1: `run_flow.py` пишет пул истории обратно в
    `candidates` (`pass_no=0`), и следующий прогон читает и исходную строку, и
    её же копии. Без склейки один и тот же кандидат попадает в пул истории
    несколько раз — здесь три строки с одним ключом и разными оценками должны
    свестись к одной, с наибольшим `total`."""
    rows = [
        _row(1, score=_score(5), request_id=1),
        _row(1, score=_score(9), request_id=2),  # копия того же кандидата, найденная позже
        _row(1, score=_score(3), request_id=3),  # ещё одна копия, оценка хуже — не должна победить
    ]
    pool = build_history_pool(rows)
    assert pool.taken == 1
    assert pool.dropped == {}
    assert pool.scored[0].relevance == 9


def test_format_history_summary_называет_каждую_причину_и_число():
    pool = HistoryPool(
        scored=[restore_history_candidate(_row(1))[0]], dropped={"пустая оценка": 2, "нейтральная оценка": 1}
    )
    text = format_history_summary(pool)
    assert "из истории взято 1" in text
    assert "отброшено по причине нейтральная оценка — 1" in text
    assert "отброшено по причине пустая оценка — 2" in text


def test_exclude_known_убирает_кандидатов_с_известным_ключом():
    history = [restore_history_candidate(_row(1))[0], restore_history_candidate(_row(2))[0]]
    fresh = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Новый",
        url="https://doi.org/10.1/hist-3",
        raw_id="10.1/hist-3",
        provider="crossref",
    )
    known_again = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Кандидат 1",
        url="https://doi.org/10.1/hist-1",
        raw_id="10.1/hist-1",
        provider="crossref",
    )
    result = exclude_known([fresh, known_again], history)
    assert result == [fresh]


# ── Уровень 2: чтение из базы (app.pipeline.persistence.load_history_pool) ──


@pytest.fixture
async def db_session_factory(tmp_path):
    """Фабрика сессий на временном SQLite — той же формы, что `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history-reuse-test.db'}", future=True)
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


async def _seed(session_factory, *, raw_text: str, status: str = "drafted", candidates: list[Candidate] | None = None):
    """Записать прогон-«прошлое» напрямую через `persist_run`, без пайплайна."""
    records = [
        CandidateRecord(candidate=candidate, score=_score(8), status="selected") for candidate in (candidates or [])
    ]
    return await persist_run(
        session_factory,
        raw_text=raw_text,
        status=status,
        cost_rub=0.0,
        cards=[],
        contest_cards=[],
        stages=[],
        candidates=records,
    )


def _candidate(number: int, *, raw_id: str | None = None) -> Candidate:
    key = raw_id or f"10.1/seed-{number}"
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=f"Кандидат {number}",
        url=f"https://doi.org/{key}",
        org="Институт катализа",
        year=2025,
        snippet="Фрагмент описания.",
        raw_id=key,
        provider="crossref",
    )


async def test_load_history_pool_подхватывает_по_нормализованному_тексту(db_session_factory):
    await _seed(db_session_factory, raw_text="  Текст   задачи  \n", candidates=[_candidate(1), _candidate(2)])

    pool = await load_history_pool(db_session_factory, raw_text="текст задачи")

    assert pool.taken == 2


async def test_load_history_pool_явный_номер_важнее_текста(db_session_factory):
    request_id = await _seed(db_session_factory, raw_text="совсем другими словами", candidates=[_candidate(1)])

    pool = await load_history_pool(
        db_session_factory, raw_text="текст, который не совпадёт ни с чем", explicit_request_id=request_id
    )

    assert pool.taken == 1


async def test_load_history_pool_чужая_задача_не_подмешивается(db_session_factory):
    """Критерий 6: в базе есть прогон другой задачи — её кандидаты не попадают."""
    await _seed(db_session_factory, raw_text="задача про катализаторы", candidates=[_candidate(1), _candidate(2)])
    await _seed(db_session_factory, raw_text="совсем другая задача про диагностику УЗИ", candidates=[_candidate(3)])

    pool = await load_history_pool(db_session_factory, raw_text="задача про катализаторы")

    assert pool.taken == 2
    assert {item.candidate.dedup_key() for item in pool.scored} == {"10.1/seed-1", "10.1/seed-2"}


async def test_load_history_pool_отказавший_прогон_не_учитывается(db_session_factory):
    await _seed(db_session_factory, raw_text="упавшая задача", status="failed", candidates=[_candidate(1)])

    pool = await load_history_pool(db_session_factory, raw_text="упавшая задача")

    assert pool.taken == 0


async def test_три_прогона_одной_задачи_не_удваивают_пул_истории(db_session_factory):
    """Мутация Б-1 пост-аудита Э2-Е: `run_flow.py` пишет пул истории обратно в
    `candidates` со своим `pass_no=0` (пункт 6 задачи З-4) — следующий прогон
    читает и исходные строки, и их копии. Аудит замерил на пяти прогонах трёх
    кандидатов кратное удвоение пула истории (3 → 6 → 12 → 24 → 48); здесь то
    же самое на трёх прогонах: без склейки по `dedup_key` третий прогон отдал
    бы 6 записей на 3 уникальных ключа, со склейкой — ровно 3.
    """
    task_text = "Задача для проверки накопления без удвоения пула истории."
    candidates = [_candidate(1), _candidate(2), _candidate(3)]

    # Прогон 1: своих кандидатов в истории ещё нет, пишем их напрямую.
    await _seed(db_session_factory, raw_text=task_text, candidates=candidates)

    # Прогон 2: читаем историю (3 уникальных), пишем её обратно с pass_no=0 —
    # ровно то, что делает run_flow.py после каждого прогона.
    pool2 = await load_history_pool(db_session_factory, raw_text=task_text)
    assert pool2.taken == 3
    await persist_run(
        db_session_factory,
        raw_text=task_text,
        status="drafted",
        cost_rub=0.0,
        cards=[],
        contest_cards=[],
        stages=[],
        candidates=[
            CandidateRecord(candidate=item.candidate, score=_score(item.relevance), status="selected", pass_no=0)
            for item in pool2.scored
        ],
    )

    # Прогон 3: история теперь читается из прогона 1 (3 исходных строки) и
    # прогона 2 (3 их копии) — без склейки это дало бы 6 записей на 3 ключа.
    pool3 = await load_history_pool(db_session_factory, raw_text=task_text)
    assert pool3.taken == 3, "склейка по dedup_key обязана свести дубли к уникальным ключам"
    assert pool3.dropped == {}
    assert {item.candidate.dedup_key() for item in pool3.scored} == {
        "10.1/seed-1",
        "10.1/seed-2",
        "10.1/seed-3",
    }


async def test_load_history_pool_без_фабрики_отдаёт_пустой_пул():
    pool = await load_history_pool(None, raw_text="что угодно")
    assert pool.scored == []
    assert pool.dropped == {}


async def test_load_history_pool_поднимает_typed_ошибку_на_отказе_чтения():
    def broken_factory():
        raise RuntimeError("база залокирована другим процессом")

    with pytest.raises(HistoryReadError):
        await load_history_pool(broken_factory, raw_text="что угодно")


async def test_load_and_log_пишет_причину_отбраковки_в_лог(db_session_factory, caplog):
    """Критерий 5, целиком через `history.load_and_log`: причина — в логе."""
    from app.pipeline import history as history_module

    request_id = await _seed(db_session_factory, raw_text="задача с порчей строки", candidates=[_candidate(1)])
    async with db_session_factory() as session:
        row = (await session.execute(select(CandidateRow).where(CandidateRow.request_id == request_id))).scalars().one()
        row.score = {}  # пустая оценка — одна из трёх причин отбраковки

    caplog.set_level(logging.INFO, logger="app.pipeline.orchestrator")
    stage_log: list = []
    pool = await history_module.load_and_log(
        db_session_factory, raw_text="задача с порчей строки", explicit_request_id=None, stage_log=stage_log
    )

    assert pool.taken == 0
    assert "отброшено по причине пустая оценка — 1" in caplog.text
    assert stage_log == []  # отказа чтения не было — сама строка просто негодная


async def test_load_and_log_отказ_чтения_уходит_в_журнал_стадий_и_не_роняет():
    """Инвариант №5: отказ чтения истории — StageRecord с ошибкой, пул пуст."""
    from app.pipeline import history as history_module

    def broken_factory():
        raise RuntimeError("база залокирована другим процессом")

    stage_log: list = []
    pool = await history_module.load_and_log(
        broken_factory, raw_text="что угодно", explicit_request_id=None, stage_log=stage_log
    )

    assert pool.scored == []
    assert len(stage_log) == 1
    assert stage_log[0].stage == "history"
    assert "залокирована" in stage_log[0].error


async def test_load_and_log_печатает_история_пуста_когда_совпадений_нет(db_session_factory, caplog):
    """Пост-аудит Э2-Е, находка В-1: строка печатается ВСЕГДА, одним из двух
    дословных вариантов, по образцу условного второго прохода (РЕШ-81). Раньше
    пустая история молчала — «накопление не сработало» было неотличимо от
    «код до проверки не дошёл»."""
    from app.pipeline import history as history_module

    caplog.set_level(logging.INFO, logger="app.pipeline.orchestrator")
    stage_log: list = []
    pool = await history_module.load_and_log(
        db_session_factory,
        raw_text="задачи с таким текстом в истории ещё не было",
        explicit_request_id=None,
        stage_log=stage_log,
    )

    assert pool.taken == 0
    assert pool.dropped == {}
    assert "история пуста" in caplog.text


# ── Уровень 3: полный прогон ScoutPipeline.run ──────────────────────────────


class _ListAdapter(SearchAdapter):
    """Синхронно отдаёт заранее заданный список — сети нет (инвариант №7)."""

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    def __init__(self, client: httpx.AsyncClient, candidates: list[Candidate]) -> None:
        super().__init__(client)
        self._candidates = candidates

    async def search(self, query, *, limit: int = 20) -> list[Candidate]:
        return list(self._candidates)


class _RoutedProvider(LLMProvider):
    """Провайдер-заглушка: ответ выбирается по ключевой фразе роли в системном
    сообщении, ответы на одну фразу выдаются по очереди, последний — повторно.

    Тот же приём, что в `tests/test_pipeline_second_pass.py`: устойчив к
    порядку конкурентных вызовов (`asyncio.gather` в `_author_cards`), потому
    что подбирает ответ по роли, а не по номеру вызова.
    """

    name = "deepseek"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    def __init__(self, routes: dict[str, list[str]]) -> None:
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
            return LLMResult(
                text=text, usage=Usage(tokens_in=10, tokens_out=10, cost_rub=0.001, provider=self.name, model="fake")
            )
        raise AssertionError(f"тест не подготовил ответ для запроса: {system_text[:120]}")

    async def health(self) -> tuple[bool, str]:
        return True, "заглушка"


def _plan_json(text: str = "формулировка теста") -> str:
    return json.dumps(
        {"queries": [{"text": text, "source_type": "publication", "lang": "ru", "rationale": "тест"}]},
        ensure_ascii=False,
    )


def _scores_json(count: int, relevance: int) -> str:
    return json.dumps(
        {
            "scores": [
                {
                    "index": i,
                    "relevance": relevance,
                    "maturity": 5,
                    "accessibility": 5,
                    "freshness": 5,
                    "completeness": 5,
                }
                for i in range(count)
            ]
        },
        ensure_ascii=False,
    )


_CARD_JSON = json.dumps(
    {
        "title": "Катализатор с присадками",
        "essence": "Присадка продлевает срок службы катализатора.",
        "maturity_stage": "pilot",
        "trl_estimate": None,
        "developer_org": "Институт катализа",
        "developer_team": None,
        "contact_leads": [],
        "evidence": [{"claim": "Испытан на установке", "url": "https://doi.org/10.1/hist-1"}],
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "risks": [],
        "gov_support": [],
        "next_step": "Запросить протокол испытаний.",
        "confidence": "medium",
        "confidence_note": "тест",
        "candidate_urls": ["https://doi.org/10.1/hist-1"],
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

_PLANNER_KEY = "специалист по научно-техническому поиску"
_REPLAN_KEY = "аналитик повторного поиска"
_SCORER_KEY = "аналитик технологического скаутинга"
_CARD_AUTHOR_KEY = "технический писатель службы подбора"
_VERIFIER_KEY = "научный редактор-контролёр"
_SUMMARY_KEY = "руководитель проекта подбора"


def _hist_candidate(number: int) -> Candidate:
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=f"Кандидат {number}",
        url=f"https://doi.org/10.1/hist-{number}",
        org="Институт катализа",
        year=2025,
        snippet="Фрагмент описания кандидата.",
        raw_id=f"10.1/hist-{number}",
        provider="crossref",
    )


async def _rows(session_factory, model, *, request_id: int | None = None):
    async with session_factory() as session:
        query = select(model)
        if request_id is not None:
            query = query.where(model.request_id == request_id)
        return (await session.execute(query)).scalars().all()


async def test_история_не_платит_дважды_и_статус_считается_по_текущему_прогону(settings, brief, db_session_factory):
    """Критерии 1 и 4 разом: пять из десяти кандидатов уже есть в истории —
    скоринг второго прогона получил ровно пятерых новых, один батч; строки
    истории пишутся с `pass_no=0`, судьба посчитана по текущему отбору, а
    строка первого прогона не изменилась.
    """
    task_text = "Катализатор гидроочистки теряет активность раньше срока."

    # Прогон 1: пять кандидатов, оценка 5 — невысокая, чтобы во втором прогоне
    # их обошли новые кандидаты с более высокой оценкой (см. ниже).
    provider1 = _RoutedProvider(
        {
            _PLANNER_KEY: [_plan_json()],
            _SCORER_KEY: [_scores_json(5, relevance=5)],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    async with httpx.AsyncClient() as client:
        adapter1 = _ListAdapter(client, [_hist_candidate(n) for n in range(1, 6)])
        pipeline1 = ScoutPipeline(
            settings=settings,
            router=LLMRouter(settings, providers={"deepseek": provider1}),
            adapters=[adapter1],
            session_factory=db_session_factory,
        )
        report1 = await pipeline1.run(brief, raw_text=task_text)
    assert report1.cards, "первый прогон обязан дойти до карточек"
    request1 = report1.request_id

    # Прогон 2: та же задача, источник отдаёт кандидатов 1-5 (уже известны
    # истории) и 6-10 (новые, оценка выше). Скоринг обязан получить только
    # пятерых новых — иначе `_scores_json(5, …)` не совпадёт по числу записей
    # с батчем, и провайдер бросит несовпадение схемы.
    provider2 = _RoutedProvider(
        {
            _PLANNER_KEY: [_plan_json("другая формулировка")],
            _SCORER_KEY: [_scores_json(5, relevance=9)],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    async with httpx.AsyncClient() as client:
        adapter2 = _ListAdapter(client, [_hist_candidate(n) for n in range(1, 11)])
        pipeline2 = ScoutPipeline(
            settings=settings,
            router=LLMRouter(settings, providers={"deepseek": provider2}),
            adapters=[adapter2],
            session_factory=db_session_factory,
        )
        report2 = await pipeline2.run(brief, raw_text=task_text)
    assert report2.cards
    request2 = report2.request_id

    scorer_calls = [call for call in provider2.calls if _SCORER_KEY in call[0].content]
    assert len(scorer_calls) == 1, "скоринг обязан получить один батч, а не два (без переоценки истории)"
    assert "Кандидаты (5 шт.):" in scorer_calls[0][1].content, "в скоринг должны уйти ровно пятеро новых"

    history_rows = await _rows(db_session_factory, CandidateRow, request_id=request2)
    history_rows = [row for row in history_rows if row.pass_no == 0]
    assert len(history_rows) == 5, "пункт 6: строки истории пишутся своим вызовом с pass_no=0"
    assert {row.dedup_key for row in history_rows} == {f"10.1/hist-{n}" for n in range(1, 6)}

    # Кандидат, бывший `selected` в первом прогоне: во втором прогоне его цена
    # (relevance 5) проигрывает пятерым новым (relevance 9), и при потолке на
    # источник (`cap`) он обязан стать `rejected` — судьба считается заново.
    first_run_all = await _rows(db_session_factory, CandidateRow, request_id=request1)
    first_run_rows = {row.dedup_key: row.status for row in first_run_all}
    assert all(status == "selected" for status in first_run_rows.values()), "в первом прогоне отсева не было"

    second_run_by_key = {row.dedup_key: row.status for row in history_rows}
    rejected_in_second_run = [key for key, status in second_run_by_key.items() if status == "rejected"]
    assert rejected_in_second_run, "хотя бы один кандидат истории обязан не пройти отбор во втором прогоне"

    # Строка первого прогона не изменилась ни для одного из этих ключей.
    unchanged = await _rows(db_session_factory, CandidateRow, request_id=request1)
    assert all(row.status == "selected" for row in unchanged if row.dedup_key in second_run_by_key)


async def test_история_участвует_в_отборе_даже_когда_сегодня_ничего_не_нашли(settings, brief, db_session_factory):
    """Критерий 2: кандидат из истории с высокой оценкой попадает в карточки
    наравне с новыми — здесь наравне в буквальном смысле «даже один», когда
    сегодняшний поиск не принёс ничего своего. Без истории тот же случай даёт
    пустой отчёт (`_empty_report`) — сравнение прямое, тем же провайдером.
    """
    task_text = "Диагностика супоросности свиноматок по УЗИ."
    provider1 = _RoutedProvider(
        {
            _PLANNER_KEY: [_plan_json()],
            _SCORER_KEY: [_scores_json(1, relevance=9)],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    async with httpx.AsyncClient() as client:
        pipeline1 = ScoutPipeline(
            settings=settings,
            router=LLMRouter(settings, providers={"deepseek": provider1}),
            adapters=[_ListAdapter(client, [_hist_candidate(1)])],
            session_factory=db_session_factory,
        )
        report1 = await pipeline1.run(brief, raw_text=task_text)
    assert report1.cards

    # Прогон 2, та же задача, источник сегодня не нашёл ничего своего.
    provider2 = _RoutedProvider(
        {
            _PLANNER_KEY: [_plan_json("формулировка без результата")],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    async with httpx.AsyncClient() as client:
        pipeline2 = ScoutPipeline(
            settings=settings,
            router=LLMRouter(settings, providers={"deepseek": provider2}),
            adapters=[_ListAdapter(client, [])],
            session_factory=db_session_factory,
        )
        report2 = await pipeline2.run(brief, raw_text=task_text)
    assert report2.cards, "история обязана дать карточку даже без сегодняшней находки"
    assert not any(_SCORER_KEY in call[0].content for call in provider2.calls), "скоринг не звался — платить не за что"

    # Без истории (другая задача, тот же пустой источник) — прежнее поведение.
    provider3 = _RoutedProvider(
        {
            _PLANNER_KEY: [_plan_json("формулировка без результата")],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    async with httpx.AsyncClient() as client:
        pipeline3 = ScoutPipeline(
            settings=settings,
            router=LLMRouter(settings, providers={"deepseek": provider3}),
            adapters=[_ListAdapter(client, [])],
            session_factory=db_session_factory,
        )
        report3 = await pipeline3.run(brief, raw_text="задача, которой не было в истории совсем")
    assert report3.cards == [], "без совпавшей истории пустая выдача остаётся пустой"


async def test_чужая_задача_не_подмешивается_в_полном_прогоне(settings, brief, db_session_factory):
    """Критерий 6 на уровне прогона: прогон другой задачи в базе есть, но его
    кандидаты не попадают ни в пул, ни в `candidates` нового прогона."""
    provider1 = _RoutedProvider(
        {
            _PLANNER_KEY: [_plan_json()],
            _SCORER_KEY: [_scores_json(1, relevance=8)],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    async with httpx.AsyncClient() as client:
        pipeline1 = ScoutPipeline(
            settings=settings,
            router=LLMRouter(settings, providers={"deepseek": provider1}),
            adapters=[_ListAdapter(client, [_hist_candidate(1)])],
            session_factory=db_session_factory,
        )
        await pipeline1.run(brief, raw_text="первая задача про катализаторы")

    provider2 = _RoutedProvider(
        {
            _PLANNER_KEY: [_plan_json("формулировка второй задачи")],
            _SCORER_KEY: [_scores_json(1, relevance=8)],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    async with httpx.AsyncClient() as client:
        pipeline2 = ScoutPipeline(
            settings=settings,
            router=LLMRouter(settings, providers={"deepseek": provider2}),
            adapters=[_ListAdapter(client, [_hist_candidate(2)])],
            session_factory=db_session_factory,
        )
        report2 = await pipeline2.run(brief, raw_text="совершенно другая задача про диагностику")

    scorer_calls = [call for call in provider2.calls if _SCORER_KEY in call[0].content]
    assert len(scorer_calls) == 1
    assert "Кандидаты (1 шт.):" in scorer_calls[0][1].content, "чужая история не должна вычитаться из скоринга"

    all_rows = await _rows(db_session_factory, CandidateRow, request_id=report2.request_id)
    rows = [row for row in all_rows if row.pass_no == 0]
    assert rows == [], "чужая задача не должна попасть в пул ни одной строкой"


async def test_второй_проход_не_переоценивает_повторно_найденного_кандидата_истории(
    settings, brief, db_session_factory
):
    """Критерий 3: второй проход поиска повторно находит кандидата из истории —
    в скоринг второго прохода он попасть не должен (мутация «б» задачи)."""
    task_text = "Регенерация катализатора после отравления серой."

    provider1 = _RoutedProvider(
        {
            _PLANNER_KEY: [_plan_json()],
            _SCORER_KEY: [_scores_json(1, relevance=3)],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    async with httpx.AsyncClient() as client:
        pipeline1 = ScoutPipeline(
            settings=settings,
            router=LLMRouter(settings, providers={"deepseek": provider1}),
            adapters=[_ListAdapter(client, [_hist_candidate(1)])],
            session_factory=db_session_factory,
        )
        await pipeline1.run(brief, raw_text=task_text)

    tuned = settings.model_copy(update={"second_pass_relevance_max": 6})

    class _SecondPassAdapter(SearchAdapter):
        name = "crossref"
        source_types = (SourceType.PUBLICATION,)

        async def search(self, query, *, limit: int = 20) -> list[Candidate]:
            if "повторно" in query.text:
                # Второй проход снова находит кандидата из истории (тот же
                # dedup_key) плюс одного по-настоящему нового.
                return [_hist_candidate(1), _hist_candidate(99)]
            return [_hist_candidate(2)]

    provider2 = _RoutedProvider(
        {
            _PLANNER_KEY: [_plan_json("формулировка первого прохода")],
            _REPLAN_KEY: [_plan_json("формулировка повторно, другими словами")],
            _SCORER_KEY: [_scores_json(1, relevance=3), _scores_json(1, relevance=8)],
            _CARD_AUTHOR_KEY: [_CARD_JSON],
            _VERIFIER_KEY: [_VERIFY_JSON],
            _SUMMARY_KEY: [_SUMMARY_JSON],
        }
    )
    async with httpx.AsyncClient() as client:
        pipeline2 = ScoutPipeline(
            settings=tuned,
            router=LLMRouter(tuned, providers={"deepseek": provider2}),
            adapters=[_SecondPassAdapter(client)],
            session_factory=db_session_factory,
        )
        report2 = await pipeline2.run(brief, raw_text=task_text)

    assert report2.cards
    scorer_calls = [call for call in provider2.calls if _SCORER_KEY in call[0].content]
    assert len(scorer_calls) == 2, "первый и второй проход дают по одному батчу скоринга"
    # Второй батч обязан содержать ровно нового кандидата (99), а не двоих:
    # известного истории кандидата 1 второй проход переоценивать не должен.
    assert "Кандидаты (1 шт.):" in scorer_calls[1][1].content

    request2 = report2.request_id
    pass2_rows = [row for row in await _rows(db_session_factory, CandidateRow, request_id=request2) if row.pass_no == 2]
    assert {row.dedup_key for row in pass2_rows} == {"10.1/hist-99"}, "кандидат истории не попал в pass_no=2"
