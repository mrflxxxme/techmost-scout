/* Контраст текста там, где под ним стекло. Числа берутся с живых пикселей:
   вычислять по токенам бессмысленно — полупрозрачная поверхность смешивается
   с полем под собой, и настоящий фон текста не равен ни одному значению из CSS.
   Скрипт снимает кадр по рамке узла, дальше clips.py считает по пикселям. */
import { chromium } from 'playwright';
import { writeFileSync } from 'fs';

const here = new URL('.', import.meta.url).pathname;
const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
await p.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
await p.waitForTimeout(1800);
// шаг цикла раскрывается, иначе его строки не попадут в кадр
await p.evaluate(() => document.querySelector('#steps button').scrollIntoView({ block: 'center', behavior: 'instant' }));
await p.waitForTimeout(500);
await p.click('#steps button');
await p.waitForTimeout(700);

const spots = [
  ['карточка · значение', '#card dl dd'],
  ['карточка · подпись', '#card .meas'],
  ['карточка · оговорка', '#card .note'],
  ['карточка · источник', '#card .evid a'],
  ['карточка · задача', '#card .task'],
  ['ячейка позиции', '.stance li p'],
  ['ячейка команды', '.team li p'],
  ['спутник разрыва', '.minor p'],
  ['шапка · пункт', '.hdr .sections span'],
  ['шаг цикла · строка', '#steps button.on .detail dd'],
  ['шаг цикла · подпись', '#steps button.on .detail dt'],
  ['вкладка развилки', '.tabs button[aria-selected="false"]'],
  ['инструмент цикла', '.tools div'],
  ['подвал', '.foot span'],
];
const names = [];
let i = 0;
for (const [name, sel] of spots) {
  const el = await p.$(sel);
  if (!el) { console.log('нет узла: ' + sel); continue; }
  await p.evaluate((s) => document.querySelector(s).scrollIntoView({ block: 'center', behavior: 'instant' }), sel);
  await p.waitForTimeout(600);
  const bb = await el.boundingBox();
  if (!bb || bb.width < 6 || bb.height < 6) { console.log('нулевая рамка: ' + sel); continue; }
  // рамку обязательно подрезать по экрану: узел под шапкой или у самого низа
  // даёт координаты вне кадра, и снимок падает вместо того, чтобы дать число
  // с блока снимаются три полосы: одна может прийтись между строк, и тогда
  // в кадре окажется чистый фон, который легко принять за провал контраста
  for (const at of [0.28, 0.5, 0.72]) {
    const cy = bb.y + bb.height * at;
    const x0 = Math.max(0, Math.round(bb.x)), y0 = Math.max(0, Math.round(cy - 13));
    const x1 = Math.min(1440, Math.round(bb.x + Math.min(bb.width, 760)));
    const y1 = Math.min(900, Math.round(cy + 13));
    if (x1 - x0 < 6 || y1 - y0 < 6) continue;
    writeFileSync(here + `clip-${i}.png`,
      await p.screenshot({ clip: { x: x0, y: y0, width: x1 - x0, height: y1 - y0 } }));
    names.push([name, `clip-${i}.png`]);
    i++;
  }
}
writeFileSync(here + 'clips.json', JSON.stringify(names));
await b.close();
