"""Оснастка диагностического скрипта scripts/dedup_judge_probe.py (Д-6 Э2-З, РЕШ-133/152).

Инвариант №7 для исполнителя: сам скрипт исполнитель не запускает на живой
модели — платный вызов делает только оркестратор. Здесь проверяется код
скрипта на подменах: разметка, разбор пар, счёт точности, сухой прогон и
живой прогон на `FakeProvider` — без единого настоящего сетевого вызова.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = REPO_ROOT / "scripts" / "dedup_judge_probe.py"

_MODULE_NAME = "scripts_dedup_judge_probe_under_test"


def _load_module() -> ModuleType:
    """Загрузить scripts/dedup_judge_probe.py как модуль — он не пакет и не импортируется штатно.

    Модуль регистрируется в `sys.modules` ДО выполнения тела: `from __future__
    import annotations` откладывает разбор аннотаций до строки, а `@dataclass`
    в `DatasetResult` резолвит их через `sys.modules[cls.__module__]` — без
    регистрации разбор падает `AttributeError` на первом же импорте.
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


# ── Разметка: дословные заголовки и готовые пары (критерий приёмки) ─────────


def test_разметка_фармы_совпадает_с_постановкой() -> None:
    module = _load_module()
    assert len(module.PHARMA_ITEMS) == 5
    assert [item.number for item in module.PHARMA_ITEMS] == [1, 2, 3, 4, 5]
    assert frozenset({frozenset({3, 4})}) == module.PHARMA_TRUE_DUP_PAIRS


def test_разметка_нефтегаза_совпадает_с_постановкой() -> None:
    module = _load_module()
    assert len(module.OILGAS_ITEMS) == 6
    assert [item.number for item in module.OILGAS_ITEMS] == [1, 2, 3, 4, 5, 6]
    expected = frozenset(frozenset(pair) for pair in [(2, 3), (2, 4), (2, 5), (3, 4), (3, 5), (4, 5)])
    assert expected == module.OILGAS_TRUE_DUP_PAIRS
    # Карточки 1 и 6 — контрольные недубли, ни в одной паре разметки их нет.
    flat = {n for pair in module.OILGAS_TRUE_DUP_PAIRS for n in pair}
    assert 1 not in flat
    assert 6 not in flat


# ── org/snippet: главная находка ревью цикла 1 (E2Z-Д6-review.md) ───────────


def test_кандидаты_несут_org_и_snippet_из_воротных_карточек() -> None:
    """Блокирующая находка ревью цикла 1: проба подавала судье голые
    заголовки (`org=None`, `snippet=""`), боевая стадия — реальные поля.
    Кандидат 3 фармы — организация известна (`Kangwon National University`,
    названа в ревью дословно); кандидат 2 фармы — организация честно
    неизвестна (`org=None`), но `snippet` не пуст."""
    module = _load_module()
    pharma_3 = next(item for item in module.PHARMA_ITEMS if item.number == 3)
    assert pharma_3.org is not None
    assert "Kangwon National University" in pharma_3.org
    assert pharma_3.snippet

    pharma_2 = next(item for item in module.PHARMA_ITEMS if item.number == 2)
    assert pharma_2.org is None
    assert pharma_2.snippet


def test_org_none_только_там_где_карточка_честно_говорит_не_найдено() -> None:
    """Нефтегаз: организация известна только у карточки 6 (Instituto Federal
    do Piauí) — у остальных пяти `Разработчик: не найдено` дословно."""
    module = _load_module()
    orgs = {item.number: item.org for item in module.OILGAS_ITEMS}
    assert orgs[6] == "Instituto Federal do Piauí"
    assert all(orgs[n] is None for n in (1, 2, 3, 4, 5))


def test_все_кандидаты_несут_непустой_snippet() -> None:
    module = _load_module()
    for item in module.PHARMA_ITEMS + module.OILGAS_ITEMS:
        assert item.snippet, f"кандидат {item.number} набора без snippet"


def test_to_candidates_переносит_org_и_snippet_в_scored_candidate() -> None:
    """То, что реально уходит в `judge_duplicate_groups` (и дальше в
    `_listing`), обязано нести те же `org`/`snippet`, что и `ProbeItem` —
    иначе обогащение полей осталось бы только в докстроке, а не в вызове."""
    module = _load_module()
    candidates = module.to_candidates(module.PHARMA_ITEMS)

    assert len(candidates) == 5
    by_title = {c.candidate.title: c.candidate for c in candidates}
    kangwon = by_title["ИИ-система диагностики супоросности свиноматок по ультразвуковым изображениям"]
    assert kangwon.org is not None
    assert "Kangwon National University" in kangwon.org
    assert kangwon.snippet

    no_org = by_title["ИИ-система анализа ультразвуковых исследований беременности свиноматок"]
    assert no_org.org is None
    assert no_org.snippet


# ── all_pairs / judged_pairs / score: чистая арифметика точности ────────────


def test_all_pairs_считает_сочетания_без_повторов() -> None:
    module = _load_module()
    pairs = module.all_pairs(3)
    assert set(pairs) == {frozenset({1, 2}), frozenset({1, 3}), frozenset({2, 3})}
    assert len(pairs) == 3


