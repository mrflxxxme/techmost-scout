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


def _urls_of(raw_results: list[dict]) -> set[str]:
    """Канонизированные адреса сырых записей — множество допустимых ссылок.

    Канонизация та же, что применяется к ссылке кандидата (`_canonical_url`), и
    включает нормализацию, которая склеивает веб-дубли: иначе кандидат отвалился
    бы из-за хвостового слэша, регистра или кодировки — из-за оформления, а не
    из-за выдумки.
    """
    return {_canonical_url(str(record["url"])) for record in raw_results if record.get("url")}


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
    Так же отбрасывается кандидат, чьего адреса нет в сырых записях (РЕШ-61
    п. 2). Сверка включается, только когда адреса в записях вообще есть: у
    источника с другой формой записи пустое множество выкосило бы всю выдачу,
    а это уже не защита, а второй способ потерять оплаченный ответ.
    """
    if not raw_results:
        return []
    # Сырая выдача — чужой текст: метка `{{...}}` со страницы иначе уронила бы
    # `render_prompt` уже после оплаченного запроса (см. neutralize_placeholders).
    raw_text = neutralize_placeholders(json.dumps(raw_results, ensure_ascii=False, indent=2))
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
        # РЕШ-61 п. 1: столько же, сколько у автора карточек. Ответ короткий
        # только на вид: двадцать кандидатов с дословной цитатой до 400 знаков
        # каждый — это ≈6 334 токена по измерению пост-аудита, в полтора раза
        # больше прежних 4096. Симптом превышения — не ошибка, а обрыв JSON,
        # который роутер лечит удвоением бюджета и ПОВТОРНЫМ запросом целиком,
        # оплачивая ≈10,7 тыс. входных токенов второй раз.
        max_tokens=7000,
    )

    known_urls = _urls_of(raw_results)
    candidates: list[Candidate] = []
    for item in batch.candidates:
        try:
            candidate = Candidate.model_validate({**item, "source_type": source_type, "provider": provider})
        except ValidationError as exc:
            log.warning("extractor: запись отброшена (%s): %s", provider, exc)
            continue
        if known_urls and _canonical_url(str(candidate.url)) not in known_urls:
            # РЕШ-61 п. 2. До Т-6 адрес веб-кандидата брался кодом из ответа
            # источника — структурная гарантия. Стадия поставила между
            # источником и `evidence` языковую модель, а выдуманная либо
            # усечённая ссылка проходит ВСЕ машинные проверки (поле непустое,
            # `HttpUrl` валиден, `evidence` непуст) и падает только там, где
            # инвариант №1 проверяет человек. Точное множество адресов лежит
            # рядом — в тех же сырых записях, что уехали в модель.
            log.warning(
                "extractor: кандидат отброшен (%s) — ссылки %s нет в выдаче источника",
                provider,
                candidate.url,
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
#: Типы источников, которые НЕ засчитываются в метрику «покрытие типов
#: источников ≥ 4 на запрос» (РЕШ-85 п. 2). Сегодня в наборе только `contest`:
#: иначе четвёртый тип добирался бы конкурсами вместо грантов и организаций, и
#: метрика приёмки тихо стала бы легче задуманного — ровно та подгонка порога
#: под удобство, которую владелец отверг в РЕШ-76.
COVERAGE_EXCLUDED_SOURCE_TYPES: frozenset[SourceType] = frozenset({SourceType.CONTEST})


def count_source_type_coverage(candidates: Iterable[Candidate]) -> int:
    """Сколько РАЗНЫХ типов источников дала выдача — по правилам метрики РЕШ-85.

    Считаются только типы, засчитываемые метрикой: конкурсы (`contest`) в счёт
    не идут, сколько бы их ни нашлось. Выдача из `publication`, `patent` и
    `contest` даёт покрытие 2, а не 3.
    """
    return len({item.source_type for item in candidates} - COVERAGE_EXCLUDED_SOURCE_TYPES)
