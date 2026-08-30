import { chromium } from 'playwright';
const b = await chromium.launch({ headless: true, executablePath: '/opt/pw-browsers/chromium' });
const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
await p.goto('http://127.0.0.1:8765/index.html', { waitUntil: 'load' });
await p.waitForTimeout(1200);
console.log('шапка:', await p.evaluate(() => Math.round(document.getElementById('hdr').getBoundingClientRect().height)), 'px');
console.log('на 390:', await (async () => { await p.setViewportSize({width:390,height:844}); await p.waitForTimeout(400);
  return Math.round(document.getElementById('hdr').getBoundingClientRect().height); })(), 'px');
await b.close();
