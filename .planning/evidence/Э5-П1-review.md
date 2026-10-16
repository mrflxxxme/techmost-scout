# Ревью Э5-П1 · цикл 1
Вердикт: ПРИНЯТЬ (блокирующих 0, советов 1)

## Разбор порядка в Dockerfile (построчно, как исполнитель)

1. `COPY pyproject.toml README.md ./`, `COPY backend`, `COPY prompts`,
   `COPY data/gov_support.yaml` — все источники существуют в дереве репозитория
   (проверено `fnmatch`/`Path.exists`, тест `test_copy_sources_exist_in_build_context`
   плюс собственная мутация подмены источника).
2. `ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` теперь стоит **до**
   `RUN pip install ".[export]" && python -m playwright install --with-deps chromium`.
   Переменная будет видна playwright в момент установки → браузер ляжет в
   `/ms-playwright`, каталог создастся.
3. `RUN chmod -R a+rX /ms-playwright` идёт следующей инструкцией — каталог к
   этому моменту существует, `chmod` не упадёт.
4. `ENV PYTHONPATH=/srv/scout/backend`, затем `RUN groupadd/useradd/mkdir -p
   /srv/scout/data /srv/scout/out/chown -R scout:scout /srv/scout` — `chown`
   идёт после всех `COPY` (они уже закоммичены в предыдущих слоях) и после
   `chmod` над `/ms-playwright` (тот каталог вне `/srv/scout`, ему `chown` не
   нужен — права на чтение уже открыты `a+rX` для всех, включая `scout`).
5. `VOLUME` (data, out) → `USER scout` (после последнего `RUN`) → `EXPOSE` →
   `HEALTHCHECK` (`/health`) → `CMD … --host 0.0.0.0 …`.

Порядок исполнения инструкций Dockerfile последователен, второго
«порядкового» дефекта такого же класса, как Б1, не нашёл. Перестановка `ENV`
выше `RUN pip install` не портит кэш слоёв по содержимому — `COPY`-слои,
идущие раньше `ENV`, не зависят от переменных окружения, объявленных позже
в файле; на пересборку без демона это и не проверить, но по семантике
Dockerfile ничего не выпадает из образа.

## Мутации (свои, не по протоколу исполнителя; каждая — `git checkout --` сразу после прогона, в репозиторий не закоммичены)

| # | Мутация | Результат |
|---|---|---|
| 1 | `ENV PLAYWRIGHT_BROWSERS_PATH` переставлен обратно после `RUN chmod` (форма Б1) | `test_playwright_browsers_path_declared_before_install` — **упал** (`assert 24 < 16`) |
| 2 | `ENV` оставлен наверху, но значение заменено на `/opt/pw-browsers`, `chmod` не тронут (рассинхронизация) | `test_browsers_path_is_readable_by_non_root_user` — **упал** (`'/opt/pw-browsers' in 'RUN chmod -R a+rX /ms-playwright'` → False) |
| 3 | `.dockerignore`: `**/.env`, `**/.env.*`, `**/*.pem`, `**/*.key` → `.env`, `.env.*`, `*.pem`, `*.key` | `test_dockerignore_secret_patterns_are_recursive` — **упал** |
| 4 | `.dockerignore`: `data/*.db*` → `data/*.db` (возврат к находке аудита) | `test_dockerignore_covers_backup_copies_of_database` — **упал** (`scout.db-journal` не покрыт) |
| 5 | Удалена строка `.git` из `.dockerignore` | `test_dockerignore_excludes_secrets_and_dev_artifacts` — **упал** |
| 6 | `COPY prompts ./prompts` → `COPY prompts_nonexistent ./prompts` | **упали оба**: `test_copy_sources_exist_in_build_context` и `test_all_required_copy_sources_are_present` |

Все шесть предписанных мутаций пойманы; тестов, «зелёных по неверной
причине», по этому набору не нашёл. После каждой мутации файл возвращён
(`git checkout -- <файл>`), рабочая копия чиста (`git status --porcelain` пуст
перед отчётом).

## Проверка `.dockerignore` по фактическому дереву репозитория

Собственным `fnmatch`-прогоном по `git ls-files` (987 файлов) против всех 18
шаблонов `.dockerignore`: `pyproject.toml`, `README.md`, `backend/app/main.py`,
`prompts/card_author.md`, `data/gov_support.yaml` — ни один не попадает под
игнор. Шаблон `data/*.db*` покрывает `scout.db`, `scout.db-journal`,
`scout.db-wal`, `scout.db.bak-2026-08-20`, но не задевает
`data/gov_support.yaml`. В репозитории нет ни одного отслеживаемого `*.pem`
или `*.key` — рекурсивные шаблоны ничего полезного не срезали.

## Инварианты (все десять)

№1, №2, №4, №5, №6, №10 — не затронуты задачей (нет карточек, промптов,
адаптеров, `httpx`, YAML). №3 — секретов не найдено (`grep -rE
"sk-|gigachat|Api-Key" backend/ tests/ docker/ .dockerignore` пусто),
`.env` в `.gitignore` есть, шаблоны `.dockerignore` стали строже, а не слабее.
№7 — `tests/test_docker_image.py` только читает файлы, ни `subprocess`, ни
`httpx`, ни обращений к демону. №8 — оба поведенческих изменения (порядок
`ENV`, рекурсивные шаблоны, `data/*.db*`) закреплены новыми тестами. №9 — diff
ограничен `docker/Dockerfile`, `.dockerignore`, `tests/test_docker_image.py` и
двумя файлами evidence; `docker-compose.yml`, `Caddyfile`, `docs/*`,
`backend/app/config.py` не тронуты (сверено `git diff --name-only`).

## Советы (не блокируют)

1. `docker/Dockerfile` вырос до 61 строки (было 54 на базе, ориентир
   волны 1 — «≤60 строк»). Превышение на одну строку целиком из-за
   добавленного шестистрочного комментария, объясняющего находку аудита —
   само по себе оправдано (CLAUDE.md требует комментировать неочевидные
   ловушки), но формально выходит за прежний ориентир на 1 строку. Не
   функциональный дефект, тестом не охраняется — решение по компактности
   комментария оставляю на усмотрение владельца/оркестратора.

## Проверено

```
$ /home/user/techmost-scout/.venv/bin/python -m pytest tests/test_docker_image.py -v
16 passed

$ /home/user/techmost-scout/.venv/bin/python -m pytest -q
1281 passed, 1 warning (httpx/httpx2 deprecation — не наш код)

$ /home/user/techmost-scout/.venv/bin/ruff check .
All checks passed!

$ /home/user/techmost-scout/.venv/bin/ruff format --check .
211 files already formatted

$ grep -rE "sk-|gigachat|Api-Key" backend/ tests/ docker/ .dockerignore
(пусто)

$ git diff fa4b53f wave5/e5-p1-image --name-only
.dockerignore
.planning/evidence/Э5-П1-handoff.yaml
.planning/evidence/Э5-П1-pytest.txt
docker/Dockerfile
tests/test_docker_image.py
```

Handoff исполнителя (`Э5-П1-handoff.yaml`) проверен: артефакты существуют,
`checks` воспроизведены (1281 passed, ruff чист), `self_audit: pass` и
`escalate: none` не противоречат друг другу. Границы задачи в `notes`
соблюдены — мутация M8 (`docker-compose.yml`) осознанно оставлена другой
задаче, это верно: файл вне списка Э5-П1.
