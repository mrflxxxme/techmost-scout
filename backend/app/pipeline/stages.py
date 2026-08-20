"""Фасад стадий пайплайна (РЕШ-94): реэкспорт из `prompting`, `intake`,
`planning`, `extraction`, `scoring`, `selection`, `cards`, `summary`.

Модуль остаётся точкой импорта: `orchestrator.py` зовёт стадии через
`from app.pipeline import stages`, `cli.py` — локальным импортом, а тесты
подменяют их через `monkeypatch.setattr(stages, "имя", …)`. Ни одного `def`,
`async def` или `class` здесь нет — только реэкспорт.
"""

from __future__ import annotations

from app.config import get_settings
from app.pipeline.cards import (
    LONG_CYCLE_CONTEST_DOMAINS,
    LONG_CYCLE_CONTEST_NAMES,
    VerifyResult,
    author_card,
    cap_contest_maturity,
    contest_maturity_ceiling,
    verify_card,
)
from app.pipeline.extraction import (
    COVERAGE_EXCLUDED_SOURCE_TYPES,
    ExtractedBatch,
    count_source_type_coverage,
    deduplicate,
    extract_candidates,
)
from app.pipeline.intake import brief_from_raw_text, refine_brief, run_intake
from app.pipeline.planning import (
    first_pass_listing,
    normalize_contest_queries,
    plan_queries_listing,
    plan_search,
    replan_search,
    strip_contest_queries,
)
from app.pipeline.prompting import (
    candidates_listing,
    candidates_to_json,
    load_gov_support,
    load_prompt,
    neutralize_placeholders,
    owner_answers_block,
    render_prompt,
)
from app.pipeline.scoring import (
    NEUTRAL_SCORE_NOTE,
    NO_MODEL_SCORES_LABEL,
    REPLAN_ENTER_LOG,
    REPLAN_SKIP_LOG,
    CandidateScore,
    NeutralScoredCandidate,
    ScoreBatch,
    has_neutral_score,
    log_relevance_calibration,
    max_model_relevance,
    needs_second_pass,
    score_candidates,
)
from app.pipeline.scoring import _has_neutral_score as _has_neutral_score
from app.pipeline.scoring import _model_relevance as _model_relevance
from app.pipeline.scoring import _neutral_scores as _neutral_scores
from app.pipeline.selection import (
    MAX_PUBLISHED_CARDS,
    group_for_cards,
    select_publishable_cards,
    select_top,
)
from app.pipeline.summary import fallback_summary, summarize

__all__ = [
    "COVERAGE_EXCLUDED_SOURCE_TYPES",
    "LONG_CYCLE_CONTEST_DOMAINS",
    "LONG_CYCLE_CONTEST_NAMES",
    "MAX_PUBLISHED_CARDS",
    "NEUTRAL_SCORE_NOTE",
    "NO_MODEL_SCORES_LABEL",
    "REPLAN_ENTER_LOG",
    "REPLAN_SKIP_LOG",
    "CandidateScore",
    "ExtractedBatch",
    "NeutralScoredCandidate",
    "ScoreBatch",
    "VerifyResult",
    "author_card",
    "brief_from_raw_text",
    "candidates_listing",
    "candidates_to_json",
    "cap_contest_maturity",
    "contest_maturity_ceiling",
    "count_source_type_coverage",
    "deduplicate",
    "extract_candidates",
    "fallback_summary",
    "first_pass_listing",
    "get_settings",
    "group_for_cards",
    "has_neutral_score",
    "load_gov_support",
    "load_prompt",
    "log_relevance_calibration",
    "max_model_relevance",
    "needs_second_pass",
    "neutralize_placeholders",
    "normalize_contest_queries",
    "owner_answers_block",
    "plan_queries_listing",
    "plan_search",
    "refine_brief",
    "render_prompt",
    "replan_search",
    "run_intake",
    "score_candidates",
    "select_publishable_cards",
    "select_top",
    "strip_contest_queries",
    "summarize",
    "verify_card",
]
