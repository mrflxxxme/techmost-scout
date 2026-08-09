"""Провайдер GigaChat (Сбер) — основная рабочая модель системы.

Почему основная: бесплатный тариф даёт 250 млн токенов на линейке Lite на 12
месяцев, работает из России без зарубежной карты и VPN. Этого хватает на весь
пилот и первый год — при средней карточке ~2 500 токенов это порядка сотни
тысяч обработанных документов.

Две особенности, которые ломают наивную реализацию:
1. Авторизация двухшаговая. Ключ из личного кабинета — это base64(client_id:secret),
   его меняют на access_token со временем жизни 30 минут. Токен кэшируем.
2. Домены Сбера подписаны корневым сертификатом НУЦ Минцифры, которого нет в
   certifi. Без бандла с этим сертификатом httpx выдаёт ошибку проверки TLS —
   отсюда настройки gigachat_ca_bundle и gigachat_verify_ssl.
"""

from __future__ import annotations

import logging
import time
import uuid

import httpx

from app.config import Settings
from app.llm.base import LLMError, LLMProvider, LLMResult, Message, Usage

log = logging.getLogger(__name__)

#: Рабочий адрес, проверен живым вызовом 9 августа 2026: отдаёт список моделей
#: (GigaChat, GigaChat-2, GigaChat-2-Pro, GigaChat-2-Max) и обслуживает чат.
DEFAULT_BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"
DEFAULT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
#: Адрес api.giga.chat встречается в публикациях как «новый», но на нашем
#: тарифе отвечает 403 от nginx. Оставлен как резерв: если Сбер всё-таки
#: переведёт трафик туда, провайдер переключится сам, увидев 403/404.
ALT_BASE_URL = "https://api.giga.chat/api/v1"
ALT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"


class GigaChatProvider(LLMProvider):
    name = "gigachat"
    #: Сверх бесплатного тарифа — 65 ₽ за 1 млн токенов (Lite, физлицо).
    price_in_rub_per_mtok = 65.0
    price_out_rub_per_mtok = 65.0

    def __init__(
        self,
        settings: Settings,
        base_url: str = DEFAULT_BASE_URL,
        oauth_url: str = DEFAULT_OAUTH_URL,
    ) -> None:
        self._settings = settings
        self._base_url = base_url.rstrip("/")
        self._oauth_url = oauth_url
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._tried_alt = False

    # ── TLS ─────────────────────────────────────────────────────────────────
    def _verify(self) -> bool | str:
        if not self._settings.gigachat_verify_ssl:
            log.warning("проверка TLS для GigaChat отключена — так можно только локально")
            return False
        bundle = self._settings.gigachat_ca_bundle_path
        if bundle:
            return bundle
        if self._settings.gigachat_ca_bundle:
            log.warning(
                "бандл сертификатов %s не найден — беру стандартный. "
                "Ожидается ошибка проверки TLS: домены Сбера подписаны НУЦ Минцифры",
                self._settings.gigachat_ca_bundle,
            )
        return True

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._settings.llm_provider_timeout_seconds,
            verify=self._verify(),
        )

    # ── Авторизация ─────────────────────────────────────────────────────────
    async def _access_token(self) -> str:
        """Получить или переиспользовать access_token (живёт 30 минут)."""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        auth_key = self._settings.gigachat_auth_key.get_secret_value()
        if not auth_key or auth_key.startswith("TBD_"):
            raise LLMError(self.name, "не задан GIGACHAT_AUTH_KEY", retryable=False)

        async with self._client() as client:
            try:
                response = await client.post(
                    self._oauth_url,
                    headers={
                        "Authorization": f"Basic {auth_key}",
                        "RqUID": str(uuid.uuid4()),
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"scope": self._settings.gigachat_scope},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:200]
                raise LLMError(
                    self.name,
                    f"авторизация отклонена ({exc.response.status_code}): {detail}",
                    retryable=exc.response.status_code >= 500,
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(self.name, f"сеть при авторизации: {exc}") from exc

        payload = response.json()
        self._token = payload["access_token"]
        # expires_at приходит в миллисекундах epoch; если его нет — считаем 30 минут.
        expires_at = payload.get("expires_at")
        self._token_expires_at = float(expires_at) / 1000 if expires_at else time.time() + 1800
        return self._token

    async def _switch_to_alt(self, reason: str) -> bool:
        """Переключиться на альтернативный адрес. True — стоит повторить вызов."""
        if self._tried_alt or self._base_url == ALT_BASE_URL:
            return False
        log.warning("GigaChat: адрес %s не работает (%s), пробую %s", self._base_url, reason, ALT_BASE_URL)
        self._tried_alt = True
        self._base_url, self._oauth_url = ALT_BASE_URL, ALT_OAUTH_URL
        self._token = None
        return True

    # ── Основной вызов ──────────────────────────────────────────────────────
    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResult:
        token = await self._access_token()
        body: dict = {
            "model": model or self._settings.gigachat_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": max(temperature, 0.01),  # ноль GigaChat не принимает
            "max_tokens": max_tokens,
        }
        # Строгого json_mode у GigaChat нет — просим форматом в промпте,
        # а разбор терпимый (parse_json_payload в base.py).

        async with self._client() as client:
            try:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=body,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 401:
                    self._token = None  # токен протух — следующий вызов возьмёт новый
                # 403/404 обычно означают, что Сбер перевёл трафик на другой
                # адрес: один раз пробуем альтернативный, потом сдаёмся.
                if status in (403, 404) and await self._switch_to_alt(f"HTTP {status}"):
                    return await self.complete(
                        messages, model=model, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
                    )
                raise LLMError(
                    self.name,
                    f"HTTP {status}: {exc.response.text[:200]}",
                    retryable=status in (401, 429) or status >= 500,
                ) from exc
            except httpx.HTTPError as exc:
                if await self._switch_to_alt(str(exc)):
                    return await self.complete(
                        messages, model=model, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
                    )
                raise LLMError(self.name, f"сеть: {exc}") from exc

        payload = response.json()
        usage_raw = payload.get("usage", {})
        tokens_in = int(usage_raw.get("prompt_tokens", 0))
        tokens_out = int(usage_raw.get("completion_tokens", 0))
        text = payload["choices"][0]["message"]["content"]
        return LLMResult(
            text=text,
            usage=Usage(
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_rub=self.estimate_cost(tokens_in, tokens_out),
                provider=self.name,
                model=body["model"],
            ),
            raw=payload,
        )

    async def health(self) -> tuple[bool, str]:
        if not self._settings.has_secret("gigachat_auth_key"):
            return False, "нет GIGACHAT_AUTH_KEY"
        try:
            result = await self.complete([Message(role="user", content="Ответь одним словом: работает")], max_tokens=64)
        except LLMError as exc:
            return False, str(exc)
        return True, f"ок, модель {result.usage.model}, потрачено {result.usage.cost_rub:.4f} ₽"
