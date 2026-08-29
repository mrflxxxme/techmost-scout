# Боевой образ приложения. Собирается на этапе Э5-1 (docs/SPEC.md).

FROM python:3.12-slim

WORKDIR /srv/scout

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY prompts ./prompts
COPY data/gov_support.yaml ./data/gov_support.yaml

# Переменная объявлена ДО установки playwright намеренно: инструкции RUN
# выполняются по порядку, и playwright читает PLAYWRIGHT_BROWSERS_PATH именно
# в момент установки. Ниже неё — браузер уедет в домашний кэш root
# (/root/.cache/ms-playwright), каталог /ms-playwright не появится никогда,
# и следующий chmod упадёт с кодом 1 — сборка образа оборвётся (найдено на
# аудите этапа Э5, находка Б1).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Группа export ставит python-docx и playwright; следом playwright тянет сам
# Chromium и системные библиотеки для него (--with-deps), иначе headless-
# браузер падает на отсутствующих .so уже в контейнере. Список пакетов apt
# playwright определяет сам, поэтому свои apt-install здесь не нужны.
RUN pip install --no-cache-dir ".[export]" \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Каталог с браузером — вне домашней папки пользователя root, поэтому после
# смены владельца ниже он остаётся нечитаемым для непривилегированного
# процесса, если явно не открыть права на чтение и выполнение.
RUN chmod -R a+rX /ms-playwright

# PYTHONPATH указывает и uvicorn, и консольной scout, откуда импортировать
# пакет приложения; settings.prompts_dir/data_dir/out_dir вычисляются от
# каталога app/config.py и благодаря этому совпадают с /srv/scout.
ENV PYTHONPATH=/srv/scout/backend

# Непривилегированный пользователь: процесс приложения не должен работать
# от root даже внутри контейнера. uid фиксирован, чтобы права на тома были
# предсказуемы между пересозданиями контейнера.
RUN groupadd --gid 10001 scout \
    && useradd --uid 10001 --gid scout --no-create-home --shell /usr/sbin/nologin scout \
    && mkdir -p /srv/scout/data /srv/scout/out \
    && chown -R scout:scout /srv/scout

# Ловушка: именованный том scout-data наследует содержимое /srv/scout/data
# из образа (включая gov_support.yaml) только при первом создании тома.
# Если справочник господдержки поменяется в новом образе, на уже развёрнутом
# сервере том его не подхватит — обновление руками описано в docs/deploy.md
# (задача Э5-И5).
VOLUME /srv/scout/data
VOLUME /srv/scout/out

USER scout

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend"]
