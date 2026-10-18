// Замер пустоты: где секция дышит, а где зияет.
// Считается не «на глаз», а по геометрии: доля закрашенного содержимым,
// пустые полосы сверху и снизу внутри секции и самый широкий внутренний разрыв.
import { chromium } from 'playwright';

const W = Number(process.argv[2] || 1440), H = Number(process.argv[3] || 900);
const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const p = await (await b.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1 })).newPage();
await p.goto('http://127.0.0.1:8765/', { waitUntil: 'networkidle' });
await p.waitForTimeout(400);
// проявление по наблюдателю: не прокрутив страницу, замер видит скрытые узлы
// прозрачными и считает их пустотой. Плавную прокрутку на время замера снимаем:
// scrollTo с ней анимируется, и развёртка отстаёт от цикла на километры
await p.addStyleTag({ content: 'html{scroll-behavior:auto!important}' });
for (let y = 0; y < await p.evaluate(() => document.body.scrollHeight); y += 350) {
  await p.evaluate(v => scrollTo(0, v), y); await p.waitForTimeout(90);
}
await p.evaluate(() => scrollTo(0, 0)); await p.waitForTimeout(500);

const rows = await p.evaluate(() => {
  const LEAF = 'p,h1,h2,h3,h4,dt,dd,li,span,a,em,strong,figcaption,blockquote,canvas,img,svg,hr,button,input,label,time';
  const out = [];
  for (const sec of document.querySelectorAll('section, footer.foot')) {
    const r = sec.getBoundingClientRect(), top = r.top + scrollY;
    // строки содержимого: вертикальные интервалы, занятые видимыми листьями
    const bands = [];
    for (const el of sec.querySelectorAll(LEAF)) {
      const q = el.getBoundingClientRect();
      if (q.height < 1 || q.width < 1) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
      if (!el.textContent.trim() && !['CANVAS','IMG','SVG','HR','INPUT'].includes(el.tagName)) continue;
      bands.push([q.top + scrollY - top, q.bottom + scrollY - top]);
    }
    if (!bands.length) { out.push({ id: sec.id || sec.className, h: Math.round(r.height), empty: true }); continue; }
    bands.sort((a, c) => a[0] - c[0]);
    const merged = [bands[0].slice()];
    for (const [s, e] of bands.slice(1)) {
      const last = merged[merged.length - 1];
      if (s <= last[1] + 1) last[1] = Math.max(last[1], e); else merged.push([s, e]);
    }
    const ink = merged.reduce((a, [s, e]) => a + (e - s), 0);
    let gap = 0, gapAt = 0;
    for (let i = 1; i < merged.length; i++) {
      const g = merged[i][0] - merged[i - 1][1];
      if (g > gap) { gap = g; gapAt = Math.round(merged[i - 1][1]); }
    }
    out.push({
      id: sec.id || sec.className.split(' ').slice(0, 2).join('.'),
      h: Math.round(r.height),
      fill: +(ink / r.height).toFixed(2),
      top: Math.round(merged[0][0]),
      bot: Math.round(r.height - merged[merged.length - 1][1]),
      gap: Math.round(gap), gapAt,
    });
  }
  // разрывы между секциями: нижний край содержимого до верхнего края следующего
  return { out, docH: Math.round(document.documentElement.scrollHeight) };
});

console.log('высота страницы', rows.docH, 'экранов', (rows.docH / H).toFixed(1));
console.log('секция'.padEnd(16), 'высота', 'доля', 'сверху', 'снизу', 'разрыв');
for (const r of rows.out) {
  if (r.empty) { console.log(String(r.id).padEnd(16), String(r.h).padStart(6), '  — пусто'); continue; }
  const mark = (r.fill < 0.45 ? ' ← редко' : '') + (r.gap > 110 ? ` ← разрыв ${r.gap} на ${r.gapAt}` : '')
    + (r.top > 90 || r.bot > 90 ? ` ← поля ${r.top}/${r.bot}` : '');
  console.log(String(r.id).padEnd(16), String(r.h).padStart(6), String(r.fill).padStart(5),
    String(r.top).padStart(6), String(r.bot).padStart(5), String(r.gap).padStart(6), mark);
}
await b.close();
