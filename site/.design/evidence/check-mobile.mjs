import { chromium } from 'playwright';

// снимки ложатся рядом со скриптом, откуда бы его ни запустили
const here = new URL('.', import.meta.url).pathname;
const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
const bad = [];
const ctx = await b.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
const p = await ctx.newPage();
p.on('pageerror', e => bad.push('JS: ' + String(e).split('\n')[0]));
p.on('response', r => { if (r.status() >= 400) bad.push('HTTP ' + r.status() + ' ' + r.url()); });
await p.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
await p.waitForTimeout(1500);

// доехать до цикла и пройти его скроллом
await p.evaluate(() => document.getElementById('cycle').scrollIntoView());
await p.waitForTimeout(900);

const shots = [];
for (let k = 0; k < 4; k++) {
  const st = await p.evaluate(() => {
    const stage = document.querySelector('.cycle .stage');
    const r = stage.getBoundingClientRect();
    // на узком экране лента спрятана, а панели идут подряд: смотрим их
    const panes = [...document.querySelectorAll('.pane')];
    const open = panes.filter(x => !x.hidden).length;
    const on = -1;
    const last = panes[panes.length - 1].getBoundingClientRect();
    const tools = document.querySelector('.tools').getBoundingClientRect();
    return { top: Math.round(r.top), h: Math.round(r.height), open, on,
             sticky: getComputedStyle(stage).position,
             bottomOfPanes: Math.round(last.bottom), toolsBottom: Math.round(tools.bottom),
             vh: innerHeight, overflow: document.documentElement.scrollWidth > innerWidth };
  });
  shots.push(st);
  await p.mouse.wheel(0, 620); await p.waitForTimeout(700);
}
for (const s of shots) console.log(JSON.stringify(s));

await p.evaluate(() => document.getElementById('cycle').scrollIntoView());
await p.waitForTimeout(400); await p.mouse.wheel(0, 900); await p.waitForTimeout(900);
await p.screenshot({ path: here + 'cycle-390.jpg', type: 'jpeg', quality: 74 });

await p.evaluate(() => document.getElementById('card').scrollIntoView({ block: 'center' }));
await p.waitForTimeout(900);
await p.screenshot({ path: here + 'card-390.jpg', type: 'jpeg', quality: 74 });

// карточка на десктопе
const c2 = await b.newContext({ viewport: { width: 1440, height: 980 }, deviceScaleFactor: 2 });
const p2 = await c2.newPage();
p2.on('pageerror', e => bad.push('JS(1440): ' + String(e).split('\n')[0]));
await p2.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
await p2.waitForTimeout(1200);
await p2.evaluate(() => document.getElementById('scout').scrollIntoView({ block: 'center' }));
await p2.waitForTimeout(1400);
await p2.screenshot({ path: here + 'card-1440.jpg', type: 'jpeg', quality: 74 });
const links = await p2.evaluate(() => [...document.querySelectorAll('#card a')].map(a => a.href));
console.log('ссылок в карточке:', links.length);
links.forEach(l => console.log('  ' + l));
console.log('проблемы:', bad.length ? [...new Set(bad)].join(' | ') : 'нет');
await b.close();
