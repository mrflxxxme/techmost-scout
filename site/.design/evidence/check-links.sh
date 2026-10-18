#!/bin/sh
# Каждый источник со страницы проверяется живым запросом. Правило репозитория:
# факт живёт вместе со ссылкой, значит ссылка обязана открываться.
cd "$(dirname "$0")/../.." || exit 1
grep -o 'href="https\?://[^"]*"' index.html | sed 's/href="//;s/"$//' | sort -u |
while read -r u; do
  printf '%-94s ' "$u"
  curl -sS -m 30 -o /dev/null -L -w 'HTTP %{http_code}\n' "$u" 2>&1 | tail -1
done
