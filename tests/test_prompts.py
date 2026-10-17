"""Проверки промптов как контракта.

Эти тесты появились после настоящей аварии: промпты содержат метки вида
{{BRIEF_JSON}}, код их не заполнял, и модель получала метку текстом вместо
данных. Внешне всё работало — карточки собирались, но проверяющая стадия не
видела источников и занижала уверенность каждой карточке.

Поэтому здесь проверяется не поведение модели, а то, что данные вообще до неё
доходят: ни одна метка не должна остаться незаполненной.
"""

from __future__ import annotations

import re

import pytest

from app.config import get_settings
from app.pipeline.stages import candidates_to_json, load_prompt, render_prompt
from app.schemas import ScoredCandidate

PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")

#: Какие метки обязан заполнять код для каждого промпта.
EXPECTED_PLACEHOLDERS = {
    "intake": {"RAW_TASK_TEXT", "INDUSTRY_HINT", "SOLUTION_KIND"},
    "planner": {"BRIEF_JSON"},
    # Т-21: стадия второго прохода. Прежние формулировки и первая выдача едут
    # отдельными метками — модель обязана видеть, что уже спрашивали и что из
    # этого вышло, иначе «уточнённый» план получается пересказом прежнего.
    "replanner": {"BRIEF_JSON", "PREVIOUS_QUERIES", "FIRST_PASS_RESULTS"},
    "card_author": {"BRIEF_JSON", "GROUP_CANDIDATES_JSON", "GOV_SUPPORT_JSON"},
    "verifier": {"CARD_JSON", "GROUP_CANDIDATES_JSON"},
    "summary": {"BRIEF_JSON", "CARDS_JSON"},
    "extractor": {"SOURCE_TYPE", "PROVIDER", "RAW_RESULTS"},
    "scorer": set(),
    # Д-6 волны Э2-З (РЕШ-152): семантический судья дублей — список отобранных
    # кандидатов номером, заголовком, организацией и фрагментом одной меткой.
    "dedup_judge": {"CANDIDATES_LISTING"},
}


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS))
def test_prompt_exists_and_declares_expected_placeholders(name: str):
    """Набор меток в промпте не должен разойтись с тем, что передаёт код."""
    text = load_prompt(name)
    found = set(PLACEHOLDER.findall(text))
    assert found == EXPECTED_PLACEHOLDERS[name], (
        f"промпт {name}: метки в файле {sorted(found)} не совпадают с ожидаемыми "
        f"{sorted(EXPECTED_PLACEHOLDERS[name])}. Обновите и промпт, и вызов render_prompt"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS))
def test_render_prompt_leaves_no_placeholders(name: str):
    """Заполненный промпт не содержит меток: модель не должна видеть {{...}}."""
    values = dict.fromkeys(EXPECTED_PLACEHOLDERS[name], "подставленное значение")
    rendered = render_prompt(name, **values)
    assert not PLACEHOLDER.findall(rendered)


_HEADING = re.compile(r"^## .*$", re.MULTILINE)

#: Разделы инструкций, которые обязаны стоять ВЫШЕ блока подстановок.
_INSTRUCTION_HEADINGS = ("## Правила", "## Формат ответа")

#: Что имеет право стоять НИЖЕ последней метки: закрывающая ограда кодового
#: блока и метка версии файла. Всё прочее там — неизменный текст, который
#: кэшируемый префикс уже не покрывает.
_TAIL_ALLOWED = re.compile(r"```|<!--\s*версия[^>]*-->")

#: Предел непробельного хвоста ниже последней метки, знаков. Обоснование —
#: в докстроке теста.
_TAIL_LIMIT = 200


