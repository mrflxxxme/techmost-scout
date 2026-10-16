# Ревью Э5-Д1 · цикл 1

Вердикт: **ПРИНЯТЬ**

## Что проверено

Правка тронула ровно то, что заявлено: `tests/test_docker_image.py` плюс два
файла доказательств (`Э5-Д1-handoff.yaml`, `Э5-Д1-мутации.txt`). Коммит
`69f7261` на базе `69811b6`. `docker/Dockerfile` диффом между базой и
коммитом не изменён — проверено:

```
$ git diff 69811b6 69f7261 -- docker/Dockerfile
(пусто)
```

## Мутации — проверены самостоятельно, не по протоколу исполнителя

Каждая мутация ставилась во временный `docker/Dockerfile` рабочей копии и
откатывалась `git checkout --` сразу после прогона.

1. **Прямая L3, без флага** — `COPY prompts ./prompts` → `COPY prompts_nope
   ./prompts`. Красный:
   `AssertionError: директива COPY 'prompts' отсутствует в Dockerfile` (упали
   и `test_copy_sources_exist_in_build_context`, и
   `test_all_required_copy_sources_are_present`).
2. **Прямая L3, с флагом (главная проверка)** —
   `COPY --chown=root:root prompts_nope ./prompts`. Красный, именно в целевом
   тесте: `AssertionError: источник COPY не найден в контексте сборки:
   'prompts_nope'`. Отбрасывание ведущих `--...` не превратилось в
   отбрасывание самого источника — прямая сторона не потеряна.
3. **Обратная L3** — `COPY --chown=root:root backend ./backend`. Зелёный:
   `16 passed`.
4. **Прямая совет 6 (форма Б1)** — `ENV PLAYWRIGHT_BROWSERS_PATH` перенесён
   после блока `RUN pip install ... && python -m playwright install ... &&
   rm -rf ...`. Красный: `assert 26 < 23` в
   `test_playwright_browsers_path_declared_before_install`.
5. **Обратная совет 6** — комментарий `# Заметка: далее playwright install
   ставит хромиум.` вставлен непосредственно перед `ENV
   PLAYWRIGHT_BROWSERS_PATH`, порядок директив не менялся. Зелёный:
   `16 passed`.
6. **Собственная мутация — форма RUN.** Многострочный `RUN pip install ... \
   && python -m playwright install ... \ && rm -rf ...` переписан в три
   отдельных однострочных `RUN` (без `\`-продолжений), порядок ENV/install
   сохранён — зелёный, `16 passed`: разбор не завязан на конкретный стиль
   переноса строк. Тем же файлом обратно переставлен ENV после трёх `RUN` —
   снова красный (`assert 25 < 23`): чувствительность к порядковому дефекту
   не потерялась и на альтернативной форме `RUN`.

Рабочее дерево после всех мутаций чистое (`git status --porcelain` — пусто).

## Побочная правка докстроки (SyntaxWarning)

Проверено компиляцией файла с `warnings.simplefilter("error")` —
предупреждений нет. Текст докстроки `test_playwright_browsers_path_declared_
before_install` описывает перенос строк словами («через обратную косую
черту»), не вставляя литеральный `\` в не-raw строку — смысл абзаца не
пострадал, это то же обоснование находки Б1 плюс новый абзац про совет 6.

## Прогоны

```
$ pytest tests/test_docker_image.py -q
16 passed in 0.05s

$ pytest -q
1293 passed, 1 warning in 83.52s
(предупреждение — StarletteDeprecationWarning про httpx в testclient.py,
не относится к правке)

$ ruff check .
All checks passed!

$ ruff format --check .
211 files already formatted

$ pytest tests/test_team_docs.py -q
35 passed

$ pytest tests/test_tests_layout.py -q
3 passed
```

## Инварианты

№3 — грепом по изменённым файлам (`sk-|gigachat|api-key|api_key`) совпадений
нет; секретов и адреса сервера в правке нет. №7 — тест читает файл текстом,
сети и демона не касается. №8 — правка теста относится к тестовому слою, код
продукта не тронут; связь с находками L3/совет 6 прямая. №9 — исполнитель сам
отметил находку L2 (журнал compose) как вне границ этой задачи и не правил
её — верно, `docker-compose.yml`/`tests/test_docker_compose.py` в диффе нет.
№10 — `tests/test_team_docs.py` зелёный, `.planning/evidence/*.yaml` разбору
не мешает.

## Handoff исполнителя

Артефакты существуют, `did` описывает результат (что стало ловиться/не
ловиться), а не только действие. `checks` содержат воспроизведённые числа
(1293 passed, 16 passed, ruff чисто) — совпадают с моим прогоном.
`self_audit: pass` и `escalate: none` не противоречат друг другу: блокирующих
находок нет. Указанная находка L2 вне границ корректно вынесена в `notes`, а
не исправлена молча.

## Советы (не блокируют)

Замечаний, требующих правки, нет. Идея на будущее: протокол мутаций
(`Э5-Д1-мутации.txt`) можно переиспользовать как образец формата для
аналогичных долгов Э5-Д2/Э5-Д3, но это не относится к данной задаче.

## Проверено

- pytest tests/test_docker_image.py -q: 16 passed
- pytest -q: 1293 passed
- ruff check .: All checks passed
- ruff format --check .: 211 files already formatted
- git diff 69811b6 69f7261 -- docker/Dockerfile: пусто
- git diff 69811b6 69f7261 --name-only: tests/test_docker_image.py,
  .planning/evidence/Э5-Д1-handoff.yaml, .planning/evidence/Э5-Д1-мутации.txt
- 6 мутаций (2 по L3, 2 по совету 6, 2 собственных) — результаты выше
