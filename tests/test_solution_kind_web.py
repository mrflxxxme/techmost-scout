"""Вид решения (Э6-1) на веб-пути: форма -> интейк -> черновик -> подтверждение.

Слои ниже веба закрыты `tests/test_solution_kind.py`; здесь — проводка на
`TestClient`, которую без этих тестов можно молча сломать (ревью цикла 1, §5):
- `routes/intake.py::create_request` передаёт `solution_kind` в `run_intake_stage`;
- `routes/intake.py::_confirm_brief` берёт вид из сохранённого черновика, иначе
  `brief_from_form` без поля вернул бы «Науку»;
- `_deidentification_warning.html` несёт скрытое поле `solution_kind`, иначе
  выбор теряется при «Отправить как есть».

Заглушки и база — как в `tests/test_web_intake.py`: подменяется
`app.pipeline.stages.run_intake`, сети нет (инвариант №7).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app import main
from app.db import session_scope
from app.models import Brief as BriefRow
from app.schemas import Brief, IntakeQuestion, IntakeResult
from app.web.brief_store import load_brief

_TASK_TEXT = "Катализатор гидроочистки теряет активность быстрее ожидаемого, нужна альтернатива."
_RISKY_TEXT = "ООО Ромашка просит подобрать катализатор гидроочистки."

_HIDDEN_INPUT = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)"')


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """ASGI-клиент с basic auth — тот же приём, что в `test_web_intake.py`."""
    monkeypatch.setattr(main.settings, "web_auth_user", "scout")
    monkeypatch.setattr(main.settings, "web_auth_password", SecretStr("parol123"))
    with TestClient(main.app) as test_client:
        test_client.auth = ("scout", "parol123")
        yield test_client


@pytest.fixture
def intake_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Заглушка `run_intake`: запоминает вид, пришедший в интейк, и кладёт его в черновик.

    Без аргумента `solution_kind` (путь потерян) запоминается «research» — как
    умолчание настоящего `run_intake`, поэтому потеря вида видна в утверждениях.
    """
    calls: list[dict[str, Any]] = []

    async def fake_run_intake(router, task, *, industry_hint="не указана", solution_kind="research"):
        calls.append({"task": task, "solution_kind": solution_kind})
        return IntakeResult(
            questions=[],
            draft_brief=Brief(industry="нефтепереработка", task_summary=task, solution_kind=solution_kind),
        )

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    return calls


def _request_id(response) -> int:
    """Номер запроса из фрагмента ответа — там он всегда написан «Запрос №N»."""
    return int(response.text.split("Запрос №", 1)[1].split(":", 1)[0].split(" ", 1)[0].split("<", 1)[0])


async def _fetch_brief_row(request_id: int) -> BriefRow | None:
    async with session_scope() as session:
        found = await session.execute(select(BriefRow).where(BriefRow.request_id == request_id))
        return found.scalars().one_or_none()


async def _loaded_brief(request_id: int) -> Brief | None:
    async with session_scope() as session:
        return await load_brief(session, request_id)


def _confirm_payload() -> dict[str, str]:
    """Форма подтверждения: поля вида решения в ней нет — он берётся из черновика."""
    return {
        "confirm": "1",
        "industry": "нефтепереработка",
        "task_summary": _TASK_TEXT,
        "constraints": "",
        "geography": "РФ",
        "keywords_ru": "",
        "keywords_en": "",
    }


# ── (а) POST /requests ────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["digital", "product", "business_model"])
async def test_create_request_passes_solution_kind_to_intake_and_draft(client, intake_calls, kind):
    """Выбор на форме доходит до интейка и ложится в черновик брифа (колонка `briefs.solution_kind`)."""
    response = client.post("/requests", data={"raw_text": _TASK_TEXT, "solution_kind": kind})
    assert response.status_code == 200

    assert [call["solution_kind"] for call in intake_calls] == [kind]
    row = await _fetch_brief_row(_request_id(response))
    assert row is not None
    assert row.solution_kind == kind


