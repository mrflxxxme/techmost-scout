# Верификация Э6-3 · цикл 1

Записано оркестратором со слов верификатора (роль без права записи, CONVENTIONS §13а),
сокращено только в оформлении; вердикт и факты — как у верификатора.

Вердикт: **FAIL** (закрыто 2 из 3 критериев задачи; общие критерии закрыты).

Рабочая копия `.claude/worktrees/e6-3-onw1`, ветка `claude/e6-3`, HEAD `1c9134d`, `git status` чистый.

| № | Критерий из PLAN.md | Факт | Статус |
|---|---|---|---|
| 1 | `pytest tests/test_countries.py -v` со всеми утверждениями | 31 passed; business→main для US/TW/CN; gov CN→main; US/DE/JP/KR/GB/NO→footnote; TW→excluded при наличии в 430-р; ValueError на "us", "USA", режим "world"; коды str и ^[A-Z]{2}$; дублей нет; 27 стран ЕС | ок |
| 2 | `rg -n "\"TW\"\|'TW'" backend/app` — пусто (Тайвань только в справочнике) | 2 совпадения: `backend/app/reference/countries_gov.yaml:71` и `:79`; в `*.py` — 0 | НЕ ЗАКРЫТ |
| 3 | Мутации «сноска раньше исключения» и «gov → всегда main» дают ≥1 failed | A: 1 failed (`test_gov_mode_taiwan_is_excluded_though_listed_in_430r`); B: 7 failed; без мутаций 31 passed | ок |
| О1 | `pytest -q` — 0 failed | Windows, `--deselect tests/test_docker_compose_backup_runtime.py`: 1403 passed, 9 deselected, 0 failed | ок |
| О2 | Прогон в `python:3.12-slim` (РЕШ-249) | `pip install -e '.[dev,export]'`, `pytest -q`: 1412 passed, rc=0 | ок |
| О3 | `ruff check .`, `ruff format --check .` | All checks passed; 222 files already formatted (Windows и контейнер) | ок |

## Инварианты (по diff `b65bbb4..HEAD`)
- №3: в файлах diff `sk-|gigachat|Api-Key` — пусто; в `git log -p` одно упоминание шаблона в отчёте ревью, не значение. ок.
- №6: проверка httpx по INVARIANTS — пусто. ок.
- №7: `tests/test_countries.py` с заблокированным сокетом — 31 passed. ок.
- №8: в diff код и тест вместе. ок.
- №9: потребителей `country_status` вне задачи нет. ок.
- №10: коды и значения с двоеточием в кавычках, `yaml.safe_load` читает, `test_yaml_codes_are_quoted_in_source_text` зелёный. ок.
- №1 по духу: `source_url` — корень publication.pravo.gov.ru, `edition` «не подтверждена», `verified_against_source: false` — см. DLG-1.

## Отложено
- **DLG-1:** состав перечня 430-р (52 записи), редакция и ссылка не сверены с первоисточником. Действие владельца: сверить и утвердить в PR, выставить `verified_against_source: true`.

## Причина FAIL
Критерий 2 буквально требует «пусто», а справочник лежит в `backend/app` и по контракту содержит код в кавычках. Нужна правка формулировки критерия (например, проверка только `*.py`) решением владельца или оркестратора, затем повторная проверка по уточнённой команде.

## Расхождения с handoff
- Handoff заявлял 1 failed в `tests/test_env_isolation.py`; у верификатора 0 failed (Windows и контейнер), у ревью тоже не воспроизвелось.
