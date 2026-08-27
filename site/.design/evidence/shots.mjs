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
await go('.gap', 'g-gap.jpg');
await go('.stance', 'g-stance.jpg');
await go('#scout', 'g-scout.jpg');
await go('#you', 'g-fork.jpg');
await go('#team', 'g-team.jpg');

// лист разделов
await p.evaluate(() => scrollTo(0, 600)); await p.waitForTimeout(600);
await p.click('#burger'); await p.waitForTimeout(900);
await p.screenshot({ path: OUT + 'g-sheet.jpg', type: 'jpeg', quality: 76 });
await p.click('#sheetClose'); await p.waitForTimeout(500);

// контраст текста поверх стекла: реальный пиксель фона против цвета текста
const lum = (c) => { const f = c.map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
  return .2126 * f[0] + .7152 * f[1] + .0722 * f[2]; };
const pairs = await p.evaluate(async () => {
  const spots = [['карточка · значение', '#card dd'], ['карточка · подпись', '#card .meas'],
                 ['карточка · оговорка', '#card .note'], ['ячейка позиции', '.stance li p'],
                 ['ячейка команды', '.team li p'], ['спутник разрыва', '.minor p'],
                 ['шапка · пункт', '.hdr .sections']];
  const out = [];
  for (const [name, sel] of spots) {
    const n = document.querySelector(sel); if (!n) continue;
    n.scrollIntoView({ block: 'center' });
    const r = n.getBoundingClientRect();
    out.push({ name, color: getComputedStyle(n).color, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) });
  }
  return out;
});
console.log('контраст текста на стеклянных поверхностях:');
for (const s of pairs) {
  const bg = await p.evaluate(({ x, y }) => {
    let el = document.elementFromPoint(x, y);
    while (el) { const c = getComputedStyle(el).backgroundColor;
      if (c && c !== 'rgba(0, 0, 0, 0)' && !c.endsWith(', 0)')) return c; el = el.parentElement; }
    return getComputedStyle(document.body).backgroundColor;
  }, { x: s.x, y: s.y });
  const num = (c) => c.match(/[\d.]+/g).slice(0, 3).map(Number);
  const a = parseFloat((bg.match(/[\d.]+\)$/) || ['1)'])[0]) || 1;
  const base = [247, 246, 242];
  const over = num(bg).map((v, i) => v * a + base[i] * (1 - a));
  const L1 = lum(num(s.color)), L2 = lum(over);
  const cr = (Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05);
  console.log(`  ${s.name.padEnd(22)} ${cr.toFixed(2)}:1  ${cr >= 4.5 ? 'AA' : cr >= 3 ? 'AA крупный' : 'НИЖЕ AA'}`);
}
console.log('проблемы:', bad.length ? [...new Set(bad)].join(' | ') : 'нет');
await b.close();
