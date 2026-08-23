"""Оснастка диагностического скрипта scripts/rescore_probe.py (З-1 Э2-Е, РЕШ-161).

Инвариант №7 для исполнителя: сам скрипт исполнитель не запускает на живой
модели — платный вызов делает только оркестратор. Здесь проверяется код
скрипта на подменах: разбор аргументов, чтение базы только на чтение, состав
и порядок батчей, отсутствие `citations`, чтение брифа из файла и итоговая
сводка — без единого настоящего сетевого вызова.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.config import get_settings
from app.pipeline.stages import NEUTRAL_SCORE_NOTE, NeutralScoredCandidate
from app.schemas import Candidate, ScoredCandidate, SourceType

REPO_ROOT = Path(__file__).resolve().parent.parent
RESCORE_PATH = REPO_ROOT / "scripts" / "rescore_probe.py"


def _load_module() -> ModuleType:
    """Загрузить scripts/rescore_probe.py как модуль — он не пакет и не импортируется штатно."""
    spec = importlib.util.spec_from_file_location("scripts_rescore_probe_under_test", RESCORE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(title: str, *, provider: str = "crossref") -> Candidate:
    """Кандидат-заготовка: различается только заголовком."""
    return Candidate(
        source_type=SourceType.PUBLICATION,
        title=title,
        url=f"https://example.org/{abs(hash(title)) % 10_000}",
        provider=provider,
    )


#: Столбцы минимальной таблицы `candidates` — ровно те, что читает `load_candidates`,
#: плюс `id`/`request_id`, по которым она фильтрует и упорядочивает.
_DB_COLUMNS = (
    "id",
    "request_id",
    "source_type",
    "provider",
    "title",
    "url",
    "org",
    "authors",
    "year",
    "snippet",
    "raw_id",
)


def _make_db(path: Path, rows: list[tuple]) -> None:
    """Собрать минимальную базу candidates для теста — без SQLAlchemy, только чтение."""
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE candidates ("
            "id INTEGER PRIMARY KEY, request_id INTEGER, source_type TEXT, provider TEXT, "
            "title TEXT, url TEXT, org TEXT, authors TEXT, year INTEGER, snippet TEXT, raw_id TEXT)"
        )
        placeholders = ",".join("?" for _ in _DB_COLUMNS)
        # Имена столбцов — константа модуля `_DB_COLUMNS`, не пользовательский
        # ввод: подстановка строкой безопасна, значения идут через `?`.
        query = f"INSERT INTO candidates ({','.join(_DB_COLUMNS)}) VALUES ({placeholders})"  # noqa: S608
        con.executemany(query, rows)
        con.commit()
    finally:
        con.close()


# ── Критерий 1: --help называет параметры ───────────────────────────────────
def test_help_называет_параметры() -> None:
    result = subprocess.run(  # noqa: S603 — фиксированный путь к интерпретатору и скрипту, без ввода извне
        [sys.executable, str(RESCORE_PATH), "--help"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    for flag in ("--db", "--request", "--brief-file", "--limit"):
        assert flag in result.stdout, f"{flag} не назван в --help"


# ── Критерий 2: правила замера — размер и порядок батча, citations, бриф ────
def test_batches_of_режет_ровно_по_10_и_последний_неполный() -> None:
    module = _load_module()
    # Убывающий порядок — не совпадает ни с одной «естественной» сортировкой
    # по возрастанию: мутация, добавляющая сортировку, это переставит.
    items = list(range(22, -1, -1))  # 23 элемента: 22, 21, ..., 0

    chunks = module.batches_of(items)

    assert [len(c) for c in chunks] == [10, 10, 3]
    assert [x for chunk in chunks for x in chunk] == items, "порядок должен остаться без изменений"


def test_batches_of_размер_батча_фиксирован_10() -> None:
    """Мутация (а) — размер батча изменён на 20 — обязана уронить этот тест."""
    module = _load_module()
    assert module.BATCH_SIZE == 10


def test_load_candidates_упорядочивает_по_id_и_фильтрует_по_request(tmp_path: Path) -> None:
    """Мутация (б) — сортировка «для удобства» вместо порядка `id» — обязана
    уронить этот тест: заголовки специально не совпадают по алфавиту с id."""
    db_path = tmp_path / "probe.db"
    _make_db(
        db_path,
        [
            (3, 1, "publication", "crossref", "Я третий по id", "https://example.org/3", None, "[]", 2020, "", None),
            (1, 1, "publication", "crossref", "Б первый по id", "https://example.org/1", None, "[]", 2019, "", None),
            (2, 1, "publication", "crossref", "А второй по id", "https://example.org/2", None, "[]", 2021, "", None),
            (10, 2, "publication", "crossref", "Чужой запрос", "https://example.org/10", None, "[]", 2020, "", None),
        ],
    )
    module = _load_module()

    candidates = module.load_candidates(str(db_path), request_id=1)

    assert [c.title for c in candidates] == ["Б первый по id", "А второй по id", "Я третий по id"]


def test_load_candidates_limit_берёт_первые_n_по_id(tmp_path: Path) -> None:
    db_path = tmp_path / "probe.db"
    _make_db(
        db_path,
        [
            (
                i,
                1,
                "publication",
                "crossref",
                f"Кандидат {i:02d}",
                f"https://example.org/{i}",
                None,
                "[]",
                2020,
                "",
                None,
            )
            for i in range(1, 6)
        ],
    )
    module = _load_module()

    candidates = module.load_candidates(str(db_path), request_id=1, limit=2)

    assert [c.title for c in candidates] == ["Кандидат 01", "Кандидат 02"]


def test_load_candidates_разбирает_authors_из_json(tmp_path: Path) -> None:
    db_path = tmp_path / "probe.db"
    _make_db(
        db_path,
        [
            (
                1,
                1,
                "publication",
                "crossref",
                "T",
                "https://example.org/1",
                "Орг",
                '["Иванов И."]',
                2020,
                "снип",
                "10.1/x",
            )
        ],
    )
    module = _load_module()

    (candidate,) = module.load_candidates(str(db_path), request_id=1)

    assert candidate.authors == ["Иванов И."]
    assert candidate.org == "Орг"
    assert candidate.raw_id == "10.1/x"


# ── Критерий 3: база открыта только на чтение ───────────────────────────────
def test_readonly_uri_содержит_mode_ro_и_запись_отказывает(tmp_path: Path) -> None:
    """Мутация (г) — база открывается без `mode=ro» — обязана уронить этот тест."""
    db_path = tmp_path / "probe.db"
    _make_db(
        db_path,
        [(1, 1, "publication", "crossref", "T", "https://example.org/1", None, "[]", 2020, "", None)],
    )
    module = _load_module()

    uri = module._readonly_uri(str(db_path))
    assert "mode=ro" in uri

    con = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO candidates (id, request_id) VALUES (99, 1)")
            con.commit()
    finally:
        con.close()


# ── Правило замера: бриф читается из файла, не из базы ──────────────────────
def test_load_brief_читает_json_и_сохраняет_дословный_текст(tmp_path: Path) -> None:
    module = _load_module()
    brief_path = tmp_path / "brief.json"
    content = json.dumps(
        {
            "industry": "свиноводство",
            "task_summary": "Автоматический анализ УЗИ свиноматок.",
            "constraints": ["точность определения состояния — 95-98%"],
            "target_maturity": "prototype",
        },
        ensure_ascii=False,
        indent=2,
    )
    brief_path.write_text(content, encoding="utf-8")

    brief, raw = module.load_brief(str(brief_path))

    assert brief.industry == "свиноводство"
    assert brief.constraints == ["точность определения состояния — 95-98%"]
    assert raw == content, "бриф обязан печататься в отчёт дословно — байт в байт из файла"


# ── Критерий 2: citations=None и сохранённый порядок передаются в стадию ────
async def test_run_live_батчи_порядок_и_citations_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Мутация (в) — `citations` собирается и передаётся — обязана уронить этот тест."""
    module = _load_module()
    candidates = [_candidate(f"Кандидат {i:02d}") for i in range(23)]
    chunks = module.batches_of(candidates)

    calls: list[dict] = []

    async def fake_score_candidates(router, brief, chunk, *, citations=None):
        calls.append({"chunk": list(chunk), "citations": citations})
        return []

    monkeypatch.setattr("app.pipeline.stages.score_candidates", fake_score_candidates)

    scored = await module.run_live(router=object(), brief=object(), chunks=chunks)

    assert scored == []
    assert [len(c["chunk"]) for c in calls] == [10, 10, 3]
    flat = [cand for c in calls for cand in c["chunk"]]
    assert flat == candidates, "порядок и состав батчей обязаны совпасть с порядком дедупликации"
    assert all(c["citations"] is None for c in calls), "citations обязан быть None для каждого батча"


