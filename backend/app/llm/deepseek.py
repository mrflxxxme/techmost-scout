"""Провайдер DeepSeek — модель для качественных стадий пайплайна.

Куда идёт: сборка карточек, проверка утверждений, сводка. То есть там, где
цена ошибки — плохая карточка на глазах у клиента, а не лишние 10 копеек.

Экономика (август 2026): $0.14 за 1 млн входных токенов и $0.28 за выходные,
при попадании в кэш префикса вход дешевеет в пятьдесят раз. Поэтому системный
промпт и справочники всегда кладутся в начало сообщения — так они кэшируются.

Важно: 24 июля 2026 отключены алиасы deepseek-chat и deepseek-reasoner, а
скидки off-peak отменены. Работают только явные имена моделей (V4-Flash,
V4-Pro), поэтому имя модели берётся из настроек, а не хардкодится.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage

log = logging.getLogger(__name__)

#: Цены в долларах за миллион токенов; в рубли переводим по курсу из настроек.
PRICE_IN_USD_PER_MTOK = 0.14
PRICE_OUT_USD_PER_MTOK = 0.28


class DeepSeekProvider(LLMProvider):
    name = "deepseek"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        fx = settings.fx_rate_usd_to_rub
        self.price_in_rub_per_mtok = PRICE_IN_USD_PER_MTOK * fx
        self.price_out_rub_per_mtok = PRICE_OUT_USD_PER_MTOK * fx

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
        key = self._settings.deepseek_api_key.get_secret_value()
        if not key or key.startswith("TBD_"):
            raise LLMError(self.name, "не задан DEEPSEEK_API_KEY", retryable=False)

        body: dict = {
            "model": model or self._settings.deepseek_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=self._settings.llm_provider_timeout_seconds) as client:
            try:
                response = await client.post(
                    f"{self._settings.deepseek_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=body,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                hint = ""
                if status == 402:
                    hint = " (нулевой баланс — пополнить platform.deepseek.com)"
                elif status == 400 and "model" in exc.response.text.lower():
                    hint = " (проверьте имя модели: алиасы deepseek-chat отключены 24.07.2026)"
                raise LLMError(
                    self.name,
                    f"HTTP {status}{hint}: {exc.response.text[:200]}",
                    retryable=status == 429 or status >= 500,
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(self.name, f"сеть: {exc}") from exc

        payload = response.json()
        choice = payload["choices"][0]
        message = choice.get("message", {})
        text = (message.get("content") or "").strip()

        # V4-Flash — рассуждающая модель: думает в reasoning_content и только
        # потом пишет content. Если лимит токенов съеден рассуждениями, content
        # приходит пустым. Спасаем ответ из рассуждений, иначе просим повтор.
        if not text:
            reasoning = (message.get("reasoning_content") or "").strip()
            if reasoning and ("{" in reasoning or "[" in reasoning):
                log.warning("DeepSeek: content пуст, беру JSON из reasoning_content")
                text = reasoning
            else:
                raise LLMError(
                    self.name,
                    "пустой content: лимит токенов ушёл на рассуждения модели "
                    f"(finish_reason={choice.get('finish_reason')}) — нужен больший max_tokens",
                    retryable=True,
                )

        usage_raw = payload.get("usage", {})
        tokens_in = int(usage_raw.get("prompt_tokens", 0))
        tokens_out = int(usage_raw.get("completion_tokens", 0))
        cache_hit = int(usage_raw.get("prompt_cache_hit_tokens", 0))
        # Кэшированный вход стоит примерно в пятьдесят раз дешевле обычного.
        cost = (
            (tokens_in - cache_hit) / 1_000_000 * self.price_in_rub_per_mtok
            + cache_hit / 1_000_000 * self.price_in_rub_per_mtok / 50
            + tokens_out / 1_000_000 * self.price_out_rub_per_mtok
        )
        return LLMResult(
            text=text,
            usage=Usage(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_rub=cost,
                provider=self.name,
                model=body["model"],
                cached=cache_hit > 0,
            ),
            raw=payload,
        )

    async def health(self) -> tuple[bool, str]:
        if not self._settings.has_secret("deepseek_api_key"):
            return False, "нет DEEPSEEK_API_KEY"
        try:
            result = await self.complete([Message(role="user", content="Ответь одним словом: работает")], max_tokens=16)
        except LLMError as exc:
            return False, str(exc)
        return True, f"ок, модель {result.usage.model}, потрачено {result.usage.cost_rub:.4f} ₽"
