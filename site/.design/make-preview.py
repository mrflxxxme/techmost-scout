"""Собирает превью страницы одним файлом для показа владельцу.

Боевой сайт (site/index.html) остаётся статикой без сборки: файлы стилей,
скриптов и шрифтов лежат рядом и подключаются ссылками. Этот скрипт нужен
только для артефакта-превью, где всё обязано быть в одном файле.
"""
import base64, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".design" / "preview.html"

tokens = (ROOT / "assets/css/tokens.css").read_text(encoding="utf-8")
for f in (ROOT / "assets/fonts").glob("*.woff2"):
    b64 = base64.b64encode(f.read_bytes()).decode()
    tokens = tokens.replace(f'url("../fonts/{f.name}")', f'url(data:font/woff2;base64,{b64})')

site_css = (ROOT / "assets/css/site.css").read_text(encoding="utf-8")

js = ""
for name in ("hero.js", "motion.js", "fork.js"):
    src = (ROOT / "assets/js" / name).read_text(encoding="utf-8")
    js += re.sub(r"^export\s+", "", src, flags=re.M) + "\n"

html = (ROOT / "index.html").read_text(encoding="utf-8")
title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
body = re.sub(r'<script type="module">.*?</script>', "", body, flags=re.S)

boot = """
startHero(document.getElementById('spark'));
riseOnEnter([...document.querySelectorAll('.rise')]);
scrubCycle(document.getElementById('track'), [...document.querySelectorAll('#steps > div')]);
initFork(document.getElementById('fork'));
initSheet(document.getElementById('burger'), document.getElementById('sheet'),
          document.getElementById('sheetClose'));
"""

OUT.write_text(
    f"<title>{title}</title>\n<style>\n{tokens}\n{site_css}\n</style>\n"
    f"{body}\n<script>\n{js}\n{boot}\n</script>\n",
    encoding="utf-8")
print(f"превью: {OUT} — {OUT.stat().st_size // 1024} КБ")
