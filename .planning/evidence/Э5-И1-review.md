# Ревью Э5-И1 · цикл 1
Вердикт: ПРИНЯТЬ (блокирующих 0, советов 2)

## Разбор
Diff соответствует списку файлов задачи: `docker/Dockerfile`, `.dockerignore`
(новый), `tests/test_docker_image.py` (новый), плюс доказательства в
`.planning/evidence/`. `docker-compose.yml`, `Caddyfile`, `backend/app/config.py`
не тронуты (`git diff` по трём файлам — пусто). `docker/Dockerfile` — 55 строк
(≤60).

Прочитан код Dockerfile построчно: порядок `pip install .[export]` →
`playwright install --with-deps chromium` → `chmod -R a+rX /ms-playwright`
(ещё под root) → `groupadd/useradd/mkdir/chown` → `USER scout` логически
непротиворечив: права на каталог браузера открываются до смены пользователя,
владение `/srv/scout` передаётся `scout` тоже до смены пользователя.

Существо (не только буква):
- `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` + `chmod -R a+rX` до `USER scout`:
  непривилегированный процесс получит право на чтение и выполнение — работает.
- `PYTHONPATH=/srv/scout/backend` vs `CMD ... --app-dir backend`: конфликта
  нет. `REPO_ROOT` в `config.py` вычисляется как `Path(__file__).resolve().parents[2]`
  от `backend/app/config.py`, что при любом способе импорта (через `--app-dir`
  или через `PYTHONPATH`, если он резолвит на файл именно в дереве
  `/srv/scout/backend`, а не в site-packages) даёт `/srv/scout`. Проверено
  вручную: `Path('/srv/scout/backend/app/config.py').resolve().parents[2]` →
  `/srv/scout`. Оба способа запуска (веб и консольная `scout`) видят одинаковый
  `data_dir`/`prompts_dir`/`out_dir`.
- `HEALTHCHECK` без `curl`: команда `python -c "import urllib.request,sys; ..."`
  проверена вручную через `sh -c` (именно так интерпретируется CMD-часть
  HEALTHCHECK в shell-форме) — синтаксис валиден, при недоступном сервисе
  завершается кодом 1, при 200 завершился бы 0. Штатный `python:3.12-slim`
  содержит `urllib.request` из коробки.
- `.dockerignore`: не исключены `pyproject.toml`, `README.md`, `backend`,
  `prompts`, `data/gov_support.yaml` — всё, что `Dockerfile` копирует явным
  `COPY`, остаётся в контексте сборки. Исключены `.env`, `.env.*`, `*.pem`,
  `*.key`, `.git`, `.planning`, `tests`, `out`, `data/*.db*` — секреты и боевая
  база в образ не попадут.

Найдена одна деталь сверх формального списка «Что делает»: `ENV
DATABASE_URL=sqlite+aiosqlite:////srv/scout/data/scout.db`. Проверено —
значение совпадает с тем, что и так вычислил бы дефолт `Settings.database_url`
при `REPO_ROOT=/srv/scout` (посчитано вручную, см. выше), то есть строка
избыточна, но не меняет поведения и не выходит за пределы `docker/Dockerfile`
(файл этой задачи). Отнесено к советам, не к находкам — см. ниже.

## Мутации (обязательный набор из задания на ревью)
Каждая мутация вносилась, тест `tests/test_docker_image.py` прогонялся, затем
`git checkout -- <файл>` для отката. Ни одна мутация не пережила прогон —
рабочая копия после всех манипуляций чистая (`git status --short` пуст).

1. Убрать `.[export]` из `pip install` → `test_install_uses_export_extra`
   падает (`1 failed, 8 passed`).
2. Убрать строку `playwright install --with-deps chromium` →
   `test_playwright_installs_chromium_with_deps` падает.
3. Убрать `USER scout` → `test_user_directive_is_non_root_and_after_last_run`
   падает. Дополнительно проверено: переставить `USER scout` перед первым
   `RUN` (сразу после `WORKDIR`) — тот же тест падает («после последнего RUN»).
4. Убрать `ENV PYTHONPATH=/srv/scout/backend` →
   `test_pythonpath_points_to_backend_tree` падает.
5. Убрать блок `HEALTHCHECK ... CMD python -c ...` →
   `test_healthcheck_present_and_checks_health_endpoint` падает.
