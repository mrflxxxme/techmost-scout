/* Четыре роли движения, каждая там, где что-то значит.
   1. вход сцены      — riseOnEnter, разный жест для разных типов блоков
   2. отклик на курсор — pointerLight, свет идёт за курсором по материалу
   3. скраб по скроллу — scrubCycle, положение внутри трека выбирает шаг
   4. смена состояния  — initCycle и вкладки развилки (fork.js)

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
export function scrubCycle(track, count, show) {
  if (!track || !count) return;
  if (calm() || getComputedStyle(track).height === 'auto') return;
  let at = -1;
  const put = (i) => { if (i !== at) { at = i; show(i); } };
  put(0);
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
      put(Math.min(count - 1, Math.floor(p * count)));
    });
  }, { passive: true });
}

/** 4. Смена состояния: лента шагов выбирает панель.
    Панели именно переключаются, а не раскрываются: при скрабе раскрытие
    двигало бы соседние строки на каждом кадре, и лента дрожала бы. */
export function initCycle(rail, panes) {
  const tabs = rail ? [...rail.querySelectorAll('[role="tab"]')] : [];
  const cards = panes ? [...panes.children] : [];
  if (tabs.length !== cards.length || !tabs.length) return () => {};
  // на узком экране кульминация не закрепляется: закреплённый кадр там выше
  // экрана и срезал бы собственную нижнюю строку. Лента прячется, панели
  // идут подряд — у каждой свой номер, шаг и срок в подзаголовке
  const wide = matchMedia('(min-width:901px)');
  let at = 0;
  const show = (i) => {
    at = i;
    if (!wide.matches) { cards.forEach((c) => { c.hidden = false; }); return; }
    tabs.forEach((t, k) => t.setAttribute('aria-selected', String(k === i)));
    cards.forEach((c, k) => { c.hidden = k !== i; });
  };
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => show(i));
    // стрелки внутри ленты — обычное поведение вкладок
    t.addEventListener('keydown', (e) => {
      const d = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
      if (!d) return;
      e.preventDefault();
      const n = (i + d + tabs.length) % tabs.length;
      show(n); tabs[n].focus();
    });
    t.setAttribute('tabindex', i ? '-1' : '0');
  });
  wide.addEventListener('change', () => show(at));
  show(0);
  return show;
}

/** 5. Состояние шапки: капсула набирает плотность, когда под ней пошло содержимое. */
export function stickyHeader(node, darkSelector) {
  if (!node) return;
  // высота шапки уезжает в переменную: от неё зависят пиннинг кульминации
  // и отступ якорей, а подбирать её числом в CSS — гарантированный рассинхрон
  const measure = () => document.documentElement.style
    .setProperty('--hdr', Math.round(node.getBoundingClientRect().height) + 'px');
  measure();
  addEventListener('resize', measure, { passive: true });
  const darks = darkSelector ? [...document.querySelectorAll(darkSelector)] : [];
  let stuck = null;
  let dark = null;
  const sync = () => {
    const nextStuck = scrollY > 24;
    if (nextStuck !== stuck) {
      stuck = nextStuck;
      node.classList.toggle('stuck', nextStuck);
    }
    // Смотрим полосу чуть ниже кромки шапки. Запас в 12 пикселей обязателен:
    // прилипшая шапка занимает место в потоке, и на самом верху страницы
    // под ней формально ничего нет — тёмная обложка начинается сразу за ней.
    const y = node.getBoundingClientRect().bottom + 12;
    const nextDark = darks.some((d) => {
      const r = d.getBoundingClientRect();
      return r.top <= y && r.bottom >= y;
    });
    if (nextDark !== dark) {
      dark = nextDark;
      node.classList.toggle('on-dark', nextDark);
    }
  };
  // чтение геометрии на каждом событии прокрутки — принудительный пересчёт
  // раскладки: без этого дросселя профиль кадров портился втрое
  let ticking = false;
  const onScroll = () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; sync(); });
  };
  sync();
  addEventListener('scroll', onScroll, { passive: true });
  addEventListener('resize', onScroll, { passive: true });
}
