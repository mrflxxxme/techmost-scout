"""Проверки маршрутизатора моделей: маршруты стадий, резерв, стоп-кран по стоимости."""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.config import Settings
from app.llm.base import LLMError, Message, extract_json_block, parse_json_payload
from app.llm.deepseek import DeepSeekProvider
from app.llm.router import CostLimitExceededError, LLMRouter


class Payload(BaseModel):
    answer: str
    number: int


def _router(settings, providers) -> LLMRouter:
    return LLMRouter(settings=settings, providers=providers)


async def test_stage_route_picks_configured_provider(settings, fake_provider_factory):
    """Стадия сборки карточек по умолчанию идёт на DeepSeek, а не на резервный YandexGPT."""
    deep = fake_provider_factory("deepseek", ['{"answer": "deep", "number": 2}'])
    yandex = fake_provider_factory("yandex", ['{"answer": "yandex", "number": 1}'])
    router = _router(settings, {"deepseek": deep, "yandex": yandex})

    result = await router.complete("card_author", [Message(role="user", content="привет")])

    assert result.usage.provider == "deepseek"
    assert not yandex.calls, "резервный провайдер не должен получать стадию, пока основной жив"


async def test_all_stages_default_to_deepseek(settings, fake_provider_factory):
    """РЕШ-171: ГигаЧат выпилен, все стадии по умолчанию идут на DeepSeek —
    двухпровайдерная экономия обслуживала путь, которым никто не пользовался
    (на прогоне Э2-Е все 46 вызовов и так ушли на DeepSeek)."""
    deep = fake_provider_factory("deepseek")
    yandex = fake_provider_factory("yandex")
    router = _router(settings, {"deepseek": deep, "yandex": yandex})

    result = await router.complete("extractor", [Message(role="user", content="извлеки")])

    assert result.usage.provider == "deepseek"
    assert not yandex.calls


async def test_falls_back_when_primary_fails(settings, fake_provider_factory):
    """Отказ основного провайдера переводит стадию на резервный YandexGPT, а не роняет запрос."""
    deep = fake_provider_factory("deepseek", fail_times=99)
    yandex = fake_provider_factory("yandex", ['{"answer": "ok", "number": 3}'])
    router = _router(settings, {"deepseek": deep, "yandex": yandex})

    result = await router.complete("intake", [Message(role="user", content="?")], attempts_per_provider=1)

    assert result.usage.provider == "yandex"


async def test_all_providers_down_raises(settings, fake_provider_factory):
    deep = fake_provider_factory("deepseek", fail_times=99)
    yandex = fake_provider_factory("yandex", fail_times=99)
    router = _router(settings, {"deepseek": deep, "yandex": yandex})

    with pytest.raises(LLMError, match="все провайдеры отказали"):
        await router.complete("intake", [Message(role="user", content="?")], attempts_per_provider=1)


async def test_json_parsed_from_markdown_fence(settings, fake_provider_factory):
    """Дешёвые модели обрамляют JSON заборами из тройных кавычек — это не должно ломать разбор."""
    fenced = '```json\n{"answer": "да", "number": 7}\n```'
    provider = fake_provider_factory("deepseek", [fenced])
    router = _router(settings, {"deepseek": provider})

    payload = await router.complete_json("intake", [Message(role="user", content="?")], Payload)

    assert payload.answer == "да"
    assert payload.number == 7


async def test_broken_json_is_repaired_once(settings, fake_provider_factory):
    """При несовпадении со схемой модель получает шанс починить ответ."""
    provider = fake_provider_factory(
        "deepseek",
        ['{"answer": "да"}', '{"answer": "да", "number": 5}'],  # первый ответ без обязательного поля
    )
    router = _router(settings, {"deepseek": provider})

    payload = await router.complete_json("intake", [Message(role="user", content="?")], Payload)

    assert payload.number == 5
    assert len(provider.calls) == 2, "должен быть ровно один повтор с текстом ошибки"


