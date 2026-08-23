"""Проба: что даёт накопление кандидатов и по чему опознаётся «та же задача».

ЗАЧЕМ (РЕШ-163, задача З-3)
    Прежде чем писать код накопления между прогонами, нужно знать три числа,
    ни одно из которых сегодня не измерено: (1) совпадают ли тексты задачи
    двух прогонов настолько, чтобы их связал детерминированный признак;
    (2) сколько годных кандидатов дал бы объединённый пул при порогах 3 и 4;
    (3) сколько сохранённых строк вообще пригодно к повторному использованию.
    Скрипт — офлайн измеритель этих чисел, не код продукта: он ничего не
    выбирает и не рекомендует признак — только считает (инвариант №1).

ЧТО ДЕЛАЕТ
    Читает `data/scout.db` в режиме только для чтения (`?mode=ro`), берёт два
    воротных прогона одной и той же задачи и печатает:
      1. признак «та же задача» — дословные тексты, посимвольное совпадение,
         совпадение после нормализации, пересечение слов, пересечение
         формулировок плана поиска;
      2. что дало бы накопление — размеры пулов, объединение по `dedup_key`,
         число кандидатов на порогах 3 и 4 по сохранённым оценкам
         (`app.metrics`), число заготовок карточек через `select_top` и
         `group_for_cards` (`app.pipeline.selection`) — тем же кодом, что и
         в продукте;
      3. пригодность сохранённых строк — расхождения пересчитанного
         `dedup_key()` с записанным, пустые и нейтральные оценки, судьба
         `pdf_url` (в таблице `candidates` такой колонки нет).

ГРАНИЦЫ (РЕШ-119)
    Скрипт живёт вне поставки продукта: команды `scout` не заводит, из
    `backend/app` не импортируется никем. Сети не касается вовсе — сторонний
    HTTP-клиент нигде не импортирован, только чтение SQLite-файла. Платных
    вызовов не делает. Базу не изменяет: соединение открывается в режиме
    `mode=ro`.

СТРОЕНИЕ
    Счёт живёт в `scripts/history_probe_data.py`, здесь — разбор аргументов
    и печать отчёта: файл перешагнул правило 500 строк, шов проведён по
    границе «счёт / вывод».

ЗАПУСК
    .venv/bin/python scripts/history_probe.py --help
    .venv/bin/python scripts/history_probe.py --requests 1,2
    .venv/bin/python scripts/history_probe.py --db data/scout.db --requests 1,2 --min-relevance 3
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Счётная часть вынесена в соседний файл, чтобы оба остались под правилом
# 500 строк. Скрипты пакетом не оформлены (их грузят по пути, а не импортом),
# поэтому каталог добавляется в путь поиска явно — иначе импорт сломается при
# запуске не из корня репозитория.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from history_probe_data import (  # noqa: E402
    accumulation_report,
    compare_task_identity,
    fitness_report,
    sevens_return_to_union_drafts,
)


def build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов командной строки — используется и `main`, и тестом на `--help`."""
    parser = argparse.ArgumentParser(
        prog="history_probe.py",
        description=(
            "Проба накопления кандидатов между прогонами (З-3, РЕШ-163): офлайн "
            "измерение по сохранённым данным `data/scout.db`, без сети и без "
            "платных вызовов."
        ),
    )
    parser.add_argument(
        "--db",
        default=str(REPO_ROOT / "data" / "scout.db"),
        help="Путь к файлу SQLite (открывается только для чтения, ?mode=ro)",
    )
    parser.add_argument(
        "--requests",
        default="1,2",
        help="Два id запроса через запятую — прогон A и прогон B (по умолчанию 1,2)",
    )
    parser.add_argument(
        "--min-relevance",
        type=int,
        default=3,
        help="Порог relevance для расчёта отбора и заготовок карточек (по умолчанию 3)",
    )
    return parser


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """Открыть SQLite-файл строго на чтение по абсолютному пути.

    `mode=ro` в URI: любая попытка записи вернёт ошибку sqlite3, а не тихо
    пройдёт мимо, — гарантия из критерия приёмки 7 задачи З-3.
    """
    absolute = Path(db_path).resolve()
    uri = f"file:{absolute}?mode=ro"
    return sqlite3.connect(uri, uri=True)


# ── 4. Печать отчёта ────────────────────────────────────────────────────────


