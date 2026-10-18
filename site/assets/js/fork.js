/* Развилка на три адресата: единое ядро, меняется одна секция.
   Высота контейнера фиксируется по самому длинному состоянию — переключение
   не должно дёргать страницу. */

export function initFork(root) {
  if (!root) return;
  const tabs = [...root.querySelectorAll('[role="tab"]')];
  const panes = [...root.querySelectorAll('[role="tabpanel"]')];
  if (!tabs.length) return;

  const lockHeight = () => {
    const box = panes[0].parentElement;
    box.style.minHeight = '';
    const tallest = panes.reduce((max, p) => {
      const hidden = p.hidden; p.hidden = false;
      const h = p.getBoundingClientRect().height;
      p.hidden = hidden;
      return Math.max(max, h);
    }, 0);
    box.style.minHeight = Math.ceil(tallest) + 'px';
  };

  const select = (i) => {
    tabs.forEach((t, k) => {
      t.setAttribute('aria-selected', String(k === i));
      t.tabIndex = k === i ? 0 : -1;
    });
    panes.forEach((p, k) => { p.hidden = k !== i; });
  };

  tabs.forEach((t, i) => {
    t.addEventListener('click', () => select(i));
    t.addEventListener('keydown', (e) => {
      const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const n = (i + d + tabs.length) % tabs.length;
      tabs[n].focus(); select(n);
    });
  });

  select(0);
  lockHeight();
  addEventListener('resize', lockHeight, { passive: true });
}

export function initSheet(btn, sheet, close) {
  if (!btn || !sheet) return;
  const open = (on) => {
    sheet.classList.toggle('open', on);
    sheet.setAttribute('aria-hidden', String(!on));
    btn.setAttribute('aria-expanded', String(on));
    // шапка убирается: сквозь полупрозрачный лист она читалась как сбой слоёв
    document.body.classList.toggle('sheet-on', on);
    document.body.style.overflow = on ? 'hidden' : '';
  };
  btn.addEventListener('click', () => open(true));
  close?.addEventListener('click', () => open(false));
  sheet.querySelectorAll('a').forEach((a) => a.addEventListener('click', () => open(false)));
  addEventListener('keydown', (e) => { if (e.key === 'Escape') open(false); });
  open(false);
}