async def test_truncated_json_is_retried_with_bigger_budget(settings, fake_provider_factory):
    """Обрыв по лимиту токенов лечится увеличением лимита, а не просьбой «исправь».

    Симптом тот же, что у сломанного JSON, но лечение другое: если переспросить
    с прежним лимитом, ответ обрежется на том же месте.
    """
    truncated = '{"answer": "да", "number'  # ответ оборвался на середине
    provider = fake_provider_factory("deepseek", [truncated, '{"answer": "да", "number": 9}'])
    router = _router(settings, {"deepseek": provider})

    payload = await router.complete_json("intake", [Message(role="user", content="?")], Payload, max_tokens=100)

    assert payload.number == 9
    # Второй вызов — не «починка» с историей, а чистый повтор исходного запроса
    assert len(provider.calls) == 2
    assert len(provider.calls[1]) == 1, "при обрыве переспрашиваем заново, а не досылаем историю"


async def test_cost_limit_stops_runaway_request(settings, fake_provider_factory):
    """Стоп-кран: запрос не может обойтись дороже разрешённого лимита."""
    settings.cost_limit_rub_per_request = 0.015  # хватает ровно на один вызов
    provider = fake_provider_factory("deepseek", ['{"answer": "a", "number": 1}'])
    router = _router(settings, {"deepseek": provider})

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
    provider = fake_provider_factory("deepseek", [reasoning])
    router = _router(settings, {"deepseek": provider})

    payload = await router.complete_json("intake", [Message(role="user", content="?")], Payload)

    assert payload.number == 4
    assert len(provider.calls) == 1, "правильный блок должен находиться без повторного запроса"


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


DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


@pytest.fixture
def deepseek_settings() -> Settings:
    """Настройки, в которых ключ DeepSeek действительно доходит до провайдера.

    Поля `Settings` объявлены с `validation_alias` (`DEEPSEEK_API_KEY` и
    прочие), а `populate_by_name` в `model_config` не включён. Что при этом
    происходит со значением, переданным по ИМЕНИ ПОЛЯ (`deepseek_api_key=...`),
    ЗАВИСИТ ОТ ВЕРСИИ pydantic-settings, а `pyproject.toml` держит плавающий
    предел `pydantic-settings>=2.6`, поэтому законны обе:

        2.9.1  (системный python):   Settings(deepseek_api_key=...) -> ''
        2.15.0 (эталонный .venv):    Settings(deepseek_api_key=...) -> 'sk-...'

    На 2.9.1 значение при `extra="ignore"` молча отбрасывается и в настройках
    остаётся пустой `SecretStr("")` — именно на этой версии и был найден отказ
    (РЕШ-63). На 2.15.0, которая стоит в эталонном интерпретаторе (РЕШ-64),
    имя поля работает. Передача по АЛИАСУ работает в обеих, поэтому ключ здесь
    передаётся под именем алиаса: тест не должен зависеть от того, какая версия
    подтянулась.

    Общая фикстура `settings` из conftest.py передаёт ключи по имени поля; всем
    остальным тестам это безразлично, потому что они работают через
    `FakeProvider`. Здесь же строится настоящий `DeepSeekProvider`, который без
    ключа отказывает ещё до похода в подменённый respx-ом HTTP. Общую оснастку
    не чиним: это отдельная задача (РЕШ-63).
    """
    return Settings(
        APP_ENV="test",
        database_url="sqlite+aiosqlite:///:memory:",
        DEEPSEEK_API_KEY="sk-test",
        cost_limit_rub_per_request=10.0,
    )


