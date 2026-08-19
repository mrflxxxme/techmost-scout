"""Дешёвая проба перед платным прогоном (РЕШ-119, задача З-4).

ЗАЧЕМ
    Полный прогон пайплайна стоит 8–11 ₽ и полчаса. Проверить, доехали ли
    ограничения задачи до брифа (РЕШ-115, РЕШ-116), или что даёт новая
    конкурсная формулировка (РЕШ-117), полным прогоном — дорого и медленно.
    Этот скрипт зовёт ровно одну стадию за раз:

      --intake "текст задачи"           — интейк, печатает бриф и constraints
      --query "формулировка" --type ... — одну формулировку в Яндекс-поиск

    Проба интейка стоит 0,1–0,3 ₽, проба формулировки — 0,05–0,2 ₽: примерно в
    тридцать раз дешевле полного прогона.

ГРАНИЦЫ (РЕШ-119)
    Скрипт живёт ВНЕ поставки продукта: из backend/app он не импортируется, и
    команды `scout probe` в CLI нет и не будет — расширение клиентского
    интерфейса MVP этой задачей не покупается. Запускает скрипт только
    оркестратор: оба режима делают платный вызов (инвариант №7 для агентов,
    §9 CONVENTIONS.md для денег).

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
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def build_parser() -> argparse.ArgumentParser:
    """Собрать разбор аргументов — используется и `main`, и тестом на `--help`."""
    parser = argparse.ArgumentParser(
        prog="probe.py",
        description=(
            "Дешёвая проба перед платным прогоном (РЕШ-119): --intake проверяет, "
            "доехали ли ограничения задачи до брифа; --query проверяет одну "
            "поисковую формулировку Яндекса, включая сужение site:. Скрипт вне "
            "поставки продукта — команды scout probe не существует."
        ),
    )
    parser.add_argument("--intake", metavar="ТЕКСТ", help="Проба интейка: текст постановки задачи одной строкой")
    parser.add_argument("--query", metavar="ФОРМУЛИРОВКА", help="Проба одной поисковой формулировки Яндекса")
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
        help="Язык формулировки для --query — определяет контур выдачи (SEARCH_TYPE_RU/COM)",
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.intake and args.query:
        parser.error("укажите либо --intake, либо --query, не оба сразу")
    if not args.intake and not args.query:
        parser.print_help()
        return 1

    if args.intake:
        return asyncio.run(probe_intake(args.intake))
    return asyncio.run(probe_query(args.query, args.type, args.lang))


if __name__ == "__main__":
    sys.exit(main())