async def test_create_request_without_solution_kind_means_research(client, intake_calls):
    """Форма без поля (старый клиент) — «Наука», как и раньше."""
    response = client.post("/requests", data={"raw_text": _TASK_TEXT})
    assert response.status_code == 200

    assert [call["solution_kind"] for call in intake_calls] == ["research"]
    row = await _fetch_brief_row(_request_id(response))
    assert row is not None
    assert row.solution_kind == "research"


# ── (б) подтверждение брифа ───────────────────────────────────────────────────


async def test_confirm_brief_keeps_solution_kind_of_saved_draft(client, intake_calls):
    """Подтверждение не сбрасывает вид в `research`: `brief_from_form` без поля дал бы «Науку»."""
    created = client.post("/requests", data={"raw_text": _TASK_TEXT, "solution_kind": "digital"})
    request_id = _request_id(created)
    assert (await _loaded_brief(request_id)).solution_kind == "digital"

    confirmed = client.post(f"/requests/{request_id}/brief", data=_confirm_payload())
    assert confirmed.status_code == 200
    assert "Начать поиск" in confirmed.text

    row = await _fetch_brief_row(request_id)
    assert row is not None
    assert row.confirmed_at is not None
    assert row.solution_kind == "digital"
    assert (await _loaded_brief(request_id)).solution_kind == "digital"


# ── (в) предупреждение об обезличивании ───────────────────────────────────────


async def test_deidentification_warning_carries_hidden_solution_kind_and_resend_keeps_it(client, intake_calls):
    """Страница предупреждения несёт скрытое поле с выбранным видом; отправка «как есть» с ним
    (так её соберёт браузер) доводит вид до интейка."""
    warning = client.post("/requests", data={"raw_text": _RISKY_TEXT, "solution_kind": "digital"})
    assert warning.status_code == 200
    assert "Отправить как есть" in warning.text
    assert intake_calls == []  # модель до подтверждения не вызвана

    hidden = dict(_HIDDEN_INPUT.findall(warning.text))
    assert hidden.get("solution_kind") == "digital"
    assert hidden.get("accept_risks") == "1"

    resent = client.post("/requests", data=hidden)
    assert resent.status_code == 200
    assert [call["solution_kind"] for call in intake_calls] == ["digital"]
    row = await _fetch_brief_row(_request_id(resent))
    assert row is not None
    assert row.solution_kind == "digital"


# ── (г) ответы на вопросы интейка ─────────────────────────────────────────────


async def test_answers_step_passes_solution_kind_to_refine_brief_and_keeps_it(client, monkeypatch):
    """Шаг «ответы на вопросы» (без `confirm`) отдаёт `refine_brief` вид из черновика.

    Без этого `refine_brief` получил бы умолчание «research» и выбор человека молча
    стал бы «Наукой» (ревью цикла 2, мутация М4)."""
    refine_calls: list[str] = []

    async def fake_run_intake(router, task, *, industry_hint="не указана", solution_kind="research"):
        draft = Brief(industry="нефтепереработка", task_summary=task, solution_kind=solution_kind)
        return IntakeResult(questions=[IntakeQuestion(question="Какая нагрузка?", why="для отбора")], draft_brief=draft)

    async def fake_refine_brief(router, task, answers, *, industry_hint="не указана", solution_kind="research"):
        refine_calls.append(solution_kind)
        return Brief(industry="нефтепереработка", task_summary=task, solution_kind=solution_kind)

    monkeypatch.setattr("app.pipeline.stages.run_intake", fake_run_intake)
    monkeypatch.setattr("app.pipeline.stages.refine_brief", fake_refine_brief)

    created = client.post("/requests", data={"raw_text": _TASK_TEXT, "solution_kind": "digital"})
    request_id = int(re.search(r'hx-post="/requests/(\d+)/brief"', created.text).group(1))

    step = client.post(f"/requests/{request_id}/brief", data={"question": "Какая нагрузка?", "answer": "100 RPS"})
    assert step.status_code == 200

    assert refine_calls == ["digital"]
    assert (await _loaded_brief(request_id)).solution_kind == "digital"
