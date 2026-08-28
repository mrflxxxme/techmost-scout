"""Стенд-тесты пополевых ворот печати сводки через настоящий `build_export_view`
(БЛОК-5 финального аудита Э4, РЕШ-211, РЕШ-212).

Вынесены из `tests/test_summary_overrides.py`, чтобы не растить его сверх
500 строк (страж `tests/test_tests_layout.py`) — свой стенд БД на временном
файле SQLite, сеть не задействована (инвариант №7).

В конце файла — тесты Э4-П14 (совет 1 ревью Э4-П13): `export.view.
_reindex_after_filter` больше не держит собственного сравнения с `REJECT`, а
берёт решение у `verdicts.hidden_reason` тем же вызовом, что и `visible_report`
без `require_evidence` (операторский вид, `ExportView.card_ids`/`rework_keys`,
не `client_report`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from docx import Document
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.export import view as view_module
from app.export.docx import export_docx
from app.export.markdown import SUMMARY_NOT_PRINTABLE_NOTICE
from app.export.pdf import render_print_html
from app.export.view import ExportView, build_export_view, caveats_printable, recommendation_printable
from app.models import Base, RequestStatus
from app.models import Request as RequestRow
from app.models import SolutionCard as SolutionCardRow
from app.models import SummaryOverride as SummaryOverrideRow
from app.models import Verdict as VerdictRow
from app.schemas import Evidence, Report, ReportSummary, SolutionCard
from app.schemas import Verdict as VerdictDecision


def _make_card(**overrides) -> SolutionCard:
    fields = {
        "title": "Регенерация катализатора добавками молибдена",
        "essence": "Продление срока службы катализатора за счёт присадки.",
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "next_step": "Запросить образец у разработчика.",
        # Подтверждение по умолчанию есть у всех карточек этого файла — здесь
        # проверяется фильтр по reject/несовпадению строк (РЕШ-212), а не по
        # `evidence` (тот отдельно закрыт БЛОК-6-тестом ниже и tests/test_verdicts.py).
        "evidence": [Evidence(claim="Испытан на действующей установке", url="https://example.org/a")],
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _make_report(**overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": [_make_card()],
        "contest_cards": [],
        "summary": ReportSummary(
            recommendation="Первым берём вариант 1.",
            caveats=["В карточке 3 нет данных о TRL."],
        ),
        "generated_at": datetime(2026, 8, 27, 9, 0),
    }
    fields.update(overrides)
    return Report(**fields)


@pytest.fixture
async def db_session_factory(tmp_path: Path):
    """Фабрика сессий на временном файле SQLite (тот же приём, что и в
    `test_summary_overrides.py` — второй фикстуры для той же формы нет
    смысла заводить общей: файлы независимы, дублирование дешевле связывания)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'summary-field-gate-test.db'}", future=True)
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


async def _seed_request(session_factory, *, report: Report) -> tuple[int, dict[tuple[bool, int], int]]:
    """Строка `requests` с готовым `report_json` и строками `solution_cards`."""
    async with session_factory() as session:
        row = RequestRow(
            raw_text="катализатор теряет активность раньше срока",
            status=RequestStatus.DRAFTED,
            report_json=report.model_dump_json(),
        )
        session.add(row)
        await session.flush()
        request_id = int(row.id)
        card_rows: dict[tuple[bool, int], SolutionCardRow] = {}

        def _add_section(cards: list[SolutionCard], *, is_contest: bool) -> None:
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
                    confidence=card.confidence.value,
                )
                session.add(card_row)
                card_rows[(is_contest, position)] = card_row

        _add_section(report.cards, is_contest=False)
        _add_section(report.contest_cards, is_contest=True)
        await session.flush()
        card_ids = {key: int(card_row.id) for key, card_row in card_rows.items()}
    return request_id, card_ids


async def _add_verdict(session_factory, card_id: int, decision: VerdictDecision) -> None:
    async with session_factory() as session:
        session.add(VerdictRow(card_id=card_id, reviewer_name="Иван Иванов", decision=decision.value))


async def _build_view(session_factory, request_id: int) -> ExportView:
    async with session_factory() as session:
        return await build_export_view(session, request_id)


