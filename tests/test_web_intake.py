"""Живой поток уточняющих вопросов и подтверждения брифа (задача И-10, SPEC Э3-2).

Модель подменяется заглушками на уровне `app.pipeline.stages.run_intake` и
`app.pipeline.stages.refine_brief` — тем же приёмом, что в
`tests/test_intake_flow.py`: маршрут вызывает эти функции косвенно, через
`app.pipeline.intake_flow`, который читает их отложенным импортом на момент
вызова. Сети нет (инвариант №7): `LLMRouter`, который создаёт маршрут, ни
разу не доходит до `complete_json`, потому что сама стадия подменена раньше.

Тексты задач в тестах намеренно не содержат маркеров измеримых требований
(«%», «TRL», «ГОСТ», «не менее/не более», числа с единицами) — иначе
`resolve_constraints_stage` завёл бы третий платный вызов
(`retry_constraints_if_missing`), который не подменён и не нужен по существу
проверки.

БД — общий процесс-движок `sqlite+aiosqlite:///:memory:` (см. `TEST_ENV` в
`tests/conftest.py`): `TestClient(main.app)` как контекстный менеджер
запускает `lifespan` (и, значит, `init_db`), а строки, записанные одним
HTTP-запросом, видны следующему — соединения делят один и тот же engine.
Номер запроса каждый тест читает из текста ответа (строка «Запрос №N» есть
и во фрагментах успеха, и во фрагментах отказа), а не предполагает его по
порядку — движок общий для всего тестового прогона, и порядковый номер
запроса от теста к тесту не гарантирован.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app import main
from app.db import session_scope
from app.llm import CostLimitExceededError
from app.models import Brief as BriefRow
from app.models import Request as RequestRow
from app.pipeline.intake_flow import INTAKE_FALLBACK_CAVEAT
from app.schemas import Brief, IntakeQuestion, IntakeResult

_TASK_TEXT = "Катализатор гидроочистки теряет активность быстрее ожидаемого, нужна альтернатива."


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """ASGI-клиент с basic auth — тот же приём, что в `test_web_templates.py`."""
    monkeypatch.setattr(main.settings, "web_auth_user", "scout")
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr("parol123"))
    with TestClient(main.app) as test_client:
        test_client.auth = ("scout", "parol123")
        yield test_client


def _draft(*, constraints: list[str] | None = None) -> Brief:
    return Brief(
        industry="нефтепереработка",
        task_summary=_TASK_TEXT,
        constraints=constraints or [],
    )


def _one_question_intake() -> IntakeResult:
    return IntakeResult(
        questions=[IntakeQuestion(question="Какая температура процесса?", why="нужна для отбора решений")],
        draft_brief=_draft(),
    )


def _request_id(response) -> int:
    """Номер запроса из фрагмента ответа — там он всегда написан «Запрос №N»."""
    return int(response.text.split("Запрос №", 1)[1].split(":", 1)[0].split(" ", 1)[0].split("<", 1)[0])


async def _fetch_row(request_id: int) -> RequestRow | None:
    async with session_scope() as session:
        return await session.get(RequestRow, request_id)


async def _fetch_brief_row(request_id: int) -> BriefRow | None:
    async with session_scope() as session:
        found = await session.execute(select(BriefRow).where(BriefRow.request_id == request_id))
        return found.scalars().one_or_none()


def _confirm_payload(request_id: int, **overrides) -> dict[str, str]:
    payload = {
        "confirm": "1",
        "industry": "нефтепереработка",
        "task_summary": _TASK_TEXT,
        "constraints": "",
        "geography": "РФ",
        "keywords_ru": "",
        "keywords_en": "",
    }
    payload.update(overrides)
    return payload


# ── POST /requests ───────────────────────────────────────────────────────


async def test_create_request_returns_questions_fragment_and_intake_status(monkeypatch, client):
    """Успешный разбор: строка `requests` со статусом `intake`, в ответе — вопрос."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return _one_question_intake()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    response = client.post("/requests", data={"raw_text": _TASK_TEXT})
    assert response.status_code == 200
    assert "Какая температура процесса?" in response.text
    assert "Собрать бриф" in response.text

    row = await _fetch_row(_request_id(response))
    assert row is not None
    assert row.status == "intake"
    assert row.raw_text == _TASK_TEXT


