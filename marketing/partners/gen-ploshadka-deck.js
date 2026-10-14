// Генератор дека для Площадки IT-Заказов (t.me/ploshadkait): предложение
// о партнёрстве. Слайды 1–6 повторяют дек для региональных партнёров,
// дальше идёт предложение площадке и слайд контактов сооснователей.
// Акцент предложения — внедрение: мы дополняем площадку для её заказчиков
// там, где после выбора исполнителя начинается сделка и доведение до
// производства.
// Геометрия, кегль и медный акцент — как в партнёрском деке. Раскладки
// продублированы намеренно: в этом каталоге каждый генератор самодостаточен,
// как gov-deck/gen-decks.js.
// Тексты слайдов — в deck-texts-ploshadka.md: правьте там, затем переносите
// сюда. Факты о площадке — в research-ploshadka-2026-09-01.md.
// Запуск: node gen-ploshadka-deck.js → sitall-deck-ploshadka.pptx

const pptxgen = require("pptxgenjs");
const path = require("path");

const NAVY = "1E2761";
const INKD = "1B2140";
const GRAY = "6B7699";
const ACC = "B4552D";
const LINE = "C9D3EA";
const ICEBG = "E3EAF8";
const F = "Segoe UI";
const F_SB = "Segoe UI Semibold";
const W = 13.333;
const H = 7.5;
const MX = 0.55;
const TOTAL = 10;

// Плашка на обложке.
const ADDRESSEE = "Площадке IT-Заказов: предложение о партнёрстве";

// Контакты сооснователей для финального слайда.
const KIRILL_EMAIL = "uklonskiy.k@ya.ru";
// Почта Данилы Кашева в материалах компании отсутствует. Впишите её здесь —
// строка с адресом появится на его плитке, как только значение перестанет
// быть пустым.
const KASHEV_EMAIL = "";

// Единственный публичный первоисточник о площадке — её собственный канал.
const PLOSHADKA = "https://t.me/ploshadkait";

const shadow = () => ({ type: "outer", angle: 90, blur: 14, offset: 4, color: "7E88A8", opacity: 0.3 });

// ---------------------------------------------------------------- помощники

function footer(slide, page) {
  slide.addText("Ситалл", {
    x: MX, y: H - 0.42, w: 2, h: 0.3, fontFace: F, fontSize: 12,
    color: GRAY, margin: 0, isTextBox: true,
  });
  slide.addText(`${String(page).padStart(2, "0")} / ${TOTAL}`, {
    x: W - 1.55, y: H - 0.42, w: 1, h: 0.3, fontFace: F, fontSize: 12,
    color: GRAY, align: "right", margin: 0, isTextBox: true,
  });
}

function kickerTitle(slide, kicker, title) {
  slide.addText(kicker.toUpperCase(), {
    x: MX, y: 0.24, w: W - 2 * MX, h: 0.3, fontFace: F_SB, fontSize: 13,
    color: ACC, charSpacing: 3, margin: 0, isTextBox: true,
  });
  slide.addText(title, {
    x: MX - 0.02, y: 0.56, w: W - 2 * MX, h: 0.62, fontFace: F_SB, fontSize: 27,
    color: NAVY, margin: 0, isTextBox: true,
  });
  slide.addShape("rect", { x: MX, y: 1.24, w: 1.2, h: 0.07, fill: { color: ACC } });
}

function card(slide, x, y, w, h, accent) {
  slide.addShape("roundRect", {
    x, y, w, h, rectRadius: 0.09,
    fill: { color: "FFFFFF" },
    line: { color: accent ? ACC : LINE, width: accent ? 1.75 : 1 },
    shadow: shadow(),
  });
}

