"""Страж секретов на путях `search/` и `llm/` (З-6, аудит Э2-Д).

РЕШ-124 закрыт частично: единственный страж по фактическому выводу
(`tests/test_run_visibility.py`) гоняет прогон с `adapters=[]` и подставным
провайдером — печать ключа внутри `YandexSearchAdapter._run_search` или
`DeepSeekProvider.complete` он не увидел бы вовсе, потому что ни один из этих
двух классов там ни разу не вызывается. Здесь оба класса — настоящие, с
respx-подмененной сетью и заведомо фиктивными уникальными ключами; страж читает
не исходный код, а фактический `caplog`/`stdout`/`stderr` перехваченного
прогона (тот же метод, что и в `test_run_visibility.py`).

`tests/test_run_visibility.py` этой задачей не правится и не ослабляется
(граница плана) — здесь отдельный, самостоятельный набор.
"""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest
import respx
from rich.console import Console

from app import cli
from app.config import Settings, get_settings
from app.llm import LLMRouter
from app.llm.base import LLMError, LLMProvider, LLMResult, Message
from app.llm.deepseek import DeepSeekProvider
from app.pipeline.orchestrator import ScoutPipeline
from app.search.yandex_search import SEARCH_ASYNC_URL, YandexSearchAdapter

log = logging.getLogger(__name__)

#: Уникальные фиктивные значения ключей — не настоящие секреты (инвариант №3).
#: Уникальные (а не как в `conftest.TEST_ENV`), чтобы страж указывал ровно на
#: свою утечку, а не на случайное совпадение с чужим тестовым значением.
_FAKE_YANDEX_SEARCH_KEY = "FAKE-YANDEX-SEARCH-KEY-guard-e91cf047"
_FAKE_DEEPSEEK_KEY = "FAKE-DEEPSEEK-KEY-guard-771ba4d9"
_FAKE_YANDEX_LLM_KEY = "FAKE-YANDEX-LLM-KEY-guard-06c9e2b3"

#: Формулировка веб-плана: тип `web`, чтобы формулировка реально ушла в
#: `YandexSearchAdapter` (у него `source_types = (WEB, CONTEST)`; формулировка
#: типа `publication`, как в `test_run_visibility.py`, до адаптера не дошла бы
#: вовсе — оркестратор пропускает несовпадающие типы формулировок).
_WEB_PLAN_JSON = json.dumps(
    {
        "queries": [
            {
                "text": "катализатор гидроочистки серостойкий разработка",
                "source_type": "web",
                "lang": "ru",
                "rationale": "прямой веб-поиск по задаче",
            }
        ]
    },
    ensure_ascii=False,
)


def _assert_no_secrets(secrets: tuple[str, ...], *blobs: str) -> None:
    """Ни одно фиктивное значение ключа не встречается ни в одном перехваченном тексте."""
    for secret in secrets:
        for blob in blobs:
            assert secret not in blob, "значение ключа обнаружено в перехваченном выводе прогона"


# ── Критерий 1: путь адаптера (YandexSearchAdapter._run_search) ─────────────
@pytest.mark.parametrize("status_code", [401, 500])
async def test_отказ_yandex_search_не_печатает_ключ(
    status_code, settings, brief, fake_provider_factory, caplog, capsys
):
    """Настоящий `YandexSearchAdapter` с фиктивным ключом опрашивается,
    respx отвечает отказом (401 — плохой ключ, 500 — сбой источника); адаптер
    поднимает `SearchError` (инвариант №5), пайплайн доходит до пустого
    отчёта. Ни один фиктивный ключ не имеет права попасть в лог или в
    перехваченные потоки."""
    caplog.set_level(logging.DEBUG)
    with respx.mock:
        respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(status_code, json={"message": "отказ поиска"}))

        async with httpx.AsyncClient() as client:
            adapter = YandexSearchAdapter(client, api_key=_FAKE_YANDEX_SEARCH_KEY, folder_id="b1gguardtest")
            provider = fake_provider_factory("deepseek", [_WEB_PLAN_JSON])
            router = LLMRouter(settings, providers={"deepseek": provider})
            pipeline = ScoutPipeline(settings=settings, router=router, adapters=[adapter])

            report = await pipeline.run(brief, request_id=1)

    # Мутация (в): пустой список адаптеров прошёл бы этот тест без единого
    # обращения к источнику — страж проверял бы только собственную тишину.
    assert adapter.queries_used >= 1, "адаптер обязан был реально вызваться, а не остаться нетронутым"
    assert "yandex_search" in report.sources_unavailable

    captured = capsys.readouterr()
    _assert_no_secrets((_FAKE_YANDEX_SEARCH_KEY,), caplog.text, captured.out, captured.err)


