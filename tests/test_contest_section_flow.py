"""Проводка конкурсных карточек от оркестратора до отчёта и до базы (Т-33, РЕШ-92).

Что здесь закрыто, по критериям РЕШ-92:

1. Конкурсные карточки идут **вне лимита 5–7** (FR-9): шесть обычных карточек
   остаются шестью, конкурсные печатаются сверх того.
2. Конкурсные кандидаты **не вытесняют** обычных из основного отбора — оценки
   им здесь нарочно выставлены выше, чем обычным: попади они в общий
   `select_top`, они заняли бы половину выдачи.
3. Карточки обеих секций сохраняются в базу и **различимы** там колонкой
   `solution_cards.is_contest`.
4. Мутация «конкурсные пошли в общий отбор» роняет тест — вход `select_top`
   проверяется соглядатаем поимённо.

Сети нет: источник подменён поддельным адаптером, стадии модели — заглушками,
база — временным файлом SQLite (инвариант №7).
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export.markdown import CONTEST_SECTION_HEADING, render_markdown
from app.llm import LLMRouter
from app.models import Base
from app.models import Candidate as CandidateRow
from app.models import SolutionCard as SolutionCardRow
from app.pipeline import stages
from app.pipeline.orchestrator import CONTEST_CARDS_TARGET, ScoutPipeline, split_contest_candidates
from app.schemas import (
    Brief,
    Candidate,
    Evidence,
    Report,
    ReportSummary,
    ScoredCandidate,
    SearchPlan,
    SearchQuery,
    SolutionCard,
    SourceType,
)
from app.search.base import SearchAdapter

#: Сколько обычных кандидатов заводит прогон теста. Ровно `target_cards` по
#: умолчанию (6): основная секция обязана остаться шестью карточками, и любое
#: вытеснение конкурсным кандидатом видно сразу, без арифметики.
MAIN_COUNT = 6

#: Конкурсных кандидатов на один больше, чем помещается в секцию: так проверяется
#: и верхняя граница секции, и судьба не попавшего в неё кандидата.
CONTEST_COUNT = CONTEST_CARDS_TARGET + 1


def _main_candidate(number: int) -> Candidate:
    """Обычный кандидат-публикация. Организация своя у каждого — иначе
    `group_for_cards` склеит их в одну карточку по точной строке `org`."""
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
    """Кандидат конкурсного класса — тип проставляет адаптер по типу
    формулировки плана (Т-23), здесь он задан прямо."""
    return Candidate(
        source_type=SourceType.CONTEST,
        title=f"Хакатонный прототип №{number}",
        url=f"https://hacks-ai.ru/event/{number}",
        org=f"Команда №{number}",
        year=2026,
        provider="yandex_search",
    )


class _MixedAdapter(SearchAdapter):
    """Один источник на оба типа формулировок — как платный контур Яндекса
    после Т-23: конкурсные формулировки и обычный веб идут через него же."""

    name = "yandex_search"
    source_types = (SourceType.PUBLICATION, SourceType.CONTEST)

    def __init__(self, client: httpx.AsyncClient, *, with_contest: bool = True) -> None:
        super().__init__(client)
        self._with_contest = with_contest

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        if query.source_type is SourceType.CONTEST:
            return [_contest_candidate(n) for n in range(1, CONTEST_COUNT + 1)] if self._with_contest else []
        return [_main_candidate(n) for n in range(1, MAIN_COUNT + 1)]


_PLAN = SearchPlan(
    queries=[
        SearchQuery(text="регенерация катализатора", source_type=SourceType.PUBLICATION, lang="ru", rationale="тест"),
        SearchQuery(text="хакатон катализатор", source_type=SourceType.CONTEST, lang="ru", rationale="тест"),
    ]
)


def _score(candidate: Candidate) -> ScoredCandidate:
    """Оценки: конкурсному кандидату — ВЫШЕ, чем обычному.

    Так и должно быть в тесте, хотя в жизни бывает наоборот: если конкурсные
    попадут в общий отбор, они по взвешенной сумме вытеснят обычных, и критерий
    2 упадёт. С равными оценками мутация прошла бы незамеченной.
    """
    if candidate.source_type is SourceType.CONTEST:
        return ScoredCandidate(
            candidate=candidate, relevance=10, maturity=10, accessibility=10, freshness=10, completeness=10
        )
    return ScoredCandidate(candidate=candidate, relevance=6, maturity=5, accessibility=5, freshness=5, completeness=5)


@pytest.fixture
def stub_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Стадии модели — заглушки: тест про разделение в оркестраторе, а не про
    качество карточек. Ни один вызов наружу не уходит (инвариант №7)."""

    async def fake_plan(router, brief: Brief) -> SearchPlan:
        return _PLAN

    async def fake_score(router, brief: Brief, candidates: list[Candidate], **kwargs) -> list[ScoredCandidate]:
        return [_score(candidate) for candidate in candidates]

    async def fake_author(router, brief: Brief, group: list[ScoredCandidate]) -> SolutionCard:
        candidate = group[0].candidate
        return SolutionCard(
            title=candidate.title,
            essence="Суть решения одной строкой.",
            applicability="Закрывает задачу продления цикла.",
            next_step="Связаться с разработчиком.",
            evidence=[Evidence(claim="Подтверждение из источника", url=candidate.url)],
            candidate_urls=[candidate.url],
        )

    async def fake_verify(router, card: SolutionCard, group=None) -> SolutionCard:
        return card

    async def fake_summary(router, brief: Brief, cards: list[SolutionCard]) -> ReportSummary:
        return ReportSummary(recommendation="Начать с варианта №1.")

    monkeypatch.setattr(stages, "plan_search", fake_plan)
    monkeypatch.setattr(stages, "score_candidates", fake_score)
    monkeypatch.setattr(stages, "author_card", fake_author)
    monkeypatch.setattr(stages, "verify_card", fake_verify)
    monkeypatch.setattr(stages, "summarize", fake_summary)


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Временный файл SQLite той же формы, что `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'contest-test.db'}", future=True)
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
    with_contest: bool = True,
    session_factory=None,
    progress=None,
):
    """Полный проход на подменённых стадиях и одном поддельном источнике."""
    router = LLMRouter(settings, providers={"deepseek": fake_provider_factory("deepseek")})
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(
            settings=settings,
            router=router,
            adapters=[_MixedAdapter(client, with_contest=with_contest)],
            session_factory=session_factory,
            progress=progress,
        )
        return await pipeline.run(brief, request_id=1, raw_text="текст задачи про катализатор")


def _section_titles(text: str, heading: str) -> list[str]:
    """Заголовки карточек `### …` внутри секции, ограниченной следующим `## `."""
    lines = text.split("\n")
    titles: list[str] = []
    for line in lines[lines.index(heading) + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("### "):
            titles.append(line)
    return titles


# ── Критерий 1: секция вне лимита 5–7 ───────────────────────────────────────
async def test_конкурсные_карточки_идут_сверх_лимита_основной_выдачи(
    settings, brief, fake_provider_factory, stub_stages
):
    """Шесть обычных карточек остаются шестью, конкурсные печатаются сверх них.

    Лимит 5–7 (FR-9) не трогается ни в какую сторону: он про основную выдачу,
    а конкурсная секция к ней приложение (РЕШ-85 п. 1).
    """
    report = await _run(settings, fake_provider_factory, brief)

    assert len(report.cards) == MAIN_COUNT
    assert len(report.contest_cards) == CONTEST_CARDS_TARGET

    text = render_markdown(report)
    assert len(_section_titles(text, "## Варианты решений")) == MAIN_COUNT
    assert len(_section_titles(text, CONTEST_SECTION_HEADING)) == CONTEST_CARDS_TARGET


async def test_основная_секция_не_содержит_конкурсных_карточек(settings, brief, fake_provider_factory, stub_stages):
    """Мутация «конкурсные пошли в общий отбор» роняет этот тест: их оценки
    выше, и в основной секции появился бы хакатонный прототип."""
    report = await _run(settings, fake_provider_factory, brief)

    main_titles = [card.title for card in report.cards]
    assert all("Хакатонный прототип" not in title for title in main_titles)
    assert all("Хакатонный прототип" in card.title for card in report.contest_cards)


# ── Критерий 2: конкурсные не вытесняют обычных ─────────────────────────────
async def test_конкурсные_кандидаты_не_вытесняют_обычных_из_основного_отбора(
    settings, brief, fake_provider_factory, stub_stages
):
    """Состав основной выдачи одинаков с конкурсными кандидатами и без них.

    Это и есть цена вопроса из РЕШ-85: хакатонный прототип, попав в общий
    зачёт, вытесняет зрелое решение из обещанных клиенту 5–7 вариантов.
    """
    with_contest = await _run(settings, fake_provider_factory, brief, with_contest=True)
    without_contest = await _run(settings, fake_provider_factory, brief, with_contest=False)

    assert [card.title for card in with_contest.cards] == [card.title for card in without_contest.cards]
    assert without_contest.contest_cards == []


async def test_конкурсные_кандидаты_не_доходят_до_общего_отбора(
    settings, brief, fake_provider_factory, stub_stages, monkeypatch
):
    """Соглядатай на входе `select_top`: конкурсного кандидата там нет ни одного.

    Проверка поимённая, а не по составу выдачи: вернуть конкурсного из общего
    отбора мог бы и потолок на источник, и тогда «не вытеснил» держалось бы на
    случайности, а не на разделении.
    """
    seen: list[list[ScoredCandidate]] = []
    original = stages.select_top

    def spy(scored: list[ScoredCandidate], **kwargs) -> list[ScoredCandidate]:
        seen.append(list(scored))
        return original(scored, **kwargs)

    monkeypatch.setattr(stages, "select_top", spy)

    await _run(settings, fake_provider_factory, brief)

    assert seen, "select_top не вызывался — тест сторожил бы пустоту"
    for call in seen:
        assert all(item.candidate.source_type is not SourceType.CONTEST for item in call)


def test_разделение_идёт_по_типу_источника_а_не_по_адресу():
    """Чистая функция разделения: тип кандидата — единственный признак."""
    main = [_score(_main_candidate(1))]
    contest = [_score(_contest_candidate(1))]

    got_main, got_contest = split_contest_candidates([*main, *contest])

    assert got_main == main
    assert got_contest == contest


# ── Критерий 3: обе секции в базе и различимы ───────────────────────────────
async def test_карточки_обеих_секций_ложатся_в_базу_и_различимы(
    settings, brief, fake_provider_factory, stub_stages, db_session_factory
):
    """Критерий 3: в `solution_cards` лежат карточки обеих секций, и колонка
    `is_contest` разводит их без догадок по позиции или по типу кандидата."""
    report = await _run(settings, fake_provider_factory, brief, session_factory=db_session_factory)

    async with db_session_factory() as session:
        rows = (await session.execute(select(SolutionCardRow).order_by(SolutionCardRow.id))).scalars().all()

    main_rows = [row for row in rows if not row.is_contest]
    contest_rows = [row for row in rows if row.is_contest]

    assert [row.title for row in main_rows] == [card.title for card in report.cards]
    assert [row.title for row in contest_rows] == [card.title for card in report.contest_cards]
    assert contest_rows, "конкурсная секция в базу не доехала"
    # Позиции нумеруются заново у каждой секции — как и в отчёте, где конкурсные
    # карточки идут с единицы.
    assert [row.position for row in contest_rows] == list(range(len(contest_rows)))


async def test_прогон_без_конкурсных_кандидатов_не_пишет_конкурсных_строк(
    settings, brief, fake_provider_factory, stub_stages, db_session_factory
):
    """Обратная сторона: без конкурсных кандидатов в базе нет ни одной строки с
    `is_contest`, а обычные карточки на месте — признак не проставляется всем
    подряд."""
    await _run(settings, fake_provider_factory, brief, with_contest=False, session_factory=db_session_factory)

    async with db_session_factory() as session:
        rows = (await session.execute(select(SolutionCardRow))).scalars().all()

    assert len(rows) == MAIN_COUNT
    assert all(row.is_contest is False for row in rows)


# ── Предупреждение ревью Т-20: судьба кандидата в таблице `candidates` ──────
async def test_конкурсный_кандидат_не_попавший_в_секцию_помечен_rejected(
    settings, brief, fake_provider_factory, stub_stages, db_session_factory
):
    """Кандидатов конкурсного класса больше, чем помещается в секцию: лишний
    обязан остаться `rejected`, а отобранный — получить `selected` ровно один
    раз (предупреждение ревью Т-20 про статус по ключу склейки)."""
    await _run(settings, fake_provider_factory, brief, session_factory=db_session_factory)

    async with db_session_factory() as session:
        rows = (await session.execute(select(CandidateRow))).scalars().all()

    contest_rows = [row for row in rows if row.source_type == SourceType.CONTEST.value]
    assert len(contest_rows) == CONTEST_COUNT
    assert sum(1 for row in contest_rows if row.status == "selected") == CONTEST_CARDS_TARGET
    assert sum(1 for row in contest_rows if row.status == "rejected") == CONTEST_COUNT - CONTEST_CARDS_TARGET
    # Ни один ключ склейки не помечен отобранным дважды.
    selected_keys = [row.dedup_key for row in rows if row.status == "selected"]
    assert len(selected_keys) == len(set(selected_keys))


# ── Инвариант №1 в конкурсной секции: пустая карточка до клиента не доходит ──
async def test_конкурсная_карточка_без_подтверждений_не_доходит_ни_до_отчёта_ни_до_базы(
    settings, brief, fake_provider_factory, stub_stages, db_session_factory, monkeypatch
):
    """Отсев пустых карточек работает и в новой секции (инвариант №1).

    Три заготовки конкурсного класса дают три разных исхода: карточку без
    единого подтверждения, заглушку «не найдено» (формат ошибки 1 из
    `prompts/card_author.md`) и честную карточку. До отчёта, до клиента и до
    базы доходит только третья.

    Сценарий не выдуман под тест: `author_card` возвращает оба вида брака и
    сегодня, а у конкурсного класса источники слабее публикаций — вероятность
    выше, чем в основной выдаче. Цена регрессии названа в колонке «как
    проверять» инварианта №1: в отчёте не должно быть строки «Подтверждений не
    приложено», а экспортёр печатает её для карточки с пустым `evidence`.
    """

    async def author_with_broken_contest(router, brief_: Brief, group: list[ScoredCandidate]) -> SolutionCard:
        candidate = group[0].candidate
        if candidate.source_type is not SourceType.CONTEST:
            return SolutionCard(
                title=candidate.title,
                essence="Суть решения одной строкой.",
                applicability="Закрывает задачу продления цикла.",
                next_step="Связаться с разработчиком.",
                evidence=[Evidence(claim="Подтверждение из источника", url=candidate.url)],
                candidate_urls=[candidate.url],
            )
        if candidate.title.endswith("№1"):
            # Ни одного подтверждения: экспортёр напечатал бы «Подтверждений не
            # приложено», то есть ровно то, что запрещает инвариант №1.
            return SolutionCard(
                title=candidate.title,
                essence="Команда собрала прототип за 48 часов.",
                applicability="Закрывает ту же задачу дешевле.",
                next_step="Связаться с капитаном команды.",
                evidence=[],
                candidate_urls=[candidate.url],
            )
        if candidate.title.endswith("№2"):
            # Заглушка «не найдено» — группа без источников.
            return SolutionCard(
                title="не найдено",
                essence="не найдено",
                applicability="не найдено",
                next_step="не найдено",
                evidence=[Evidence(claim="Формальная ссылка заглушки", url=candidate.url)],
                candidate_urls=[candidate.url],
            )
        return SolutionCard(
            title=candidate.title,
            essence="Команда собрала работающий прототип.",
            applicability="Закрывает ту же задачу дешевле.",
            next_step="Связаться с капитаном команды.",
            evidence=[Evidence(claim="Победитель хакатона", url=candidate.url)],
            candidate_urls=[candidate.url],
        )

    monkeypatch.setattr(stages, "author_card", author_with_broken_contest)

    events: list[tuple[str, str, float]] = []

    async def progress(stage: str, message: str, fraction: float) -> None:
        events.append((stage, message, fraction))

    report = await _run(settings, fake_provider_factory, brief, session_factory=db_session_factory, progress=progress)

    # В отчёт дошла одна конкурсная карточка из трёх заготовок.
    assert [card.title for card in report.contest_cards] == ["Хакатонный прототип №3"]
    assert len(report.cards) == MAIN_COUNT, "основная секция от брака в конкурсной не пострадала"

    # Инвариант №1 дословно по колонке «как проверять»: строки в отчёте нет.
    text = render_markdown(report)
    assert "Подтверждений не приложено" not in text
    assert "не найдено" not in _section_titles(text, CONTEST_SECTION_HEADING)[0]
    assert len(_section_titles(text, CONTEST_SECTION_HEADING)) == 1

    # В базу брак тоже не попал: карточка в БД не «увереннее» отчёта.
    async with db_session_factory() as session:
        rows = (await session.execute(select(SolutionCardRow))).scalars().all()
    contest_rows = [row for row in rows if row.is_contest]
    assert [row.title for row in contest_rows] == ["Хакатонный прототип №3"]
    assert all(row.evidence for row in rows), "карточка без подтверждений не имеет права лежать в базе"

    # Отброшенное видно оператору числом, а не только отсутствием карточек.
    card_messages = [message for stage, message, _ in events if stage == "cards"]
    assert any("Карточек конкурсного класса: 1, отброшено пустых: 2" in message for message in card_messages)


# ── Строка прогресса конкурсного класса печатается всегда (совет 3 ревью) ────
async def test_строка_прогресса_конкурсного_класса_печатается_с_фактическими_числами(
    settings, brief, fake_provider_factory, stub_stages
):
    """Замок на строку, которой оркестратор отчитывается о конкурсном классе.

    Образец — та же проверка основной секции
    (`test_progress_reports_published_and_dropped_card_counts`): числа в строке
    сверяются дословно, иначе испорченный текст или подменённое число проходят
    молча — ровно это ревьюер и показал мутацией.
    """
    events: list[tuple[str, str, float]] = []

    async def progress(stage: str, message: str, fraction: float) -> None:
        events.append((stage, message, fraction))

    await _run(settings, fake_provider_factory, brief, progress=progress)

    messages = [message for _, message, _ in events]
    assert any(
        f"Конкурсный класс: кандидатов {CONTEST_COUNT}, заготовок карточек {CONTEST_CARDS_TARGET} "
        "(секция вне лимита 5–7)" == message
        for message in messages
    ), messages
    assert any(
        f"Карточек конкурсного класса: {CONTEST_CARDS_TARGET}, отброшено пустых: 0" == message for message in messages
    )


async def test_строка_прогресса_конкурсного_класса_печатается_и_при_нулях(
    settings, brief, fake_provider_factory, stub_stages
):
    """Нули печатаются наравне с числами.

    На этом держится отличие «конкурсных кандидатов не было» от «конкурсные
    были и до карточек не дошли»: молчание сделало бы события неотличимыми, а
    неинтерпретируемый ноль этот проект уже разбирал (урок Э1-2).
    """
    events: list[tuple[str, str, float]] = []

    async def progress(stage: str, message: str, fraction: float) -> None:
        events.append((stage, message, fraction))

    report = await _run(settings, fake_provider_factory, brief, with_contest=False, progress=progress)

    assert report.contest_cards == []
    messages = [message for _, message, _ in events]
    assert "Конкурсный класс: кандидатов 0, заготовок карточек 0 (секция вне лимита 5–7)" in messages
    assert "Карточек конкурсного класса: 0, отброшено пустых: 0" in messages


# ── Экспорт берёт карточки только из отчёта ─────────────────────────────────
def test_у_render_markdown_нет_параметра_contest_cards():
    """Т-33, п. 2: параметр удалён.

    Пока он был, `render_markdown(report)` молча печатал отчёт без секции — два
    способа передать одно и то же, и умолчание побеждало. Тест сторожит именно
    отсутствие второго способа, а не наличие первого.
    """
    assert "contest_cards" not in inspect.signature(render_markdown).parameters


def test_секция_печатается_из_поля_отчёта(brief):
    """Единственный источник карточек секции — сам отчёт."""
    contest = SolutionCard(
        title="Хакатонный прототип №1",
        essence="Команда собрала прототип за 48 часов.",
        applicability="Закрывает ту же задачу дешевле.",
        next_step="Связаться с капитаном команды.",
    )
    report = Report(
        request_id=1,
        brief=brief,
        cards=[],
        contest_cards=[contest],
        summary=ReportSummary(recommendation="Смотреть раннюю разработку."),
        generated_at=datetime(2026, 8, 18, 10, 0),
    )

    assert CONTEST_SECTION_HEADING in render_markdown(report)


def test_отчёт_без_конкурсных_карточек_остаётся_прежним(brief):
    """Умолчание поля пустое: старые отчёты секции не получают."""
    report = Report(
        request_id=1,
        brief=brief,
        cards=[],
        summary=ReportSummary(recommendation="Ничего не найдено."),
        generated_at=datetime(2026, 8, 18, 10, 0),
    )

    assert report.contest_cards == []
    assert CONTEST_SECTION_HEADING not in render_markdown(report)
