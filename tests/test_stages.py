"""Проверки чистых стадий пайплайна: склейка, отбор, отсечка по предметному
соответствию, группировка, честная выдача карточек (Ф-7), нормализация сырой
выдачи моделью (Э2-5).

Сети нет: провайдер модели — заглушка из conftest, ответ Яндекса подменяется
через respx."""

from __future__ import annotations

import base64
import json
import logging

import httpx
import pytest
import respx

from app.config import Settings
from app.llm import CostLimitExceededError, LLMRouter
from app.pipeline import stages
from app.pipeline.orchestrator import ScoutPipeline
from app.pipeline.stages import (
    NEUTRAL_SCORE_NOTE,
    candidates_listing,
    deduplicate,
    group_for_cards,
    select_publishable_cards,
    select_top,
)
from app.schemas import Candidate, Evidence, ScoredCandidate, SearchPlan, SearchQuery, SolutionCard, SourceType
from app.search.base import SearchAdapter

#: Ответ скоринга на одного кандидата — ровно по схеме `ScoreBatch`.
_SCORE_JSON = (
    '{"scores": [{"index": 0, "relevance": 8, "maturity": 6, '
    '"accessibility": 6, "freshness": 6, "completeness": 6, "notes": ""}]}'
)


def _card(title: str, *, evidence: bool = True) -> SolutionCard:
    """Минимально достаточная карточка для тестов отбора."""
    return SolutionCard(
        title=title,
        essence=f"Суть варианта «{title}».",
        applicability="Закрывает часть задачи из брифа.",
        next_step="Запросить у разработчика протокол испытаний.",
        evidence=[Evidence(claim="Факт из источника", url="https://example.org/a")] if evidence else [],
    )


def _placeholder_card() -> SolutionCard:
    """Каркас-заглушка из формата ошибок 1 `prompts/card_author.md`: группа
    кандидатов без источников."""
    return SolutionCard(
        title="не найдено",
        essence="не найдено",
        applicability="не найдено",
        next_step="не найдено",
        evidence=[],
    )


def _candidate(number: int, *, provider: str = "crossref") -> Candidate:
    """Кандидат-заготовка: различается только номером и источником."""
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=f"Кандидат {number}",
        url=f"https://example.org/{number}",
        provider=provider,
    )


def _scored(
    number: int, *, relevance: int, provider: str = "crossref", notes: str = "", others: int = 8
) -> ScoredCandidate:
    """Оценённый кандидат с заданной релевантностью; прочие оценки одинаковы,
    чтобы порядок отбора зависел только от `relevance`.

    `others` поднимает или опускает прочие оценки: так кандидат с низкой
    релевантностью попадает в отбор за счёт взвешенной суммы — ровно тот случай,
    ради которого отсечка и заводилась (РЕШ-48).
    """
    return ScoredCandidate(
        candidate=_candidate(number, provider=provider),
        relevance=relevance,
        maturity=others,
        accessibility=others,
        freshness=others,
        completeness=others,
        notes=notes,
    )


def test_deduplicate_merges_same_doi(candidates):
    """Дубли по DOI склеиваются: четыре кандидата превращаются в три варианта."""
    unique = deduplicate(candidates)
    assert len(unique) == 3


def test_deduplicate_prefers_showcase_over_open_source():
    """При равенстве ключа выигрывает витрина партнёров: там есть контакт разработчика."""
    common = {"title": "Технология регенерации", "year": 2025}
    open_source = Candidate(
        source_type=SourceType.PUBLICATION, url="https://example.org/a", provider="crossref", **common
    )
    partner = Candidate(source_type=SourceType.SHOWCASE, url="https://example.org/b", provider="showcase", **common)

    unique = deduplicate([open_source, partner])

    assert len(unique) == 1
    assert unique[0].provider == "showcase"


def test_select_top_limits_single_provider_dominance(candidates):
    """Один источник не должен занять всю выдачу, даже если его оценки выше."""
    scored = [
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title=f"Статья {i}",
                url=f"https://example.org/{i}",
                provider="crossref",
            ),
            relevance=10,
            maturity=8,
            accessibility=8,
            freshness=8,
            completeness=8,
        )
        for i in range(10)
    ]
    scored.append(
        ScoredCandidate(
            candidate=candidates[2],  # из витрины
            relevance=6,
            maturity=6,
            accessibility=6,
            freshness=6,
            completeness=6,
        )
    )

    picked = select_top(scored, limit=6)

    providers = {item.candidate.provider for item in picked}
    assert "showcase" in providers, "выдача не должна состоять из одного источника"
    assert sum(1 for i in picked if i.candidate.provider == "crossref") <= 3


def test_relevance_min_default_is_zero_cutoff_disabled():
    """Порог по умолчанию — 0: отсечка выключена, пока значение не подобрано на
    прогоне (Т-10). Догадка в коде хуже отсутствия отсечки."""
    field = Settings.model_fields["relevance_min"]

    assert field.default == 0


def test_select_top_drops_candidate_below_relevance_min():
    """Кандидат с relevance ниже порога в отбор не попадает (РЕШ-48)."""
    scored = [_scored(0, relevance=9), _scored(1, relevance=3)]

    picked = select_top(scored, limit=6, relevance_min=6)

    assert [item.candidate.title for item in picked] == ["Кандидат 0"]


def test_select_top_keeps_neutral_score_at_same_threshold():
    """Нейтральная оценка отсечкой не режется никогда: её проставил код после
    отказа батча скоринга, и отсечь её — значит выдать отказ стадии за «все
    нерелевантны». Порог тот же, что и в тесте выше."""
    scored = [
        _scored(0, relevance=9),
        _scored(1, relevance=3),
        _scored(2, relevance=5, notes=NEUTRAL_SCORE_NOTE),
    ]

    picked = select_top(scored, limit=6, relevance_min=6)

    titles = [item.candidate.title for item in picked]
    assert "Кандидат 2" in titles, "кандидат с нейтральной оценкой отсечён"
    assert "Кандидат 1" not in titles


def test_select_top_with_zero_threshold_keeps_previous_behaviour():
    """При relevance_min=0 состав отбора совпадает с прежним поведением: отсечка
    выключена, отбирают только сумма оценок и лимит на один источник."""
    scored = [_scored(number, relevance=number) for number in range(8)]

    with_cutoff_off = select_top(scored, limit=4, relevance_min=0)

    # Прежнее поведение: сортировка по total и потолок на один источник.
    ordered = sorted(scored, key=lambda s: s.total, reverse=True)
    assert [item.candidate.title for item in with_cutoff_off] == [item.candidate.title for item in ordered[:2]]


def test_select_top_reads_threshold_from_settings(monkeypatch):
    """Без явного аргумента порог берётся из настроек — иначе `RELEVANCE_MIN`
    остался бы настройкой, которую никто не читает."""
    monkeypatch.setattr(stages, "get_settings", lambda: Settings(relevance_min=7))
    scored = [_scored(0, relevance=9), _scored(1, relevance=3)]

    picked = select_top(scored, limit=6)

    assert [item.candidate.title for item in picked] == ["Кандидат 0"]


def test_select_top_logs_calibration_even_when_nothing_is_cut(caplog):
    """Строка калибровки печатается всегда, в том числе при пороге 0 и нулевой
    отсечке: это единственный источник данных для Т-10 (сохранённых оценок нет),
    и ноль обязан быть отличим от «отсечка не сработала»."""
    scored = [_scored(number, relevance=relevance) for number, relevance in enumerate([3, 5, 5, 8, 10])]

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top(scored, limit=6, relevance_min=0)

    line = next((r.getMessage() for r in caplog.records if "калибровка relevance" in r.getMessage()), None)
    assert line is not None, "строки калибровки нет в журнале"
    assert "кандидатов 5" in line
    assert "3/5/10" in line, f"нет распределения min/медиана/max: {line}"
    assert "порог 0" in line
    assert "отсечено 0" in line
    # Колонка «отсеклось бы»: при 4 уходит один кандидат, при 6 — три, при 7 — три.
    assert "при 4: 1" in line and "при 5: 1" in line and "при 6: 3" in line and "при 7: 3" in line, line


