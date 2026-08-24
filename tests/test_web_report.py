"""Отчёт с карточками и правкой оператора (задача И-12, SPEC Э3-5).

Проверяется: страница собирается из `requests.report_json`, а не из
`solution_cards` (РЕШ-189 п. 4) — отдельный тест снимает строки карточек из
базы и убеждается, что страница всё равно открывается; пометка «ЧЕРНОВИК»
живёт до валидации и не снимается сохранением правки (РЕШ-189 п. 6); пустой
`evidence` даёт красную плашку и прячет стадию зрелости и TRL (инвариант №1);
правка идёт строго по белому списку `app.report_overrides.EDITABLE_FIELDS`
(РЕШ-148) — `confidence` и новый URL `evidence` правкой не меняются.

База — временный файл SQLite на `tmp_path` (та же форма, что у
`app.db.session_scope`), как в `tests/test_pipeline_resilience.py`: сеть в
тестах не задействована (инвариант №7), лишний движок приложения на
`:memory:` не трогается ни одним тестом файла.
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
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.schemas import Evidence, MaturityStage, Report, ReportSummary, SolutionCard

_AUTH = ("scout", "parol123")


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite — форма `app.db.session_scope`."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'report-test.db'}", future=True)
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
    """Маршруты идут через `Depends(get_session)` → `app.db.session_scope`,
    поэтому подмена этого имени и есть подключение временной базы — тот же
    приём, что в `tests/test_pipeline_resilience.py`."""
    monkeypatch.setattr("app.db.session_scope", db_session_factory)

    async def fake_init_db() -> None:
        """Таблицы временной базы уже созданы фикстурой — второй раз не нужно,
        а настоящий `init_db` работал бы с чужим (общим тестовым) движком."""
        return None

    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main.settings, "web_auth_user", _AUTH[0])
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr(_AUTH[1]))
    yield db_session_factory


def _make_card(**overrides) -> SolutionCard:
    fields = {
        "title": "Катализатор с присадкой молибдена",
        "essence": "Присадка продлевает межрегенерационный цикл катализатора гидроочистки.",
        "maturity_stage": MaturityStage.PILOT,
        "trl_estimate": 6,
        "developer_org": "Институт катализа",
        "applicability": "Закрывает задачу продления срока службы катализатора.",
        "next_step": "Запросить протокол испытаний.",
        "evidence": [Evidence(claim="Испытан на действующей установке", url="https://example.org/a")],
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _make_report(cards: list[SolutionCard], **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": cards,
        "contest_cards": [],
        "summary": ReportSummary(
            recommendation="Начать с варианта №1.",
            comparison_rows=[{"Вариант": "Катализатор с присадкой молибдена", "Зрелость": "пилот"}],
        ),
        "generated_at": datetime(2026, 8, 12, 10, 0),
    }
    fields.update(overrides)
    return Report(**fields)


async def _seed_request(
    session_factory,
    *,
    report: Report,
    status: RequestStatus = RequestStatus.DRAFTED,
    with_card_rows: bool = True,
) -> int:
    """Строка `requests` с готовым `report_json` и (по умолчанию) строками
    `solution_cards` того же состава — для теста «страница без строк карточек»
    флаг выключается."""
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
        if with_card_rows:
            for position, card in enumerate(report.cards):
                session.add(
                    SolutionCardRow(
                        request_id=request_id,
                        position=position,
                        is_contest=False,
                        title=card.title,
                        essence=card.essence,
                        maturity_stage=card.maturity_stage.value,
                        trl_estimate=card.trl_estimate,
                        developer_org=card.developer_org,
                        applicability=card.applicability,
                        next_step=card.next_step,
                        evidence=[item.model_dump(mode="json") for item in card.evidence],
                        confidence=card.confidence.value,
                    )
                )
    return request_id


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def test_report_renders_from_report_json_without_solution_cards_rows(client, db_session_factory, brief):
    """Доказательство источника данных (РЕШ-189 п. 4): строк `solution_cards`
    нет вовсе, а страница всё равно собирается из `report_json`."""
    report = _make_report([_make_card()], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report, with_card_rows=False))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert response.status_code == 200
    assert "Катализатор с присадкой молибдена" in response.text
    assert "style=" not in response.text


def test_draft_banner_present_at_drafted_status(client, db_session_factory, brief):
    report = _make_report([_make_card()], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "ЧЕРНОВИК — не проверено экспертом" in response.text


def test_draft_banner_survives_edit(client, db_session_factory, brief):
    """РЕШ-189 п. 6: сохранение правки не снимает пометку «черновик»."""
    report = _make_report([_make_card()], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    async def _card_id() -> int:
        async with db_session_factory() as session:
            row = (await session.execute(select(SolutionCardRow))).scalars().one()
            return int(row.id)

    card_id = asyncio.run(_card_id())

    with client:
        client.post(f"/cards/{card_id}/edit", data={"title": "Новое название"}, auth=_AUTH)
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "ЧЕРНОВИК — не проверено экспертом" in response.text
    assert "Новое название" in response.text


def test_empty_evidence_hides_maturity_and_shows_danger_notice(client, db_session_factory, brief):
    """Инвариант №1: без подтверждений — плашка, без стадии зрелости и TRL."""
    card = _make_card(evidence=[], trl_estimate=6, maturity_stage=MaturityStage.PILOT)
    report = _make_report([card], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "Нельзя показывать клиенту: нет подтверждений" in response.text
    assert "TRL" not in response.text
    assert "прошла пилот на объекте" not in response.text


def test_edit_title_persists_and_report_json_unchanged(client, db_session_factory, brief):
    """Правка видна после повторного запроса страницы; машинный `report_json`
    (то, что реально собрал пайплайн) при этом не меняется."""
    report = _make_report([_make_card()], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    async def _card_id_and_json() -> tuple[int, str]:
        async with db_session_factory() as session:
            card_row = (await session.execute(select(SolutionCardRow))).scalars().one()
            request_row = (await session.execute(select(RequestRow).where(RequestRow.id == request_id))).scalars().one()
            return int(card_row.id), request_row.report_json

    card_id, original_json = asyncio.run(_card_id_and_json())

    with client:
        client.post(f"/cards/{card_id}/edit", data={"title": "Катализатор нового поколения"}, auth=_AUTH)
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "Катализатор нового поколения" in response.text

    async def _current_json() -> str:
        async with db_session_factory() as session:
            request_row = (await session.execute(select(RequestRow).where(RequestRow.id == request_id))).scalars().one()
            return request_row.report_json

    assert asyncio.run(_current_json()) == original_json


def test_edit_ignores_confidence_and_cannot_add_evidence(client, db_session_factory, brief):
    """РЕШ-148: `confidence` не принимается маршрутом вовсе, добавить новую
    строку `evidence` через форму нельзя — в обработчике для этого нет параметра."""
    report = _make_report([_make_card()], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    async def _card_id() -> int:
        async with db_session_factory() as session:
            row = (await session.execute(select(SolutionCardRow))).scalars().one()
            return int(row.id)

    card_id = asyncio.run(_card_id())

    with client:
        response = client.post(
            f"/cards/{card_id}/edit",
            data={
                "confidence": "high",
                "evidence": "https://attacker.example/fake",
                "requirement_checks": "все подтверждено",
            },
            auth=_AUTH,
        )
        report_response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert response.status_code in (200, 303)
    assert "attacker.example" not in report_response.text
    assert "Уверенность: средняя" in report_response.text  # уверенность не изменилась


def test_edit_removes_evidence_line(client, db_session_factory, brief):
    card = _make_card(
        evidence=[
            Evidence(claim="Испытан на установке №1", url="https://example.org/first"),
            Evidence(claim="Испытан на установке №2", url="https://example.org/second"),
        ]
    )
    report = _make_report([card], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    async def _card_id() -> int:
        async with db_session_factory() as session:
            row = (await session.execute(select(SolutionCardRow))).scalars().one()
            return int(row.id)

    card_id = asyncio.run(_card_id())

    with client:
        client.post(
            f"/cards/{card_id}/edit",
            data={"evidence_removed": "https://example.org/first"},
            auth=_AUTH,
        )
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "Испытан на установке №1" not in response.text
    assert "Испытан на установке №2" in response.text


def test_comparison_table_and_recommendation_from_summary(client, db_session_factory, brief):
    report = _make_report([_make_card()], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "Начать с варианта №1." in response.text
    assert "Вариант" in response.text and "Зрелость" in response.text


def test_limitations_block_matches_limitations_lines(client, db_session_factory, brief):
    """Критерий 2 задачи И-12: строки блока на странице — те же самые, что
    отдаёт `app.export.markdown.limitations_lines`, которой пользуется и
    `render_markdown` (РЕШ-189 п. 5). Один источник текста на два выхода."""
    from app.export.markdown import limitations_lines

    report = _make_report(
        [_make_card()],
        brief=brief,
        sources_unavailable=["Роспатент"],
        summary=ReportSummary(recommendation="Начать с варианта №1.", caveats=["Уточнения по задаче не собраны."]),
    )
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert "Ограничения этого поиска" in response.text
    for line in limitations_lines(report):
        assert line in response.text


def test_limitations_lines_keep_separator_between_sources_and_caveats(brief):
    """Регрессия ревью цикла 1 (инвариант №8): вынос блока «Ограничения этого
    поиска» в `limitations_lines` обязан быть переносом без изменений —
    пустая строка-разделитель между абзацем «Не ответили источники…» и
    списком оговорок должна остаться на месте, когда оба присутствуют
    одновременно. Раньше (цикл 1) она пропадала, и `render_markdown` менял
    байтовый вывод при том же входе. Тест — на самой функции `render_markdown`,
    `tests/test_export_markdown.py` не тронут (критерий 2 задачи И-12)."""
    from app.export.markdown import render_markdown

    report = _make_report(
        [_make_card()],
        brief=brief,
        sources_unavailable=["Роспатент"],
        summary=ReportSummary(recommendation="Начать с варианта №1.", caveats=["Оговорка 1", "Оговорка 2"]),
    )

    text = render_markdown(report)

    assert (
        "Не ответили источники: Роспатент. Выдача могла быть полнее — стоит повторить запрос позже.\n\n- Оговорка 1"
        in text
    ), "разделитель между абзацем и списком оговорок обязан остаться пустой строкой"


def test_report_not_found_shows_human_text_without_raw_exception(client, db_session_factory, brief):
    """Строка `requests` есть, но `report_json` пуст (прогон не дошёл до
    сборки отчёта или отчёт не читается схемой) — страница обязана показать
    человеческую фразу без текста внутреннего исключения (замечание ревью
    цикла 2, РЕШ-143 п. 7): в тексте `ReportNotFoundError` есть слова
    `report_json` и «схемой», которых на странице быть не должно."""

    async def _seed_without_report() -> int:
        async with db_session_factory() as session:
            row = RequestRow(raw_text="задача без готового отчёта", status=RequestStatus.SEARCHING, report_json=None)
            session.add(row)
            await session.flush()
            return int(row.id)

    request_id = asyncio.run(_seed_without_report())

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert response.status_code == 404
    assert "Подбор решений — запрос" in response.text
    assert "report_json" not in response.text
    assert "схемой" not in response.text
    assert "Traceback" not in response.text
    assert "Отчёт по этому запросу ещё не готов" in response.text


def test_evidence_links_are_clickable(client, db_session_factory, brief):
    report = _make_report([_make_card()], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert 'href="https://example.org/a"' in response.text


def test_edit_with_garbage_request_does_not_500(client, db_session_factory, brief):
    """Пустая форма и мусорное поле не роняют обработчик 500-й (замечание
    ревью И-7): в обработчике нет параметра под произвольные имена, лишнее
    FastAPI отбрасывает молча."""
    report = _make_report([_make_card()], brief=brief)
    asyncio.run(_seed_request(db_session_factory, report=report))

    async def _card_id() -> int:
        async with db_session_factory() as session:
            row = (await session.execute(select(SolutionCardRow))).scalars().one()
            return int(row.id)

    card_id = asyncio.run(_card_id())

    with client:
        response = client.post(f"/cards/{card_id}/edit", data={"мусорное_поле": "что угодно"}, auth=_AUTH)

    assert response.status_code < 500


def test_edit_unknown_card_returns_404(client, db_session_factory, brief):
    report = _make_report([_make_card()], brief=brief)
    asyncio.run(_seed_request(db_session_factory, report=report))

    with client:
        response = client.post("/cards/999999/edit", data={"title": "x"}, auth=_AUTH)

    assert response.status_code == 404


def test_unknown_request_returns_404(client, db_session_factory):
    with client:
        response = client.get("/requests/999999/report", auth=_AUTH)

    assert response.status_code == 404


# ── П-5 аудита Э3: страж автоэкранирования Jinja ─────────────────────────
#
# Мутация autoescape=False прошла все 960 тестов волны Э3 (аудит, находка
# П-5) — контракт держался умолчанием, а не проверкой. Тесты ниже ловят обе
# стороны: сам флаг окружения шаблонов и фактический escape поля карточки,
# которое печатается без фильтра |safe.


def test_templates_env_autoescape_is_enabled_for_html():
    """Флаг окружения Jinja — `select_autoescape`, включён для html-файлов.
    Первая линия защиты: если кто-то заменит `Jinja2Templates(...)` на
    окружение с `autoescape=False`, этот тест ловит это до второго теста."""
    from app.main import templates

    assert templates.env.autoescape("report.html") is True
    assert templates.env.autoescape(None) is True


def test_card_title_with_script_tag_is_escaped_in_report_html(client, db_session_factory, brief):
    """Заголовок карточки с `<script>` печатается экранированным текстом —
    сырой тег в ответе не встречается ни разу. Ловит и подмену `autoescape`,
    и появление `|safe`/`Markup` на поле карточки."""
    payload = "<script>alert(1)</script>"
    report = _make_report([_make_card(title=payload)], brief=brief)
    request_id = asyncio.run(_seed_request(db_session_factory, report=report))

    with client:
        response = client.get(f"/requests/{request_id}/report", auth=_AUTH)

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
