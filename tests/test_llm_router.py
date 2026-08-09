"""Проверки маршрутизатора моделей: маршруты стадий, резерв, стоп-кран по стоимости."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.llm.base import LLMError, Message
from app.llm.router import CostLimitExceeded, LLMRouter


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


async def test_cheap_stage_goes_to_free_provider(settings, fake_provider_factory):
    """Массовые стадии идут на бесплатный GigaChat — это и есть экономия."""
    giga = fake_provider_factory("gigachat")
    deep = fake_provider_factory("deepseek")
    router = _router(settings, {"gigachat": giga, "deepseek": deep})

    result = await router.complete("scorer", [Message(role="user", content="оцени")])

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


async def test_cost_limit_stops_runaway_request(settings, fake_provider_factory):
    """Стоп-кран: запрос не может обойтись дороже разрешённого лимита."""
    settings.cost_limit_rub_per_request = 0.015  # хватает ровно на один вызов
    giga = fake_provider_factory("gigachat", ['{"answer": "a", "number": 1}'])
    router = _router(settings, {"gigachat": giga})

    await router.complete("intake", [Message(role="user", content="1")])
    with pytest.raises(CostLimitExceeded):
        await router.complete("intake", [Message(role="user", content="2")])


def test_chain_puts_preferred_first(settings, fake_provider_factory):
    providers = {name: fake_provider_factory(name) for name in ("gigachat", "deepseek", "yandex")}
    router = _router(settings, providers)

    assert router.chain_for("card_author")[0] == "deepseek"
    assert router.chain_for("scorer")[0] == "gigachat"
    # Неизвестная стадия обслуживается общим резервным порядком
    assert router.chain_for("неизвестная") == ["gigachat", "deepseek", "yandex"]
