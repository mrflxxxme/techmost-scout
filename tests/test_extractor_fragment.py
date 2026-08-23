"""Критерии приёмки задачи З-4: `extractor` — номер записи и границы фрагмента
вместо дословной цитаты.

Находка В-1 и совет С-1 пост-аудита Э2-Г: на воротном прогоне стадия не
сработала ни разу — три непустые пачки Яндекса дали три невалидных разбора,
1,65 ₽ (18% прогона) ушли в стадию с нулевым выходом. Причина — правило 4
промпта требовало дословную цитату до 400 знаков на каждого из двадцати
кандидатов, ответ обрывался по лимиту токенов. Правка просит номер записи и
границы фрагмента вместо цитаты; саму цитату собирает код срезом сырья.

Три группы проверок (карточка `.planning/PLAN.md`, задача З-4):
1. Ожидаемый выход стадии сократился не менее чем вдвое.
2. Цитату собирает КОД срезом сырья, а не модель.
3. Негодные ссылки на сырьё обработаны штатно — кандидат не теряется молча.

Сети нет (инвариант №7): провайдер модели — заглушка из `conftest.py`.
"""

from __future__ import annotations

import json
import logging

from app.llm import LLMRouter
from app.pipeline import stages
from app.pipeline.stages import load_prompt
from app.schemas import SourceType


def _extractor_router(settings, fake_provider_factory, responses: list[str]):
    """Роутер с заглушкой на месте GigaChat — маршрут стадии `extractor:gigachat`."""
    provider = fake_provider_factory("gigachat", responses)
    return LLMRouter(settings, providers={"gigachat": provider}), provider


# ── Критерий 1: выход стадии сократился не менее чем вдвое ──────────────────

#: 400 знаков — прежний жёсткий предел дословной цитаты (правило 4 промпта до
#: правки). Используем ровно этот объём, чтобы сравнение было честным: старый
#: формат тратит на цитату максимум допустимого правилом, а не произвольную
#: короткую строку, которая занизила бы разницу.
_FILLER_400 = (
    (
        "Показано, что при повышении содержания серы в сырье с 0,8 до 1,6 % масс. "
        "срок службы промышленного катализатора сокращается более чем в два раза, "
        "а межремонтный пробег установки падает пропорционально доле тяжёлых "
        "фракций в исходном сырье, поступающем на блок гидроочистки установки. "
    )
    * 2
)[:400]


def _build_raw_records(n: int = 20) -> list[dict]:
    """20 сырых записей веб-поиска с сниппетом ровно в 400 знаков — тот же
    объём, что раньше был жёстким пределом дословной цитаты модели."""
    assert len(_FILLER_400) == 400
    return [
        {
            "url": f"https://example.org/item-{i}",
            "title": f"Запись номер {i}",
            "domain": "example.org",
            "snippet": _FILLER_400,
        }
        for i in range(n)
    ]


#: Прочие поля кандидата — те же в обоих форматах намеренно: правка меняет
#: только цитату, а не остальную часть контракта, и сравнение честно только
#: тогда, когда неизменная часть одна и та же в обоих форматах. `org: null` и
#: `authors: []` — законное «данных нет» (правило 3 промпта), а не подгонка
#: теста: у веб-записи организация и авторы чаще всего и правда не разобраны
#: кодом, это разбирает следующая стадия.
def _old_format_candidate(i: int, snippet: str) -> dict:
    """Кандидат в СТАРОМ контракте: дословная цитата в поле `snippet`."""
    return {
        "source_type": "web",
        "title": f"З{i}",
        "url": f"https://e.org/{i}",
        "org": None,
        "authors": [],
        "year": 2021,
        "snippet": snippet,
        "raw_id": None,
        "provider": "yandex_search",
    }


def _new_format_candidate(i: int) -> dict:
    """Тот же кандидат в НОВОМ контракте: номер записи и границы фрагмента —
    поле `snippet` вообще не заполняется."""
    return {
        "source_type": "web",
        "title": f"З{i}",
        "url": f"https://e.org/{i}",
        "org": None,
        "authors": [],
        "year": 2021,
        "record_index": i,
        "fragment_offset": 0,
        "fragment_length": 152,
        "raw_id": None,
        "provider": "yandex_search",
    }


