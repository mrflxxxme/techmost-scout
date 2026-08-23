"""Бухгалтерия прогона (РЕШ-151, дробление `orchestrator.py`): прирост
стоимости стадии по снимкам счётчика роутера и строка активных параметров.

Обе функции читает не только оркестратор: `cli.py` и `constraints_guard.py`
зовут `cost_delta` отложенным импортом из `app.pipeline.orchestrator` (а не
отсюда напрямую) — фасад остаётся единственной точкой контракта для внешнего
кода, реальное расположение функции ему знать не нужно.
"""

from __future__ import annotations

from app.config import Settings


def cost_delta(before: dict[str, float], after: dict[str, float]) -> tuple[float, str | None]:
    """Прирост стоимости и провайдер(ы), которые её потратили, между двумя
    снимками `LLMRouter.cost.by_provider`.

    Роутер считает расход по запросу целиком, не по стадиям, поэтому для
    журнала стадий (Ф-8) берём разницу снимков вокруг вызова стадии —
    точность до провайдера, не до токена.

    Функция публичная, потому что тем же снимком пользуется `cli._run`: стадия
    интейка живёт до пайплайна, а её строка в журнале обязана нести
    фактическую стоимость, а не ноль (П-4 аудита Т-2).
    """
    changed: list[str] = []
    total = 0.0
    for name, value in after.items():
        diff = value - before.get(name, 0.0)
        if diff > 1e-9:
            total += diff
            changed.append(name)
    provider = ",".join(sorted(changed)) if changed else None
    return round(total, 6), provider


def format_active_run_params(settings: Settings) -> str:
    """Строка активных параметров прогона (З-7): стоп-кран, оба порога отбора,
    маршрут каждой стадии, порядок резерва, имена моделей.

    Печатается ДО первой платной стадии — окружение между подготовкой и
    прогоном способно измениться незаметно (Б-2 аудита Э2-Г: `extractor` ушёл
    на `deepseek`, а модель оказалась не той, что боевая, и это вскрылось
    только аудитом задним числом). Значения секретов сюда не попадают: из
    настроек берутся только величины, у которых нет тайной составляющей, —
    ключи в строку не подставляются вовсе, ни значением, ни фрагментом.
    """
    routes = ", ".join(f"{stage}:{provider}" for stage, provider in settings.stage_routes().items())
    fallback = "→".join(settings.fallback_order())
    return (
        f"Параметры прогона: стоп-кран {settings.cost_limit_rub_per_request:.2f} ₽/запрос; "
        f"relevance_min={settings.relevance_min}; "
        f"second_pass_relevance_max={settings.second_pass_relevance_max}; "
        f"маршруты стадий: {routes}; "
        f"порядок резерва: {fallback}; "
        f"модели: deepseek={settings.deepseek_model}, yandex={settings.yandex_model}"
    )
