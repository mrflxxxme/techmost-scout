"""Стадии 2а и 3 — extractor и дедупликация.

Вынесено из `stages.py` (РЕШ-94) без изменения поведения — только перенос
тела функций и нужных импортов. Дедупликация (раздел «3») и подсчёт покрытия
типов источников (раздел «3а») собраны здесь же, а не в отдельных модулях:
план дробления кладёт их сюда, потому что обе функции работают с тем же
списком `Candidate`, который строит extractor.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from pydantic import BaseModel, HttpUrl, TypeAdapter, ValidationError

from app.llm import LLMRouter, Message
from app.pipeline.prompting import _GO, neutralize_placeholders, render_prompt
from app.schemas import Candidate, SourceType
from app.search.base import normalize_url

#: Имя логгера намеренно не `__name__` — см. пояснение в `scoring.py`: код
#: до дробления жил в `app.pipeline.stages`, и тесты фильтруют `caplog` по
#: этой строке.
log = logging.getLogger("app.pipeline.stages")


# ── 2а. Разбор сырой выдачи моделью (extractor) ─────────────────────────────
class ExtractedBatch(BaseModel):
    """Ответ стадии extractor.

    `candidates` объявлены словарями намеренно: записи проверяются поштучно, а
    не всем списком. Со списком `Candidate` одна кривая запись (пустой адрес,
    год строкой) роняла бы разбор всей выдачи, а вместе с ним — вклад платного
    источника целиком.

    А вот **значения по умолчанию у поля быть не должно**, и это защита, а не
    формальность (Н-1 аудита). `_validate_first_fitting` берёт первый блок,
    уложившийся в схему, перебирая блоки от конца текста к началу, — значит
    вложенный объект-кандидат проверяется раньше внешней обёртки. Пока оба поля
    имели умолчания, любой JSON-объект был валиден как ПУСТОЙ батч: хватало
    точки после закрывающей скобки, чтобы срез «от первой скобки до конца»
    перестал разбираться, и разбор выигрывал вложенный объект — оплаченная
    выдача Яндекса стиралась молча, без единого исключения. Со строгим полем
    негодный блок отваливается по схеме, и разбор доходит до настоящей обёртки.
    Ровно этой строгостью защищён `ScoreBatch` и все остальные стадии проекта.
    """

    candidates: list[dict]
    #: Сколько записей модель отбросила из-за отсутствия ссылки; служебное
    #: число из промпта, в пайплайн не идёт
    dropped_no_url: int = 0


#: Тот же тип, что у `Candidate.url`. Нужен, чтобы прогнать через него сырую
#: строку записи — ровно так же, как её уже прогнал pydantic на стороне
#: кандидата (см. `_canonical_url`).
_HTTP_URL = TypeAdapter(HttpUrl)


def _canonical_url(url: str) -> str:
    """Привести адрес к виду, пригодному для сравнения с любым другим адресом.

    Обе стороны сверки проходят ЭТУ функцию, и в этом всё содержание. Раньше
    слева нормализовалась сырая строка записи источника, а справа —
    `str(candidate.url)`, то есть адрес ПОСЛЕ `HttpUrl`; сравнивались разные
    величины. Pydantic percent-кодирует не-ASCII путь (`/статьи` →
    `/%D1%81%D1%82…`), переводит домен в punycode (`наука.рф` →
    `xn--80aa3ak5a.xn--p1ai`) и убирает порт по умолчанию (`:443`).
    `normalize_url` этой разницы не снимает: она отбрасывает схему, `www`,
    параметры и хвостовой слэш, но не кодировку. Поэтому ссылка, скопированная
    моделью из сырой записи БУКВА В БУКВУ, не совпадала сама с собой и
    отбрасывалась как выдуманная — вместе со строкой лога, утверждавшей
    неправду («ссылки нет в выдаче источника»). Список кандидатов при этом
    оставался непустым, так что в `problems` не попадало ничего: тихая потеря
    части оплаченной выдачи, которую откат не страхует.

    Порядок «сначала `HttpUrl`, потом `normalize_url`», а не набор поправок на
    кодировку: канонизацию делает та же библиотека, что построила
    `candidate.url`, поэтому симметрия держится по построению, а не по списку
    учтённых случаев.

    Схема добавляется, если её нет: `normalize_url` её всё равно отбросит, а
    без неё `HttpUrl` адрес не берёт — и запись вида `наука.рф/новости`
    осталась бы некодированной, то есть асимметрия вернулась бы с чёрного
    хода. Адрес, который не берётся и так, нормализуется как есть: он не
    совпадёт с прошедшим `HttpUrl` кандидатом, но это лучше исключения посреди
    разбора уже оплаченной выдачи.
    """
    raw = url.strip()
    with_scheme = raw if "://" in raw else f"https://{raw}"
    try:
        canonical = str(_HTTP_URL.validate_python(with_scheme))
    except ValidationError:
        return normalize_url(raw)
    return normalize_url(canonical)


#: Предел длины цитаты, если модель не назвала границы фрагмента (З-4). Тот же
#: предел, что раньше был жёстко вшит в правило 4 промпта как требование к
#: дословной цитате модели, — теперь это только аварийный потолок кода.
_FALLBACK_SNIPPET_LIMIT = 400


def _numbered_raw_results(raw_results: list[dict]) -> list[dict]:
    """Пронумеровать сырые записи перед показом модели.

    Номер проставляется КОДОМ при сериализации, а не берётся из ответа модели
    (З-4, находка В-1 и совет С-1 аудита Э2-Г): модель ссылается на запись
    номером `index`, а дословную цитату из неё собирает код срезом — выход
    модели перестаёт нести полный текст цитаты на каждого из двадцати
    кандидатов, а переписать цитату своими словами модель уже не может
    (побочная выгода для инварианта №1).
    """
    return [{"index": i, **record} for i, record in enumerate(raw_results)]


def _record_text(record: dict) -> str:
    """Текст записи, из которого код нарезает цитату.

    У неструктурированной выдачи (веб-поиск) это то же поле `snippet`, что сам
    адаптер уже положил в сырую запись (`yandex_search.py`). Запись без такого
    поля — источник другой формы, не ошибка: цитата у кандидата будет пустой,
    а не выброшенной.
    """
    value = record.get("snippet")
    return value if isinstance(value, str) else ""


def _resolve_snippet(record: dict, item: dict, provider: str) -> str:
    """Собрать цитату КОДОМ срезом сырой записи — не тем, что прислала модель.

    Поле `snippet` в ответе модели, если оно есть, здесь даже не читается: цель
    правки — чтобы текст `evidence` физически совпадал с выдачей источника, а
    не с пересказом модели (инвариант №1, критерий 2 задачи З-4).

    Три ветки валидации границ (критерий 3 задачи):
    - границ нет вовсе (не оба поля — целые числа) → цитата — вся запись
      целиком, обрезанная прежним пределом промпта (400 символов);
    - границы есть, но выходят за пределы записи (отрицательное смещение,
      конец за концом текста) → усечение до фактических границ записи, причина
      уходит в лог — кандидат при этом не теряется, теряется только лишний
      хвост цитаты;
    - границы укладываются в запись → ровно заявленный срез.
    """
    text = _record_text(record)
    offset, length = item.get("fragment_offset"), item.get("fragment_length")
    if not isinstance(offset, int) or not isinstance(length, int) or length < 0:
        return text[:_FALLBACK_SNIPPET_LIMIT]
    start = min(max(offset, 0), len(text))
    end = min(max(offset + length, start), len(text))
    if start != offset or end != offset + length:
        log.warning(
            "extractor: границы фрагмента (%s) %s..%s вышли за пределы записи (%s символов) — усечено до %s..%s",
            provider,
            offset,
            offset + length,
            len(text),
            start,
            end,
        )
    return text[start:end]


async def extract_candidates(
    source_type: SourceType,
    provider: str,
    raw_results: list[dict],
    router: LLMRouter,
) -> list[Candidate]:
    """Собрать кандидатов из неструктурированной выдачи источника.

    Нужна источникам без структурированных полей — веб-поиску прежде всего:
    у страницы нет ни аффилиации, ни года публикации, они лежат в тексте.
    Источники со структурированным API (Crossref, arXiv, КиберЛенинка,
    витрина) через стадию не идут — разбор кодом там точнее и бесплатен.

    `source_type` и `provider` навязываются коду, а не берутся из ответа
    модели: правило 9 промпта модель нарушает регулярно, а кандидат с чужим
    провайдером испортил бы и счётчики выдачи, и ограничение доли одного
    источника в отборе.

    Невалидная запись отбрасывается с записью в лог и стадию не роняет
    (инвариант №5): выдача из двадцати записей не должна пропадать из-за одной.
    Так же отбрасывается кандидат с номером записи вне диапазона и кандидат,
    чей адрес не совпадает с адресом названной записи (РЕШ-61 п. 2, ужесточено
    задачей З-4: раньше годился любой адрес из всей выдачи, теперь — только
    адрес именно той записи, чей номер назван).

    Цитату (`snippet`) собирает КОД срезом сырой записи по границам, которые
    назвала модель, — не то, что модель прислала бы в поле `snippet` сама
    (задача З-4, находка В-1 и совет С-1 аудита Э2-Г): промпт, требовавший
    дословную цитату на каждого из двадцати кандидатов, давал ответ на грани
    лимита токенов и стадия срывалась в обрыв JSON. Номер записи и границы
    фрагмента занимают на порядок меньше места в ответе, а переписать цитату
    своими словами модель этим способом уже не может.
    """
    if not raw_results:
        return []
    # Сырая выдача — чужой текст: метка `{{...}}` со страницы иначе уронила бы
    # `render_prompt` уже после оплаченного запроса (см. neutralize_placeholders).
    # Нумерация — до обезвреживания меток и до сериализации: код должен уметь
    # достать raw_results[i] по индексу, который назовёт модель.
    numbered = _numbered_raw_results(raw_results)
    raw_text = neutralize_placeholders(json.dumps(numbered, ensure_ascii=False, indent=2))
    batch = await router.complete_json(
        "extractor",
        [
            Message(
                role="system",
                content=render_prompt(
                    "extractor",
                    SOURCE_TYPE=source_type.value,
                    PROVIDER=provider,
                    RAW_RESULTS=raw_text,
                ),
            ),
            Message(role="user", content=_GO),
        ],
        ExtractedBatch,
        # РЕШ-61 п. 1: столько же, сколько у автора карточек. З-4 понизила
        # фактический выход (номер записи и границы фрагмента вместо дословной
        # цитаты до 400 знаков), но лимит сам по себе не понижаем — щедрость
        # нужна на случай, если модель всё же распишется многословно, а замок
        # `tests/test_stage_token_budgets.py` не пускает стадию ниже 6000.
        max_tokens=7000,
    )

    candidates: list[Candidate] = []
    for item in batch.candidates:
        record_index = item.get("record_index")
        if (
            not isinstance(record_index, int)
            or isinstance(record_index, bool)
            or not (0 <= record_index < len(raw_results))
        ):
            log.warning(
                "extractor: кандидат отброшен (%s) — номер записи %r вне диапазона (записей: %s)",
                provider,
                record_index,
                len(raw_results),
            )
            continue
        record = raw_results[record_index]
        payload = {
            key: value
            for key, value in item.items()
            if key not in ("record_index", "fragment_offset", "fragment_length", "snippet")
        }
        payload["snippet"] = _resolve_snippet(record, item, provider)
        try:
            candidate = Candidate.model_validate({**payload, "source_type": source_type, "provider": provider})
        except ValidationError as exc:
            log.warning("extractor: запись отброшена (%s): %s", provider, exc)
            continue
        record_url = record.get("url")
        if record_url and _canonical_url(str(candidate.url)) != _canonical_url(str(record_url)):
            # До Т-6 адрес веб-кандидата брался кодом из ответа источника —
            # структурная гарантия. Стадия поставила между источником и
            # `evidence` языковую модель, а выдуманный либо усечённый адрес
            # проходит ВСЕ машинные проверки (поле непустое, `HttpUrl` валиден,
            # `evidence` непуст) и падает только там, где инвариант №1
            # проверяет человек. З-4 ужесточает сверку бесплатно: адрес кода из
            # раздела «Вход» уже назван записью — сравниваем именно с ней, а не
            # с любым адресом всей выдачи.
            log.warning(
                "extractor: кандидат отброшен (%s) — адрес %s не принадлежит записи №%s (%s)",
                provider,
                candidate.url,
                record_index,
                record_url,
            )
            continue
        candidates.append(candidate)
    log.info("extractor: %s, сырых записей %s → кандидатов %s", provider, len(raw_results), len(candidates))
    if not candidates:
        # Ноль кандидатов из непустой выдачи — не успех, а пробел: выдача
        # источника уже оплачена, а её вклад в прогон нулевой. Молчание здесь
        # снаружи неотличимо от честного «источник ответил и ничего не нашёл»
        # (Н-1 аудита, инвариант №5). Что делать с пробелом, решает оркестратор
        # — стадия остаётся чистой функцией и только говорит о нём вслух.
        log.warning(
            "extractor: %s вернул ноль кандидатов из %s сырых записей — вклад оплаченной выдачи нулевой",
            provider,
            len(raw_results),
        )
    return candidates


# ── 3. Дедупликация ─────────────────────────────────────────────────────────
def deduplicate(candidates: list[Candidate]) -> list[Candidate]:
    """Склеить дубли по DOI/номеру или нормализованному названию.

    При равенстве ключа выигрывает кандидат из витрины партнёров: он проверен
    человеком и у него есть контакт разработчика.
    """
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        key = candidate.dedup_key()
        current = best.get(key)
        if current is None:
            best[key] = candidate
            continue
        from_showcase = candidate.source_type is SourceType.SHOWCASE
        current_from_showcase = current.source_type is SourceType.SHOWCASE
        richer_snippet = len(candidate.snippet) > len(current.snippet)
        if (from_showcase and not current_from_showcase) or richer_snippet:
            best[key] = candidate
    removed = len(candidates) - len(best)
    if removed:
        log.info("дедупликация убрала %s дублей из %s", removed, len(candidates))
    return list(best.values())


# ── 3а. Покрытие типов источников ───────────────────────────────────────────
#: Типы источников, которые НЕ засчитываются в метрику «покрытие ≥ 4 из:
#: публикации, патенты, гранты, веб» (РЕШ-85 п. 2, РЕШ-173). Сегодня в наборе
#: только `contest`: иначе четвёртый тип метрики добирался бы конкурсами
#: вместо гранта или веба, и метрика приёмки тихо стала бы легче задуманного —
#: ровно та подгонка порога под удобство, которую владелец отверг в РЕШ-76.
COVERAGE_EXCLUDED_SOURCE_TYPES: frozenset[SourceType] = frozenset({SourceType.CONTEST})


def count_source_type_coverage(candidates: Iterable[Candidate]) -> int:
    """Сколько РАЗНЫХ типов источников дала выдача — по правилам метрики РЕШ-85.

    Считаются только типы, засчитываемые метрикой: конкурсы (`contest`) в счёт
    не идут, сколько бы их ни нашлось. Выдача из `publication`, `patent` и
    `contest` даёт покрытие 2, а не 3.
    """
    return len({item.source_type for item in candidates} - COVERAGE_EXCLUDED_SOURCE_TYPES)