def test_выход_стадии_сократился_не_менее_чем_вдвое():
    """Критерий 1: эталонный ответ старого и нового формата на одних и тех же
    20 записях (сниппеты по 400 знаков, критерий приёмки задачи) — новый
    формат не длиннее 40% старого по знакам. Фактическое число — в
    `evidence/E2D-З4-tokens.txt`, здесь закреплён порог."""
    records = _build_raw_records(20)
    old_json = json.dumps(
        {
            "candidates": [_old_format_candidate(i, records[i]["snippet"]) for i in range(20)],
            "dropped_no_url": 0,
        },
        ensure_ascii=False,
    )
    new_json = json.dumps(
        {"candidates": [_new_format_candidate(i) for i in range(20)], "dropped_no_url": 0},
        ensure_ascii=False,
    )

    assert len(new_json) <= 0.4 * len(old_json), (
        f"новый формат {len(new_json)} знаков — не короче 40% старого {len(old_json)} знаков"
    )


# ── Критерий 2: цитату собирает код, а не модель ─────────────────────────────


async def test_цитата_дословно_совпадает_со_срезом_сырья(settings, fake_provider_factory):
    """Модель называет номер записи и границы фрагмента — `candidate.snippet`
    равен ровно тому срезу сырой записи, а не тому, что написала бы модель."""
    raw = [
        {
            "url": "https://ipc.rssi.ru/lab/catalysis",
            "title": "Лаборатория катализа ИПХФ РАН",
            "snippet": "В 2021 году в ИПХФ РАН разработан катализатор гидроочистки дизельных фракций.",
        }
    ]
    ответ = json.dumps(
        {
            "candidates": [
                {
                    "source_type": "web",
                    "title": "Лаборатория катализа ИПХФ РАН",
                    "url": "https://ipc.rssi.ru/lab/catalysis",
                    "org": "ИПХФ РАН",
                    "authors": [],
                    "year": 2021,
                    "record_index": 0,
                    "fragment_offset": 9,
                    "fragment_length": 12,
                    "raw_id": None,
                    "provider": "yandex_search",
                }
            ],
            "dropped_no_url": 0,
        },
        ensure_ascii=False,
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", raw, router)

    assert len(found) == 1
    assert found[0].snippet == raw[0]["snippet"][9:21]


async def test_собственная_цитата_модели_игнорируется(settings, fake_provider_factory):
    """Мутация (а) карточки: если бы код брал цитату из поля `snippet` ответа
    модели, эта проверка провалилась бы — модель здесь присылает выдуманный
    текст, которого в сырой записи нет."""
    raw = [{"url": "https://ipc.rssi.ru/x", "title": "Заголовок", "snippet": "Настоящий текст записи из источника."}]
    ответ = json.dumps(
        {
            "candidates": [
                {
                    "source_type": "web",
                    "title": "Заголовок",
                    "url": "https://ipc.rssi.ru/x",
                    "org": None,
                    "authors": [],
                    "year": None,
                    "record_index": 0,
                    "snippet": "Придуманная моделью цитата, которой в записи нет.",
                    "raw_id": None,
                    "provider": "yandex_search",
                }
            ],
            "dropped_no_url": 0,
        },
        ensure_ascii=False,
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", raw, router)

    assert len(found) == 1
    assert found[0].snippet == raw[0]["snippet"]
    assert "Придуманная" not in found[0].snippet


async def test_без_границ_берётся_запись_целиком_обрезанная_пределом(settings, fake_provider_factory):
    """Границ нет вовсе (правило 4 промпта, третья ветка) — цитата - вся
    запись целиком, обрезанная прежним пределом в 400 знаков."""
    raw = [{"url": "https://ipc.rssi.ru/x", "title": "Заголовок", "snippet": _FILLER_400 * 2}]
    ответ = json.dumps(
        {
            "candidates": [
                {
                    "source_type": "web",
                    "title": "Заголовок",
                    "url": "https://ipc.rssi.ru/x",
                    "authors": [],
                    "record_index": 0,
                    "provider": "yandex_search",
                }
            ],
            "dropped_no_url": 0,
        },
        ensure_ascii=False,
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", raw, router)

    assert len(found) == 1
    assert found[0].snippet == raw[0]["snippet"][:400]
    assert len(found[0].snippet) == 400


# ── Критерий 3: негодные ссылки на сырьё обработаны штатно ──────────────────


async def test_номер_записи_вне_диапазона_кандидат_отброшен(settings, fake_provider_factory, caplog):
    """Мутация (б) карточки: без проверки диапазона номера этот тест обязан
    провалиться — либо кандидат с несуществующим номером просочится в ответ,
    либо код упадёт на индексации вместо честного отброса с причиной в логе."""
    raw = [{"url": "https://ipc.rssi.ru/x", "title": "Заголовок", "snippet": "Текст записи."}]
    ответ = json.dumps(
        {
            "candidates": [
                {
                    "source_type": "web",
                    "title": "Заголовок",
                    "url": "https://ipc.rssi.ru/x",
                    "authors": [],
                    "record_index": 5,
                    "provider": "yandex_search",
                }
            ],
            "dropped_no_url": 0,
        },
        ensure_ascii=False,
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    with caplog.at_level(logging.WARNING, logger="app.pipeline.stages"):
        found = await stages.extract_candidates(SourceType.WEB, "yandex_search", raw, router)

    assert found == []
    assert "вне диапазона" in caplog.text


async def test_границы_за_пределом_записи_усекаются(settings, fake_provider_factory, caplog):
    """Границы, вышедшие за пределы записи, усекаются до фактических границ,
    причина уходит в лог, а кандидат не теряется — теряется только лишний
    хвост цитаты."""
    raw = [{"url": "https://ipc.rssi.ru/x", "title": "Заголовок", "snippet": "Короткий текст записи."}]
    ответ = json.dumps(
        {
            "candidates": [
                {
                    "source_type": "web",
                    "title": "Заголовок",
                    "url": "https://ipc.rssi.ru/x",
                    "authors": [],
                    "record_index": 0,
                    "fragment_offset": 5,
                    "fragment_length": 1000,
                    "provider": "yandex_search",
                }
            ],
            "dropped_no_url": 0,
        },
        ensure_ascii=False,
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    with caplog.at_level(logging.WARNING, logger="app.pipeline.stages"):
        found = await stages.extract_candidates(SourceType.WEB, "yandex_search", raw, router)

    assert len(found) == 1
    assert found[0].snippet == raw[0]["snippet"][5:]
    assert "вышли за пределы записи" in caplog.text


async def test_адрес_из_чужой_записи_кандидат_отброшен(settings, fake_provider_factory, caplog):
    """Мутация (в) карточки — сверка адреса с выдачей снята: кандидат называет
    номер записи A, но адрес записи B — это ужесточённая часть проверки
    (РЕШ-61 п. 2), а не только «адрес есть хоть где-то в выдаче»."""
    raw = [
        {"url": "https://ipc.rssi.ru/a", "title": "Запись A", "snippet": "Текст записи A."},
        {"url": "https://ipc.rssi.ru/b", "title": "Запись B", "snippet": "Текст записи B."},
    ]
    ответ = json.dumps(
        {
            "candidates": [
                {
                    "source_type": "web",
                    "title": "Запись A",
                    "url": "https://ipc.rssi.ru/b",
                    "authors": [],
                    "record_index": 0,
                    "provider": "yandex_search",
                }
            ],
            "dropped_no_url": 0,
        },
        ensure_ascii=False,
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    with caplog.at_level(logging.WARNING, logger="app.pipeline.stages"):
        found = await stages.extract_candidates(SourceType.WEB, "yandex_search", raw, router)

    assert found == []
    assert "не принадлежит записи" in caplog.text


async def test_границы_применяются_к_названной_записи_а_не_к_первой(settings, fake_provider_factory):
    """Мутация (г) карточки — границы применяются к записи с другим номером:
    кандидат ссылается на запись №1, а её текст отличается от записи №0.
    Если код срежет фрагмент не той записи, тест поймает подмену по содержимому."""
    raw = [
        {"url": "https://ipc.rssi.ru/a", "title": "Запись A", "snippet": "ААААА текст записи A."},
        {"url": "https://ipc.rssi.ru/b", "title": "Запись B", "snippet": "БББББ текст записи B."},
    ]
    ответ = json.dumps(
        {
            "candidates": [
                {
                    "source_type": "web",
                    "title": "Запись B",
                    "url": "https://ipc.rssi.ru/b",
                    "authors": [],
                    "record_index": 1,
                    "fragment_offset": 0,
                    "fragment_length": 5,
                    "provider": "yandex_search",
                }
            ],
            "dropped_no_url": 0,
        },
        ensure_ascii=False,
    )
    router, _ = _extractor_router(settings, fake_provider_factory, [ответ])

    found = await stages.extract_candidates(SourceType.WEB, "yandex_search", raw, router)

    assert len(found) == 1
    assert found[0].snippet == raw[1]["snippet"][0:5] == "БББББ"
    assert found[0].snippet != raw[0]["snippet"][0:5]


# ── Критерий 5 (замок на мутацию (д)): промпт не просит дословную цитату ────


def test_промпт_не_требует_дословную_цитату_и_называет_номер_записи():
    """Мутация (д) карточки — правило 4 промпта возвращено к дословной цитате.
    Дублирует ручной `grep` из критерия 5 приёмки прямо в наборе тестов."""
    text = load_prompt("extractor")
    assert "400 символов" not in text
    assert "номер записи" in text
