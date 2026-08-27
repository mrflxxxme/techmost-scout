"""Экран ревью эксперта (Э4-И3, SPEC Э4-1): форма вердикта, статус `validated`.

Фикстуры — та же форма, что в `tests/test_web_report_verdicts.py` (временная
SQLite на `tmp_path`, подмена `app.db.session_scope`): сеть не задействована
(инвариант №7).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.config import get_settings
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import SummaryOverride as SummaryOverrideRow
from app.models import Verdict as VerdictRow
from app.schemas import Evidence, Report, ReportSummary, SolutionCard

_AUTH = ("scout", "parol123")


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'review-test.db'}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def _wire_test_db(monkeypatch: pytest.MonkeyPatch, db_session_factory):
    """Подмена `session_scope` и данных доступа под временную базу запроса.

    `reviewer_name` правится через текущий `get_settings()`, а не через
    `main.settings` напрямую: маршрут ревью читает настройку свежим вызовом
    `get_settings()` при каждом запросе (кэш `lru_cache` сбрасывают другие
    тестовые файлы через фикстуру `settings` из `conftest.py`), и правка
    объекта, который `get_settings()` вернёт позже, до него не долетала бы.
    """
    monkeypatch.setattr("app.db.session_scope", db_session_factory)

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main.settings, "web_auth_user", _AUTH[0])
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr(_AUTH[1]))
    monkeypatch.setattr(get_settings(), "reviewer_name", "Мария Петрова")
    yield db_session_factory


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def _make_card(**overrides) -> SolutionCard:
    fields = {
        "title": "Катализатор с присадкой молибдена",
        "essence": "Присадка продлевает межрегенерационный цикл катализатора гидроочистки.",
        "applicability": "Закрывает задачу продления срока службы катализатора.",
        "next_step": "Запросить протокол испытаний.",
        "evidence": [Evidence(claim="Испытан на действующей установке", url="https://example.org/a")],
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _make_report(cards: list[SolutionCard], contest_cards: list[SolutionCard], **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": cards,
        "contest_cards": contest_cards,
        "summary": ReportSummary(recommendation="Начать с варианта №1."),
        "generated_at": datetime(2026, 8, 27, 9, 0),
    }
    fields.update(overrides)
    return Report(**fields)


async def _seed(session_factory, *, report: Report, status: RequestStatus) -> tuple[int, list[int], list[int]]:
    """Кладёт запрос и карточки обеих секций, возвращает id основной и конкурсной секций."""
    async with session_factory() as session:
        row = RequestRow(
            raw_text="катализатор теряет активность раньше срока",
            status=status,
            cost_rub=report.cost_rub,
            report_json=report.model_dump_json(),
        )
        session.add(row)
        await session.flush()
        request_id = int(row.id)

        def _add_section(cards: list[SolutionCard], *, is_contest: bool) -> list[int]:
            ids: list[int] = []
            for position, card in enumerate(cards):
                card_row = SolutionCardRow(
                    request_id=request_id,
                    position=position,
                    is_contest=is_contest,
                    title=card.title,
                    essence=card.essence,
                    maturity_stage=card.maturity_stage.value,
                    applicability=card.applicability,
                    next_step=card.next_step,
                    evidence=[item.model_dump(mode="json") for item in card.evidence],
                    confidence=card.confidence.value,
                )
                session.add(card_row)
                ids.append(card_row)
            return ids

        main_rows = _add_section(report.cards, is_contest=False)
        contest_rows = _add_section(report.contest_cards, is_contest=True)
        await session.flush()
        main_ids = [int(r.id) for r in main_rows]
        contest_ids = [int(r.id) for r in contest_rows]
    return request_id, main_ids, contest_ids


async def _status(session_factory, request_id: int) -> str:
    async with session_factory() as session:
        row = (await session.execute(select(RequestRow).where(RequestRow.id == request_id))).scalars().one()
        return str(row.status)


async def _verdict_count(session_factory, card_id: int) -> int:
    async with session_factory() as session:
        rows = (await session.execute(select(VerdictRow).where(VerdictRow.card_id == card_id))).scalars().all()
        return len(rows)


def test_экран_открывается_и_содержит_формы_обеих_секций(client, db_session_factory, brief):
    """Экран отдаёт формы вердикта для карточек и основной, и конкурсной секции."""
    report = _make_report(
        [_make_card(title="Вариант основной секции")], [_make_card(title="Вариант конкурса")], brief=brief
    )
    request_id, main_ids, contest_ids = asyncio.run(
        _seed(db_session_factory, report=report, status=RequestStatus.DRAFTED)
    )

    with client:
        response = client.get(f"/requests/{request_id}/review", auth=_AUTH)

    assert response.status_code == 200
    assert "Вариант основной секции" in response.text
    assert "Вариант конкурса" in response.text
    assert f"/cards/{main_ids[0]}/verdict" in response.text
    assert f"/cards/{contest_ids[0]}/verdict" in response.text
    assert 'value="Мария Петрова"' in response.text


def test_правка_title_видна_на_экране_ревью(client, db_session_factory, brief):
    """РЕШ-205: экран ревью показывает текст после наложения `editor_overrides`."""
    report = _make_report([_make_card(title="Исходный заголовок")], [], brief=brief)
    request_id, main_ids, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    async def _apply_override() -> None:
        async with db_session_factory() as session:
            card_row = await session.get(SolutionCardRow, main_ids[0])
            card_row.editor_overrides = {"title": "Правленый заголовок"}

    asyncio.run(_apply_override())

    with client:
        response = client.get(f"/requests/{request_id}/review", auth=_AUTH)

    assert response.status_code == 200
    assert "Правленый заголовок" in response.text
    assert "Исходный заголовок" not in response.text
    # Ключ (секция, позиция) остаётся позицией из БД — форма вердикта всё
    # так же ссылается на исходный id карточки, наложение правки его не сдвигает.
    assert f"/cards/{main_ids[0]}/verdict" in response.text


def test_вердикт_пишет_новую_строку_дважды(client, db_session_factory, brief):
    """Два вердикта подряд по одной карточке — две строки в `verdicts`."""
    report = _make_report([_make_card()], [], brief=brief)
    _request_id, main_ids, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))
    card_id = main_ids[0]

    with client:
        client.post(
            f"/cards/{card_id}/verdict",
            data={"decision": "approve", "rating": "4", "comment": "хорошо", "reviewer_name": "Иван Иванов"},
            auth=_AUTH,
        )
        response = client.post(
            f"/cards/{card_id}/verdict",
            data={"decision": "reject", "rating": "2", "comment": "передумал", "reviewer_name": "Иван Иванов"},
            auth=_AUTH,
        )

    assert response.status_code == 200
    assert "Вердикт записан: Отклонено" in response.text
    assert asyncio.run(_verdict_count(db_session_factory, card_id)) == 2


def test_статус_validated_после_вердиктов_по_всем_карточкам(client, db_session_factory, brief):
    """Пока не размечена хотя бы одна карточка — статус остаётся `drafted`."""
    report = _make_report([_make_card(title="Первая"), _make_card(title="Вторая")], [], brief=brief)
    request_id, main_ids, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    with client:
        client.post(
            f"/cards/{main_ids[0]}/verdict",
            data={"decision": "approve", "rating": "5", "comment": "", "reviewer_name": "Иван Иванов"},
            auth=_AUTH,
        )
        assert asyncio.run(_status(db_session_factory, request_id)) == RequestStatus.DRAFTED.value

        response = client.post(
            f"/cards/{main_ids[1]}/verdict",
            data={"decision": "approve", "rating": "5", "comment": "", "reviewer_name": "Иван Иванов"},
            auth=_AUTH,
        )

    assert "Проверено 2 из 2" in response.text
    assert asyncio.run(_status(db_session_factory, request_id)) == RequestStatus.VALIDATED.value


def test_статус_validated_считает_обе_секции(client, db_session_factory, brief):
    """`validated` наступает только когда размечены карточки основной И конкурсной секции."""
    report = _make_report([_make_card(title="Основная")], [_make_card(title="Конкурсная")], brief=brief)
    request_id, main_ids, contest_ids = asyncio.run(
        _seed(db_session_factory, report=report, status=RequestStatus.DRAFTED)
    )

    with client:
        client.post(
            f"/cards/{main_ids[0]}/verdict",
            data={"decision": "approve", "rating": "5", "comment": "", "reviewer_name": "Иван Иванов"},
            auth=_AUTH,
        )
        assert asyncio.run(_status(db_session_factory, request_id)) == RequestStatus.DRAFTED.value

        client.post(
            f"/cards/{contest_ids[0]}/verdict",
            data={"decision": "rework", "rating": "3", "comment": "", "reviewer_name": "Иван Иванов"},
            auth=_AUTH,
        )

    assert asyncio.run(_status(db_session_factory, request_id)) == RequestStatus.VALIDATED.value


def test_оценка_вне_диапазона_отклоняется(client, db_session_factory, brief):
    """Оценка 0 или 6 — отказ, новой строки `verdicts` не появляется."""
    report = _make_report([_make_card()], [], brief=brief)
    _request_id, main_ids, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))
    card_id = main_ids[0]

    with client:
        response = client.post(
            f"/cards/{card_id}/verdict",
            data={"decision": "approve", "rating": "0", "comment": "", "reviewer_name": "Иван Иванов"},
            auth=_AUTH,
        )

    assert response.status_code == 400
    assert asyncio.run(_verdict_count(db_session_factory, card_id)) == 0


def test_пустое_имя_отклоняется(client, db_session_factory, brief):
    """Пустое имя проверяющего — 400 человеческой фразой, ноль новых строк (инвариант №2)."""
    report = _make_report([_make_card()], [], brief=brief)
    _request_id, main_ids, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))
    card_id = main_ids[0]

    with client:
        response = client.post(
            f"/cards/{card_id}/verdict",
            data={"decision": "approve", "rating": "5", "comment": "", "reviewer_name": "   "},
            auth=_AUTH,
        )

    assert response.status_code == 400
    assert "Укажите имя проверяющего" in response.text
    assert asyncio.run(_verdict_count(db_session_factory, card_id)) == 0


def test_exported_не_понижается_до_validated(client, db_session_factory, brief):
    """Запрос в статусе `exported` не откатывается к `validated` после нового вердикта."""
    report = _make_report([_make_card()], [], brief=brief)
    request_id, main_ids, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.EXPORTED))
    card_id = main_ids[0]

    with client:
        client.post(
            f"/cards/{card_id}/verdict",
            data={"decision": "approve", "rating": "5", "comment": "", "reviewer_name": "Иван Иванов"},
            auth=_AUTH,
        )

    assert asyncio.run(_status(db_session_factory, request_id)) == RequestStatus.EXPORTED.value


async def _summary_override_rows(session_factory, request_id: int) -> list[SummaryOverrideRow]:
    async with session_factory() as session:
        rows = (
            (await session.execute(select(SummaryOverrideRow).where(SummaryOverrideRow.request_id == request_id)))
            .scalars()
            .all()
        )
        return rows


def test_экран_показывает_поля_правки_сводки_с_исходным_текстом(client, db_session_factory, brief):
    """Критерий 2: форма правки предзаполнена текущим (пока непраленым) текстом модели."""
    report = _make_report(
        [_make_card()],
        [],
        brief=brief,
        summary=ReportSummary(recommendation="Рекомендация модели.", caveats=["Оговорка модели."]),
    )
    request_id, _, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    with client:
        response = client.get(f"/requests/{request_id}/review", auth=_AUTH)

    assert response.status_code == 200
    assert "Рекомендация модели." in response.text
    assert "Оговорка модели." in response.text
    assert f"/requests/{request_id}/summary" in response.text


def test_экран_показывает_правленый_текст_сводки_вместо_исходного(client, db_session_factory, brief):
    """РЕШ-206/205: правка эксперта видна на экране ревью вместо исходного вывода модели."""
    report = _make_report(
        [_make_card()],
        [],
        brief=brief,
        summary=ReportSummary(recommendation="Исходная рекомендация.", caveats=["Исходная оговорка."]),
    )
    request_id, _, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    async def _seed_override() -> None:
        async with db_session_factory() as session:
            session.add(
                SummaryOverrideRow(
                    request_id=request_id,
                    recommendation="Правленая рекомендация.",
                    caveats=["Правленая оговорка."],
                )
            )

    asyncio.run(_seed_override())

    with client:
        response = client.get(f"/requests/{request_id}/review", auth=_AUTH)

    assert response.status_code == 200
    assert "Правленая рекомендация." in response.text
    assert "Исходная рекомендация." not in response.text


def test_summary_post_пишет_строку_и_возвращает_правленый_текст(client, db_session_factory, brief):
    """Критерий 1: `POST /requests/{id}/summary` пишет строку и отдаёт правленый текст."""
    report = _make_report([_make_card()], [], brief=brief)
    request_id, _, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    with client:
        response = client.post(
            f"/requests/{request_id}/summary",
            data={"recommendation": "Начать с варианта 2.", "caveats": "Оговорка первая\nОговорка вторая"},
            auth=_AUTH,
        )

    assert response.status_code == 200
    assert "Начать с варианта 2." in response.text
    assert "Оговорка первая" in response.text

    rows = asyncio.run(_summary_override_rows(db_session_factory, request_id))
    assert len(rows) == 1
    assert rows[0].recommendation == "Начать с варианта 2."
    assert rows[0].caveats == ["Оговорка первая", "Оговорка вторая"]


def test_summary_post_повторно_перезаписывает_ту_же_строку(client, db_session_factory, brief):
    """Критерий 1: повторный `POST` перезаписывает строку, а не добавляет вторую."""
    report = _make_report([_make_card()], [], brief=brief)
    request_id, _, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    with client:
        client.post(
            f"/requests/{request_id}/summary",
            data={"recommendation": "Первая правка.", "caveats": ""},
            auth=_AUTH,
        )
        client.post(
            f"/requests/{request_id}/summary",
            data={"recommendation": "Вторая правка.", "caveats": "Новая оговорка"},
            auth=_AUTH,
        )

    rows = asyncio.run(_summary_override_rows(db_session_factory, request_id))
    assert len(rows) == 1
    assert rows[0].recommendation == "Вторая правка."
    assert rows[0].caveats == ["Новая оговорка"]


def test_summary_post_пустая_рекомендация_отклоняется(client, db_session_factory, brief):
    """Пустая рекомендация — отказ 400, строка не пишется."""
    report = _make_report([_make_card()], [], brief=brief)
    request_id, _, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    with client:
        response = client.post(
            f"/requests/{request_id}/summary",
            data={"recommendation": "   ", "caveats": ""},
            auth=_AUTH,
        )

    assert response.status_code == 400
    assert "Рекомендация не может быть пустой" in response.text
    assert asyncio.run(_summary_override_rows(db_session_factory, request_id)) == []


def test_summary_post_несуществующий_запрос_отклоняется(client, db_session_factory):
    """Запроса нет в базе — человеческий отказ 404, а не 500."""
    with client:
        response = client.post(
            "/requests/999999/summary",
            data={"recommendation": "Что-то", "caveats": ""},
            auth=_AUTH,
        )

    assert response.status_code == 404


def test_summary_post_требует_авторизацию(client, db_session_factory, brief):
    """Критерий 5: маршрут правки сводки закрыт Basic auth наравне с остальными."""
    report = _make_report([_make_card()], [], brief=brief)
    request_id, _, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    with client:
        response = client.post(
            f"/requests/{request_id}/summary",
            data={"recommendation": "Что-то", "caveats": ""},
        )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Basic"
