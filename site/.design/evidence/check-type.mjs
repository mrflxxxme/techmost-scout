/* Кириллица проверяется рендером, а не описанием гарнитуры: буквы, у которых
   в неродной кириллице первым делом ломается рисунок, набираются реальным
   шрифтом и снимаются кадром. */
import { chromium } from 'playwright';
const here = new URL('.', import.meta.url).pathname;
const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
const p = await (await b.newContext({ viewport: { width: 1200, height: 560 }, deviceScaleFactor: 2 })).newPage();
await p.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
await p.waitForTimeout(1500);
await p.evaluate(() => {
  document.body.innerHTML = `<div id="t" style="padding:36px;background:#F4F6F5;color:#0B1416">
    <p style="font:700 64px/1.05 Geologica;letter-spacing:-.04em;margin:0">дзкъяжщ ДЗКЪЯЖЩ</p>
    <p style="font:500 30px/1.3 Geologica;margin:18px 0 0">«Ёлочки» — длинное тире, № 218, 250 млн ₽, 20–40 °C</p>
    <p style="font:400 22px/1.5 Geologica;margin:14px 0 0">Промывка водой снимает хлор с пыли перед переработкой. McKinsey, R&amp;D, 80–88&#8239;%.</p>
    <p style="font:400 15px/1.6 'Martian Mono';margin:20px 0 0">Martian Mono: Известия вузов. Чёрная металлургия, 2026, № 1, с. 14–22 · 24-79-10120</p>
    <p style="font:500 15px/1.6 'Martian Mono';margin:8px 0 0">ПОСТАНОВЛЕНИЕ 218 · КОЭФФИЦИЕНТ 2 · ГРАНТ ДО 250 МЛН ₽</p></div>`;
  document.body.style.margin = '0';
});
await p.waitForTimeout(900);
const box = await (await p.$('#t')).boundingBox();
await p.screenshot({ path: here + 'type-cyrillic.jpg', type: 'jpeg', quality: 82,
  clip: { x: 0, y: 0, width: 1200, height: Math.min(560, Math.ceil(box.height)) } });
// шрифты обязаны быть настоящими, а не подменой системным
console.log(await p.evaluate(async () => {
  const need = [['700 64px Geologica', 'дзкъяжщ'], ['400 22px Geologica', 'Промывка'],
                ['400 15px "Martian Mono"', 'Известия'], ['500 15px "Martian Mono"', 'ПОСТАНОВЛЕНИЕ']];
  const out = [];
  for (const [f, t] of need) out.push(`${f}: ${document.fonts.check(f) ? 'загружен' : 'НЕ ЗАГРУЖЕН'}`);
  return out.join(' · ');
}));
await b.close();
