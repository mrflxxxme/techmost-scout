# Верификация Э6-7 · поштучный вердикт эксперта на аналог (цикл 1)
Вердикт: PASS (закрыто 5 из 5 критериев задачи; общие критерии этапа и инварианты подтверждены; открытых DLG-N: 1, см. «Отложено»)

Рабочая копия: `/home/user/techmost-scout/.claude/worktrees/e6-7`, ветка `claude/e6-7`, HEAD до коммита верификации `2b60ba6`.
База: `claude/sleepy-planck-11w8jv` (предок HEAD). Diff задачи (`git diff --stat claude/sleepy-planck-11w8jv HEAD`): 10 файлов, 914 вставок, 19 удалений:
продуктовые `models.py`, `verdicts.py`, `export/view.py`, `web/routes/review.py`, `web/templates/review.html`; новый `tests/test_analog_verdicts.py` (460 строк);
четыре файла evidence (`Э6-W4-Э6-7-checks.txt`, `-handoff.yaml`, `-review-handoff.yaml`, `-review.md`). Выхода за список задачи нет.
Импорт пакета `app` при запуске pytest из рабочей копии — `backend/` рабочей копии (`pythonpath = ["backend"]` в pyproject): подтверждено мутациями M1–M4, каждая дала failed.

## Критерии приёмки PLAN.md «Э6-7»

| № | Критерий из PLAN.md | Команда | Факт | Статус |
|---|---|---|---|---|
| 1 | `tests/test_analog_verdicts.py -v` проходит | `pytest tests/test_analog_verdicts.py -v -p no:cacheprovider` | 24 passed, 0 failed (все 24 сценария из критерия 1 — в тестах; шесть выходов в `_outputs`: страница, markdown, печать, DOCX, JSON, `/api/v1`) | ок |
| 2 | Мутации: каждая ≥1 failed (по одной, откат `git checkout --`) | M1 `build_export_view` без `visible_analogs`: 4 failed · M2 `visible_analogs` передаёт в `hidden_reason` вердикт `None`: 4 failed · M3 `effective_analog_verdicts` берёт первую строку: 2 failed · M4 POST пишет при неизвестном ключе: 2 failed | 4 мутации убиты, откат после каждой, `git status` пуст | ок |
| 3a | `rg -n "\.decision\s*(==|!=)\s*VerdictDecision\.REJECT" backend/app` — ровно одна строка, в `verdicts.py` | та же команда | 1 строка: `backend/app/verdicts.py:207` (на базе: 1 строка, `verdicts.py:153`) | ок |
| 3b | `rg -n "VerdictDecision\.REJECT\|[\"']reject[\"']" backend/app --glob "*.py"` — ровно три строки | та же команда | 3 строки: `verdicts.py:207` (сравнение), `web/routes/review.py:86` (DECISION_LABELS), `schemas.py:92` (определение); на базе те же три места (`review.py:68`) | ок |
| 3c | `rg -n "reject" backend/app/web/templates \| rg -v 'option value="reject"'` — пусто | та же команда | пусто (на базе пусто) | ок |
| 4 | `rg -n "country_status" backend/app/web` — пусто | та же команда | пусто (на базе пусто) | ок |
| 5 | `pytest tests/test_export_analogs.py tests/test_backend_layout.py -q` зелёные без правок | та же команда; `git diff --name-status claude/sleepy-planck-11w8jv HEAD -- tests/` | 13 passed; в `tests/` только `A tests/test_analog_verdicts.py` (прежние тесты не тронуты) | ок |

## Общие критерии этапа (дополнение РЕШ-277 и «Общие правила этапа»)