def test_правка_только_оговорок_печатает_независимо_блок5(db_session_factory, brief):
    """Критерий 1, воспроизведение БЛОК-5 финального аудита стендом через
    настоящий `build_export_view`: две карточки, карточка 1 отклонена, эксперт
    правит только оговорки. `recommendation_edited` обязан остаться `False`
    (система честно помнит «не правил»), а нетронутая рекомендация модели —
    «Первым берём вариант 1 — «Первый».» — не должна попасть ни в
    `recommendation_printable`, ни в реальный печатный HTML, хотя карточки
    «Первый» в документе уже нет."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title="Первый"), _make_card(title="Второй")],
        summary=ReportSummary(
            recommendation="Первым берём вариант 1 — «Первый».",
            caveats=["Оговорка модели про карточку 2."],
            comparison_rows=[{"option": "Первый"}, {"option": "Второй"}],
        ),
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    async def _seed_override() -> None:
        async with db_session_factory() as session:
            session.add(SummaryOverrideRow(request_id=request_id, caveats=["Оговорка эксперта: проверил лично."]))

    asyncio.run(_seed_override())

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.recommendation_edited is False
    assert view.caveats_edited is True

    printed_cards = len(view.report.cards) + len(view.report.contest_cards)
    assert printed_cards == 1
    assert recommendation_printable(view, printed_cards) is False
    assert caveats_printable(view, printed_cards) is True

    html = render_print_html(view)
    assert "Первым берём вариант 1 — «Первый»." not in html
    assert "Первый" not in html  # отклонённой карточки в документе нет вовсе
    assert "Оговорка эксперта: проверил лично." in html
    assert SUMMARY_NOT_PRINTABLE_NOTICE in html


def test_правка_только_рекомендации_печатает_независимо_зеркало(db_session_factory, brief):
    """Зеркало предыдущего теста (воспроизведение 2 БЛОК-5, симметрия):
    правит только рекомендацию — проза оговорок модели не печатается, хотя
    рекомендация эксперта на месте."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title="Первый"), _make_card(title="Второй")],
        summary=ReportSummary(
            recommendation="Первым берём вариант 1 — «Первый».",
            caveats=["В карточке 1 риск не подтверждён."],
        ),
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    async def _seed_override() -> None:
        async with db_session_factory() as session:
            session.add(SummaryOverrideRow(request_id=request_id, recommendation="Начать со второго варианта."))

    asyncio.run(_seed_override())

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.recommendation_edited is True
    assert view.caveats_edited is False

    html = render_print_html(view)
    assert "Начать со второго варианта." in html
    assert "В карточке 1 риск не подтверждён." not in html


def test_comparison_table_hidden_при_несовпадении_строк(db_session_factory, brief):
    """РЕШ-212: три карточки, четыре строки сравнения от модели (запасной путь
    или сбойный ответ), карточка 1 отклонена — сопоставить строки с карточками
    нечем, `comparison_table_hidden` поднимается и таблица приходит пустой."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title=f"Карточка {n}") for n in range(1, 4)],
        summary=ReportSummary(
            recommendation="Рекомендация.",
            comparison_rows=[{"option": f"В{n}"} for n in range(1, 5)],
        ),
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.comparison_table_hidden is True
    assert view.report.summary.comparison_rows == []


def test_comparison_table_not_hidden_без_отклонений(db_session_factory, brief):
    """Обратная сторона: несовпадение строк с карточками есть, но ни одна
    карточка не отклонена — `_visible_summary` таблицу не трогает вовсе,
    флаг остаётся `False`."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title=f"Карточка {n}") for n in range(1, 4)],
        summary=ReportSummary(
            recommendation="Рекомендация.",
            comparison_rows=[{"option": f"В{n}"} for n in range(1, 5)],
        ),
    )
    request_id, _card_ids = asyncio.run(_seed_request(db_session_factory, report=report))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.comparison_table_hidden is False
    assert len(view.report.summary.comparison_rows) == 4


