"""Диагностика: контрольный пересчёт сохранённого пула кандидатов (РЕШ-161, З-1 Э2-Е).

ЗАЧЕМ
    Бесплатный разбор Э2-Е уже показал: на общих кандидатах модель не стала
    строже (`.planning/evidence` волны Э2-Е). Владелец требует решающий опыт —
    пересчитать **весь** пул воротного прогона Э2-Г (112 кандидатов) сегодняшней
    моделью и сегодняшним промптом `scorer`. Если потолок оценок вернётся к 7 —
    версия «виновата модель» закрыта окончательно, а не по косвенным признакам.

    Скрипт умеет пересчитать и другой сохранённый пул (например, Э2-Д) тем же
    путём — номер запроса передаётся аргументом, а не зашивается в код.

ПРАВИЛА ЗАМЕРА (обязательны, закреплены тестами `tests/test_rescore_probe.py`)
    * размер батча и его состав — те же, что в бою: ровно 10 кандидатов,
      порядок — порядок дедупликации того прогона, то есть порядок строк
      `candidates` по возрастанию `id` (сортировок «для удобства» нет);
    * бриф не читается из базы (таблица `briefs` пуста, РЕШ-156) — подаётся
      файлом `--brief-file` и печатается в отчёт дословно вместе с указанием,
      из какого файла он взят;
    * `citations` не передаётся НИ ОДНОМУ пулу (`citations=None`) — цитируемость
      не сохранена и для старых прогонов невосстановима. Это отличие от
      боевого прогона называется отдельной строкой в выводе, а не замалчивается;
    * промпт `scorer` не трогается ни одной буквой — смысл замера в том, что
      промпт сегодняшний.

ГРАНИЦЫ (РЕШ-119)
    Скрипт живёт ВНЕ поставки продукта: `backend/app` его не импортирует,
    подкоманды `scout rescore` нет и не будет. Запускает скрипт только
    оркестратор — платный вызов делает он, не исполнитель задачи (§9
    CONVENTIONS.md, инвариант №7 для агентов).

    База открывается ТОЛЬКО НА ЧТЕНИЕ (`sqlite3`, URI `?mode=ro`) — попытка
    записи в такое соединение отказывает на уровне sqlite, а не по соглашению.
    Внешние вызовы модели идут исключительно через `app.llm.LLMRouter` и
    стадию `app.pipeline.stages.score_candidates` — тот же путь, которым идёт
    продукт (инвариант №6). Ключи берутся из окружения тем же способом, что и
    в продукте (`app.config.get_settings`), и нигде не печатаются (инвариант №3).

ЗАПУСК
    .venv/bin/python scripts/rescore_probe.py --help
    .venv/bin/python scripts/rescore_probe.py \\
        --db /home/user/techmost-scout/data/scout.db --request 1 \\
        --brief-file .planning/evidence/E2E-brief-фарма.json --dry-run
    .venv/bin/python scripts/rescore_probe.py \\
        --db /home/user/techmost-scout/data/scout.db --request 1 \\
        --brief-file .planning/evidence/E2E-brief-фарма.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import sys
from pathlib import Path

#: Размер батча скоринга в бою (`backend/app/pipeline/scoring.py:90`). Число не
#: выносится в аргумент командной строки нарочно: смысл замера — повторить
#: боевые условия, а не подобрать их под желаемый результат (граница задачи).
BATCH_SIZE = 10


def build_parser() -> argparse.ArgumentParser:
    """Собрать разбор аргументов — используется и `main`, и тестом на `--help`."""
    parser = argparse.ArgumentParser(
        prog="rescore_probe.py",
        description=(
            "Контрольный пересчёт сохранённого пула кандидатов сегодняшней моделью "
            "и сегодняшним промптом scorer (РЕШ-161). Скрипт вне поставки продукта — "
            "команды scout rescore не существует."
        ),
    )
    parser.add_argument(
        "--db",
        required=True,
        metavar="ПУТЬ",
        help="Путь к базе SQLite (открывается только на чтение, ?mode=ro).",
    )
    parser.add_argument(
        "--request",
        required=True,
        type=int,
        metavar="N",
        help="Номер запроса (request_id) в базе, чей пул кандидатов пересчитывается.",
    )
    parser.add_argument(
        "--brief-file",
        required=True,
        metavar="ПУТЬ",
        help=(
            "Файл с брифом в формате JSON схемы Brief (industry, task_summary, "
            "constraints, ...) — восстановлен вручную из воротного отчёта, "
            "потому что таблица briefs пуста (РЕШ-156). Путь к файлу и есть "
            "указание, откуда бриф восстановлен."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Взять только первые N кандидатов пула (по возрастанию id). По умолчанию — весь пул.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ничего не оплачивать: напечатать состав батчей и число будущих вызовов модели.",
    )
    return parser


def _readonly_uri(db_path: str) -> str:
    """URI подключения sqlite только на чтение — единая точка правды для замера.

    Мутация «база открывается без `mode=ro`» ловится тестом, который сверяет
    ровно эту строку и отдельно проверяет, что запись через неё отказывает.
    """
    return f"file:{db_path}?mode=ro"


def load_candidates(db_path: str, request_id: int, limit: int | None = None) -> list:
    """Читает пул кандидатов запроса из базы, порядок — по возрастанию `id`.

    Порядок `id` — это порядок дедупликации того прогона (Candidate пишутся в
    базу в порядке, в котором дошли до отбора, `backend/app/models.py`).
    Никакой сортировки «для удобства»: промпт скоринга различает кандидатов
    внутри списка, и порядок влияет на оценку (постановка задачи).

    Соединение открывается в режиме `?mode=ro`: попытка записи в него
    отказывает средствами sqlite, а не по соглашению кода.
    """
    from app.schemas import Candidate, SourceType

    con = sqlite3.connect(_readonly_uri(db_path), uri=True)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT source_type, provider, title, url, org, authors, year, snippet, raw_id "
            "FROM candidates WHERE request_id = ? ORDER BY id",
            (request_id,),
        )
        rows = cur.fetchall()
    finally:
        con.close()

    if limit is not None:
        rows = rows[:limit]

    candidates = []
    for source_type, provider, title, url, org, authors_json, year, snippet, raw_id in rows:
        candidates.append(
            Candidate(
                source_type=SourceType(source_type),
                title=title,
                url=url,
                org=org,
                authors=json.loads(authors_json) if authors_json else [],
                year=year,
                snippet=snippet or "",
                raw_id=raw_id,
                provider=provider,
            )
        )
    return candidates


def load_brief(path: str):
    """Читает бриф из файла JSON схемы `Brief` и его дословный текст.

    Дословный текст возвращается отдельно: критерий приёмки требует, чтобы
    бриф печатался в отчёт дословно, а не пересказом полей после валидации
    pydantic (порядок ключей, лишние пробелы и т.п. — не предмет замера).
    """
    from app.schemas import Brief

    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    return Brief.model_validate(data), raw


def batches_of(items: list, size: int = BATCH_SIZE) -> list[list]:
    """Разбивка списка на батчи размером `size`, последний — неполный.

    Та же простая нарезка срезами, что и в бою (`scoring.py`, `score_candidates`):
    один источник правды для дробления, а не вторая копия арифметики.
    """
    return [items[i : i + size] for i in range(0, len(items), size)]


def provider_and_model(settings) -> tuple[str, str]:
    """Провайдер и модель, НАЗНАЧЕННЫЕ на стадию `scorer` сегодняшними настройками.

    Печатается назначенный путь, а не угаданный по факту вызова: реальный
    вызов может уйти на резервного провайдера при отказе основного — это
    видно в журнале самого роутера, а не в этой диагностической строке.
    """
    provider = settings.stage_routes().get("scorer", "не назначен")
    model = getattr(settings, f"{provider}_model", "не задано")
    return provider, model


def format_distribution(values: list[int]) -> str:
    """Распределение оценок вида «0×28, 1×17, 2×22, 4×3, 7×2» — только занятые значения."""
    if not values:
        return "нет оценок"
    counts: dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return ", ".join(f"{value}×{count}" for value, count in sorted(counts.items()))


def print_dry_run(
    candidates: list, chunks: list[list], brief_path: str, brief_raw: str, provider: str, model: str
) -> None:
    """Сухой прогон: состав батчей и число будущих вызовов, без единой оплаты."""
    print("=== СУХОЙ ПРОГОН (ничего не оплачено) ===")
    print(f"провайдер/модель для стадии scorer: {provider} / {model}")
    print(f"бриф восстановлен из файла: {brief_path}")
    print("бриф дословно:")
    print(brief_raw)
    print(
        "citations: не передаётся ни одному пулу (значение None) — цитируемость "
        "не сохранена и для этого прогона невосстановима (РЕШ-156); это отличие "
        "от боевого прогона, а не умолчание."
    )
    print(f"кандидатов в пуле: {len(candidates)}")
    print(f"размер батча: {BATCH_SIZE} (как в бою, scoring.py:90)")
    print(f"батчей: {len(chunks)} — столько же будущих вызовов модели")
    for i, chunk in enumerate(chunks, start=1):
        titles = "; ".join(c.title[:60] for c in chunk)
        print(f"  батч {i}/{len(chunks)} ({len(chunk)} шт.): {titles}")


def print_summary(scored: list, batches: int, provider: str, model: str, spent_rub: float) -> None:
    """Печатает распределение оценок relevance и служебные счётчики (критерий 3 п.3)."""
    from app.pipeline.stages import has_neutral_score

    model_values = [item.relevance for item in scored if not has_neutral_score(item)]
    neutral_count = sum(1 for item in scored if has_neutral_score(item))

    print("=== ИТОГ ПЕРЕСЧЁТА ===")
    print(f"провайдер/модель: {provider} / {model}")
    print(f"кандидатов: {len(scored)}")
    print(f"батчей: {batches}")
    print(f"распределение оценок relevance (только оценки модели): {format_distribution(model_values)}")
    if model_values:
        print(
            f"min/медиана/max по relevance: {min(model_values)}/{statistics.median(model_values):g}/{max(model_values)}"
        )
    else:
        print("min/медиана/max по relevance: нет оценок модели")
    print(f"кандидатов с relevance >= 3: {sum(1 for v in model_values if v >= 3)}")
    print(f"кандидатов с relevance >= 4: {sum(1 for v in model_values if v >= 4)}")
    print(f"нейтральных оценок (следы отказа батча, не измерение): {neutral_count}")
    print(f"стоимость прогона скрипта: {spent_rub:.4f} ₽")


async def run_live(router, brief, chunks: list[list]) -> list:
    """Прогнать батчи через боевую стадию `score_candidates`, печатая прогресс.

    `citations=None` передаётся ЯВНО и всегда — правило замера (постановка
    задачи, РЕШ-156): цитируемость для старых пулов не сохранена и не
    восстановима, домысливать её было бы нарушением инварианта №1.
    """
    from app.pipeline.stages import score_candidates

    scored: list = []
    for i, chunk in enumerate(chunks, start=1):
        print(f"батч {i}/{len(chunks)} ({len(chunk)} шт.)...", file=sys.stderr)
        scored.extend(await score_candidates(router, brief, chunk, citations=None))
    return scored


async def _main_async(args: argparse.Namespace) -> int:
    from app.config import get_settings
    from app.llm import LLMRouter

    candidates = load_candidates(args.db, args.request, args.limit)
    if not candidates:
        print(f"кандидатов запроса {args.request} не найдено — пересчитывать нечего", file=sys.stderr)
        return 1

    brief, brief_raw = load_brief(args.brief_file)
    chunks = batches_of(candidates, BATCH_SIZE)

    settings = get_settings()
    provider, model = provider_and_model(settings)

    if args.dry_run:
        print_dry_run(candidates, chunks, args.brief_file, brief_raw, provider, model)
        return 0

    router = LLMRouter(settings)
    scored = await run_live(router, brief, chunks)
    print_summary(scored, len(chunks), provider, model, router.cost.spent_rub)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
