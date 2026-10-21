"""Автосверка фактов с текстами источников (Э6-6б, РЕШ-252).

Строки кандидатов — простые заглушки с теми же полями, что у `models.Candidate`:
сеть и база не нужны (инвариант №7). Проверяется каждое условие сверки отдельно:
ссылка среди кандидатов, числа утверждения в тексте источника, все факты карточки,
пустой `unsourced_claims`, числа описания, подтверждение страны у аналога.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.pipeline.autoverify import (
    analog_verified,
    card_verified,
    fact_verified,
    numbers_in,
    source_texts,
)
from app.schemas import AnalogCard, Evidence, SolutionCard
from app.search.base import normalize_url

URL_A = "https://www.example.org/a/"
URL_B = "https://example.org/b"
URL_C = "https://example.org/c"
URL_OTHER = "https://stranger.example.org/x"


@dataclass
class Row:
    """Те же поля, что у строки `candidates`, которые читает `source_texts`."""

    url: str
    title: str = ""
    snippet: str = ""
    org: str | None = None
    year: int | None = None


def _texts() -> dict[str, str]:
    return source_texts(
        [
            Row(URL_A, title="Мембрана", snippet="Снижение затрат на 35 % при пилоте", org="Институт Ж", year=2021),
            Row(URL_B, title="Фильтр", snippet="Ресурс 12 000 часов, КПД 94,5 процента"),
            Row(URL_C, title="Обзор", snippet="Без чисел"),
        ]
    )


def _card(**changes) -> SolutionCard:
    data = {
        "title": "Мембрана",
        "essence": "Мембрана снижает затраты",
        "applicability": "Подходит для задачи",
        "next_step": "Запросить пилот",
        "evidence": [Evidence(claim="Затраты ниже на 35%", url=URL_A)],
    }
    data.update(changes)
    return SolutionCard(**data)


def _analog(**changes) -> AnalogCard:
    data = {
        "title": "Аналог",
        "essence": "Сервис подписки",
        "country": "US",
        "country_evidence_url": URL_C,
        "business_model": "Подписка",
        "traction": "",
        "transferability_ru": "Перенос возможен",
        "what_to_do_in_ru": "Изучить",
        "evidence": [Evidence(claim="Работает с 2021 года", url=URL_A)],
    }
    data.update(changes)
    return AnalogCard(**data)


def test_source_texts_ключ_нормализован_а_текст_собран_из_полей():
    texts = _texts()
    assert set(texts) == {normalize_url(URL_A), normalize_url(URL_B), normalize_url(URL_C)}
    text = texts[normalize_url(URL_A)]
    assert "Мембрана" in text
    assert "35 %" in text
    assert "Институт Ж" in text
    assert "2021" in text


def test_source_texts_дубль_ссылки_склеивает_тексты():
    texts = source_texts([Row(URL_A, snippet="первое 10"), Row("https://example.org/a", snippet="второе 20")])
    assert len(texts) == 1
    assert numbers_in(next(iter(texts.values()))) == {"10", "20"}


def test_numbers_in_нормализует_пробелы_запятую_и_разряды():
    assert numbers_in("35%") == numbers_in("35 %") == {"35"}
    assert numbers_in("94,5") == numbers_in("94.5") == {"94.5"}
    assert numbers_in("12 000") == numbers_in("12 000") == numbers_in("12000") == {"12000"}
    assert numbers_in("в 2020 100 штук") == {"2020", "100"}
    assert numbers_in("без чисел") == frozenset()


def test_факт_со_ссылкой_не_из_кандидатов_не_сверен():
    assert not fact_verified(Evidence(claim="Без чисел", url=URL_OTHER), _texts())


def test_факт_с_числом_которого_нет_в_сниппете_не_сверен():
    # 35 % есть у A, но не у B: число утверждения ищется в тексте именно его ссылки.
    assert not fact_verified(Evidence(claim="Затраты ниже на 35 %", url=URL_B), _texts())


def test_факт_35_без_пробела_при_35_с_пробелом_сверен():
    assert fact_verified(Evidence(claim="Затраты ниже на 35%", url=URL_A), _texts())


def test_факт_запятая_и_разряды_сверены():
    assert fact_verified(Evidence(claim="КПД 94.5 %, ресурс 12000 ч", url=URL_B), _texts())


def test_факт_без_чисел_сверен_по_одной_ссылке():
    assert fact_verified(Evidence(claim="Описано в обзоре", url="https://EXAMPLE.org/c/"), _texts())


def test_число_внутри_другого_числа_не_считается():
    assert not fact_verified(Evidence(claim="Всего 3 установки", url=URL_B), _texts())
    assert not fact_verified(Evidence(claim="Ресурс 12 часов", url=URL_B), _texts())


def test_карточка_со_сверенными_фактами_сверена():
    card = _card(evidence=[Evidence(claim="Затраты ниже на 35%", url=URL_A), Evidence(claim="КПД 94,5 %", url=URL_B)])
    assert card_verified(card, _texts())


def test_один_несверенный_факт_из_трёх_губит_карточку():
    evidence = [
        Evidence(claim="Затраты ниже на 35%", url=URL_A),
        Evidence(claim="КПД 94,5 %", url=URL_B),
        Evidence(claim="Ресурс 99 часов", url=URL_B),
    ]
    assert not card_verified(_card(evidence=evidence), _texts())


def test_непустой_unsourced_claims_карточка_не_сверена():
    assert not card_verified(_card(unsourced_claims=["Срок службы не подтверждён"]), _texts())


def test_карточка_без_evidence_не_сверена():
    assert not card_verified(_card(evidence=[]), _texts())


def test_число_в_essence_без_подтверждения_источником_не_сверена():
    assert not card_verified(_card(essence="Экономия 70 % затрат"), _texts())


def test_число_в_applicability_без_подтверждения_источником_не_сверена():
    assert not card_verified(_card(applicability="Окупается за 14 месяцев"), _texts())


def test_число_описания_есть_в_тексте_источника_сверена():
    assert card_verified(_card(essence="Экономия 35 % затрат"), _texts())


def test_число_описания_из_кандидата_без_ссылки_на_него_не_подтверждение():
    # 94,5 есть у B, но карточка сослалась только на A.
    assert not card_verified(_card(essence="КПД 94,5 процента"), _texts())


def test_аналог_сверен_при_странe_и_фактах_из_кандидатов():
    assert analog_verified(_analog(), _texts())


def test_аналог_со_страной_не_из_кандидатов_не_сверен():
    assert not analog_verified(_analog(country_evidence_url=URL_OTHER), _texts())


def test_аналог_без_evidence_не_сверен():
    assert not analog_verified(_analog(evidence=[]), _texts())


def test_аналог_с_несверенным_фактом_не_сверен():
    bad = Evidence(claim="Выручка 500 млн", url=URL_A)
    assert not analog_verified(_analog(evidence=[Evidence(claim="Работает с 2021 года", url=URL_A), bad]), _texts())


def test_аналог_с_числом_в_traction_и_transferability_без_подтверждения_не_сверен():
    assert not analog_verified(_analog(traction="40 000 клиентов"), _texts())
    assert not analog_verified(_analog(transferability_ru="Нужно 6 месяцев"), _texts())
    assert analog_verified(_analog(traction="Сервис работает с 2021 года"), _texts())