def test_comparison_table_not_hidden_с_конкурсной_секцией_без_отклонений(db_session_factory, brief):
    """П-18 аудита (мутация 1, адаптирована под код после Э4-П11): раньше
    `export.view._comparison_table_hidden` считал видимые позиции основной
    секции суммой `sum(... if not is_contest)` по уже пересчитанному
    `card_ids` — при непустой конкурсной секции мутация «считать все ключи»
    ломала охрану «ни одна карточка не отклонена — скрывать нечего». После
    П11 счёт идёт через `verdicts.visible_position_count(edited_report.cards,
    …)` — список карточек уже заведомо ограничен основной секцией, но эта
    охрана прежде не проверялась ни на одном сценарии с непустой конкурсной
    секцией. Три карточки основной секции без единого отклонения, конкурсная
    карточка с подтверждением рядом, четыре строки сравнения (несовпадение,
    как у запасной сводки) — таблица не должна помечаться спрятанной."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title=f"Карточка {n}") for n in range(1, 4)],
        contest_cards=[_make_card(title="Конкурсная карточка")],
        summary=ReportSummary(
            recommendation="Рекомендация.",
            comparison_rows=[{"option": f"В{n}"} for n in range(1, 5)],
        ),
    )
    request_id, _card_ids = asyncio.run(_seed_request(db_session_factory, report=report))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.comparison_table_hidden is False
    assert len(view.report.summary.comparison_rows) == 4


def test_comparison_table_hidden_когда_строк_меньше_чем_карточек(db_session_factory, brief):
    """П-18 аудита, мутация 2 (`export.view._comparison_table_hidden`:
    `len(rows) != cards` подменяется на `len(rows) > cards`) — случай «строк
    меньше, чем карточек» сегодня не закреплён ни одним тестом. Три карточки
    основной секции, две строки сравнения (запасная сводка короче обычного),
    первая карточка отклонена — сопоставить строку с конкретной карточкой
    нечем, таблица обязана быть помечена спрятанной, а не показана как есть."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title=f"Карточка {n}") for n in range(1, 4)],
        summary=ReportSummary(
            recommendation="Рекомендация.",
            comparison_rows=[{"option": f"В{n}"} for n in range(1, 3)],
        ),
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.comparison_table_hidden is True
    assert view.report.summary.comparison_rows == []


def test_comparison_table_not_hidden_когда_строк_нет_с_самого_начала(db_session_factory, brief):
    """Пустая сводка модели (без единой строки, например `fallback_summary`)
    не поднимает `comparison_table_hidden`, даже если карточка отклонена —
    там нечего было прятать с самого начала."""
    report = _make_report(
        brief=brief,
        cards=[_make_card(title="Первый"), _make_card(title="Второй")],
        summary=ReportSummary(recommendation="Рекомендация.", comparison_rows=[]),
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert view.comparison_table_hidden is False


def test_блок6_бессылочная_карточка_не_печатает_свою_строку_сравнения(db_session_factory, brief, tmp_path):
    """БЛОК-6 финального аудита, воспроизведение на живом маршруте: оператор
    снял единственное подтверждение карточки (`POST /cards/{id}/edit
    evidence_removed=…`, здесь — карточка изначально без `evidence`, тот же
    итог). Признак видимости — один (РЕШ-213): карточка без строки сравнения
    убрана из PDF и DOCX одним и тем же проходом `verdicts.visible_report`, а
    операторская страница отчёта (`view.report`, РЕШ-204/205) её по-прежнему
    показывает — экран для эксперта этой задачей не меняется (критерий 3)."""
    kept = _make_card(title="Испытанный вариант")
    bare = _make_card(title="Присадка молибдена", evidence=[])
    report = _make_report(
        brief=brief,
        cards=[kept, bare],
        summary=ReportSummary(
            recommendation="Начать с варианта 1.",
            comparison_rows=[
                {"option": "Испытанный вариант", "gives": "Испытан на действующей установке"},
                {"option": "Присадка молибдена", "gives": "Испытан на действующей установке"},
            ],
        ),
    )
    request_id, _card_ids = asyncio.run(_seed_request(db_session_factory, report=report))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    # Критерий 3: операторская страница отчёта видит обе карточки, включая
    # бессылочную, — `view.report` эта задача не трогает.
    assert [c.title for c in view.report.cards] == ["Испытанный вариант", "Присадка молибдена"]

    # Критерий 1/2: клиентский вид видит только подтверждённую карточку и её
    # строку — без бессылочной и без второй строки таблицы.
    assert [c.title for c in view.client_report.cards] == ["Испытанный вариант"]
    assert view.client_report.summary.comparison_rows == [
        {"option": "Испытанный вариант", "gives": "Испытан на действующей установке"}
    ]

    html = render_print_html(view)
    assert "Присадка молибдена" not in html
    assert html.count("<td>Испытан на действующей установке</td>") == 1

    out_path = tmp_path / "report.docx"
    export_docx(view, out_path)
    document = Document(str(out_path))
    text = "\n".join(p.text for p in document.paragraphs)
    assert "Присадка молибдена" not in text
    # Заголовок + ровно одна строка сравнения (без строки бессылочной карточки).
    assert len(document.tables[0].rows) == 2


def test_reindex_after_filter_reject_rework_evidence_и_конкурсная_секция(db_session_factory, brief):
    """Э4-П14, критерий 3: `_reindex_after_filter` больше не считает видимость
    сама, а берёт её у `verdicts.hidden_reason` — стенд с четырьмя условиями
    сразу (reject, rework, пустой `evidence`, конкурсная секция) на настоящем
    `build_export_view`.

    Ключевая проверка — согласование: `view.card_ids`/`rework_keys` обязаны
    остаться режимом операторской страницы (`require_evidence=False`, только
    reject), поэтому карточка без единого подтверждения из `card_ids` НЕ
    убирается (иначе правка оператора и пометка «на доработку» перестали бы
    попадать на свою позицию операторской страницы) — убирается она только из
    `client_report` (`require_evidence=True`, критерий 3 задачи Э4-П13)."""
    rejected = _make_card(title="Отклонённая")
    bare = _make_card(title="БезПодтверждений", evidence=[])
    reworked = _make_card(title="НаДоработку")
    plain = _make_card(title="Обычная")
    contest_rejected = _make_card(title="КонкурснаяОтклонённая")
    contest_plain = _make_card(title="КонкурснаяОбычная")
    report = _make_report(
        brief=brief,
        cards=[rejected, bare, reworked, plain],
        contest_cards=[contest_rejected, contest_plain],
    )
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 2)], VerdictDecision.REWORK))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(True, 0)], VerdictDecision.REJECT))

    view = asyncio.run(_build_view(db_session_factory, request_id))

    # Операторский вид (`view.report`/`card_ids`): только reject убран, порядок
    # сохранён, бессылочная карточка остаётся — тот же признак, что и раньше.
    assert [c.title for c in view.report.cards] == ["БезПодтверждений", "НаДоработку", "Обычная"]
    assert [c.title for c in view.report.contest_cards] == ["КонкурснаяОбычная"]
    assert view.card_ids == {
        (False, 0): card_ids[(False, 1)],  # БезПодтверждений
        (False, 1): card_ids[(False, 2)],  # НаДоработку
        (False, 2): card_ids[(False, 3)],  # Обычная
        (True, 0): card_ids[(True, 1)],  # КонкурснаяОбычная
    }
    # rework — второй, независимый признак: карточка «на доработку» видна
    # клиенту в `view.report`, пометка живёт только в `rework_keys`.
    assert view.rework_keys == {(False, 1)}

    # Клиентский вид (`view.client_report`): reject и пустой `evidence` убраны
    # одним проходом (require_evidence=True) — этой задачей не меняется.
    assert [c.title for c in view.client_report.cards] == ["НаДоработку", "Обычная"]
    assert [c.title for c in view.client_report.contest_cards] == ["КонкурснаяОбычная"]


