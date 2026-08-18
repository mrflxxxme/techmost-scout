"""Реестр источников после уборки мёртвого кода (Т-25).

Убрано два пути, которые никто не проходил:

* отрасль прогона (`industry`) ехала из оркестратора в `build_adapters` и
  дальше в конструктор адаптера — но принимать её перестала и КиберЛенинка
  (Т-16, РЕШ-68), так что аргумент шёл в никуда;
* пометка «только обогащение» на адаптере заставляла оркестратор пропускать
  источник в поиске — единственный её носитель, OpenAlex, лишился пометки
  (Т-15, РЕШ-71), а ветка осталась.

Тесты здесь — замок на обратное: если у кого-то снова появится конструктор с
отраслью или пометка «только обогащение», это должно быть замечено, а не тихо
работать вхолостую. Сети тесты не касаются: `httpx.AsyncClient` создаётся, но
запросов не делает.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

from app.search import ADAPTERS, build_adapters


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_конструктор_адаптера_не_принимает_отрасль(name: str):
    """Ни один источник не настраивается отраслью запроса.

    Появился такой — путь передачи возвращается вместе с ним и осознанно:
    отрасль не настройка окружения, а данные прогона, и молча она не поедет.
    """
    accepted = inspect.signature(ADAPTERS[name].__init__).parameters

    assert "industry" not in accepted


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_адаптер_не_помечен_только_обогащением(name: str):
    """Пометки `enrich_only` больше не существует, и читать её некому.

    Источник с такой пометкой сегодня был бы опрошен поиском наравне со всеми:
    оставить её на классе — значит получить `NotImplementedError` в прогоне.
    """
    assert not getattr(ADAPTERS[name], "enrich_only", False)


async def test_сборка_адаптеров_не_принимает_отрасль():
    """Аргумента `industry` у `build_adapters` нет: его никто не читает."""
    assert "industry" not in inspect.signature(build_adapters).parameters

    async with httpx.AsyncClient() as client:
        with pytest.raises(TypeError):
            build_adapters(client, None, industry="нефтепереработка")


async def test_бесплатные_источники_собираются_без_настроек():
    """Уборка не задела сборку: без ключей остаются источники, которым ключ не
    нужен, и каждый из них рабочий (`disabled_status` не выставлен)."""
    async with httpx.AsyncClient() as client:
        adapters = build_adapters(client, None)

    names = {adapter.name for adapter in adapters}
    assert {"crossref", "cyberleninka", "arxiv", "openalex"} <= names
    assert all(getattr(adapter, "disabled_status", None) is None for adapter in adapters)
