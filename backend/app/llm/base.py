"""Контракт провайдера языковой модели.

Все провайдеры выглядят для пайплайна одинаково: дай сообщения — получи текст
или разобранный JSON. Стоимость каждого вызова считается здесь же, чтобы
оператор всегда видел цену запроса.
"""

from __future__ import annotations

import abc
import json
import re
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class Usage:
    """Расход одного вызова. Цены — в рублях за миллион токенов."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_rub: float = 0.0
    provider: str = ""
    model: str = ""
    cached: bool = False


@dataclass(slots=True)
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(slots=True)
class LLMResult:
    text: str
    usage: Usage
    raw: dict[str, Any] = field(default_factory=dict)


class LLMError(RuntimeError):
    """Провайдер не смог ответить. Роутер по этой ошибке уходит на резерв."""

    def __init__(self, provider: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable


class LLMProvider(abc.ABC):
    """Базовый провайдер.

    Реализация обязана: (1) уложиться в таймаут из настроек, (2) вернуть Usage
    с посчитанной стоимостью, (3) бросить LLMError вместо своих исключений.
    """

    name: str = "base"
    price_in_rub_per_mtok: float = 0.0
    price_out_rub_per_mtok: float = 0.0

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
        """Один вызов модели."""

    @abc.abstractmethod
    async def health(self) -> tuple[bool, str]:
        """Проверка доступности для команды `scout doctor`: (ок, пояснение)."""

    def estimate_cost(self, tokens_in: int, tokens_out: int) -> float:
        return tokens_in / 1_000_000 * self.price_in_rub_per_mtok + tokens_out / 1_000_000 * self.price_out_rub_per_mtok


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def parse_json_payload(text: str) -> Any:
    """Достать JSON из ответа модели.

    Модели любят обрамлять JSON пояснениями и markdown-заборами, поэтому парсим
    терпимо: сначала как есть, потом из блока кода, потом по первой фигурной
    скобке. Если ничего не вышло — это ошибка провайдера, а не данных.
    """

    candidates: list[str] = [text.strip()]
    if match := _JSON_BLOCK.search(text):
        candidates.append(match.group(1))
    first_brace = min(
        (i for i in (text.find("{"), text.find("[")) if i != -1),
        default=-1,
    )
    if first_brace != -1:
        candidates.append(text[first_brace:])

    for chunk in candidates:
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    raise LLMError("parser", f"ответ не содержит валидный JSON: {text[:200]}…", retryable=True)
