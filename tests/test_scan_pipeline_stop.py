"""Экспресс-скан: стоп-кран внутри пайплайна показывается посетителю как остановка (аудит Э6, «Корректность» п. 1).

Пайплайн после интейка пишет в `requests.error` текст исключения («запрос израсходовал
… ₽»), и страница показывала общий отказ вместо `MSG_STOPPED`. Теперь остановку
распознаёт `scan_run` по типу исключения (`CostLimitExceededError`), а не по тексту.
Приложение, база и заглушки — те же, что в `test_scan_routes.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

import test_scan_routes as base
from app.llm import CostLimitExceededError
from app.models import Request as RequestRow
from app.models import RequestStatus
from app.schemas import Brief
from app.web import scan_run
from app.web.routes import scan as scan_routes

_scan_env = base._scan_env
db = base.db
client = base.client

#: Текст исключения стоп-крана, как его пишет `CostTracker.add`.
STOP_TEXT = "запрос израсходовал 10.40 ₽ при лимите 10.00 ₽"


class _StopPipeline(base._FakePipeline):
    """Пайплайн, остановленный стоп-краном на стадии после интейка: пишет `failed` с текстом исключения."""

    error: Exception = CostLimitExceededError(STOP_TEXT)

    async def run(self, brief: Brief, *, request_id: int = 0, **kwargs: Any):
        async with self._session_factory() as session:
            row = await session.get(RequestRow, request_id)
            row.status = RequestStatus.FAILED
            row.error = str(type(self).error)
            row.cost_rub = 10.4
        raise type(self).error


async def _failed_scan(client: AsyncClient, monkeypatch: pytest.MonkeyPatch, error: Exception) -> str:
    _StopPipeline.error = error
    monkeypatch.setattr(base.run, "ScoutPipeline", _StopPipeline)
    token = base._token(await base._post(client))
    await base._finish()
    return token


async def test_стоп_кран_после_интейка_страница_показывает_остановку(client, db, monkeypatch):
    token = await _failed_scan(client, monkeypatch, CostLimitExceededError(STOP_TEXT))

    html = (await client.get(f"/scan/{token}")).text
    assert scan_routes.MSG_STOPPED in html
    assert scan_routes.MSG_FAILED not in html
    assert STOP_TEXT not in html  # текст исключения до посетителя не доходит


async def test_стоп_кран_после_интейка_json_с_остановкой(client, db, monkeypatch):
    token = await _failed_scan(client, monkeypatch, CostLimitExceededError(STOP_TEXT))

    response = await client.get(f"/scan/{token}.json")
    assert response.status_code == 409
    assert response.json()["detail"] == {"error": "scan_failed", "message": scan_routes.MSG_STOPPED}


async def test_строка_остановленного_скана_хранит_метку_и_потраченное(client, db, monkeypatch):
    await _failed_scan(client, monkeypatch, CostLimitExceededError(STOP_TEXT))

    async with db() as session:
        row = await session.get(RequestRow, 1)
    assert row.status == RequestStatus.FAILED
    assert row.error == scan_run.SCAN_ERROR_STOP
    assert row.cost_rub == 10.4  # траты остаются в суточном потолке


async def test_другой_отказ_пайплайна_остаётся_общим_отказом(client, db, monkeypatch):
    token = await _failed_scan(client, monkeypatch, RuntimeError(STOP_TEXT))  # тот же текст, но не стоп-кран

    html = (await client.get(f"/scan/{token}")).text
    assert scan_routes.MSG_FAILED in html
    assert scan_routes.MSG_STOPPED not in html
    body = (await client.get(f"/scan/{token}.json")).json()
    assert body["detail"]["message"] == scan_routes.MSG_FAILED
