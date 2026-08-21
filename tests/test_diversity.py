"""Разнообразие выдачи: составной признак одной разработки и склейка заготовок
карточек до их платной сборки (З-6, РЕШ-132 п. 2, РЕШ-139 Р-4).

Фикстуры критериев 1 и 2 построены дословно по реальному воротному прогону
фармы (`.planning/evidence/E2V-gate-фарма.md`): критерий 1 — по признанному
самим отчётом дублю (карточки 3 и 4, `:132` и `:166`), критерий 2 — по паре
карточек на одну тему, которые дублями не являются (карточки 1 и 5, `:33` и
`:170`). Порог сходства заголовков (`TITLE_SIMILARITY_THRESHOLD` в
`app/pipeline/diversity.py`) подобран так, чтобы разделять именно эту пару:
расчёт по обеим отраслям — нефтегазу и фарме — приведён в отчёте задачи, а не
здесь.

Сети нет: провайдер модели — заглушка из conftest, источник — поддельный
адаптер (инвариант №7)."""

from __future__ import annotations

import httpx
import pytest

from app.llm import LLMRouter
from app.pipeline import stages
from app.pipeline.diversity import (
    TITLE_SIMILARITY_THRESHOLD,
    is_same_development,
    merge_duplicate_groups,
)
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

# ── Данные критерия 1: реальный дубль Э2-В (карточки 3 и 4 отчёта) ──────────
_DUP_TITLE = "ИИ-система диагностики супоросности свиноматок по ультразвуковым изображениям"

_DUP_CANDIDATE_A = Candidate(
    source_type=SourceType.PUBLICATION,
    title=_DUP_TITLE,
    url="https://doi.org/10.3390/ani15030318",
    org="Department of Data Science, Kangwon National University, Chuncheon 24341, Republic of Korea",
    raw_id="10.3390/ani15030318",
    provider="crossref",
)
_DUP_CANDIDATE_B = Candidate(
    source_type=SourceType.PUBLICATION,
    # СИНТЕТИКА, и это сказано прямо: заголовок совпадает с `_DUP_CANDIDATE_A`
    # по построению. Пара годится для проверки МАШИНЕРИИ склейки — что группы
    # объединяются, что склеенная заготовка не идёт на отдельную сборку
    # карточки, — но НЕ доказывает, что признак узнаёт настоящий дубль.
    # Настоящая пара из отчёта Э2-В лежит ниже (`_REAL_DUP_*`) и признаком
    # не ловится: 0.182. Расчёт по обеим отраслям —
    # `.planning/evidence/E2G-З6-similarity.md`.
    title=_DUP_TITLE,
    url="https://pure.jbnu.ac.kr/en/publications/deep-learning-enhanced-diagnosis-of-sow-pregnancy-through-low-fre/",
    org=None,
    raw_id=None,
    provider="yandex_search",
)

# ── Данные критерия 2: карточки 1 и 5 отчёта Э2-В — одна тема, разные разработки
_DIFFERENT_A = Candidate(
    source_type=SourceType.PUBLICATION,
    title="ИИ-система определения беременности свиноматок по УЗИ",
    url="http://koreabizwire.com/south-korea-pioneers-ai-technology-for-swine-pregnancy-detection",
    org=None,
    raw_id=None,
    provider="yandex_search",
)
_DIFFERENT_B = Candidate(
    source_type=SourceType.PUBLICATION,
    title="ИИ-система для анализа УЗИ свиноматок и определения супоросности",
    url="https://www.researchgate.net/publication/388346172_Deep_Learning",
    org=None,
    raw_id=None,
    provider="yandex_search",
)


def _scored(candidate: Candidate, *, relevance: int = 8) -> ScoredCandidate:
    return ScoredCandidate(
        candidate=candidate, relevance=relevance, maturity=6, accessibility=6, freshness=6, completeness=6
    )


# ── Составной признак: чистые функции ────────────────────────────────────────


#: Настоящая пара карточек 3 и 4 отчёта Э2-В, заголовки дословно
#: (`evidence/E2V-gate-фарма.md`). Отчёт сам признал их одной разработкой.
_REAL_DUP_A = Candidate(
    source_type=SourceType.PUBLICATION,
    title="ИИ-система диагностики супоросности свиноматок по ультразвуковым изображениям",
    url="https://doi.org/10.3390/ani15030318",
    org="Department of Data Science, Kangwon National University",
    raw_id="10.3390/ani15030318",
    provider="crossref",
)
_REAL_DUP_B = Candidate(
    source_type=SourceType.PUBLICATION,
    title="ИИ-диагностика супоросности свиноматок по УЗИ-изображениям (ViT-H, ConvNeXt, Xception)",
    url="https://pure.jbnu.ac.kr/en/publications/deep-learning-enhanced-diagnosis-of-sow-pregnancy-through-low-fre/",
    org=None,
    raw_id=None,
    provider="yandex_search",
)


