"""Допуск публичного экспресс-скана: лимит по IP и суточный потолок расходов (Э6-6а).

Две независимые защиты (РЕШ-258). Лимит по IP живёт **в памяти процесса**:
адрес не пишется ни в базу, ни в лог, рестарт счётчик обнуляет — этот риск
закрывает суточный потолок. Потолок считается по базе, поэтому переживает
рестарт: сумма `cost_rub` сканов за сутки, платный Яндекс Поиск и резерв на
каждый ещё не завершённый скан (иначе параллельные сканы вместе перебрали бы
потолок, пока ни один не записал свою стоимость).
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Request as RequestRow
from app.models import RequestStatus

log = logging.getLogger(__name__)

#: Сутки скана — календарные по Москве, фиксированный UTC+3 без `zoneinfo`:
#: в образе `python:3.12-slim` нет `tzdata`.
SCAN_TZ = timezone(timedelta(hours=3))
#: Предел платных запросов Яндекса на один прогон (`YandexSearchAdapter.max_queries`).
#: Сколько запросов сделал именно этот скан, база не хранит, поэтому потолок
#: считает консервативно — по пределу на каждый скан.
YANDEX_QUERIES_PER_SCAN = 6
#: Статусы, после которых скан денег уже не тратит.
FINISHED_STATUSES = frozenset(
    {RequestStatus.DRAFTED, RequestStatus.VALIDATED, RequestStatus.EXPORTED, RequestStatus.FAILED}
)


class ScanAdmission(StrEnum):
    """Решение о допуске нового скана."""

    OK = "ok"
    IP_LIMIT = "ip_limit"
    BUDGET_EXHAUSTED = "budget_exhausted"


def scan_day(now: datetime) -> str:
    """Календарные сутки по UTC+3 строкой `ГГГГ-ММ-ДД`; момент без пояса считается UTC."""
    moment = now.replace(tzinfo=UTC) if now.tzinfo is None else now
    return moment.astimezone(SCAN_TZ).date().isoformat()


def day_bounds_utc(now: datetime) -> tuple[datetime, datetime]:
    """Границы суток UTC+3, в которые попал `now`, в UTC — для сравнения с `created_at`."""
    moment = now.replace(tzinfo=UTC) if now.tzinfo is None else now
    local = moment.astimezone(SCAN_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def client_ip(request: Request) -> str:
    """Адрес клиента: последний `X-Forwarded-For` — только если соединение пришло с частного адреса.

    Частный адрес соединения — наш прокси (Caddy), он дописывает в конец
    цепочки адрес, который видел сам. Всё левее подделывает клиент. Соединение
    с публичного адреса — заголовок не от нас, ему не верим вовсе.
    """
    peer = request.client.host if request.client else ""
    if not _is_private(peer):
        return peer or "unknown"
    last = request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1].strip()
    return last if _parses_as_ip(last) else peer


def _is_private(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return False


def _parses_as_ip(address: str) -> bool:
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return False
    return True


class IpDailyCounter:
    """Сканы с одного IP за сутки UTC+3. Память процесса; адрес не покидает этот словарь.

    `check` и `record` разделены: скан, отказанный по другой причине
    (потолок денег), попытку IP не расходует.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}

    def check(self, ip: str, now: datetime, *, limit: int) -> bool:
        """Можно ли этому IP ещё один скан в сутки `now`."""
        return self._counts.get((scan_day(now), ip), 0) < limit

    def record(self, ip: str, now: datetime) -> None:
        """Засчитать скан; записи прошлых суток выбрасываются."""
        day = scan_day(now)
        self._counts = {key: n for key, n in self._counts.items() if key[0] == day}
        self._counts[(day, ip)] = self._counts.get((day, ip), 0) + 1

    def reset(self) -> None:
        """Обнулить счётчики (тесты и перезапуск процесса)."""
        self._counts.clear()


#: Общий на процесс счётчик.
ip_counter = IpDailyCounter()


async def spent_today_rub(session: AsyncSession, now: datetime, settings: Settings) -> float:
    """Потрачено за сутки: модели + Яндекс Поиск (по пределу) + резерв на незавершённые сканы."""
    start, end = day_bounds_utc(now)
    rows = (
        await session.execute(
            select(RequestRow.cost_rub, RequestRow.status).where(
                RequestRow.scan_token.is_not(None),
                RequestRow.created_at >= start,
                RequestRow.created_at < end,
            )
        )
    ).all()
    models = sum(cost or 0.0 for cost, _ in rows)
    search = len(rows) * YANDEX_QUERIES_PER_SCAN * settings.yandex_search_rub_per_query
    unfinished = sum(1 for _, status in rows if status not in FINISHED_STATUSES)
    return models + search + unfinished * settings.scan_cost_limit_rub


async def admit_scan(
    session: AsyncSession,
    ip: str,
    now: datetime,
    *,
    counter: IpDailyCounter | None = None,
    settings: Settings | None = None,
) -> ScanAdmission:
    """Решить, пускать ли новый скан. Ничего не записывает: `ip_counter.record` — за вызывающим.

    Остаток суточного потолка должен покрывать стоп-кран одного скана, иначе
    допущенный скан мог бы потолок пробить.
    """
    cfg = settings or get_settings()
    active_counter = ip_counter if counter is None else counter
    if not active_counter.check(ip, now, limit=cfg.scan_limit_per_ip_per_day):
        log.info("скан не допущен: лимит по адресу за сутки")
        return ScanAdmission.IP_LIMIT
    remaining = cfg.scan_daily_budget_rub - await spent_today_rub(session, now, cfg)
    if remaining < cfg.scan_cost_limit_rub:
        log.info("скан не допущен: суточный потолок расходов, остаток %.2f ₽", max(remaining, 0.0))
        return ScanAdmission.BUDGET_EXHAUSTED
    return ScanAdmission.OK