def test_calibration_line_counts_neutral_scores_separately(caplog):
    """Нейтральные оценки в журнале считаются отдельно и не попадают в «отсеклось
    бы»: иначе калибровка Т-10 приняла бы отказ батча за нерелевантных кандидатов."""
    scored = [_scored(0, relevance=9), _scored(1, relevance=5, notes=NEUTRAL_SCORE_NOTE)]

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top(scored, limit=6, relevance_min=0)

    line = next(r.getMessage() for r in caplog.records if "калибровка relevance" in r.getMessage())
    assert "нейтральных оценок 1" in line
    assert "при 6: 0" in line, f"нейтральная оценка попала в «отсеклось бы»: {line}"


def test_calibration_line_printed_on_empty_input(caplog):
    """Кандидатов нет — строка всё равно печатается: пустой отбор обязан быть
    отличим от неработающей стадии."""
    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top([], limit=6, relevance_min=0)

    assert any("калибровка relevance" in r.getMessage() for r in caplog.records)


def _calibration_line(caplog) -> str:
    """Строка калибровки из журнала: по ней и только по ней Т-10 выбирает порог."""
    line = next((r.getMessage() for r in caplog.records if "калибровка relevance" in r.getMessage()), None)
    assert line is not None, "строки калибровки нет в журнале"
    return line


def test_calibration_spread_uses_only_model_scores(caplog):
    """min/медиана/max считаются по тем же оценкам, что и «отсеклось бы», — только
    по оценкам модели.

    Иначе один отказавший батч сдвигает распределение ровно в том диапазоне, из
    которого Т-10 выбирает порог: те же десять оценок модели дают 1/2/4, а с
    добавленным батчем из десяти нейтральных пятёрок — 1/4.5/5, при том что
    колонка «отсеклось бы» не меняется ни на единицу.
    """
    scored = [_scored(number, relevance=relevance) for number, relevance in enumerate([1, 1, 2, 2, 3, 3, 4, 4, 2, 1])]
    scored.extend(stages._neutral_scores([_candidate(100 + number) for number in range(10)]))

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top(scored, limit=12, relevance_min=0)

    line = _calibration_line(caplog)
    assert "оценок модели 10" in line, line
    assert "min/медиана/max по оценкам модели 1/2/4" in line, line
    assert "1/4.5/5" not in line, f"нейтральные оценки попали в распределение: {line}"
    assert "нейтральных оценок 10" in line, line


def test_calibration_says_nothing_measured_when_whole_batch_failed(caplog):
    """Отказал весь батч — строка обязана показать, что измерять было нечего.

    Предельный случай прежней формулы: `кандидатов 4, min/медиана/max 5/5/5,
    отсечено 0` читается как здоровое измерение, хотя ни одной оценки модель не
    дала.
    """
    scored = stages._neutral_scores([_candidate(number) for number in range(4)])

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top(scored, limit=6, relevance_min=6)

    line = _calibration_line(caplog)
    assert "кандидатов 4" in line, line
    assert "оценок модели 0" in line, line
    assert "нет оценок" in line, line
    assert "5/5/5" not in line, f"нейтральные пятёрки выданы за распределение оценок: {line}"


def test_calibration_reports_zero_cut_inside_selected_limit(caplog):
    """Отсечка по всему списку и отсечка внутри отбираемых `limit` — разные числа.

    Решение Т-10 принимается по составу карточек, а до карточек доходит только
    top-`limit`. Здесь порог 6 срезал бы 30 кандидатов из 42, но состав top-12 не
    изменился бы ни на одну позицию: по первой колонке это неотличимо от случая,
    где меняется четверть выдачи.
    """
    scored = [_scored(number, relevance=9, provider="crossref" if number % 2 else "openalex") for number in range(12)]
    scored.extend(
        _scored(100 + number, relevance=1, provider="crossref" if number % 2 else "openalex") for number in range(30)
    )

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top(scored, limit=12, relevance_min=0)

    line = _calibration_line(caplog)
    assert "кандидатов 42" in line, line
    assert "отсеклось бы по всему списку при 4: 30, при 5: 30, при 6: 30, при 7: 30" in line, line
    assert "среди отобранных 12 (лимит 12) ниже порога 4: 0, 5: 0, 6: 0, 7: 0" in line, line


def test_calibration_counts_cut_inside_selected_limit(caplog):
    """Вторая колонка считает только тех, кто реально доходит до карточек.

    Шесть кандидатов «мимо задачи» на весь список, но лишь три из них взвешенная
    сумма вытянула в top-12 — именно эти три и есть то, ради чего Т-10 включает
    отсечку.
    """
    providers = ("crossref", "openalex", "elibrary")
    scored = [_scored(number, relevance=8, provider=providers[number % 3]) for number in range(9)]
    # Низкая релевантность, но высокие прочие оценки — в отбор попадают.
    scored.extend(_scored(20 + number, relevance=3, others=10, provider=providers[number % 3]) for number in range(3))
    # Те же 3 балла релевантности, но в отбор не проходят: не о них решение.
    scored.extend(_scored(40 + number, relevance=3, others=4, provider=providers[number % 3]) for number in range(3))

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top(scored, limit=12, relevance_min=0)

    line = _calibration_line(caplog)
    assert "кандидатов 15" in line, line
    assert "отсеклось бы по всему списку при 4: 6, при 5: 6, при 6: 6, при 7: 6" in line, line
    assert "среди отобранных 12 (лимит 12) ниже порога 4: 3, 5: 3, 6: 3, 7: 3" in line, line


def test_neutral_scores_from_producer_survive_cutoff():
    """Фактический выход `_neutral_scores` проходит отсечку при пороге выше 5.

    Прежние три теста строили пометку руками из константы, поэтому производитель
    пометки и её потребитель могли разойтись молча: `_neutral_scores` в тестах не
    вызывался ни разу.
    """
    neutral = stages._neutral_scores([_candidate(number) for number in range(3)])

    picked = select_top(neutral, limit=6, relevance_min=7)

    assert len(picked) == 3, "кандидаты с нейтральной оценкой отсечены порогом"


def test_neutral_mark_survives_change_of_note_text(caplog):
    """Пометка структурная: правка текста `notes` её не рвёт.

    Со сравнением строк дописанный к пометке номер батча тихо отправлял бы
    нейтральных кандидатов под отсечку, а журнал показывал бы «нейтральных
    оценок 0».
    """
    item = stages.NeutralScoredCandidate(
        candidate=_candidate(0),
        relevance=5,
        maturity=5,
        accessibility=5,
        freshness=5,
        completeness=5,
        notes=f"{NEUTRAL_SCORE_NOTE} (батч 2)",
    )

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        picked = select_top([item], limit=6, relevance_min=7)

    assert len(picked) == 1, "нейтральный кандидат отсечён из-за изменения текста пометки"
    assert "нейтральных оценок 1" in _calibration_line(caplog)


async def test_цитируемость_доезжает_до_входа_скоринга(settings, fake_provider_factory, brief):
    """РЕШ-50: цитируемость — единственный бесплатный внешний признак качества
    публикации, и работает он только если доходит до модели, а не оседает в
    оркестраторе."""
    provider = fake_provider_factory("deepseek", [_SCORE_JSON])
    router = LLMRouter(settings, providers={"deepseek": provider})
    candidate = _candidate(1)

    await stages.score_candidates(router, brief, [candidate], citations={candidate.dedup_key(): 137})

    listing = provider.calls[0][1].content
    assert "Цитирований: 137" in listing


