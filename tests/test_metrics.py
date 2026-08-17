"""Числовые измерители выдачи (Т-20, РЕШ-84, РЕШ-86).

Главное утверждение набора: измерители считают **только по оценкам модели**.
Нейтральная оценка — это ровно пятёрка по всем пяти шкалам, которую код ставит
после отказа батча скоринга; один такой отказ на десять кандидатов поднял бы
«долю `relevance` ≥ 4» с нуля до половины, не измерив ничего. Ровно так журнал
калибровки однажды сдвигал медиану с 2 на 4.5 — внутри диапазона, из которого
подбирался порог.

Второе утверждение: максимум измерителя совпадает с максимумом строки
калибровки на одних и тех же данных. Два числа, считающие одно и то же разными
путями, обязаны сходиться — иначе приёмка подэтапа (РЕШ-86) зависит от того,
куда посмотрел проверяющий.

Ни базы, ни сети: измерители — чистые функции над сохранёнными словарями оценок.
"""

from __future__ import annotations

import logging
import re

from app.metrics import (
    RELEVANCE_MIN,
    count_at_least_relevance,
    max_relevance,
    model_relevance,
    score_payload,
    share_at_least_relevance,
    silent_sources_share,
)
from app.pipeline import stages
from app.pipeline.stages import select_top
from app.schemas import Candidate, ScoredCandidate, SourceType


def _candidate(number: int, *, provider: str = "crossref") -> Candidate:
    """Кандидат-заготовка: различается только номером и источником."""
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=f"Кандидат {number}",
        url=f"https://example.org/{number}",
        provider=provider,
    )


def _scored(number: int, *, relevance: int, provider: str = "crossref") -> ScoredCandidate:
    """Оценка модели с заданной релевантностью; прочие шкалы одинаковы."""
    return ScoredCandidate(
        candidate=_candidate(number, provider=provider),
        relevance=relevance,
        maturity=6,
        accessibility=6,
        freshness=6,
        completeness=6,
    )


def _saved(scored: list[ScoredCandidate]) -> list[dict]:
    """Оценки в том виде, в каком их кладёт в БД прогон.

    Пара «`score_payload` + признак от стадии скоринга» здесь ровно та же, что в
    `orchestrator.candidate_records`: набор проверяет сохранённый формат, а не
    свою выдумку о нём.
    """
    return [score_payload(item, neutral=stages._has_neutral_score(item)) for item in scored]


#: Десять оценок модели: три из них (6, 4 и 9) не ниже порога 4.
_MODEL_TEN = [1, 0, 6, 2, 4, 3, 1, 9, 2, 0]


def test_model_relevance_skips_neutral_scores():
    """Нейтральные оценки не попадают в выборку измерителей.

    Признак структурный: он приходит из `score_payload` вместе с оценками, а не
    угадывается по значению — нейтральная пятёрка неотличима от честной пятёрки
    модели по самому числу.
    """
    scored = [_scored(0, relevance=9), *stages._neutral_scores([_candidate(1)])]

    assert model_relevance(_saved(scored)) == [9]


def test_failed_batch_does_not_change_max_and_share():
    """Критерий 2 Т-20: отказавший батч из десяти нейтральных оценок не меняет
    ни максимум, ни долю `relevance ≥ 4`.

    Это и есть цена ошибки: без пометки нейтральных доля выросла бы с 0,3 до
    0,65, а прогон с полностью отказавшим скорингом выглядел бы лучше исправного.
    """
    model = [_scored(number, relevance=value) for number, value in enumerate(_MODEL_TEN)]
    with_failed_batch = [*model, *stages._neutral_scores([_candidate(100 + number) for number in range(10)])]

    assert max_relevance(_saved(with_failed_batch)) == max_relevance(_saved(model)) == 9
    assert share_at_least_relevance(_saved(with_failed_batch)) == share_at_least_relevance(_saved(model)) == 0.3
    assert count_at_least_relevance(_saved(with_failed_batch)) == 3