@pytest.mark.parametrize("name", sorted(n for n, marks in EXPECTED_PLACEHOLDERS.items() if marks))
def test_placeholders_stand_after_instruction_blocks(name: str):
    """Блок подстановок последний, и ниже него нет текста — иначе кэш префикса не работает (РЕШ-52.6).

    Рендер промпта уходит провайдеру одним системным сообщением, а кэш префикса
    совпадает только по неизменной ГОЛОВЕ запроса и обрывается на первом же
    изменяемом байте. Метка `{{...}}` в начале файла оставляла кэшируемым
    огрызок в десяток строк, а корпус правил, таблиц и примеров пересчитывался
    каждым вызовом заново.

    Проверяются три условия. Первые два — про положение заголовков:

    1. первая метка стоит правее последнего заголовка `^## `, то есть блок
       подстановок и есть последний раздел файла;
    2. разделы «## Правила» и «## Формат ответа» стоят выше первой метки.

    Третье — про остаток, и оно единственное меряет по существу:

    3. ниже ПОСЛЕДНЕЙ метки не остаётся содержательного текста: после вычета
       закрывающей ограды ``` и метки `<!-- версия … -->` там не больше
       `_TAIL_LIMIT` знаков без пробелов.

    Третье условие добавил пост-аудит Т-7, и без него докстрока обещала
    больше, чем тест делал. Условия 1 и 2 смотрят только на позиции
    заголовков и молчат о том, сколько неизменного текста осталось ПОД
    меткой. Аудит собрал три раскладки, зелёные при одних лишь первых двух
    условиях и при этом рвущие кэшируемый префикс:

    * `intake.md`: блок «## Вход» вставлен сразу под последний заголовок, так
      что ниже первой метки остаётся 1326 знаков тела предыдущего раздела;
    * `planner.md`: блок подстановок остаётся последним `##`-разделом, но ниже
      дописано приложение под `### ` — с тремя решётками, поэтому регулярка
      `^## ` его не видит; некэшируемый хвост — 63,9 % файла;
    * `summary.md`: «## Правила» вырождены в отсылку, а корпус правил уехал
      под метки в то же приложение, то есть инструкции физически ниже данных.

    Порог не нулевой, потому что у `intake.md` ниже последней метки законно
    стоит однострочное пояснение про пустой `INDUSTRY_HINT` — 50 знаков без
    пробелов, самый длинный законный хвост во всём наборе. 200 знаков дают
    запас ещё на строку-другую такого рода и при этом остаются на порядок
    ниже самого маленького из воспроизведённых разрывов (1326 знаков), то
    есть порог отличает пояснение от переехавшего вниз корпуса инструкций.

    Порогом «во второй половине файла» ничего из этого не меряется: он
    проходится сам собой на коротком промпте с метками в конце входного блока
    и не отличает перенесённый файл от неперенесённого.
    """
    text = load_prompt(name)
    headings = list(_HEADING.finditer(text))
    placeholders = list(PLACEHOLDER.finditer(text))
    assert placeholders, f"промпт {name}: ни одной метки — тест должен идти только по промптам с метками"
    assert len(headings) >= 2, f"промпт {name}: меньше двух разделов, положение блока подстановок не определено"

    first_placeholder = min(match.start() for match in placeholders)
    last_heading = headings[-1]
    assert first_placeholder > last_heading.start(), (
        f"промпт {name}: первая метка стоит выше последнего раздела «{last_heading.group(0).strip()}». "
        "Блок подстановок обязан быть последним разделом файла, иначе кэшируемый префикс обрывается на нём"
    )

    for heading in _INSTRUCTION_HEADINGS:
        index = text.find(f"\n{heading}")
        assert index != -1, f"промпт {name}: нет раздела «{heading}»"
        assert index < first_placeholder, (
            f"промпт {name}: раздел «{heading}» стоит НИЖЕ первой метки подстановки — "
            "инструкции обязаны быть в неизменяемом префиксе, а меняющиеся данные под ними"
        )

    tail = text[max(match.end() for match in placeholders) :]
    residue = re.sub(r"\s+", "", _TAIL_ALLOWED.sub("", tail))
    assert len(residue) <= _TAIL_LIMIT, (
        f"промпт {name}: ниже последней метки осталось {len(residue)} знаков текста при пределе {_TAIL_LIMIT} "
        f"(всего хвоста {len(tail)} знаков, файл {len(text)}). Этот текст неизменен, но кэшируемым префиксом "
        f"уже не покрыт: перенесите его ВЫШЕ блока подстановок. Начало хвоста: {residue[:80]!r}"
    )


def test_render_prompt_fails_loudly_on_missing_value():
    """Забыли метку — падаем сразу, а не отправляем её модели."""
    with pytest.raises(ValueError, match="не заполнены метки"):
        render_prompt("verifier", CARD_JSON="{}")


@pytest.mark.parametrize(
    "name", ["intake", "planner", "replanner", "card_author", "verifier", "summary", "scorer", "dedup_judge"]
)
def test_prompt_has_error_section(name: str):
    """У каждого промпта есть раздел про ошибки: дешёвые модели без него врут."""
    text = load_prompt(name).lower()
    assert "форматы ошибок" in text, f"промпт {name}: нет раздела «Форматы ошибок»"


def test_gov_support_reference_is_loadable():
    """Справочник господдержки читается и содержит ключевые инструменты."""
    import yaml

    path = get_settings().data_dir / "gov_support.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    names = [item["name"] for item in data["instruments"]]
    assert any("218" in name for name in names)
    assert any("доращивание" in name.lower() for name in names)


