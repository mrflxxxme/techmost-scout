"""Активные параметры прогона печатаются; секреты проверяются по выводу (З-7).

Долг 5 (заметка З-2 и совет С-2 аудита Э2-Г): критерий ворот про стоп-кран
держался на утверждении оркестратора, а не на выводе команды. Отступление Б-2
того же аудита: маршруты стадий и имя модели нигде не печатались, окружение
изменилось между подготовкой и воротным прогоном незаметно — обнаружил это
только аудит, задним числом.

Способ проверки секретов — РЕШ-124: перехват фактического `stdout`/`stderr`
(и `caplog`) с заведомо фиктивным ключом, а не грep по исходнику. Значения
ключей здесь заведомо фиктивные, настоящих секретов в файле нет (инвариант №3).
"""

from __future__ import annotations

import json
import logging

import pytest

from app.llm import LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message
from app.pipeline.orchestrator import ScoutPipeline

#: Формулировка плана — единственный платный вызов «успешного» прогона этого
#: файла: адаптеров нет, поэтому после планировщика прогон честно возвращает
#: пустой отчёт (`_empty_report`), не доходя до скоринга и карточек.
_PLAN_JSON = json.dumps(
    {
        "queries": [
            {
                "text": "катализатор гидроочистки серостойкий",
                "source_type": "publication",
                "lang": "ru",
                "rationale": "прямой поиск по задаче",
            }
        ]
    },
    ensure_ascii=False,
)

#: Уникальные фиктивные значения секретов (РЕШ-124, инвариант №3) — не
#: настоящие ключи, а маркеры для стража: если хоть один встретится в выводе,
#: тест провалится и укажет ровно на утечку, а не на что-то ещё.
_FAKE_GIGACHAT_KEY = "FAKE-GIGACHAT-KEY-9f3d1c7b"
_FAKE_DEEPSEEK_KEY = "FAKE-DEEPSEEK-KEY-2b7a44dd"
_FAKE_YANDEX_KEY = "FAKE-YANDEX-KEY-6e0a91aa"


class _AlwaysFailsProvider(LLMProvider):
    """Провайдер, который безусловно отказывает — нужен, чтобы уронить прогон
    на первой же платной стадии (планировщик) и проверить, что строка
    параметров успела напечататься до падения (критерий 1, тест б)."""

    name = "deepseek"
    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
        raise LLMError(self.name, "тестовый безусловный отказ", retryable=False)

    async def health(self) -> tuple[bool, str]:
        return False, "заглушка"


def _params_line(records: list[logging.LogRecord]) -> logging.LogRecord | None:
    """Найти запись со строкой активных параметров среди перехваченных."""
    return next((r for r in records if r.getMessage().startswith("Параметры прогона:")), None)


# ── Критерий 1: строка печатается всегда, включая упавший прогон ────────────
async def test_params_line_printed_on_successful_run(settings, brief, fake_provider_factory, caplog):
    """Удачный прогон: строка на уровне INFO присутствует в caplog."""
    caplog.set_level(logging.INFO)
    provider = fake_provider_factory("deepseek", [_PLAN_JSON])
    router = LLMRouter(settings, providers={"deepseek": provider})
    pipeline = ScoutPipeline(settings=settings, router=router, adapters=[])

    report = await pipeline.run(brief, request_id=1)

    assert report.cards == []  # пустой отчёт: адаптеров нет — стадии карточек не звались
    record = _params_line(caplog.records)
    assert record is not None, "строка активных параметров обязана попасть в caplog"
    assert record.levelno == logging.INFO


async def test_params_line_printed_on_run_failing_at_first_stage(settings, brief, caplog):
    """Прогон, упавший на первой же платной стадии (планировщик): строка всё
    равно присутствует — параметры нужны именно тогда, когда прогон не
    удался (Б-2 аудита)."""
    caplog.set_level(logging.INFO)
    router = LLMRouter(settings, providers={"deepseek": _AlwaysFailsProvider()})
    pipeline = ScoutPipeline(settings=settings, router=router, adapters=[])

    with pytest.raises(LLMError):
        await pipeline.run(brief, request_id=1)

    record = _params_line(caplog.records)
    assert record is not None, "строка обязана печататься и на упавшем прогоне"
    assert record.levelno == logging.INFO