async def test_неизвестная_цитируемость_строки_не_создаёт(settings, fake_provider_factory, brief):
    """Инвариант №1: чего в данных нет, того нет и в промпте. Строка
    «Цитирований: неизвестно» была бы приглашением модели домыслить число."""
    provider = fake_provider_factory("deepseek", [_SCORE_JSON])
    router = LLMRouter(settings, providers={"deepseek": provider})

    await stages.score_candidates(router, brief, [_candidate(1)])

    listing = provider.calls[0][1].content
    assert "Цитирований" not in listing


def test_нулевая_цитируемость_это_известное_значение():
    """Ноль — не «неизвестно»: работа в каталоге есть, её просто не цитировали.
    Это сигнал для скоринга, а не повод молчать."""
    candidate = _candidate(1)

    listing = candidates_listing([candidate], {candidate.dedup_key(): 0})

    assert "Цитирований: 0" in listing


def test_цитируемость_достаётся_своему_кандидату():
    """Ключ — тот же, по которому склеивались дубли: перепутанные местами
    числа хуже отсутствующих."""
    first, second = _candidate(1), _candidate(2)

    listing = candidates_listing([first, second], {second.dedup_key(): 9})

    head, tail = listing.split("\n\n")
    assert "Цитирований" not in head
    assert "Цитирований: 9" in tail


def test_group_for_cards_merges_same_organisation():
    """Три публикации одной лаборатории — это один вариант решения, а не три карточки."""
    scored = [
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title=f"Работа {i}",
                url=f"https://example.org/{i}",
                org="Институт катализа",
                provider="crossref",
            ),
            relevance=8,
            maturity=6,
            accessibility=7,
            freshness=7,
            completeness=7,
        )
        for i in range(3)
    ]
    scored.append(
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title="Работа другой команды",
                url="https://example.org/other",
                org="Технический университет",
                provider="crossref",
            ),
            relevance=7,
            maturity=6,
            accessibility=6,
            freshness=6,
            completeness=6,
        )
    )

    groups = group_for_cards(scored, target=6)

    assert len(groups) == 2
    assert len(groups[0]) == 3


def test_group_for_cards_respects_target():
    """Больше карточек, чем заказано, пайплайн не отдаёт."""
    scored = [
        ScoredCandidate(
            candidate=Candidate(
                source_type=SourceType.PUBLICATION,
                title=f"Работа {i}",
                url=f"https://example.org/{i}",
                org=f"Организация {i}",
                provider="crossref",
            ),
            relevance=8,
            maturity=6,
            accessibility=6,
            freshness=6,
            completeness=6,
        )
        for i in range(12)
    ]

    assert len(group_for_cards(scored, target=5)) == 5


def test_select_publishable_cards_drops_empty_evidence_and_placeholder():
    """Из шести карточек — две с пустым evidence и одна-пустышка «не найдено»
    во всех ключевых полях — в отчёт идут три честных, порядок сохраняется."""
    honest = [_card(f"Вариант {i}") for i in range(3)]
    no_evidence = [_card(f"Без источника {i}", evidence=False) for i in range(2)]
    cards = [honest[0], no_evidence[0], honest[1], _placeholder_card(), no_evidence[1], honest[2]]

    published = select_publishable_cards(cards)

    assert [c.title for c in published] == ["Вариант 0", "Вариант 1", "Вариант 2"]


def test_select_publishable_cards_caps_at_seven():
    """Девять честных карточек — в отчёте не больше семи (§2а SPEC, FR-9)."""
    cards = [_card(f"Вариант {i}") for i in range(9)]

    published = select_publishable_cards(cards)

    assert len(published) == 7
    assert [c.title for c in published] == [f"Вариант {i}" for i in range(7)]


def test_select_publishable_cards_keeps_all_when_under_limit():
    """Меньше семи честных карточек — нижней границы нет, отдаём сколько есть."""
    cards = [_card(f"Вариант {i}") for i in range(3)]

    published = select_publishable_cards(cards)

    assert len(published) == 3


# ── Стадия extractor: нормализация сырой выдачи моделью (Э2-5) ──────────────

#: Сырые записи веб-поиска: заголовок, домен, время правки и кусок текста
#: страницы. Ни организации, ни года, ни авторов в полях нет — они лежат
#: внутри текста, ради этого стадия и заведена.
_RAW_WEB = [
    {
        "url": "https://ipc.rssi.ru/lab/catalysis",
        "title": "Лаборатория катализа ИПХФ РАН",
        "domain": "ipc.rssi.ru",
        "modtime": "20240115T103000",
        "snippet": "В 2021 году в ИПХФ РАН разработан катализатор гидроочистки дизельных фракций.",
    },
    {
        "url": "https://ipc.rssi.ru/news/",
        "title": "Новости института",
        "domain": "ipc.rssi.ru",
        "modtime": "20250101T000000",
        "snippet": "Читайте также: конференции, объявления, вакансии.",
    },
]

#: Ответ модели: годный кандидат плюс запись без ссылки. `provider` и
#: `source_type` в ответе намеренно чужие — код обязан навязать свои.
_EXTRACT_JSON = json.dumps(
    {
        "candidates": [
            {
                "source_type": "publication",
                "title": "Лаборатория катализа ИПХФ РАН",
                "url": "https://ipc.rssi.ru/lab/catalysis",
                "org": "ИПХФ РАН",
                "authors": [],
                "year": 2021,
                "snippet": "В 2021 году в ИПХФ РАН разработан катализатор гидроочистки дизельных фракций.",
                "raw_id": None,
                "provider": "модель-выдумщица",
            },
            {
                "source_type": "web",
                "title": "Запись без ссылки",
                "url": "",
                "authors": [],
                "year": None,
                "snippet": "",
                "raw_id": None,
                "provider": "yandex_search",
            },
        ],
        "dropped_no_url": 1,
    },
    ensure_ascii=False,
)


def _extractor_router(settings, fake_provider_factory, responses: list[str] | None = None, **kwargs):
    """Роутер с заглушкой на месте GigaChat: маршрут стадии — `extractor:gigachat`."""
    provider = fake_provider_factory("gigachat", responses, **kwargs)
    return LLMRouter(settings, providers={"gigachat": provider}), provider


class _FakeWebAdapter(SearchAdapter):
    """Источник с неструктурированной выдачей: отдаёт свой разбор и сырые
    записи ТОГО ЖЕ ответа — как настоящий веб-поиск (`search_raw`)."""

    name = "fake_web"
    source_types = (SourceType.WEB,)
    needs_extraction = True

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        candidates, _ = await self.search_raw(query, limit=limit)
        return candidates

    async def search_raw(self, query: SearchQuery, *, limit: int = 20) -> tuple[list[Candidate], list[dict]]:
        own = Candidate(
            source_type=SourceType.WEB,
            title="Разбор адаптера",
            url="https://ipc.rssi.ru/lab/catalysis",
            provider=self.name,
        )
        return [own], [dict(record) for record in _RAW_WEB]


class _FakeApiAdapter(SearchAdapter):
    """Источник со структурированным API: флага нет, стадии не видит.

    `search_raw` намеренно взрывается: у источника без флага оркестратор обязан
    спрашивать `search`. Правило это не косметическое — адаптер, собранный не от
    `SearchAdapter` (в проекте такие есть в тестах доктора), метода `search_raw`
    не имеет вовсе, и поход в него превратил бы рабочий источник в недоступный.
    """

    name = "fake_api"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        return [_candidate(1, provider=self.name)]

    async def search_raw(self, query: SearchQuery, *, limit: int = 20) -> tuple[list[Candidate], list[dict]]:
        raise AssertionError("источник без needs_extraction спрашивают через search")


def _plan(*source_types: SourceType) -> SearchPlan:
    return SearchPlan(
        queries=[
            SearchQuery(text="катализатор гидроочистки", source_type=source_type, lang="ru", rationale="тест")
            for source_type in source_types
        ]
    )


