"""Ссылка на белорусские публикации ведёт на Белгоспатент, а не в 404 (РЕШ-217).

Владелец: Google Patents не отдаёт публикации `BY` ни в одном формате — 404
подтверждён дважды (аудит и повторный аудит, РЕШ-209). Рабочий шаблон найден
проверкой по первоисточнику (не догадкой) в handoff задачи Э4-П16:

    https://search.ncip.by/database/index.php?pref=inv&lng=ru&page=2&patent={число}

— страница результатов поиска по базе изобретений НЦИС, номер патента без
ведущих нулей и без префикса страны. Проверено `curl` по всем шести уникальным
номерам `BY` из `data/scout.db` (девять ссылок в базе, шесть уникальных
патентов): каждый запрос вернул HTTP 200 и ровно одну найденную запись, чьё
название совпало с формальным названием изобретения. Фактические ответы —
в `.planning/evidence/Э4-П16-handoff.yaml`.

Сети здесь нет (инвариант №7): ответы платформы Роспатента подменяются через
respx, как и в `test_rospatent.py`; сам `curl` по Белгоспатенту в тестовый
набор не входит.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.schemas import SearchQuery, SourceType
from app.search.rospatent import SEARCH_PLATFORM_URL, RospatentAdapter, _document_url


def _hit(*, office: str, number: str, kind: str, title: str) -> dict:
    return {
        "id": f"{office}{number}{kind}_20200101",
        "common": {
            "publishing_office": office,
            "document_number": number,
            "kind": kind,
            "publication_date": "2019.04.30",
        },
        "biblio": {"ru": {"title": title, "applicant": [{"name": "Испытуемая организация"}]}},
        "snippet": {"title": title, "description": "Описание изобретения.", "lang": "ru"},
    }


def _payload(*hits: dict) -> dict:
    return {"available": len(hits), "total": len(hits), "timings": {"search": 1}, "hits": list(hits)}


def _query() -> SearchQuery:
    return SearchQuery(text="испытание", source_type=SourceType.PATENT, lang="ru", rationale="тест")


@pytest.fixture
def adapter_factory():
    def make(client: httpx.AsyncClient) -> RospatentAdapter:
        adapter = RospatentAdapter(client, api_key="test-key", contact_email="owner@example.org")
        adapter.min_interval_seconds = 0.0
        return adapter

    return make


#: Реальные номера `BY` из `data/scout.db` (только чтение, `mode=ro`):
#: `select url from candidates where url like '%patents.google.com/patent/BY%'`
#: даёт девять ссылок с ведущими нулями (`BY0000022510C1` и т. п.) — шесть
#: уникальных номеров патентов. Все проверены `curl` по адресу Белгоспатента:
#: HTTP 200, единственная найденная запись, название совпало.
BY_РЕАЛЬНЫЕ_ИЗ_БАЗЫ = [
    ("BY0000022510C1", "22510"),
    ("BY0000019778C1", "19778"),
    ("BY0000022858C1", "22858"),
    ("BY0000021648C1", "21648"),
    ("BY0000016737C1", "16737"),
    ("BY0000015595C1", "15595"),
]


@pytest.mark.parametrize(("raw", "significant"), BY_РЕАЛЬНЫЕ_ИЗ_БАЗЫ)
def test_by_ссылка_ведёт_на_белгоспатент_без_ведущих_нулей(raw, significant):
    """Критерий 2 задачи: адрес для `BY` строится по найденному шаблону
    (реестр Белгоспатента), не на Google Patents."""
    expected = f"https://search.ncip.by/database/index.php?pref=inv&lng=ru&page=2&patent={significant}"
    assert _document_url(raw) == expected


@pytest.mark.parametrize(("raw", "significant"), BY_РЕАЛЬНЫЕ_ИЗ_БАЗЫ)
@respx.mock
async def test_by_сквозной_случай_карточка_получает_ссылку_на_белгоспатент(adapter_factory, raw, significant):
    """Тест закрепляет формат на реальных номерах `BY` из базы (критерий 3),
    проходя через полный `search()`, а не только через `_document_url`."""
    title = "Испытываемое белорусское изобретение"
    office, number, kind = raw[:2], raw[2:-2], raw[-2:]
    respx.post(SEARCH_PLATFORM_URL).mock(
        return_value=httpx.Response(200, json=_payload(_hit(office=office, number=number, kind=kind, title=title)))
    )
    async with httpx.AsyncClient() as client:
        found = await adapter_factory(client).search(_query(), limit=1)

    assert len(found) == 1
    assert found[0].raw_id == raw
    assert str(found[0].url) == f"https://search.ncip.by/database/index.php?pref=inv&lng=ru&page=2&patent={significant}"
    assert "patents.google.com" not in str(found[0].url)


def test_by_нераспознанный_формат_не_ломается_на_google_patents():
    """Номер `BY`, не разобравшийся по шаблону «страна+цифры+вид» (в базе
    таких нет — на будущее), не должен приводить к ошибке: адрес уходит в
    общую ветку Google Patents, а не выдумывается для Белгоспатента."""
    assert _document_url("BY-без-структуры") == "https://patents.google.com/patent/BY-без-структуры"


def test_by_другие_офисы_по_прежнему_ведут_на_google_patents():
    """Смежная проверка: правка `_document_url` не задела прочие офисы —
    они как строили ссылку на Google Patents, так и строят."""
    assert _document_url("RU2612531C2") == "https://patents.google.com/patent/RU2612531C2"
    assert _document_url("WO2018038642A1") == "https://patents.google.com/patent/WO2018038642A1"
