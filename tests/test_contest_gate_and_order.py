"""Конкурсный класс на стыках прогона: гейт второго прохода, отсечка, порядок стадий (Т-34).

Три замка по итогам пост-аудита Э2-Б. Каждый закрывает место, где конкурсный
класс (РЕШ-92) влиял на решения, принимаемые ради основной выдачи:

1. **Гейт второго прохода считает основную выдачу.** `needs_second_pass` берёт
   максимум `relevance`, и один конкурсный кандидат с высокой оценкой гасил
   второй проход при слабой основной выдаче — при том, что в обещанные клиенту
   5–7 карточек он по построению не попадает. Мутация «гейт снова по всему
   списку» роняет тесты этого раздела.
2. **Конкурсная очередь проходит отсечку по `relevance_min`.** `select_top`
   отсекает нерелевантных внутри себя, `group_for_cards` не отсекает ничем, и
   конкурсный кандидат с оценкой 1 доезжал до карточки и до клиента.
3. **Сводка собирается раньше конкурсной секции** (РЕШ-93): порядок «основные
   карточки → сводка → конкурсная секция».

Сети нет (инвариант №7): источник — поддельный адаптер, стадии модели —
заглушки, база — временный файл SQLite в `tmp_path`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.llm import LLMRouter
from app.models import Base, PipelineLog
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import (
    Brief,
    Candidate,
    Evidence,
    ReportSummary,
    ScoredCandidate,
    SearchPlan,
    SearchQuery,
    SolutionCard,
    SourceType,
)
from app.search.base import SearchAdapter

#: Порог включения второго прохода в тестах раздела 1 — то же число, на котором
#: аудитор показал подавленный проход (`SECOND_PASS_RELEVANCE_MAX = 6`).
SECOND_PASS_THRESHOLD = 6

#: Порог отсечки по предметному соответствию в тестах раздела 2.
RELEVANCE_MIN = 5

#: Оценки основной выдачи: обе строго ниже порога второго прохода — это и есть
#: «выдача мимо задачи», ради которой затеян РЕШ-81.
WEAK_MAIN_RELEVANCE = {1: 2, 2: 3}

#: Оценка конкурсного кандидата: заведомо выше порога. Именно она гасила проход,
#: пока решение принималось по всему списку.
STRONG_CONTEST_RELEVANCE = 9

#: Оценки конкурсных кандидатов по номеру. Тесты раздела 2 задают их через
#: `monkeypatch.setitem`; по умолчанию конкурсный кандидат силён.
CONTEST_RELEVANCE: dict[int, int] = {}


def _main_candidate(number: int) -> Candidate:
    """Публикация основной выдачи. Организация своя у каждой — иначе
    `group_for_cards` склеит их в одну карточку."""
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=f"Публикация №{number} о регенерации катализатора",
        url=f"https://doi.org/10.1234/main-{number}",
        org=f"Институт катализа №{number}",
        year=2025,
        raw_id=f"10.1234/main-{number}",
        provider="crossref",
    )


def _contest_candidate(number: int) -> Candidate:
    """Кандидат конкурсного класса: тип проставляет адаптер по типу формулировки
    плана (Т-23), здесь он задан прямо."""
    return Candidate(
        source_type=SourceType.CONTEST,
        title=f"Хакатонный прототип №{number}",
        url=f"https://hacks-ai.ru/event/{number}",
        org=f"Команда №{number}",
        year=2026,
        provider="yandex_search",
    )


_PLAN = SearchPlan(
    queries=[
        SearchQuery(text="регенерация катализатора", source_type=SourceType.PUBLICATION, lang="ru", rationale="тест"),
        SearchQuery(text="хакатон катализатор", source_type=SourceType.CONTEST, lang="ru", rationale="тест"),
    ]
)


class _MixedAdapter(SearchAdapter):
    """Один источник на оба типа формулировок — как платный контур Яндекса."""

    name = "yandex_search"
    source_types = (SourceType.PUBLICATION, SourceType.CONTEST)

    def __init__(self, client: httpx.AsyncClient, *, main: list[int], contest: list[int]) -> None:
        super().__init__(client)
        self._main = main
        self._contest = contest

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        if query.source_type is SourceType.CONTEST:
            return [_contest_candidate(n) for n in self._contest]
        return [_main_candidate(n) for n in self._main]


def _number_of(candidate: Candidate) -> int:
    """Номер кандидата из заголовка — по нему заглушка скоринга берёт оценку."""
    match = re.search(r"№(\d+)", candidate.title)
    assert match is not None, f"заголовок без номера: {candidate.title}"
    return int(match.group(1))


def _card(candidate: Candidate) -> SolutionCard:
    """Годная карточка: подтверждение есть, отсев пустых её не тронет."""
    return SolutionCard(
        title=candidate.title,
        essence="Суть решения одной строкой.",
        applicability="Закрывает задачу продления цикла.",
        next_step="Связаться с разработчиком.",
        evidence=[Evidence(claim="Подтверждение из источника", url=candidate.url)],
        candidate_urls=[candidate.url],
    )


@pytest.fixture
def stub_stages(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Стадии модели — заглушки, попутно пишущие порядок вызовов.

    Возвращает журнал событий: `("card", заголовок)` на каждую собранную
    карточку и `("summary", "")` на сводку. По нему проверяется порядок РЕШ-93.
    """
    events: list[tuple[str, str]] = []

    async def fake_plan(router, brief: Brief) -> SearchPlan:
        return _PLAN

    async def fake_score(router, brief: Brief, candidates: list[Candidate], **kwargs) -> list[ScoredCandidate]:
        scored: list[ScoredCandidate] = []
        for candidate in candidates:
            number = _number_of(candidate)
            if candidate.source_type is SourceType.CONTEST:
                relevance = CONTEST_RELEVANCE.get(number, STRONG_CONTEST_RELEVANCE)
            else:
                relevance = WEAK_MAIN_RELEVANCE.get(number, 2)
            scored.append(
                ScoredCandidate(
                    candidate=candidate,
                    relevance=relevance,
                    # Остальные оси не по 5: пятёрка по всем осям — признак
                    # нейтральной оценки отказавшего батча, а такую отсечка не
                    # трогает (`has_neutral_score`), и тест раздела 2 мерил бы
                    # не то.
                    maturity=4,
                    accessibility=6,
                    freshness=6,
                    completeness=4,
                )
            )
        return scored

    async def fake_author(router, brief: Brief, group: list[ScoredCandidate]) -> SolutionCard:
        candidate = group[0].candidate
        events.append(("card", candidate.title))
        return _card(candidate)

    async def fake_verify(router, card: SolutionCard, group=None) -> SolutionCard:
        return card

    async def fake_summary(router, brief: Brief, cards: list[SolutionCard]) -> ReportSummary:
        events.append(("summary", ""))
        return ReportSummary(recommendation="Начать с варианта №1.")

    monkeypatch.setattr(stages, "plan_search", fake_plan)
    monkeypatch.setattr(stages, "score_candidates", fake_score)
    monkeypatch.setattr(stages, "author_card", fake_author)
    monkeypatch.setattr(stages, "verify_card", fake_verify)
    monkeypatch.setattr(stages, "summarize", fake_summary)
    return events


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Временный файл SQLite той же формы, что `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gate-test.db'}", future=True)
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


async def _run(
    settings,
    fake_provider_factory,
    brief: Brief,
    *,
    main: list[int],
    contest: list[int],
    session_factory=None,
    progress=None,
):
    """Полный проход на подменённых стадиях и одном поддельном источнике."""
    router = LLMRouter(settings, providers={"deepseek": fake_provider_factory("deepseek")})
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(
            settings=settings,
            router=router,
            adapters=[_MixedAdapter(client, main=main, contest=contest)],
            session_factory=session_factory,
            progress=progress,
        )
        return await pipeline.run(brief, request_id=1, raw_text="текст задачи про катализатор")


@pytest.fixture
def replan_spy(monkeypatch: pytest.MonkeyPatch) -> list[SearchPlan]:
    """Соглядатай на стадию `replan`: сюда попадает по записи на каждый запуск
    второго прохода. Второй план тот же — тест меряет факт запуска, а не выдачу."""
    calls: list[SearchPlan] = []

    async def fake_replan(router, brief: Brief, plan: SearchPlan, scored: list[ScoredCandidate]) -> SearchPlan:
        calls.append(plan)
        return _PLAN

    monkeypatch.setattr(stages, "replan_search", fake_replan)
    return calls


# ── Правка 1: гейт второго прохода считает основную выдачу ──────────────────
async def test_сильный_конкурсный_кандидат_не_гасит_второй_проход(
    settings, brief, fake_provider_factory, stub_stages, replan_spy
):
    """Слабая основная выдача плюс один сильный конкурсный → проход ЗАПУСКАЕТСЯ.

    Зонд аудитора дословно: relevance 2 и 3 в основной выдаче при пороге 6 —
    проход нужен; добавление конкурсного кандидата с relevance 9 гасило его.
    Мутация «гейт снова по всему списку» роняет этот тест: `replan` не позовут.
    """
    tuned = settings.model_copy(update={"second_pass_relevance_max": SECOND_PASS_THRESHOLD})

    await _run(tuned, fake_provider_factory, brief, main=[1, 2], contest=[1])

    assert replan_spy, "второй проход подавлен конкурсным кандидатом, который до клиента не доходит"


async def test_гейт_второго_прохода_не_видит_конкурсных_кандидатов(
    settings, brief, fake_provider_factory, stub_stages, replan_spy, monkeypatch
):
    """Решение принимается по списку БЕЗ конкурсного класса — поимённо.

    Проверка входа, а не только исхода: с другими оценками неверный список дал
    бы верный ответ случайно, и мутация прошла бы молча.
    """
    seen: list[list[ScoredCandidate]] = []
    original = stages.needs_second_pass

    def spy(scored: list[ScoredCandidate], *, threshold: int) -> bool:
        seen.append(list(scored))
        return original(scored, threshold=threshold)

    monkeypatch.setattr(stages, "needs_second_pass", spy)
    tuned = settings.model_copy(update={"second_pass_relevance_max": SECOND_PASS_THRESHOLD})

    await _run(tuned, fake_provider_factory, brief, main=[1, 2], contest=[1, 2])

    assert seen, "гейт второго прохода не вызывался вовсе"
    gate_input = seen[0]
    assert [item.candidate.title for item in gate_input] == [
        _main_candidate(1).title,
        _main_candidate(2).title,
    ], "в гейт второго прохода уехал не список основной выдачи"


async def test_без_конкурсных_кандидатов_решение_гейта_прежнее(
    settings, brief, fake_provider_factory, stub_stages, replan_spy
):
    """Годная основная выдача прохода не запускает — правка не превратила гейт
    в «всегда да»."""
    tuned = settings.model_copy(update={"second_pass_relevance_max": 2})

    await _run(tuned, fake_provider_factory, brief, main=[1, 2], contest=[1])

    assert not replan_spy, "проход запустился на выдаче, дотянувшей до порога"


# ── Правка 2: конкурсная очередь проходит отсечку по relevance_min ──────────
async def test_конкурсный_кандидат_ниже_порога_до_карточки_не_доходит(
    settings, brief, fake_provider_factory, stub_stages, monkeypatch
):
    """Отсечка по `relevance_min` работает и в конкурсной очереди.

    Конкурсный кандидат №1 с оценкой 1 при пороге 5 до карточки не доходит, №2
    с оценкой 8 доходит. Мутация «отсечка снята с конкурсной очереди» роняет
    тест: в отчёте появится вторая карточка.
    """
    monkeypatch.setitem(CONTEST_RELEVANCE, 1, 1)
    monkeypatch.setitem(CONTEST_RELEVANCE, 2, 8)
    tuned = settings.model_copy(update={"relevance_min": RELEVANCE_MIN})

    report = await _run(tuned, fake_provider_factory, brief, main=[1, 2], contest=[1, 2])

    assert [card.title for card in report.contest_cards] == [_contest_candidate(2).title]


async def test_конкурсный_кандидат_на_пороге_до_карточки_доходит(
    settings, brief, fake_provider_factory, stub_stages, monkeypatch
):
    """Граница отсечки та же, что у `select_top`: кандидат ровно с порогом
    проходит, ниже порога — нет."""
    monkeypatch.setitem(CONTEST_RELEVANCE, 1, RELEVANCE_MIN)
    monkeypatch.setitem(CONTEST_RELEVANCE, 2, RELEVANCE_MIN - 1)
    tuned = settings.model_copy(update={"relevance_min": RELEVANCE_MIN})

    report = await _run(tuned, fake_provider_factory, brief, main=[1, 2], contest=[1, 2])

    assert [card.title for card in report.contest_cards] == [_contest_candidate(1).title]


async def test_при_выключенной_отсечке_конкурсная_очередь_прежняя(
    settings, brief, fake_provider_factory, stub_stages, monkeypatch
):
    """При явно выключенной отсечке (`RELEVANCE_MIN = 0`) слабый конкурсный
    кандидат доезжает до секции, как до правки Ц-2.

    Умолчание порога перестало быть нулём (РЕШ-174: рабочее значение 3
    переехало в код по итогам калибровки), поэтому «выключено» здесь —
    выбор настройки теста, а не Settings() без переменных окружения."""
    monkeypatch.setitem(CONTEST_RELEVANCE, 1, 1)
    tuned = settings.model_copy(update={"relevance_min": 0})

    report = await _run(tuned, fake_provider_factory, brief, main=[1, 2], contest=[1])

    assert [card.title for card in report.contest_cards] == [_contest_candidate(1).title]


# ── Правка 3: сводка собирается раньше конкурсной секции (РЕШ-93) ───────────
async def test_сводка_собирается_до_конкурсной_секции(settings, brief, fake_provider_factory, stub_stages):
    """Порядок вызовов: основные карточки → сводка → конкурсные карточки.

    Перестановка обратно роняет тест: сводка окажется последней.
    """
    await _run(settings, fake_provider_factory, brief, main=[1, 2], contest=[1, 2])

    kinds = [
        ("summary" if kind == "summary" else ("contest" if "Хакатонный" in title else "main"))
        for kind, title in stub_stages
    ]
    assert kinds.count("summary") == 1, kinds
    boundary = kinds.index("summary")
    assert set(kinds[:boundary]) == {"main"}, f"до сводки собрано не только основное: {kinds}"
    assert set(kinds[boundary + 1 :]) == {"contest"}, f"после сводки собрано не только конкурсное: {kinds}"


async def test_журнал_стадий_ставит_конкурсные_карточки_после_сводки(
    settings, brief, fake_provider_factory, stub_stages, db_session_factory
):
    """Тот же порядок виден в журнале стадий: трата конкурсной секции записана
    отдельной строкой ПОСЛЕ сводки, а не растворена в строке основных карточек.

    Второй проход здесь выключен явно: умолчание порога с РЕШ-103 равно
    рабочему значению 6, а основная выдача (relevance 1 и 2) его не пройдёт —
    проверяется порядок стадий журнала, а не гейт второго прохода."""
    tuned = settings.model_copy(update={"second_pass_relevance_max": 0})
    await _run(tuned, fake_provider_factory, brief, main=[1, 2], contest=[1, 2], session_factory=db_session_factory)

    async with db_session_factory() as session:
        rows = (await session.execute(select(PipelineLog).order_by(PipelineLog.id))).scalars().all()

    order = [row.stage for row in rows]
    assert order == ["planner", "search", "scorer", "cards", "summary", "contest_cards"], order


async def test_прогон_без_конкурсных_кандидатов_не_добавляет_строку_журнала(
    settings, brief, fake_provider_factory, stub_stages, db_session_factory
):
    """Без конкурсных кандидатов лишней строки в журнале нет: стадии не было —
    записывать нечего, и прежний порядок стадий не меняется.

    Второй проход здесь выключен явно (см. соседний тест выше — та же причина:
    умолчание порога с РЕШ-103 равно 6, а не 0)."""
    tuned = settings.model_copy(update={"second_pass_relevance_max": 0})
    await _run(tuned, fake_provider_factory, brief, main=[1, 2], contest=[], session_factory=db_session_factory)

    async with db_session_factory() as session:
        rows = (await session.execute(select(PipelineLog).order_by(PipelineLog.id))).scalars().all()

    order = [row.stage for row in rows]
    assert order == ["planner", "search", "scorer", "cards", "summary"], order