def test_card_author_does_not_default_to_lab_without_signs():
    """Правило 8 не должно предписывать `lab`, если признаков зрелости в источниках нет —
    это был найденный аудитом источник нарушения инварианта №1 (ложная зрелость без ссылки)."""
    text = load_prompt("card_author")
    assert not re.search(r"признаков нет.*lab", text), (
        "card_author.md всё ещё предписывает ставить lab при отсутствии признаков"
    )
    assert "unknown" in text


def test_card_author_response_format_has_requirement_checks():
    """Формат ответа обязан содержать requirement_checks — построчную сверку с брифом (РЕШ-38)."""
    text = load_prompt("card_author")
    assert "requirement_checks" in text


# ── Атрибуция чисел аналога/прототипа (РЕШ-181 п. 1, Б-1 пост-аудита Э2-З) ───
#
# Живой воротный прогон приписал разработке из карточки 6 чужие числа: 98%
# восстановления активности и «менее 10 ppm» серы взяты из раздела патента
# RU2758845C1 «уровень техники», где разбирается ДРУГОЙ патент (№2666355) как
# аналог с недостатками (`.planning/evidence/audit-Э2-З.md`, находка Б-1).
# Первая строка отчёта клиенту и единственный `confirmed` во всём отчёте
# держались на этой подмене. Ниже — не поведенческий тест (живой прогон —
# работа владельца, РЕШ-181 п. 2), а текстовый страж: без правила атрибуции в
# промпте модель может повторить ту же ошибку на следующем патенте, и страж
# обязан упасть первым, до платного прогона.


def test_card_author_запрещает_переносить_числа_аналога_на_разработку():
    """Мутация «правило атрибуции убрано из промпта» обязана ронять этот тест."""
    text = load_prompt("card_author")
    assert "АНАЛОГ" in text and "ПРОТОТИП" in text
    assert "принадлежат этому аналогу" in text
    # Правило прямо перечисляет requirement_checks среди полей, куда числа
    # аналога переносить нельзя — карточка 6 нарушила именно на этом поле.
    assert "requirement_checks" in text.split("АНАЛОГ")[-1][:600]


# ── Латиница в плашке уверенности (Э4-П17, РЕШ-218) ──────────────────────────
#
# Аудит клиентского отчёта №8 нашёл в плашке уверенности карточки строку модели
# «confidence остаётся low ввиду исходной низкой оценки» — имена полей и
# значения перечисления латиницей в тексте, который клиент читает дословно (на
# странице отчёта, в PDF и в DOCX). Правило языка проекта требует русского во
# всём человекочитаемом. Ниже — текстовый страж: мутация «правило 13 больше не
# запрещает латиницу явно» обязана ронять этот тест.


def test_verifier_запрещает_латиницу_в_confidence_note():
    """Правило 13 обязано явно запрещать латинские имена полей и значения
    перечисления в `confidence_note` и показывать дословный пример из аудита
    рядом с исправленным видом строки."""
    text = load_prompt("verifier")
    rule_13 = text.split("13.")[1].split("14.")[0]
    # Дословная строка из отчёта №8, найденная аудитом.
    assert "confidence остаётся low ввиду исходной низкой оценки" in rule_13
    # Уровни названы по-русски, а не именами значений перечисления.
    for level in ("низкая", "средняя", "высокая"):
        assert level in rule_13
    # Имена полей запрещены явно, а не только упомянуты как пример нарушения.
    assert "confidence" in rule_13 and "maturity_stage" in rule_13
    assert "латиниц" in rule_13.lower()


def test_verifier_называет_неподтверждённую_стадию_по_русски():
    """Правило 7 не должно подсказывать модели значение перечисления MaturityStage
    (lab/prototype/pilot/series) в фиксированной строке unsourced_claims — она печатается
    клиенту дословно, и в сохранённых отчётах уже нашлись «…: prototype» и «…: pilot»
    (запросы 5–7, находка оркестратора при расширении Э4-П17). Мутация «правило 7 больше не
    требует русского названия стадии» обязана ронять этот тест."""
    text = load_prompt("verifier")
    rule_7 = text.split("7.")[1].split("8.")[0]
    # Префикс строки, на который смотрит код-страховка cards.py::_MATURITY_UNCONFIRMED_MARKER,
    # обязан остаться неизменным — иначе понижение стадии до unknown перестанет срабатывать.
    assert "стадия зрелости не подтверждена" in rule_7
    # Стадии называются теми же русскими словами, какими их печатает отчёт клиенту
    # (MATURITY_RU из backend/app/export/markdown.py).
    for stage_ru in (
        "работает в лаборатории",
        "есть опытный образец",
        "прошла пилот на объекте",
        "применяется серийно",
    ):
        assert stage_ru in rule_7
    # Правильный пример строки — без значения перечисления после двоеточия.
    assert "стадия зрелости не подтверждена: прошла пилот на объекте" in rule_7
    assert "латиниц" in rule_7.lower()


