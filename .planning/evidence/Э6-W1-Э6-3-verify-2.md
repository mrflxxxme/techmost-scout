# Верификация Э6-3 · цикл 2 (после РЕШ-268)

Записано оркестратором по тексту отчёта свежего верификатора: верификатор вернул
отчёт текстом и не закоммитил (отступление от §13а). Сокращено только в
оформлении, вердикт и факты — как у верификатора.

Вердикт: **PASS** (3 из 3 критериев задачи; общие критерии закрыты; DLG-1 открыт, на вердикт не влияет).

Рабочая копия `.claude/worktrees/e6-3-onw1` (= origin/claude/e6-3), HEAD `43077b8`, `git status` чистый до и после мутаций.

| № | Критерий (кр. 2 уточнён РЕШ-268) | Факт | Статус |
|---|---|---|---|
| 1 | `pytest tests/test_countries.py -v`, все утверждения | 31 passed. business→main US/TW/CN; gov CN→main; gov US/DE/JP/KR/GB (и NO)→footnote; gov TW→excluded при наличии в unfriendly_430r; ValueError на "us", "USA", режим "world"; коды str и `^[A-Z]{2}$`, ловушка NO; дублей нет; 27 стран ЕС | ок |
| 2 | `rg -n "\"TW\"\|'TW'" backend/app --glob "*.py"` — пусто; `rg -ln … backend/app` — ровно `countries_gov.yaml` | первая — пусто (exit 1); вторая — ровно `backend/app/reference/countries_gov.yaml` (строки 71, 79) | ок |
| 3 | Мутации «сноска раньше исключения», «gov → всегда main» — ≥1 failed | 1 failed (30 passed); 7 failed (24 passed); откат, `git status` чистый | ок |

## Общие критерии

| Проверка | Факт | Статус |
|---|---|---|
| `pytest -q --deselect tests/test_docker_compose_backup_runtime.py` (Windows) | 1403 passed, 9 deselected, 0 failed | ок |
| База `origin/claude/e6-w1` | 1372 собрано + 31 новых = 1403 | ок |
| Офлайн-прогон (внешняя сеть заблокирована) | 1403 passed | ок |
| `ruff check .`, `ruff format --check .` | All checks passed; 222 files already formatted | ок |
| Контейнер `python:3.12-slim`, `pip install -e '.[dev,export]'` на `git archive HEAD` | 1403 passed, 9 deselected, exit 0 | ок |
| Правило 4: `git diff --stat origin/claude/e6-w1...HEAD -- tests/` | только новый `tests/test_countries.py` (+114) | ок |
| Размеры: `countries.py` 93, `test_countries.py` 114 строк; `test_backend_layout.py` зелёный | | ок |

## Инварианты

- №3: в диффе задачи (`backend/`, `tests/`) совпадений `sk-|gigachat|Api-Key` ноль. Буквальный grep по дереву не пуст из-за фиктивных значений и имени заголовка в файлах базы (не из Э6-3, не секреты).
- №6: httpx вне разрешённых мест — пусто. №7: тест и весь набор офлайн зелёные. №8: `backend/` и `tests/` изменены вместе. №9: дифф — 3 файла задачи. №10: `yaml.safe_load`, все коды — строки в кавычках.
- №1 по духу: `source_url`, `checked_at` есть; `edition` и состав не сверены — DLG-1.

## Отложено

- **DLG-1 (владелец):** состав перечня 430-р (52 записи) не сверен с первоисточником (`verified_against_source: false`). Действие владельца: сверить с publication.pravo.gov.ru, заполнить `edition`, выставить `verified_against_source: true` в PR.

## Причина PASS

Все три критерия закрыты фактическим выводом; уточнённая по РЕШ-268 проверка кр. 2 проходит; обе мутации дают падения; общие критерии и инварианты закрыты.
