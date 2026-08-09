"""Общие фикстуры.

Правило тестов: ни одного обращения в сеть. Провайдеры моделей подменяются
поддельными, HTTP-ответы источников — через respx. Поэтому набор проходит
на машине без ключей и без интернета.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.llm.base import LLMProvider, LLMResult, Message, Usage
from app.schemas import Brief, Candidate, MaturityStage, SourceType


@pytest.fixture
def settings() -> Settings:
    """Настройки для тестов: ключи-заглушки, дешёвые лимиты."""
    return Settings(
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        gigachat_auth_key="dGVzdDp0ZXN0",  # base64("test:test")
        deepseek_api_key="sk-test",
        yandex_api_key="test",
        yandex_catalog_id="b1gtest",
        cost_limit_rub_per_request=10.0,
    )


class FakeProvider(LLMProvider):
    """Провайдер-заглушка: отдаёт заранее заданные ответы по очереди."""

    price_in_rub_per_mtok = 10.0
    price_out_rub_per_mtok = 20.0

    def __init__(self, name: str, responses: list[str] | None = None, *, fail_times: int = 0) -> None:
        self.name = name
        self._responses = responses or ["{}"]
        self._index = 0
        self._fail_times = fail_times
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
        self.calls.append(messages)
        if self._fail_times > 0:
            self._fail_times -= 1
            from app.llm.base import LLMError

            raise LLMError(self.name, "тестовый отказ", retryable=True)
        text = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return LLMResult(
            text=text,
            usage=Usage(tokens_in=100, tokens_out=50, cost_rub=0.01, provider=self.name, model=model or "fake"),
        )

    async def health(self) -> tuple[bool, str]:
        return True, "заглушка"


@pytest.fixture
def fake_provider_factory():
    return FakeProvider


@pytest.fixture
def brief() -> Brief:
    """Типовой бриф: реальная боль из нефтепереработки."""
    return Brief(
        industry="нефтепереработка",
        task_summary=(
            "Катализатор гидроочистки теряет активность за 8 месяцев вместо 24. "
            "Замена стоит около 40 млн рублей и трёх суток простоя."
        ),
        constraints=["работа при 340 °C", "сырьё с повышенным содержанием серы"],
        target_maturity=MaturityStage.PILOT,
        keywords_ru=["катализатор гидроочистки", "дезактивация катализатора"],
        keywords_en=["hydrotreating catalyst", "catalyst deactivation"],
    )


@pytest.fixture
def candidates() -> list[Candidate]:
    """Набор кандидатов, включая пару дублей и одного из витрины."""
    return [
        Candidate(
            source_type=SourceType.PUBLICATION,
            title="Стабилизация катализаторов гидроочистки добавками молибдена",
            url="https://doi.org/10.1234/example-a",
            org="Институт катализа",
            year=2025,
            snippet="Показано увеличение срока службы на 40%.",
            raw_id="10.1234/example-a",
            provider="crossref",
        ),
        Candidate(
            source_type=SourceType.PUBLICATION,
            # Тот же DOI из другого источника — должен склеиться
            title="Стабилизация катализаторов гидроочистки (препринт)",
            url="https://cyberleninka.ru/article/n/example-a",
            org="Институт катализа",
            year=2025,
            snippet="Более подробный фрагмент с описанием методики и результатов испытаний.",
            raw_id="10.1234/example-a",
            provider="cyberleninka",
        ),
        Candidate(
            source_type=SourceType.SHOWCASE,
            title="Промышленная технология регенерации катализаторов",
            url="https://example.org/showcase/1",
            org="НИИ нефтехимии",
            year=2026,
            snippet="[проверенный партнёр] Пилот на двух установках.",
            provider="showcase",
        ),
        Candidate(
            source_type=SourceType.PATENT,
            title="Способ продления срока службы катализатора",
            url="https://patents.example/RU2799999",
            org="Технический университет",
            year=2024,
            snippet="Патент на способ обработки.",
            raw_id="RU2799999",
            provider="rospatent",
        ),
    ]