# ── Реестр НЦИС в доверенных доменах для patent (Э4-П18, РЕШ-217) ────────────
#
# Э4-П16 научила адаптер вести ссылки на белорусские патенты на официальный
# реестр НЦИС (`search.ncip.by`) вместо Google Patents, который для них отдаёт
# 404. Совет 3 ревью Э4-П16 нашёл, что правило 6 этот домен не знает — рабочая
# ссылка могла получать заниженную уверенность только потому, что домен ей
# незнаком. Ниже — текстовый страж: мутация «домен убран из списка» обязана
# ронять этот тест.


def test_verifier_доверяет_домену_реестра_нцис_для_patent():
    """Правило 6 обязано включать `search.ncip.by` в список доверенных доменов
    для типа `patent` — тот же домен, на который реально строит адрес
    `BY_DOCUMENT_URL` в `backend/app/search/rospatent.py` (РЕШ-217)."""
    text = load_prompt("verifier")
    rule_6 = text.split("6.")[1].split("7.")[0]
    assert "search.ncip.by" in rule_6


def test_verifier_проверяет_что_подтверждение_requirement_checks_не_из_описания_аналога():
    """Verifier получает отдельную проверку (РЕШ-181 п. 1): подтверждение
    requirement_checks обязано быть про саму разработку, а не про аналог или
    прототип, описанные тем же источником. Мутация «проверка убрана из
    промпта» обязана ронять этот тест."""
    text = load_prompt("verifier")
    assert "requirement_checks" in text and "confirmed" in text
    assert "АНАЛОГА" in text or "АНАЛОГ" in text
    assert "требование подтверждено описанием аналога/прототипа, а не разработки" in text


_CHANGELOG_ENTRY_VERSION = re.compile(r"^##\s*([\d.]+)\s", re.MULTILINE)
_FILE_VERSION_LABEL = re.compile(r"<!--\s*версия\s*([\d.]+)\s*-->")


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS))
def test_prompt_version_matches_latest_changelog_entry(name: str):
    """Метка версии в конце файла обязана совпадать с версией самой свежей записи CHANGELOG.md,
    где этот файл упомянут: иначе история изменений расходится с фактическим состоянием промпта."""
    text = load_prompt(name)
    label_match = _FILE_VERSION_LABEL.search(text)
    assert label_match, f"{name}.md: нет метки версии <!-- версия X.Y -->"
    file_version = label_match.group(1)

    changelog = (get_settings().prompts_dir / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = re.split(r"\n(?=## )", changelog)
    mentioning = [entry for entry in entries if f"`{name}.md`" in entry]
    assert mentioning, f"в CHANGELOG.md нет ни одной записи про {name}.md"
    latest_entry = mentioning[0]  # записи идут от новых к старым, самая свежая — первая по счёту
    version_match = _CHANGELOG_ENTRY_VERSION.search(latest_entry)
    assert version_match, "запись CHANGELOG.md про промпт без номера версии в заголовке"
    assert version_match.group(1) == file_version, (
        f"{name}.md: версия в метке файла {file_version} не совпадает с версией последней записи "
        f"CHANGELOG.md {version_match.group(1)}"
    )


def test_intake_empty_questions_requires_all_four_conditions():
    """Правило 2 не должно поощрять пустой список вопросов без условия — прежняя формулировка
    прямо называла его «нормальным и частым результатом», из-за чего на живых текстах НАТТ с
    готовым списком требований модель ни разу не задала вопрос (причина нулевого диалога)."""
    text = load_prompt("intake")
    assert "не менее двух" in text, "intake.md: не описано минимальное число вопросов при неполной постановке"
    assert "нормальный и частый результат" not in text, (
        "intake.md: прежняя формулировка всё ещё поощряет пустой список вопросов без условия"
    )
    assert "для солидности" in text, "intake.md: запрет на добор лишних вопросов не должен пропасть"


def test_candidates_to_json_keeps_url_and_trims_snippet(candidates):
    """Кандидаты для модели: ссылка на месте, длинный текст обрезан."""
    long_snippet = candidates[0].model_copy(update={"snippet": "с" * 5000})
    group = [
        ScoredCandidate(candidate=long_snippet, relevance=5, maturity=5, accessibility=5, freshness=5, completeness=5)
    ]

    payload = candidates_to_json(group)

    assert str(long_snippet.url) in payload
    assert len("с" * 5000) > payload.count("с")  # обрезка сработала
