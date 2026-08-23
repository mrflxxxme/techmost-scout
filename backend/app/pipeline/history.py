"""Накопительный поиск: кандидаты прошлых прогонов той же задачи (РЕШ-163, З-4).

Зачем модуль. Формулировки поиска придумывает модель (РЕШ-109), поэтому два
прогона одной задачи ищут разное: проба З-3 показала пересечение пулов на 6
кандидатах из 72, и обе «семёрки» первого прогона во втором пуле отсутствовали.
Лечится не симптом (строгость скоринга), а причина: однажды найденное и
оценённое не теряется, а накапливается прогон к прогону, и повторно за него не
платят.

Здесь только чистые функции без обращения к базе (CLAUDE.md — стадии и
подобная логика работы с базой не касаются): чтение строк `candidates` живёт в
`app.pipeline.persistence.load_history_pool`, а восстановление и учёт
отбраковки — здесь, на плоских данных, без единого импорта SQLAlchemy.

Клиенту происхождение кандидата не показывается ни в карточке, ни в
«Ограничениях этого поиска» (РЕШ-167): пометки нет нигде в отчёте. Внутри
системы след остаётся — `HISTORY_PASS_NO`, `HistoryRow.request_id` и запись в
журнале стадий при отказе чтения.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from app.schemas import Candidate, ScoredCandidate, SourceType

if TYPE_CHECKING:
    from app.pipeline.persistence import SessionFactory, StageRecord

#: Тот же логгер, что у оркестратора (см. пояснение в `pipeline_context.py`):
#: код читателей caplog ловит записи ровно по этому имени.
log = logging.getLogger("app.pipeline.orchestrator")

#: `pass_no`, которым помечаются в новом прогоне строки, восстановленные из
#: истории (пункт 6 задачи). Ноль, а не третье число прохода: новых колонок
#: волна не заводит (`init_db` — это `create_all`, `models.py`), а ноль не
#: занят ни первым, ни условным вторым проходом поиска (РЕШ-81).
HISTORY_PASS_NO = 0

#: Ключ признака нейтральной оценки в сохранённом `score` — тот же, что у
#: `app.metrics.NEUTRAL_KEY`; здесь не импортируется напрямую, чтобы модуль
#: восстановления не тянул за собой измеритель ради одной строки.
_NEUTRAL_KEY = "neutral"

_WHITESPACE = re.compile(r"\s+")


def normalize_task_text(raw_text: str) -> str:
    """Нормализовать текст задачи для признака «та же задача» (РЕШ-166).

    Только пробелы и регистр. Проба З-3: тексты двух прогонов одной задачи
    совпали посимвольно (690 знаков), а формулировки плана поиска — ни разу
    (0 из 8). Значит нормализация страхует только от того, что мог случайно
    внести оператор при копировании текста (лишний пробел, регистр), а не
    пытается угадывать смысл по-другому изложенной задачи — для неё есть явный
    номер прогона (второй путь Р-1).
    """
    return _WHITESPACE.sub(" ", raw_text.strip()).casefold()


@dataclass(slots=True)
class HistoryRow:
    """Плоский снимок нужных колонок `candidates` прошлого прогона.

    Без ORM-типов намеренно: восстановление — чистая функция, ей нечего знать
    про SQLAlchemy. Строку собирает `app.pipeline.persistence.load_history_pool`
    из строки таблицы `candidates`.
    """

    source_type: str
    provider: str
    title: str
    url: str
    org: str | None
    authors: list[str]
    year: int | None
    snippet: str
    raw_id: str | None
    dedup_key: str
    score: dict
    request_id: int


@dataclass(slots=True)
class HistoryPool:
    """Итог восстановления: годные кандидаты с оценками и причины отбраковки."""

    scored: list[ScoredCandidate] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def taken(self) -> int:
        return len(self.scored)


def restore_history_candidate(row: HistoryRow) -> tuple[ScoredCandidate | None, str | None]:
    """Собрать `ScoredCandidate` из строки истории либо назвать причину отказа.

    Строка не берётся (пункт 3 задачи, три случая): пустая оценка; нейтральная
    оценка (её проставил код после отказа батча скоринга, а не измерение); и
    пересчитанный `dedup_key()` не совпал с записанным (порча строки, правка
    руками, несовместимая версия схемы).
    """
    if not row.score:
        return None, "пустая оценка"
    if row.score.get(_NEUTRAL_KEY):
        return None, "нейтральная оценка"
    try:
        candidate = Candidate(
            source_type=SourceType(row.source_type),
            title=row.title,
            url=row.url,
            org=row.org,
            authors=list(row.authors or []),
            year=row.year,
            snippet=row.snippet or "",
            raw_id=row.raw_id,
            provider=row.provider,
        )
    except (ValidationError, ValueError):
        return None, "кандидат не читается схемой"
    if candidate.dedup_key() != row.dedup_key:
        return None, "ключ склейки не совпал"
    try:
        scored = ScoredCandidate(
            candidate=candidate,
            relevance=row.score["relevance"],
            maturity=row.score["maturity"],
            accessibility=row.score["accessibility"],
            freshness=row.score["freshness"],
            completeness=row.score["completeness"],
            notes=row.score.get("notes", ""),
        )
    except (KeyError, ValidationError):
        return None, "оценка не читается схемой"
    return scored, None


def build_history_pool(rows: list[HistoryRow]) -> HistoryPool:
    """Восстановить пул кандидатов из строк истории, посчитав отбраковку."""
    pool = HistoryPool()
    for row in rows:
        scored, reason = restore_history_candidate(row)
        if scored is None:
            assert reason is not None  # для читателя типов: пара всегда согласована
            pool.dropped[reason] = pool.dropped.get(reason, 0) + 1
        else:
            pool.scored.append(scored)
    return pool


def format_history_summary(pool: HistoryPool) -> str:
    """Строка лога (пункт 3 задачи): «из истории взято N, отброшено по причине
    X — M» для каждой причины отбраковки, если она случилась."""
    parts = [f"из истории взято {pool.taken}"]
    for reason, count in sorted(pool.dropped.items()):
        parts.append(f"отброшено по причине {reason} — {count}")
    return ", ".join(parts)


def exclude_known(candidates: list[Candidate], history: list[ScoredCandidate]) -> list[Candidate]:
    """Кандидаты этого прогона за вычетом уже известных истории (пункт 4).

    Кандидат, чей ключ склейки уже встречался в истории, в скоринг не идёт:
    оценка на него уже есть, и прогон платит только за то, чего не знает.
    """
    known = {item.candidate.dedup_key() for item in history}
    return [candidate for candidate in candidates if candidate.dedup_key() not in known]


async def load_and_log(
    session_factory: SessionFactory | None,
    *,
    raw_text: str,
    explicit_request_id: int | None,
    stage_log: list[StageRecord],
) -> HistoryPool:
    """Врезка для `run_flow.ScoutPipeline.run`: прочитать историю, отчитаться
    в лог и в журнал стадий, отдать пул (инвариант №5 — отказ не роняет прогон).

    Чтение вынесено сюда одной функцией, а не разложено по `run_flow.py`,
    чтобы врезка в него осталась короткой (пункт 1 задачи, лимит строк файла).
    Импорт `persistence` — отложенный: она сама импортирует отсюда чистые
    функции восстановления, и обратный импорт на уровне модуля был бы циклом.
    """
    from app.pipeline.persistence import HistoryReadError, StageRecord, load_history_pool

    started = datetime.now(UTC)
    try:
        pool = await load_history_pool(session_factory, raw_text=raw_text, explicit_request_id=explicit_request_id)
    except HistoryReadError as exc:
        finished = datetime.now(UTC)
        stage_log.append(StageRecord(stage="history", started_at=started, finished_at=finished, error=str(exc)))
        return HistoryPool()
    if pool.taken or pool.dropped:
        log.info(format_history_summary(pool))
    return pool
