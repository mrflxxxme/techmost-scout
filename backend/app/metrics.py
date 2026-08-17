"""Числовые измерители выдачи по сохранённым оценкам (РЕШ-84, РЕШ-86).

Зачем модуль. До Т-20 оценки скоринга жили ровно в одной строке журнала
калибровки и умирали вместе с прогоном. Числовой критерий приёмки подэтапа
(РЕШ-86 — «на том же сохранённом плане максимум `relevance` вырос с 4 до ≥6,
число кандидатов не ниже порога выросло с 3») подтверждать было нечем: сравнить
два прогона можно только по числам, которые пережили оба.

Здесь только чистые функции над **сохранёнными** оценками — теми самыми
словарями, что лежат в `candidates.score`. Ни базы, ни сети: измеритель обязан
считаться одинаково и в тесте на выдуманном наборе, и в разборе прогона запросом
к БД. Формат этих словарей задаёт `score_payload` — она же ставит признак
нейтральной оценки, поэтому писатель и читатель формата не могут разойтись
молча (тот же урок, что и с пометкой `NEUTRAL_SCORE_NOTE` в стадии скоринга).

Главное правило всех измерителей: **считаем только по оценкам модели**.
Нейтральные пятёрки — не измерение, а след отказа батча (`NeutralScoredCandidate`
в `app.pipeline.stages`), и один такой отказ поднял бы «долю `relevance` ≥ 4» на
десять кандидатов, ничего при этом не измерив. Та же логика уже действует в
журнале калибровки (`_model_relevance`, `stages.py`).

«Нет ни одной оценки модели» возвращается как `None`, а не как ноль: ноль здесь
читался бы как «измерили и вышло плохо», хотя измерять было нечего. Эта разница
стоила проекту разбора на этапе Э1-2.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.schemas import ScoredCandidate

#: Порог предметного соответствия, по которому считается доля годных кандидатов
#: (РЕШ-74, подобран на калибровочном прогоне Т-10). Здесь он не переоткрывается:
#: измеритель обязан считать по тому же числу, по которому работает отсечка
#: отбора, иначе прогоны разных дней сравнивать нечем.
RELEVANCE_MIN = 4

#: Ключ признака «оценку поставил код, а не модель» в сохранённом `score`.
NEUTRAL_KEY = "neutral"


def score_payload(item: ScoredCandidate, *, neutral: bool) -> dict[str, Any]:
    """Оценки одного кандидата в том виде, в каком они ложатся в `candidates.score`.

    `neutral` приходит снаружи, а не вычисляется здесь: признак нейтральной
    оценки знает стадия скоринга (тип `NeutralScoredCandidate`), и вторая копия
    этого знания разошлась бы с первой — ровно так однажды разошлись
    производитель и потребитель текстовой пометки.

    `total` кладётся рядом с пятью оценками, потому что это единственное, что
    считается по ним не сложением: восстанавливать веса из отчёта позже пришлось
    бы вручную.
    """
    return {
        "relevance": item.relevance,
        "maturity": item.maturity,
        "accessibility": item.accessibility,
        "freshness": item.freshness,
        "completeness": item.completeness,
        "total": round(item.total, 4),
        "notes": item.notes,
        NEUTRAL_KEY: bool(neutral),
    }


def model_relevance(scores: Iterable[Mapping[str, Any]]) -> list[int]:
    """Оценки `relevance`, которые действительно поставила модель.

    Нейтральные исключаются — та же логика, что в `_model_relevance` журнала
    калибровки. Записи без `relevance` (строка от несовместимой версии схемы,
    правка руками) в счёт не идут: выдуманный ноль испортил бы и минимум, и долю.
    """
    values: list[int] = []
    for score in scores:
        if score.get(NEUTRAL_KEY):
            continue
        relevance = score.get("relevance")
        if isinstance(relevance, int) and not isinstance(relevance, bool):
            values.append(relevance)
    return values


def max_relevance(scores: Iterable[Mapping[str, Any]]) -> int | None:
    """Максимум `relevance` по оценкам модели; `None` — оценок модели нет.

    Это и есть число из РЕШ-86: «максимум вырос с 4 до ≥6». Совпадает с третьим
    числом в `min/медиана/max` строки калибровки на тех же данных — по
    построению, обе величины считаются по одной и той же выборке.
    """
    values = model_relevance(scores)
    return max(values) if values else None


def count_at_least_relevance(scores: Iterable[Mapping[str, Any]], *, minimum: int = RELEVANCE_MIN) -> int:
    """Сколько кандидатов набрали `relevance` не ниже порога (по оценкам модели).

    Второе число РЕШ-86 («число кандидатов не ниже порога выросло с 3»).
    Сравнение нестрогое: порог 4 означает, что четвёрка проходит.
    """
    return sum(1 for value in model_relevance(scores) if value >= minimum)


def share_at_least_relevance(scores: Iterable[Mapping[str, Any]], *, minimum: int = RELEVANCE_MIN) -> float | None:
    """Доля кандидатов с `relevance` не ниже порога; `None` — оценок модели нет.

    Доля считается от числа оценок модели, а не от всех сохранённых строк:
    иначе отказавший батч разбавлял бы знаменатель и занижал долю, ничего при
    этом не измерив.
    """
    values = model_relevance(scores)
    if not values:
        return None
    return sum(1 for value in values if value >= minimum) / len(values)


def silent_sources_share(asked: Iterable[str], hits: Mapping[str, int]) -> float | None:
    """Доля опрошенных источников, не давших ни одного кандидата; `None` — не
    спрашивали никого.

    `asked` — **ключи `pipeline_logs.sources_hit`, то есть метки контура**
    (`SearchAdapter.contour_label`), а не имена адаптеров: единственный
    сохраняемый источник данных для этого измерителя — журнал стадий, а он
    заполняется `SearchOutcome.hits_by_provider`, ключованным метками (РЕШ-31).
    Один адаптер отдаёт разные метки разным формулировкам, поэтому смешать
    вокабуляры — получить долю 1,0 на исправном прогоне. `hits` — сколько
    кандидатов принесла каждая метка.

    Список опрошенных передаётся отдельно намеренно: источник с нулевой отдачей
    в счётчике попаданий отсутствует вовсе, и считать долю по одному словарю
    значило бы всегда получать ноль — измеритель, который не может показать
    проблему, ради которой заведён.
    """
    names = list(dict.fromkeys(asked))
    if not names:
        return None
    return sum(1 for name in names if hits.get(name, 0) <= 0) / len(names)