def test_реальный_дубль_фармы_лексическим_сходством_не_ловится():
    """Названное ограничение признака, измеренное на настоящих данных.

    Карточки 3 и 4 отчёта Э2-В — одна разработка по признанию самого отчёта.
    На **дословных** заголовках сходство `_title_similarity` = **0.182** при
    любом разумном пороге: `raw_id` и организация у второй карточки пусты,
    адреса — разные площадки. Все четыре слагаемых признака молчат.

    Тест закрепляет факт, а не желаемое: лексическое сходство заголовков этот
    дубль не ловит и поймать не может. Понижение порога до уровня, где 0.182
    сработало бы, склеило бы всё подряд — расчёт по обеим отраслям приведён в
    `.planning/evidence/E2G-З6-similarity.md`.

    До цикла 2 на этом месте стоял тест с обратным утверждением, зелёный
    только потому, что заголовок второй карточки был подменён заголовком
    первой.
    """
    assert is_same_development(_REAL_DUP_A, _REAL_DUP_B) is False


def test_разные_разработки_на_одну_тему_не_признаются_одной():
    """Критерий 2: карточки 1 и 5 отчёта Э2-В — общая тема (УЗИ-диагностика
    супоросности), но разные конкретные работы. Доля общих значимых слов их
    заголовков — 4/7 ≈ 0.571, порог модуля строго выше (0.65), поэтому признак
    не срабатывает."""
    assert is_same_development(_DIFFERENT_A, _DIFFERENT_B) is False


def test_порог_держит_пару_критерия_2_ниже_себя():
    """Замок на сам порог (мутация «г»): если его опустить почти до нуля,
    пара критерия 2 тоже окажется дублем. Тест фиксирует фактическое числовое
    соотношение, а не только итоговый булев результат."""
    from app.pipeline.diversity import _title_similarity

    similarity = _title_similarity(_DIFFERENT_A.title, _DIFFERENT_B.title)
    assert similarity < TITLE_SIMILARITY_THRESHOLD
    assert similarity == pytest.approx(4 / 7, abs=1e-9)


def test_нормализация_организации_ловит_дубль_с_опф_и_адресным_хвостом():
    """Мутация «б»: без нормализации организации эта пара дублем не признаётся
    — заголовки разные, raw_id и адреса тоже. Признак обязан сработать именно
    через организацию: ОПФ («ФГБОУ ВО») и адресный хвост после запятой не
    должны мешать совпадению."""
    a = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Работа первой команды",
        url="https://example.org/a",
        org="ФГБОУ ВО Пермский национальный исследовательский политехнический университет",
        provider="crossref",
    )
    b = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Совершенно иначе названная работа второй команды",
        url="https://example.org/b",
        org="Пермский национальный исследовательский политехнический университет, г. Пермь",
        provider="cyberleninka",
    )
    assert is_same_development(a, b) is True


def test_разные_организации_без_опф_не_склеиваются():
    """Обратная сторона предыдущего теста: нормализация не схлопывает разные
    организации в одну — снятие ОПФ работает только на форме, не на сути."""
    a = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Работа первой команды",
        url="https://example.org/a",
        org="ФГБОУ ВО Пермский политехнический университет",
        provider="crossref",
    )
    b = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Совершенно другая работа второй команды",
        url="https://example.org/b",
        org="ФГБОУ ВО Уральский федеральный университет",
        provider="cyberleninka",
    )
    assert is_same_development(a, b) is False


def test_пустая_организация_не_совпадает_сама_с_собой():
    """Два кандидата без организации не считаются одной разработкой просто по
    факту отсутствия данных — иначе любая пара «не найдено» дублировала бы
    друг друга."""
    a = Candidate(
        source_type=SourceType.PUBLICATION, title="Работа один", url="https://example.org/a", provider="crossref"
    )
    b = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Совсем другая работа два",
        url="https://example.org/b",
        provider="crossref",
    )
    assert is_same_development(a, b) is False


def test_канонический_адрес_ловит_дубль_независимо_от_www_и_схемы():
    """Признак адреса нормализует схему, `www.` и завершающий слэш."""
    a = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Один заголовок",
        url="http://www.example.org/paper/1",
        provider="crossref",
    )
    b = Candidate(
        source_type=SourceType.PUBLICATION,
        title="Совсем другой заголовок про то же",
        url="https://example.org/paper/1/",
        provider="cyberleninka",
    )
    assert is_same_development(a, b) is True


# ── merge_duplicate_groups: пустой вход, один элемент, все дубли, все разные ─


