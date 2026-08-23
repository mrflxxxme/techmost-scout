"""Маршрутизатор моделей: какая стадия на какой модели считается.

ГигаЧат выпилен (РЕШ-171): двухпровайдерная экономия обслуживала путь, которым
никто не ходил — на прогоне Э2-Е все 46 вызовов ушли на DeepSeek. Главное
требование к работе LLM теперь — качество, а не бесплатный тариф, поэтому все
стадии по умолчанию считает DeepSeek — он платный, но копеечный. YandexGPT
держим резервом на случай, если DeepSeek недоступен.

Маршруты задаются строкой в настройках (LLM_STAGE_ROUTES), поэтому порядок
меняется одной правкой .env без изменения кода.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage, iter_json_candidates
from app.llm.deepseek import DeepSeekProvider
from app.llm.yandex import YandexGPTProvider

log = logging.getLogger(__name__)

PROVIDER_CLASSES = {
    "deepseek": DeepSeekProvider,
    "yandex": YandexGPTProvider,
}


class CostLimitExceededError(RuntimeError):
    """Стоп-кран: запрос уже стоит дороже разрешённого."""


def _validate_first_fitting[T: BaseModel](text: str, schema: type[T]) -> T:
    """Разобрать ответ модели, выбирая блок JSON по схеме, а не по виду.

    Рассуждающая модель может пересказать в ответе входные данные и только потом
    написать вердикт. Проверка по схеме отличает одно от другого: берём первый
    блок, который в схему укладывается.
    """
    candidates = iter_json_candidates(text)
    last_error: Exception | None = None
    for chunk in candidates:
        try:
            payload: Any = json.loads(chunk)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            last_error = exc
    if isinstance(last_error, ValidationError):
        raise last_error
    raise LLMError("parser", f"ответ не содержит валидный JSON: {text[:200]}…", retryable=True)


def _looks_truncated(text: str) -> bool:
    """Похоже ли, что ответ модели обрезали на середине.

    Признак — незакрытые скобки: JSON начался, но не кончился. Проверка грубая
    (кавычки и вложенность не считаем), но для решения «переспросить с большим
    лимитом или просить исправить» этого достаточно.
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        # Ответ мог быть в markdown-заборе — тогда смотрим на его содержимое.
        stripped = stripped.lstrip("`").removeprefix("json").strip()
    if not stripped or stripped[0] not in "{[":
        return False
    return stripped.count("{") > stripped.count("}") or stripped.count("[") > stripped.count("]")


@dataclass(slots=True)
class CostTracker:
    """Копилка расходов одного запроса, чтобы оператор видел цену.

    `cached_calls` — сколько вызовов попали в кэш префикса провайдера (РЕШ-52.6,
    Т-7). Провайдер уже отдаёт этот факт в `Usage.cached` и уже считает по нему
    скидку, но счётчик его терял, и эффект переноса блоков подстановки в конец
    промптов измерить было нечем: цена в рублях падает и от короткого ответа
    модели тоже. Число вызовов с попаданием — прямая мера, а не косвенная.
    """

    limit_rub: float
    spent_rub: float = 0.0
    by_provider: dict[str, float] = field(default_factory=dict)
    calls: int = 0
    cached_calls: int = 0

    def add(self, usage: Usage) -> None:
        self.spent_rub += usage.cost_rub
        self.by_provider[usage.provider] = self.by_provider.get(usage.provider, 0.0) + usage.cost_rub
        self.calls += 1
        if usage.cached:
            self.cached_calls += 1
        if self.spent_rub > self.limit_rub:
            raise CostLimitExceededError(
                f"запрос израсходовал {self.spent_rub:.2f} ₽ при лимите {self.limit_rub:.2f} ₽"
            )


