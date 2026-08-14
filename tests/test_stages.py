"""Проверки чистых стадий пайплайна: склейка, отбор, отсечка по предметному
соответствию, группировка, честная выдача карточек (Ф-7)."""

from __future__ import annotations

import logging

from app.config import Settings
from app.llm import LLMRouter
from app.pipeline import stages
from app.pipeline.stages import (
    NEUTRAL_SCORE_NOTE,
    candidates_listing,
    deduplicate,
    group_for_cards,
    select_publishable_cards,
    select_top,
)
from app.schemas import Candidate, Evidence, ScoredCandidate, SolutionCard, SourceType

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