async def test_extractor_собирает_кандидата_из_сырого_сниппета(settings, fake_provider_factory):
    """Из текста страницы получается Candidate, а провайдер и тип источника —
    те, что передал код: правило 9 промпта модель нарушает регулярно, а
    кандидат с чужим провайдером испортил бы и счётчики, и отбор."""
    router, _ = _extractor_router(settings, fake_provider_factory, [_EXTRACT_JSON])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", _RAW_WEB, router)

    assert len(found) == 1
    assert found[0].provider == "yandex_search"
    assert found[0].source_type is SourceType.WEB
    assert found[0].org == "ИПХФ РАН"
    assert found[0].year == 2021


async def test_extractor_отбрасывает_невалидную_запись_и_отдаёт_остальные(settings, fake_provider_factory, caplog):
    """Запись без ссылки выпадает с записью в лог, а выдача из-за неё не
    пропадает целиком (инвариант №5)."""
    router, _ = _extractor_router(settings, fake_provider_factory, [_EXTRACT_JSON])

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        found = await stages.extract_candidates(SourceType.WEB, "yandex_search", _RAW_WEB, router)

    assert [c.title for c in found] == ["Лаборатория катализа ИПХФ РАН"]
    assert "extractor: запись отброшена (yandex_search)" in caplog.text
    assert "extractor: yandex_search, сырых записей 2 → кандидатов 1" in caplog.text


async def test_extractor_заполняет_метки_промпта_через_render_prompt(settings, fake_provider_factory):
    """Инвариант №4: незаполненная метка ушла бы в модель текстом. Проверяем
    факт подстановки всех трёх, а не их состав — состав закреплён
    `tests/test_prompts.py`."""
    router, provider = _extractor_router(settings, fake_provider_factory, [_EXTRACT_JSON])

    await stages.extract_candidates(SourceType.WEB, "yandex_search", _RAW_WEB, router)

    system = provider.calls[0][0].content
    assert "{{" not in system
    assert "web" in system and "yandex_search" in system
    assert "https://ipc.rssi.ru/lab/catalysis" in system


async def test_extractor_просит_свой_лимит_токенов(settings, fake_provider_factory):
    """§0.1 п. 7 и РЕШ-61 п. 1: у каждой стадии своя щедрость по токенам, у
    extractor — 7000, как у автора карточек. Пост-аудит измерил предельный по
    контракту промпта ответ в ≈6 334 токена: при 4096 он обрывался, и роутер
    переспрашивал заново, оплачивая вход второй раз."""
    base = fake_provider_factory

    class _Recording(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.limits: list[int] = []

        async def complete(self, messages, *, model=None, temperature=0.2, max_tokens=4096, json_mode=False):
            self.limits.append(max_tokens)
            return await super().complete(
                messages, model=model, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
            )

    provider = _Recording("gigachat", [_EXTRACT_JSON])
    router = LLMRouter(settings, providers={"gigachat": provider})

    await stages.extract_candidates(SourceType.WEB, "yandex_search", _RAW_WEB, router)

    assert provider.limits == [7000]


async def test_extractor_на_пустой_выдаче_модель_не_зовёт(settings, fake_provider_factory):
    """Пустых записей нечего нормализовать — платить за это незачем."""
    router, provider = _extractor_router(settings, fake_provider_factory, [_EXTRACT_JSON])

    assert await stages.extract_candidates(SourceType.WEB, "yandex_search", [], router) == []
    assert provider.calls == []


async def test_адаптер_без_флага_стадию_не_вызывает(settings, fake_provider_factory):
    """Структурированный источник через модель не гоняем — незачем жечь токены."""
    router, provider = _extractor_router(settings, fake_provider_factory, [_EXTRACT_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[_FakeApiAdapter(client)])
        outcome = await pipeline.gather_candidates(_plan(SourceType.PUBLICATION))

    assert provider.calls == [], "стадия вызвана для источника со структурированным API"
    assert [c.provider for c in outcome.candidates] == ["fake_api"]


async def test_адаптер_с_флагом_отдаёт_кандидатов_стадии(settings, fake_provider_factory):
    """У источника с `needs_extraction` в пайплайн идёт разбор модели, а не
    собственный разбор адаптера: это одни и те же записи выдачи, и складывать
    их значило бы протащить страницу в отбор дважды."""
    router, provider = _extractor_router(settings, fake_provider_factory, [_EXTRACT_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[_FakeWebAdapter(client)])
        outcome = await pipeline.gather_candidates(_plan(SourceType.WEB))

    assert len(provider.calls) == 1
    assert [c.title for c in outcome.candidates] == ["Лаборатория катализа ИПХФ РАН"]
    assert outcome.hits_by_provider == {"fake_web": 1}
    assert outcome.unavailable == []


async def test_отказ_стадии_не_роняет_прогон_и_не_прячет_источник(settings, fake_provider_factory, caplog):
    """Инвариант №5: модель отказала — выдача уже оплачена и разобрана
    адаптером, берём его разбор. Недоступным источник при этом не становится:
    он ответил."""
    router, _ = _extractor_router(settings, fake_provider_factory, ["не json"], fail_times=99)
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[_FakeWebAdapter(client)])
        with caplog.at_level(logging.WARNING, logger="app.pipeline.orchestrator"):
            outcome = await pipeline.gather_candidates(_plan(SourceType.WEB))

    assert [c.title for c in outcome.candidates] == ["Разбор адаптера"]
    assert outcome.unavailable == []
    assert "extractor: стадия не удалась" in caplog.text


async def test_стоп_кран_стоимости_из_стадии_не_подменяется_разбором_адаптера(settings, fake_provider_factory, caplog):
    """РЕШ-45: исчерпанный лимит — не отказ модели, а команда «стоп тратить».

    `CostLimitExceededError` — наследник `RuntimeError`, поэтому общий
    `except Exception` в `_extract` поймал бы её вместе с отказами модели и
    молча превратил стоп-кран в «беру разбор адаптера»: остальные формулировки
    плана продолжили бы платить дальше. Проверяем ровно это: стадия падает по
    лимиту, наружу выходит `CostLimitExceededError`, а не выдача с разбором
    адаптера, и в лог не уходит «беру разбор адаптера».

    Лимит 0.005 ₽ при 0.01 ₽ за вызов заглушки: первый же вызов стадии его
    выбирает.
    """
    low_limit = settings.model_copy(update={"cost_limit_rub_per_request": 0.005})
    router, provider = _extractor_router(low_limit, fake_provider_factory, [_EXTRACT_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=low_limit, router=router, adapters=[_FakeWebAdapter(client)])
        with (
            caplog.at_level(logging.WARNING, logger="app.pipeline.orchestrator"),
            pytest.raises((CostLimitExceededError, BaseExceptionGroup)) as raised,
        ):
            await pipeline.gather_candidates(_plan(SourceType.WEB))

    # Требуем ТИП, а не «суть в любой обёртке» (П-1 аудита). Прежняя редакция
    # теста принимала и `BaseExceptionGroup` — то есть узаконивала форму, на
    # которой контракт РЕШ-45 ломается: `cli.py` ловит стоп-кран по типу, а в
    # `requests.error` вместо причины и суммы попадала строка «unhandled errors
    # in a TaskGroup». Гейт, принимающий фактическое поведение вместо
    # требования контракта, перестаёт быть гейтом.
    assert type(raised.value) is CostLimitExceededError
    assert "лимите" in str(raised.value)
    assert len(provider.calls) == 1, "стадию звали ровно один раз — на нём лимит и кончился"
    assert "беру разбор адаптера" not in caplog.text


async def test_стоп_кран_доезжает_до_записи_прогона_причиной_а_не_обёрткой(settings, fake_provider_factory):
    """П-1: три теста `tests/test_persistence.py` требуют слова «лимит» в
    `requests.error`, а `persist_run` пишет туда `str(exc)`. Обёртка
    `TaskGroup` давала «unhandled errors in a TaskGroup (1 sub-exception)» —
    без причины и без суммы. Проверяем ровно ту строку, что уйдёт в БД."""
    low_limit = settings.model_copy(update={"cost_limit_rub_per_request": 0.005})
    router, _ = _extractor_router(low_limit, fake_provider_factory, [_EXTRACT_JSON])
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=low_limit, router=router, adapters=[_FakeWebAdapter(client)])
        with pytest.raises(CostLimitExceededError) as raised:
            await pipeline.gather_candidates(_plan(SourceType.WEB))

    written = str(raised.value)
    assert "лимит" in written
    assert "TaskGroup" not in written


# ── Н-1: текст вокруг JSON больше не обнуляет оплаченную выдачу ─────────────


def _model_candidate(url: str, title: str) -> dict:
    """Кандидат в ответе модели. `provider` и `source_type` намеренно чужие —
    код обязан навязать свои."""
    return {
        "source_type": "publication",
        "title": title,
        "url": url,
        "org": "ИПХФ РАН",
        "authors": [],
        "year": 2021,
        "snippet": "Дословная цитата со страницы.",
        "raw_id": None,
        "provider": "модель-выдумщица",
    }


def _model_batch(*candidates: dict, dropped: int = 0) -> str:
    return json.dumps({"candidates": list(candidates), "dropped_no_url": dropped}, ensure_ascii=False)


#: Два годных кандидата — оба адреса есть в `_RAW_WEB`.
_EXTRACT_JSON_TWO = _model_batch(
    _model_candidate("https://ipc.rssi.ru/lab/catalysis", "Лаборатория катализа ИПХФ РАН"),
    _model_candidate("https://ipc.rssi.ru/news/", "Новости института"),
)


@pytest.mark.parametrize(
    ("ответ_модели", "ожидается"),
    [
        pytest.param(_EXTRACT_JSON + "\n\nЕсли нужно, разберу ещё.", 1, id="проза-после-json"),
        pytest.param(_EXTRACT_JSON + ".", 1, id="одна-точка-после-json"),
        pytest.param(_EXTRACT_JSON_TWO + "\n\nВот такие записи нашлись.", 2, id="два-кандидата-и-проза"),
        pytest.param("Разобрал выдачу:\n" + _EXTRACT_JSON, 1, id="проза-перед-json"),
        pytest.param(f"```json\n{_EXTRACT_JSON}\n```", 1, id="markdown-забор"),
    ],
)
async def test_текст_вокруг_json_не_обнуляет_выдачу(settings, fake_provider_factory, ответ_модели, ожидается):
    """Н-1: пока `candidates` имели значение по умолчанию, любой JSON-объект был
    валиден как ПУСТОЙ батч, а разборщик берёт первый подошедший блок, перебирая
    их от конца текста к началу, — выигрывал вложенный объект-кандидат.
    Достаточно было ОДНОЙ ТОЧКИ после закрывающей скобки, чтобы оплаченная
    выдача Яндекса стёрлась молча, без единого исключения."""
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ_модели])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", _RAW_WEB, router)

    assert len(found) == ожидается
    assert all(c.provider == "yandex_search" for c in found)