def _deepseek_reply(*, cache_hit_tokens: int) -> dict:
    """Ответ DeepSeek с попаданием в кэш префикса и без него.

    Поле `prompt_cache_hit_tokens` — единственное, чем провайдер отличает
    кэшированный вход от обычного, и оно же даёт скидку в цене.
    """
    return {
        "choices": [{"message": {"content": '{"answer": "да", "number": 1}'}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 4000,
            "completion_tokens": 100,
            "prompt_cache_hit_tokens": cache_hit_tokens,
        },
    }


@respx.mock
async def test_cache_hit_increments_cached_calls(deepseek_settings):
    """Ответ с `prompt_cache_hit_tokens > 0` увеличивает счётчик кэша (Т-7, РЕШ-52.6).

    Перенос блоков подстановки в конец промптов делался ради попаданий в кэш
    префикса, а измерить эффект было нечем: `CostTracker` терял `Usage.cached`,
    и оставалась только цена в рублях — она падает и от короткого ответа модели.
    """
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_deepseek_reply(cache_hit_tokens=3900)))
    router = _router(deepseek_settings, {"deepseek": DeepSeekProvider(deepseek_settings)})

    await router.complete("card_author", [Message(role="user", content="?")])

    assert router.cost.calls == 1
    assert router.cost.cached_calls == 1


@respx.mock
async def test_call_without_cache_hit_does_not_increment_cached_calls(deepseek_settings):
    """Без попадания в кэш счётчик кэша стоит на месте, а счётчик вызовов растёт.

    Обратная половина проверки: счётчик, который увеличивается на каждом вызове,
    показывал бы стопроцентный кэш при полностью холодном префиксе.
    """
    respx.post(DEEPSEEK_URL).mock(return_value=httpx.Response(200, json=_deepseek_reply(cache_hit_tokens=0)))
    router = _router(deepseek_settings, {"deepseek": DeepSeekProvider(deepseek_settings)})

    await router.complete("card_author", [Message(role="user", content="?")])

    assert router.cost.calls == 1
    assert router.cost.cached_calls == 0


@respx.mock
async def test_cached_calls_counts_only_cached_of_several(deepseek_settings):
    """Из трёх вызовов в кэш попали два — счётчик обязан показать именно два, а не «был кэш»."""
    respx.post(DEEPSEEK_URL).mock(
        side_effect=[
            httpx.Response(200, json=_deepseek_reply(cache_hit_tokens=0)),
            httpx.Response(200, json=_deepseek_reply(cache_hit_tokens=3900)),
            httpx.Response(200, json=_deepseek_reply(cache_hit_tokens=3800)),
        ]
    )
    router = _router(deepseek_settings, {"deepseek": DeepSeekProvider(deepseek_settings)})

    for _ in range(3):
        await router.complete("card_author", [Message(role="user", content="?")])

    assert router.cost.calls == 3
    assert router.cost.cached_calls == 2


def test_chain_puts_preferred_first(settings, fake_provider_factory):
    providers = {name: fake_provider_factory(name) for name in ("deepseek", "yandex")}
    router = _router(settings, providers)

    assert router.chain_for("card_author")[0] == "deepseek"
    assert router.chain_for("extractor")[0] == "deepseek"
    # Неизвестная стадия обслуживается общим резервным порядком (РЕШ-171: DeepSeek, потом YandexGPT)
    assert router.chain_for("неизвестная") == ["deepseek", "yandex"]


def test_unknown_provider_name_in_routes_raises_readable_error(settings):
    """Неизвестное имя провайдера в LLM_STAGE_ROUTES — читаемая ошибка, а не
    тихий пропуск стадии (РЕШ-171): опечатка или упоминание выпиленного
    провайдера обязаны быть замечены сразу при создании роутера."""
    settings.llm_stage_routes = "intake:chatgpt"

    with pytest.raises(LLMError, match="неизвестный провайдер"):
        LLMRouter(settings)


def test_unknown_provider_name_in_fallback_raises_readable_error(settings):
    """Та же проверка для порядка резерва: пустой резерв или опечатка в нём
    не должны молча выпадать из цепочки (мутация а карточки задачи)."""
    settings.llm_fallback_order = "chatgpt"

    with pytest.raises(LLMError, match="неизвестный провайдер"):
        LLMRouter(settings)
