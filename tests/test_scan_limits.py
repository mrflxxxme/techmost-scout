"""Допуск экспресс-скана (Э6-6а): лимит по IP, суточный потолок, адрес клиента.

База — временный SQLite, сети нет. Адреса в тестах — из документационных
диапазонов (RFC 5737) и частных сетей, настоящих людей среди них нет.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from inspect import signature
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request as HttpRequest

from app.config import Settings
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.search.yandex_quota import DEFAULT_MAX_QUERIES
from app.search.yandex_search import YandexSearchAdapter
from app.web.scan_limits import (
    YANDEX_QUERIES_PER_SCAN,
    IpDailyCounter,
    ScanAdmission,
    admit_scan,
    client_ip,
    day_bounds_utc,
    scan_day,
    spent_today_rub,
)

IP = "203.0.113.7"
# 12:00 UTC = 15:00 по Москве, 17 октября 2026.
NOON = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _settings(**overrides: object) -> Settings:
    """Настройки без `.env`; имена — как у переменных окружения (так заданы алиасы полей)."""
    base = {"YANDEX_SEARCH_RUB_PER_QUERY": 0.0}  # деньги Яндекса проверяются отдельно
    return Settings(_env_file=None, **{**base, **{key.upper(): value for key, value in overrides.items()}})


@pytest.fixture
async def session(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scan.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as db:
        yield db
    await engine.dispose()


async def _scan(
    db: AsyncSession,
    *,
    cost: float = 0.0,
    status: RequestStatus = RequestStatus.DRAFTED,
    at: datetime = NOON,
    tag: str | None = "t",
) -> None:
    db.add(RequestRow(raw_text="скан", status=status, cost_rub=cost, scan_token=tag, created_at=at))
    await db.flush()


async def _verdict(db: AsyncSession, cfg: Settings, ip: str = IP) -> ScanAdmission:
    """Допуск при пустом счётчике адреса: проверяется только потолок расходов."""
    return await admit_scan(db, ip, NOON, counter=IpDailyCounter(), settings=cfg)


def _http(peer: str | None, forwarded: str | None = None) -> HttpRequest:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    scope = {"type": "http", "headers": headers, "client": (peer, 5555) if peer else None}
    return HttpRequest(scope)


# ── Сутки UTC+3 ───────────────────────────────────────────────────────────────
def test_сутки_считаются_по_utc_плюс_3():
    assert scan_day(datetime(2026, 10, 17, 20, 59, tzinfo=UTC)) == "2026-10-17"
    assert scan_day(datetime(2026, 10, 17, 21, 0, tzinfo=UTC)) == "2026-10-18"
    assert scan_day(datetime(2026, 10, 17, 21, 0)) == "2026-10-18"  # без пояса — UTC


def test_границы_суток_в_utc():
    start, end = day_bounds_utc(NOON)

    assert start == datetime(2026, 10, 16, 21, 0, tzinfo=UTC)
    assert end - start == timedelta(days=1)


# ── Лимит по IP ───────────────────────────────────────────────────────────────
async def test_третий_скан_с_адреса_за_сутки_отказ_а_в_следующие_сутки_допуск(session):
    counter, cfg = IpDailyCounter(), _settings()
    assert await admit_scan(session, IP, NOON, counter=counter, settings=cfg) is ScanAdmission.OK
    counter.record(IP, NOON)
    assert await admit_scan(session, IP, NOON, counter=counter, settings=cfg) is ScanAdmission.OK
    counter.record(IP, NOON)

    assert await admit_scan(session, IP, NOON, counter=counter, settings=cfg) is ScanAdmission.IP_LIMIT
    # 23:59 по Москве — те же сутки, 00:00 — уже следующие.
    late = datetime(2026, 10, 17, 20, 59, tzinfo=UTC)
    assert await admit_scan(session, IP, late, counter=counter, settings=cfg) is ScanAdmission.IP_LIMIT
    after_midnight = datetime(2026, 10, 17, 21, 0, tzinfo=UTC)
    assert await admit_scan(session, IP, after_midnight, counter=counter, settings=cfg) is ScanAdmission.OK


async def test_другой_адрес_лимит_не_делит(session):
    counter, cfg = IpDailyCounter(), _settings()
    counter.record(IP, NOON)
    counter.record(IP, NOON)

    assert await admit_scan(session, "203.0.113.8", NOON, counter=counter, settings=cfg) is ScanAdmission.OK


def test_запись_нового_дня_выбрасывает_старые_ключи():
    counter = IpDailyCounter()
    counter.record(IP, NOON)
    counter.record(IP, NOON + timedelta(days=1))

    assert len(counter._counts) == 1


async def test_отказ_по_потолку_не_расходует_попытку_адреса(session):
    counter = IpDailyCounter()
    await _scan(session, cost=295)
    result = await admit_scan(session, IP, NOON, counter=counter, settings=_settings())

    assert result is ScanAdmission.BUDGET_EXHAUSTED
    assert counter.check(IP, NOON, limit=1)  # admit_scan сам ничего не записывает


# ── Суточный потолок ──────────────────────────────────────────────────────────
async def test_потрачено_291_отказ_285_допуск(session):
    cfg = _settings()
    await _scan(session, cost=291)
    assert await _verdict(session, cfg) is ScanAdmission.BUDGET_EXHAUSTED

    await session.rollback()
    await _scan(session, cost=285)
    assert await _verdict(session, cfg) is ScanAdmission.OK


async def test_граница_остатка_равного_стоп_крану_допуск(session):
    cfg = _settings()
    await _scan(session, cost=290)
    assert await _verdict(session, cfg) is ScanAdmission.OK

    await _scan(session, cost=0.5)
    assert await _verdict(session, cfg) is ScanAdmission.BUDGET_EXHAUSTED


async def test_два_незавершённых_скана_резервируют_20_рублей(session):
    cfg = _settings()
    await _scan(session, cost=0, status=RequestStatus.SEARCHING, tag="a")
    await _scan(session, cost=0, status=RequestStatus.INTAKE, tag="b")
    await _scan(session, cost=0, status=RequestStatus.FAILED, tag="c")  # завершён — резерва нет

    assert await spent_today_rub(session, NOON, cfg) == 20
    # 20 резерва + 270 = 290, остаток ровно 10: допуск; ещё 0,5 ₽ — отказ.
    await _scan(session, cost=270)
    assert await _verdict(session, cfg) is ScanAdmission.OK
    await _scan(session, cost=0.5)
    assert await _verdict(session, cfg) is ScanAdmission.BUDGET_EXHAUSTED


async def test_яндекс_поиск_входит_в_потолок_по_пределу_запросов(session):
    cfg = _settings(yandex_search_rub_per_query=0.5)  # 6 запросов × 0,5 ₽ = 3 ₽ на скан
    await _scan(session, tag="a")
    await _scan(session, tag="b")

    assert await spent_today_rub(session, NOON, cfg) == 6


async def test_в_потолок_входят_только_сканы_текущих_суток(session):
    cfg = _settings()
    await _scan(session, cost=290, at=NOON - timedelta(days=1), tag="old")
    await _scan(session, cost=1000, tag=None)  # обычный запрос, не скан
    await _scan(session, cost=5, tag="new")

    assert await spent_today_rub(session, NOON, cfg) == 5


async def test_скан_следующих_суток_в_потолок_не_входит(session):
    cfg = _settings()
    # 00:00 по Москве следующих суток — ровно верхняя граница, она в сумму не входит.
    _, end = day_bounds_utc(NOON)
    await _scan(session, cost=40, at=end, tag="boundary")
    await _scan(session, cost=40, at=NOON + timedelta(days=1), tag="tomorrow")
    await _scan(session, cost=5, at=end - timedelta(seconds=1), tag="last-second")

    assert await spent_today_rub(session, NOON, cfg) == 5


async def test_потолок_берётся_из_настройки(session):
    cfg = _settings(scan_daily_budget_rub=50)
    await _scan(session, cost=41)

    assert await _verdict(session, cfg) is ScanAdmission.BUDGET_EXHAUSTED


def _with_yandex(**overrides: object) -> Settings:
    """Настройки с боевым тарифом Яндекса (0,48 ₽) — худший случай скана 10 + 6 × 0,48 = 12,88 ₽."""
    return Settings(_env_file=None, **{key.upper(): value for key, value in overrides.items()})


async def test_допуск_требует_стоп_кран_плюс_яндекс_по_пределу_запросов(session):
    # Пустая база: остаток равен потолку. 12,88 — допуск, 12,87 — отказ (аудит Э6, потолок пробивался на 2,88 ₽).
    assert await _verdict(session, _with_yandex(scan_daily_budget_rub=12.88)) is ScanAdmission.OK
    assert await _verdict(session, _with_yandex(scan_daily_budget_rub=12.87)) is ScanAdmission.BUDGET_EXHAUSTED


async def test_граница_допуска_при_потраченном_300_минус_12_88(session):
    # Завершённый скан стоит в потолке cost + 2,88 ₽ Яндекса: 284,24 + 2,88 = 287,12, остаток 12,88.
    cfg = _with_yandex()
    await _scan(session, cost=284.24)
    assert await _verdict(session, cfg) is ScanAdmission.OK


async def test_при_потраченном_300_минус_12_87_отказ(session):
    await _scan(session, cost=284.25)  # 284,25 + 2,88 = 287,13, остаток 12,87
    assert await _verdict(session, _with_yandex()) is ScanAdmission.BUDGET_EXHAUSTED


async def test_граница_допуска_с_резервом_незавершённого_скана(session):
    cfg = _with_yandex(yandex_search_rub_per_query=0.5)  # порог 10 + 6 × 0,5 = 13 ₽; на скан в сумме 3 ₽ яндекса
    await _scan(session, cost=0, status=RequestStatus.SEARCHING, tag="a")  # 3 + резерв 10 = 13
    await _scan(session, cost=271, tag="b")  # 271 + 3 = 274; потрачено 287, остаток ровно 13
    assert await _verdict(session, cfg) is ScanAdmission.OK

    await _scan(session, cost=0.5, tag="c")  # ещё 0,5 + 3: остаток меньше 13
    assert await _verdict(session, cfg) is ScanAdmission.BUDGET_EXHAUSTED


def test_предел_запросов_яндекса_в_потолке_равен_умолчанию_адаптера():
    default = signature(YandexSearchAdapter.__init__).parameters["max_queries"].default
    assert YANDEX_QUERIES_PER_SCAN == default == DEFAULT_MAX_QUERIES == 6


# ── Догон колонки scan_token в существующей базе ──────────────────────────────
async def test_init_db_возвращает_scan_token_в_старую_базу(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Боевая база без `requests.scan_token` после `init_db` читается моделью `Request`.

    Колонка названа явно, а не обходом `ADDED_COLUMNS`: исчезновение записи из
    списка иначе осталось бы незамеченным (отказал бы весь рабочий интерфейс).
    """
    import app.db as db_module

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE requests DROP COLUMN scan_token"))
        await conn.execute(
            text(
                "INSERT INTO requests (created_at, raw_text, status, cost_rub, demo_flag) "
                "VALUES ('2026-01-01 00:00:00', 'старая', 'drafted', 0, 0)"
            )
        )
    monkeypatch.setattr(db_module, "engine", engine)

    await db_module.init_db()

    async with engine.connect() as conn:
        columns = await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns("requests")})
    assert "scan_token" in columns
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as db:
        rows = (await db.execute(select(RequestRow))).scalars().all()
    assert [row.raw_text for row in rows] == ["старая"]
    assert rows[0].scan_token is None
    await engine.dispose()