async def test_стадия_возвращает_всех_годных_кандидатов_а_не_первого(settings, fake_provider_factory):
    """Критерий 1 плана: «невалидная запись отбрасывается, стадия возвращает
    ОСТАЛЬНЫЕ». Во всех прежних тестах валидный кандидат был ровно один, и
    «первый» с «остальными» совпадали — мутация `return candidates[:1]`
    переживала весь набор (M8 аудита)."""
    ответ = _model_batch(
        _model_candidate("https://ipc.rssi.ru/lab/catalysis", "Лаборатория катализа ИПХФ РАН"),
        {"source_type": "web", "title": "Запись без ссылки", "url": "", "provider": "yandex_search"},
        _model_candidate("https://ipc.rssi.ru/news/", "Новости института"),
        dropped=1,
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", _RAW_WEB, router)

    assert [c.title for c in found] == ["Лаборатория катализа ИПХФ РАН", "Новости института"]


_EXTRACT_EMPTY = _model_batch()


async def test_пустой_результат_стадии_виден_снаружи_и_не_стирает_выдачу(settings, fake_provider_factory, caplog):
    """Н-1, часть 2: ноль кандидатов из непустых сырых записей — не успех.

    Откат живёт только в ветке `except`, а пустой список исключением не был:
    оплаченная выдача исчезала, `hits` показывал 0, и снаружи это было
    неотличимо от честного «источник ответил и ничего не нашёл». Теперь пробел
    и назван в логе, и уехал в `SearchOutcome.problems`, а выдача восстановлена
    разбором адаптера — тем самым, каким веб приходил в прогон до Т-6."""
    router, _ = _extractor_router(settings, fake_provider_factory, [_EXTRACT_EMPTY])
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[_FakeWebAdapter(client)])
        with caplog.at_level(logging.WARNING):
            outcome = await pipeline.gather_candidates(_plan(SourceType.WEB))

    assert [c.title for c in outcome.candidates] == ["Разбор адаптера"]
    assert outcome.hits_by_provider == {"fake_web": 1}
    assert outcome.unavailable == []
    assert outcome.problems and "ноль кандидатов из 2 сырых записей" in outcome.problems[0]
    assert "вклад оплаченной выдачи нулевой" in caplog.text


async def test_отказ_стадии_тоже_попадает_в_problems(settings, fake_provider_factory):
    """Отказ стадии и её пустой результат — один класс пробела: выдача
    оплачена, вклад нулевой. Оба обязаны оставить след снаружи."""
    router, _ = _extractor_router(settings, fake_provider_factory, ["не json"], fail_times=99)
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[_FakeWebAdapter(client)])
        outcome = await pipeline.gather_candidates(_plan(SourceType.WEB))

    assert outcome.problems and "стадия не удалась" in outcome.problems[0]


# ── M1 и П-6: обезвреживание меток правит метку, а не содержание ────────────


async def test_метка_с_чужой_страницы_не_роняет_сборку_промпта(settings, fake_provider_factory):
    """M1 аудита: обезвреживание меток перед `render_prompt` не было закреплено
    ни одним тестом — существующие проверки закрывают шаблон, а не данные.
    Метка `{{...}}` со случайной страницы уронила бы сборку промпта УЖЕ ПОСЛЕ
    оплаченного запроса к источнику: выдача получена, деньги потрачены, прогон
    падает на подстановке."""
    заражённые = [
        {
            "url": "https://ipc.rssi.ru/lab/catalysis",
            "title": "Шаблон отчёта {{CLIENT_NAME}}",
            "domain": "ipc.rssi.ru",
            "modtime": "20240115T103000",
            "snippet": "Подставьте {{INDUSTRY}} в поле отрасли.",
        }
    ]
    router, provider = _extractor_router(settings, fake_provider_factory, [_EXTRACT_JSON])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", заражённые, router)

    system = provider.calls[0][0].content
    assert "{{" not in system, "метка со страницы уронила бы render_prompt после оплаченного запроса"
    assert "{CLIENT_NAME}" in system and "{INDUSTRY}" in system
    assert len(found) == 1


@pytest.mark.parametrize(
    ("исходный", "ожидаемый"),
    [
        pytest.param("задача завода {{CLIENT_NAME}}", "задача завода {CLIENT_NAME}", id="метка-обезврежена"),
        pytest.param("{{{{X}}}}", "{X}", id="серия-скобок-за-один-проход"),
        pytest.param('JSON на странице: {"a": {"b": 1}}', 'JSON на странице: {"a": {"b": 1}}', id="json-цел"),
        pytest.param("код: if (x) {{ y(); }}", "код: if (x) {{ y(); }}", id="код-цел"),
        pytest.param("формула a = {{n}} при n>1", "формула a = {{n}} при n>1", id="строчные-не-метка"),
    ],
)
def test_обезвреживание_правит_метку_а_не_содержание(исходный, ожидаемый):
    """П-6: верификация Т-6 заявила, что содержание, уходящее в модель, не
    портится. Это было неверно — сдвоенные скобки схлопывались без разбора, и
    `{"a": {"b": 1}}` превращался в сломанный фрагмент. Для extractor это удар
    по главному: промпт требует ДОСЛОВНОЙ цитаты, а модель цитировала бы уже
    испорченный текст. Обезвреживается ровно то, на чём падает `render_prompt`
    — имя из заглавных латинских букв и подчёркиваний."""
    assert stages.neutralize_placeholders(исходный) == ожидаемый


