"""Стадия 4 — скоринг и решение о втором проходе поиска.

Вынесено из `stages.py` (РЕШ-94) без изменения поведения — только перенос
тела функций и нужных импортов. Из раздела «4а» исходного файла сюда попали
`max_model_relevance` и `needs_second_pass`: они судят по уже выставленным
оценкам, а не строят план — построение плана (`replan_search` и соседи)
осталось в `planning.py`.
"""

from __future__ import annotations

import logging
import statistics
import time

from pydantic import BaseModel, Field

from app.llm import CostLimitExceededError, LLMRouter, Message
from app.pipeline.prompting import candidates_listing, load_prompt, owner_answers_block
from app.schemas import Brief, Candidate, ScoredCandidate

#: Имя логгера намеренно не `__name__`: до дробления (РЕШ-94) весь этот код
#: жил в одном модуле `app.pipeline.stages`, и тесты фильтруют `caplog` по
#: этой строке (`caplog.at_level(..., logger="app.pipeline.stages")`). Смена
#: имени логгера при переносе — наблюдаемая правка поведения, а не мелочь.
log = logging.getLogger("app.pipeline.stages")


class CandidateScore(BaseModel):
    """Оценка одного кандидата в ответе модели.

    Модель ссылается на кандидата номером, а не пересказывает его поля: иначе
    ответ на батч из десяти кандидатов не влезает в лимит токенов и JSON
    обрывается на середине. Сопоставление номеров с кандидатами делает код.
    """

    index: int = Field(ge=0)
    relevance: int = Field(ge=0, le=10)
    maturity: int = Field(ge=0, le=10)
    accessibility: int = Field(ge=0, le=10)
    freshness: int = Field(ge=0, le=10)
    completeness: int = Field(ge=0, le=10)
    notes: str = ""


class ScoreBatch(BaseModel):
    """Обёртка ответа скоринга: модели проще вернуть объект, чем голый массив."""

    scores: list[CandidateScore]


# ── 4. Скоринг ──────────────────────────────────────────────────────────────
#: Подстрока предупреждения роутера о повторе из-за нехватки лимита
#: (`complete_json`, `app/llm/router.py`). Стадия зовёт роутер и о его
#: внутреннем повторе иначе не узнаёт вовсе: наружу возвращается обычный
#: разобранный ответ, хотя вход оплачен второй раз целиком.
_LIMIT_RETRY_MARKER = "ответ обрезан по лимиту токенов"


class _TokenRetryCounter(logging.Handler):
    """Счётчик повторов роутера по нехватке лимита за время одной стадии.

    Слушает журнал роутера, а не подменяет его вызов: контракт `llm/` — общий,
    и правка ради измерителя одной стадии стоила бы дороже, чем даёт. Связь с
    формулировкой роутера закреплена тестом, который проходит настоящий путь
    повтора (`tests/test_stages.py`), — смена текста уронит его, а не спрячется
    вечным нулём.

    Записи чужих стадий не в счёт: имя стадии — первый аргумент записи. Стадии
    делят один роутер, и чужой повтор в своём счёте — это измеритель, который
    показывает не свою работу.
    """

    def __init__(self, stage: str) -> None:
        super().__init__(level=logging.WARNING)
        self._stage = stage
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        args = record.args if isinstance(record.args, tuple) else ()
        if args[:1] == (self._stage,) and _LIMIT_RETRY_MARKER in str(record.msg):
            self.count += 1