def test_склейка_пустого_списка_возвращает_пустой_список():
    assert merge_duplicate_groups([]) == []


def test_склейка_единственной_заготовки_возвращает_её_без_изменений():
    groups = [[_scored(_DUP_CANDIDATE_A)]]
    merged = merge_duplicate_groups(groups)
    assert len(merged) == 1
    assert merged[0] == groups[0]


def test_все_заготовки_дубли_схлопываются_в_одну():
    groups = [[_scored(_DUP_CANDIDATE_A)], [_scored(_DUP_CANDIDATE_B)]]
    merged = merge_duplicate_groups(groups)
    assert len(merged) == 1
    assert len(merged[0]) == 2


def test_ни_одна_пара_не_дубли_список_не_меняется():
    groups = [[_scored(_DIFFERENT_A)], [_scored(_DIFFERENT_B)]]
    merged = merge_duplicate_groups(groups)
    assert len(merged) == 2


def test_склейка_смешанного_набора_оставляет_дубли_вместе_а_остальных_врозь():
    """Четыре заготовки: две из них дубль (A и B), третья и четвёртая — разные
    разработки. На выходе три группы, дублирующая пара — в одной."""
    groups = [
        [_scored(_DUP_CANDIDATE_A, relevance=9)],
        [_scored(_DIFFERENT_A, relevance=8)],
        [_scored(_DUP_CANDIDATE_B, relevance=7)],
        [_scored(_DIFFERENT_B, relevance=6)],
    ]
    merged = merge_duplicate_groups(groups)
    assert len(merged) == 3
    sizes = sorted(len(group) for group in merged)
    assert sizes == [1, 1, 2]


# ── Сквозные сценарии через ScoutPipeline.run на FakeProvider ───────────────

_CONTEST_PLAN = SearchPlan(
    queries=[
        SearchQuery(text="узи супоросность свиноматки", source_type=SourceType.PUBLICATION, lang="ru", rationale="т"),
        SearchQuery(text="хакатон узи свиноматки", source_type=SourceType.CONTEST, lang="ru", rationale="т"),
    ]
)


class _MainAdapter(SearchAdapter):
    """Источник основной очереди: две настоящие разработки плюс дубль одной из
    них — четыре заготовки на входе группировки."""

    name = "yandex_search"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        return [_DUP_CANDIDATE_A, _DIFFERENT_A, _DUP_CANDIDATE_B, _DIFFERENT_B]


class _NoDupAdapter(SearchAdapter):
    """Источник без единого дубля — для проверки строки «схлопнуто 0»."""

    name = "yandex_search"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        return [_DIFFERENT_A, _DIFFERENT_B]


def _contest_candidate(number: int, *, title: str, url: str, org: str | None = None) -> Candidate:
    return Candidate(
        source_type=SourceType.CONTEST,
        title=title,
        url=url,
        org=org,
        provider="yandex_search",
    )


# Заголовки A и B РАЗНЫЕ по строке (иначе их склеила бы уже существующая
# `deduplicate` по точному `dedup_key`, и тест проверял бы старый механизм, а
# не новый признак): дубль здесь ловится организацией — с ОПФ и без.
_CONTEST_A = _contest_candidate(
    1,
    title="Хакатонный прототип определения супоросности по УЗИ",
    url="https://hacks-ai.ru/event/1",
    org="ФГБОУ ВО Университет ИТМО",
)
_CONTEST_B = _contest_candidate(
    2,
    title="Система быстрой диагностики беременности свиней на выставке",
    url="https://vk.com/wall-1_1",
    org="Университет ИТМО, Санкт-Петербург",
)
_CONTEST_C = _contest_candidate(
    3, title="Совсем другой конкурсный прототип диагностики", url="https://hacks-ai.ru/event/3"
)
_CONTEST_DUP_TITLE = _CONTEST_A.title


class _ContestAdapter(SearchAdapter):
    """Источник на обе очереди: основная без дублей, конкурсная — с дублем."""

    name = "yandex_search"
    source_types = (SourceType.PUBLICATION, SourceType.CONTEST)

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        if query.source_type is SourceType.CONTEST:
            return [_CONTEST_A, _CONTEST_B, _CONTEST_C]
        return [_DIFFERENT_A, _DIFFERENT_B]


