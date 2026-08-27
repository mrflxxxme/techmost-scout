"""Проверки подписи эксперта и строки «Разработчик» экспортёра (Э4-И1).

Отдельный файл, а не дописывание в `test_export_markdown.py` (440 строк из
500) — правило задачи. Слой чистый: без БД и без сети.
"""

from __future__ import annotations

from datetime import datetime

from app.export.markdown import REVIEWED_FOOTER, TEMPLATE_FILTERS, developer_line, render_markdown
from app.schemas import Report, ReportSummary, SolutionCard

_GENERATED_AT = datetime(2026, 8, 27, 9, 30)


def _make_card(**overrides) -> SolutionCard:
    fields = {
        "title": "Регенерация катализатора добавками молибдена",
        "essence": "Продление срока службы катализатора за счёт присадки.",
        "applicability": "Закрывает задачу продления межрегенерационного цикла.",
        "next_step": "Запросить образец у разработчика.",
    }
    fields.update(overrides)
    return SolutionCard(**fields)


def _make_report(cards: list[SolutionCard], **overrides) -> Report:
    fields = {
        "request_id": 1,
        "brief": overrides.pop("brief"),
        "cards": cards,
        "summary": ReportSummary(recommendation="Начать с варианта №1."),
        "generated_at": _GENERATED_AT,
    }
    fields.update(overrides)
    return Report(**fields)


def test_подпись_эксперта_дословна_символ_в_символ(brief):
    """При `validated=True` и непустом имени в markdown ровно строка РЕШ-197."""
    report = _make_report([_make_card()], brief=brief)

    text = render_markdown(report, validated=True, reviewer="Иван Иванов")

    expected = REVIEWED_FOOTER.format(name="Иван Иванов", date="27.08.2026")
    assert expected == "Подготовлено системой ТехМост Скаут, проверено: Иван Иванов, 27.08.2026"
    assert expected in text


def test_пустое_имя_проверяющего_оставляет_пометку_черновика(brief):
    """При `validated=True`, но пустом имени — пометка ЧЕРНОВИК остаётся (инвариант №2)."""
    report = _make_report([_make_card()], brief=brief)

    text = render_markdown(report, validated=True, reviewer="")

    assert "ЧЕРНОВИК — не проверено экспертом" in text
    assert "проверено:" not in text


def test_в_markdown_нет_ёлочек_вокруг_названия_системы(brief):
    """Прежняя форма с ёлочками (`markdown.py:117` на базе) снята."""
    report = _make_report([_make_card()], brief=brief)

    text = render_markdown(report, validated=True, reviewer="Иван Иванов")

    assert "«ТехМост Скаут»" not in text


def test_developer_line_не_печатает_не_найдено_хвостом():
    """developer_team = «не найдено» не даёт хвоста «— не найдено» (совет С-3)."""
    card = _make_card(developer_org="ООО «Ромашка»", developer_team="не найдено")

    line = developer_line(card)

    assert "— не найдено" not in line
    assert line == "ООО «Ромашка»"


def test_developer_line_при_пустом_developer_org_остаётся_не_найдено():
    """Пустой `developer_org` — строка остаётся честным «не найдено»."""
    card = _make_card(developer_org=None, developer_team=None)

    assert developer_line(card) == "не найдено"


def test_developer_line_печатает_настоящую_команду():
    """Непустой `developer_team`, отличный от заглушки, печатается как раньше."""
    card = _make_card(developer_org="ООО «Ромашка»", developer_team="лаборатория катализа")

    assert developer_line(card) == "ООО «Ромашка» — лаборатория катализа"


def test_template_filters_регистрирует_developer_line():
    """`TEMPLATE_FILTERS` — точка регистрации фильтра для Jinja обоих потребителей."""
    assert TEMPLATE_FILTERS["developer_line"] is developer_line