class LLMRouter:
    """Выбирает провайдера по стадии и уходит на резерв при отказе."""

    def __init__(self, settings: Settings | None = None, providers: dict[str, LLMProvider] | None = None) -> None:
        self._settings = settings or get_settings()
        self._providers: dict[str, LLMProvider] = providers or {
            name: cls(self._settings) for name, cls in PROVIDER_CLASSES.items()
        }
        self._routes = self._settings.stage_routes()
        self._fallback = self._settings.fallback_order()
        self._check_known_providers()
        self.cost = CostTracker(limit_rub=self._settings.cost_limit_rub_per_request)

    def _check_known_providers(self) -> None:
        """Проверить, что маршруты и резерв ссылаются на существующих провайдеров.

        Раньше неизвестное имя (опечатка или удалённый провайдер вроде
        выпиленного ГигаЧата, РЕШ-171) молча выпадало из цепочки в `chain_for`:
        стадия просто уходила на резерв, а причина терялась. Сверяем со
        статическим реестром `PROVIDER_CLASSES`, а не с `self._providers` —
        тесты законно подменяют часть провайдеров заглушками и не обязаны
        подсовывать их все.
        """
        known = set(PROVIDER_CLASSES)
        bad_routes = {stage: name for stage, name in self._routes.items() if name and name not in known}
        bad_fallback = [name for name in self._fallback if name not in known]
        if bad_routes or bad_fallback:
            raise LLMError(
                "router",
                (
                    "неизвестный провайдер в настройках маршрутизации LLM: "
                    f"маршруты {bad_routes}, резерв {bad_fallback}; "
                    f"известные провайдеры: {sorted(known)}"
                ),
                retryable=False,
            )

    # ── Выбор провайдера ────────────────────────────────────────────────────
    def chain_for(self, stage: str) -> list[str]:
        """Порядок провайдеров для стадии: сначала назначенный, потом резервные."""
        preferred = self._routes.get(stage)
        chain = [preferred] if preferred else []
        chain += [name for name in self._fallback if name != preferred]
        return [name for name in chain if name in self._providers]

    def available(self) -> dict[str, LLMProvider]:
        return dict(self._providers)

    # ── Вызовы ──────────────────────────────────────────────────────────────
    async def complete(
        self,
        stage: str,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
        attempts_per_provider: int = 2,
    ) -> LLMResult:
        """Выполнить вызов, перебирая провайдеров по цепочке стадии."""
        chain = self.chain_for(stage)
        if not chain:
            raise LLMError("router", f"для стадии {stage} не настроен ни один провайдер", retryable=False)

        last_error: LLMError | None = None
        for provider_name in chain:
            provider = self._providers[provider_name]
            for attempt in range(1, attempts_per_provider + 1):
                try:
                    result = await provider.complete(
                        messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
                    )
                except LLMError as exc:
                    last_error = exc
                    if not exc.retryable:
                        log.warning("стадия %s: %s не подходит (%s), перехожу к резерву", stage, provider_name, exc)
                        break
                    if attempt < attempts_per_provider:
                        pause = 2**attempt
                        log.info("стадия %s: %s ошибка (%s), повтор через %s с", stage, provider_name, exc, pause)
                        await asyncio.sleep(pause)
                    continue
                else:
                    self.cost.add(result.usage)
                    if provider_name != chain[0]:
                        log.warning("стадия %s выполнена резервным провайдером %s", stage, provider_name)
                    return result
        raise LLMError("router", f"все провайдеры отказали на стадии {stage}: {last_error}", retryable=False)

    async def complete_json[T: BaseModel](
        self,
        stage: str,
        messages: list[Message],
        schema: type[T],
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        repair_attempts: int = 1,
    ) -> T:
        """Вызов с разбором ответа в pydantic-модель.

        Дешёвые модели регулярно ломают JSON: обрамляют пояснениями, теряют
        кавычки, путают имена полей. Поэтому один раз просим модель починить
        собственный ответ, показав ей текст ошибки, и только потом падаем.

        Отдельно обрабатываем обрыв по лимиту токенов: у него те же симптомы
        (невалидный JSON), но просить «исправь» бессмысленно — повторный ответ
        обрежется там же. В этом случае удваиваем лимит и переспрашиваем заново.
        """
        result = await self.complete(stage, messages, temperature=temperature, max_tokens=max_tokens, json_mode=True)
        payload_text = result.text
        budget = max_tokens

        for attempt in range(repair_attempts + 1):
            try:
                return _validate_first_fitting(payload_text, schema)
            except (LLMError, ValidationError) as exc:
                if attempt >= repair_attempts:
                    raise LLMError(
                        "router",
                        f"стадия {stage}: ответ не сходится со схемой {schema.__name__}: {exc}",
                        retryable=False,
                    ) from exc

                if _looks_truncated(payload_text):
                    budget *= 2
                    log.warning("стадия %s: ответ обрезан по лимиту токенов, переспрашиваю с лимитом %s", stage, budget)
                    retry = await self.complete(
                        stage, messages, temperature=temperature, max_tokens=budget, json_mode=True
                    )
                    payload_text = retry.text
                    continue

                log.info("стадия %s: прошу модель починить JSON (%s)", stage, type(exc).__name__)
                repair = await self.complete(
                    stage,
                    [
                        *messages,
                        Message(role="assistant", content=payload_text),
                        Message(
                            role="user",
                            content=(
                                "Твой предыдущий ответ не прошёл проверку схемы. Ошибка:\n"
                                f"{exc}\n\n"
                                "Верни ИСПРАВЛЕННЫЙ ответ. Только JSON, без пояснений и markdown."
                            ),
                        ),
                    ],
                    temperature=0.0,
                    max_tokens=budget,
                    json_mode=True,
                )
                payload_text = repair.text
        raise AssertionError("недостижимо")

    async def health(self) -> dict[str, tuple[bool, str]]:
        """Опрос всех провайдеров для команды `scout doctor`."""
        names = list(self._providers)
        results = await asyncio.gather(*(self._providers[name].health() for name in names), return_exceptions=True)
        report: dict[str, tuple[bool, str]] = {}
        for name, outcome in zip(names, results, strict=True):
            if isinstance(outcome, BaseException):
                report[name] = (False, f"{type(outcome).__name__}: {outcome}")
            else:
                report[name] = outcome
        return report
