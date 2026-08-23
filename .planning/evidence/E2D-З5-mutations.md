# З-5 — прицельный прогон четырёх обязательных мутаций (РЕШ-135)

Команда прогона после каждой мутации:
`pytest tests/test_fallback_without_guesses.py tests/test_search_yandex.py -q`
(окружение без `RELEVANCE_MIN`/`SECOND_PASS_RELEVANCE_MAX`/`YANDEX_SEARCH_API_KEY`/
`YANDEX_CATALOG_ID`/`LLM_STAGE_ROUTES`/`LLM_FALLBACK_ORDER`, как в задании).
Дерево восстанавливалось копией сохранённого оригинала
`backend/app/search/yandex_search.py` между мутациями, `git status --short`
проверялся после каждого восстановления — только два целевых файла в diff.

## (а) `org` снова заполняется доменом

Правка: `org=None` → `org=(doc.findtext("domain") or "").strip() or None`.

Результат: 4 failed (test_откат_не_выдаёт_домен_и_modtime_за_org_и_year,
test_откат_не_доносит_домен_до_промпта_карточки_и_до_отчёта,
test_разбирает_ответ_в_кандидатов, test_search_raw_несёт_domain_и_modtime…) —
критерий 1 падает, как требует постановка. `git status --short` после
восстановления: только `backend/app/search/yandex_search.py` и
`tests/test_search_yandex.py` изменены, `test_fallback_without_guesses.py` не
отслежен (новый файл) — посторонних правок нет.

## (б) `year` снова выводится из `modtime`

Правка: `year=None` → `year=int((doc.findtext("modtime") or "0000")[:4]) or None`
(инлайн-эквивалент удалённой `_year_from_modtime`, сам хелпер удалён — правка
проверяет именно поведение, а не наличие функции).

Результат: те же 4 failed, теперь падают на `card.year is None` /
`candidate.year is None` — критерий 1 падает. Дерево восстановлено, `git status
--short` чист от посторонних файлов.

## (в) `domain`/`modtime` убраны и из сырых записей

Правка: в `_raw_records` удалены ключи `"domain"` и `"modtime"` из собираемого
словаря.

Результат: 1 failed — `test_search_raw_несёт_domain_и_modtime_для_стадии_extractor`
(`KeyError: 'domain'`) — критерий 3 падает, остальные 32 теста файла зелёные:
мутация ловится ровно тем тестом, для которого она и написана. Дерево
восстановлено.

## (г) откат перестал отдавать кандидатов вовсе

Правка: `search_raw` возвращает `([], _raw_records(root, limit=limit))` вместо
`(self._to_candidates(...), _raw_records(...))`.

Результат: 7 failed, включая
`test_откат_не_выдаёт_домен_и_modtime_за_org_и_year` (`len(outcome.candidates)
== 2` не сходится) — критерий 2 падает; попутно упали и не связанные с
задачей тесты дублей/склейки/конкурсного контура — ожидаемо, потому что
мутация убивает разбор адаптера целиком, а не только путь отката. Дерево
восстановлено, `git status --short` после отката — только целевые два файла и
новый тестовый файл.

## Итог

Все четыре мутации ловятся набором `test_fallback_without_guesses.py` +
`tests/test_search_yandex.py`, рабочее дерево после каждой мутации
восстановлено байт-в-байт (сверено копией оригинала), лишних файлов в
`git status` не появилось.