| Правило | Команда | Факт | Статус |
|---|---|---|---|
| 3 · pytest без сети | `env -u <переменные> unshare -rn .venv/bin/pytest -q` | 1697 passed, 0 failed (ожидание 1673 + 24 = 1697) | ок |
| 3 · сеть выключена | `unshare -rn` + попытка соединения из той же оболочки | `OSError: [Errno 101] Network is unreachable` | ок |
| 3 · ruff | `ruff check .` | All checks passed! | ок |
| 3 · ruff format | `ruff format --check .` | 244 files already formatted | ок |
| 4 · прежние тесты не правятся | `git diff --name-status claude/sleepy-planck-11w8jv HEAD -- tests/` | только `A tests/test_analog_verdicts.py` | ок |
| 5 · размер файлов `backend/app` ≤ 500 | `find backend/app -name '*.py' -exec wc -l {} +` (старшие) | `review.py` 492, `run_stages.py` 457, `markdown.py` 449, `intake.py` 435, `rospatent.py` 432 | ок |
| 6 · инв. 6 | `grep -rln "httpx" backend/app --include='*.py' \| grep -vE "backend/app/(llm/\|search/\|cli_doctor\.py$\|pipeline/pipeline_context\.py$\|pipeline/pipeline_search\.py$)"` | пусто | ок |
| 8 · зависимостей нет | имена файлов diff | `pyproject.toml` и списки зависимостей в diff не входят | ок |

## Инварианты

- №1 (ссылка на каждый факт): бессылочный аналог скрывается тем же проходом `hidden_reason(require_evidence=True)` — тест `test_бессылочный_аналог_скрыт_тем_же_проходом_и_объяснён_на_экране` зелёный.
- №2 (замок не тронут): в diff нет изменений логики статуса, `all_cards_reviewed` и подписи (строки со словом `status` — это `status_code` в ответах и комментарии; `all_cards_reviewed` добавлен в импорт). Тесты `test_вердикты_аналогов_не_меняют_статус_запроса` и `test_валидированный_запрос_остаётся_валидированным` зелёные.
- №3 (секреты): буквальный `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` не пуст — см. DLG-1. Добавленных в diff строк с совпадениями: 0 (`git diff ... HEAD | grep -cE "^\+.*(sk-|gigachat|Api-Key)"` → 0). Совпадений на базе: 18, в HEAD: 18.
- №4 (подстановки): `pytest tests/test_prompts.py tests/test_team_docs.py -q` — 94 passed. Промпты и `docs/team/` в diff не входят.
- №5 (отказы источников): diff не трогает `search/`, `pipeline/` — не затронут.
- №6 (слои): см. общие критерии. Файлы с `httpx` в `backend/app`: `cli_doctor.py`, `llm/`, `pipeline/pipeline_context.py`, `pipeline/pipeline_search.py`, `search/` — только разрешённая карта.
- №7 (без сети): полный набор 1697 passed под `unshare -rn`, сеть недоступна (см. общие критерии).
- №8 (тест на новое поведение): в diff `backend/` (5 файлов) и `tests/test_analog_verdicts.py` (новый, 24 теста).
- №9 (скоуп): diff ограничен файлами задачи из PLAN.md «Файлы»; новых кабинетов, биллинга и интеграций нет.
- №10 (YAML): `pytest tests/test_team_docs.py` — в составе 94 passed.

## Проверка утечек (без значений)
Совпадения инварианта 3 классифицированы по контексту, значения не печатались:
`Api-Key` — имя HTTP-заголовка и комментарии в `config.py:76`, `llm/yandex.py:8,9,40`, `search/patents_view.py:26`, `search/yandex_search.py:323`, `tests/test_config.py:113`;
`sk-` — фиктивные значения в фикстурах и параметризации тестов (`tests/conftest.py:30`, `test_llm_router.py:202,220`, `test_plan_types.py:387,397,403`, `test_rescore_probe.py:383,397`, `test_dedup_judge_probe.py:230,242`, `test_config.py:113`) и текст комментариев; длины токенов после `sk-` — 0, 4, 19 символов, что не совпадает с форматом боевого ключа; тесты сами утверждают, что фиктивный ключ не попадает в вывод.

## Отложено
- **DLG-1 (инв. 3, буквальная форма проверки).** Причина: `grep -rE "sk-|gigachat|Api-Key" backend/ tests/` даёт 18 совпадений. Они уже есть на базе `claude/sleepy-planck-11w8jv` (те же 18), к задаче не относятся и классифицированы выше как имя заголовка и фиктивные значения в тестах. Инвариант по существу не нарушен: в diff задачи нет ни одного совпадения. Действие владельца: подтвердить классификацию и решить, сужать ли проверку в `docs/team/INVARIANTS.md` до формата ключа (шаблоны вида `sk-[A-Za-z0-9]{32}`), либо переписать фикстуры и заголовок, чтобы буквальный grep был пуст.

## Причина FAIL
Не применимо: все критерии задачи закрыты.