# ── Критерий 2: путь модели (DeepSeekProvider.complete + резерв роутера) ────
async def test_отказ_deepseek_и_исчерпанный_резерв_не_печатают_ключ(brief, caplog, capsys, monkeypatch):
    """Настоящий `DeepSeekProvider` с фиктивным ключом отвечает 401 через
    respx; резервный провайдер тоже отказывает — роутер исчерпывает цепочку
    и поднимает `LLMError`. Сообщения роутера («перехожу к резерву», «все
    провайдеры отказали») печатаются в лог — ни в одном не может быть значения
    ключа ни одного из двух провайдеров."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("DEEPSEEK_API_KEY", _FAKE_DEEPSEEK_KEY)
    monkeypatch.setenv("YANDEX_API_KEY", _FAKE_YANDEX_LLM_KEY)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("YANDEX_CATALOG_ID", "b1gguardtest")
    monkeypatch.setenv("COST_LIMIT_RUB_PER_REQUEST", "10.0")
    get_settings.cache_clear()
    fake_settings = Settings()

    class _ReserveAlsoFails(LLMProvider):
        """Второй провайдер цепочки (резерв YandexGPT) — тоже отказывает, без похода в сеть."""

        name = "yandex"
        price_in_rub_per_mtok = 0.0
        price_out_rub_per_mtok = 0.0

        async def complete(
            self,
            messages: list[Message],
            *,
            model: str | None = None,
            temperature: float = 0.2,
            max_tokens: int = 4096,
            json_mode: bool = False,
        ) -> LLMResult:
            raise LLMError(self.name, "тестовый безусловный отказ резерва", retryable=False)

        async def health(self) -> tuple[bool, str]:
            return False, "заглушка"

    try:
        with respx.mock:
            respx.post(f"{fake_settings.deepseek_base_url.rstrip('/')}/chat/completions").mock(
                return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}})
            )

            real_deepseek = DeepSeekProvider(fake_settings)
            router = LLMRouter(fake_settings, providers={"deepseek": real_deepseek, "yandex": _ReserveAlsoFails()})
            pipeline = ScoutPipeline(settings=fake_settings, router=router, adapters=[])

            with pytest.raises(LLMError):
                await pipeline.run(brief, request_id=1)
    finally:
        get_settings.cache_clear()

    captured = capsys.readouterr()
    _assert_no_secrets((_FAKE_DEEPSEEK_KEY, _FAKE_YANDEX_LLM_KEY), caplog.text, captured.out, captured.err)


# ── Критерий отдельной ветки: `scout doctor` называет имена, а не значения ──
class _DoctorFakeRouter:
    """Роутер-заглушка для `_doctor()`: без неё проверка моделей ходит в сеть
    живыми ключами (тот же приём, что в `tests/test_openalex.py:_run_doctor`)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def health(self) -> dict[str, tuple[bool, str]]:
        return {"deepseek": (True, "заглушка")}