# ── П-4: ссылка кандидата сверяется с сырыми записями (РЕШ-61 п. 2) ─────────


async def test_кандидат_с_выдуманной_ссылкой_отбрасывается(settings, fake_provider_factory, caplog):
    """До Т-6 адрес веб-кандидата брал код из ответа Яндекса — структурная
    гарантия. Стадия поставила между источником и `evidence` языковую модель, и
    выдуманная ссылка проходит все машинные проверки: поле непустое, `HttpUrl`
    валиден, `evidence` непуст. Падает она там, где инвариант №1 проверяет
    человек, — в отчёте клиенту."""
    ответ = _model_batch(
        _model_candidate("https://ipc.rssi.ru/lab/catalysis", "Лаборатория катализа ИПХФ РАН"),
        _model_candidate("https://ipc.rssi.ru/lab/catalysis/2021-report.pdf", "Отчёт, которого в выдаче не было"),
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    with caplog.at_level(logging.WARNING, logger="app.pipeline.stages"):
        found = await stages.extract_candidates(SourceType.WEB, "yandex_search", _RAW_WEB, router)

    assert [c.title for c in found] == ["Лаборатория катализа ИПХФ РАН"]
    assert "нет в выдаче источника" in caplog.text


async def test_сверка_ссылок_не_придирается_к_оформлению(settings, fake_provider_factory):
    """Сравнение нормализованное (`normalize_url`): хвостовой слэш, регистр и
    `www.` — оформление, а не выдумка. Иначе сверка выкашивала бы годных
    кандидатов, и её пришлось бы снять целиком."""
    ответ = _model_batch(
        _model_candidate("https://IPC.rssi.ru/lab/catalysis/", "Лаборатория катализа ИПХФ РАН"),
        _model_candidate("https://www.ipc.rssi.ru/news", "Новости института"),
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", _RAW_WEB, router)

    assert len(found) == 2


async def test_сверка_ссылок_молчит_когда_адресов_в_записях_нет(settings, fake_provider_factory):
    """Записи без поля `url` — это источник другой формы, а не выдумка модели.
    Пустое множество адресов выкосило бы всю выдачу: второй способ потерять
    оплаченный ответ вместо защиты от первого."""
    без_адресов = [{"title": "Запись без поля url", "snippet": "Текст страницы."}]
    ответ = _model_batch(_model_candidate("https://example.org/whatever", "Кандидат"))
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", без_адресов, router)

    assert len(found) == 1


# ── РЕШ-62 п. 1: сверка ссылок симметрична ──────────────────────────────────

#: Один и тот же адрес в двух написаниях: так его пишет страница и так его
#: переписывает pydantic, прогоняя через `HttpUrl`.
_КИРИЛЛИЦА_КАК_ЕСТЬ = "https://ipc.rssi.ru/статьи/2024"
_КИРИЛЛИЦА_КОДИРОВАННАЯ = "https://ipc.rssi.ru/%D1%81%D1%82%D0%B0%D1%82%D1%8C%D0%B8/2024"


def _raw_one(url: str) -> list[dict]:
    """Сырая запись источника с заданным адресом."""
    return [{"url": url, "title": "Страница источника", "domain": "ipc.rssi.ru", "snippet": "Текст страницы."}]


@pytest.mark.parametrize(
    ("сырой_адрес", "ссылка_кандидата", "оставлен"),
    [
        # Края, на которых сверка ложно срабатывала: слева нормализовалась
        # сырая строка, справа — адрес ПОСЛЕ `HttpUrl`, а pydantic
        # percent-кодирует не-ASCII путь, переводит домен в punycode и
        # выбрасывает порт по умолчанию.
        pytest.param(_КИРИЛЛИЦА_КАК_ЕСТЬ, _КИРИЛЛИЦА_КАК_ЕСТЬ, True, id="кириллический-путь-дословно"),
        pytest.param(
            "https://наука.рф/новости/катализатор",
            "https://наука.рф/новости/катализатор",
            True,
            id="кириллический-домен-в-зоне-рф",
        ),
        pytest.param("https://ipc.rssi.ru:443/lab/catalysis", "https://ipc.rssi.ru/lab/catalysis", True, id="порт-443"),
        pytest.param("http://ipc.rssi.ru:80/lab", "http://ipc.rssi.ru/lab", True, id="порт-80"),
        pytest.param("наука.рф/новости", "https://наука.рф/новости", True, id="запись-без-схемы"),
        # Кодировка с любой стороны: модель копирует ссылку то как есть, то в
        # том виде, в каком её отдала страница.
        pytest.param(_КИРИЛЛИЦА_КАК_ЕСТЬ, _КИРИЛЛИЦА_КОДИРОВАННАЯ, True, id="кандидат-кодирует-запись-нет"),
        pytest.param(_КИРИЛЛИЦА_КОДИРОВАННАЯ, _КИРИЛЛИЦА_КАК_ЕСТЬ, True, id="запись-кодирует-кандидат-нет"),
        pytest.param(_КИРИЛЛИЦА_КОДИРОВАННАЯ, _КИРИЛЛИЦА_КОДИРОВАННАЯ, True, id="кодировка-с-обеих-сторон"),
        # Оформление, которое сверка не считала выдумкой и раньше.
        pytest.param("https://ipc.rssi.ru/lab", "https://ipc.rssi.ru/lab/", True, id="хвостовой-слэш"),
        pytest.param("https://IPC.rssi.ru/Lab", "https://ipc.rssi.ru/lab", True, id="регистр"),
        pytest.param("https://ipc.rssi.ru/lab", "https://www.ipc.rssi.ru/lab", True, id="www"),
        pytest.param("https://ipc.rssi.ru/lab", "https://ipc.rssi.ru/lab#catalysis", True, id="якорь"),
        pytest.param("https://ipc.rssi.ru/lab", "https://ipc.rssi.ru/lab?utm_source=yandex", True, id="параметры"),
        pytest.param("http://ipc.rssi.ru/lab", "https://ipc.rssi.ru/lab", True, id="http-против-https"),
        # Обратное направление: защита обязана остаться защитой.
        pytest.param("https://ipc.rssi.ru/lab", "https://ipc-ran.ru/lab", False, id="выдуманный-домен"),
        pytest.param("https://ipc.rssi.ru/lab/catalysis", "https://ipc.rssi.ru/lab", False, id="усечённый-путь"),
        pytest.param(
            "https://наука.рф/новости/катализатор",
            "https://наука.рф/новости/катализатор-2021",
            False,
            id="выдуманный-кириллический-путь",
        ),
    ],
)
async def test_сверка_ссылок_симметрична(
    settings, fake_provider_factory, caplog, сырой_адрес, ссылка_кандидата, оставлен
):
    """РЕШ-62 п. 1. Сверка сравнивала разнородные величины: слева — сырую строку
    записи, справа — `str(candidate.url)`, то есть адрес после `HttpUrl`.
    Ссылка, скопированная моделью из записи БУКВА В БУКВУ, не совпадала сама с
    собой: `наука.рф` → `xn--80aa3ak5a.xn--p1ai`, `/статьи` → `/%D1%81%D1%82…`.
    Годный кандидат отбрасывался как выдуманный, лог утверждал, что «ссылки нет
    в выдаче источника», а `problems` молчал, пока список кандидатов не пуст, —
    тихая потеря части оплаченной выдачи, которую откат не страхует.

    Вторая половина таблицы важна не меньше первой: симметризация не имеет
    права снять защиту. Выдуманный домен, усечённый путь и подмена
    кириллического слага обязаны отбрасываться по-прежнему.
    """
    ответ = _model_batch(_model_candidate(ссылка_кандидата, "Лаборатория катализа ИПХФ РАН"))
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    with caplog.at_level(logging.WARNING, logger="app.pipeline.stages"):
        found = await stages.extract_candidates(SourceType.WEB, "yandex_search", _raw_one(сырой_адрес), router)

    assert [c.title for c in found] == (["Лаборатория катализа ИПХФ РАН"] if оставлен else [])
    assert ("нет в выдаче источника" in caplog.text) is not оставлен


# ── П-2: трата и пробел стадии extractor видны в журнале стадий ─────────────

_PLAN_WEB_JSON = json.dumps(
    {"queries": [{"text": "катализатор гидроочистки", "source_type": "web", "lang": "ru", "rationale": "тест"}]},
    ensure_ascii=False,
)
_SCORE_ONE_JSON = json.dumps(
    {"scores": [{"index": 0, "relevance": 8, "maturity": 6, "accessibility": 7, "freshness": 6, "completeness": 7}]}
)
_CARD_ONE_JSON = json.dumps(
    {
        "title": "Катализатор гидроочистки ИПХФ РАН",
        "essence": "Разработка лаборатории катализа.",
        "maturity_stage": "pilot",
        "evidence": [{"claim": "Разработан катализатор гидроочистки", "url": "https://ipc.rssi.ru/lab/catalysis"}],
        "applicability": "Закрывает задачу продления цикла.",
        "next_step": "Запросить протокол испытаний.",
        "candidate_urls": ["https://ipc.rssi.ru/lab/catalysis"],
    },
    ensure_ascii=False,
)
_VERIFY_OK_JSON = json.dumps(
    {"needs_rework": False, "confidence": "medium", "confidence_note": "подтверждения найдены"}, ensure_ascii=False
)
_SUMMARY_ONE_JSON = json.dumps(
    {"comparison_rows": [], "recommendation": "Рассмотреть вариант №1.", "caveats": []}, ensure_ascii=False
)


async def test_журнал_стадии_поиска_несёт_трату_и_пробел_extractor(settings, brief, fake_provider_factory):
    """П-2 и Н-1: строка `search` в `pipeline_logs` создавалась без `provider`
    и `cost_rub`, хотя поля есть и все прочие стадии их заполняют. До Т-6 поиск
    не звал модель ни разу — пробел был безобиден по построению; задача сделала
    его значимым. Здесь стадия ещё и вернула пусто: это обязано стать непустым
    `error`, иначе прогон снаружи выглядит безупречным."""
    from app.pipeline.persistence import StageRecord

    responses = [
        _PLAN_WEB_JSON,
        _EXTRACT_EMPTY,
        _SCORE_ONE_JSON,
        _CARD_ONE_JSON,
        _VERIFY_OK_JSON,
        _SUMMARY_ONE_JSON,
    ]
    router, _ = _extractor_router(settings, fake_provider_factory, responses)
    stage_log: list[StageRecord] = []
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[_FakeWebAdapter(client)])
        # Журналы плана и кандидатов обязательны (РЕШ-89): тесту нужен только
        # журнал стадий, но необязательная ветка переживала бы мутацию молча.
        await pipeline._run_stages(brief, 1, stage_log, [], [])

    record = next(item for item in stage_log if item.stage == "search")
    assert record.provider == "gigachat", "платный вызов стадии extractor не отражён в журнале"
    assert record.cost_rub > 0
    assert record.error and "ноль кандидатов" in record.error


async def test_журнал_стадии_поиска_несёт_фактическую_цену_а_не_любое_число(settings, brief, fake_provider_factory):
    """РЕШ-62 п. 2: закрепить П-2 значением, а не признаком «больше нуля».

    Заглушка провайдера берёт 0.01 ₽ за вызов, а стадия поиска зовёт модель
    ровно один раз — extractor поверх единственного веб-источника. Значит в
    записи `search` обязано лежать 0.01 ₽: не ноль (поля journal не заполнены
    вовсе) и не 0.02 ₽ — столько роутер потратил К ЭТОМУ МОМЕНТУ вместе с
    планировщиком, и подстановка накопленного итога вместо разницы снимков
    выглядела бы в журнале так же правдоподобно. Цена стадии — доля прогона:
    сверка с полным расходом роутера показывает, что это именно доля.
    """
    from app.pipeline.persistence import StageRecord

    responses = [
        _PLAN_WEB_JSON,
        _EXTRACT_EMPTY,
        _SCORE_ONE_JSON,
        _CARD_ONE_JSON,
        _VERIFY_OK_JSON,
        _SUMMARY_ONE_JSON,
    ]
    router, provider = _extractor_router(settings, fake_provider_factory, responses)
    stage_log: list[StageRecord] = []
    async with httpx.AsyncClient() as client:
        pipeline = ScoutPipeline(settings=settings, router=router, adapters=[_FakeWebAdapter(client)])
        # Журналы плана и кандидатов обязательны (РЕШ-89): тесту нужен только
        # журнал стадий, но необязательная ветка переживала бы мутацию молча.
        await pipeline._run_stages(brief, 1, stage_log, [], [])

    record = next(item for item in stage_log if item.stage == "search")
    цена_вызова = 0.01
    assert record.provider == "gigachat"
    assert record.cost_rub == pytest.approx(цена_вызова), "в журнале не цена стадии, а другое число"
    assert router.cost.spent_rub == pytest.approx(цена_вызова * len(provider.calls))
    assert record.cost_rub < router.cost.spent_rub, "цена стадии подменена накопленным итогом прогона"


@respx.mock
async def test_сырые_записи_яндекса_из_того_же_ответа_без_второго_платного_запроса():
    """Критерий 2 Т-6 и риск Р-8: вход стадии extractor не стоит ни одного
    дополнительного запроса. Одна формулировка — один POST searchAsync и один
    шаг счётчика `queries_used`, при этом сырые записи получены.

    Тест живёт здесь, а не в `tests/test_search_yandex.py`: файл адаптерных
    тестов в границы задачи Т-6 не входит, а проверяемое свойство — свойство
    самой стадии.
    """
    from app.search.yandex_search import OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<yandexsearch><response><results><grouping><group><doc>"
        "<url>https://ipc.rssi.ru/lab/catalysis</url><domain>ipc.rssi.ru</domain>"
        "<title>Лаборатория <hlword>катализа</hlword></title><modtime>20240115T103000</modtime>"
        "<passages><passage>Разработан катализатор гидроочистки.</passage></passages>"
        "</doc></group></grouping></results></response></yandexsearch>"
    )
    started = respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    polled = respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"done": True, "response": {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}},
        )
    )

    async with httpx.AsyncClient() as client:
        adapter = YandexSearchAdapter(
            client, api_key="test-key", folder_id="b1gtest", poll_interval_seconds=0.0, max_wait_seconds=5.0
        )
        candidates, raw = await adapter.search_raw(
            SearchQuery(text="катализатор", source_type=SourceType.WEB, lang="ru", rationale="тест"), limit=10
        )

    assert adapter.needs_extraction is True
    assert started.call_count == 1, "сырые записи взяты вторым платным запросом"
    assert adapter.queries_used == 1
    assert polled.call_count == 1
    assert len(candidates) == 1
    assert raw == [
        {
            "url": "https://ipc.rssi.ru/lab/catalysis",
            "title": "Лаборатория катализа",
            "domain": "ipc.rssi.ru",
            "modtime": "20240115T103000",
            "snippet": "Разработан катализатор гидроочистки.",
        }
    ]