6. Убрать строку `.env` из `.dockerignore` →
   `test_dockerignore_excludes_secrets_and_dev_artifacts` падает.

Дополнительная (не из обязательного списка, для полноты): убрать `chmod -R
a+rX /ms-playwright` — весь набор `tests/test_docker_image.py` остаётся
зелёным (9 passed). Плановые критерии приёмки для этой строки теста не требуют
(раздел «Э5-И1», критерий 1, перечисляет ровно семь проверок, chmod среди них
нет) — это пробел не исполнителя, а формулировки плана. Не блокирует (тест
за пределы утверждённых критериев не расширялся ни одной из параллельных
задач волны), выношу советом.

## Инварианты
- №3: `grep -rE "sk-|gigachat|Api-Key" backend/ tests/ docker/` — совпадения
  только в существующем (до этой задачи) коде работы с YandexGPT и в тестовых
  фикстурах с заведомо фиктивными ключами (`sk-fake-leak-check-*`, `sk-test`) —
  к diff'у не относится. `.env`/`.env.*`/`*.pem`/`*.key` в `.dockerignore`
  есть, `.gitignore` содержит `.env`/`.env.*`. Секретов в `docker/Dockerfile`
  нет (критерий 4 плана — `rg "certs|GIGACHAT|НУЦ"` пуст).
- №5, №2, №4, №6, №1: диапазон задачи их не касается (не пайплайн, не промпты,
  не LLM/поиск) — не нарушены по построению.
- №7: `tests/test_docker_image.py` не собирает образ, не обращается к docker
  daemon и сети — только чтение файлов (`Path.read_text`), проверено чтением
  теста целиком.
- №8: новое поведение (Dockerfile, .dockerignore) закрыто новым
  `tests/test_docker_image.py` — 9 тестов, все смысловые директивы покрыты
  мутациями выше.
- №9: диффом подтверждено, что `docker-compose.yml`, `Caddyfile`,
  `backend/app/config.py` не тронуты; в образ не добавлено пакетов сверх
  `export`-группы и Chromium (`playwright install --with-deps chromium`
  сам подтягивает системные библиотеки, апт-пакетов вручную не добавлено).
- №10: `pytest tests/test_team_docs.py` — 35 passed; `.planning/evidence/Э5-И1-
  handoff.yaml` дополнительно проверен `yaml.safe_load` — парсится без ошибок.

## Грабли §0.1 SPEC.md
Не применимы к задаче (нет промптов, `render_prompt`, разбора `content`,
адресов GigaChat) — проверено чтением diff, ни один паттерн грабель не
затронут.

## Проверено
- `pytest tests/test_docker_image.py -v`: 9 passed (совпадает с заявленным)
- `pytest -q`: 1235 passed, 1 warning in ~71s (1226 базовых + 9 новых —
  совпадает с заявленным)
- `ruff check .`: All checks passed!
- `ruff format --check .`: 207 files already formatted
- `rg -n "^COPY \.( |$)|^COPY \.env|^COPY tests|^COPY \.planning"
  docker/Dockerfile`: совпадений нет (код возврата 1)
- `rg -n "certs|GIGACHAT|НУЦ" docker/Dockerfile`: совпадений нет (код
  возврата 1)
- `git diff f7f3d4f dd306b9 -- docker-compose.yml Caddyfile
  backend/app/config.py`: пусто — границы задачи не нарушены
- Мутации (6 обязательных + 1 дополнительная) — результаты выше
- `pytest tests/test_team_docs.py`: 35 passed

## Советы (не блокируют)
1. `ENV DATABASE_URL=sqlite+aiosqlite:////srv/scout/data/scout.db` в
   Dockerfile избыточен: дефолт `Settings.database_url` при `REPO_ROOT=/srv/scout`
   вычисляет то же самое значение. Не вредит (защита на случай будущего
   изменения способа вычисления `REPO_ROOT`), но не упомянут в разделе «Что
   делает» плана — стоило бы явно объяснить комментарием, зачем он тут, если
   решение осознанное, либо убрать как дублирующий дефолт.
2. Тестовый набор не проверяет `chmod -R a+rX /ms-playwright` — единственная
   строка Dockerfile, отсутствие которой не ловится ни одним тестом. Формально
   вне утверждённых критериев приёмки плана, но раз план сам объясняет, зачем
   эта строка нужна («иначе браузер... не читается»), тест на неё выглядел бы
   уместным дополнением при следующей правке образа.