// Карточка с крупной цифрой. src: [текст, url] или null.
function statCard(slide, o) {
  const accent = !!o.accent;
  card(slide, o.x, o.y, o.w, o.h, accent);
  const pad = 0.32;
  slide.addShape("rect", {
    x: o.x + pad, y: o.y + 0.28, w: 0.42, h: 0.05,
    fill: { color: accent ? ACC : NAVY },
  });
  slide.addText(o.big, {
    x: o.x + pad, y: o.y + 0.36, w: o.w - 2 * pad, h: 0.78,
    fontFace: F_SB, fontSize: o.bigSize || 44, color: accent ? ACC : NAVY,
    margin: 0, isTextBox: true,
  });
  slide.addText(o.caption, {
    x: o.x + pad, y: o.y + 1.16, w: o.w - 2 * pad, h: o.h - 1.6,
    fontFace: F, fontSize: 16, color: INKD, margin: 0,
    lineSpacingMultiple: 1.08, isTextBox: true, valign: "top",
  });
  if (o.src) {
    slide.addText(
      [
        { text: "Источник: ", options: { italic: true } },
        { text: o.src[0], options: { italic: true, underline: true, hyperlink: { url: o.src[1] } } },
      ],
      {
        x: o.x + pad, y: o.y + o.h - 0.38, w: o.w - 2 * pad, h: 0.26,
        fontFace: F, fontSize: 12, color: GRAY, margin: 0, isTextBox: true,
      }
    );
  }
}

// Карточка с номером в кружке.
function numCard(slide, o) {
  const accent = !!o.accent;
  card(slide, o.x, o.y, o.w, o.h, accent);
  const pad = 0.32;
  slide.addShape("ellipse", {
    x: o.x + pad, y: o.y + 0.26, w: 0.42, h: 0.42,
    fill: { color: accent ? ACC : NAVY },
  });
  slide.addText(String(o.n), {
    x: o.x + pad, y: o.y + 0.26, w: 0.42, h: 0.42, fontFace: F_SB,
    fontSize: 16, color: "FFFFFF", align: "center", valign: "middle",
    margin: 0, isTextBox: true,
  });
  slide.addText(o.head, {
    x: o.x + pad, y: o.y + 0.78, w: o.w - 2 * pad, h: o.headH || 0.4,
    fontFace: F_SB, fontSize: 20, color: NAVY, margin: 0,
    lineSpacingMultiple: 1.02, isTextBox: true, valign: "top",
  });
  const subH = o.h - 0.78 - (o.headH || 0.4) - (o.src ? 0.6 : 0.24);
  slide.addText(o.sub, {
    x: o.x + pad, y: o.y + 0.78 + (o.headH || 0.4) + 0.08,
    w: o.w - 2 * pad, h: subH,
    fontFace: F, fontSize: 16, color: INKD, margin: 0,
    lineSpacingMultiple: 1.08, isTextBox: true, valign: "top",
  });
  // Ссылка нужна карточке, которая утверждает факт о чужом бизнесе.
  if (o.src) {
    slide.addText(
      [
        { text: "Источник: ", options: { italic: true } },
        { text: o.src[0], options: { italic: true, underline: true, hyperlink: { url: o.src[1] } } },
      ],
      {
        x: o.x + pad, y: o.y + o.h - 0.38, w: o.w - 2 * pad, h: 0.26,
        fontFace: F, fontSize: 12, color: GRAY, margin: 0, isTextBox: true,
      }
    );
  }
}

function thesis(slide, text, y, h) {
  slide.addShape("rect", { x: MX, y: y, w: 0.07, h: h || 0.7, fill: { color: NAVY } });
  slide.addText(text, {
    x: MX + 0.28, y: y - 0.03, w: W - 2 * MX - 0.4, h: (h || 0.7) + 0.06,
    fontFace: F_SB, fontSize: 20, color: NAVY, margin: 0, valign: "middle",
    lineSpacingMultiple: 1.05, isTextBox: true,
  });
}

function quote(slide, text, author, url, y) {
  slide.addShape("rect", { x: MX, y: y, w: 0.07, h: 1.5, fill: { color: ACC } });
  slide.addText("«", {
    x: MX + 0.24, y: y - 0.1, w: 0.7, h: 0.9, fontFace: F_SB, fontSize: 48,
    color: ACC, margin: 0, isTextBox: true,
  });
  slide.addText(text, {
    x: MX + 0.95, y: y, w: W - 2 * MX - 1.2, h: 1.0, fontFace: F, fontSize: 20,
    italic: true, color: NAVY, margin: 0, lineSpacingMultiple: 1.12, isTextBox: true,
  });
  slide.addText(author, {
    x: MX + 0.95, y: y + 1.06, w: W - 2 * MX - 1.2, h: 0.32, fontFace: F,
    fontSize: 14, color: NAVY, underline: true, hyperlink: { url }, margin: 0, isTextBox: true,
  });
}