# ── П-5: границы платного входа стадии (M14, M15, M16) ──────────────────────
#
# Три из пяти выживших мутаций аудита — свойства СОДЕРЖАНИЯ сырых записей, а
# оно определяет, сколько платит модель и что она видит. В прежней фикстуре был
# один документ с адресом и сниппетом в 36 знаков при пороге 700: границу было
# нечем нащупать, и мутации «игнорировать limit», «не резать сниппет», «не
# отсеивать записи без адреса» не роняли ни одного теста.


def _doc_xml(url: str | None, title: str, snippet: str) -> str:
    """Узел `<doc>` выдачи Яндекса; `url=None` — запись без адреса."""
    адрес = f"<url>{url}</url>" if url else ""
    return (
        f"<doc>{адрес}<domain>ipc.rssi.ru</domain><title>{title}</title>"
        f"<modtime>20240115T103000</modtime><passages><passage>{snippet}</passage></passages></doc>"
    )


@respx.mock
async def test_сырые_записи_ограничены_по_числу_по_адресу_и_по_длине_сниппета():
    """M15, M14 и M16: `limit` — первая граница размера платного входа,
    `MAX_SNIPPET` — вторая, а запись без адреса не имеет смысла отдавать модели
    вовсе (кандидат без ссылки всё равно отбрасывается, правило 1 промпта).

    Выдача из четырёх документов при `limit=2`: первый с гигантским сниппетом,
    второй без адреса, третий и четвёртый обычные. В записи обязаны попасть
    ровно первый и третий, а сниппет первого — ровно `MAX_SNIPPET` знаков.
    """
    from app.search.yandex_search import MAX_SNIPPET, OPERATION_URL, SEARCH_ASYNC_URL, YandexSearchAdapter

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<yandexsearch><response><results><grouping><group>"
        + _doc_xml("https://ipc.rssi.ru/1", "Первая", "А" * 1000)
        + _doc_xml(None, "Без адреса", "Навигация сайта")
        + _doc_xml("https://ipc.rssi.ru/2", "Вторая", "Короткий фрагмент")
        + _doc_xml("https://ipc.rssi.ru/3", "Третья", "Ещё фрагмент")
        + "</group></grouping></results></response></yandexsearch>"
    )
    respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(200, json={"id": "op-1"}))
    respx.get(url__startswith=OPERATION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"done": True, "response": {"rawData": base64.b64encode(xml.encode("utf-8")).decode("ascii")}},
        )
    )

    async with httpx.AsyncClient() as client:
        adapter = YandexSearchAdapter(
            client, api_key="test-key", folder_id="b1gtest", poll_interval_seconds=0.0, max_wait_seconds=5.0
        )
        _, raw = await adapter.search_raw(
            SearchQuery(text="катализатор", source_type=SourceType.WEB, lang="ru", rationale="тест"), limit=2
        )

    assert [record["url"] for record in raw] == ["https://ipc.rssi.ru/1", "https://ipc.rssi.ru/2"], (
        "нарушена граница: либо запись без адреса попала в платный вход, либо не соблюдён limit"
    )
    assert len(raw[0]["snippet"]) == MAX_SNIPPET, "сниппет не обрезан — вторая граница размера платного входа"