def test_neutral_batch_does_not_raise_maximum_above_model_scores():
    """Нейтральная пятёрка не имеет права стать максимумом выдачи.

    Это ровно случай агропрома на воротах Э2-А: максимум по всем оценкам модели
    — 4, и один отказавший батч нарисовал бы максимум 5. Числовой критерий
    РЕШ-86 («максимум вырос с 4 до ≥6») тогда измерял бы здоровье скоринга, а не
    качество поиска — и прошёл бы тем легче, чем хуже работает модель.
    """
    model = [_scored(number, relevance=value) for number, value in enumerate([1, 0, 4, 2, 3])]
    with_failed_batch = [*model, *stages._neutral_scores([_candidate(100 + number) for number in range(10)])]

    assert max_relevance(_saved(with_failed_batch)) == 4


def test_share_counts_score_exactly_at_threshold():
    """Оценка ровно 4 считается годной: порог нестрогий (`RELEVANCE_MIN = 4`, РЕШ-74).

    Замок на подмену `>=` на `>`: при строгом сравнении доля стала бы нулевой, а
    вся выдача агропрома с максимумом 4 — «ни одного годного кандидата».
    """
    assert RELEVANCE_MIN == 4
    at_threshold = _saved([_scored(0, relevance=RELEVANCE_MIN)])

    assert share_at_least_relevance(at_threshold) == 1.0
    assert count_at_least_relevance(at_threshold) == 1
    assert share_at_least_relevance(_saved([_scored(0, relevance=RELEVANCE_MIN - 1)])) == 0.0


def test_measures_are_none_when_model_scored_nothing():
    """Отказал весь скоринг — измерять нечего, и это не ноль.

    Ноль читался бы как «измерили, вышло плохо». Та же честность, что и в строке
    калибровки: она в этом случае печатает «нет оценок», а не 5/5/5.
    """
    only_neutral = _saved(stages._neutral_scores([_candidate(number) for number in range(4)]))

    assert max_relevance(only_neutral) is None
    assert share_at_least_relevance(only_neutral) is None
    assert count_at_least_relevance(only_neutral) == 0
    assert max_relevance([]) is None


def test_max_relevance_matches_calibration_line(caplog):
    """Критерий 3 Т-20: максимум измерителя равен максимуму строки калибровки.

    Число берётся не из соседней переменной теста, а разбирается из фактической
    записи журнала — той самой строки, по которой на воротах Э2-А читали
    «максимум по всей выдаче 4». Разойдись эти два пути, приёмка РЕШ-86 зависела
    бы от того, куда посмотрел проверяющий.
    """
    scored = [_scored(number, relevance=value) for number, value in enumerate(_MODEL_TEN)]
    scored.extend(stages._neutral_scores([_candidate(100 + number) for number in range(10)]))

    with caplog.at_level(logging.INFO, logger="app.pipeline.stages"):
        select_top(scored, limit=12, relevance_min=0)

    line = next(record.getMessage() for record in caplog.records if "калибровка relevance" in record.getMessage())
    found = re.search(r"min/медиана/max по оценкам модели (\d+)/[\d.]+/(\d+)", line)
    assert found is not None, f"строка калибровки не разобралась: {line}"

    assert max_relevance(_saved(scored)) == int(found.group(2))


def test_silent_sources_share_counts_sources_that_returned_nothing():
    """Доля источников с нулевой отдачей: спрошены четверо, ответили двое."""
    asked = ["crossref", "cyberleninka", "rospatent", "yandex"]
    hits = {"crossref": 7, "yandex": 3}

    assert silent_sources_share(asked, hits) == 0.5


def test_silent_sources_share_is_none_when_nobody_was_asked():
    """Никого не спрашивали — доли нет. Ноль означал бы «все источники ответили»."""
    assert silent_sources_share([], {}) is None


def test_silent_sources_share_ignores_repeated_names():
    """Источник, спрошенный несколькими формулировками, считается один раз.

    Иначе доля зависела бы от длины плана поиска, а не от отдачи источников.
    """
    assert silent_sources_share(["crossref", "crossref", "rospatent"], {"crossref": 5}) == 0.5