async def _run_doctor_with_fake_keys(monkeypatch) -> str:
    """Прогнать `scout doctor` без сети и БД, с фиктивными уникальными ключами.

    Подменяются те же три опоры, что и в `tests/test_openalex.py:_run_doctor`:
    инициализация БД, роутер моделей и реестр источников. Ключи читаются
    настоящей `Settings` тем же путём, что в боевом коде — таблицу ключей
    строит непосредственно `_doctor`, не подмена.
    """
    import app.db

    monkeypatch.setenv("DEEPSEEK_API_KEY", _FAKE_DEEPSEEK_KEY)
    monkeypatch.setenv("YANDEX_API_KEY", _FAKE_YANDEX_LLM_KEY)
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", _FAKE_YANDEX_SEARCH_KEY)
    monkeypatch.setenv("YANDEX_CATALOG_ID", "b1gguardtest")
    get_settings.cache_clear()

    async def fake_init_db() -> None:
        return None

    monkeypatch.setattr(app.db, "init_db", fake_init_db)
    monkeypatch.setattr("app.search.build_adapters", lambda client, settings, **kwargs: [])
    monkeypatch.setattr(cli, "LLMRouter", _DoctorFakeRouter)
    recorder = Console(file=io.StringIO(), width=200, record=True)
    monkeypatch.setattr(cli, "console", recorder)

    try:
        await cli._doctor()
    finally:
        get_settings.cache_clear()
    return recorder.export_text()


async def test_scout_doctor_называет_имена_ключей_а_не_значения(monkeypatch, caplog, capsys):
    """Таблица ключей доктора обязана называть переменные окружения и статус
    («есть»/«нет»), а не значения (инвариант №3, РЕШ-155: часть значения —
    тоже значение)."""
    caplog.set_level(logging.DEBUG)

    text = await _run_doctor_with_fake_keys(monkeypatch)

    # Имена переменных — обязаны быть в выводе, иначе доктором нечем пользоваться.
    assert "DEEPSEEK_API_KEY" in text
    assert "YANDEX_SEARCH_API_KEY" in text
    # Статус "есть" — ключ распознан непустым, но напечатано имя, не значение.
    assert "есть" in text

    captured = capsys.readouterr()
    _assert_no_secrets(
        (_FAKE_DEEPSEEK_KEY, _FAKE_YANDEX_LLM_KEY, _FAKE_YANDEX_SEARCH_KEY),
        text,
        caplog.text,
        captured.out,
        captured.err,
    )


# ── Критерий 3: страж действительно ловит утечку, а не молчит по построению ─
async def test_страж_ловит_утечку_в_stdout_а_не_только_в_caplog(settings, brief, fake_provider_factory, caplog, capsys):
    """Контроль плана (критерий 3): временно подсаженная утечка в `stdout`
    (через `print`, минуя логирование) обязана провалить проверку. Мутации
    (а)/(б) задачи — «перехват capsys убран» и «проверяется только caplog» —
    именно эту проверку и убивают: без неё "тест" остаётся зелёным даже когда
    значение ключа честно напечатано, просто не через `logging`.

    Подмена временная и живёт только внутри этого теста — исходники
    `search/`/`llm/` она не трогает."""
    caplog.set_level(logging.DEBUG)
    with respx.mock:
        respx.post(SEARCH_ASYNC_URL).mock(return_value=httpx.Response(500, json={"message": "отказ поиска"}))

        async with httpx.AsyncClient() as client:
            adapter = YandexSearchAdapter(client, api_key=_FAKE_YANDEX_SEARCH_KEY, folder_id="b1gguardtest")
            provider = fake_provider_factory("deepseek", [_WEB_PLAN_JSON])
            router = LLMRouter(settings, providers={"deepseek": provider})
            pipeline = ScoutPipeline(settings=settings, router=router, adapters=[adapter])

            await pipeline.run(brief, request_id=1)
            # Подсаженная утечка: код продукта её не печатает — это временная
            # проверка самого стража, а не поведение адаптера или провайдера.
            print(_FAKE_YANDEX_SEARCH_KEY)

    captured = capsys.readouterr()
    with pytest.raises(AssertionError):
        _assert_no_secrets((_FAKE_YANDEX_SEARCH_KEY,), caplog.text, captured.out, captured.err)
