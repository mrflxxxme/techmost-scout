/* Профиль кадров по трём прогонам на каждой ширине: одиночный замер в этой
   среде гуляет от 0,3 % до 3 % на одной и той же сборке, и по нему нельзя
   отличить правку от шума. Берётся медиана. */
import { chromium } from 'playwright';
const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
async function once(w, h) {
  const p = await (await b.newContext({ viewport: { width: w, height: h } })).newPage();
  await p.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
  await p.waitForTimeout(1400);
  await p.evaluate(() => { window.__f = []; let last = performance.now();
    const t = (x) => { window.__f.push(x - last); last = x; requestAnimationFrame(t); }; requestAnimationFrame(t); });
  for (let i = 0; i < 20; i++) { await p.mouse.wheel(0, 420); await p.waitForTimeout(500); }
  const r = await p.evaluate(() => { const f = window.__f.slice(4);
    const s = [...f].sort((a, b) => a - b);
    return { n: f.length, long: f.filter(d => d > 33).length, p50: +s[Math.floor(f.length * .5)].toFixed(1) }; });
  await p.close();
  return r;
}
for (const [w, h] of [[1440, 900], [390, 844]]) {
  const runs = []; for (let k = 0; k < 3; k++) runs.push(await once(w, h));
  const longs = runs.map(r => r.long).sort((a, b) => a - b);
  console.log(`${w}×${h}: длинных по трём прогонам ${runs.map(r => `${r.long}/${r.n}`).join(', ')} · медиана длинных ${longs[1]} · медианный кадр ${runs[1].p50} мс`);
}
await b.close();
