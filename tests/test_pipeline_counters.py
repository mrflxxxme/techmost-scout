"""Счётчик кандидатов в `gather_candidates` — раздельный учёт по контурам (З-0).

Проверяем ровно две вещи из задачи: `hits_by_provider` ключуется меткой
контура (`SearchAdapter.contour_label`), а не именем адаптера, и при этом
`sources_unavailable` — тот список, что печатается в клиентском отчёте
(`backend/app/export/markdown.py`) — остаётся по имени адаптера. Поддельные
адаптеры ниже не ходят в сеть: они синхронно возвращают заранее заданные
кандидаты или падают исключением, как настоящий адаптер при отказе источника.
"""

from __future__ import annotations

import httpx

from app.llm import LLMRouter
from app.pipeline.orchestrator import ScoutPipeline
from app.schemas import Candidate, SearchPlan, SearchQuery, SourceType
from app.search.base import SearchAdapter


def _candidate(url: str, *, source_type: SourceType = SourceType.WEB, provider: str = "yandex_search") -> Candidate:
    return Candidate(source_type=source_type, title="Заголовок", url=url, provider=provider)


class _FakeYandexLikeAdapter(SearchAdapter):
    """Как настоящий Яндекс: одно имя адаптера, метка контура — по языку
    формулировки. Правило то же самое, что в `YandexSearchAdapter`, здесь оно
    продублировано намеренно — тест не должен зависеть от реализации адаптера,
    только от контракта `contour_label`."""

    name = "yandex_search"
    source_types = (SourceType.WEB,)

    def __init__(self, client: httpx.AsyncClient, *, fail: bool = False) -> None:
        super().__init__(client)
        self._fail = fail

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        if self._fail:
            raise RuntimeError("яндекс временно недоступен")
        return [_candidate(f"https://example.org/page-{query.lang}")]

    def contour_label(self, query: SearchQuery) -> str:
        return "yandex-ru" if query.lang == "ru" else "yandex-com"


class _FakeCrossrefAdapter(SearchAdapter):
    """Обычный источник: `contour_label` не переопределён, значит равен имени."""

    name = "crossref"
    source_types = (SourceType.PUBLICATION,)

    async def search(self, query: SearchQuery, *, limit: int = 20) -> list[Candidate]:
        return [_candidate("https://doi.org/10.1/example", source_type=SourceType.PUBLICATION, provider="crossref")]


def _pipeline(settings, fake_provider_factory, adapters: list[SearchAdapter]) -> ScoutPipeline:
    """Пайплайн с зафиксированными адаптерами и провайдером-заглушкой моделей —
    gather_candidates модели не вызывает, но конструктор роутера её требует."""
    router = LLMRouter(settings, providers={"fake": fake_provider_factory("fake")})
    return ScoutPipeline(settings=settings, router=router, adapters=adapters)


async def test_план_с_русской_и_английской_формулировкой_даёт_метки_yandex_ru_и_yandex_com(
    settings, fake_provider_factory
):
    """Раздельный счёт контуров: план из русской и английской формулировки —
    в hits_by_provider два разных ключа с числами, а не один по имени адаптера.
    Третья формулировка идёт в другой источник и остаётся под его именем."""
    plan = SearchPlan(
        queries=[
            SearchQuery(text="катализатор", source_type=SourceType.WEB, lang="ru", rationale="тест"),
            SearchQuery(text="catalyst deactivation", source_type=SourceType.WEB, lang="en", rationale="тест"),
            SearchQuery(text="катализатор", source_type=SourceType.PUBLICATION, lang="ru", rationale="тест"),
        ]
    )
    async with httpx.AsyncClient() as client:
        adapters: list[SearchAdapter] = [_FakeYandexLikeAdapter(client), _FakeCrossrefAdapter(client)]
        outcome = await _pipeline(settings, fake_provider_factory, adapters).gather_candidates(plan)

    assert outcome.hits_by_provider == {"yandex-ru": 1, "yandex-com": 1, "crossref": 1}
    assert outcome.unavailable == []


async def test_вклад_остальных_источников_остаётся_под_прежним_именем(settings, fake_provider_factory):
    """Источник без своего contour_label не меняет поведения: ключ — имя адаптера,
    как было до задачи."""
    plan = SearchPlan(
        queries=[SearchQuery(text="катализатор", source_type=SourceType.PUBLICATION, lang="ru", rationale="тест")]
    )
    async with httpx.AsyncClient() as client:
        outcome = await _pipeline(settings, fake_provider_factory, [_FakeCrossrefAdapter(client)]).gather_candidates(
            plan
        )

    assert outcome.hits_by_provider == {"crossref": 1}


async def test_отказ_яндекса_виден_в_sources_unavailable_по_имени_адаптера_а_не_по_метке_контура(
    settings, fake_provider_factory
):
    """Инвариант №5 и критерий 5 задачи: `sources_unavailable` печатается в
    клиентском отчёте строкой «Не ответили источники: …» — его состав не
    трогаем. Отказ Яндекса даёт в списке `yandex_search`, а не `yandex-ru`
    или `yandex-com`."""
    plan = SearchPlan(
        queries=[
            SearchQuery(text="катализатор", source_type=SourceType.WEB, lang="ru", rationale="тест"),
            SearchQuery(text="catalyst deactivation", source_type=SourceType.WEB, lang="en", rationale="тест"),
        ]
    )
    async with httpx.AsyncClient() as client:
        adapters: list[SearchAdapter] = [_FakeYandexLikeAdapter(client, fail=True)]
        outcome = await _pipeline(settings, fake_provider_factory, adapters).gather_candidates(plan)

    assert outcome.unavailable == ["yandex_search"]
    assert "yandex-ru" not in outcome.unavailable
    assert "yandex-com" not in outcome.unavailable
    assert outcome.hits_by_provider == {}
