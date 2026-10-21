"""Публичный экспресс-скан: форма, запуск, страница, JSON и заявка (Э6-6в).

Приложение собирается настоящим `main.build_app` с публичным хостом, пайплайн
и интейк подменены заглушками (инвариант №7: ни моделей, ни сети). База —
временный файл SQLite; подмена `app.db.session_scope` одна на маршруты и фон.
Контакты в данные кладутся намеренно, чтобы проверить, что до посетителя они
не доходят ни в HTML, ни в JSON.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.config import get_settings
from app.models import Base, RequestStatus
from app.models import Candidate as CandidateRow
from app.models import Request as RequestRow
from app.pipeline.intake_flow import IntakeStageOutcome
from app.schemas import AnalogCard, Brief, Evidence, Report, ReportSummary, SolutionCard
from app.web import progress, scan_run
from app.web.routes import run
from app.web.scan_limits import ip_counter
from app.web.scan_view import SCAN_BANNER

PUBLIC = "scan.example.test"
WORK = "work.example.test"
PEER = "8.8.8.8"  # публичный адрес соединения: X-Forwarded-For ему не верим
TASK = "Катализатор гидроочистки теряет активность за 8 месяцев вместо 24, нужна замена."
PHONE = "+7 (495) 123-45-67"
EMAIL = "ivanov@lab.example.org"
SOURCE = "https://source.example.org/1"
COUNTRY_SOURCE = "https://source.example.org/2"
TELEGRAM = "https://t.me/techmost_orders"
ORDER_EMAIL = "orders@techmost.example.org"


class _FakePipeline:
    """Заглушка `ScoutPipeline`: пишет отчёт с подложенными контактами и кандидатов-источников."""

    calls: ClassVar[list[dict[str, Any]]] = []
    gate: ClassVar[asyncio.Event | None] = None
    fail: ClassVar[bool] = False

    def __init__(self, *, session_factory=None, progress=None, router=None, settings=None, **_: Any) -> None:
        self._session_factory = session_factory
        self._router = router
        self._settings = settings

    async def run(self, brief: Brief, *, request_id: int = 0, **kwargs: Any):
        type(self).calls.append(
            {
                "brief": brief,
                "router_limit": self._router.cost.limit_rub,
                "target_cards": self._settings.target_cards,
            }
        )
        if type(self).gate is not None:
            await type(self).gate.wait()
        async with self._session_factory() as session:
            row = await session.get(RequestRow, request_id)
            if type(self).fail:
                row.status = RequestStatus.FAILED
                row.error = "секретная причина отказа"
            else:
                row.report_json = _report(request_id, brief).model_dump_json()
                for url, title in ((SOURCE, "Источник решения"), (COUNTRY_SOURCE, "Источник страны")):
                    session.add(
                        CandidateRow(
                            request_id=request_id,
                            source_type="web",
                            provider="stub",
                            title=title,
                            url=url,
                            snippet=f"Затраты ниже на 35 %. Тел. {PHONE}, почта {EMAIL}",
                            dedup_key=url,
                        )
                    )
                row.status = RequestStatus.DRAFTED
        if type(self).fail:
            raise RuntimeError("секретная причина отказа")

    async def aclose(self) -> None:
        return None


def _report(request_id: int, brief: Brief) -> Report:
    card = SolutionCard(
        title="Катализатор с добавкой церия",
        essence=f"Снижает затраты. Звоните {PHONE}, пишите {EMAIL}.",
        applicability="Подходит для установки",
        developer_org="Институт Ж",
        contact_leads=[PHONE, EMAIL],
        next_step="Позвонить разработчику",
        evidence=[Evidence(claim="Затраты ниже на 35%", url=SOURCE)],
    )
    analog = AnalogCard(
        title="Сервис подписки",
        essence="Платформа за рубежом",
        country="US",
        country_evidence_url=COUNTRY_SOURCE,
        business_model="Подписка",
        transferability_ru="Перенос возможен",
        what_to_do_in_ru="Изучить условия",
        evidence=[Evidence(claim="Сервис работает", url=COUNTRY_SOURCE)],
    )
    return Report(
        request_id=request_id,
        brief=brief,
        cards=[card],
        analog_cards=[analog],
        summary=ReportSummary(recommendation="Сводка не для скана."),
        sources_unavailable=["OpenAlex"],
        generated_at=datetime(2026, 10, 21, 9, 0),
    )


async def _fake_intake(router, task, *, industry_hint, session_factory, stage_log, solution_kind="research"):
    brief = Brief(industry=industry_hint, task_summary=task[:60], solution_kind="digital")
    return IntakeStageOutcome(intake=None, brief=brief, caveats=[], warning=None)


async def _fake_constraints(router, brief, task, *, industry_hint, session_factory, stage_log):
    return brief, False


@pytest.fixture(autouse=True)
def _scan_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "SCAN_LIMIT_PER_IP_PER_DAY",
        "SCAN_DAILY_BUDGET_RUB",
        "SCAN_COST_LIMIT_RUB",
        "SCAN_ORDER_TELEGRAM_URL",
        "SCAN_ORDER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SCAN_ORDER_TELEGRAM_URL", TELEGRAM)
    monkeypatch.setenv("SCAN_ORDER_EMAIL", ORDER_EMAIL)
    get_settings.cache_clear()
    ip_counter.reset()
    run._active_runs.clear()
    monkeypatch.setattr(run, "_start_lock", asyncio.Lock())
    monkeypatch.setattr(progress, "_broker", progress.ProgressBroker())
    _FakePipeline.calls = []
    _FakePipeline.gate = None
    _FakePipeline.fail = False
    monkeypatch.setattr(run, "ScoutPipeline", _FakePipeline)
    monkeypatch.setattr(scan_run, "run_intake_stage", _fake_intake)
    monkeypatch.setattr(scan_run, "resolve_constraints_stage", _fake_constraints)
    yield
    run._active_runs.clear()
    ip_counter.reset()
    get_settings.cache_clear()


@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scan-routes.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def factory():
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr("app.db.session_scope", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
async def client(db):
    app = main.build_app("test", public_scan_host=PUBLIC)
    transport = ASGITransport(app=app, client=(PEER, 4321))
    async with AsyncClient(transport=transport, base_url=f"http://{PUBLIC}") as http:
        yield http


async def _finish() -> None:
    for task in list(run._active_runs.values()):
        await task


async def _post(client: AsyncClient, *, task: str = TASK, kind: str = "product", **headers: str):
    return await client.post("/scan", data={"task": task, "kind": kind}, headers=headers)


def _token(response) -> str:
    assert response.status_code == 303, response.text
    match = re.fullmatch(r"/scan/([A-Za-z0-9_-]{43})", response.headers["location"])
    assert match, response.headers["location"]
    return match.group(1)


async def _ready(client: AsyncClient) -> str:
    token = _token(await _post(client))
    await _finish()
    return token


async def _count(db) -> int:
    async with db() as session:
        return (await session.execute(select(func.count()).select_from(RequestRow))).scalar_one()


async def test_форма_с_четырьмя_видами_без_контактов_и_науки(client: AsyncClient):
    response = await client.get("/scan")
    assert response.status_code == 200
    html = response.text
    assert SCAN_BANNER in html
    for label in ("Продукт", "Цифровое решение", "Бизнес-модель", "Не знаю"):
        assert f">{label}</option>" in html
    for value in ("product", "digital", "business_model", "unknown"):
        assert f'value="{value}"' in html
    assert 'value="research"' not in html
    assert not re.search(r'type="(email|tel)"|name="(email|phone|contact)', html)
    assert "noindex" in html


async def test_в_основе_нет_ссылок_рабочего_места_и_api(client: AsyncClient):
    html = (await client.get("/scan")).text
    for forbidden in ("/requests", "/showcase", "/demo", ">API<", "/api/docs", "История"):
        assert forbidden not in html


async def test_форма_ведёт_на_страницу_с_плашкой_и_результатом(client: AsyncClient):
    token = _token(await _post(client))
    await _finish()
    page = await client.get(f"/scan/{token}")
    assert page.status_code == 200
    assert SCAN_BANNER in page.text
    assert "Катализатор с добавкой церия" in page.text
    assert "Сервис подписки" in page.text
    assert "Не ответили источники: OpenAlex" in page.text
    assert "Сводка не для скана" not in page.text and "Позвонить разработчику" not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"


async def test_json_с_плашкой_видом_и_версией(client: AsyncClient):
    token = await _ready(client)
    response = await client.get(f"/scan/{token}.json")
    assert response.status_code == 200
    payload = response.json()
    assert payload["banner"] == SCAN_BANNER
    assert payload["kind"] == "express_scan" and payload["schema_version"] == "1.0"
    assert [card["title"] for card in payload["cards"]] == ["Катализатор с добавкой церия"]
    assert payload["analogs"][0]["country"] == "US"
    assert payload["sources_unavailable"] == ["OpenAlex"]
    assert response.headers["cache-control"] == "no-store"
    assert "request_id" not in response.text and '"id"' not in response.text  # номер запроса наружу не уходит


async def test_подложенных_телефона_и_почты_нет_ни_в_html_ни_в_json(client: AsyncClient):
    token = await _ready(client)
    html = (await client.get(f"/scan/{token}")).text
    payload = json.dumps((await client.get(f"/scan/{token}.json")).json(), ensure_ascii=False)
    for secret in (PHONE, EMAIL, "123-45-67", "lab.example.org"):
        assert secret not in html and secret not in payload
    assert "[контакт скрыт]" in html
    assert "contact_leads" not in payload and "next_step" not in payload


async def test_бриф_режим_business_и_вид_из_формы(client: AsyncClient):
    _token(await _post(client, kind="business_model"))
    await _finish()
    [call] = _FakePipeline.calls
    assert call["brief"].mode == "business" and call["brief"].solution_kind == "business_model"


async def test_роутер_скана_лимит_10_рублей_и_три_карточки(client: AsyncClient):
    await _ready(client)
    [call] = _FakePipeline.calls
    assert call["router_limit"] == 10.0
    assert call["target_cards"] == 3


async def test_в_базе_скан_с_токеном_и_без_контактов_посетителя(client: AsyncClient, db):
    token = await _ready(client)
    async with db() as session:
        row = (await session.execute(select(RequestRow))).scalar_one()
    assert row.scan_token == token and row.api_client is None and row.raw_text == TASK


async def test_research_422_без_запуска_и_без_расхода_лимита(client: AsyncClient, db):
    response = await _post(client, kind="research")
    assert response.status_code == 422
    assert "Выберите вид решения" in response.text and SCAN_BANNER in response.text
    assert await _count(db) == 0 and run._active_runs == {} and _FakePipeline.calls == []
    assert ip_counter.check(PEER, datetime.now().astimezone(), limit=1)


@pytest.mark.parametrize("kind", ["Product", "gov", "science", "ПРОДУКТ"])
async def test_неизвестный_вид_422(client: AsyncClient, kind: str):
    assert (await _post(client, kind=kind)).status_code == 422


@pytest.mark.parametrize("task", ["", "   ", "коротко", "а" * 3001])
async def test_пустой_короткий_и_длинный_текст_422(client: AsyncClient, db, task: str):
    response = await _post(client, task=task)
    assert response.status_code == 422
    assert "знаков" in response.text
    assert await _count(db) == 0


async def test_название_компании_422_и_не_эхом(client: AsyncClient, db):
    response = await _post(client, task="Нужна замена катализатора для ООО «Ромашка» на установке.")
    assert response.status_code == 422
    notice = re.search(r"data-scan-error>(.*?)</div>", response.text, re.S).group(1)
    assert "название компании" in notice and "Ромашка" not in notice  # в тексте формы остаётся только ввод посетителя
    assert await _count(db) == 0


async def test_третий_скан_с_адреса_429(client: AsyncClient, db):
    for _ in range(2):
        _token(await _post(client))
        await _finish()
    third = await _post(client)
    assert third.status_code == 429
    assert "уже запущено максимальное число обзоров" in third.text
    assert await _count(db) == 2 and len(_FakePipeline.calls) == 2


async def test_другой_адрес_через_прокси_не_делит_лимит(client: AsyncClient):
    for _ in range(2):
        _token(await _post(client, **{"X-Forwarded-For": "198.51.100.1"}))
        await _finish()
    assert (await _post(client, **{"X-Forwarded-For": "198.51.100.2"})).status_code == 429


async def test_потолок_429_лимит_на_сегодня_исчерпан_и_попытки_адреса_целы(
    client: AsyncClient, db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SCAN_DAILY_BUDGET_RUB", "5")  # меньше стоп-крана одного скана
    get_settings.cache_clear()
    for _ in range(3):
        response = await _post(client)
        assert response.status_code == 429
        assert "лимит на сегодня исчерпан" in response.text
    assert await _count(db) == 0 and _FakePipeline.calls == []
    # Отказ по потолку попытку адреса не тратит: после подъёма потолка проходят оба скана.
    monkeypatch.setenv("SCAN_DAILY_BUDGET_RUB", "300")
    get_settings.cache_clear()
    for _ in range(2):
        _token(await _post(client))
        await _finish()


async def test_занято_отказ_без_расхода_лимита_повтор_после_освобождения_проходит(client: AsyncClient, db):
    _FakePipeline.gate = asyncio.Event()
    first = _token(await _post(client))
    await asyncio.sleep(0)
    busy = await _post(client)
    assert busy.status_code == 503
    assert "готовится другой обзор" in busy.text
    assert busy.headers["retry-after"] == "120"
    assert await _count(db) == 1
    _FakePipeline.gate.set()
    await _finish()
    # Лимит адреса — два: отказ «занято» ни одной попытки не съел.
    second = _token(await _post(client))
    await _finish()
    assert second != first
    assert (await _post(client)).status_code == 429


async def test_два_одновременных_запуска_проходит_один(client: AsyncClient, db):
    _FakePipeline.gate = asyncio.Event()
    first, second = await asyncio.gather(_post(client), _post(client))
    assert sorted([first.status_code, second.status_code]) == [303, 503]
    assert await _count(db) == 1
    _FakePipeline.gate.set()
    await _finish()


async def test_пока_скан_идёт_страница_с_прогрессом_и_плашкой_статус_и_409_в_json(client: AsyncClient):
    _FakePipeline.gate = asyncio.Event()
    token = _token(await _post(client))
    await asyncio.sleep(0)
    page = await client.get(f"/scan/{token}")
    assert page.status_code == 200
    assert SCAN_BANNER in page.text and f"/scan/{token}/status" in page.text
    assert "Заказать полный отчёт" not in page.text
    status = await client.get(f"/scan/{token}/status")
    assert status.status_code == 200 and "hx-get" in status.text and "hx-redirect" not in status.headers
    assert (await client.get(f"/scan/{token}.json")).status_code == 409
    _FakePipeline.gate.set()
    await _finish()
    done = await client.get(f"/scan/{token}/status")
    assert done.headers["hx-redirect"] == f"/scan/{token}"


async def test_отказ_скана_честная_страница_без_причины_и_кнопки(client: AsyncClient):
    _FakePipeline.fail = True
    token = _token(await _post(client))
    await _finish()
    page = await client.get(f"/scan/{token}")
    assert page.status_code == 200
    assert "Обзор не получился" in page.text and SCAN_BANNER in page.text
    assert "секретная причина" not in page.text
    assert "Заказать полный отчёт" not in page.text
    failed = await client.get(f"/scan/{token}.json")
    assert failed.status_code == 409 and failed.json()["detail"]["error"] == "scan_failed"
    assert "секретная причина" not in failed.text


async def test_прерванный_перезапуском_скан_объяснён(client: AsyncClient, db):
    request_id, token = await scan_run.create_scan_request(TASK)  # статус intake, задачи в реестре нет
    page = await client.get(f"/scan/{token}")
    assert page.status_code == 200 and "прервана перезапуском" in page.text
    assert request_id not in run._active_runs


async def test_чужой_токен_404_и_страницей_и_json(client: AsyncClient, db):
    token = await _ready(client)
    other = token[:-1] + ("A" if token[-1] != "A" else "B")
    page = await client.get(f"/scan/{other}")
    assert page.status_code == 404 and SCAN_BANNER in page.text and "не найден" in page.text
    assert (await client.get(f"/scan/{other}.json")).status_code == 404
    assert (await client.get(f"/scan/{other}/status")).headers["hx-redirect"] == f"/scan/{other}"
    for guess in ("1", "0", "None"):
        assert (await client.get(f"/scan/{guess}")).status_code == 404


async def test_обычный_запрос_без_токена_по_scan_не_открывается(client: AsyncClient, db):
    async with db() as session:
        session.add(RequestRow(raw_text="рабочий запрос", status=RequestStatus.DRAFTED))
    for path in ("/scan/1", "/scan/1.json", "/scan/None"):
        assert (await client.get(path)).status_code == 404


async def test_кнопка_заявки_одна_элемент_следующего_шага_ровно_один(client: AsyncClient):
    token = await _ready(client)
    html = (await client.get(f"/scan/{token}")).text
    assert html.count("data-next-step") == 1
    assert html.count("Заказать полный отчёт") == 1
    assert f'href="{TELEGRAM}"' in html
    assert f"mailto:{ORDER_EMAIL}?subject=" in html and token[:8] in html


async def test_только_почта_номер_скана_в_теме(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCAN_ORDER_TELEGRAM_URL", "")
    get_settings.cache_clear()
    token = await _ready(client)
    html = (await client.get(f"/scan/{token}")).text
    assert html.count("data-next-step") == 1 and html.count("Заказать полный отчёт") == 1
    assert f'href="mailto:{ORDER_EMAIL}?subject=' in html and token[:8] in html
    assert "t.me" not in html


async def test_только_telegram_без_почты(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SCAN_ORDER_EMAIL", "")
    get_settings.cache_clear()
    html = (await client.get(f"/scan/{await _ready(client)}")).text
    assert html.count("data-next-step") == 1 and f'href="{TELEGRAM}"' in html and "mailto:" not in html


@pytest.mark.parametrize(
    ("telegram", "email"),
    [
        ("", ""),
        ("http://t.me/x", "не почта"),
        ('https://evil.example/x"onmouseover="a', "a b@c.d"),
        ("javascript:alert(1)", ""),
    ],
)
async def test_нет_годных_адресов_кнопки_нет(client: AsyncClient, monkeypatch: pytest.MonkeyPatch, telegram, email):
    monkeypatch.setenv("SCAN_ORDER_TELEGRAM_URL", telegram)
    monkeypatch.setenv("SCAN_ORDER_EMAIL", email)
    get_settings.cache_clear()
    html = (await client.get(f"/scan/{await _ready(client)}")).text
    assert "data-next-step" not in html and "Заказать полный отчёт" not in html
    assert "evil.example" not in html and "javascript:" not in html


async def test_на_рабочем_хосте_scan_закрыт_а_на_публичном_закрыт_интерфейс(client: AsyncClient):
    assert (await client.get("/scan", headers={"Host": WORK})).status_code == 404
    assert (
        await client.post("/scan", data={"task": TASK, "kind": "product"}, headers={"Host": WORK})
    ).status_code == 404
    assert (await client.get("/requests")).status_code == 404
    assert _FakePipeline.calls == []
