"""Настройки приложения.

Имена переменных окружения намеренно совпадают с проектом TEAMLY_RU/oriion
(DEEPSEEK_API_KEY, YANDEX_API_KEY, YANDEX_CATALOG_ID, GIGACHAT_AUTH_KEY,
YANDEX_SEARCH_API_KEY, LLM_PROVIDER_TIMEOUT_SECONDS, FX_RATE_USD_TO_RUB) —
чтобы значения переносились копированием, без ручной правки.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Приложение ──────────────────────────────────────────────────────────
    app_env: str = Field(default="dev", validation_alias="APP_ENV")
    database_url: str = Field(default=f"sqlite+aiosqlite:///{REPO_ROOT / 'data' / 'scout.db'}")
    backend_port: int = 8000
    log_level: str = "INFO"

    # Простой общий вход в веб-интерфейс (личных кабинетов в MVP нет)
    web_auth_user: str = Field(default="scout")
    web_auth_password: SecretStr = Field(default=SecretStr("change-me"))

    # ── Языковые модели ─────────────────────────────────────────────────────
    # GigaChat: ключ авторизации = base64(client_id:client_secret) из личного
    # кабинета developers.sber.ru. Бесплатный тариф — 250 млн токенов на Lite.
    gigachat_auth_key: SecretStr = Field(default=SecretStr(""), validation_alias="GIGACHAT_AUTH_KEY")
    gigachat_scope: str = Field(default="GIGACHAT_API_PERS", description="PERS — физлицо, CORP/B2B — организация")
    gigachat_model: str = Field(default="GigaChat-2", description="Lite-модель, самая дешёвая линейка")
    # *.sberbank.ru подписан корневым сертификатом НУЦ Минцифры, которого нет в
    # certifi. Либо подложить бандл с ним, либо (только для локальной отладки)
    # выключить проверку.
    gigachat_verify_ssl: bool = True
    gigachat_ca_bundle: str = ""

    # DeepSeek: с 24.07.2026 алиасы deepseek-chat/deepseek-reasoner отключены,
    # актуальны V4-Flash (дёшево) и V4-Pro. Скидок off-peak больше нет.
    deepseek_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field(default="deepseek-v4-flash")
    deepseek_base_url: str = "https://api.deepseek.com/v1"

    # YandexGPT: Api-Key предпочтительнее IAM-токена (не протухает).
    yandex_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="YANDEX_API_KEY")
    yandex_iam_token: SecretStr = Field(default=SecretStr(""), validation_alias="YANDEX_IAM_TOKEN")
    yandex_catalog_id: str = Field(default="", validation_alias="YANDEX_CATALOG_ID")
    yandex_model: str = Field(default="yandexgpt-lite/latest")

    llm_provider_timeout_seconds: int = 120
    fx_rate_usd_to_rub: float = 100.0

    # Маршрутизация по стадиям — распределение по факту, а не по надежде.
    #
    # Живые прогоны показали: GigaChat-2 Lite надёжен там, где ответ короткий
    # (уточняющие вопросы, извлечение кандидатов), но на длинном
    # структурированном JSON — план поиска, батч оценок, карточка — обрывает
    # ответ на середине и стадия уходит на резерв. Поэтому бесплатный тариф
    # используем там, где он работает, остальное считает DeepSeek: при
    # измеренных 3–5 ₽ на запрос экономить на этом нечего.
    # `replan` (Т-21, РЕШ-81) идёт туда же, куда `planner`: это та же работа —
    # длинный структурированный JSON плана поиска, — а на ней GigaChat-2 Lite
    # обрывает ответ. Без явного маршрута стадия ушла бы по общему резерву,
    # который начинается как раз с gigachat.
    llm_stage_routes: str = Field(
        default=(
            "intake:gigachat,extractor:gigachat,planner:deepseek,replan:deepseek,scorer:deepseek,"
            "card_author:deepseek,verifier:deepseek,summary:deepseek"
        )
    )
    llm_fallback_order: str = Field(default="gigachat,deepseek,yandex")

    # ── Поисковые источники ─────────────────────────────────────────────────
    # Работают без ключа: Crossref, КиберЛенинка, arXiv, OpenAlex. Роспатента
    # в этом списке больше нет: с Т-14 (РЕШ-69) адаптер ходит в поисковую
    # платформу, режима «без ключа» у него не существует, и без
    # ROSPATENT_API_KEY источник не собирается вовсе — патентов не будет ни
    # одного. Бесплатны из четвёрки первые три; поиск OpenAlex ключа не
    # требует, но тарифицируется (см. ниже).
    crossref_mailto: str = Field(default="uklonskiy.k@gmail.com", description="Условие вежливого доступа Crossref")
    # Настройки набора OAI-PMH КиберЛенинки здесь больше нет (Т-25): с Т-16
    # (РЕШ-68) источник ищет через собственный поисковый API, наборов у
    # которого не существует, и значение не читала ни одна строка кода.
    # OpenAlex: ключ НЕ обязателен. Живая анонимная проба 17.08.2026 (только
    # mailto, без ключа) ответила 200 и вернула тариф в заголовках: $0,001 за
    # запрос (те же $1 за 1000 вызовов) при суточной бесплатной норме $0,10 —
    # это сто поисковых запросов в сутки. Поэтому поиск мы используем (РЕШ-10
    # отменён), а расход печатается отдельной строкой в конце `scout run`.
    # Ключ нужен только обогащению по DOI: без него оно пропускается, поиск не
    # задевая. Прежняя запись «с 13.02.2026 ключ обязателен… используем ТОЛЬКО
    # для обогащения» была неверна в обеих половинах (Т-18, П-3).
    openalex_api_key: SecretStr = Field(default=SecretStr(""))
    semanticscholar_api_key: SecretStr = Field(default=SecretStr(""))
    patentsview_api_key: SecretStr = Field(default=SecretStr(""))
    yandex_search_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="YANDEX_SEARCH_API_KEY")
    rospatent_api_key: SecretStr = Field(default=SecretStr(""))
    # Google Programmable Search: мировой веб там, где Яндекс не достаёт.
    # Нужны оба значения — ключ и идентификатор поисковой системы, причём сама
    # система обязана быть настроена на режим «искать по всему вебу».
    # Бесплатно 100 запросов в сутки, дальше $5 за 1000, потолок 10 000 в сутки.
    google_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="GOOGLE_API_KEY")
    google_cse_id: str = Field(default="", validation_alias="GOOGLE_CSE_ID")
    # Ограничение языка выдачи (например lang_en). Пусто = искать на всех
    # языках: в этом и смысл источника.
    google_lang_restrict: str = Field(default="", validation_alias="GOOGLE_LANG_RESTRICT")

    search_timeout_seconds: int = 30
    max_candidates_per_query: int = 20
    target_cards: int = Field(default=6, ge=3, le=10, description="Сколько карточек в отчёте")

    # ── Ограничители ────────────────────────────────────────────────────────
    cost_limit_rub_per_request: float = Field(default=1000.0, description="Стоп-кран по стоимости одного запроса")
    demo_mode_default: bool = False

    # Отсечка по предметному соответствию (РЕШ-48): кандидат с `relevance` ниже
    # порога до карточек не доходит. По умолчанию 0 — отсечка выключена.
    # Значение подбирается по журналу калибровки на живом прогоне, а не
    # угадывается: сохранённых оценок прошлых прогонов не существует.
    relevance_min: int = Field(default=0, ge=0, le=10, description="Порог relevance для отбора; 0 — отсечка выключена")

    # Условный второй проход поиска (РЕШ-81, Т-21): если максимум `relevance` по
    # оценкам МОДЕЛИ первой выдачи строго ниже этого порога, система признаёт
    # выдачу мимо задачи, перестраивает план и ищет второй раз. По умолчанию 0 —
    # проход выключен: при нулевом пороге ни один максимум не окажется строго
    # ниже. Значение подбирается на живом прогоне (Т-29) тем же способом, каким
    # подбирался `RELEVANCE_MIN`, а не угадывается здесь: цена ошибки — лишний
    # платный проход на каждом прогоне.
    second_pass_relevance_max: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Порог включения второго прохода поиска; 0 — второй проход выключен",
    )

    @field_validator("gigachat_scope")
    @classmethod
    def _known_scope(cls, v: str) -> str:
        allowed = {"GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"}
        if v not in allowed:
            raise ValueError(f"неизвестный scope GigaChat: {v}; допустимо {sorted(allowed)}")
        return v

    # ── Производные значения ────────────────────────────────────────────────
    def stage_routes(self) -> dict[str, str]:
        """Разобрать строку маршрутов в словарь «стадия → провайдер»."""
        routes: dict[str, str] = {}
        for chunk in self.llm_stage_routes.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            stage, _, provider = chunk.partition(":")
            routes[stage.strip()] = provider.strip()
        return routes

    def fallback_order(self) -> list[str]:
        return [p.strip() for p in self.llm_fallback_order.split(",") if p.strip()]

    def has_secret(self, name: str) -> bool:
        """Есть ли непустой секрет. Заглушки вида TBD_* считаются отсутствием."""
        value = getattr(self, name, None)
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value or "")
        return bool(raw) and not raw.startswith("TBD_")

    @property
    def gigachat_ca_bundle_path(self) -> str | None:
        """Абсолютный путь к бандлу сертификатов для GigaChat.

        В .env путь пишут относительным (data/ru_bundle.pem), чтобы файл
        оставался переносимым, но httpx считает относительный путь от текущего
        каталога — а команды запускают откуда угодно. Поэтому разворачиваем от
        корня репозитория. Отсутствующий файл возвращаем как None: пусть лучше
        сработает обычная проверка и понятная ошибка, чем загадочный сбой.
        """
        if not self.gigachat_ca_bundle:
            return None
        path = Path(self.gigachat_ca_bundle)
        if not path.is_absolute():
            path = REPO_ROOT / path
        return str(path) if path.exists() else None

    @property
    def prompts_dir(self) -> Path:
        return REPO_ROOT / "prompts"

    @property
    def data_dir(self) -> Path:
        return REPO_ROOT / "data"

    @property
    def out_dir(self) -> Path:
        return REPO_ROOT / "out"


@lru_cache
def get_settings() -> Settings:
    return Settings()