async def score_candidates(
    router: LLMRouter,
    brief: Brief,
    candidates: list[Candidate],
    *,
    batch_size: int = 10,
    citations: dict[str, int] | None = None,
) -> list[ScoredCandidate]:
    """Оценить кандидатов по пяти критериям.

    Батчами, потому что дешёвая модель на длинном списке начинает ставить всем
    одинаковые оценки и терять кандидатов.

    `citations` — цитируемость с обогащения по DOI; см. `candidates_listing`.
    """

    scored: list[ScoredCandidate] = []
    batches = 0
    started_at = time.monotonic()
    retries = _TokenRetryCounter("scorer")
    router_log = logging.getLogger(LLMRouter.__module__)
    router_log.addHandler(retries)
    try:
        for start in range(0, len(candidates), batch_size):
            batches += 1
            chunk = candidates[start : start + batch_size]
            listing = candidates_listing(chunk, citations)
            try:
                batch = await router.complete_json(
                    "scorer",
                    [
                        Message(role="system", content=load_prompt("scorer")),
                        Message(
                            role="user",
                            content=(
                                f"Задача бизнеса:\n{brief.task_summary}\n"
                                f"Отрасль: {brief.industry}\n"
                                f"Ограничения: {'; '.join(brief.constraints) or 'не заданы'}\n\n"
                                f"{owner_answers_block(brief)}"
                                f"Кандидаты ({len(chunk)} шт.):\n{listing}\n\n"
                                f"Верни ровно {len(chunk)} оценок, ссылаясь на кандидатов "
                                f"номерами от 0 до {len(chunk) - 1}."
                            ),
                        ),
                    ],
                    ScoreBatch,
                    # Т-13: на 4000 рассуждающая модель не доходила до ответа —
                    # лимит целиком уходил в reasoning_content, `content`
                    # приходил пустым, и КАЖДЫЙ батч скатывался в нейтральные
                    # оценки, то есть ранжирование умирало молча, оплаченное
                    # полностью. Расчёт. Худший ответ на батч из 10 кандидатов
                    # (пять оценок и `notes` до 160 знаков на каждого) — 3 320
                    # знаков, ≈1 660 токенов при консервативных двух знаках на
                    # токен. Остальное — запас на рассуждение: стадия взвешивает
                    # 10 кандидатов по пяти якорным шкалам, 50 суждений за
                    # вызов, самая тяжёлая по рассуждению стадия конвейера.
                    # Т-26: 8000 оказались впритык. На воротах Э2-А часть батчей
                    # бралась со ВТОРОЙ попытки — роутер видел обрыв JSON,
                    # удваивал лимит и переспрашивал заново, со всем входом.
                    # Повтор перестал быть страховкой и стал штатным путём, а
                    # прогон вырос до 25–30 минут; второй проход (РЕШ-81) удвоил
                    # бы и это. 16000 — тот самый удвоенный лимит, на котором
                    # ответ доходил, только запрошенный сразу.
                    # Щедрость здесь бесплатна: `max_tokens` — потолок, а не
                    # счёт, несгенерированные токены не оплачиваются. Нехватка,
                    # наоборот, стоит повторного запроса целиком со всем входом.
                    max_tokens=16000,
                )
            except CostLimitExceededError:
                # Стоп-кран (РЕШ-45): исчерпанный лимит — не отказ модели, а
                # команда «стоп тратить». Нейтральные оценки платили бы дальше на
                # следующих батчах, тихо продолжая расходовать при исчерпанном
                # лимите — это и есть скрытая деградация, которую стоп-кран
                # обязан пресекать.
                raise
            except Exception as exc:
                log.warning("скоринг батча не удался (%s), беру нейтральные оценки", exc)
                scored.extend(_neutral_scores(chunk))
                continue

            by_index = {item.index: item for item in batch.scores}
            for position, candidate in enumerate(chunk):
                score = by_index.get(position)
                if score is None:
                    # Модель потеряла кандидата — не выбрасываем его, а ставим
                    # нейтральную оценку: пусть решает отбор, а не пропуск.
                    log.info("скоринг пропустил кандидата %s, ставлю нейтральную оценку", candidate.title[:60])
                    scored.extend(_neutral_scores([candidate]))
                    continue
                scored.append(
                    ScoredCandidate(
                        candidate=candidate,
                        relevance=score.relevance,
                        maturity=score.maturity,
                        accessibility=score.accessibility,
                        freshness=score.freshness,
                        completeness=score.completeness,
                        notes=score.notes,
                    )
                )
    finally:
        router_log.removeHandler(retries)
        # Печатается ВСЕГДА: и при нуле повторов, и при пустом входе, и когда
        # стадию оборвал стоп-кран. Молчание сделало бы «повторов не стало»
        # неотличимым от «код до счётчика не дошёл» — тот же урок, ради
        # которого всегда печатается строка калибровки. Время стадии здесь же:
        # длительность прогона — то, ради чего лимит и поднимали.
        log.info(
            "скоринг: батчей %s, повторов по лимиту %s, время стадии %.1f с",
            batches,
            retries.count,
            time.monotonic() - started_at,
        )
    return scored


