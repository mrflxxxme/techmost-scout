"""Проверки промптов как контракта.

Эти тесты появились после настоящей аварии: промпты содержат метки вида
{{BRIEF_JSON}}, код их не заполнял, и модель получала метку текстом вместо
данных. Внешне всё работало — карточки собирались, но проверяющая стадия не
видела источников и занижала уверенность каждой карточке.

Поэтому здесь проверяется не поведение модели, а то, что данные вообще до неё
доходят: ни одна метка не должна остаться незаполненной.
"""

from __future__ import annotations

import re

import pytest

from app.config import get_settings
from app.pipeline.stages import candidates_to_json, load_prompt, render_prompt
from app.schemas import ScoredCandidate

PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")

#: Какие метки обязан заполнять код для каждого промпта.
EXPECTED_PLACEHOLDERS = {
    "intake": {"RAW_TASK_TEXT", "INDUSTRY_HINT"},
    "planner": {"BRIEF_JSON"},
    "card_author": {"BRIEF_JSON", "GROUP_CANDIDATES_JSON", "GOV_SUPPORT_JSON"},
    "verifier": {"CARD_JSON", "GROUP_CANDIDATES_JSON"},
    "summary": {"BRIEF_JSON", "CARDS_JSON"},
    "extractor": {"SOURCE_TYPE", "PROVIDER", "RAW_RESULTS"},
    "scorer": set(),
}


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS))
def test_prompt_exists_and_declares_expected_placeholders(name: str):
    """Набор меток в промпте не должен разойтись с тем, что передаёт код."""
    text = load_prompt(name)
    found = set(PLACEHOLDER.findall(text))
    assert found == EXPECTED_PLACEHOLDERS[name], (
        f"промпт {name}: метки в файле {sorted(found)} не совпадают с ожидаемыми "
        f"{sorted(EXPECTED_PLACEHOLDERS[name])}. Обновите и промпт, и вызов render_prompt"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS))
def test_render_prompt_leaves_no_placeholders(name: str):
    """Заполненный промпт не содержит меток: модель не должна видеть {{...}}."""
    values = dict.fromkeys(EXPECTED_PLACEHOLDERS[name], "подставленное значение")
    rendered = render_prompt(name, **values)
    assert not PLACEHOLDER.findall(rendered)


def test_render_prompt_fails_loudly_on_missing_value():
    """Забыли метку — падаем сразу, а не отправляем её модели."""
    with pytest.raises(ValueError, match="не заполнены метки"):
        render_prompt("verifier", CARD_JSON="{}")


@pytest.mark.parametrize("name", ["intake", "planner", "card_author", "verifier", "summary", "scorer"])
def test_prompt_has_error_section(name: str):
    """У каждого промпта есть раздел про ошибки: дешёвые модели без него врут."""
    text = load_prompt(name).lower()
    assert "форматы ошибок" in text, f"промпт {name}: нет раздела «Форматы ошибок»"


def test_gov_support_reference_is_loadable():
    """Справочник господдержки читается и содержит ключевые инструменты."""
    import yaml

    path = get_settings().data_dir / "gov_support.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    names = [item["name"] for item in data["instruments"]]
    assert any("218" in name for name in names)
    assert any("доращивание" in name.lower() for name in names)


def test_candidates_to_json_keeps_url_and_trims_snippet(candidates):
    """Кандидаты для модели: ссылка на месте, длинный текст обрезан."""
    long_snippet = candidates[0].model_copy(update={"snippet": "с" * 5000})
    group = [
        ScoredCandidate(candidate=long_snippet, relevance=5, maturity=5, accessibility=5, freshness=5, completeness=5)
    ]

    payload = candidates_to_json(group)

    assert str(long_snippet.url) in payload
    assert len("с" * 5000) > payload.count("с")  # обрезка сработала
