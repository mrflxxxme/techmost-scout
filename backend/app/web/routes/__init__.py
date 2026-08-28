"""Сборка пакета маршрутов веб-интерфейса.

`routes.py` раздроблен на подмодули по разделам этапа 3 (РЕШ-143 п. 5):
`pages.py` — уже готовые страницы, `intake.py` — интейк и бриф, `run.py` —
запуск прогона и живой прогресс, `report.py` — отчёт, `review.py` — экран
ревью эксперта (Э4-И3), `demo.py` — демо-режим (Э4-И7). Пути и имена
обработчиков не меняются, поэтому `from app.web.routes import router` в
`main.py` продолжает работать без правок.

Этот модуль ничего, кроме сборки роутера, не делает — логика экранов живёт в
подмодулях.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.web.routes.demo import router as demo_router
from app.web.routes.intake import router as intake_router
from app.web.routes.pages import router as pages_router
from app.web.routes.report import router as report_router
from app.web.routes.review import router as review_router
from app.web.routes.run import router as run_router

router = APIRouter()
router.include_router(pages_router)
router.include_router(intake_router)
router.include_router(run_router)
router.include_router(report_router)
router.include_router(review_router)
router.include_router(demo_router)

__all__ = ["router"]