def test_reindex_after_filter_зовёт_verdicts_hidden_reason_а_не_свою_копию(db_session_factory, brief, monkeypatch):
    """Э4-П14, критерий 4 (мутация «вернуть прямое сравнение с `REJECT`,
    игнорируя пустой `evidence`»): подменяем импортированную в `export.view`
    `hidden_reason` заглушкой «никогда не скрыто» и проверяем, что отклонённая
    карточка возвращается в `card_ids`.

    Если `_reindex_after_filter` держит свою копию сравнения с `REJECT` (как
    до этой задачи), подмена `hidden_reason` на это не повлияет — отклонённая
    карточка всё равно останется убрана, и эта проверка покраснеет. Она красна
    именно на мутации, которую называет критерий 4."""
    rejected = _make_card(title="Отклонённая")
    plain = _make_card(title="Обычная")
    report = _make_report(brief=brief, cards=[rejected, plain])
    request_id, card_ids = asyncio.run(_seed_request(db_session_factory, report=report))
    asyncio.run(_add_verdict(db_session_factory, card_ids[(False, 0)], VerdictDecision.REJECT))

    monkeypatch.setattr(view_module, "hidden_reason", lambda card, verdict, **kwargs: None)

    view = asyncio.run(_build_view(db_session_factory, request_id))

    assert [c.title for c in view.report.cards] == ["Обычная"]  # visible_report фильтрует независимо
    assert view.card_ids == {
        (False, 0): card_ids[(False, 0)],  # Отклонённая — вернулась под заглушкой hidden_reason
        (False, 1): card_ids[(False, 1)],  # Обычная
    }
    assert view.rework_keys == set()
