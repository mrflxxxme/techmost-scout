"""Тесты проверки обезличенности (FR-4, РЕШ-189 п. 1-3).

Функция `find_deidentification_risks` — чистый код без вызова модели: только
регулярные выражения над строкой. Сеть в тестах не используется.
"""

import pytest

from app.anonymize import find_deidentification_risks


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Заказчик — ООО Ромашка, ищем решение по очистке воды.", ["ООО Ромашка"]),
        ("Поставщик — АО «Ромашка», требуется модернизация линии.", ["АО «Ромашка»"]),
        ("Партнёр проекта — ПАО Газпром, нефтепереработка.", ["ПАО Газпром"]),
        ("Подрядчик ЗАО Вымпел готов к пилоту.", ["ЗАО Вымпел"]),
        ("Работы ведёт ОАО Ижсталь совместно с институтом.", ["ОАО Ижсталь"]),
        ("Учредитель — НАО Синтез, испытания на площадке.", ["НАО Синтез"]),
        ("Заказчик — ИП Кузнецов, малое производство.", ["ИП Кузнецов"]),
        ("Технологию предлагает компания AcmeCorp, США.", ["AcmeCorp"]),
    ],
)
def test_finds_known_positive_cases(text: str, expected: list[str]) -> None:
    """Юридическая форма рядом с названием и латинский CamelCase ловятся."""
    assert find_deidentification_risks(text) == expected


def test_plain_technical_text_gives_empty_list() -> None:
    """Обычный технический текст без юрформ не даёт срабатываний."""
    text = (
        "Требуется технология очистки промышленных стоков от взвешенных "
        "частиц, производительность не менее 50 куб. м в час, температура "
        "среды до 80 градусов Цельсия."
    )
    assert find_deidentification_risks(text) == []


def test_spelled_out_legal_form_in_the_middle_of_sentence_is_not_a_false_positive() -> None:
    """Полное написание «Акционерное общество» не абревиатура — не ловится."""
    text = "Опыт показывает, что акционерное общество как форма собственности не влияет на выбор технологии."
    assert find_deidentification_risks(text) == []


def test_gost_abbreviation_is_not_caught() -> None:
    """Аббревиатура ГОСТ не похожа на название компании — не ловится."""
    text = "Материал должен соответствовать требованиям ГОСТ 12.1.007-76."
    assert find_deidentification_risks(text) == []


def test_trl_abbreviation_is_not_caught() -> None:
    """Аббревиатура TRL (латиница, все заглавные) не CamelCase — не ловится."""
    text = "Ожидаемая зрелость решения — не ниже TRL 6."
    assert find_deidentification_risks(text) == []


def test_legal_form_without_capitalized_word_after_is_not_caught() -> None:
    """Юрформа без следующего слова с заглавной буквы не считается названием."""
    text = "ООО работает над проектом уже третий год."
    assert find_deidentification_risks(text) == []


def test_empty_text_gives_empty_list() -> None:
    """Пустая строка не вызывает исключений и не даёт срабатываний."""
    assert find_deidentification_risks("") == []


def test_deduplicates_repeated_mentions_preserving_order() -> None:
    """Повторное упоминание того же названия попадает в список один раз."""
    text = "ООО Ромашка подала заявку. Позже ООО Ромашка уточнила требования."
    assert find_deidentification_risks(text) == ["ООО Ромашка"]


def test_multiple_distinct_entities_are_returned_in_order_of_appearance() -> None:
    """Несколько разных названий возвращаются в порядке появления в тексте."""
    text = "Проект ведут ООО Ромашка и партнёр AcmeCorp совместно с АО «Вымпел»."
    assert find_deidentification_risks(text) == ["ООО Ромашка", "AcmeCorp", "АО «Вымпел»"]


def test_lowercase_latin_word_is_not_camel_case() -> None:
    """Обычное латинское слово без чередования регистра не CamelCase."""
    text = "Используется протокол modbus для передачи данных с датчиков."
    assert find_deidentification_risks(text) == []