#: Текст пометки в `notes` для оценки, которую поставил код, а не модель. Виден
#: человеку в разборе; отсечка узнаёт таких кандидатов по типу
#: `NeutralScoredCandidate`, а по этой строке — только запасным путём.
NEUTRAL_SCORE_NOTE = "оценка не получена, значение нейтральное"

#: Пороги, для которых журнал калибровки печатает «сколько отсеклось бы».
#: Рабочее значение подбирается по этим числам на прогоне (Т-10).
_CALIBRATION_THRESHOLDS = (4, 5, 6, 7)


class NeutralScoredCandidate(ScoredCandidate):
    """Кандидат с оценками, которые проставил код после отказа скоринга.

    Пометка структурная, а не текстовая. Сравнение `notes` со строкой держалось
    на том, что производитель пометки и её потребитель никогда не разойдутся:
    допиши `_neutral_scores` к пометке номер батча — и отсечка перестала бы
    узнавать нейтральных кандидатов, выкосила бы их молча, а журнал показал бы
    «нейтральных оценок 0». Тип таким образом не ломается.

    Подкласс живёт здесь, а не в `schemas.py`: это деталь стадии скоринга, а не
    контракт между стадиями — снаружи он остаётся обычным `ScoredCandidate`.
    """


def _neutral_scores(candidates: list[Candidate]) -> list[NeutralScoredCandidate]:
    """Средние оценки для кандидатов, которых модель не осилила оценить."""
    return [
        NeutralScoredCandidate(
            candidate=candidate,
            relevance=5,
            maturity=5,
            accessibility=5,
            freshness=5,
            completeness=5,
            notes=NEUTRAL_SCORE_NOTE,
        )
        for candidate in candidates
    ]


def has_neutral_score(item: ScoredCandidate) -> bool:
    """Оценки проставлены кодом после отказа скоринга, а не получены от модели.

    Основной признак — тип. Сравнение с `NEUTRAL_SCORE_NOTE` оставлено запасным
    путём: `ScoredCandidate` собирают не только в `_neutral_scores`, и кандидат,
    восстановленный из обычной модели с той же пометкой (`model_validate`,
    фикстура теста), обязан остаться защищённым от отсечки.

    Имя публичное, потому что признак спрашивают снаружи модуля: `orchestrator`
    ставит по нему пометку нейтральной оценки в таблице `candidates` (РЕШ-84).
    До Т-21 он обращался к приватному `_has_neutral_score` — единственное во
    всём `backend/app` обращение к приватному имени чужого модуля, то есть
    контракт, о котором знали только двое.
    """
    return isinstance(item, NeutralScoredCandidate) or item.notes.strip() == NEUTRAL_SCORE_NOTE


def _has_neutral_score(item: ScoredCandidate) -> bool:
    """Прежнее приватное имя — тонкая обёртка над `has_neutral_score`.

    Оставлено ради вызовов внутри модуля, которых много; поведение целиком
    задаётся публичной функцией, второй копии знания здесь нет.
    """
    return has_neutral_score(item)


def _cut_by_relevance(scored: list[ScoredCandidate], threshold: int) -> int:
    """Сколько кандидатов отсечёт порог. Нейтральные оценки не в счёт: их
    отсечка не трогает ни при каком пороге."""
    return sum(1 for item in scored if not _has_neutral_score(item) and item.relevance < threshold)


def _model_relevance(scored: list[ScoredCandidate]) -> list[int]:
    """Оценки, которые действительно поставила модель, — без нейтральных."""
    return [item.relevance for item in scored if not _has_neutral_score(item)]