# ── Т-26: лимит стадии скоринга и учёт повторов по лимиту ───────────────────
#
# На воротах Э2-А часть батчей скоринга бралась со второй попытки: роутер видел
# обрыв JSON, удваивал лимит и переспрашивал заново — со всем входом. Повтор
# стал штатным путём вместо страховки, и прогон вырос до 25–30 минут. Здесь
# закрепляются обе меры: щедрый лимит и строка учёта, по которой видно, стало
# ли повторов меньше.


def _recording_provider(fake_provider_factory, name: str, responses: list[str]):
    """Провайдер-заглушка, запоминающий запрошенный лимит токенов."""

    class _Recording(fake_provider_factory):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.limits: list[int] = []

        async def complete(self, messages, *, model=None, temperature=0.2, max_tokens=4096, json_mode=False):
            self.limits.append(max_tokens)
            return await super().complete(
                messages, model=model, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
            )

    return _Recording(name, responses)


def _scoring_line(caplog) -> str:
    """Строка учёта стадии скоринга из журнала; её отсутствие — сразу отказ."""
    line = next((r.getMessage() for r in caplog.records if "скоринг: батчей" in r.getMessage()), None)
    assert line is not None, "строка учёта скоринга не напечатана — измерять повторы нечем"
    return line


async def test_скоринг_просит_16000_а_планировщик_остаётся_на_8000(settings, fake_provider_factory, brief):
    """Т-26: лимит поднят ровно у скоринга.

    В `stages.py` четыре вызова с лимитом 8000 (интейк, уточнение, планировщик,
    скоринг) — ровно та картина, в которой правка расползается по соседям. Тест
    держит обе стороны: 16000 у скоринга и нетронутые 8000 у планировщика.
    """
    scorer = _recording_provider(fake_provider_factory, "deepseek", [_SCORE_JSON])
    planner = _recording_provider(fake_provider_factory, "gigachat", [_PLAN_WEB_JSON])

    await stages.score_candidates(LLMRouter(settings, providers={"deepseek": scorer}), brief, [_candidate(1)])
    await stages.plan_search(LLMRouter(settings, providers={"gigachat": planner}), brief)

    assert scorer.limits == [16000], "скоринг снова уходит на прежнем лимите — повтор остаётся штатным путём"
    assert planner.limits == [8000], "правка лимита расползлась на соседний вызов"


async def test_строка_учёта_скоринга_печатается_и_при_нуле_повторов(settings, fake_provider_factory, brief, caplog):
    """Строка печатается всегда. Молчание при нуле сделало бы «повторов не
    стало» неотличимым от «код до счётчика не дошёл» — тот же урок, ради
    которого всегда печатается строка калибровки."""
    provider = fake_provider_factory("deepseek", [_SCORE_JSON])
    router = LLMRouter(settings, providers={"deepseek": provider})

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        await stages.score_candidates(router, brief, [_candidate(1)])

    line = _scoring_line(caplog)
    assert "батчей 1" in line
    assert "повторов по лимиту 0" in line
    assert "время стадии" in line


async def test_повтор_по_нехватке_лимита_попадает_в_строку_учёта(settings, fake_provider_factory, brief, caplog):
    """Счётчик считает именно повтор роутера по лимиту, а не любую вторую
    попытку: путь проходится настоящим роутером — первый ответ обрезан на
    середине, второй целый."""
    обрезанный = '{"scores": [{"index": 0, "relevance": 8, "maturity": 6,'
    provider = _recording_provider(fake_provider_factory, "deepseek", [обрезанный, _SCORE_JSON])
    router = LLMRouter(settings, providers={"deepseek": provider})

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        scored = await stages.score_candidates(router, brief, [_candidate(1)])

    assert [item.relevance for item in scored] == [8]
    assert provider.limits == [16000, 32000], "роутер переспросил не с удвоенным лимитом — путь повтора не тот"
    assert "повторов по лимиту 1" in _scoring_line(caplog)


async def test_счётчик_повторов_не_приписывает_скорингу_чужие_стадии(settings, fake_provider_factory, brief, caplog):
    """Стадии идут через один и тот же роутер: повтор extractor'а, случившийся
    во время скоринга, не должен попасть в его счёт — иначе измеритель показывает
    чужую работу."""

    class _Noisy(fake_provider_factory):  # type: ignore[misc, valid-type]
        async def complete(self, messages, *, model=None, temperature=0.2, max_tokens=4096, json_mode=False):
            logging.getLogger("app.llm.router").warning(
                "стадия %s: ответ обрезан по лимиту токенов, переспрашиваю с лимитом %s", "extractor", 14000
            )
            return await super().complete(
                messages, model=model, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
            )

    router = LLMRouter(settings, providers={"deepseek": _Noisy("deepseek", [_SCORE_JSON])})

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        await stages.score_candidates(router, brief, [_candidate(1)])

    assert "повторов по лимиту 0" in _scoring_line(caplog)