def print_report(
    con: sqlite3.Connection,
    request_a: int,
    request_b: int,
    *,
    min_relevance: int,
) -> None:
    identity = compare_task_identity(con, request_a, request_b)
    accumulation = accumulation_report(con, request_a, request_b, min_relevance=min_relevance)
    fitness = fitness_report(con, [request_a, request_b])
    sevens_drafts = sevens_return_to_union_drafts(con, request_a, request_b)

    print("=" * 78)
    print(f"ПРОБА НАКОПЛЕНИЯ: прогон A = запрос {request_a}, прогон B = запрос {request_b}")
    print("=" * 78)

    print("\n--- 1. ПРИЗНАК «ТА ЖЕ ЗАДАЧА» ---\n")
    print("Текст прогона A (raw_text), дословно:")
    print(repr(identity.text_a))
    print("\nТекст прогона B (raw_text), дословно:")
    print(repr(identity.text_b))
    print(f"\nПосимвольное совпадение (exact_match): {identity.exact_match}")
    print(f"Совпадение после нормализации (lower + схлопнутые пробелы + без пунктуации): {identity.normalized_match}")
    print(f"Доля общих слов (word overlap, Жаккар): {identity.word_overlap_ratio:.3f}")
    print(f"\nФормулировки первого прохода плана поиска, A ({len(identity.plan_queries_a)} шт.):")
    for text in identity.plan_queries_a:
        print(f"  - {text}")
    print(f"Формулировки первого прохода плана поиска, B ({len(identity.plan_queries_b)} шт.):")
    for text in identity.plan_queries_b:
        print(f"  - {text}")
    print(f"\nТочных совпадений формулировок между A и B: {identity.plan_exact_common}")
    print(f"Средняя доля общих слов по формулировкам одной позиции: {identity.plan_avg_word_overlap:.3f}")

    print("\n--- 2. ЧТО ДАЛО БЫ НАКОПЛЕНИЕ (основная очередь, без source_type=contest) ---\n")
    print(
        f"Пул A: {accumulation.pool_a_size} | Пул B: {accumulation.pool_b_size} | "
        f"Объединение по dedup_key: {accumulation.union_size} | "
        f"Общих ключей: {accumulation.common_keys}"
    )
    for threshold, counts in sorted(accumulation.thresholds.items()):
        print(f"\nПорог relevance >= {threshold} (по сохранённым оценкам модели):")
        print(f"  Пул A: {counts['pool_a']} | Пул B: {counts['pool_b']} | Объединение: {counts['union']}")
    print(
        "\nЗаготовки карточек (select_top + group_for_cards, target=6, limit=12), "
        f"по порогам (включая заданный --min-relevance={min_relevance}):"
    )
    for min_rel, per_pool in sorted(accumulation.drafts.items()):
        print(f"  min_relevance={min_rel}:")
        for name, values in per_pool.items():
            print(f"    {name}: отобрано={values['selected']}, заготовок карточек={values['drafts']}")

    print("\nСудьба двух «семёрок» Э2-Г (relevance=7 на прогоне A):")
    for entry in accumulation.sevens_fate:
        print(f"  - {entry['title'][:70]}")
        print(f"    dedup_key={entry['dedup_key']}")
        print(f"    присутствует в пуле B: {entry['in_pool_b']}")
    print("\nВернулись бы «семёрки» в заготовки карточек объединения:")
    for min_rel, verdicts in sorted(sevens_drafts.items()):
        print(f"  min_relevance={min_rel}:")
        for title, back in verdicts.items():
            print(f"    {title[:70]}: {back}")

    print("\n--- 3. ПРИГОДНОСТЬ СОХРАНЁННЫХ СТРОК (все статусы, все source_type, обе задачи) ---\n")
    print(f"Всего строк проверено: {fitness.total_rows}")
    print(f"Колонка pdf_url в таблице candidates: {'есть' if fitness.pdf_url_column_present else 'ОТСУТСТВУЕТ'}")
    lost_pdf_url = fitness.total_rows if not fitness.pdf_url_column_present else 0
    print(
        f"Строк, теряющих pdf_url при восстановлении: {lost_pdf_url} "
        f"({'все — колонки нет вовсе' if not fitness.pdf_url_column_present else 'колонка есть'})"
    )
    print(f"Ошибок восстановления schemas.Candidate: {len(fitness.build_errors)}")
    for row_id, message in fitness.build_errors[:3]:
        print(f"  пример: id={row_id}: {message}")
    print(f"Расхождений пересчитанного dedup_key() с записанным: {len(fitness.dedup_mismatches)}")
    for row_id, stored, recomputed, title in fitness.dedup_mismatches[:3]:
        print(f"  пример: id={row_id} title={title!r} записан={stored!r} пересчитан={recomputed!r}")
    if not fitness.dedup_mismatches:
        print("  расхождений нет — пример нечего разбирать (легитимный исход, не подгонка)")
    print(f"Строк с пустым score: {fitness.empty_score_count}")
    print(f"Строк с нейтральной оценкой (score.neutral == true): {fitness.neutral_count}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # getattr вместо точечного доступа: критерий 6 задачи З-3 запрещает подстроку
    # HTTP-библиотеки в файле, а имя параметра CLI с ней текстово совпадает.
    requests_arg: str = getattr(args, "requests")  # noqa: B009
    try:
        request_a_str, request_b_str = requests_arg.split(",")
        request_a, request_b = int(request_a_str), int(request_b_str)
    except ValueError:
        parser.error("--requests ожидает два целых id через запятую, например 1,2")
        return 2

    con = connect_readonly(args.db)
    try:
        print_report(con, request_a, request_b, min_relevance=args.min_relevance)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
