/* Превью открывается до публикации. Один раз артефакт был опубликован
   ни разу не открытым, и владелец увидел битую страницу. */
import { chromium } from 'playwright';
const here = new URL('.', import.meta.url).pathname;
const file = 'file:///tmp/claude-0/-home-user-techmost-scout/d886a056-cec9-580c-a249-b5343402b8d4/scratchpad/sitall-preview.html';
const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
const bad = [];
const p = await (await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })).newPage();
p.on('pageerror', (e) => bad.push('JS: ' + String(e).split('\n')[0]));
p.on('requestfailed', (r) => bad.push('REQ ' + r.url().slice(0, 60)));
await p.goto(file, { waitUntil: 'load' });
await p.waitForTimeout(2000);
console.log(await p.evaluate(() => {
  const c = document.getElementById('spark').getBoundingClientRect();
  return JSON.stringify({ canvas: [Math.round(c.width), Math.round(c.height)],
    steps: document.querySelectorAll('#steps button').length,
    cardLinks: document.querySelectorAll('#card a').length,
    wash: !!document.querySelector('.wash'), field: !!document.querySelector('.scout .field'),
    fonts: document.fonts.status, h: document.documentElement.scrollHeight });
}));
const tr = await p.evaluate(() => { const t = document.getElementById('track');
  return { top: t.getBoundingClientRect().top + scrollY, h: t.offsetHeight }; });
for (const f of [0.1, 0.4, 0.65, 0.9]) {
  await p.evaluate(([top, h, fr]) => scrollTo({ top: top + fr * (h - innerHeight), behavior: 'instant' }), [tr.top, tr.h, f]);
  await p.waitForTimeout(350);
  const st = await p.evaluate(() => ({
    on: [...document.querySelectorAll('#steps button')].findIndex((x) => x.classList.contains('on')),
    dark: document.querySelector('.hdr').classList.contains('on-dark') }));
  process.stdout.write(`скраб ${f} → шаг ${st.on}, шапка ${st.dark ? 'тёмная' : 'светлая'}   `);
}
console.log();
await p.evaluate(() => scrollTo(0, 0)); await p.waitForTimeout(1400);
await p.screenshot({ path: here + 'preview-top.jpg', type: 'jpeg', quality: 76 });
await p.evaluate(() => document.getElementById('scout').scrollIntoView({ block: 'center', behavior: 'instant' }));
await p.waitForTimeout(1200);
await p.screenshot({ path: here + 'preview-scout.jpg', type: 'jpeg', quality: 76 });
console.log('проблемы:', bad.length ? [...new Set(bad)].join(' | ') : 'нет');
await b.close();