def test_judged_pairs_разворачивает_группы_в_пары() -> None:
    module = _load_module()
    assert module.judged_pairs([[1, 2, 3]]) == {
        frozenset({1, 2}),
        frozenset({1, 3}),
        frozenset({2, 3}),
    }
    assert module.judged_pairs([[1, 2], [4, 5]]) == {frozenset({1, 2}), frozenset({4, 5})}
    assert module.judged_pairs([]) == set()


def test_score_идеальный_вердикт_даёт_только_tp_и_tn() -> None:
    module = _load_module()
    true_dup = frozenset({frozenset({3, 4})})
    result = module.score("тест", 5, true_dup, groups=[[3, 4]])

    assert result.true_positive == {frozenset({3, 4})}
    assert result.false_positive == set()
    assert result.false_negative == set()
    assert len(result.true_negative) == len(module.all_pairs(5)) - 1


def test_score_отказ_судьи_none_считается_пустым_вердиктом() -> None:
    """Мутация «а» для пробы: отказ судьи не должен вылетать исключением или
    подменяться «правильным» ответом — это честный ноль найденных дублей."""
    module = _load_module()
    true_dup = frozenset({frozenset({3, 4})})
    result = module.score("тест", 5, true_dup, groups=None)

    assert result.false_negative == {frozenset({3, 4})}
    assert result.true_positive == set()


def test_score_ложная_склейка_попадает_в_false_positive() -> None:
    module = _load_module()
    true_dup = frozenset({frozenset({3, 4})})

    result = module.score("тест", 5, true_dup, groups=[[1, 2, 3, 4, 5]])

    assert frozenset({1, 2}) in result.false_positive
    assert frozenset({3, 4}) in result.true_positive  # входит в ту же большую группу


# ── Сухой прогон ──────────────────────────────────────────────────────────


async def test_сухой_прогон_не_создаёт_router_и_печатает_оба_набора(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("сухой прогон не должен создавать LLMRouter — это уже платный путь")

    monkeypatch.setattr("app.llm.LLMRouter", _boom)

    args = module.build_parser().parse_args(["--dry-run"])
    code = await module._main_async(args)

    out = capsys.readouterr().out
    assert code == 0
    assert "СУХОЙ ПРОГОН" in out
    assert "Фарма" in out and "Нефтегаз" in out
    assert "ИИ-система определения беременности свиноматок по УЗИ" in out
    assert "керамической мембраны" in out
    # Блокирующая находка ревью цикла 1: дословный вывод обязан нести org и
    # snippet, а не только заголовки — иначе регрессия к «голым» кандидатам
    # прошла бы этот тест незамеченной.
    assert "org: Kangwon National University" in out or "Kangwon National University" in out
    assert "org: Instituto Federal do Piauí" in out
    assert "org: не найдено" in out
    assert "snippet: Исследователи RDA разработали программу" in out


def test_help_называет_параметр_dry_run() -> None:
    module = _load_module()
    parser = module.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0


# ── Живой прогон на подмене — точность печатается и ключ не палится ─────────


async def test_живой_прогон_печатает_точность_и_не_палит_ключ(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_provider_factory
) -> None:
    module = _load_module()
    # Идеальный судья: пара 3-4 фармы, вся группа 2-3-4-5 нефтегаза — по одному
    # ответу на набор (два вызова router.complete_json подряд).
    provider = fake_provider_factory(
        "deepseek",
        ['{"duplicate_groups": [[3, 4]]}', '{"duplicate_groups": [[2, 3, 4, 5]]}'],
    )

    from app.llm.router import LLMRouter as RealLLMRouter

    def _router_factory(settings=None, providers=None):
        return RealLLMRouter(settings, providers={"deepseek": provider})

    monkeypatch.setattr("app.llm.LLMRouter", _router_factory)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-leak-check-999")
    get_settings.cache_clear()

    args = module.build_parser().parse_args([])
    code = await module._main_async(args)
    get_settings.cache_clear()

    out = capsys.readouterr().out
    assert code == 0
    assert "ИТОГ" in out
    assert "точность по всем парам обоих наборов: 25/25 = 100.00%" in out
    assert "стоимость пробы" in out
    assert "sk-fake-leak-check-999" not in out, "фиктивный ключ не должен просочиться в вывод (инвариант №3)"


async def test_живой_прогон_отказ_судьи_не_роняет_пробу(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_provider_factory
) -> None:
    """Отказ на каждой попытке — проба всё равно печатает отчёт с честной
    (низкой) точностью, а не падает исключением."""
    module = _load_module()
    provider = fake_provider_factory("deepseek", fail_times=10)

    from app.llm.router import LLMRouter as RealLLMRouter

    def _router_factory(settings=None, providers=None):
        return RealLLMRouter(settings, providers={"deepseek": provider})

    monkeypatch.setattr("app.llm.LLMRouter", _router_factory)
    get_settings.cache_clear()

    args = module.build_parser().parse_args([])
    code = await module._main_async(args)
    get_settings.cache_clear()

    out = capsys.readouterr().out
    assert code == 0
    assert "судья отказал" in out


# ── Инварианты 3/6: слои, а не прямой httpx ──────────────────────────────────
def test_нет_прямого_httpx_и_есть_llmrouter() -> None:
    text = PROBE_PATH.read_text(encoding="utf-8")
    assert "import httpx" not in text
    assert "LLMRouter" in text
    assert "judge_duplicate_groups" in text