// ------------------------------------------------------------------ макеты

function cover(pres) {
  const s = pres.addSlide();
  s.addShape("ellipse", { x: 9.2, y: -1.8, w: 5.8, h: 5.8, fill: { type: "none" }, line: { color: LINE, width: 1.25 } });
  s.addShape("ellipse", { x: 11.0, y: 0.9, w: 3.6, h: 3.6, fill: { type: "none" }, line: { color: ACC, width: 1.5 } });
  s.addShape("ellipse", { x: 9.9, y: 4.2, w: 4.8, h: 4.8, fill: { type: "none" }, line: { color: LINE, width: 1.25 } });
  s.addText("СЕНТЯБРЬ 2026 · КОНФИДЕНЦИАЛЬНО", {
    x: MX, y: 0.4, w: 7, h: 0.3, fontFace: F_SB, fontSize: 13, color: ACC,
    charSpacing: 3, margin: 0, isTextBox: true,
  });
  s.addText("Ситалл", {
    x: MX - 0.06, y: 1.05, w: 9, h: 1.7, fontFace: F_SB, fontSize: 80,
    color: NAVY, margin: 0, isTextBox: true,
  });
  s.addShape("rect", { x: MX, y: 3.0, w: 1.5, h: 0.09, fill: { color: ACC } });
  s.addText("Технологический брокер для промышленности", {
    x: MX - 0.02, y: 3.35, w: 9.5, h: 0.55, fontFace: F_SB, fontSize: 24,
    color: NAVY, margin: 0, isTextBox: true,
  });
  s.addText("Работаем от задачи предприятия до внедрённой технологии", {
    x: MX, y: 3.98, w: 9.5, h: 0.42, fontFace: F, fontSize: 18, color: INKD,
    margin: 0, isTextBox: true,
  });
  s.addShape("roundRect", {
    x: MX, y: 4.75, w: 7.6, h: 0.66, rectRadius: 0.09,
    fill: { color: ICEBG }, line: { color: LINE, width: 1 },
  });
  s.addText(ADDRESSEE, {
    x: MX + 0.3, y: 4.75, w: 7.1, h: 0.66, fontFace: F_SB, fontSize: 18,
    color: NAVY, margin: 0, valign: "middle", isTextBox: true,
  });
  s.addText(`Кирилл Уклонский · ${KIRILL_EMAIL}`, {
    x: MX, y: 6.5, w: 7, h: 0.36, fontFace: F_SB, fontSize: 16, color: NAVY,
    margin: 0, isTextBox: true,
  });
}

function statGrid(pres, d, page) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.34;
  const cw = (W - 2 * MX - gap) / 2;
  const bottom = d.thesis ? 6.28 : 7.0;
  const ch = (bottom - 1.62 - gap) / 2;
  d.cards.forEach((c, i) => {
    statCard(s, {
      x: MX + (i % 2) * (cw + gap),
      y: 1.62 + Math.floor(i / 2) * (ch + gap),
      w: cw, h: ch, big: c.big, bigSize: c.bigSize, caption: c.caption,
      src: c.src, accent: (d.accents || [1, 2]).includes(i),
    });
  });
  if (d.thesis) thesis(s, d.thesis, 6.48, 0.6);
  footer(s, page);
}

function statRow3(pres, d, page) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.34;
  const cw = (W - 2 * MX - 2 * gap) / 3;
  d.cards.forEach((c, i) => {
    statCard(s, {
      x: MX + i * (cw + gap), y: 1.62, w: cw, h: 3.35,
      big: c.big, bigSize: c.bigSize || 34, caption: c.caption, src: c.src,
      accent: i === 1,
    });
  });
  if (d.quote) quote(s, d.quote[0], d.quote[1], d.quote[2], 5.35);
  footer(s, page);
}

