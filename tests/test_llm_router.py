"""Проверки маршрутизатора моделей: маршруты стадий, резерв, стоп-кран по стоимости."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.llm.base import LLMError, Message, extract_json_block, parse_json_payload
from app.llm.router import CostLimitExceededError, LLMRouter


class Payload(BaseModel):
    answer: str
    number: int


def _router(settings, providers) -> LLMRouter:
    return LLMRouter(settings=settings, providers=providers)


async def test_stage_route_picks_configured_provider(settings, fake_provider_factory):
    """Стадия сборки карточек по умолчанию идёт на DeepSeek, а не на GigaChat."""
    giga = fake_provider_factory("gigachat", ['{"answer": "giga", "number": 1}'])
    deep = fake_provider_factory("deepseek", ['{"answer": "deep", "number": 2}'])
    router = _router(settings, {"gigachat": giga, "deepseek": deep})

    result = await router.complete("card_author", [Message(role="user", content="привет")])

    assert result.usage.provider == "deepseek"
    assert not giga.calls, "дешёвая модель не должна получать качественную стадию"


async def test_short_answer_stage_goes_to_free_provider(settings, fake_provider_factory):
    """Стадии с коротким ответом идут на бесплатный GigaChat — это и есть экономия.

    Длинный структурированный JSON ему не доверяем: живые прогоны показали, что
    на плане поиска и батче оценок он обрывает ответ на середине.
    """
    giga = fake_provider_factory("gigachat")
    deep = fake_provider_factory("deepseek")
    router = _router(settings, {"gigachat": giga, "deepseek": deep})

    result = await router.complete("extractor", [Message(role="user", content="извлеки")])

    assert result.usage.provider == "gigachat"
    assert not deep.calls


async def test_falls_back_when_primary_fails(settings, fake_provider_factory):
    """Отказ основного провайдера переводит стадию на резервный, а не роняет запрос."""
    giga = fake_provider_factory("gigachat", fail_times=99)
    deep = fake_provider_factory("deepseek", ['{"answer": "ok", "number": 3}'])
    router = _router(settings, {"gigachat": giga, "deepseek": deep})

    result = await router.complete("intake", [Message(role="user", content="?")], attempts_per_provider=1)

    assert result.usage.provider == "deepseek"


async def test_all_providers_down_raises(settings, fake_provider_factory):
    giga = fake_provider_factory("gigachat", fail_times=99)
    deep = fake_provider_factory("deepseek", fail_times=99)
    router = _router(settings, {"gigachat": giga, "deepseek": deep})

    with pytest.raises(LLMError, match="все провайдеры отказали"):
        await router.complete("intake", [Message(role="user", content="?")], attempts_per_provider=1)


async def test_json_parsed_from_markdown_fence(settings, fake_provider_factory):
    """Дешёвые модели обрамляют JSON заборами из тройных кавычек — это не должно ломать разбор."""
    fenced = '```json\n{"answer": "да", "number": 7}\n```'
    giga = fake_provider_factory("gigachat", [fenced])
    router = _router(settings, {"gigachat": giga})

    payload = await router.complete_json("intake", [Message(role="user", content="?")], Payload)

    assert payload.answer == "да"
    assert payload.number == 7


async def test_broken_json_is_repaired_once(settings, fake_provider_factory):
    """При несовпадении со схемой модель получает шанс починить ответ."""
    giga = fake_provider_factory(
        "gigachat",
        ['{"answer": "да"}', '{"answer": "да", "number": 5}'],  # первый ответ без обязательного поля
    )
    router = _router(settings, {"gigachat": giga})

    payload = await router.complete_json("intake", [Message(role="user", content="?")], Payload)

    assert payload.number == 5
    assert len(giga.calls) == 2, "должен быть ровно один повтор с текстом ошибки"


async def test_truncated_json_is_retried_with_bigger_budget(settings, fake_provider_factory):
    """Обрыв по лимиту токенов лечится увеличением лимита, а не просьбой «исправь».

    Симптом тот же, что у сломанного JSON, но лечение другое: если переспросить
    с прежним лимитом, ответ обрежется на том же месте.
    """
    truncated = '{"answer": "да", "number'  # ответ оборвался на середине
    giga = fake_provider_factory("gigachat", [truncated, '{"answer": "да", "number": 9}'])
    router = _router(settings, {"gigachat": giga})

    payload = await router.complete_json("intake", [Message(role="user", content="?")], Payload, max_tokens=100)

    assert payload.number == 9
    # Второй вызов — не «починка» с историей, а чистый повтор исходного запроса
    assert len(giga.calls) == 2
    assert len(giga.calls[1]) == 1, "при обрыве переспрашиваем заново, а не досылаем историю"


async def test_cost_limit_stops_runaway_request(settings, fake_provider_factory):
    """Стоп-кран: запрос не может обойтись дороже разрешённого лимита."""
    settings.cost_limit_rub_per_request = 0.015  # хватает ровно на один вызов
    giga = fake_provider_factory("gigachat", ['{"answer": "a", "number": 1}'])
    router = _router(settings, {"gigachat": giga})

    await router.complete("intake", [Message(role="user", content="1")])
    with pytest.raises(CostLimitExceededError):
        await router.complete("intake", [Message(role="user", content="2")])


async def test_answer_is_picked_by_schema_not_by_size(settings, fake_provider_factory):
    """Ответ выбирается по схеме, а не по длине блока.

    Рассуждающая модель пересказывает входные данные и только потом пишет
    вердикт. Пересказ бывает длиннее ответа — если брать самый большой блок,
    в схему попадёт мусор. Так и падала стадия проверки карточек.
    """
    reasoning = (
        "Сначала перечислю кандидатов, с которыми сверяюсь:\n"
        '[{"url": "https://doi.org/10.1234/a", "title": "Очень длинная запись про кандидата, '
        'которая заметно длиннее итогового ответа модели"}]\n'
        'Теперь вердикт: {"answer": "да", "number": 4}'
    )
    giga = fake_provider_factory("gigachat", [reasoning])
    router = _router(settings, {"gigachat": giga})

    payload = await router.complete_json("intake", [Message(role="user", content="?")], Payload)

    assert payload.number == 4
    assert len(giga.calls) == 1, "правильный блок должен находиться без повторного запроса"


def test_extract_json_block_pulls_answer_out_of_reasoning():
    """Рассуждающая модель пишет размышления прозой — ответ надо вычленить.

    Именно так DeepSeek V4-Flash ведёт себя, когда лимит токенов ушёл на
    reasoning_content: готовый JSON лежит внутри текста рассуждений.
    """
    reasoning = (
        "We need respond with JSON only. Need analyze and produce final.\n"
        'Итог: {"needs_rework": false, "confidence": "high", "notes": "всё сошлось"}\n'
        "That should be the answer."
    )

    block = extract_json_block(reasoning)

    assert block is not None
    assert parse_json_payload(block)["confidence"] == "high"


def test_braces_inside_strings_do_not_break_parsing():
    """Скобка внутри строкового значения не должна ломать подсчёт вложенности."""
    text = 'префикс {"note": "выражение вида {x} в тексте", "ok": true} суффикс'

    payload = parse_json_payload(text)

    assert payload["ok"] is True
    assert "{x}" in payload["note"]


def test_extract_json_block_returns_none_on_prose():
    """Если JSON в тексте нет, честно возвращаем None, а не мусор."""
    assert extract_json_block("Модель извиняется и ничего не возвращает") is None


def test_parse_json_payload_survives_leading_explanation():
    """Пояснение перед JSON — обычное дело у дешёвых моделей."""
    text = 'Хорошо, вот результат:\n{"answer": "да", "number": 3}'

    assert parse_json_payload(text)["number"] == 3


def test_chain_puts_preferred_first(settings, fake_provider_factory):
    providers = {name: fake_provider_factory(name) for name in ("gigachat", "deepseek", "yandex")}
    router = _router(settings, providers)

    assert router.chain_for("card_author")[0] == "deepseek"
    assert router.chain_for("extractor")[0] == "gigachat"
    # Неизвестная стадия обслуживается общим резервным порядком
    assert router.chain_for("неизвестная") == ["gigachat", "deepseek", "yandex"]
