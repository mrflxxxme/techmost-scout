import { chromium } from 'playwright';
const OUT = new URL('.', import.meta.url).pathname;
const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
const bad = [];
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
const p = await ctx.newPage();
p.on('pageerror', e => bad.push('JS: ' + String(e).split('\n')[0]));
p.on('response', r => { if (r.status() >= 400) bad.push('HTTP ' + r.status() + ' ' + r.url()); });
await p.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
await p.waitForTimeout(1800);
await p.screenshot({ path: OUT + 'g-hero.jpg', type: 'jpeg', quality: 76 });

const go = async (sel, file, wait = 1400, block = 'center') => {
  await p.evaluate(([s, bl]) => document.querySelector(s).scrollIntoView({ block: bl }), [sel, block]);
  await p.waitForTimeout(wait);
  await p.screenshot({ path: OUT + file, type: 'jpeg', quality: 76 });
};
await go('#gap', 'g-gap.jpg');
// кульминацию снимаем в середине скраба: у её края закрепление ещё не сработало
// и кадр показывает не то, что видит читатель
await p.evaluate(() => { const t = document.getElementById('track');
  scrollTo({ top: t.getBoundingClientRect().top + scrollY + 0.4 * (t.offsetHeight - innerHeight), behavior: 'instant' }); });
await p.waitForTimeout(1400);
await p.screenshot({ path: OUT + 'g-cycle.jpg', type: 'jpeg', quality: 76 });
await go('#value', 'g-value.jpg');
await go('#scout', 'g-scout.jpg');
await go('#you', 'g-fork.jpg');
await go('#team', 'g-team.jpg');
await go('#contact', 'g-contact.jpg');

// лист разделов
await p.evaluate(() => scrollTo(0, 600)); await p.waitForTimeout(600);
await p.click('#burger'); await p.waitForTimeout(900);
await p.screenshot({ path: OUT + 'g-sheet.jpg', type: 'jpeg', quality: 76 });
await p.click('#sheetClose'); await p.waitForTimeout(500);

console.log('проблемы:', bad.length ? [...new Set(bad)].join(' | ') : 'нет');
await b.close();