function numGrid(pres, d, page) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.34;
  const cw = (W - 2 * MX - gap) / 2;
  const bottom = d.note ? 6.5 : 7.0;
  const ch = (bottom - 1.62 - gap) / 2;
  // Три карточки: последняя встаёт по центру нижнего ряда и берёт акцент,
  // иначе правая нижняя четверть слайда остаётся пустой.
  const odd = d.cards.length === 3;
  const accents = d.accents || (odd ? [2] : [1, 3]);
  d.cards.forEach((c, i) => {
    const last = odd && i === 2;
    numCard(s, {
      x: last ? MX + (W - 2 * MX - cw) / 2 : MX + (i % 2) * (cw + gap),
      y: 1.62 + Math.floor(i / 2) * (ch + gap),
      w: cw, h: ch, n: i + 1, head: c.head, sub: c.sub, src: c.src,
      accent: accents.includes(i), headH: c.headH,
    });
  });
  if (d.note) {
    s.addText(d.note, {
      x: MX, y: 6.62, w: W - 2 * MX, h: 0.36, fontFace: F, fontSize: 16,
      italic: true, color: GRAY, margin: 0, isTextBox: true,
    });
  }
  footer(s, page);
}

function cycle(pres, d, page) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const labW = 1.85;
  const gap = 0.22;
  const colW = (W - 2 * MX - labW - 0.15 - 3 * gap) / 4;
  const colX = (i) => MX + labW + 0.15 + i * (colW + gap);
  ["1. Задача", "2. Поиск и проверка", "3. Сделка и поддержка", "4. Внедрение"].forEach((t, i) => {
    s.addShape("rect", { x: colX(i), y: 1.58, w: colW, h: 0.05, fill: { color: ACC } });
    s.addText(t, {
      x: colX(i), y: 1.7, w: colW, h: 0.62, fontFace: F_SB, fontSize: 16,
      color: NAVY, margin: 0, isTextBox: true, valign: "top",
    });
  });
  const rows = [
    { lab: "ПРЕДПРИЯТИЕ", cells: d.rows[0], accent: false },
    { lab: "СИТАЛЛ", cells: d.rows[1], accent: true },
    { lab: "НАУКА", cells: d.rows[2], accent: false },
  ];
  const rh = 1.26;
  const ry = (r) => 2.44 + r * (rh + 0.2);
  rows.forEach((row, r) => {
    s.addText(row.lab, {
      x: MX, y: ry(r), w: labW, h: rh, fontFace: F_SB, fontSize: 14,
      color: row.accent ? NAVY : GRAY, charSpacing: 2, margin: 0,
      valign: "middle", isTextBox: true,
    });
    row.cells.forEach((cell, i) => {
      if (!cell) return;
      s.addShape("roundRect", {
        x: colX(i), y: ry(r), w: colW, h: rh, rectRadius: 0.09,
        fill: { color: "FFFFFF" },
        line: { color: row.accent ? NAVY : LINE, width: row.accent ? 1.75 : 1 },
        shadow: shadow(),
      });
      s.addText(cell, {
        x: colX(i) + 0.18, y: ry(r), w: colW - 0.36, h: rh,
        fontFace: row.accent ? F_SB : F, fontSize: 16, color: INKD,
        margin: 0, valign: "middle", lineSpacingMultiple: 1.05, isTextBox: true,
      });
    });
  });
  footer(s, page);
}

function instrument(pres, d, page) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const bigW = 4.5;
  card(s, MX, 1.62, bigW, 5.15, true);
  s.addShape("rect", { x: MX + 0.36, y: 1.98, w: 0.5, h: 0.06, fill: { color: ACC } });
  s.addText("5–7", {
    x: MX + 0.36, y: 2.2, w: bigW - 0.72, h: 1.5, fontFace: F_SB, fontSize: 76,
    color: NAVY, margin: 0, isTextBox: true,
  });
  s.addText("проверенных решений\nпод задачу предприятия.\nСрок: сутки вместо недель", {
    x: MX + 0.36, y: 3.85, w: bigW - 0.72, h: 1.9, fontFace: F, fontSize: 18,
    color: INKD, margin: 0, lineSpacingMultiple: 1.18, isTextBox: true,
  });
  const rx = MX + bigW + 0.4;
  const rw = W - rx - MX;
  d.feats.forEach((f, i) => {
    const y = 1.62 + i * 1.78;
    card(s, rx, y, rw, 1.56, false);
    s.addShape("rect", { x: rx + 0.32, y: y + 0.24, w: 0.42, h: 0.05, fill: { color: ACC } });
    s.addText(f[0], {
      x: rx + 0.32, y: y + 0.36, w: rw - 0.64, h: 0.38, fontFace: F_SB,
      fontSize: 20, color: NAVY, margin: 0, isTextBox: true,
    });
    s.addText(f[1], {
      x: rx + 0.32, y: y + 0.78, w: rw - 0.64, h: 0.66, fontFace: F,
      fontSize: 16, color: INKD, margin: 0, lineSpacingMultiple: 1.05,
      isTextBox: true, valign: "top",
    });
  });
  footer(s, page);
}

