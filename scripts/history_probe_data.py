"""Счётная часть пробы накопления: чтение базы и три раздела чисел.

Модуль выделен из `scripts/history_probe.py`, когда тот перешагнул правило
500 строк (CLAUDE.md). Разделение проходит по естественному шву пробы: здесь
только счёт — чтение сохранённых строк, признак «та же задача», числа
накопления и пригодность строк к восстановлению; печать отчёта и разбор
командной строки остались в `history_probe.py`. Ни одна функция отсюда ничего
не печатает и базу не изменяет.

Границы те же, что у самой пробы (РЕШ-119): вне поставки продукта, из
`backend/app` не импортируется никем, сети не касается — только чтение
SQLite-файла, открытого вызывающим в режиме `mode=ro`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

#: Тип источника «конкурсы» не входит в основную очередь — та же граница,
#: что в продукте (`app.pipeline.diversity.split_contest_candidates`,
#: `COVERAGE_EXCLUDED_SOURCE_TYPES`): конкурсный кандидат не конкурирует за
#: общий отбор с публикациями и патентами.
CONTEST_SOURCE_TYPE = "contest"


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
