/* Вход секций и скраб кульминации. Пиннинг — нативный sticky, как у всех
   десяти живо проверенных сайтов выборки; библиотека анимации не нужна. */

export function riseOnEnter(nodes) {
  // Прячем только здесь: в CSS секции видны, и упавший скрипт их не стирает.
  if (!('IntersectionObserver' in window)) return;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  nodes.forEach((n) => n.classList.add('armed'));
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
    }
  }, { rootMargin: '0px 0px -12% 0px', threshold: 0.05 });
  nodes.forEach((n) => io.observe(n));
}

/** Один шаг цикла на экран: положение скролла внутри трека выбирает активный. */
export function scrubCycle(track, steps) {
  if (!track || !steps.length) return;
  const setActive = (i) => steps.forEach((s, k) => s.classList.toggle('on', k === i));
  setActive(0);
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
    steps.forEach((s) => s.classList.add('on'));
    return;
  }
  let ticking = false;
  const onScroll = () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      const r = track.getBoundingClientRect();
      const span = r.height - innerHeight;
      if (span <= 0) return;
      const p = Math.min(1, Math.max(0, -r.top / span));
      setActive(Math.min(steps.length - 1, Math.floor(p * steps.length)));
    });
  };
  addEventListener('scroll', onScroll, { passive: true });
  onScroll();
}