function team(pres, d, page) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.34;
  const cw = (W - 2 * MX - 2 * gap) / 3;
  d.people.forEach((p, i) => {
    const x = MX + i * (cw + gap);
    card(s, x, 1.62, cw, 5.15, i === 2);
    s.addShape("rect", { x: x + 0.32, y: 1.9, w: 0.42, h: 0.05, fill: { color: i === 2 ? ACC : NAVY } });
    s.addText(p.name, {
      x: x + 0.32, y: 2.04, w: cw - 0.64, h: 0.82, fontFace: F_SB, fontSize: 22,
      color: NAVY, margin: 0, lineSpacingMultiple: 1.0, isTextBox: true, valign: "top",
    });
    s.addText(p.role.toUpperCase(), {
      x: x + 0.32, y: 2.9, w: cw - 0.64, h: 0.3, fontFace: F_SB, fontSize: 13,
      color: ACC, charSpacing: 2, margin: 0, isTextBox: true,
    });
    s.addText(
      p.items.map((t, j) => ({
        text: t,
        options: {
          bullet: { code: "2022", indent: 12 },
          color: INKD,
          breakLine: j < p.items.length - 1,
          paraSpaceAfter: 8,
        },
      })),
      {
        x: x + 0.34, y: 3.3, w: cw - 0.62, h: 3.3, fontFace: F, fontSize: 16,
        margin: 0, lineSpacingMultiple: 1.08, valign: "top", isTextBox: true,
      }
    );
  });
  footer(s, page);
}

// Финал: две плитки контактов сооснователей.
// Плитка без почты (пустая константа) показывает имя, роль и заслуги —
// строка с адресом появляется сама, как только константа заполнена.
function contacts(pres, d, page) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.5;
  const cw = (W - 2 * MX - gap) / 2;
  d.people.forEach((p, i) => {
    const x = MX + i * (cw + gap);
    card(s, x, 1.62, cw, 5.15, i === 0);
    s.addShape("rect", { x: x + 0.4, y: 1.98, w: 0.5, h: 0.06, fill: { color: i === 0 ? ACC : NAVY } });
    s.addText(p.name, {
      x: x + 0.4, y: 2.2, w: cw - 0.8, h: 0.55, fontFace: F_SB, fontSize: 30,
      color: NAVY, margin: 0, isTextBox: true,
    });
    s.addText(p.role.toUpperCase(), {
      x: x + 0.4, y: 2.82, w: cw - 0.8, h: 0.3, fontFace: F_SB, fontSize: 13,
      color: ACC, charSpacing: 2, margin: 0, isTextBox: true,
    });
    if (p.email) {
      s.addText(p.email, {
        x: x + 0.4, y: 3.25, w: cw - 0.8, h: 0.42, fontFace: F_SB, fontSize: 20,
        color: NAVY, underline: true, hyperlink: { url: `mailto:${p.email}` },
        margin: 0, isTextBox: true,
      });
    }
    s.addText(
      p.items.map((t, j) => ({
        text: t,
        options: {
          bullet: { code: "2022", indent: 12 },
          color: INKD,
          breakLine: j < p.items.length - 1,
          paraSpaceAfter: 8,
        },
      })),
      {
        x: x + 0.42, y: p.email ? 3.9 : 3.3, w: cw - 0.84,
        h: p.email ? 2.7 : 3.3, fontFace: F, fontSize: 16,
        margin: 0, lineSpacingMultiple: 1.08, valign: "top", isTextBox: true,
      }
    );
  });
  footer(s, page);
}

// ------------------------------------------------------------------- данные

