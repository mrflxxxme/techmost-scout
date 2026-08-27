"""Дыры покрытия аудита Э4 (правки П-11, П-13) на экране ревью эксперта.

Отдельный файл, а не дополнение `tests/test_web_review.py`: тот файл уже у
предела в 500 строк (страж `tests/test_tests_layout.py`), новые тесты сюда
не влезают без ужимания существующих до нечитаемости.

Фикстуры — та же форма, что в `tests/test_web_review.py` (временная SQLite на
`tmp_path`, подмена `app.db.session_scope`): сеть не задействована
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import main
from app.config import get_settings
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import Verdict as VerdictRow
from app.schemas import Evidence, Report, ReportSummary, SolutionCard

_AUTH = ("scout", "parol123")


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'review-coverage-test.db'}", future=True)
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
    """Подмена `session_scope` и данных доступа под временную базу запроса."""
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


async def _seed(session_factory, *, report: Report, status: RequestStatus) -> tuple[int, list[int]]:
    """Кладёт запрос и карточки основной секции, возвращает id запроса и карточек по порядку."""
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

        ids: list[int] = []
        for position, card in enumerate(report.cards):
            card_row = SolutionCardRow(
                request_id=request_id,
                position=position,
                is_contest=False,
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
        await session.flush()
        card_ids = [int(r.id) for r in ids]
    return request_id, card_ids


def test_summary_post_экранирует_html_в_строке_оговорок(client, db_session_factory, brief):
    """П-11: строка оговорок фрагмента `edit_summary` экранирует HTML целиком.

    Аудит нашёл, что мутация «убрать `escape()` у рекомендации» уже ловится
    (`test_summary_и_экран_ревью_честны_с_экспертом` в `test_web_review.py`),
    а «убрать `escape()` у строки оговорок» (там, где оговорки склеиваются
    через `'; '.join(caveats_list)`) переживает весь набор. Живой POST с
    `<img src=x onerror=alert(1)>` — тот же вектор, каким проверял аудит.
    """
    report = _make_report([_make_card()], [], brief=brief)
    request_id, _ = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))

    payload = "<img src=x onerror=alert(1)>"
    with client:
        response = client.post(
            f"/requests/{request_id}/summary",
            data={"recommendation": "Вариант 1.", "caveats": payload},
            auth=_AUTH,
        )

    assert response.status_code == 200
    assert payload not in response.text
    assert "<img" not in response.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in response.text


def test_скрытая_карточка_перед_видимой_не_сдвигает_клиентский_номер(client, db_session_factory, brief):
    """П-13: клиентская нумерация считается заново, скрытые карточки в неё не входят.

    Сегодняшняя фикстура прошлого теста ставит скрытую карточку ПОСЛЕ
    видимой, и мутация «нумеровать скрытые тоже» там не проявляется (обе
    карточки в этом порядке дают одинаковый результат). Здесь скрытая
    карточка идёт ПЕРВОЙ — если мутант считает и её, видимая вторая карточка
    получит клиентский номер №2 вместо верного №1.
    """
    cards = [_make_card(title="Отклонённый"), _make_card(title="Одобренный")]
    report = _make_report(cards, [], brief=brief)
    request_id, card_ids = asyncio.run(_seed(db_session_factory, report=report, status=RequestStatus.DRAFTED))
    rejected_id, _visible_id = card_ids

    async def _reject_first() -> None:
        async with db_session_factory() as session:
            session.add(VerdictRow(card_id=rejected_id, reviewer_name="И.И.", decision="reject"))

    asyncio.run(_reject_first())

    with client:
        response = client.get(f"/requests/{request_id}/review", auth=_AUTH)

    assert response.status_code == 200
    assert "Вариант 1 «Отклонённый» — клиенту не уедет: отклонена экспертом." in response.text
    assert "Вариант 2 «Одобренный» — уедет клиенту как вариант №1." in response.text
