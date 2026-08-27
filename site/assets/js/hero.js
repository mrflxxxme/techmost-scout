/* Предмет первого экрана: поток задач через четыре створа.
   Механизм — воронка первого года из решения Р-21: десять задач предприятий,
   пять подборов с научной проверкой, две заявки на софинансирование, одно
   внедрение. Доли створов заданы этими числами, а не подобраны на глаз.
   Тёплая искра лежит на холодном фоне — находка 02 разведки среды. */

export function startHero(canvas) {
  if (!canvas) return;
  const g = canvas.getContext('2d', { alpha: false });
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const GATES = [0.36, 0.56, 0.74, 0.90];
  const KEEP = [5 / 10, 2 / 5, 1 / 2, 1];
  const COUNT = 78;

  let W = 0, H = 0, live = false;
  const dpr = Math.min(devicePixelRatio || 1, 2);
  const parts = [];

  const spawn = (x) => parts.push({
    x: x === undefined ? 0.015 : x,
    y: 0.08 + Math.random() * 0.84,
    v: 0.0016 + Math.random() * 0.0012,
    gate: 0, roll: Math.random(), fade: 0,
  });
  for (let i = 0; i < COUNT; i++) spawn(Math.random());

  function step() {
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i];
      if (p.fade) { p.fade++; if (p.fade > 30) parts.splice(i, 1); continue; }
      p.x += p.v;
      if (p.gate < GATES.length && p.x >= GATES[p.gate]) {
        if (p.roll > KEEP[p.gate]) { p.fade = 1; p.x = GATES[p.gate]; }
        p.gate++; p.roll = Math.random();
      }
      if (p.x > 1.05) parts.splice(i, 1);
    }
    while (parts.length < COUNT) spawn();
  }

  function draw() {
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);

    g.fillStyle = '#0E1620';
    g.fillRect(0, 0, W, H);

    // источник света стоит там, где поток выходит из последнего створа
    const lx = W * 0.9, ly = H * 0.42;
    const lit = g.createRadialGradient(lx, ly, 0, lx, ly, Math.max(W, H) * 0.8);
    lit.addColorStop(0, 'rgba(224,104,47,.15)');
    lit.addColorStop(0.4, 'rgba(120,60,30,.06)');
    lit.addColorStop(1, 'rgba(14,22,32,0)');
    g.fillStyle = lit; g.fillRect(0, 0, W, H);

    for (const gx of GATES) {
      g.save(); g.globalAlpha = 0.3; g.strokeStyle = '#22303E'; g.lineWidth = 1;
      g.beginPath(); g.moveTo(gx * W, H * 0.05); g.lineTo(gx * W, H * 0.95); g.stroke(); g.restore();
    }

    for (const p of parts) {
      const x = p.x * W, y = p.y * H;
      if (p.fade) {
        const f = 1 - p.fade / 30;
        g.fillStyle = `rgba(150,70,40,${f * 0.4})`;
        g.beginPath(); g.arc(x, y + p.fade * 0.5, 1.2 + (1 - f) * 4, 0, 6.283); g.fill();
        continue;
      }
      const t = Math.min(1, p.x), len = 18 + t * 46;
      const trail = g.createLinearGradient(x - len, y, x, y);
      trail.addColorStop(0, 'rgba(224,104,47,0)');
      trail.addColorStop(1, `rgba(255,180,110,${0.18 + t * 0.6})`);
      g.strokeStyle = trail; g.lineWidth = 0.9 + t * 1.5;
      g.beginPath(); g.moveTo(x - len, y); g.lineTo(x, y); g.stroke();
      g.fillStyle = `rgba(255,225,190,${0.3 + t * 0.6})`;
      g.beginPath(); g.arc(x, y, 0.9 + t * 1.5, 0, 6.283); g.fill();
    }

    // набор стоит на своём: мягкое радиальное затемнение, не заливка поперёк экрана
    const sx = W * 0.1, sy = H * 0.5;
    const shade = g.createRadialGradient(sx, sy, 0, sx, sy, Math.max(W, H) * 0.7);
    shade.addColorStop(0, 'rgba(14,22,32,.9)');
    shade.addColorStop(0.55, 'rgba(14,22,32,.55)');
    shade.addColorStop(1, 'rgba(14,22,32,0)');
    g.fillStyle = shade; g.fillRect(0, 0, W, H);

    if (live && !reduce) { step(); requestAnimationFrame(draw); }
  }

  // сцена не участвует в первом кадре и стоит вне вьюпорта
  const io = new IntersectionObserver(([e]) => {
    if (e.isIntersecting && !live) { live = true; requestAnimationFrame(draw); }
    else if (!e.isIntersecting) live = false;
  }, { threshold: 0 });
  io.observe(canvas);

  addEventListener('resize', () => { if (!live || reduce) draw(); }, { passive: true });
  draw();
}
