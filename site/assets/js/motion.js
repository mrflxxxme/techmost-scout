/* Четыре роли движения, каждая там, где что-то значит.
   1. вход сцены      — riseOnEnter, разный жест для разных типов блоков
   2. отклик на курсор — pointerLight, свет идёт за курсором по материалу
   3. скраб по скроллу — scrubCycle, положение внутри трека выбирает шаг
   4. смена состояния  — expandSteps и вкладки развилки (fork.js)

   Кривая и набор длительностей общие на весь сайт: цельность читается как дорогое. */

const calm = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

/** 1. Вход сцены. Прячет только скрипт: без него секции видны (см. site.css). */
export function riseOnEnter(nodes) {
  if (!('IntersectionObserver' in window) || calm()) return;
  nodes.forEach((n) => n.classList.add('armed'));
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      e.target.classList.add('in');
      io.unobserve(e.target);
    }
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0 });
  nodes.forEach((n) => io.observe(n));
}

/** 2. Отклик на курсор: свет по материалу и в кнопках. */
export function pointerLight(nodes) {
  if (calm() || !matchMedia('(hover: hover)').matches) return;
  for (const n of nodes) {
    n.addEventListener('pointermove', (e) => {
      const r = n.getBoundingClientRect();
      n.style.setProperty('--mx', `${((e.clientX - r.left) / r.width) * 100}%`);
      n.style.setProperty('--my', `${((e.clientY - r.top) / r.height) * 100}%`);
    }, { passive: true });
  }
}

/** 3. Скраб по скроллу: один шаг цикла на экран. */
export function scrubCycle(track, steps, onChange) {
  if (!track || !steps.length) return;
  const mark = (i) => {
    steps.forEach((s, k) => s.classList.toggle('on', k === i));
    onChange?.(i);
  };
  if (calm() || getComputedStyle(track).height === 'auto') {
    steps.forEach((s) => s.classList.add('on'));
    return;
  }
  mark(0);
  let ticking = false;
  addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      const r = track.getBoundingClientRect();
      const span = r.height - innerHeight;
      if (span <= 0) return;
      const p = Math.min(1, Math.max(0, -r.top / span));
      mark(Math.min(steps.length - 1, Math.floor(p * steps.length)));
    });
  }, { passive: true });
}

/** 4. Смена состояния: шаг цикла разворачивает, кто что делает. */
export function expandSteps(steps) {
  const setOpen = (btn, open) => {
    const panel = btn.querySelector('.detail');
    if (!panel) return;
    btn.setAttribute('aria-expanded', String(open));
    panel.style.height = open ? `${panel.scrollHeight}px` : '0px';
  };
  for (const btn of steps) {
    setOpen(btn, false);
    btn.addEventListener('click', () => {
      const open = btn.getAttribute('aria-expanded') !== 'true';
      steps.forEach((b) => {
        setOpen(b, b === btn && open);
        // раскрытый шаг становится активным: иначе владелец открывает один,
        // а подсвечен другой, и это читается как рассинхрон
        b.classList.toggle('on', open ? b === btn : b.classList.contains('on'));
      });
      if (open) steps.forEach((b) => b.classList.toggle('on', b === btn));
    });
  }
  addEventListener('resize', () => {
    for (const btn of steps) {
      if (btn.getAttribute('aria-expanded') === 'true') setOpen(btn, true);
    }
  }, { passive: true });
  // на узком экране раскрытый шаг ведёт скролл сам: там кульминация держит
  // экран, и список из четырёх заголовков без содержимого читать нечего
  const narrow = () => innerWidth <= 900;
  return (i) => {
    // скролл ведёт шаг: раскрытый следует за активным, если владелец уже раскрывал
    const anyOpen = steps.some((b) => b.getAttribute('aria-expanded') === 'true');
    if (anyOpen || narrow()) steps.forEach((b, k) => setOpen(b, k === i));
  };
}