const WHO = {
  kicker: "Кто мы",
  title: "Внешний офис трансфера технологий",
  cards: [
    { head: "От задачи, а не от разработки", sub: "Начинаем там, где предприятие теряет деньги" },
    { head: "Отвечаем деньгами за внедрение", sub: "Часть вознаграждения только при результате" },
    { head: "Господдержку собираем сами", sub: "Документацию готовим мы, а не сотрудники вуза" },
    { head: "ЦТТ приводим заказчика", sub: "Центры трансфера и инжиниринговые центры для нас партнёры и исполнители" },
  ],
};

const CYCLE = {
  kicker: "Как это работает",
  title: "Один цикл: от задачи завода до внедрения",
  rows: [
    ["Обозначает\nпотребность", "Выбирает из списка", "Подписывает договор", "Получает работающую\nтехнологию"],
    ["Переводим\nв техзадание", "Ищем и проверяем\nрешения и поставщиков", "Структурируем\nи ведём сделку", "Сопровождаем\nвнедрение до запуска"],
    [null, "Даёт решения", "Исполняет НИОКР", "Дорабатывает\nпод производство"],
  ],
};

const TEAM = {
  kicker: "Команда",
  title: "Доводим проекты до внедрения",
  people: [
    {
      name: "Кирилл\nУклонский",
      role: "Со-основатель",
      items: [
        "«ФинАналитик», Роспатент № 2024618827: учёт 10 млрд ₽ в год",
        "Платформа «Больше, чем путешествие»: 80 000 пользователей",
        "РАНХиГС, госуправление. Преподаватель РЭУ им. Плеханова",
      ],
    },
    {
      name: "Данила\nКашев",
      role: "Со-основатель",
      items: [
        "X5 Retail: внедрение новых технологий в рознице",
        "ФГУП «ЗащитаИнфоТранс»: реестр отечественного ПО",
        "Студия разработки: 4 → 15 человек, 50+ проектов",
      ],
    },
    {
      name: "Дмитрий\nПересторонин",
      role: "Региональный партнёр, Свердловская область",
      items: [
        "Привлечение спонсирования для спортивных мероприятий федерального и международного уровня",
        "Инициатор проектов по созданию и развитию индустриальных парков в Свердловской области",
      ],
    },
  ],
};

const SCOUT = {
  kicker: "Чем работаем",
  title: "Ситалл Скаут: платформа подбора решений",
  feats: [
    ["Проверяет человек", "Инструмент даёт варианты, подтверждает профильный специалист"],
    ["Открытые данные", "Публикации, патенты, гранты, российский и мировой веб"],
    ["Показываем в деле", "Демонстрация на вашей задаче по запросу"],
  ],
};

// --------------------------------------------------------------- сборка дека