def log_relevance_calibration(
    scored: list[ScoredCandidate], picked: list[ScoredCandidate], *, relevance_min: int, limit: int
) -> None:
    """Напечатать распределение `relevance` и прикидку отсечки по порогам.

    Печатается **всегда**: и при пороге 0, и когда не отсечён ни один кандидат.
    Это единственный источник данных для подбора порога (Т-10) — оценки нигде
    не сохраняются, таблица `candidates` не пишется, в отчёты оценки не
    попадают. Молчание при нулевой отсечке сделало бы ноль неотличимым от
    «отсечка не сработала»: ровно этот неинтерпретируемый ноль стоил разбора
    на этапе Э1-2.

    Строка одна, но популяции в ней разделены — иначе по ней нельзя принять
    решение:

    * `min/медиана/max` считаются **только по оценкам модели**, как и колонка
      «отсеклось бы». Нейтральные пятёрки — не измерение, а след отказа батча:
      добавленный к десяти оценкам модели отказавший батч из десяти нейтральных
      сдвигал медиану с 2 на 4.5 — ровно внутри диапазона 4–7, из которого Т-10
      выбирает порог, и при неизменной колонке «отсеклось бы».
    * `отсеклось бы по всему списку` — цена порога для всего скоринга.
    * `среди отобранных ... ниже порога` — цена порога для тех, кто реально
      доходит до карточек: до них добирается только top-`limit`. Без этой
      колонки «отсечено 30 из 42» при нетронутом составе top-12 и «отсечено 3
      из 15» при замене четверти выдачи выглядят одинаково.
    """
    values = _model_relevance(scored)
    spread = f"{min(values)}/{statistics.median(values):g}/{max(values)}" if values else "нет оценок"
    would_cut = ", ".join(f"при {t}: {_cut_by_relevance(scored, t)}" for t in _CALIBRATION_THRESHOLDS)
    picked_below = ", ".join(f"{t}: {_cut_by_relevance(picked, t)}" for t in _CALIBRATION_THRESHOLDS)
    log.info(
        "калибровка relevance: кандидатов %s, оценок модели %s, min/медиана/max по оценкам модели %s, "
        "порог %s, отсечено %s; отсеклось бы по всему списку %s; среди отобранных %s (лимит %s) ниже порога %s; "
        "нейтральных оценок %s (не отсекаются никогда)",
        len(scored),
        len(values),
        spread,
        relevance_min,
        _cut_by_relevance(scored, relevance_min),
        would_cut,
        len(picked),
        limit,
        picked_below,
        sum(1 for item in scored if _has_neutral_score(item)),
    )


# ── 4а. Условный второй проход поиска (РЕШ-81) ──────────────────────────────
#: Дословные строки решения о втором проходе. Печатается ВСЕГДА ровно одна из
#: двух — в том числе при выключенном пороге. Молчание сделало бы «проход не
#: сработал» неотличимым от «код до проверки не дошёл»: тот же урок, ради
#: которого всегда печатается строка калибровки `log_relevance_calibration`.
REPLAN_ENTER_LOG = "второй проход: максимум relevance %s ниже порога %s — иду повторно"
REPLAN_SKIP_LOG = "второй проход не нужен: максимум relevance %s не ниже порога %s"

#: Что встаёт в строку вместо максимума, когда оценок модели нет вовсе (все
#: батчи скоринга отказали). Слово вместо числа — чтобы разбор прогона не читал
#: отсутствие измерения как измеренный ноль.
NO_MODEL_SCORES_LABEL = "нет оценок модели"


def max_model_relevance(scored: list[ScoredCandidate]) -> int | None:
    """Максимум `relevance` по оценкам МОДЕЛИ; `None`, если их нет ни одной.

    Нейтральные оценки в максимум не входят (`_model_relevance`): они равны
    ровно пятёрке, и отказавший батч на слабой выдаче нарисовал бы максимум 5
    там, где модель не поставила выше четырёх, — то есть погасил бы второй
    проход ровно на той задаче, ради которой он затеян (агропром, максимум 4).
    """
    values = _model_relevance(scored)
    return max(values) if values else None


def needs_second_pass(scored: list[ScoredCandidate], *, threshold: int) -> bool:
    """Нужен ли второй проход поиска, и запись об этом в журнал (РЕШ-81).

    Условие входа — максимум `relevance` по оценкам модели **строго ниже**
    порога. Строго, а не «не выше»: порог здесь — граница годной выдачи, и
    выдача, дотянувшая до неё, годной уже считается. При пороге 0 (умолчание,
    проход выключен) условие не выполняется ни при каком максимуме.

    Оценок модели нет вовсе — проход не запускается: платить за второй заход
    можно по измеренному промаху, а не по отсутствию измерения.
    """
    top = max_model_relevance(scored)
    if top is not None and top < threshold:
        log.info(REPLAN_ENTER_LOG, top, threshold)
        return True
    log.info(REPLAN_SKIP_LOG, NO_MODEL_SCORES_LABEL if top is None else top, threshold)
    return False
