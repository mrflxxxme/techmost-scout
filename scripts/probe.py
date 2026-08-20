"""Дешёвая проба перед платным прогоном (РЕШ-119, задачи З-4 и Э2-Г/З-1).

ЗАЧЕМ
    Полный прогон пайплайна стоит 8–11 ₽ и полчаса. Проверить, доехали ли
    ограничения задачи до брифа (РЕШ-115, РЕШ-116), что даёт новая конкурсная
    формулировка (РЕШ-117), или чем вообще располагает конкурсная находка и за
    что модель снижает ей оценку (РЕШ-133) — полным прогоном дорого и медленно.
    Этот скрипт зовёт ровно один срез конвейера за раз:

      --intake "текст задачи"           — интейк, печатает бриф и constraints
      --query "формулировка" --type ... — одну формулировку в Яндекс-поиск
      --contest "формулировка"          — та же формулировка ПУТЁМ ПРОДУКТА
                                           целиком: адаптер Яндекса с
                                           source_type=contest, нормализация
                                           стадией extractor, оценка стадией
                                           score_candidates — с печатью всех
                                           пяти оценок, notes и сводки по
                                           каждому кандидату (Э2-Г, З-1)

    Проба интейка стоит 0,1–0,3 ₽, проба формулировки — 0,05–0,2 ₽, проба
    конкурсного класса — сравнимо с двумя LLM-вызовами (extractor + scorer):
    примерно в тридцать раз дешевле полного прогона.

ГРАНИЦЫ (РЕШ-119)
    Скрипт живёт ВНЕ поставки продукта: из backend/app он не импортируется, и
    команды `scout probe` в CLI нет и не будет — расширение клиентского
    интерфейса MVP этой задачей не покупается. Запускает скрипт только
    оркестратор: все три режима делают платный вызов (инвариант №7 для
    агентов, §9 CONVENTIONS.md для денег).

    Ключи берутся из окружения тем же способом, что и в продукте
    (`app.config.get_settings`), и НИГДЕ не печатаются — ни в обычном, ни в
    отладочном выводе (инвариант №3).

    Внешние вызовы идут исключительно через `app.llm.LLMRouter` и
    `app.search.build_adapters` — тот же путь, которым идёт продукт
    (инвариант №6). Скрипт не открывает собственный HTTP-канал к провайдеру
    модели или к Яндексу в обход этих слоёв.

ЗАПУСК
    .venv\\Scripts\\python.exe scripts\\probe.py --help
    .venv\\Scripts\\python.exe scripts\\probe.py --intake "текст постановки задачи"
    .venv\\Scripts\\python.exe scripts\\probe.py --query "инновационный конкурс сенсоры износа" --type contest --lang ru
    .venv\\Scripts\\python.exe scripts\\probe.py --contest "победители хакатона сенсоры износа"
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys


def build_parser() -> argparse.ArgumentParser:
    """Собрать разбор аргументов — используется и `main`, и тестом на `--help`."""
    parser = argparse.ArgumentParser(
        prog="probe.py",
        description=(
            "Дешёвая проба перед платным прогоном (РЕШ-119): --intake проверяет, "
            "доехали ли ограничения задачи до брифа; --query проверяет одну "
            "поисковую формулировку Яндекса, включая сужение site:; --contest "
            "проверяет ту же формулировку путём продукта целиком — источник, "
            "extractor, скоринг — и печатает все пять оценок и notes на "
            "каждого кандидата (РЕШ-133). Скрипт вне поставки продукта — "
            "команды scout probe не существует."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--intake", metavar="ТЕКСТ", help="Проба интейка: текст постановки задачи одной строкой")
    group.add_argument("--query", metavar="ФОРМУЛИРОВКА", help="Проба одной поисковой формулировки Яндекса")
    group.add_argument(
        "--contest",
        metavar="ФОРМУЛИРОВКА",
        help=(
            "Проба конкурсного класса: та же формулировка путём продукта "
            "целиком — адаптер Яндекса (source_type=contest), extractor, "
            "score_candidates — с печатью пяти оценок, notes и сводки "
            "по каждому кандидату (РЕШ-133)"
        ),
    )
    parser.add_argument(
        "--type",
        choices=("contest", "web"),
        default="web",
        help="Тип формулировки для --query (по умолчанию web)",
    )
    parser.add_argument(
        "--lang",
        choices=("ru", "en"),
        default="ru",
        help="Язык формулировки для --query и --contest — определяет контур выдачи (SEARCH_TYPE_RU/COM)",
    )
    return parser


async def probe_intake(text: str) -> int:
    """Проба интейка: собрать бриф тем же путём, что и продукт, без полного прогона.

    Печатает `industry`, `task_summary`, `constraints` отдельной строкой и цену
    вызова — именно `constraints` предмет пробы (РЕШ-115, РЕШ-116).
    """
    # Импорты — внутри функции: модуль пробы не создаёт роутер и не трогает
    # сеть на импорте (критерий 2 задачи З-4).
    from app.config import get_settings
    from app.llm import LLMRouter
    from app.pipeline.stages import run_intake

    settings = get_settings()
    router = LLMRouter(settings)
    result = await run_intake(router, text)
    brief = result.draft_brief

    print(f"industry: {brief.industry}")
    print(f"task_summary: {brief.task_summary}")
    print("constraints:")
    if brief.constraints:
        for item in brief.constraints:
            print(f"  - {item}")
    else:
        print("  (пусто)")
    print(f"стоимость вызова: {router.cost.spent_rub:.4f} ₽")
    return 0


async def probe_query(text: str, source_type: str, lang: str) -> int:
    """Проба формулировки: тот же адаптер и та же функция сужения, что у продукта.

    Печатает итоговую строку запроса ПОСЛЕ сужения `_with_site_filter` —
    именно она отвечает на вопрос, ради которого проба заводится: сузился
    запрос или нет. Строка печатается раньше самого вызова, поэтому видна
    даже если источник ответит пусто.
    """
    import httpx

    from app.config import get_settings
    from app.schemas import SearchQuery, SourceType
    from app.search import build_adapters
    from app.search.yandex_search import YandexSearchAdapter, _with_site_filter

    settings = get_settings()
    query = SearchQuery(
        text=text,
        source_type=SourceType(source_type),
        lang=lang,
        rationale="проба scripts/probe.py",
    )

    async with httpx.AsyncClient(
        timeout=settings.search_timeout_seconds,
        headers={"User-Agent": f"TechMostScout/1.0 (mailto:{settings.crossref_mailto})"},
        follow_redirects=True,
    ) as client:
        adapters = build_adapters(client, settings)
        adapter = next((a for a in adapters if isinstance(a, YandexSearchAdapter)), None)
        if adapter is None:
            print("yandex_search недоступен: нет ключа YANDEX_SEARCH_API_KEY или каталога YANDEX_CATALOG_ID")
            return 1

        search_type = YandexSearchAdapter._search_type(query)
        narrowed = _with_site_filter(query, search_type)
        print(f"итоговый запрос после сужения: {narrowed}")

        found = await adapter.search(query, limit=10)

    print(f"кандидатов: {len(found)}")
    for candidate in found:
        print(f"  - {candidate.title} — {candidate.url}")
    print(f"платных запросов израсходовано: {adapter.queries_used}")
    return 0


def _print_contest_summary(candidates: list, scored: list | None) -> None:
    """Сводка пробы конкурсного класса — печатается ВСЕГДА (критерий 4 З-1).

    Пустой список кандидатов и отказавший скоринг — два разных пробела, и оба
    обязаны остаться видимыми: явной пометкой отсутствия числа, а не тихим
    нулём, который снаружи неотличим от честной оценки «ноль».

    `candidates` — сырые кандидаты (для org/snippet/CONTEST_SITES, они не
    зависят от того, удался ли скоринг); `scored` — оценённые кандидаты, либо
    `None`, если стадия скоринга отказала целиком (см. `probe_contest`).
    """
    from app.search.yandex_search import CONTEST_SITES

    print("сводка:")
    print(f"  кандидатов: {len(candidates)}")

    if not candidates:
        print("  relevance min/медиана/max: нет данных (кандидатов нет)")
        print("  relevance >= 4 / >= 3 / >= 2: нет данных (кандидатов нет)")
    elif scored is None:
        print("  relevance min/медиана/max: нет данных (скоринг отказал)")
        print("  relevance >= 4 / >= 3 / >= 2: нет данных (скоринг отказал)")
    else:
        relevances = [item.relevance for item in scored]
        print(f"  relevance min/медиана/max: {min(relevances)}/{statistics.median(relevances)}/{max(relevances)}")
        print(
            "  relevance >= 4 / >= 3 / >= 2: "
            f"{sum(1 for r in relevances if r >= 4)} / "
            f"{sum(1 for r in relevances if r >= 3)} / "
            f"{sum(1 for r in relevances if r >= 2)}"
        )

    with_org = sum(1 for c in candidates if c.org)
    with_snippet = sum(1 for c in candidates if c.snippet)
    with_contest_site = sum(1 for c in candidates if any(site in (c.url.host or "") for site in CONTEST_SITES))
    print(f"  с непустым org: {with_org} из {len(candidates)}")
    print(f"  с непустым snippet: {with_snippet} из {len(candidates)}")
    print(f"  адресов с узлом из CONTEST_SITES: {with_contest_site} из {len(candidates)}")


async def probe_contest(text: str, lang: str = "ru") -> int:
    """Проба конкурсного класса: путь продукта целиком, без единой своей ветки.

    Адаптер Яндекса с `source_type=contest` → нормализация выдачи стадией
    `extract_candidates` → оценка стадией `score_candidates` — тот же роутер и
    тот же промпт `scorer`, что использует прогон. Отвечает на вопрос РЕШ-133:
    модель снижает оценку по мотиву «нет методики и испытаний» или «тема
    мимо» — это видно только из `notes` на каждого кандидата, поэтому они
    печатаются дословно и целиком, а не сокращаются.

    Бриф для скоринга собирается напрямую из формулировки без отдельного
    вызова интейка: сама проба — это проверка `--contest`, а не проверка
    интейка (для неё есть отдельный режим `--intake`), и лишний платный вызов
    не отвечал бы на вопрос, ради которого проба заводится.
    """
    import httpx

    from app.config import get_settings
    from app.llm import CostLimitExceededError, LLMRouter
    from app.pipeline.stages import extract_candidates, score_candidates
    from app.schemas import Brief, SearchQuery, SourceType
    from app.search import build_adapters
    from app.search.yandex_search import YandexSearchAdapter

    settings = get_settings()
    router = LLMRouter(settings)
    query = SearchQuery(
        text=text,
        source_type=SourceType.CONTEST,
        lang=lang,
        rationale="проба scripts/probe.py --contest",
    )

    async with httpx.AsyncClient(
        timeout=settings.search_timeout_seconds,
        headers={"User-Agent": f"TechMostScout/1.0 (mailto:{settings.crossref_mailto})"},
        follow_redirects=True,
    ) as client:
        adapters = build_adapters(client, settings)
        adapter = next((a for a in adapters if isinstance(a, YandexSearchAdapter)), None)
        if adapter is None:
            print("yandex_search недоступен: нет ключа YANDEX_SEARCH_API_KEY или каталога YANDEX_CATALOG_ID")
            _print_contest_summary([], None)
            print(f"стоимость вызовов: {router.cost.spent_rub:.4f} ₽")
            return 1

        found, raw = await adapter.search_raw(query, limit=20)

    if raw:
        # Сырая выдача уже оплачена этим же вызовом; extractor нормализует её
        # моделью (Э2-5) — второй раз к Яндексу проба не ходит (Р-8 плана).
        found = await extract_candidates(SourceType.CONTEST, adapter.name, raw, router)

    print(f"кандидатов после нормализации: {len(found)}")

    if not found:
        _print_contest_summary([], None)
        print(f"стоимость вызовов: {router.cost.spent_rub:.4f} ₽")
        return 0

    brief = Brief(industry="проба scripts/probe.py --contest", task_summary=text)
    try:
        scored = await score_candidates(router, brief, found)
    except CostLimitExceededError as exc:
        print(f"скоринг остановлен стоп-краном по расходу: {exc}")
        _print_contest_summary(found, None)
        print(f"стоимость вызовов: {router.cost.spent_rub:.4f} ₽")
        return 0

    for item in scored:
        print(f"- {item.candidate.title}")
        print(f"  узел адреса: {item.candidate.url.host}")
        print(f"  org: {item.candidate.org or 'нет'}")
        print(
            f"  оценки: relevance={item.relevance} maturity={item.maturity} "
            f"accessibility={item.accessibility} freshness={item.freshness} completeness={item.completeness}"
        )
        print(f"  notes: {item.notes}")

    _print_contest_summary(found, scored)
    print(f"стоимость вызовов: {router.cost.spent_rub:.4f} ₽")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.intake and not args.query and not args.contest:
        parser.print_help()
        return 1

    if args.intake:
        return asyncio.run(probe_intake(args.intake))
    if args.query:
        return asyncio.run(probe_query(args.query, args.type, args.lang))
    return asyncio.run(probe_contest(args.contest, args.lang))


if __name__ == "__main__":
    sys.exit(main())