async def test_create_request_flags_deidentification_risk_without_calling_model(monkeypatch, client):
    """РЕШ-193 п. 2 (П-2 аудита Э3): текст с «ООО Ромашка» без `accept_risks`
    не вызывает модель вовсе — оператор видит предупреждение и кнопку
    «Отправить как есть», строка `requests` не заводится."""
    calls = {"run_intake": 0}

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        calls["run_intake"] += 1
        return _one_question_intake()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    response = client.post("/requests", data={"raw_text": "ООО Ромашка просит подобрать катализатор."})
    assert response.status_code == 200
    assert calls["run_intake"] == 0  # модель не вызвана
    assert "ООО Ромашка" in response.text  # фрагмент найденного риска показан оператору
    assert "Отправить как есть" in response.text  # кнопка повторного POST с accept_risks
    assert "<form" in response.text

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(RequestRow).where(RequestRow.raw_text == "ООО Ромашка просит подобрать катализатор.")
                )
            )
            .scalars()
            .all()
        )
    assert rows == []  # строка requests для отклонённой попытки не заведена


async def test_create_request_with_accept_risks_calls_model_and_proceeds(monkeypatch, client):
    """Кнопка «Отправить как есть» повторяет POST с `accept_risks` — на этот
    раз модель вызывается, интейк идёт как обычно."""
    calls = {"run_intake": 0}

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        calls["run_intake"] += 1
        return _one_question_intake()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    response = client.post(
        "/requests",
        data={"raw_text": "ООО Ромашка просит подобрать катализатор.", "accept_risks": "1"},
    )
    assert response.status_code == 200
    assert calls["run_intake"] == 1
    assert "Какая температура процесса?" in response.text
    assert "Собрать бриф" in response.text

    row = await _fetch_row(_request_id(response))
    assert row is not None
    assert row.status == "intake"


async def test_create_request_without_risk_goes_straight_to_intake(monkeypatch, client):
    """Текст без маркеров риска не показывает предупреждение и сразу идёт
    в интейк, как раньше."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return _one_question_intake()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    response = client.post("/requests", data={"raw_text": _TASK_TEXT})
    assert response.status_code == 200
    assert "Уточняющие вопросы" in response.text
    assert "Отправить как есть" not in response.text


async def test_answer_i_dont_know_is_accepted(monkeypatch, client):
    """Ответ «не знаю» проходит через уточнение брифа без ошибок."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return _one_question_intake()

    async def fake_refine_brief(router, task, answers, *, industry_hint="не указана"):
        assert answers == {"Какая температура процесса?": "не знаю"}
        return _draft()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fake_refine_brief)

    created = client.post("/requests", data={"raw_text": _TASK_TEXT})
    request_id = _request_id(created)

    response = client.post(
        f"/requests/{request_id}/brief",
        data={"question": "Какая температура процесса?", "answer": ""},
    )
    assert response.status_code == 200
    assert "Проверьте бриф" in response.text


# ── POST /requests/{id}/brief ────────────────────────────────────────────


async def test_brief_without_confirm_saves_draft_and_sets_briefing_status(monkeypatch, client):
    """Без `confirm`: строка `briefs` появляется, статус — `briefing`, без отметки подтверждения."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return _one_question_intake()

    async def fake_refine_brief(router, task, answers, *, industry_hint="не указана"):
        return _draft(constraints=["работа при повышенной температуре"])

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fake_refine_brief)

    created = client.post("/requests", data={"raw_text": _TASK_TEXT})
    request_id = _request_id(created)

    response = client.post(
        f"/requests/{request_id}/brief",
        data={"question": "Какая температура процесса?", "answer": "380 градусов"},
    )
    assert response.status_code == 200
    assert "Начать поиск" not in response.text  # без confirm кнопки старта быть не должно

    row = await _fetch_row(request_id)
    assert row.status == "briefing"

    brief_row = await _fetch_brief_row(request_id)
    assert brief_row is not None
    assert brief_row.confirmed_at is None
    assert brief_row.constraints == ["работа при повышенной температуре"]


async def test_brief_with_confirm_sets_confirmed_at_and_returns_start_button(monkeypatch, client):
    """С `confirm`: `confirmed_at` проставлен, в ответе — кнопка «Начать поиск»."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return _one_question_intake()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    created = client.post("/requests", data={"raw_text": _TASK_TEXT})
    request_id = _request_id(created)

    response = client.post(f"/requests/{request_id}/brief", data=_confirm_payload(request_id))
    assert response.status_code == 200
    assert "Начать поиск" in response.text
    assert f"/requests/{request_id}/start" in response.text

    brief_row = await _fetch_brief_row(request_id)
    assert brief_row is not None
    assert brief_row.confirmed_at is not None