@pytest.fixture
def stub_stages(monkeypatch: pytest.MonkeyPatch):
    """Стадии модели — заглушки: тест про склейку в оркестраторе, а не про
    качество карточек (§7 — та же граница, что в `test_contest_section_flow.py`).
    Возвращает счётчик вызовов `author_card`."""
    calls: list[int] = []

    async def fake_plan(router, brief: Brief) -> SearchPlan:
        # План несёт обе формулировки всегда: адаптеры основной очереди
        # (`_MainAdapter`, `_NoDupAdapter`) не подписаны на `SourceType.CONTEST`
        # и конкурсную формулировку просто не получат — искать по ней некому.
        return _CONTEST_PLAN

    async def fake_score(router, brief: Brief, candidates: list[Candidate], **kwargs) -> list[ScoredCandidate]:
        return [_scored(candidate) for candidate in candidates]

    async def fake_author(router, brief: Brief, group: list[ScoredCandidate]) -> SolutionCard:
        calls.append(1)
        candidate = group[0].candidate
        return SolutionCard(
            title=candidate.title,
            essence="Суть решения одной строкой.",
            applicability="Закрывает часть задачи из брифа.",
            next_step="Запросить протокол испытаний.",
            evidence=[Evidence(claim="Факт из источника", url=candidate.url)],
            candidate_urls=[item.candidate.url for item in group],
        )

    async def fake_verify(router, card: SolutionCard, group=None) -> SolutionCard:
        return card

    async def fake_summary(router, brief: Brief, cards: list[SolutionCard]) -> ReportSummary:
        return ReportSummary(recommendation="Начать с первого варианта.")

    monkeypatch.setattr(stages, "plan_search", fake_plan)
    monkeypatch.setattr(stages, "score_candidates", fake_score)
    monkeypatch.setattr(stages, "author_card", fake_author)
    monkeypatch.setattr(stages, "verify_card", fake_verify)
    monkeypatch.setattr(stages, "summarize", fake_summary)
    return calls


async def _run(settings, fake_provider_factory, brief, adapter, *, progress=None):
    router = LLMRouter(settings, providers={"deepseek": fake_provider_factory("deepseek")})
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[adapter(client)], progress=progress)
        return await pipeline.run(brief, request_id=1, raw_text="узи свиноматок супоросность")


# ── Критерий 3: склейка доезжает до отчёта и экономит платный вызов ─────────


async def test_склейка_доезжает_до_отчёта_и_экономит_платный_вызов(settings, brief, fake_provider_factory, stub_stages):
    """Четыре заготовки, две из них дубль: `report.cards` — три карточки, а
    `author_card` вызван три раза, а не четыре (счёт фактически отправленного,
    РЕШ-126)."""
    report = await _run(settings, fake_provider_factory, brief, _MainAdapter)

    assert len(report.cards) == 3
    assert len(stub_stages) == 3


async def test_дублирующая_заготовка_не_печатается_дважды(settings, brief, fake_provider_factory, stub_stages):
    """Заголовок дубля встречается в отчёте один раз, а не два — обе заготовки
    склеены в одну карточку до сборки."""
    report = await _run(settings, fake_provider_factory, brief, _MainAdapter)

    titles = [card.title for card in report.cards]
    assert titles.count(_DUP_TITLE) == 1


# ── Критерий 4: конкурсная секция меряется тем же измерителем ───────────────


async def test_конкурсная_очередь_склеивает_дубль_и_печатает_свою_строку(
    settings, brief, fake_provider_factory, stub_stages
):
    """В `report.contest_cards` дублей нет, метрика по конкурсной очереди
    напечатана отдельной строкой от основной."""
    events: list[tuple[str, str, float]] = []

    async def progress(stage: str, message: str, fraction: float) -> None:
        events.append((stage, message, fraction))

    report = await _run(settings, fake_provider_factory, brief, _ContestAdapter, progress=progress)

    contest_titles = [card.title for card in report.contest_cards]
    assert contest_titles.count(_CONTEST_DUP_TITLE) == 1
    assert len(report.contest_cards) == 2

    messages = [message for _, message, _ in events]
    assert any(
        m == "Разнообразие (конкурсная очередь): заготовок до склейки 3, после 2, схлопнуто 1" for m in messages
    ), messages
    assert any(
        m == "Разнообразие (основная очередь): заготовок до склейки 2, после 2, схлопнуто 0" for m in messages
    ), messages


# ── Критерий 5: метрика печатается всегда, включая случай без единой склейки ─


async def test_метрика_печатается_при_нуле_склеек(settings, brief, fake_provider_factory, stub_stages):
    """Прогон без единого дубля: строка есть, «схлопнуто 0»."""
    events: list[tuple[str, str, float]] = []

    async def progress(stage: str, message: str, fraction: float) -> None:
        events.append((stage, message, fraction))

    await _run(settings, fake_provider_factory, brief, _NoDupAdapter, progress=progress)

    messages = [message for _, message, _ in events]
    assert any(
        m == "Разнообразие (основная очередь): заготовок до склейки 2, после 2, схлопнуто 0" for m in messages
    ), messages
