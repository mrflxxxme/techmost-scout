/* Критерий приёмки 1: страница читается на 390, 768, 1280 и 1920 без
   горизонтальной прокрутки. Заодно ловится любой узел, вылезший за экран. */
import { chromium } from 'playwright';
const here = new URL('.', import.meta.url).pathname;
const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
for (const w of [390, 768, 1280, 1920]) {
  const p = await (await b.newContext({ viewport: { width: w, height: 900 } })).newPage();
  const bad = [];
  p.on('pageerror', (e) => bad.push(String(e).split('\n')[0]));
  await p.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
  await p.waitForTimeout(1500);
  const r = await p.evaluate(() => {
    const over = [...document.querySelectorAll('body *')].filter((n) => {
      const b = n.getBoundingClientRect();
      return b.width > 0 && (b.right > innerWidth + 1.5 || b.left < -1.5);
    }).map((n) => n.tagName.toLowerCase() + (n.className && typeof n.className === 'string'
      ? '.' + n.className.split(' ').filter(Boolean).slice(0, 2).join('.') : ''));
    return { scrollW: document.documentElement.scrollWidth, inner: innerWidth,
             h: document.documentElement.scrollHeight, over: [...new Set(over)].slice(0, 6) };
  });
  const ok = r.scrollW <= r.inner + 1;
  console.log(`${String(w).padStart(4)} px: прокрутка вбок ${ok ? 'нет' : 'ЕСТЬ (' + r.scrollW + ')'} · высота ${r.h} px` +
    (r.over.length ? ` · за экраном: ${r.over.join(', ')}` : '') +
    (bad.length ? ` · ошибки: ${bad.join('|')}` : ''));
  await p.screenshot({ path: here + `w-${w}.jpg`, type: 'jpeg', quality: 70 });
  await p.close();
}
await b.close();