# ── Отказ интейка и стоп-кран ────────────────────────────────────────────


async def test_run_intake_failure_falls_back_with_caveat(monkeypatch, client):
    """Отказ `run_intake`: сразу подтверждение с запасным брифом и оговоркой."""

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)

    response = client.post("/requests", data={"raw_text": _TASK_TEXT})
    assert response.status_code == 200
    assert INTAKE_FALLBACK_CAVEAT in response.text
    assert "Проверьте бриф" in response.text

    row = await _fetch_row(_request_id(response))
    assert row.status == "briefing"


async def test_cost_limit_exceeded_marks_request_failed(monkeypatch, client):
    """Стоп-кран: запрос помечается `failed`, ответ — человеческий текст, без исключения."""

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        raise CostLimitExceededError("запрос израсходовал 10.00 ₽ при лимите 10.00 ₽")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)

    response = client.post("/requests", data={"raw_text": _TASK_TEXT})
    assert response.status_code == 200
    assert "лимит" in response.text.lower()
    assert "Запрос Запрос" not in response.text  # префикс "Запрос №N: " не дублирует слово

    row = await _fetch_row(_request_id(response))
    assert row.status == "failed"
    assert row.error


async def test_cost_limit_exceeded_keeps_spent_amount_in_cost_rub(monkeypatch, client):
    """Деньги неудавшегося интейка не пропадают без следа (П-5, ревью цикла 1):
    `CostTracker.add` прибавляет расход к счётчику раньше, чем поднимает
    исключение (`app/llm/router.py`), — потраченная сумма обязана попасть
    в `requests.cost_rub`, а не остаться нулём."""
    spent = 15.0

    async def broken_run_intake(router, task, *, industry_hint="не указана"):
        # Тот же порядок, что у настоящего CostTracker.add: расход учтён
        # раньше, чем поднято исключение.
        router.cost.spent_rub += spent
        router.cost.by_provider["deepseek"] = router.cost.by_provider.get("deepseek", 0.0) + spent
        router.cost.calls += 1
        raise CostLimitExceededError(f"запрос израсходовал {spent:.2f} ₽ при лимите 10.00 ₽")

    monkeypatch.setattr("app.pipeline.stages.run_intake", broken_run_intake)

    response = client.post("/requests", data={"raw_text": _TASK_TEXT})
    assert response.status_code == 200

    row = await _fetch_row(_request_id(response))
    assert row.status == "failed"
    assert row.cost_rub == pytest.approx(spent)


# ── Единый счёт трат на запрос (РЕШ-193 п. 1, П-1 аудита Э3) ─────────────


async def test_brief_phase_router_seeds_spent_from_existing_request_cost(monkeypatch, client):
    """Роутер фазы `POST /brief` стартует не с нуля, а с уже потраченного по
    строке `requests` (интейк): при `requests.cost_rub=19.9` (лимит в тестовом
    окружении — 10 ₽) первый же вызов модели на этой фазе останавливает её
    стоп-краном, а не даёт фазе свой отдельный лимит."""
    from app.llm.base import Usage

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return _one_question_intake()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    created = client.post("/requests", data={"raw_text": _TASK_TEXT})
    request_id = _request_id(created)

    async with session_scope() as session:
        row = await session.get(RequestRow, request_id)
        row.cost_rub = 19.9

    async def fake_refine_brief(router, task, answers, *, industry_hint="не указана"):
        # Роутер этой фазы обязан стартовать уже с 19,9 ₽ (РЕШ-193 п. 1), а
        # не с нуля — иначе стоп-кран на веб-пути действовал бы на фазу.
        assert router.cost.spent_rub == pytest.approx(19.9)
        router.cost.add(Usage(tokens_in=10, tokens_out=10, cost_rub=0.01, provider="deepseek", model="fake"))

    monkeypatch.setattr("app.pipeline.stages.refine_brief", fake_refine_brief)

    response = client.post(
        f"/requests/{request_id}/brief",
        data={"question": "Какая температура процесса?", "answer": "380 градусов"},
    )
    assert response.status_code == 200
    assert "лимит" in response.text.lower()

    row_after = await _fetch_row(request_id)
    assert row_after.status == "failed"
    assert row_after.cost_rub == pytest.approx(19.91)


