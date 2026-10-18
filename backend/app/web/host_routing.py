"""Роутинг по заголовку Host (Э6-6а): публичный экспресс-скан и рабочий интерфейс.

Одно приложение обслуживает два хоста. Рабочий (основатели, эксперт) отдаёт
прежние пути и не знает `/scan*`. Публичный (`PUBLIC_SCAN_HOST`) отдаёт только
белый список путей; всё остальное — 404, причём **до** проверки пароля:
иначе чужой получил бы 401 с `WWW-Authenticate` и узнал, что за адресом есть
рабочий интерфейс. Прослойка чистая ASGI: ничего не читает из тела и не знает
о маршрутах приложения.
"""

from __future__ import annotations

from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

#: Страница публичного хоста, на которую уходит корень.
SCAN_PATH = "/scan"
#: Пути, открытые на публичном хосте: точное совпадение или префикс с `/`.
PUBLIC_EXACT_PATHS = frozenset({"/health"})
PUBLIC_PREFIXES = (SCAN_PATH, "/static", "/api/v1")


def normalize_host(raw: str) -> str:
    """Хост без порта и точки на конце, в нижнем регистре; пустая строка — хоста нет."""
    host = raw.strip().lower()
    if host.startswith("["):  # IPv6 в скобках: `[::1]:8000`
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def _under(path: str, prefix: str) -> bool:
    """Путь равен префиксу или лежит под ним (`/scan` и `/scan/x`, но не `/scanner`)."""
    return path == prefix or path.startswith(prefix + "/")


def is_scan_path(path: str) -> bool:
    """Относится ли путь к экспресс-скану (`/scan`, `/scan/{token}`, `/scan/{token}.json`)."""
    return _under(path, SCAN_PATH)


def is_public_path(path: str) -> bool:
    """Открыт ли путь на публичном хосте."""
    return path in PUBLIC_EXACT_PATHS or any(_under(path, prefix) for prefix in PUBLIC_PREFIXES)


def _request_host(scope: Scope) -> str:
    for name, value in scope.get("headers", ()):
        if name == b"host":
            return normalize_host(value.decode("latin-1"))
    return ""


class HostRoutingMiddleware:
    """Белые списки путей по хосту. `public_scan_host` пуст — публичного режима нет."""

    def __init__(self, app: ASGIApp, *, public_scan_host: str = "") -> None:
        self.app = app
        self.public_host = normalize_host(public_scan_host)

    def decision(self, host: str, path: str) -> Response | None:
        """Ответ прослойки на запрос или `None` — пропустить дальше в приложение."""
        if self.public_host and host == self.public_host:
            if path == "/":
                return RedirectResponse(SCAN_PATH, status_code=302)
            if not is_public_path(path):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            return None
        if is_scan_path(path):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        response = self.decision(_request_host(scope), scope["path"])
        if response is None:
            await self.app(scope, receive, send)
        elif scope["type"] == "http":
            await response(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1008})
