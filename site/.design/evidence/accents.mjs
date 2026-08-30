/* Четыре кандидата в акцент на синей базе дек, снятые на живом первом экране.
   Цвет выбирается глазами, а числа лежат рядом: контраст к чернильному фону
   и к белому посчитан заранее и подписан на кадре. */
import { chromium } from 'playwright';
const here = new URL('.', import.meta.url).pathname;
const VARIANTS = [
  ['Янтарь',    '#F2A03D', '#9A5A0E', '8,24:1 на чернильном · родня золоту дек · Machina, Science Group'],
  ['Терракота', '#E2703A', '#A8410F', '5,52:1 · Commonwealth Fusion #CF6D46 · ваш партнёрский дек #B4552D'],
  ['Лайм',      '#D6F24B', '#5E7A00', '13,91:1 · Anduril #DFF140 · самый смелый'],
  ['Лёд',       '#7FB6FF', '#0F5FBF', '8,38:1 · Hadrian #70B8FD · монохромный синий'],
];
const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
for (const [name, acc, ink, note] of VARIANTS) {
  const p = await (await b.newContext({ viewport: { width: 1440, height: 820 }, deviceScaleFactor: 2 })).newPage();
  await p.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
  await p.addStyleTag({ content: `
    :root{--acc:${acc};--acc-ink:${ink};
      --d-bg:#10163A;--d-plate:#1C2452;--d-teal:#1E2761;
      --d-text:#CDDCF9;--d-dim:#A9B8DC}
    .top{background:#10163A}
    .top .field{background:
      radial-gradient(58% 52% at 84% 4%,${acc}22,transparent 66%),
      radial-gradient(62% 56% at 2% 42%,rgba(30,39,97,.85),transparent 68%),
      radial-gradient(48% 44% at 74% 96%,${acc}14,transparent 70%)}
    .pill{color:#10163A}
    #badge{position:fixed;left:0;right:0;bottom:0;z-index:99;display:flex;gap:16px;align-items:baseline;
      padding:14px 28px;background:#0B1030;color:#CDDCF9;font:400 13px Geologica;letter-spacing:.01em}
    #badge b{font-weight:700;font-size:16px;color:${acc}}
    #badge s{text-decoration:none;margin-left:auto;font-family:'Martian Mono';font-size:11px;color:#A9B8DC}` });
  await p.evaluate(([n, note, acc]) => {
    const d = document.createElement('div');
    d.id = 'badge';
    d.innerHTML = `<b>${n}</b><span>${note}</span><s>${acc}</s>`;
    document.body.appendChild(d);
  }, [name, note, acc]);
  await p.waitForTimeout(1600);
  await p.screenshot({ path: `${here}acc-${name}.jpg`, type: 'jpeg', quality: 80 });
  await p.close();
  console.log(name, acc, '→', `acc-${name}.jpg`);
}
await b.close();