# ── TRL ───────────────────────────────────────────────────────────────────


async def test_target_trl_is_added_to_constraints_string(monkeypatch, client):
    """TRL из формы виден строкой в `constraints` сохранённого брифа."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return _one_question_intake()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    created = client.post("/requests", data={"raw_text": _TASK_TEXT})
    request_id = _request_id(created)

    response = client.post(f"/requests/{request_id}/brief", data=_confirm_payload(request_id, target_trl="6"))
    assert response.status_code == 200
    assert "целевой TRL 6" in response.text

    brief_row = await _fetch_brief_row(request_id)
    assert "целевой TRL 6" in brief_row.constraints


async def test_invalid_trl_is_rejected_without_polluting_constraints(monkeypatch, client):
    """Мусорный TRL («abc», вне 1–9) не уезжает в `constraints`, бриф не
    подтверждается (оператор остаётся на редактируемой форме — ревью
    цикла 1), а введённое значение возвращается в поле для исправления."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return _one_question_intake()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)

    created = client.post("/requests", data={"raw_text": _TASK_TEXT})
    request_id = _request_id(created)

    response = client.post(f"/requests/{request_id}/brief", data=_confirm_payload(request_id, target_trl="abc"))
    assert response.status_code == 200
    assert "некорректно" in response.text
    assert "Начать поиск" not in response.text  # не подтверждено — TRL нужно поправить
    assert 'value="abc"' in response.text  # введённое значение вернулось в поле

    brief_row = await _fetch_brief_row(request_id)
    assert brief_row.constraints == []
    assert brief_row.confirmed_at is None

    out_of_range = client.post(f"/requests/{request_id}/brief", data=_confirm_payload(request_id, target_trl="999"))
    assert "некорректно" in out_of_range.text
    assert "Начать поиск" not in out_of_range.text
    assert 'value="999"' in out_of_range.text
    brief_row_again = await _fetch_brief_row(request_id)
    assert brief_row_again.constraints == []
    assert brief_row_again.confirmed_at is None

    # Оператор поправил значение и подтвердил снова — на этот раз штатно.
    fixed = client.post(f"/requests/{request_id}/brief", data=_confirm_payload(request_id, target_trl="6"))
    assert "Начать поиск" in fixed.text
    brief_row_fixed = await _fetch_brief_row(request_id)
    assert brief_row_fixed.confirmed_at is not None
    assert "целевой TRL 6" in brief_row_fixed.constraints


# ── Залипший confirmed_at (ревью волны 1) ────────────────────────────────


async def test_stale_confirmation_does_not_leak_start_button(monkeypatch, client):
    """После повторного захода в ветку без `confirm` кнопки старта в ответе нет,
    даже если бриф уже был подтверждён раньше (`confirmed_at` не сбрасывается —
    поведение `brief_store.save_brief`, задокументировано в И-9)."""

    async def fake_run_intake(router, task, *, industry_hint="не указана"):
        return _one_question_intake()

    async def fake_refine_brief(router, task, answers, *, industry_hint="не указана"):
        return _draft()

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fake_refine_brief)

    created = client.post("/requests", data={"raw_text": _TASK_TEXT})
    request_id = _request_id(created)

    confirmed = client.post(f"/requests/{request_id}/brief", data=_confirm_payload(request_id))
    assert "Начать поиск" in confirmed.text

    brief_row = await _fetch_brief_row(request_id)
    stale_confirmed_at = brief_row.confirmed_at
    assert stale_confirmed_at is not None

    # Оператор вернулся к форме ответов (например, кнопкой «Назад») и снова
    # отправил её без confirm — старая отметка подтверждения в БД остаётся,
    # но ответ обязан снова показать редактируемую форму, а не кнопку старта.
    again = client.post(
        f"/requests/{request_id}/brief",
        data={"question": "Какая температура процесса?", "answer": "не знаю"},
    )
    assert again.status_code == 200
    assert "Начать поиск" not in again.text
    assert "Проверьте бриф" in again.text

    brief_row_after = await _fetch_brief_row(request_id)
    assert brief_row_after.confirmed_at == stale_confirmed_at  # save_brief(confirmed=False) не сбрасывает отметку