function build() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.author = "Ситалл";
  pres.title = "Ситалл — предложение о партнёрстве Площадке IT-Заказов";

  cover(pres);

  statGrid(pres, {
    kicker: "Задача государства",
    title: "Деньги на науку есть. Внедрять технологии некому",
    cards: [
      { big: "2% ВВП", caption: "цель по затратам на науку к 2030 году", src: ["ИСИЭЗ НИУ ВШЭ", "https://issek.hse.ru/news/849710120.html"] },
      { big: "×2", caption: "рост частных вложений бизнеса в науку — цель к 2030 году", src: ["ИСИЭЗ НИУ ВШЭ", "https://issek.hse.ru/news/849710120.html"] },
      { big: "1,9 трлн ₽", caption: "нацпроекты технологического лидерства к 2028 году", src: ["ComNews", "https://www.comnews.ru/content/241396/2025-10-01/2025-w40/1008/19-trilliona-rubley-potratyat-nacproekty-tekhnologicheskogo-liderstva-tri-goda"] },
      { big: "2,6 млн", caption: "дефицит кадров. Ставка на производительность", src: ["РБК", "https://www.rbc.ru/economics/24/12/2024/676a7e719a79473dc5db384f"] },
    ],
  }, 2);

  statRow3(pres, {
    kicker: "Разрыв",
    title: "Наука не доходит до производства",
    cards: [
      { big: "80%+", caption: "патентов не доходят до применения", src: ["Wharton", "https://knowledge.wharton.upenn.edu/article/patents-avoid-failure/"] },
      { big: "70%", caption: "пилотных проектов не выходят в масштаб", src: ["McKinsey", "https://www.mckinsey.com/capabilities/operations/our-insights/capturing-the-true-value-of-industry-four-point-zero"] },
      { big: "60", caption: "компаний в стране берут льготу на НИОКР", src: ["Решетников, ПМЭФ", "https://tass.ru/ekonomika/24246605"] },
    ],
    quote: [
      "Чтобы наука и бизнес нашли друг друга,\nнужен технологический брокер»",
      "Юрий Зубов, руководитель Роспатента",
      "https://rospatent.gov.ru/ru/news/na-pmef-2023-obsudili-tehnologicheskoe-predprinimatelstvo-15062023",
    ],
  }, 3);

  numGrid(pres, WHO, 4);
  cycle(pres, CYCLE, 5);
  instrument(pres, SCOUT, 6);

  numGrid(pres, {
    kicker: "Где мы дополняем",
    title: "Вы находите исполнителя. Мы доводим до внедрения",
    cards: [
      { head: "Заказчик приходит с задачей", sub: "Не с готовым ТЗ. Переводим задачу предприятия в техзадание, с которым можно искать исполнителя" },
      { head: "Часть задач решается не кодом", sub: "Технология, материал, процесс: под них находим научное решение и того, кто его сделает" },
      { head: "Сделку структурируем", sub: "Договор с научной организацией, этапы и приёмка, заявка на господдержку — наша зона" },
      { head: "Отвечаем за внедрение", sub: "Сопровождаем до запуска на производстве. Часть вознаграждения — только при результате" },
    ],
  }, 7);

  numGrid(pres, {
    kicker: "Предложение",
    title: "Что мы закрываем для вашего заказчика",
    cards: [
      { head: "Непрофильная задача — нам", sub: "Пришла к вам, но решается не кодом. Механику передачи вы описали сами", src: ["Площадка IT-Заказов", PLOSHADKA] },
      { head: "Подбор с проверкой", sub: "5–7 решений под задачу, каждое со ссылкой на первоисточник и заключением профильного учёного" },
      { head: "Сделка и господдержка", sub: "Договор, этапы, приёмка и заявка на софинансирование по проекту вашего заказчика" },
      { head: "Внедрение до запуска", sub: "Ведём проект на производстве. ИТ-часть таких проектов возвращаем на площадку" },
    ],
  }, 8);

  numGrid(pres, {
    kicker: "Как начнём",
    title: "Один проект вашего заказчика — от задачи до внедрения",
    cards: [
      { head: "Одна задача на пробу", sub: "Берём непрофильную задачу из вашего потока: диагностика и подбор за наш счёт" },
      { head: "Результат — заказчику", sub: "Показываем подбор ему, а не нам в отчёт. Не подошло ни одно решение — говорим об этом прямо" },
      { head: "Дальше сделка и внедрение", sub: "Договор, господдержка, сопровождение до запуска — это уже наша работа, не ваша" },
      { head: "Условия по итогу", sub: "Порядок передачи заказов и вознаграждения фиксируем письменно после первого проекта" },
    ],
    note: "Вложений с вашей стороны нет. Условия вознаграждения обсуждаем голосом",
  }, 9);

  contacts(pres, {
    kicker: "Контакты",
    title: "С кем разговаривать",
    people: [
      {
        name: "Кирилл Уклонский",
        role: "Со-основатель",
        email: KIRILL_EMAIL,
        items: [
          "«ФинАналитик», Роспатент № 2024618827: учёт 10 млрд ₽ в год",
          "РАНХиГС, госуправление. Преподаватель РЭУ им. Плеханова",
          "Господдержка, структурирование сделок, переговоры с властью",
        ],
      },
      {
        name: "Данила Кашев",
        role: "Со-основатель",
        email: KASHEV_EMAIL,
        items: [
          "X5 Retail: внедрение новых технологий в рознице",
          "ФГУП «ЗащитаИнфоТранс»: реестр отечественного ПО",
          "Студия разработки: 4 → 15 человек, 50+ проектов",
        ],
      },
    ],
  }, 10);

  return pres.writeFile({ fileName: path.join(__dirname, "sitall-deck-ploshadka.pptx") });
}

build().then((f) => console.log("Собрано:", f));