---

# Ревью Э5-И1 · цикл 2
Вердикт: ПРИНЯТЬ (блокирующих 0)

Проверка двух точечных правок поверх принятого цикла 1 (`dd4e413`), новые
коммиты `1bdcb01` (код и тест) и `5f26cee` (доказательства). Границы —
`git diff dd4e413 5f26cee` — тронуты ровно `docker/Dockerfile` (−1 строка) и
`tests/test_docker_image.py` (+25 строк), плюс evidence-файлы; `.dockerignore`,
`docker-compose.yml`, `Caddyfile`, `backend/app/config.py` не задеты (diff по
ним пуст).

## 1. Новый тест `test_browsers_path_is_readable_by_non_root_user`
Не поверил заявленной мутации — поставил сам.

- Убрал `RUN chmod -R a+rX /ms-playwright` из `docker/Dockerfile`, прогнал
  точечный набор: `1 failed, 9 passed` — падает именно
  `test_browsers_path_is_readable_by_non_root_user`, остальные девять зелёные.
  Вернул строку (`git checkout -- docker/Dockerfile`): `10 passed`.
- Проверил, что тест привязан к значению `PLAYWRIGHT_BROWSERS_PATH`, а не к
  строке `/ms-playwright`, вписанной руками: временно переставил
  `PLAYWRIGHT_BROWSERS_PATH` на `/opt/pw-browsers`, не тронув `chmod`
  (имитация рассинхронизации, если каталог переедет, а строку chmod забудут
  обновить) — тест упал именно на несовпадении
  (`assert '/opt/pw-browsers' in 'RUN chmod -R a+rX /ms-playwright'`). Затем
  переставил оба значения консистентно (`PLAYWRIGHT_BROWSERS_PATH` и `chmod`
  на `/opt/pw-browsers`) — весь набор зелёный (`10 passed`). Значит тест ловит
  именно рассинхронизацию значения и команды, а не факт наличия слова `chmod`
  в файле. После проверки Dockerfile возвращён в исходное состояние
  (`git checkout --`), рабочая копия чистая (`git status --short` пуст).

## 2. Убрана `ENV DATABASE_URL=sqlite+aiosqlite:////srv/scout/data/scout.db`
Пересчитано вручную: `Path('/srv/scout/backend/app/config.py').resolve()
.parents[2]` → `/srv/scout`, дефолт `Settings.database_url` при этом
`REPO_ROOT` даёт `sqlite+aiosqlite:////srv/scout/data/scout.db` — тот же
адрес, что удалённая строка задавала явно. Проверено грепом, что ни один тест
и ни один код в репозитории не рассчитывает на `DATABASE_URL`, заданный
именно в образе: единственные упоминания `DATABASE_URL` в `tests/` —
`monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")` в
`tests/conftest.py`, `tests/test_run_visibility.py`,
`tests/test_secret_leak_guard.py` — они сами переопределяют переменную
значением `:memory:` и от Dockerfile не зависят. `docker-compose.yml` в этой
ветке ещё не существует (создаётся параллельной задачей Э5-И2, в этот диф не
входит) — сослаться там не на что.

## Проверено (воспроизведено самостоятельно)
- `pytest tests/test_docker_image.py -v`: 10 passed.
- Мутация (убран `chmod`): `1 failed, 9 passed`, падает ожидаемый тест;
  возврат строки — `10 passed`.
- Мутация desync (переставлен только `PLAYWRIGHT_BROWSERS_PATH`, `chmod`
  не тронут): `1 failed, 9 passed`, падает тот же тест на несовпадении
  значений.
- Мутация согласованного переноса (переставлены оба): `10 passed`.
- `pytest -q`: 1236 passed, 1 warning in ~70s — совпадает с заявленным.
- `ruff check .`: All checks passed!
- `ruff format --check .`: 207 files already formatted.
- `git diff dd4e413 5f26cee -- docker-compose.yml Caddyfile backend/app/config.py .dockerignore`: пусто.
- `grep -n "DATABASE_URL" docker/Dockerfile tests/*.py backend/app/config.py`: только
  переопределения на `:memory:` в тестах, к образу не относятся.

## Итог по задаче
Оба пункта цикла 2 закрыты по существу, не только по форме. Блокирующих
находок нет. Третий цикл не понадобился.