# ── Критерий 2: строка несёт все названные величины ──────────────────────────
async def test_params_line_carries_all_named_values(settings, brief, fake_provider_factory, caplog):
    """Стоп-кран, оба порога, маршрут КАЖДОЙ стадии, порядок резерва, имена
    обеих моделей — все в одной строке."""
    caplog.set_level(logging.INFO)
    provider = fake_provider_factory("deepseek", [_PLAN_JSON])
    router = LLMRouter(settings, providers={"deepseek": provider})
    pipeline = ScoutPipeline(settings=settings, router=router, adapters=[])

    await pipeline.run(brief, request_id=1)

    record = _params_line(caplog.records)
    assert record is not None
    line = record.getMessage()

    # Стоп-кран.
    assert f"{settings.cost_limit_rub_per_request:.2f}" in line

    # Оба порога отбора.
    assert f"relevance_min={settings.relevance_min}" in line
    assert f"second_pass_relevance_max={settings.second_pass_relevance_max}" in line

    # Маршрут каждой стадии из stage_routes.
    for stage, stage_provider in settings.stage_routes().items():
        assert f"{stage}:{stage_provider}" in line, f"маршрут стадии {stage} отсутствует в строке"

    # Порядок резерва — целиком и по порядку.
    assert "→".join(settings.fallback_order()) in line

    # Имена обеих моделей.
    assert settings.gigachat_model in line
    assert settings.deepseek_model in line


# ── Критерий 3: секретов в выводе нет (страж по фактическому выводу, РЕШ-124) ─
async def test_params_line_has_no_secret_values(monkeypatch, brief, fake_provider_factory, caplog, capsys):
    """Прогон с уникальными фиктивными значениями ключей: ни одно из них не
    встречается ни в caplog, ни в перехваченных stdout/stderr."""
    from app.config import Settings, get_settings

    monkeypatch.setenv("GIGACHAT_AUTH_KEY", _FAKE_GIGACHAT_KEY)
    monkeypatch.setenv("DEEPSEEK_API_KEY", _FAKE_DEEPSEEK_KEY)
    monkeypatch.setenv("YANDEX_API_KEY", _FAKE_YANDEX_KEY)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("YANDEX_CATALOG_ID", "b1gtest")
    monkeypatch.setenv("COST_LIMIT_RUB_PER_REQUEST", "10.0")
    get_settings.cache_clear()
    fake_settings = Settings()
    try:
        caplog.set_level(logging.INFO)
        provider = fake_provider_factory("deepseek", [_PLAN_JSON])
        router = LLMRouter(fake_settings, providers={"deepseek": provider})
        pipeline = ScoutPipeline(settings=fake_settings, router=router, adapters=[])

        await pipeline.run(brief, request_id=1)
    finally:
        get_settings.cache_clear()

    captured = capsys.readouterr()
    for secret in (_FAKE_GIGACHAT_KEY, _FAKE_DEEPSEEK_KEY, _FAKE_YANDEX_KEY):
        assert secret not in caplog.text, "значение ключа не имеет права попасть в лог прогона"
        assert secret not in captured.out, "значение ключа не имеет права попасть в stdout"
        assert secret not in captured.err, "значение ключа не имеет права попасть в stderr"


# ── Критерий 5: печать не добавляет вызовов модели ───────────────────────────
async def test_params_line_does_not_add_router_calls(settings, brief, fake_provider_factory, caplog):
    """Счётчик обращений к подменённому роутеру равен ожидаемому: печать
    строки не тратит ни одного лишнего вызова. Без адаптеров единственный
    платный вызов успешного прогона — планировщик, поэтому вызов ровно один."""
    caplog.set_level(logging.INFO)
    provider = fake_provider_factory("deepseek", [_PLAN_JSON])
    router = LLMRouter(settings, providers={"deepseek": provider})
    pipeline = ScoutPipeline(settings=settings, router=router, adapters=[])

    await pipeline.run(brief, request_id=1)

    assert len(provider.calls) == 1, "печать параметров не должна добавлять обращений к роутеру"