# ── Адрес клиента ─────────────────────────────────────────────────────────────
def test_с_частного_адреса_берётся_последний_forwarded_for():
    request = _http("172.18.0.2", forwarded=f"198.51.100.99, {IP}")

    assert client_ip(request) == IP


def test_поддельный_forwarded_for_с_публичного_адреса_ip_не_меняет():
    request = _http("8.8.8.8", forwarded="203.0.113.99")

    assert client_ip(request) == "8.8.8.8"


def test_без_заголовка_или_с_мусором_берётся_адрес_соединения():
    assert client_ip(_http("127.0.0.1")) == "127.0.0.1"
    assert client_ip(_http("10.0.0.3", forwarded="не-адрес")) == "10.0.0.3"


def test_без_адреса_соединения_адрес_неизвестен():
    assert client_ip(_http(None)) == "unknown"


# ── Приватность ───────────────────────────────────────────────────────────────
async def test_адрес_не_попадает_в_лог(session, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    counter, cfg = IpDailyCounter(), _settings()
    counter.record(IP, NOON)
    counter.record(IP, NOON)
    await admit_scan(session, IP, NOON, counter=counter, settings=cfg)  # ip_limit
    await _scan(session, cost=299)
    await admit_scan(session, "198.51.100.1", NOON, counter=IpDailyCounter(), settings=cfg)  # потолок

    assert caplog.records, "отказы должны оставлять строку в логе"
    assert IP not in caplog.text
    assert "198.51.100.1" not in caplog.text


async def test_адрес_не_пишется_в_базу(session):
    counter = IpDailyCounter()
    counter.record(IP, NOON)
    await admit_scan(session, IP, NOON, counter=counter, settings=_settings())
    await session.flush()

    for table in Base.metadata.tables:
        rows = (await session.execute(text(f"SELECT * FROM {table}"))).all()  # noqa: S608
        assert IP not in repr(rows)
