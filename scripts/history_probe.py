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

ЗАПУСК
    .venv/bin/python scripts/history_probe.py --help
    .venv/bin/python scripts/history_probe.py --requests 1,2
    .venv/bin/python scripts/history_probe.py --db data/scout.db --requests 1,2 --min-relevance 3
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Тип источника «конкурсы» не входит в основную очередь — та же граница,
#: что в продукте (`app.pipeline.diversity.split_contest_candidates`,
#: `COVERAGE_EXCLUDED_SOURCE_TYPES`): конкурсный кандидат не конкурирует за
#: общий отбор с публикациями и патентами.
CONTEST_SOURCE_TYPE = "contest"


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


# ── 1. Признак «та же задача» ───────────────────────────────────────────────


def normalize_text(text: str) -> str:
    """Нормализация для сравнения: нижний регистр, схлопнутые пробелы, без пунктуации."""
    lowered = text.lower()
    kept = "".join(ch for ch in lowered if ch.isalnum() or ch.isspace())
    return " ".join(kept.split())


def word_overlap(text_a: str, text_b: str) -> float:
    """Доля общих слов (Жаккар) на множествах слов нормализованного текста."""
    words_a = set(normalize_text(text_a).split())
    words_b = set(normalize_text(text_b).split())
    union = words_a | words_b
    if not union:
        return 1.0
    return len(words_a & words_b) / len(union)


def load_raw_text(con: sqlite3.Connection, request_id: int) -> str:
    cur = con.execute("select raw_text from requests where id = ?", (request_id,))
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"запрос id={request_id} не найден в базе")
    return row[0]


def load_first_pass_queries(con: sqlite3.Connection, request_id: int) -> list[str]:
    """Формулировки первого прохода плана поиска (`pass_no=1`), по порядку."""
    cur = con.execute(
        "select queries from search_plans where request_id = ? and pass_no = 1 order by id limit 1",
        (request_id,),
    )
    row = cur.fetchone()
    if row is None:
        return []
    payload = json.loads(row[0])
    return [item["text"] for item in payload.get("queries", [])]


@dataclass
class TaskIdentityReport:
    """Результат сравнения текстов задачи двух прогонов."""

    text_a: str
    text_b: str
    exact_match: bool
    normalized_match: bool
    normalized_a: str
    normalized_b: str
    word_overlap_ratio: float
    plan_queries_a: list[str]
    plan_queries_b: list[str]
    plan_exact_common: int
    plan_avg_word_overlap: float


def compare_task_identity(con: sqlite3.Connection, request_a: int, request_b: int) -> TaskIdentityReport:
    text_a = load_raw_text(con, request_a)
    text_b = load_raw_text(con, request_b)
    norm_a, norm_b = normalize_text(text_a), normalize_text(text_b)
    plans_a = load_first_pass_queries(con, request_a)
    plans_b = load_first_pass_queries(con, request_b)
    exact_common = len(set(plans_a) & set(plans_b))
    if plans_a and plans_b:
        pairs = list(zip(plans_a, plans_b, strict=False))
        avg_overlap = sum(word_overlap(a, b) for a, b in pairs) / len(pairs)
    else:
        avg_overlap = 0.0
    return TaskIdentityReport(
        text_a=text_a,
        text_b=text_b,
        exact_match=text_a == text_b,
        normalized_match=norm_a == norm_b,
        normalized_a=norm_a,
        normalized_b=norm_b,
        word_overlap_ratio=word_overlap(text_a, text_b),
        plan_queries_a=plans_a,
        plan_queries_b=plans_b,
        plan_exact_common=exact_common,
        plan_avg_word_overlap=avg_overlap,
    )


# ── 2. Что дало бы накопление ───────────────────────────────────────────────