# ── Диагностическая печать ───────────────────────────────────────────────────
def test_format_distribution_печатает_только_занятые_значения() -> None:
    module = _load_module()
    assert module.format_distribution([0, 0, 1, 2, 2, 7]) == "0×2, 1×1, 2×2, 7×1"
    assert module.format_distribution([]) == "нет оценок"


def test_print_summary_печатает_распределение_min_median_max_и_нейтральных(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    scored = [
        ScoredCandidate(
            candidate=_candidate("a"), relevance=0, maturity=5, accessibility=5, freshness=5, completeness=5, notes=""
        ),
        ScoredCandidate(
            candidate=_candidate("b"), relevance=4, maturity=5, accessibility=5, freshness=5, completeness=5, notes=""
        ),
        NeutralScoredCandidate(
            candidate=_candidate("c"),
            relevance=5,
            maturity=5,
            accessibility=5,
            freshness=5,
            completeness=5,
            notes=NEUTRAL_SCORE_NOTE,
        ),
    ]

    module.print_summary(scored, batches=1, provider="deepseek", model="deepseek-v4-flash", spent_rub=0.05)

    out = capsys.readouterr().out
    assert "распределение оценок relevance (только оценки модели): 0×1, 4×1" in out
    assert "min/медиана/max по relevance: 0/2/4" in out
    assert "кандидатов с relevance >= 3: 1" in out
    assert "кандидатов с relevance >= 4: 1" in out
    assert "нейтральных оценок (следы отказа батча, не измерение): 1" in out
    assert "стоимость прогона скрипта: 0.0500" in out


def test_provider_and_model_берёт_маршрут_scorer(settings) -> None:
    module = _load_module()
    provider, model = module.provider_and_model(settings)
    assert provider == "deepseek"
    assert model == settings.deepseek_model


# ── Сквозные сценарии через _main_async ──────────────────────────────────────
def _brief_file(tmp_path: Path) -> Path:
    path = tmp_path / "brief.json"
    path.write_text(
        json.dumps({"industry": "свиноводство", "task_summary": "Суть задачи."}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


async def test_dry_run_не_создаёт_router_и_печатает_состав_батчей(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    db_path = tmp_path / "probe.db"
    rows = [
        (i, 1, "publication", "crossref", f"Кандидат {i:02d}", f"https://example.org/{i}", None, "[]", 2020, "", None)
        for i in range(1, 24)
    ]
    _make_db(db_path, rows)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("сухой прогон не должен создавать LLMRouter — это уже платный путь")

    monkeypatch.setattr("app.llm.LLMRouter", _boom)
    get_settings.cache_clear()

    args = module.build_parser().parse_args(
        ["--db", str(db_path), "--request", "1", "--brief-file", str(_brief_file(tmp_path)), "--dry-run"]
    )
    code = await module._main_async(args)
    get_settings.cache_clear()

    out = capsys.readouterr().out
    assert code == 0
    assert "СУХОЙ ПРОГОН" in out
    assert "батчей: 3" in out
    assert "батч 1/3 (10 шт.)" in out
    assert "батч 3/3 (3 шт.)" in out
    assert "citations: не передаётся" in out
    assert "бриф восстановлен из файла" in out


async def test_живой_прогон_печатает_итог_и_не_палит_фиктивный_ключ(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_provider_factory
) -> None:
    module = _load_module()
    db_path = tmp_path / "probe.db"
    rows = [
        (i, 1, "publication", "crossref", f"Кандидат {i:02d}", f"https://example.org/{i}", None, "[]", 2020, "", None)
        for i in range(1, 4)
    ]
    _make_db(db_path, rows)

    score_json = (
        '{"scores": ['
        + ",".join(
            f'{{"index": {i}, "relevance": {i + 2}, "maturity": 5, "accessibility": 5, '
            f'"freshness": 5, "completeness": 5, "notes": ""}}'
            for i in range(3)
        )
        + "]}"
    )
    provider = fake_provider_factory("deepseek", [score_json])

    from app.llm.router import LLMRouter as RealLLMRouter

    def _router_factory(settings=None, providers=None):
        return RealLLMRouter(settings, providers={"deepseek": provider})

    monkeypatch.setattr("app.llm.LLMRouter", _router_factory)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-leak-check-777")
    get_settings.cache_clear()

    args = module.build_parser().parse_args(
        ["--db", str(db_path), "--request", "1", "--brief-file", str(_brief_file(tmp_path))]
    )
    code = await module._main_async(args)
    get_settings.cache_clear()

    out = capsys.readouterr().out
    assert code == 0
    assert "ИТОГ ПЕРЕСЧЁТА" in out
    assert "кандидатов: 3" in out
    assert "батчей: 1" in out
    assert "sk-fake-leak-check-777" not in out, "фиктивный ключ не должен просочиться в вывод (инвариант №3)"


async def test_пустой_пул_не_падает_и_сообщает_причину(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_module()
    db_path = tmp_path / "probe.db"
    _make_db(db_path, [])
    args = module.build_parser().parse_args(
        ["--db", str(db_path), "--request", "1", "--brief-file", str(_brief_file(tmp_path)), "--dry-run"]
    )

    code = await module._main_async(args)

    assert code == 1
    err = capsys.readouterr().err
    assert "не найдено" in err


# ── Инварианты 3/6: слои, а не прямой httpx ──────────────────────────────────
def test_нет_прямого_httpx_и_есть_llmrouter() -> None:
    text = RESCORE_PATH.read_text(encoding="utf-8")
    assert "import httpx" not in text
    assert "LLMRouter" in text
