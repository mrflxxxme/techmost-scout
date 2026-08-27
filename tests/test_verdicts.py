"""Проверки слоя вердиктов (Э4-И1, РЕШ-196…РЕШ-198).

Слой чистый: строки `app.models.Verdict` собираются вручную, без сессии и без
базы (инвариант №7). Решение сравнивается со `schemas.Verdict` — второго
перечисления в тестах нет.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.export.markdown import REVIEWED_FOOTER
from app.models import Verdict as VerdictRow
from app.schemas import Report, ReportSummary, SolutionCard
from app.schemas import Verdict as VerdictDecision
from app.verdicts import all_cards_reviewed, effective_verdicts, signature_line, visible_report

_NOW = datetime(2026, 8, 27, 12, 0, 0)


def _row(card_id: int, decision: VerdictDecision, *, id: int, created_at: datetime | None = None) -> VerdictRow:
    """Строка вердикта — конструируется напрямую, без записи в базу."""
    return VerdictRow(
        id=id,
        card_id=card_id,
        reviewer_name="Иван Иванов",
        decision=decision.value,
        created_at=created_at or _NOW,
    )


def _make_card(**overrides) -> SolutionCard:
    fields = {
        "title": "Регенерация катализатора добавками молибдена",
        "essence": "Продление срока службы катализатора за счёт присадки.",
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "next_step": "Запросить образец у разработчика.",
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _make_report(cards: list[SolutionCard], contest_cards: list[SolutionCard] | None = None, **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": cards,
        "contest_cards": contest_cards or [],
        "summary": ReportSummary(recommendation="Начать с варианта №1."),
        "generated_at": _NOW,
    }
    fields.update(overrides)
    return Report(**fields)


def test_действующий_вердикт_последний_по_времени():
    """Два вердикта по одной карточке подряд — действующий тот, что позже."""
    rows = [
        _row(1, VerdictDecision.APPROVE, id=1, created_at=_NOW),
        _row(1, VerdictDecision.REJECT, id=2, created_at=_NOW + timedelta(seconds=5)),
    ]

    effective = effective_verdicts(rows)

    assert effective[1].decision == VerdictDecision.REJECT.value


def test_при_равном_времени_побеждает_больший_id():
    """Вставки одной транзакции получают одинаковый `created_at` — тай-брейк по `id`."""
    rows = [
        _row(1, VerdictDecision.APPROVE, id=5, created_at=_NOW),
        _row(1, VerdictDecision.REJECT, id=3, created_at=_NOW),
    ]

    effective = effective_verdicts(rows)

    assert effective[1].id == 5
    assert effective[1].decision == VerdictDecision.APPROVE.value


def test_effective_verdicts_на_пустом_списке():
    """Без вердиктов вообще — пустой словарь, а не ошибка."""
    assert effective_verdicts([]) == {}


def test_visible_report_убирает_reject_из_обеих_секций_и_сохраняет_порядок(brief):
    """Reject-карточка пропадает из `cards` и `contest_cards`, остальные — в исходном порядке."""
    kept_main = _make_card(title="Оставленная в основной секции")
    rejected_main = _make_card(title="Отклонённая в основной секции")
    kept_contest = _make_card(title="Оставленная в конкурсной секции")
    rejected_contest = _make_card(title="Отклонённая в конкурсной секции")
    report = _make_report(
        [rejected_main, kept_main],
        contest_cards=[kept_contest, rejected_contest],
        brief=brief,
    )
    card_ids = {(False, 0): 10, (False, 1): 11, (True, 0): 20, (True, 1): 21}
    effective = {10: _row(10, VerdictDecision.REJECT, id=1), 21: _row(21, VerdictDecision.REJECT, id=2)}

    result = visible_report(report, card_ids, effective)

    assert [card.title for card in result.cards] == ["Оставленная в основной секции"]
    assert [card.title for card in result.contest_cards] == ["Оставленная в конкурсной секции"]


def test_visible_report_не_убирает_rework(brief):
    """Вердикт `rework` не выкидывает карточку из клиентского вида."""
    card = _make_card(title="На доработку")
    report = _make_report([card], brief=brief)
    card_ids = {(False, 0): 1}
    effective = {1: _row(1, VerdictDecision.REWORK, id=1)}

    result = visible_report(report, card_ids, effective)

    assert [c.title for c in result.cards] == ["На доработку"]


def test_visible_report_не_добивает_выдачу(brief):
    """После фильтра карточек меньше исходного числа — новых не ищем и не подставляем."""
    approved = _make_card(title="Единственная одобренная")
    rejected = _make_card(title="Отклонённая")
    report = _make_report([approved, rejected], brief=brief)
    card_ids = {(False, 0): 1, (False, 1): 2}
    effective = {2: _row(2, VerdictDecision.REJECT, id=1)}

    result = visible_report(report, card_ids, effective)

    assert len(result.cards) == 1
    assert result.cards[0].title == "Единственная одобренная"


def test_visible_report_карточка_без_вердикта_остаётся(brief):
    """Карточка, для которой ещё нет вердикта, видна как обычно."""
    card = _make_card(title="Без вердикта")
    report = _make_report([card], brief=brief)

    result = visible_report(report, {(False, 0): 1}, {})

    assert [c.title for c in result.cards] == ["Без вердикта"]


def test_visible_report_фильтрует_строку_сравнения_вместе_с_карточкой(brief):
    """Критерий 1: 4 карточки, вторая — reject → 3 карточки и 3 строки, порядок сохранён."""
    cards = [_make_card(title=f"Вариант {n}") for n in range(4)]
    rows = [{"Вариант": f"строка-{n}"} for n in range(4)]
    report = _make_report(cards, brief=brief, summary=ReportSummary(comparison_rows=rows, recommendation="R"))
    card_ids = {(False, n): n + 1 for n in range(4)}
    effective = {2: _row(2, VerdictDecision.REJECT, id=1)}  # id=2 → карточка на позиции 1 (вторая)

    result = visible_report(report, card_ids, effective)

    assert [c.title for c in result.cards] == ["Вариант 0", "Вариант 2", "Вариант 3"]
    assert result.summary.comparison_rows == [
        {"Вариант": "строка-0"},
        {"Вариант": "строка-2"},
        {"Вариант": "строка-3"},
    ]


def test_visible_report_не_ловится_на_фильтр_по_позиции_после_фильтра_карточек(brief):
    """Критерий 5 (адверсариальный): reject у третьей карточки (индекс 2), а не второй.

    Мутация «фильтровать строки по позиции уже отфильтрованного списка карточек»
    для этого набора данных даёт другой результат: у неё останется 2 строки
    (`строка-0`, `строка-1`), а не 3 (`строка-0`, `строка-1`, `строка-3`) — она
    по ошибке смотрит вердикт по новой (после фильтра) позиции строки, а не по
    исходной позиции отклонённой карточки.
    """
    cards = [_make_card(title=f"Вариант {n}") for n in range(4)]
    rows = [{"Вариант": f"строка-{n}"} for n in range(4)]
    report = _make_report(cards, brief=brief, summary=ReportSummary(comparison_rows=rows, recommendation="R"))
    card_ids = {(False, n): n + 1 for n in range(4)}
    effective = {3: _row(3, VerdictDecision.REJECT, id=1)}  # id карточки на позиции 2

    result = visible_report(report, card_ids, effective)

    assert [c.title for c in result.cards] == ["Вариант 0", "Вариант 1", "Вариант 3"]
    assert result.summary.comparison_rows == [
        {"Вариант": "строка-0"},
        {"Вариант": "строка-1"},
        {"Вариант": "строка-3"},
    ]


def test_visible_report_несовпадение_числа_строк_с_reject_прячет_таблицу(brief):
    """Критерий 1 (воспроизведение аудита, правка П-7): 3 карточки, 4 строки от
    модели, карточка 1 отклонена — сопоставить строки с карточками нечем,
    печатать неотфильтрованную таблицу нельзя (в ней уехала бы строка
    отклонённой карточки), поэтому таблица не печатается вовсе.
    """
    cards = [_make_card(title=f"Карточка {n}") for n in range(3)]
    rows = [{"Вариант": f"строка-{n}"} for n in range(4)]  # запасная сводка с другим числом строк
    report = _make_report(cards, brief=brief, summary=ReportSummary(comparison_rows=rows, recommendation="R"))
    card_ids = {(False, n): n + 1 for n in range(3)}
    effective = {1: _row(1, VerdictDecision.REJECT, id=1)}  # карточка 0 отклонена

    result = visible_report(report, card_ids, effective)

    assert [c.title for c in result.cards] == ["Карточка 1", "Карточка 2"]
    assert result.summary.comparison_rows == []


def test_visible_report_несовпадение_числа_строк_без_reject_не_трогает_таблицу(brief):
    """Критерий 2: несовпадение числа строк без единого reject таблицу не трогает —
    отклонённых карточек нет, скрывать в таблице нечего.
    """
    cards = [_make_card(title="Единственная")]
    rows = [{"Вариант": "A"}, {"Вариант": "B"}]  # запасная сводка с другим числом строк
    report = _make_report(cards, brief=brief, summary=ReportSummary(comparison_rows=rows, recommendation="R"))
    card_ids = {(False, 0): 1}
    effective = {1: _row(1, VerdictDecision.APPROVE, id=1)}

    result = visible_report(report, card_ids, effective)

    assert result.cards[0].title == "Единственная"
    assert result.summary.comparison_rows == rows


def test_visible_report_пустые_строки_сравнения_без_reject_не_меняются(brief):
    """Критерий 3: пустые `comparison_rows` и отчёт без reject проходят без изменений."""
    card = _make_card(title="Без вердикта")
    report = _make_report([card], brief=brief, summary=ReportSummary(recommendation="R"))
    card_ids = {(False, 0): 1}
    effective = {1: _row(1, VerdictDecision.APPROVE, id=1)}

    result = visible_report(report, card_ids, effective)

    assert result.summary.comparison_rows == []
    assert [c.title for c in result.cards] == ["Без вердикта"]


def test_visible_report_reject_в_contest_cards_не_трогает_строки_сравнения(brief):
    """Критерий 4: `contest_cards` на строки сравнения не влияют."""
    main_cards = [_make_card(title="Основная")]
    contest = _make_card(title="Конкурсная отклонённая")
    rows = [{"Вариант": "строка-основной"}]
    report = _make_report(
        main_cards,
        contest_cards=[contest],
        brief=brief,
        summary=ReportSummary(comparison_rows=rows, recommendation="R"),
    )
    card_ids = {(False, 0): 1, (True, 0): 2}
    effective = {2: _row(2, VerdictDecision.REJECT, id=1)}

    result = visible_report(report, card_ids, effective)

    assert result.contest_cards == []
    assert result.summary.comparison_rows == rows


def test_all_cards_reviewed_false_пока_есть_карточка_без_вердикта():
    """Хотя бы одна строка `solution_cards` без вердикта — статус ещё не `validated`."""
    card_ids = {(False, 0): 1, (False, 1): 2}
    effective = {1: _row(1, VerdictDecision.APPROVE, id=1)}

    assert all_cards_reviewed(card_ids, effective) is False


def test_all_cards_reviewed_true_когда_у_всех_есть_вердикт():
    """У каждой карточки обеих секций есть вердикт — можно переводить запрос в `validated`."""
    card_ids = {(False, 0): 1, (True, 0): 2}
    effective = {
        1: _row(1, VerdictDecision.APPROVE, id=1),
        2: _row(2, VerdictDecision.REJECT, id=2),
    }

    assert all_cards_reviewed(card_ids, effective) is True


def test_all_cards_reviewed_пустой_набор_карточек_не_пройден():
    """Запрос без карточек не считается пройденным тривиально."""
    assert all_cards_reviewed({}, {}) is False


def test_signature_line_использует_общий_формат():
    """Подпись строится тем же форматом, что `REVIEWED_FOOTER` markdown-экспорта."""
    line = signature_line("Иван Иванов", datetime(2026, 8, 27))

    assert line == REVIEWED_FOOTER.format(name="Иван Иванов", date="27.08.2026")
