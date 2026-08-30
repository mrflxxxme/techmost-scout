/* Предмет первого экрана: поток задач через четыре створа, нарисованный по бумаге.
   Механизм — воронка первого года из решения Р-21: десять задач предприятий,
   пять подборов с научной проверкой, две заявки на софинансирование, одно
   внедрение. Доли створов заданы этими числами, а не подобраны на глаз.

   На чернильной базе поток идёт светом: лайм достаётся только тому, что дошло
   до последнего створа. Воронка лежит в правой половине — ровно под стеклянной
   плитой доказательства, и это её работа: стеклу есть что рассеивать. */

export function startHero(canvas) {
  if (!canvas) return;
  const g = canvas.getContext('2d', { alpha: false });
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // воронка живёт в правой половине: под крупным заголовком чертежа быть не должно
  const GATES = [0.50, 0.65, 0.78, 0.91];
  const KEEP = [5 / 10, 2 / 5, 1 / 2, 1];
  const COUNT = 62;

  const BASE = '#10163A';
  const dpr = Math.min(devicePixelRatio || 1, 2);
  let W = 0, H = 0, live = false;
  const parts = [];

  const spawn = (x) => parts.push({
    x: x === undefined ? 0.30 : x,
    y: 0.06 + Math.random() * 0.88,
    v: 0.0014 + Math.random() * 0.0011,
    gate: 0, roll: Math.random(), fade: 0,
  });
  for (let i = 0; i < COUNT; i++) spawn(0.30 + Math.random() * 0.68);

  function step() {
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i];
      if (p.fade) { p.fade++; if (p.fade > 26) parts.splice(i, 1); continue; }
      p.x += p.v;
      if (p.gate < GATES.length && p.x >= GATES[p.gate]) {
        if (p.roll > KEEP[p.gate]) { p.fade = 1; p.x = GATES[p.gate]; }
        p.gate++; p.roll = Math.random();
      }
      if (p.x > 1.04) parts.splice(i, 1);
    }
    while (parts.length < COUNT) spawn();
  }

  function draw() {
    W = canvas.clientWidth; H = canvas.clientHeight;
    if (!W || !H) return;
    canvas.width = W * dpr; canvas.height = H * dpr;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);

    g.fillStyle = BASE;
    g.fillRect(0, 0, W, H);

    // створы: пунктир, как разметка на чертеже
    g.save();
    g.strokeStyle = 'rgba(205,220,249,.12)'; g.lineWidth = 1; g.setLineDash([2, 6]);
    for (const gx of GATES) {
      g.beginPath(); g.moveTo(gx * W, H * 0.06); g.lineTo(gx * W, H * 0.94); g.stroke();
    }
    g.restore();

    for (const p of parts) {
      const x = p.x * W, y = p.y * H;
      if (p.fade) {
        const f = 1 - p.fade / 26;
        g.fillStyle = `rgba(150,165,200,${f * 0.24})`;
        g.beginPath(); g.arc(x, y, 1 + (1 - f) * 3, 0, 6.283); g.fill();
        continue;
      }
      const passed = p.gate >= GATES.length;
      const t = Math.max(0, Math.min(1, (p.x - 0.30) / 0.70)), len = 16 + t * 40;
      const trail = g.createLinearGradient(x - len, y, x, y);
      if (passed) {
        trail.addColorStop(0, 'rgba(214,242,75,0)');
        trail.addColorStop(1, 'rgba(214,242,75,.95)');
      } else {
        trail.addColorStop(0, 'rgba(205,220,249,0)');
        trail.addColorStop(1, `rgba(205,220,249,${0.08 + t * 0.26})`);
      }
      g.strokeStyle = trail; g.lineWidth = passed ? 1.6 : 0.9 + t * 0.5;
      g.beginPath(); g.moveTo(x - len, y); g.lineTo(x, y); g.stroke();
      g.fillStyle = passed ? 'rgba(214,242,75,1)' : `rgba(205,220,249,${0.14 + t * 0.3})`;
      g.beginPath(); g.arc(x, y, passed ? 2 : 1 + t * 0.7, 0, 6.283); g.fill();
    }

    // набор стоит на чистом: мягкое затемнение слева, не заливка поперёк экрана
    const sx = W * 0.22, sy = H * 0.5;
    const clear = g.createRadialGradient(sx, sy, 0, sx, sy, Math.max(W, H) * 0.8);
    clear.addColorStop(0, 'rgba(16,22,58,1)');
    clear.addColorStop(0.4, 'rgba(16,22,58,.94)');
    clear.addColorStop(0.72, 'rgba(16,22,58,.4)');
    clear.addColorStop(1, 'rgba(16,22,58,0)');
    g.fillStyle = clear; g.fillRect(0, 0, W, H);

    if (live && !reduce) { step(); requestAnimationFrame(draw); }
  }

  // сцена не участвует в первом кадре и стоит вне вьюпорта
  const io = new IntersectionObserver(([e]) => {
    if (e.isIntersecting && !live) { live = true; requestAnimationFrame(draw); }
    else if (!e.isIntersecting) live = false;
  }, { threshold: 0 });
  io.observe(canvas);

  addEventListener('resize', () => { if (!live || reduce) draw(); }, { passive: true });
  requestAnimationFrame(draw);
}
