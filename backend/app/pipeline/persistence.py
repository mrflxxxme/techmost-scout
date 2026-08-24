"""Чтение прогонов из БД и типы записей журнала: план поиска, отчёт, история
прошлых прогонов, кандидаты с оценками (Ф-8, РЕШ-40, РЕШ-83, РЕШ-84,
РЕШ-146/156).

Сама запись (`persist_run`) — в соседнем `persistence_write.py` (задача И-5):
файл дробится на чтение и запись, потому что правка записи под обновление
существующей строки `requests` (РЕШ-145, долг DLG-3) перевалила лимит 500
строк CLAUDE.md. Здесь остаются типы записей журнала (`StageRecord`,
`PlanRecord`, `CandidateRecord`) и три операции чтения: `load_search_plan`,
`load_report`, `load_history_pool` — `persistence_write.py` берёт типы отсюда
же, а `persist_run` реэкспортирован в конце файла, чтобы существующий импорт
`from app.pipeline.persistence import persist_run` не заметил дробления.

Работа с базой живёт вне стадий (CLAUDE.md, `docs/team/CONVENTIONS.md`):
стадии остаются чистыми функциями, а запись вызывает только оркестратор через
переданную фабрику сессий — той же формы, что и `app.db.session_scope`.

Миграций в MVP нет (`init_db` — это `create_all`): новые колонки появляются
только на вновь созданном файле базы, существующий файл нужно пересоздать.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Candidate as CandidateRow
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.models import SearchPlan as SearchPlanRow
from app.pipeline.history import HistoryPool, HistoryRow, build_history_pool, normalize_task_text
from app.schemas import Candidate, Report, SearchPlan

log = logging.getLogger(__name__)

#: Фабрика сессий: вызывается без аргументов, отдаёт асинхронный контекстный
#: менеджер с сессией внутри. По форме совпадает с `app.db.session_scope`,
#: которую и передаёт `scout run`; тесты подставляют свою на временной базе.
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(slots=True)
class StageRecord:
    """Один элемент журнала стадий — оркестратор собирает их по ходу прогона.

    Роутер моделей не считает стоимость по стадиям, только по запросу целиком,
    поэтому `provider`/`cost_rub` заполняются оркестратором приблизительно, по
    снимкам счётчика до и после вызова стадии; `sources_hit` — только для
    стадии поиска, из `SearchOutcome.hits_by_provider`.

    Полей `tokens_in`/`tokens_out` здесь больше нет (РЕШ-174): счётчик
    стоимости считает только рубли, токены никто не читал.

    `pass_no` — проход поиска, к которому относится стадия (РЕШ-81). Едет рядом
    со стадией по тому же доводу, что у планов (Т-19) и кандидатов (Т-20):
    второй проход повторяет стадии `search` и `scorer` под теми же именами, и
    без поля расход второго прохода (доклад по РЕШ-82) отделялся бы только
    порядком строк — упорядочить по времени и отсечь всё после `replan`.
    Умолчание — единица: однопроходных стадий большинство (интейк, карточки,
    проверка, сводка), и требовать от каждого места записи явной единицы значило
    бы завести источник расхождений на ровном месте.
    """

    stage: str
    started_at: datetime
    finished_at: datetime
    pass_no: int = 1
    provider: str | None = None
    cost_rub: float = 0.0
    sources_hit: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class PlanRecord:
    """План одного прохода поиска — что именно спрашивали у источников (РЕШ-83).

    `pass_no` едет рядом с планом, а не выводится из порядка записей: второй
    проход (РЕШ-81) появится в Т-21, и до него в списке всегда ровно одна
    запись с единицей. Порядковый номер в списке молча превратил бы повтор
    первого прохода во «второй».
    """

    plan: SearchPlan
    pass_no: int = 1


@dataclass(slots=True)
class CandidateRecord:
    """Один кандидат прогона вместе с оценками и судьбой (РЕШ-84).

    `score` — готовый словарь `app.metrics.score_payload`, а не пять отдельных
    полей: формат оценок задаётся в одном месте вместе с признаком нейтральной
    оценки, и измеритель читает ровно то, что записал прогон.

    `status` — `selected` для тех, кто прошёл отсечку по `relevance` и попал в
    отбор top-N (`select_top`), `rejected` для остальных. Это **не** «дошёл до
    карточки»: `group_for_cards` режет отобранных дальше, до `target` групп, и
    примерно половина строк со статусом `selected` в карточки не попадает —
    считать по этому полю «сколько дошло до клиента» значит завысить ответ.
    Разделение при этом обязательно: без него по базе не отличить «кандидат был
    плох» от «кандидат не дошёл из-за лимита на один источник», а вся таблица
    превращается в список найденного.

    `pass_no` — из какого прохода поиска пришёл кандидат (РЕШ-81); до Т-21
    всегда единица.
    """

    candidate: Candidate
    score: dict
    status: str
    pass_no: int = 1


class SearchPlanNotFoundError(Exception):
    """Сохранённого плана нет, он не читается схемой или база недоступна.

    Отдельный тип, а не общий `Exception`: прогон по сохранённому плану обязан
    отказать понятно и ДО первого платного запроса, а не наполовину. Текст
    исключения печатается оператору как есть, поэтому он на русском и называет
    причину.
    """


async def load_search_plan(session_factory: SessionFactory | None, plan_id: int) -> SearchPlan:
    """Прочитать сохранённый план по номеру строки `search_plans`.

    В отличие от записи, чтение отказ НЕ гасит: запись вторична по отношению к
    отчёту (инвариант №5 в редакции РЕШ-43), а вот прогон по несуществующему
    плану продолжать нечем — источники опрашивать не по чему. Любая причина
    (нет базы, нет строки, битый JSON) превращается в `SearchPlanNotFoundError` с
    человекочитаемой причиной, и решение о коде возврата принимает CLI.
    """
    if session_factory is None:
        raise SearchPlanNotFoundError(
            f"база недоступна, прочитать план №{plan_id} нечем — прогон по сохранённому плану невозможен"
        )
    try:
        async with session_factory() as session:
            found = await session.execute(select(SearchPlanRow).where(SearchPlanRow.id == plan_id))
            row = found.scalars().one_or_none()
            stored = dict(row.queries or {}) if row is not None else None
    except Exception as exc:
        raise SearchPlanNotFoundError(f"план №{plan_id} не удалось прочитать из базы: {exc}") from exc

    if stored is None:
        raise SearchPlanNotFoundError(f"плана №{plan_id} нет в базе — проверьте номер в таблице search_plans")
    try:
        return SearchPlan.model_validate(stored)
    except ValidationError as exc:
        raise SearchPlanNotFoundError(f"план №{plan_id} в базе не читается схемой SearchPlan: {exc}") from exc


class ReportNotFoundError(Exception):
    """Отчёт запроса не нашёлся, не читается схемой `Report` или база недоступна.

    Отдельный тип, а не общий `Exception`, по тому же доводу, что у
    `SearchPlanNotFoundError`: экран отчёта (Э3-5) отказывает оператору понятно
    и один раз, а не наполовину отрисованной страницей. Текст исключения на
    русском и называет причину — печатается как есть.
    """


async def load_report(session_factory: SessionFactory | None, request_id: int) -> Report:
    """Прочитать отчёт запроса №`request_id` из `requests.report_json` (РЕШ-146/156).

    Обратная операция для записи `report_json` в `_write`: JSON, сложенный туда
    `Report.model_dump_json()`, читается назад той же схемой одним вызовом
    `Report.model_validate_json`, без собственного разбора полей — тем же
    приёмом, что и `load_search_plan` для `search_plans.queries`.

    Отказ здесь НЕ гасится (в отличие от `persist_run`): чтение — по прямому
    запросу экрана отчёта, продолжать без него нечем, а не необязательное
    усиление обычного прогона.
    """
    if session_factory is None:
        raise ReportNotFoundError(f"база недоступна, прочитать отчёт запроса №{request_id} нечем")
    try:
        async with session_factory() as session:
            found = await session.execute(select(RequestRow).where(RequestRow.id == request_id))
            row = found.scalars().one_or_none()
            stored = row.report_json if row is not None else None
    except Exception as exc:
        raise ReportNotFoundError(f"отчёт запроса №{request_id} не удалось прочитать из базы: {exc}") from exc

    if not stored:
        raise ReportNotFoundError(
            f"отчёта запроса №{request_id} в базе нет — report_json пуст (прогон не дошёл до сборки отчёта)"
        )
    try:
        return Report.model_validate_json(stored)
    except ValidationError as exc:
        raise ReportNotFoundError(f"отчёт запроса №{request_id} в базе не читается схемой Report: {exc}") from exc


class HistoryReadError(Exception):
    """История прошлых прогонов не прочиталась.

    Отдельный тип по образцу `SearchPlanNotFoundError`, но поведение
    противоположное: `--plan-id` — явная команда оператора, и её отказ обязан
    остановить прогон до первого платного вызова. Накопительный поиск —
    необязательное усиление обычного прогона (инвариант №5), поэтому это
    исключение никогда не покидает `load_history_pool` — вызывающий код в
    `run_flow.py` гасит его, кладёт причину в лог и в журнал стадий, и прогон
    продолжается без истории.
    """


async def load_history_pool(
    session_factory: SessionFactory | None,
    *,
    raw_text: str,
    explicit_request_id: int | None = None,
) -> HistoryPool:
    """Кандидаты прошлых прогонов ЭТОЙ ЖЕ задачи (РЕШ-163, РЕШ-166).

    Признак «та же задача» — оба пути решения Р-1: явный номер прогона важнее
    текста (оператор указал его руками, потому что задачу пересказали другими
    словами и автоматическое опознание не сработает), иначе — совпадение
    нормализованного текста среди прогонов, не закончившихся отказом
    (`status != RequestStatus.FAILED` — из упавшего прогона брать нечего, кроме случайных
    строк с середины). Без фабрики сессий и без единого совпадения возвращается
    пустой пул, а не исключение — это тот же обычный случай, что и «БД не
    настроена» у `persist_run`, а не отказ.

    Восстановление строк — чистая функция `app.pipeline.history.build_history_pool`,
    здесь только чтение и сборка плоских `HistoryRow`.
    """
    if session_factory is None:
        return HistoryPool()
    try:
        async with session_factory() as session:
            if explicit_request_id is not None:
                request_ids = [explicit_request_id]
            else:
                target = normalize_task_text(raw_text)
                found = await session.execute(
                    select(RequestRow.id, RequestRow.raw_text).where(RequestRow.status != RequestStatus.FAILED)
                )
                request_ids = [rid for rid, text in found.all() if normalize_task_text(text) == target]
            if not request_ids:
                return HistoryPool()
            rows = await session.execute(select(CandidateRow).where(CandidateRow.request_id.in_(request_ids)))
            history_rows = [
                HistoryRow(
                    source_type=row.source_type,
                    provider=row.provider,
                    title=row.title,
                    url=row.url,
                    org=row.org,
                    authors=list(row.authors or []),
                    year=row.year,
                    snippet=row.snippet,
                    raw_id=row.raw_id,
                    dedup_key=row.dedup_key,
                    score=dict(row.score or {}),
                    request_id=row.request_id,
                )
                for row in rows.scalars().all()
            ]
    except Exception as exc:
        raise HistoryReadError(f"история прошлых прогонов не прочиталась: {exc}") from exc
    return build_history_pool(history_rows)


# Запись прогона (`persist_run`) живёт в `persistence_write.py` (задача И-5,
# долг DLG-3 грилла РЕШ-145/189): правка под `request_id` перевалила лимит
# 500 строк CLAUDE.md, а чтение (выше) и типы записей (`StageRecord`,
# `PlanRecord`, `CandidateRecord`) остались здесь. Импорт — намеренно ПОСЛЕ
# всех определений модуля: `persistence_write.py` берёт типы записей и
# `SessionFactory` отсюда же (`from app.pipeline.persistence import …`), и на
# момент этого импорта они уже существуют как атрибуты модуля — обычный
# приём разрыва циклического импорта между файлом чтения и файлом записи.
from app.pipeline.persistence_write import persist_run  # noqa: E402

__all__ = [
    "CandidateRecord",
    "HistoryReadError",
    "PlanRecord",
    "ReportNotFoundError",
    "SearchPlanNotFoundError",
    "SessionFactory",
    "StageRecord",
    "load_history_pool",
    "load_report",
    "load_search_plan",
    "persist_run",
]
