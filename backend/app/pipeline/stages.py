"""Стадии пайплайна.

Каждая стадия — чистая функция: получает данные и роутер, возвращает данные.
Никакой работы с базой внутри стадий, чтобы их можно было тестировать на
фикстурах без сети и без БД.

Порядок: интейк → план → поиск → дедуп → скоринг → группировка → карточки →
проверка → сводка.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field, HttpUrl, TypeAdapter, ValidationError, field_validator

from app.config import get_settings
from app.llm import CostLimitExceededError, LLMRouter, Message
from app.schemas import (
    Brief,
    Candidate,
    Confidence,
    IntakeResult,
    MaturityStage,
    ReportSummary,
    ScoredCandidate,
    SearchPlan,
    SolutionCard,
    SourceType,
)
from app.search.base import normalize_url

log = logging.getLogger(__name__)


class CandidateScore(BaseModel):
    """Оценка одного кандидата в ответе модели.

    Модель ссылается на кандидата номером, а не пересказывает его поля: иначе
    ответ на батч из десяти кандидатов не влезает в лимит токенов и JSON
    обрывается на середине. Сопоставление номеров с кандидатами делает код.
    """

    index: int = Field(ge=0)
    relevance: int = Field(ge=0, le=10)
    maturity: int = Field(ge=0, le=10)
    accessibility: int = Field(ge=0, le=10)
    freshness: int = Field(ge=0, le=10)
    completeness: int = Field(ge=0, le=10)
    notes: str = ""


class ScoreBatch(BaseModel):
    """Обёртка ответа скоринга: модели проще вернуть объект, чем голый массив."""

    scores: list[CandidateScore]


class VerifyResult(BaseModel):
    """Ответ проверяющей стадии по одной карточке."""

    needs_rework: bool = False
    confidence: Confidence = Confidence.MEDIUM
    confidence_note: str = ""
    unsourced_claims: list[str] = []
    notes: str = ""

    @field_validator("notes", "confidence_note", mode="before")
    @classmethod
    def _join_lists(cls, value: object) -> object:
        """Дешёвые модели любят возвращать заметки списком — склеиваем в строку."""
        if isinstance(value, list):
            return "; ".join(str(item) for item in value)
        return value


def load_prompt(name: str) -> str:
    """Прочитать промпт из prompts/. Промпты — часть контракта, а не строки в коде."""
    path = get_settings().prompts_dir / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"нет промпта {path}. См. docs/SPEC.md, раздел про prompts/")
    return path.read_text(encoding="utf-8")


def load_gov_support() -> str:
    """Справочник инструментов господдержки — подаётся модели как контекст."""
    path = get_settings().data_dir / "gov_support.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def render_prompt(name: str, **values: str) -> str:
    """Подставить данные в промпт.

    Промпты содержат метки вида {{BRIEF_JSON}} — это часть их контракта. Если
    метку не заполнить, модель получит её текстом и начнёт рассуждать о самой
    метке вместо данных: именно так пайплайн какое-то время выдавал карточки с
    низкой уверенностью и жалобами «кандидаты не переданы».

    Поэтому незаполненная метка — ошибка, а не мелочь: падаем сразу и громко.
    """
    text = load_prompt(name)
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    leftover = sorted(set(_PLACEHOLDER.findall(text)))
    if leftover:
        raise ValueError(f"промпт {name}: не заполнены метки {leftover}. Передайте их в render_prompt")
    return text


def candidates_to_json(group: list[ScoredCandidate]) -> str:
    """Кандидаты для модели: только поля, по которым можно сверять утверждения."""
    return json.dumps(
        [
            {
                "url": str(item.candidate.url),
                "title": item.candidate.title,
                "org": item.candidate.org,
                "authors": item.candidate.authors,
                "year": item.candidate.year,
                "source_type": item.candidate.source_type.value,
                "provider": item.candidate.provider,
                # Обрезаем: длинные аннотации раздувают вход, а для сверки
                # утверждений хватает первых полутора тысяч знаков.
                "snippet": item.candidate.snippet[:1500],
            }
            for item in group
        ],
        ensure_ascii=False,
        indent=2,
    )


#: Короткая команда в роли пользователя: всё содержательное уже в системном
#: промпте вместе с данными, здесь только сигнал «работай».
_GO = "Выполни инструкцию выше. Верни только JSON без пояснений."


#: Предел длины `task_summary` запасного брифа — короткое описание, а не
#: письмо целиком; отчёту хватает первых сотен знаков для сверки.
_FALLBACK_SUMMARY_LIMIT = 500


def owner_answers_block(brief: Brief) -> str:
    """Ответы владельца на уточняющие вопросы — блоком для сообщения модели.

    Нужен там, где бриф уходит в промпт не целиком: в стадиях `plan_search`,
    `author_card` и `summarize` бриф подаётся как `BRIEF_JSON`, и поле
    `owner_answers` попадает в модель само, а скоринг собирает сообщение из
    отдельных полей руками — без этого блока ответы владельца до него не
    доходили бы (РЕШ-58).

    Пустая строка, когда ответов нет: пустой заголовок в промпте — это шум,
    который дешёвая модель принимается толковать.
    """
    if not brief.owner_answers:
        return ""
    lines = "\n".join(f"— {item.question}\n  Ответ: {item.answer}" for item in brief.owner_answers)
    return f"Уточнения владельца (контекст постановки, не ограничения задачи):\n{lines}\n\n"


#: Настоящая метка шаблона, пришедшая не из шаблона, а из данных: имя внутри
#: — ровно то же, что ищет `_PLACEHOLDER`, потому что обезвреживать надо
#: строго то, на чём падает `render_prompt`, и ничего сверх того.
_DATA_PLACEHOLDER = re.compile(r"\{{2,}([A-Z_]+)\}{2,}")


def neutralize_placeholders(text: str) -> str:
    """Обезвредить метки вида `{{ИМЯ}}`, пришедшие из сырого ввода оператора.

    `render_prompt` ищет незаполненные метки в **уже собранном** тексте, а не в
    шаблоне до подстановки, поэтому метка, попавшая в промпт из данных, роняет
    вызов ничуть не хуже забытой метки шаблона: `brief_from_raw_text` переносит
    текст оператора дословно, и задача с `{{CLIENT_NAME}}` детерминированно
    убивала прогон на планировщике.

    Лечим данные, а не проверку: общий контракт `render_prompt` — «незаполненная
    метка это ошибка, падаем сразу и громко» — стоит дороже, чем этот случай,
    и ослаблять его ради него нельзя. Скобки метки схлопываются в одинарные:
    `{{CLIENT_NAME}}` → `{CLIENT_NAME}`. Текст остаётся читаемым (его увидит
    клиент в отчёте), а под шаблон метка больше не подходит; длина серии скобок
    значения не имеет — `{{{{X}}}}` схлопывается за один проход.

    Правится **только настоящая метка**, а не любые сдвоенные скобки (П-6
    аудита). Прежняя редакция схлопывала их без разбора и портила содержание:
    `{"a": {"b": 1}}` превращался в сломанный `{"a": {"b": 1}`, а `if (x) {{
    y(); }}` — в `{ y(); }`. Для стадии extractor это прямой удар по главному:
    промпт требует ДОСЛОВНОЙ цитаты, а модель цитировала бы уже испорченный
    текст, и в карточку попадала бы цитата, не совпадающая со страницей.
    """
    return _DATA_PLACEHOLDER.sub(r"{\1}", text)


def brief_from_raw_text(raw_text: str, *, industry_hint: str = "не указана") -> Brief:
    """Собрать бриф без вызова модели — запасной путь при отказе интейка.

    Ничего не домысливает (инвариант №1): отрасль берётся из подсказки
    оператора либо остаётся «не указана», `task_summary` — обрезанный сырой
    текст задачи, остальные поля брифа пустые. Единственная правка текста —
    обезвреживание меток `{{...}}`, см. `neutralize_placeholders`.
    """
    summary = neutralize_placeholders(raw_text.strip())
    if len(summary) > _FALLBACK_SUMMARY_LIMIT:
        summary = summary[:_FALLBACK_SUMMARY_LIMIT].rstrip() + "…"
    return Brief(industry=neutralize_placeholders(industry_hint), task_summary=summary or "не указано")


# ── 1. Интейк ───────────────────────────────────────────────────────────────
async def run_intake(router: LLMRouter, raw_text: str, *, industry_hint: str = "не указана") -> IntakeResult:
    """Из свободного текста — уточняющие вопросы и черновик брифа."""
    return await router.complete_json(
        "intake",
        [
            Message(
                role="system",
                content=render_prompt("intake", RAW_TASK_TEXT=raw_text, INDUSTRY_HINT=industry_hint),
            ),
            Message(role="user", content=_GO),
        ],
        IntakeResult,
        # Т-13: стадия жила на умолчании роутера в 4096 — а это ровно тот
        # скупой лимит, на котором рассуждающая модель отдаёт пустой `content`.
        # Один заполненный `IntakeResult` (пять вопросов с обоснованиями плюс
        # полностью набитый `Brief` с ответами владельца) — 8 148 знаков,
        # ≈4 070 токенов одного только ответа: на рассуждение не остаётся
        # ничего. 8000 — ответ плюс сопоставимый запас на рассуждение.
        max_tokens=8000,
    )


async def refine_brief(
    router: LLMRouter, raw_text: str, answers: dict[str, str], *, industry_hint: str = "не указана"
) -> Brief:
    """Собрать финальный бриф с учётом ответов на уточняющие вопросы."""
    answers_text = "\n".join(f"— {q}\n  Ответ: {a or 'не знаю'}" for q, a in answers.items())
    enriched = f"{raw_text}\n\nОтветы на уточняющие вопросы:\n{answers_text}"
    result = await router.complete_json(
        "intake",
        [
            Message(
                role="system",
                content=render_prompt("intake", RAW_TASK_TEXT=enriched, INDUSTRY_HINT=industry_hint),
            ),
            Message(
                role="user",
                content=(
                    "Уточнения уже получены — вопросов больше не задавай: верни questions "
                    "пустым списком и полностью заполненный draft_brief. Только JSON."
                ),
            ),
        ],
        IntakeResult,
        # Тот же расчёт, что у `run_intake`: ответ здесь ещё длиннее — вопросов
        # нет, зато `draft_brief` заполнен целиком.
        max_tokens=8000,
    )
    return result.draft_brief


# ── 2. План поиска ──────────────────────────────────────────────────────────
async def plan_search(router: LLMRouter, brief: Brief) -> SearchPlan:
    """Превратить бриф в набор поисковых формулировок на двух языках."""
    return await router.complete_json(
        "planner",
        [
            Message(role="system", content=render_prompt("planner", BRIEF_JSON=brief.model_dump_json(indent=2))),
            Message(role="user", content=_GO),
        ],
        SearchPlan,
        # План бывает из двух десятков формулировок, у каждой обоснование —
        # стандартных 4096 токенов на это не хватает.
        max_tokens=8000,
    )


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


# ── 4. Скоринг ──────────────────────────────────────────────────────────────
def candidates_listing(chunk: list[Candidate], citations: dict[str, int] | None = None) -> str:
    """Перечисление кандидатов для сообщения скоринга.

    `citations` — цитируемость по ключу склейки кандидата, её добирает
    обогащение по DOI (РЕШ-50). Строка «Цитирований» появляется, только когда
    число действительно известно: «неизвестно» в промпте — это приглашение
    модели домыслить, а выдуманных значений во входе стадии быть не должно
    (инвариант №1).
    """
    known = citations or {}
    blocks: list[str] = []
    for i, c in enumerate(chunk):
        lines = [
            f"[{i}] {c.title}",
            f"URL: {c.url}",
            f"Организация: {c.org or 'не указана'}",
            f"Год: {c.year or 'не указан'}",
            f"Тип: {c.source_type.value}",
        ]
        cited = known.get(c.dedup_key())
        if cited is not None:
            lines.append(f"Цитирований: {cited}")
        lines.append(f"Фрагмент: {c.snippet[:600]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


#: Подстрока предупреждения роутера о повторе из-за нехватки лимита
#: (`complete_json`, `app/llm/router.py`). Стадия зовёт роутер и о его
#: внутреннем повторе иначе не узнаёт вовсе: наружу возвращается обычный
#: разобранный ответ, хотя вход оплачен второй раз целиком.
_LIMIT_RETRY_MARKER = "ответ обрезан по лимиту токенов"


class _TokenRetryCounter(logging.Handler):
    """Счётчик повторов роутера по нехватке лимита за время одной стадии.

    Слушает журнал роутера, а не подменяет его вызов: контракт `llm/` — общий,
    и правка ради измерителя одной стадии стоила бы дороже, чем даёт. Связь с
    формулировкой роутера закреплена тестом, который проходит настоящий путь
    повтора (`tests/test_stages.py`), — смена текста уронит его, а не спрячется
    вечным нулём.

    Записи чужих стадий не в счёт: имя стадии — первый аргумент записи. Стадии
    делят один роутер, и чужой повтор в своём счёте — это измеритель, который
    показывает не свою работу.
    """

    def __init__(self, stage: str) -> None:
        super().__init__(level=logging.WARNING)
        self._stage = stage
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        args = record.args if isinstance(record.args, tuple) else ()
        if args[:1] == (self._stage,) and _LIMIT_RETRY_MARKER in str(record.msg):
            self.count += 1


async def score_candidates(
    router: LLMRouter,
    brief: Brief,
    candidates: list[Candidate],
    *,
    batch_size: int = 10,
    citations: dict[str, int] | None = None,
) -> list[ScoredCandidate]:
    """Оценить кандидатов по пяти критериям.

    Батчами, потому что дешёвая модель на длинном списке начинает ставить всем
    одинаковые оценки и терять кандидатов.

    `citations` — цитируемость с обогащения по DOI; см. `candidates_listing`.
    """

    scored: list[ScoredCandidate] = []
    batches = 0
    started_at = time.monotonic()
    retries = _TokenRetryCounter("scorer")
    router_log = logging.getLogger(LLMRouter.__module__)
    router_log.addHandler(retries)
    try:
        for start in range(0, len(candidates), batch_size):
            batches += 1
            chunk = candidates[start : start + batch_size]
            listing = candidates_listing(chunk, citations)
            try:
                batch = await router.complete_json(
                    "scorer",
                    [
                        Message(role="system", content=load_prompt("scorer")),
                        Message(
                            role="user",
                            content=(
                                f"Задача бизнеса:\n{brief.task_summary}\n"
                                f"Отрасль: {brief.industry}\n"
                                f"Ограничения: {'; '.join(brief.constraints) or 'не заданы'}\n\n"
                                f"{owner_answers_block(brief)}"
                                f"Кандидаты ({len(chunk)} шт.):\n{listing}\n\n"
                                f"Верни ровно {len(chunk)} оценок, ссылаясь на кандидатов "
                                f"номерами от 0 до {len(chunk) - 1}."
                            ),
                        ),
                    ],
                    ScoreBatch,
                    # Т-13: на 4000 рассуждающая модель не доходила до ответа —
                    # лимит целиком уходил в reasoning_content, `content`
                    # приходил пустым, и КАЖДЫЙ батч скатывался в нейтральные
                    # оценки, то есть ранжирование умирало молча, оплаченное
                    # полностью. Расчёт. Худший ответ на батч из 10 кандидатов
                    # (пять оценок и `notes` до 160 знаков на каждого) — 3 320
                    # знаков, ≈1 660 токенов при консервативных двух знаках на
                    # токен. Остальное — запас на рассуждение: стадия взвешивает
                    # 10 кандидатов по пяти якорным шкалам, 50 суждений за
                    # вызов, самая тяжёлая по рассуждению стадия конвейера.
                    # Т-26: 8000 оказались впритык. На воротах Э2-А часть батчей
                    # бралась со ВТОРОЙ попытки — роутер видел обрыв JSON,
                    # удваивал лимит и переспрашивал заново, со всем входом.
                    # Повтор перестал быть страховкой и стал штатным путём, а
                    # прогон вырос до 25–30 минут; второй проход (РЕШ-81) удвоил
                    # бы и это. 16000 — тот самый удвоенный лимит, на котором
                    # ответ доходил, только запрошенный сразу.
                    # Щедрость здесь бесплатна: `max_tokens` — потолок, а не
                    # счёт, несгенерированные токены не оплачиваются. Нехватка,
                    # наоборот, стоит повторного запроса целиком со всем входом.
                    max_tokens=16000,
                )
            except CostLimitExceededError:
                # Стоп-кран (РЕШ-45): исчерпанный лимит — не отказ модели, а
                # команда «стоп тратить». Нейтральные оценки платили бы дальше на
                # следующих батчах, тихо продолжая расходовать при исчерпанном
                # лимите — это и есть скрытая деградация, которую стоп-кран
                # обязан пресекать.
                raise
            except Exception as exc:
                log.warning("скоринг батча не удался (%s), беру нейтральные оценки", exc)
                scored.extend(_neutral_scores(chunk))
                continue

            by_index = {item.index: item for item in batch.scores}
            for position, candidate in enumerate(chunk):
                score = by_index.get(position)
                if score is None:
                    # Модель потеряла кандидата — не выбрасываем его, а ставим
                    # нейтральную оценку: пусть решает отбор, а не пропуск.
                    log.info("скоринг пропустил кандидата %s, ставлю нейтральную оценку", candidate.title[:60])
                    scored.extend(_neutral_scores([candidate]))
                    continue
                scored.append(
                    ScoredCandidate(
                        candidate=candidate,
                        relevance=score.relevance,
                        maturity=score.maturity,
                        accessibility=score.accessibility,
                        freshness=score.freshness,
                        completeness=score.completeness,
                        notes=score.notes,
                    )
                )
    finally:
        router_log.removeHandler(retries)
        # Печатается ВСЕГДА: и при нуле повторов, и при пустом входе, и когда
        # стадию оборвал стоп-кран. Молчание сделало бы «повторов не стало»
        # неотличимым от «код до счётчика не дошёл» — тот же урок, ради
        # которого всегда печатается строка калибровки. Время стадии здесь же:
        # длительность прогона — то, ради чего лимит и поднимали.
        log.info(
            "скоринг: батчей %s, повторов по лимиту %s, время стадии %.1f с",
            batches,
            retries.count,
            time.monotonic() - started_at,
        )
    return scored


#: Текст пометки в `notes` для оценки, которую поставил код, а не модель. Виден
#: человеку в разборе; отсечка узнаёт таких кандидатов по типу
#: `NeutralScoredCandidate`, а по этой строке — только запасным путём.
NEUTRAL_SCORE_NOTE = "оценка не получена, значение нейтральное"

#: Пороги, для которых журнал калибровки печатает «сколько отсеклось бы».
#: Рабочее значение подбирается по этим числам на прогоне (Т-10).
_CALIBRATION_THRESHOLDS = (4, 5, 6, 7)


class NeutralScoredCandidate(ScoredCandidate):
    """Кандидат с оценками, которые проставил код после отказа скоринга.

    Пометка структурная, а не текстовая. Сравнение `notes` со строкой держалось
    на том, что производитель пометки и её потребитель никогда не разойдутся:
    допиши `_neutral_scores` к пометке номер батча — и отсечка перестала бы
    узнавать нейтральных кандидатов, выкосила бы их молча, а журнал показал бы
    «нейтральных оценок 0». Тип таким образом не ломается.

    Подкласс живёт здесь, а не в `schemas.py`: это деталь стадии скоринга, а не
    контракт между стадиями — снаружи он остаётся обычным `ScoredCandidate`.
    """


def _neutral_scores(candidates: list[Candidate]) -> list[NeutralScoredCandidate]:
    """Средние оценки для кандидатов, которых модель не осилила оценить."""
    return [
        NeutralScoredCandidate(
            candidate=candidate,
            relevance=5,
            maturity=5,
            accessibility=5,
            freshness=5,
            completeness=5,
            notes=NEUTRAL_SCORE_NOTE,
        )
        for candidate in candidates
    ]


def has_neutral_score(item: ScoredCandidate) -> bool:
    """Оценки проставлены кодом после отказа скоринга, а не получены от модели.

    Основной признак — тип. Сравнение с `NEUTRAL_SCORE_NOTE` оставлено запасным
    путём: `ScoredCandidate` собирают не только в `_neutral_scores`, и кандидат,
    восстановленный из обычной модели с той же пометкой (`model_validate`,
    фикстура теста), обязан остаться защищённым от отсечки.

    Имя публичное, потому что признак спрашивают снаружи модуля: `orchestrator`
    ставит по нему пометку нейтральной оценки в таблице `candidates` (РЕШ-84).
    До Т-21 он обращался к приватному `_has_neutral_score` — единственное во
    всём `backend/app` обращение к приватному имени чужого модуля, то есть
    контракт, о котором знали только двое.
    """
    return isinstance(item, NeutralScoredCandidate) or item.notes.strip() == NEUTRAL_SCORE_NOTE


def _has_neutral_score(item: ScoredCandidate) -> bool:
    """Прежнее приватное имя — тонкая обёртка над `has_neutral_score`.

    Оставлено ради вызовов внутри модуля, которых много; поведение целиком
    задаётся публичной функцией, второй копии знания здесь нет.
    """
    return has_neutral_score(item)


def _cut_by_relevance(scored: list[ScoredCandidate], threshold: int) -> int:
    """Сколько кандидатов отсечёт порог. Нейтральные оценки не в счёт: их
    отсечка не трогает ни при каком пороге."""
    return sum(1 for item in scored if not _has_neutral_score(item) and item.relevance < threshold)


def _model_relevance(scored: list[ScoredCandidate]) -> list[int]:
    """Оценки, которые действительно поставила модель, — без нейтральных."""
    return [item.relevance for item in scored if not _has_neutral_score(item)]


def log_relevance_calibration(
    scored: list[ScoredCandidate], picked: list[ScoredCandidate], *, relevance_min: int, limit: int
) -> None:
    """Напечатать распределение `relevance` и прикидку отсечки по порогам.

    Печатается **всегда**: и при пороге 0, и когда не отсечён ни один кандидат.
    Это единственный источник данных для подбора порога (Т-10) — оценки нигде
    не сохраняются, таблица `candidates` не пишется, в отчёты оценки не
    попадают. Молчание при нулевой отсечке сделало бы ноль неотличимым от
    «отсечка не сработала»: ровно этот неинтерпретируемый ноль стоил разбора
    на этапе Э1-2.

    Строка одна, но популяции в ней разделены — иначе по ней нельзя принять
    решение:

    * `min/медиана/max` считаются **только по оценкам модели**, как и колонка
      «отсеклось бы». Нейтральные пятёрки — не измерение, а след отказа батча:
      добавленный к десяти оценкам модели отказавший батч из десяти нейтральных
      сдвигал медиану с 2 на 4.5 — ровно внутри диапазона 4–7, из которого Т-10
      выбирает порог, и при неизменной колонке «отсеклось бы».
    * `отсеклось бы по всему списку` — цена порога для всего скоринга.
    * `среди отобранных ... ниже порога` — цена порога для тех, кто реально
      доходит до карточек: до них добирается только top-`limit`. Без этой
      колонки «отсечено 30 из 42» при нетронутом составе top-12 и «отсечено 3
      из 15» при замене четверти выдачи выглядят одинаково.
    """
    values = _model_relevance(scored)
    spread = f"{min(values)}/{statistics.median(values):g}/{max(values)}" if values else "нет оценок"
    would_cut = ", ".join(f"при {t}: {_cut_by_relevance(scored, t)}" for t in _CALIBRATION_THRESHOLDS)
    picked_below = ", ".join(f"{t}: {_cut_by_relevance(picked, t)}" for t in _CALIBRATION_THRESHOLDS)
    log.info(
        "калибровка relevance: кандидатов %s, оценок модели %s, min/медиана/max по оценкам модели %s, "
        "порог %s, отсечено %s; отсеклось бы по всему списку %s; среди отобранных %s (лимит %s) ниже порога %s; "
        "нейтральных оценок %s (не отсекаются никогда)",
        len(scored),
        len(values),
        spread,
        relevance_min,
        _cut_by_relevance(scored, relevance_min),
        would_cut,
        len(picked),
        limit,
        picked_below,
        sum(1 for item in scored if _has_neutral_score(item)),
    )


# ── 4а. Условный второй проход поиска (РЕШ-81) ──────────────────────────────
#: Дословные строки решения о втором проходе. Печатается ВСЕГДА ровно одна из
#: двух — в том числе при выключенном пороге. Молчание сделало бы «проход не
#: сработал» неотличимым от «код до проверки не дошёл»: тот же урок, ради
#: которого всегда печатается строка калибровки `log_relevance_calibration`.
REPLAN_ENTER_LOG = "второй проход: максимум relevance %s ниже порога %s — иду повторно"
REPLAN_SKIP_LOG = "второй проход не нужен: максимум relevance %s не ниже порога %s"

#: Что встаёт в строку вместо максимума, когда оценок модели нет вовсе (все
#: батчи скоринга отказали). Слово вместо числа — чтобы разбор прогона не читал
#: отсутствие измерения как измеренный ноль.
NO_MODEL_SCORES_LABEL = "нет оценок модели"


def max_model_relevance(scored: list[ScoredCandidate]) -> int | None:
    """Максимум `relevance` по оценкам МОДЕЛИ; `None`, если их нет ни одной.

    Нейтральные оценки в максимум не входят (`_model_relevance`): они равны
    ровно пятёрке, и отказавший батч на слабой выдаче нарисовал бы максимум 5
    там, где модель не поставила выше четырёх, — то есть погасил бы второй
    проход ровно на той задаче, ради которой он затеян (агропром, максимум 4).
    """
    values = _model_relevance(scored)
    return max(values) if values else None


def needs_second_pass(scored: list[ScoredCandidate], *, threshold: int) -> bool:
    """Нужен ли второй проход поиска, и запись об этом в журнал (РЕШ-81).

    Условие входа — максимум `relevance` по оценкам модели **строго ниже**
    порога. Строго, а не «не выше»: порог здесь — граница годной выдачи, и
    выдача, дотянувшая до неё, годной уже считается. При пороге 0 (умолчание,
    проход выключен) условие не выполняется ни при каком максимуме.

    Оценок модели нет вовсе — проход не запускается: платить за второй заход
    можно по измеренному промаху, а не по отсутствию измерения.
    """
    top = max_model_relevance(scored)
    if top is not None and top < threshold:
        log.info(REPLAN_ENTER_LOG, top, threshold)
        return True
    log.info(REPLAN_SKIP_LOG, NO_MODEL_SCORES_LABEL if top is None else top, threshold)
    return False


def plan_queries_listing(plan: SearchPlan) -> str:
    """Формулировки плана строками — вход стадии `replan`.

    Модель обязана видеть, что уже спрашивали: без этого «уточнённый» план
    получается пересказом прежнего, и второй проход опрашивает источники теми
    же словами за те же деньги.
    """
    lines = [f"— [{query.source_type.value}/{query.lang}] {query.text}" for query in plan.queries]
    return "\n".join(lines) or "формулировок не было"


#: Сколько кандидатов первой выдачи показывать стадии `replan`. Прогон приносит
#: до сотни, и весь список раздул бы вход ради хвоста с одинаково низкими
#: оценками: для вывода «искали не там» хватает верхушки.
_REPLAN_LISTING_LIMIT = 40


def first_pass_listing(scored: list[ScoredCandidate], *, limit: int = _REPLAN_LISTING_LIMIT) -> str:
    """Первая выдача для стадии `replan`: оценка, тип источника, заголовок, организация.

    Ни ссылок, ни фрагментов: стадия не сверяет утверждения, а называет, чего не
    нашли, и лишний текст тут только съедает лимит. Кандидаты идут по убыванию
    `relevance` — промах виднее всего по верхушке списка.

    Нейтральные оценки помечены словами, а не пятёркой (`has_neutral_score`):
    показать модели поставленное кодом число как её собственное суждение значит
    соврать ей о качестве выдачи.
    """
    ranked = sorted(scored, key=lambda item: item.relevance, reverse=True)[:limit]
    lines = []
    for position, item in enumerate(ranked):
        mark = "оценка не получена" if has_neutral_score(item) else f"relevance {item.relevance}"
        org = item.candidate.org or "организация не указана"
        lines.append(f"[{position}] {mark} | {item.candidate.source_type.value} | {item.candidate.title[:160]} | {org}")
    return "\n".join(lines) or "первая выдача пуста"


async def replan_search(router: LLMRouter, brief: Brief, plan: SearchPlan, scored: list[ScoredCandidate]) -> SearchPlan:
    """Новый план поиска по итогам слабой первой выдачи (РЕШ-81).

    Модель получает бриф, прежние формулировки и верхушку первой выдачи с
    оценками, а возвращает ТОЛЬКО новые формулировки той же схемы `SearchPlan`:
    переписывать вход в ответ её не просят (грабля §0.1 п. 2 SPEC) — на
    кандидатов она ссылается номерами списка.
    """
    return await router.complete_json(
        "replan",
        [
            Message(
                role="system",
                content=render_prompt(
                    "replanner",
                    BRIEF_JSON=brief.model_dump_json(indent=2),
                    PREVIOUS_QUERIES=plan_queries_listing(plan),
                    FIRST_PASS_RESULTS=first_pass_listing(scored),
                ),
            ),
            Message(role="user", content=_GO),
        ],
        SearchPlan,
        # Тот же расчёт, что у `plan_search`: ответ — план до двенадцати
        # формулировок с обоснованием каждой, и стадии предстоит рассуждение о
        # промахе первой выдачи вдобавок к нему.
        max_tokens=8000,
    )


def select_top(scored: list[ScoredCandidate], *, limit: int, relevance_min: int | None = None) -> list[ScoredCandidate]:
    """Отобрать лучших, но не давать одному источнику занять всю выдачу.

    Перед отбором работает отсечка по предметному соответствию (РЕШ-48):
    кандидат с `relevance` ниже порога до карточек не доходит. База сравнения
    показала, почему это нужно: до половины карточек уходили «мимо задачи»,
    потому что взвешенная сумма вытягивала кандидата с низкой релевантностью за
    счёт свежести и доступности.

    Порог берётся из настроек (`RELEVANCE_MIN`), по умолчанию 0 — отсечка
    выключена, пока значение не подобрано на прогоне. Явный `relevance_min`
    нужен тестам и разбору.

    Кандидатов с нейтральной оценкой отсечка не трогает никогда: их оценки
    проставил код после отказа батча скоринга, и один такой отказ иначе тихо
    выкосил бы десяток кандидатов, а причина была бы неотличима от «все
    нерелевантны».
    """
    if relevance_min is None:
        relevance_min = get_settings().relevance_min
    eligible = [item for item in scored if _has_neutral_score(item) or item.relevance >= relevance_min]
    picked = _pick_top(eligible, limit=limit)
    # Строка считается по готовому отбору — иначе её вторая половина (сколько
    # отсеклось бы среди тех, кто доходит до карточек) неизвестна, — но уходит в
    # журнал до того, как отбор увидит следующая стадия.
    log_relevance_calibration(scored, picked, relevance_min=relevance_min, limit=limit)
    return picked


def _pick_top(eligible: list[ScoredCandidate], *, limit: int) -> list[ScoredCandidate]:
    """Отбор по взвешенной сумме с потолком на один источник.

    Вынесен отдельно, чтобы журнал калибровки считал колонку по тому же составу,
    который стадия вернёт на самом деле: две копии этой логики разошлись бы, и
    Т-10 подбирал бы порог по несуществующей выдаче.
    """
    ordered = sorted(eligible, key=lambda s: s.total, reverse=True)
    per_provider: dict[str, int] = defaultdict(int)
    picked: list[ScoredCandidate] = []
    cap = max(2, limit // 2)
    for item in ordered:
        provider = item.candidate.provider
        if per_provider[provider] >= cap:
            continue
        picked.append(item)
        per_provider[provider] += 1
        if len(picked) >= limit:
            break
    return picked


def group_for_cards(selected: list[ScoredCandidate], *, target: int) -> list[list[ScoredCandidate]]:
    """Сгруппировать кандидатов в заготовки карточек.

    Пока группировка простая: один кандидат — одна карточка, но с запасом на
    склейку по организации (несколько публикаций одной лаборатории — один
    вариант решения, а не три).
    """
    by_org: dict[str, list[ScoredCandidate]] = defaultdict(list)
    singles: list[list[ScoredCandidate]] = []
    for item in selected:
        org = (item.candidate.org or "").strip().lower()
        if org:
            by_org[org].append(item)
        else:
            singles.append([item])
    groups = [group for group in by_org.values()] + singles
    groups.sort(key=lambda g: max(i.total for i in g), reverse=True)
    return groups[:target]


# ── 5. Карточки и проверка ──────────────────────────────────────────────────
async def author_card(router: LLMRouter, brief: Brief, group: list[ScoredCandidate]) -> SolutionCard:
    """Собрать карточку из группы кандидатов."""
    card = await router.complete_json(
        "card_author",
        [
            # Всё содержательное — в системном сообщении: так DeepSeek кэширует
            # префикс и вход дешевеет в десятки раз.
            Message(
                role="system",
                content=render_prompt(
                    "card_author",
                    BRIEF_JSON=brief.model_dump_json(indent=2),
                    GROUP_CANDIDATES_JSON=candidates_to_json(group),
                    GOV_SUPPORT_JSON=load_gov_support(),
                ),
            ),
            Message(role="user", content=_GO),
        ],
        SolutionCard,
        # Запас на рассуждающую модель: она сначала тратит токены на
        # reasoning_content и лишь потом пишет сам JSON карточки.
        max_tokens=7000,
    )
    # Ссылки на исходных кандидатов заполняем кодом, а не моделью: она их
    # переписывает с опечатками, а проверяющая стадия потом справедливо
    # жалуется, что кандидаты не переданы.
    card.candidate_urls = [item.candidate.url for item in group]
    # Стадию зрелости конкурсной карточки правит код, а не модель: описание
    # хакатонного проекта пишет сама команда, и оно всегда звучит как готовый
    # продукт (см. cap_contest_maturity).
    return cap_contest_maturity(card, group)


#: Порядок стадий зрелости по возрастанию. `unknown` в него не входит: это не
#: стадия, а честное «ни один признак источниками не подтверждён», и понижать
#: его потолком некуда.
_MATURITY_ORDER: tuple[MaturityStage, ...] = (
    MaturityStage.LAB,
    MaturityStage.PROTOTYPE,
    MaturityStage.PILOT,
    MaturityStage.SERIES,
)

#: Площадки конкурсов с многолетним циклом (§2.2 разведки `contests-recon.md`):
#: только у XPRIZE и Up Great решения проходят натурные испытания по регламенту
#: организатора с независимым замером — поэтому их потолок `pilot`, а не
#: `prototype`. Опознаём по УЗЛУ адреса кандидата: домен нельзя случайно
#: получить из чужой аннотации, в отличие от слова в тексте.
LONG_CYCLE_CONTEST_DOMAINS: tuple[str, ...] = (
    "xprize.org",
    "upgreat.one",
)

#: Имена тех же конкурсов — на случай, когда решение лежит не на сайте
#: организатора (новость вуза, страница команды). Ищутся ТОЛЬКО в названии и
#: организации кандидата, но не в `snippet`: сниппет — произвольный текст чужой
#: поисковой выдачи.
#:
#: Здесь не осталось ни «многолетн», ни «национальная технологическая
#: инициатива», и обе метки убраны по одной причине — они мерили не то. Первая
#: поднимала потолок обычному хакатону на любом «многолетнем опыте команды» в
#: аннотации, то есть давала ровно то завышение, ради запрета которого потолок и
#: писался (РЕШ-79 п. 2: хакатон — не выше прототипа). Вторая в живом тексте
#: почти не срабатывала: «при поддержке Национальной технологической
#: инициативы» стоит в родительном падеже и с подстрокой не совпадает.
LONG_CYCLE_CONTEST_NAMES: tuple[str, ...] = (
    "xprize",
    "upgreat",
    "up great",
    "up-great",
    "ап грейт",
    "апгрейт",
)


def contest_maturity_ceiling(group: Sequence[ScoredCandidate]) -> MaturityStage | None:
    """Потолок зрелости, заданный конкурсными кандидатами группы.

    `None` — конкурсных кандидатов в группе нет, и потолка тоже нет: обычная
    карточка по публикациям и патентам этим правилом не трогается.
    """
    contests = [item.candidate for item in group if item.candidate.source_type is SourceType.CONTEST]
    if not contests:
        return None
    if any(_is_long_cycle_contest(candidate) for candidate in contests):
        return MaturityStage.PILOT
    return MaturityStage.PROTOTYPE


def _is_long_cycle_contest(candidate: Candidate) -> bool:
    """Конкурс ли это с многолетним циклом — по узлу адреса и по имени конкурса.

    Свободный текст выдачи (`snippet`) не смотрится вовсе: там «многолетний
    опыт команды» встречается сплошь и рядом, и слово в аннотации поднимало бы
    потолок обычному хакатону. Домен сверяется по узлу целиком, а не подстрокой:
    иначе `xprize.org.example.com` выдавал бы себя за площадку XPRIZE, и признак
    «домен» оказался бы не надёжнее слова в тексте.
    """
    host = (candidate.url.host or "").lower()
    if any(host == domain or host.endswith(f".{domain}") for domain in LONG_CYCLE_CONTEST_DOMAINS):
        return True
    named = f"{candidate.title} {candidate.org or ''}".lower()
    return any(name in named for name in LONG_CYCLE_CONTEST_NAMES)


def cap_contest_maturity(card: SolutionCard, group: Sequence[ScoredCandidate]) -> SolutionCard:
    """Понизить стадию зрелости карточки до потолка, заданного типом конкурса.

    Правило владельца (РЕШ-79 п. 2) и вывод разведки §2.2: `maturity_stage`
    решения конкурсного класса **никогда не выставляется моделью по тексту
    описания**. Описание пишет сама команда, и оно звучит как готовый продукт
    («система предиктивной диагностики для промышленных предприятий»), — модель,
    читая такой текст, ставит `pilot` за 48 часов работы. Поэтому стадию правит
    код по типу источника, как и `pdf_urls`: хакатон — не выше `prototype`,
    конкурс с многолетним циклом — не выше `pilot`.

    Понижение одностороннее: стадию ниже потолка функция не поднимает, а
    `unknown` не трогает вовсе — это не завышение, а отсутствие подтверждения.
    """
    ceiling = contest_maturity_ceiling(group)
    if ceiling is None or card.maturity_stage not in _MATURITY_ORDER:
        return card
    if _MATURITY_ORDER.index(card.maturity_stage) > _MATURITY_ORDER.index(ceiling):
        log.info(
            "конкурсная карточка «%s»: стадия %s понижена до потолка %s (РЕШ-79 п. 2)",
            card.title,
            card.maturity_stage.value,
            ceiling.value,
        )
        card.maturity_stage = ceiling
    return card


#: Формат записи о неподтверждённой зрелости из правила 7 prompts/verifier.md:
#: «стадия зрелости не подтверждена: <стадия>». По этой записи (а не по
#: пересказу) код понижает maturity_stage карточки — сама проверяющая стадия
#: карточку не переписывает (РЕШ-42).
_MATURITY_UNCONFIRMED_MARKER = "стадия зрелости не подтверждена"


async def verify_card(
    router: LLMRouter, card: SolutionCard, group: list[ScoredCandidate] | None = None
) -> SolutionCard:
    """Проверить карточку на утверждения без источника и уточнить уверенность.

    Кандидаты передавать обязательно: без текстов источников проверяющей стадии
    нечем сверять утверждения, и она справедливо занижает уверенность всем
    карточкам подряд.
    """
    verdict = await router.complete_json(
        "verifier",
        [
            Message(
                role="system",
                content=render_prompt(
                    "verifier",
                    CARD_JSON=card.model_dump_json(indent=2),
                    GROUP_CANDIDATES_JSON=candidates_to_json(group or []),
                ),
            ),
            Message(role="user", content=_GO),
        ],
        VerifyResult,
        # Проверка разбирает карточку по утверждениям и сверяет их с источниками:
        # рассуждающей модели на это нужен запас, иначе content приходит пустым.
        max_tokens=6000,
    )
    card.confidence = verdict.confidence
    card.confidence_note = verdict.confidence_note
    if verdict.unsourced_claims:
        # РЕШ-42: находки проверки — это SolutionCard.unsourced_claims, а не
        # только строка в risks. Без этого контракт промпта Ф-2 остаётся
        # мёртвым: поле заполняет модель, но код его не читает.
        card.unsourced_claims.extend(verdict.unsourced_claims)
        card.confidence = Confidence.LOW
        if any(_MATURITY_UNCONFIRMED_MARKER in claim.lower() for claim in verdict.unsourced_claims):
            # Неподтверждённая зрелость понижается до unknown, а не остаётся
            # заявленной стадией (правило 7 prompts/verifier.md).
            card.maturity_stage = MaturityStage.UNKNOWN
    if card.has_unsourced_claims():
        card.confidence = Confidence.LOW
        card.confidence_note = (card.confidence_note + " Нет ни одного подтверждения ссылкой.").strip()
    return card


# ── 5а. Отбор карточек к публикации ─────────────────────────────────────────
#: Верхняя граница выдачи (§2а SPEC, FR-9: 5–7 карточек). Нижней границы нет:
#: сколько честных карточек получилось, столько и идёт в отчёт (Ф-7).
MAX_PUBLISHED_CARDS = 7

_EMPTY_CARD_TEXT = "не найдено"


def select_publishable_cards(cards: list[SolutionCard], *, limit: int = MAX_PUBLISHED_CARDS) -> list[SolutionCard]:
    """Отобрать карточки, которые честно можно показать клиенту.

    Карточка без единого подтверждения (`evidence` пуст) до отчёта не доходит
    — тот же инвариант №1, что и в экспортёре, только на входе, а не на
    печати. Карточка-заглушка «не найдено» (формат ошибки 1 из
    `prompts/card_author.md` — группа без источников) тоже не идёт: пустая
    карточка дороже честного «карточек получилось меньше, чем просили».
    Порядок сохраняется, число карточек в отчёте искусственно не добивается.
    """
    kept = [card for card in cards if card.evidence and not _is_empty_card(card)]
    return kept[:limit]


def _is_empty_card(card: SolutionCard) -> bool:
    """Каркас-заглушка: и название, и суть — «не найдено»."""
    return card.title.strip().lower() == _EMPTY_CARD_TEXT and card.essence.strip().lower() == _EMPTY_CARD_TEXT


# ── 6. Сводка ───────────────────────────────────────────────────────────────
async def summarize(router: LLMRouter, brief: Brief, cards: list[SolutionCard]) -> ReportSummary:
    """Сравнительная сводка и рекомендация по вариантам."""
    # Отдаём модели только то, что нужно для сравнения вариантов: полные
    # карточки с доказательствами раздули бы вход втрое без пользы для сводки.
    cards_json = json.dumps(
        [
            {
                "n": index,
                "title": card.title,
                "maturity_stage": card.maturity_stage.value,
                "essence": card.essence,
                "applicability": card.applicability,
                "risks": card.risks,
                "next_step": card.next_step,
                "confidence": card.confidence.value,
                "gov_support": [item.instrument for item in card.gov_support],
            }
            for index, card in enumerate(cards, start=1)
        ],
        ensure_ascii=False,
        indent=2,
    )
    return await router.complete_json(
        "summary",
        [
            Message(
                role="system",
                content=render_prompt(
                    "summary",
                    BRIEF_JSON=brief.model_dump_json(indent=2),
                    CARDS_JSON=cards_json,
                ),
            ),
            Message(role="user", content=_GO),
        ],
        ReportSummary,
        max_tokens=6000,
    )


def fallback_summary(cards: list[SolutionCard]) -> ReportSummary:
    """Запасная сводка, когда стадия summary отказала (Ф-7).

    Не вызывает модель — сравнительная таблица собирается прямо из уже
    провалидированных полей карточек, поэтому сама отказать не может. Отказ
    настоящей сводки не должен уничтожать уже собранные карточки: раньше
    именно это и происходило (прогон агропрома потерял пять готовых карточек
    из-за LLMError на summary). Факт отказа — честная оговорка в caveats, а
    не только запись в логе.
    """
    rows = [
        {
            "Вариант": card.title,
            "Стадия зрелости": card.maturity_stage.value,
            "Уверенность": card.confidence.value,
        }
        for card in cards
    ]
    if cards:
        recommendation = (
            "Автоматическая сводка не собралась. Сравните варианты по карточкам "
            "ниже самостоятельно: у каждой есть подтверждения, риски и следующий шаг."
        )
    else:
        recommendation = (
            "Автоматическая сводка не собралась, а карточек для сравнения тоже нет — "
            "результат поиска стоит проверить вручную."
        )
    return ReportSummary(
        comparison_rows=rows,
        recommendation=recommendation,
        caveats=["Автоматическая сводка не собралась: раздел «Рекомендация» построен без участия модели."],
    )
