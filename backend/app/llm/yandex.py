"""Провайдер YandexGPT — аварийный резерв.

Роль в системе: страховка на случай, если DeepSeek недоступен (РЕШ-171 —
основной провайдер теперь один). Дороже DeepSeek примерно на порядок (порядка
200 ₽ за миллион токенов), зато это независимый российский провайдер — то
есть отказ у одного не выключает систему целиком.

Авторизация двух видов: Api-Key (не протухает, предпочтительно) и IAM-токен
(Bearer, живёт около 12 часов). Используем Api-Key, если он задан.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage

log = logging.getLogger(__name__)

COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

#: Роли в API Яндекса отличаются от общепринятых именами полей, но не смыслом.
_ROLE_MAP = {"system": "system", "user": "user", "assistant": "assistant"}


class YandexGPTProvider(LLMProvider):
    name = "yandex"
    #: ~0,20 ₽ за 1000 токенов у lite-модели.
    price_in_rub_per_mtok = 200.0
    price_out_rub_per_mtok = 200.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _auth_header(self) -> dict[str, str]:
        if self._settings.has_secret("yandex_api_key"):
            return {"Authorization": f"Api-Key {self._settings.yandex_api_key.get_secret_value()}"}
        if self._settings.has_secret("yandex_iam_token"):
            return {"Authorization": f"Bearer {self._settings.yandex_iam_token.get_secret_value()}"}
        raise LLMError(self.name, "нет ни YANDEX_API_KEY, ни YANDEX_IAM_TOKEN", retryable=False)

    def _model_uri(self, model: str | None) -> str:
        folder = self._settings.yandex_catalog_id
        if not folder or folder.startswith("TBD_"):
            raise LLMError(self.name, "не задан YANDEX_CATALOG_ID", retryable=False)
        return f"gpt://{folder}/{model or self._settings.yandex_model}"

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
        body = {
            "modelUri": self._model_uri(model),
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": str(max_tokens),
            },
            "messages": [{"role": _ROLE_MAP.get(m.role, "user"), "text": m.content} for m in messages],
        }

        async with httpx.AsyncClient(timeout=self._settings.llm_provider_timeout_seconds) as client:
            try:
                response = await client.post(
                    COMPLETION_URL,
                    headers={**self._auth_header(), "Content-Type": "application/json"},
                    json=body,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise LLMError(
                    self.name,
                    f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                    retryable=exc.response.status_code == 429 or exc.response.status_code >= 500,
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(self.name, f"сеть: {exc}") from exc

        payload = response.json().get("result", {})
        usage_raw = payload.get("usage", {})
        tokens_in = int(usage_raw.get("inputTextTokens", 0))
        tokens_out = int(usage_raw.get("completionTokens", 0))
        alternatives = payload.get("alternatives") or []
        if not alternatives:
            raise LLMError(self.name, "пустой ответ модели")
        return LLMResult(
            text=alternatives[0]["message"]["text"],
            usage=Usage(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_rub=self.estimate_cost(tokens_in, tokens_out),
                provider=self.name,
                model=body["modelUri"].rsplit("/", 1)[-1],
            ),
            raw=payload,
        )

    async def health(self) -> tuple[bool, str]:
        has_auth = self._settings.has_secret("yandex_api_key") or self._settings.has_secret("yandex_iam_token")
        if not has_auth:
            return False, "нет YANDEX_API_KEY / YANDEX_IAM_TOKEN"
        if not self._settings.yandex_catalog_id or self._settings.yandex_catalog_id.startswith("TBD_"):
            return False, "нет YANDEX_CATALOG_ID"
        try:
            result = await self.complete([Message(role="user", content="Ответь одним словом: работает")], max_tokens=16)
        except LLMError as exc:
            return False, str(exc)
        return True, f"ок, модель {result.usage.model}, потрачено {result.usage.cost_rub:.4f} ₽"