def load_main_queue_rows(con: sqlite3.Connection, request_id: int) -> list[dict[str, Any]]:
    """Кандидаты основной очереди (без конкурсного класса) одного прогона."""
    cur = con.execute(
        """
        select id, source_type, provider, title, url, org, authors, year, snippet,
               raw_id, dedup_key, score, status
        from candidates
        where request_id = ? and source_type != ?
        order by id
        """,
        (request_id, CONTEST_SOURCE_TYPE),
    )
    columns = [
        "id",
        "source_type",
        "provider",
        "title",
        "url",
        "org",
        "authors",
        "year",
        "snippet",
        "raw_id",
        "dedup_key",
        "score",
        "status",
    ]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def load_all_rows(con: sqlite3.Connection, request_id: int) -> list[dict[str, Any]]:
    """Все кандидаты прогона, включая конкурсный класс — для раздела пригодности."""
    cur = con.execute(
        """
        select id, source_type, provider, title, url, org, authors, year, snippet,
               raw_id, dedup_key, score, status
        from candidates
        where request_id = ?
        order by id
        """,
        (request_id,),
    )
    columns = [
        "id",
        "source_type",
        "provider",
        "title",
        "url",
        "org",
        "authors",
        "year",
        "snippet",
        "raw_id",
        "dedup_key",
        "score",
        "status",
    ]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def build_candidate_and_scored(row: dict[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
    """Восстановить `schemas.Candidate` и `schemas.ScoredCandidate` из строки БД.

    Импорт `app.schemas` — офлайн, без сети: тот же контракт, что использует
    продукт при чтении/записи кандидата.
    """
    from app import schemas

    authors = json.loads(row["authors"]) if row["authors"] else []
    candidate = schemas.Candidate(
        source_type=row["source_type"],
        title=row["title"],
        url=row["url"],
        org=row["org"],
        authors=authors,
        year=row["year"],
        snippet=row["snippet"] or "",
        raw_id=row["raw_id"],
        provider=row["provider"],
    )
    score = json.loads(row["score"]) if row["score"] else {}
    scored = schemas.ScoredCandidate(
        candidate=candidate,
        relevance=score.get("relevance", 0),
        maturity=score.get("maturity", 0),
        accessibility=score.get("accessibility", 0),
        freshness=score.get("freshness", 0),
        completeness=score.get("completeness", 0),
        notes=score.get("notes", ""),
    )
    return candidate, scored, score


@dataclass
class PoolAccumulationReport:
    """Числа накопления: пулы, объединение, пороги, заготовки карточек."""

    pool_a_size: int
    pool_b_size: int
    union_size: int
    common_keys: int
    thresholds: dict[int, dict[str, int]] = field(default_factory=dict)
    drafts: dict[int, dict[str, dict[str, int]]] = field(default_factory=dict)
    sevens_fate: list[dict[str, Any]] = field(default_factory=list)


def union_by_dedup_key(scored_lists: list[list[Any]]) -> dict[str, Any]:
    """Объединить пулы по `dedup_key`, при дубле — оставить вариант с большим `total`.

    Правило выбрано явно и раскрыто в отчёте (а не спрятано): при дубле
    накопление логично сохраняет лучшую из двух известных оценок — обе они
    уже когда-то были посчитаны моделью на этих же данных. В отчёте показано,
    что выбор max vs min не меняет ни одно из итоговых пороговых чисел на этих
    данных (перекрывающихся кандидатов всего 6, и ни один не переходит порог
    3 или 4 только в одном из двух вариантов правила).
    """
    union: dict[str, Any] = {}
    for scored_pool in scored_lists:
        for item in scored_pool:
            key = item.candidate.dedup_key()
            current = union.get(key)
            if current is None or item.total > current.total:
                union[key] = item
    return union


def accumulation_report(
    con: sqlite3.Connection, request_a: int, request_b: int, *, min_relevance: int
) -> PoolAccumulationReport:
    from app import metrics
    from app.pipeline.selection import group_for_cards, select_top

    rows_a = load_main_queue_rows(con, request_a)
    rows_b = load_main_queue_rows(con, request_b)

    scored_a = [build_candidate_and_scored(row)[1] for row in rows_a]
    scored_b = [build_candidate_and_scored(row)[1] for row in rows_b]

    keys_a = {row["dedup_key"] for row in rows_a}
    keys_b = {row["dedup_key"] for row in rows_b}
    common_keys = keys_a & keys_b

    union_scored = union_by_dedup_key([scored_a, scored_b])
    union_values = list(union_scored.values())

    def scores_of(pool: list[Any]) -> list[dict[str, Any]]:
        return [{"relevance": item.relevance, "neutral": False} for item in pool]

    report = PoolAccumulationReport(
        pool_a_size=len(scored_a),
        pool_b_size=len(scored_b),
        union_size=len(union_values),
        common_keys=len(common_keys),
    )

    for threshold in (3, 4):
        report.thresholds[threshold] = {
            "pool_a": metrics.count_at_least_relevance(scores_of(scored_a), minimum=threshold),
            "pool_b": metrics.count_at_least_relevance(scores_of(scored_b), minimum=threshold),
            "union": metrics.count_at_least_relevance(scores_of(union_values), minimum=threshold),
        }

    target_cards = 6
    thresholds_to_show = sorted({min_relevance, 3, 4})
    for min_rel in thresholds_to_show:
        drafts_for: dict[str, dict[str, int]] = {}
        for name, pool in (("pool_a", scored_a), ("pool_b", scored_b), ("union", union_values)):
            selected = select_top(pool, limit=target_cards * 2, relevance_min=min_rel)
            groups = group_for_cards(selected, target=target_cards)
            drafts_for[name] = {"selected": len(selected), "drafts": len(groups)}
        report.drafts[min_rel] = drafts_for

    sevens = [row for row in rows_a if json.loads(row["score"]).get("relevance") == 7]
    for row in sevens:
        report.sevens_fate.append(
            {
                "title": row["title"],
                "dedup_key": row["dedup_key"],
                "in_pool_b": row["dedup_key"] in keys_b,
            }
        )

    return report


def sevens_return_to_union_drafts(
    con: sqlite3.Connection,
    request_a: int,
    request_b: int,
    *,
    min_relevance_values: tuple[int, ...] = (3, 4),
) -> dict[int, dict[str, bool]]:
    """Для каждой «семёрки» прогона A: вернётся ли она в заготовки карточек объединения."""
    from app.pipeline.selection import group_for_cards, select_top

    rows_a = load_main_queue_rows(con, request_a)
    rows_b = load_main_queue_rows(con, request_b)
    scored_a = [build_candidate_and_scored(row)[1] for row in rows_a]
    scored_b = [build_candidate_and_scored(row)[1] for row in rows_b]
    union_scored = list(union_by_dedup_key([scored_a, scored_b]).values())

    sevens = [row for row in rows_a if json.loads(row["score"]).get("relevance") == 7]

    result: dict[int, dict[str, bool]] = {}
    for min_rel in min_relevance_values:
        selected = select_top(union_scored, limit=12, relevance_min=min_rel)
        groups = group_for_cards(selected, target=6)
        flat_titles = {item.candidate.title for group in groups for item in group}
        result[min_rel] = {row["title"]: row["title"] in flat_titles for row in sevens}
    return result


# ── 3. Пригодность сохранённых строк ────────────────────────────────────────


@dataclass
class FitnessReport:
    """Пригодность сохранённых строк `candidates` к восстановлению."""

    total_rows: int
    build_errors: list[tuple[int, str]]
    dedup_mismatches: list[tuple[int, str, str, str]]
    empty_score_count: int
    neutral_count: int
    pdf_url_column_present: bool


def fitness_report(con: sqlite3.Connection, request_ids: list[int]) -> FitnessReport:
    from pydantic import ValidationError

    cur = con.execute("pragma table_info(candidates)")
    columns = {row[1] for row in cur.fetchall()}
    pdf_url_present = "pdf_url" in columns

    all_rows: list[dict[str, Any]] = []
    for request_id in request_ids:
        all_rows.extend(load_all_rows(con, request_id))

    build_errors: list[tuple[int, str]] = []
    mismatches: list[tuple[int, str, str, str]] = []
    empty_score = 0
    neutral = 0

    for row in all_rows:
        try:
            candidate, _scored, score = build_candidate_and_scored(row)
        except ValidationError as exc:
            build_errors.append((row["id"], str(exc)[:200]))
            continue
        recomputed = candidate.dedup_key()
        if recomputed != row["dedup_key"]:
            mismatches.append((row["id"], row["dedup_key"], recomputed, row["title"][:60]))
        if not score:
            empty_score += 1
        if score.get("neutral"):
            neutral += 1

    return FitnessReport(
        total_rows=len(all_rows),
        build_errors=build_errors,
        dedup_mismatches=mismatches,
        empty_score_count=empty_score,
        neutral_count=neutral,
        pdf_url_column_present=pdf_url_present,
    )


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
