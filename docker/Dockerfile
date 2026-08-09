# Образ приложения. Собирается на этапе Э5-1 (docs/SPEC.md).
#
# Критичная деталь: в образ обязан попасть корневой сертификат НУЦ Минцифры,
# иначе GigaChat не пройдёт проверку TLS внутри контейнера.

FROM python:3.12-slim

WORKDIR /srv/scout

# Сертификаты НУЦ Минцифры кладутся в docker/certs/ на этапе деплоя
# (см. docs/deps-manual.md, пункт 1) и добавляются в системное хранилище.
COPY docker/certs/*.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY prompts ./prompts
COPY data/gov_support.yaml ./data/gov_support.yaml

RUN pip install --no-cache-dir .

ENV GIGACHAT_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    DATABASE_URL=sqlite+aiosqlite:////srv/scout/data/scout.db

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend"]
