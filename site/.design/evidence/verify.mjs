import { chromium } from 'playwright';

// снимки ложатся рядом со скриптом, откуда бы его ни запустили
const here = new URL('.', import.meta.url).pathname;

const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
const p = await ctx.newPage();
const bad = [];
p.on('pageerror', e => bad.push('JS: ' + String(e).split('\n')[0]));
p.on('response', r => { if (r.status() >= 400) bad.push('HTTP ' + r.status() + ' ' + r.url().replace('http://127.0.0.1:8765','')); });

await p.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
await p.waitForTimeout(2200);

const geo = await p.evaluate(() => {
  const c = document.getElementById('spark');
  const r = c.getBoundingClientRect();
  return { rect: [Math.round(r.width), Math.round(r.height)], attr: [c.width, c.height] };
});
console.log('канвас: рамка', geo.rect.join('×'), '· атрибуты', geo.attr.join('×'));

// профиль кадров: то, что я обещал и ни разу не мерил
await p.evaluate(() => {
  window.__f = []; let last = performance.now();
  const tick = (t) => { window.__f.push(t - last); last = t; requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
});
for (let i = 0; i < 20; i++) { await p.mouse.wheel(0, 420); await p.waitForTimeout(500); }
const prof = await p.evaluate(() => {
  const f = window.__f.slice(4);
  const long = f.filter(d => d > 33).length;
  const sorted = [...f].sort((a, b) => a - b);
  return { n: f.length, long, pct: (long / f.length * 100).toFixed(1),
           p50: sorted[Math.floor(f.length * .5)]?.toFixed(1),
           p95: sorted[Math.floor(f.length * .95)]?.toFixed(1) };
});
console.log(`кадров ${prof.n}, длинных (>33 мс) ${prof.long} = ${prof.pct} % · медиана ${prof.p50} мс · p95 ${prof.p95} мс`);

// без скрипта страница обязана быть целой
const ctx2 = await b.newContext({ viewport: { width: 1440, height: 900 }, javaScriptEnabled: false });
const p2 = await ctx2.newPage();
await p2.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
await p2.waitForTimeout(700);
const nojs = await p2.evaluate(() => {
  const rs = [...document.querySelectorAll('.rise')];
  return { hidden: rs.filter(n => parseFloat(getComputedStyle(n).opacity) < .5).length, total: rs.length,
           h1: !!document.querySelector('h1'), h: document.documentElement.scrollHeight };
});
console.log(`без JS: скрытых секций ${nojs.hidden} из ${nojs.total}, заголовок на месте ${nojs.h1}, высота ${nojs.h}px`);

await p.evaluate(() => scrollTo(0, 0));
await p.waitForTimeout(1600);
await p.screenshot({ path: here + 'fixed-top.jpg', type: 'jpeg', quality: 72 });
await p.setViewportSize({ width: 390, height: 844 });
await p.waitForTimeout(1200);
await p.screenshot({ path: here + 'fixed-390.jpg', type: 'jpeg', quality: 72 });

console.log('проблемы:', bad.length ? [...new Set(bad)].join(' | ') : 'нет');
await b.close();
