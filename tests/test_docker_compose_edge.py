"""Страж надстройки `docker-compose.edge.yml` — запуск на общем сервере.

На общем сервере 80/443 держит чужой edge-прокси, поэтому надстройка снимает
свой `caddy`, заводит приложение во внешнюю сеть под известным псевдонимом и
не публикует наружу ни одного порта. Каждое из этих четырёх свойств ломается
молча: стек поднимется, а работать не будет — либо отнимет порт у соседей,
либо окажется недоступен edge-прокси по имени. Отсюда проверки ниже.

Файл читается `yaml.safe_load` (инвариант №10), демон Docker и сеть не нужны
(инвариант №7).
"""

from __future__ import annotations

from pathlib import Path

import yaml

BASE_PATH = Path(__file__).resolve().parents[1] / "docker" / "docker-compose.yml"
EDGE_PATH = Path(__file__).resolve().parents[1] / "docker" / "docker-compose.edge.yml"

#: Имя, под которым приложение видно edge-прокси во внешней сети. По нему
#: написан блок сайта в чужом Caddyfile — расхождение ломает маршрутизацию.
EDGE_ALIAS = "techmost-scout"

#: Имя внешней сети, через которую edge-прокси доходит до приложения.
EDGE_NETWORK = "edge"


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _edge() -> dict:
    return _load(EDGE_PATH)


def test_надстройка_разбирается_и_правит_только_известные_сервисы() -> None:
    """Надстройка не заводит сервисов, которых нет в основной связке."""
    base_services = set(_load(BASE_PATH)["services"])
    edge_services = set(_edge()["services"])
    assert edge_services, "надстройка обязана что-то менять"
    assert edge_services <= base_services, (
        f"надстройка правит сервис вне основной связки: {sorted(edge_services - base_services)}"
    )


def test_свой_caddy_уходит_в_профиль_и_не_стартует() -> None:
    """Без профиля `own-proxy` свой прокси не поднимается.

    Иначе он займёт 80 и 443 — порты чужого edge-прокси, обслуживающего
    соседние сайты на том же сервере.
    """
    profiles = _edge()["services"]["caddy"].get("profiles")
    assert profiles, "у caddy в надстройке обязан быть профиль, иначе он стартует вместе со всеми"
    assert "own-proxy" in profiles, f"ожидался профиль own-proxy, получено {profiles}"


def test_приложение_входит_во_внешнюю_сеть_под_известным_псевдонимом() -> None:
    """Псевдоним — то имя, по которому приложение находит чужой прокси."""
    networks = _edge()["services"]["scout"]["networks"]
    assert EDGE_NETWORK in networks, f"приложение обязано входить в сеть {EDGE_NETWORK}"
    aliases = networks[EDGE_NETWORK].get("aliases") or []
    assert EDGE_ALIAS in aliases, f"ожидался псевдоним {EDGE_ALIAS}, получено {aliases}"


def test_приложение_не_теряет_сеть_по_умолчанию() -> None:
    """Ловушка compose: объявив сети у сервиса, он теряет `default`.

    Без `default` приложение перестанет видеть сайдкар копии базы и наоборот —
    при этом связка поднимется и ошибки не покажет.
    """
    networks = _edge()["services"]["scout"]["networks"]
    assert "default" in networks, "у приложения обязана остаться сеть default, иначе оно теряет соседей по стеку"


def test_внешняя_сеть_объявлена_внешней() -> None:
    """Сеть создаёт edge-прокси, стек к ней только присоединяется."""
    network = _edge()["networks"][EDGE_NETWORK]
    assert network.get("external") is True, "сеть edge обязана быть external: её создаёт чужой прокси, а не этот стек"


def test_надстройка_не_публикует_ни_одного_порта() -> None:
    """Наружу на общем сервере смотрит только чужой edge-прокси."""
    for name, service in _edge()["services"].items():
        assert "ports" not in service, f"сервис {name} публикует порты, а на общем сервере это запрещено"


def test_лимиты_заданы_обоим_работающим_сервисам() -> None:
    """Печать PDF через Chromium — самый прожорливый момент, и соседи по
    серверу не должны это чувствовать. Проверяются оба сервиса, которые на
    общем сервере реально работают: приложение и сайдкар копии базы."""
    services = _edge()["services"]
    for name in ("scout", "backup"):
        limits = services[name].get("deploy", {}).get("resources", {}).get("limits", {})
        assert limits.get("cpus"), f"у сервиса {name} не задан предел CPU"
        assert limits.get("memory"), f"у сервиса {name} не задан предел памяти"


def test_инструкция_деплоя_называет_ту_же_надстройку_и_тот_же_псевдоним() -> None:
    """Документ и связка расходятся молча, поэтому сверяются машинно."""
    deploy = (Path(__file__).resolve().parents[1] / "docs" / "deploy.md").read_text(encoding="utf-8")
    assert EDGE_PATH.name in deploy, f"инструкция деплоя обязана называть {EDGE_PATH.name}"
    assert EDGE_ALIAS in deploy, f"инструкция деплоя обязана называть псевдоним {EDGE_ALIAS}"
