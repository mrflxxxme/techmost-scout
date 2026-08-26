// Генератор двух деков ХМАО: Югорский рыбоводный завод и Депнедра Югры.
// Дизайн — печатная версия 3 (решение Р-29): белый фон, цвет в рамках
// карточек и цифрах, NAVY/GOLD, Segoe UI. Тексты слайдов — deck-texts.md:
// правки вносить там, затем переносить сюда и пересобирать.
// Запуск: node gen-hmao-decks.js  (создаёт sitall-deck-urz.pptx и
// sitall-deck-depnedra.pptx рядом со скриптом).

const pptxgen = require("pptxgenjs");
const path = require("path");

const NAVY = "1E2761";
const INKD = "1B2140";
const GRAY = "6B7699";
const GOLD = "C9A24B";
const LINE = "C9D3EA"; // серо-голубая окантовка обычных карточек
const ICEBG = "E3EAF8";
const F = "Segoe UI";
const F_SB = "Segoe UI Semibold";
const W = 13.333;
const H = 7.5;
const MX = 0.42; // левое поле

// ---------------------------------------------------------------- помощники

function footer(slide, page, total) {
  slide.addShape("rect", { x: 0, y: H - 0.06, w: W, h: 0.06, fill: { color: NAVY } });
  slide.addText("Ситалл", {
    x: MX, y: H - 0.36, w: 2, h: 0.24, fontFace: F, fontSize: 7.5,
    color: GRAY, margin: 0, isTextBox: true,
  });
  slide.addText(`${String(page).padStart(2, "0")} / ${total}`, {
    x: W - 1.4, y: H - 0.36, w: 1, h: 0.24, fontFace: F, fontSize: 7.5,
    color: GRAY, align: "right", margin: 0, isTextBox: true,
  });
}

function kickerTitle(slide, kicker, title) {
  slide.addText(kicker.toUpperCase(), {
    x: MX, y: 0.22, w: W - 2 * MX, h: 0.26, fontFace: F_SB, fontSize: 9.5,
    color: GOLD, charSpacing: 3, margin: 0, isTextBox: true,
  });
  slide.addText(title, {
    x: MX - 0.02, y: 0.5, w: W - 2 * MX, h: 0.52, fontFace: F_SB, fontSize: 26,
    color: NAVY, margin: 0, isTextBox: true,
  });
  slide.addShape("rect", { x: MX, y: 1.12, w: 1.05, h: 0.045, fill: { color: GOLD } });
}

// Карточка с крупной цифрой. src: [текст, url] или null.
function statCard(slide, o) {
  const accent = !!o.accent;
  slide.addShape("roundRect", {
    x: o.x, y: o.y, w: o.w, h: o.h, rectRadius: 0.07,
    fill: { color: "FFFFFF" },
    line: { color: accent ? GOLD : LINE, width: accent ? 1.75 : 1 },
  });
  const pad = 0.26;
  slide.addShape("rect", {
    x: o.x + pad, y: o.y + 0.24, w: 0.3, h: 0.038,
    fill: { color: accent ? GOLD : NAVY },
  });
  slide.addText(o.big, {
    x: o.x + pad, y: o.y + 0.34, w: o.w - 2 * pad, h: 0.62,
    fontFace: F_SB, fontSize: o.bigSize || 30, color: accent ? GOLD : NAVY,
    margin: 0, isTextBox: true,
  });
  slide.addText(o.caption, {
    x: o.x + pad, y: o.y + 0.98, w: o.w - 2 * pad, h: o.h - 1.28,
    fontFace: F, fontSize: 10.5, color: INKD, margin: 0,
    lineSpacingMultiple: 1.12, isTextBox: true, valign: "top",
  });
  if (o.src) {
    slide.addText(
      [
        { text: "Источник: ", options: { italic: true } },
        { text: o.src[0], options: { italic: true, underline: true, hyperlink: { url: o.src[1] } } },
      ],
      {
        x: o.x + pad, y: o.y + o.h - 0.32, w: o.w - 2 * pad, h: 0.22,
        fontFace: F, fontSize: 7.5, color: GRAY, margin: 0, isTextBox: true,
      }
    );
  }
}

// Карточка с номером в кружке.
function numCard(slide, o) {
  const accent = !!o.accent;
  slide.addShape("roundRect", {
    x: o.x, y: o.y, w: o.w, h: o.h, rectRadius: 0.07,
    fill: { color: "FFFFFF" },
    line: { color: accent ? GOLD : LINE, width: accent ? 1.75 : 1 },
  });
  const pad = 0.26;
  slide.addShape("ellipse", {
    x: o.x + pad, y: o.y + 0.22, w: 0.32, h: 0.32,
    fill: { color: accent ? GOLD : NAVY },
  });
  slide.addText(String(o.n), {
    x: o.x + pad, y: o.y + 0.22, w: 0.32, h: 0.32, fontFace: F_SB,
    fontSize: 12, color: "FFFFFF", align: "center", valign: "middle",
    margin: 0, isTextBox: true,
  });
  slide.addText(o.head, {
    x: o.x + pad, y: o.y + 0.62, w: o.w - 2 * pad, h: 0.6,
    fontFace: F_SB, fontSize: 13.5, color: NAVY, margin: 0,
    lineSpacingMultiple: 1.05, isTextBox: true, valign: "top",
  });
  slide.addText(o.sub, {
    x: o.x + pad, y: o.y + (o.subY || 1.16), w: o.w - 2 * pad, h: o.h - (o.subY || 1.16) - 0.16,
    fontFace: F, fontSize: 10, color: INKD, margin: 0,
    lineSpacingMultiple: 1.12, isTextBox: true, valign: "top",
  });
}

// Тезис-полоса внизу слайда: вертикальная навигационная черта + фраза.
function thesis(slide, text, y) {
  slide.addShape("rect", { x: MX, y: y, w: 0.055, h: 0.58, fill: { color: NAVY } });
  slide.addText(text, {
    x: MX + 0.22, y: y - 0.02, w: W - 2 * MX - 0.3, h: 0.66, fontFace: F_SB,
    fontSize: 15, color: NAVY, margin: 0, valign: "middle", isTextBox: true,
  });
}

// ------------------------------------------------------------------ обложка

function cover(pres, d, total) {
  const s = pres.addSlide();
  // орбы контуром справа
  s.addShape("ellipse", { x: 9.4, y: -1.6, w: 5.4, h: 5.4, fill: { type: "none" }, line: { color: LINE, width: 1 } });
  s.addShape("ellipse", { x: 11.0, y: 1.1, w: 3.3, h: 3.3, fill: { type: "none" }, line: { color: GOLD, width: 1.25 } });
  s.addShape("ellipse", { x: 10.1, y: 4.4, w: 4.4, h: 4.4, fill: { type: "none" }, line: { color: LINE, width: 1 } });
  s.addText("АВГУСТ 2026 · КОНФИДЕНЦИАЛЬНО", {
    x: MX, y: 0.34, w: 7, h: 0.26, fontFace: F_SB, fontSize: 9.5, color: GOLD,
    charSpacing: 3, margin: 0, isTextBox: true,
  });
  s.addText("Ситалл", {
    x: MX - 0.04, y: 1.1, w: 8, h: 1.3, fontFace: F_SB, fontSize: 54,
    color: NAVY, margin: 0, isTextBox: true,
  });
  s.addShape("rect", { x: MX, y: 2.62, w: 1.05, h: 0.06, fill: { color: GOLD } });
  s.addText("Технологический брокер\nдля промышленности", {
    x: MX - 0.02, y: 2.9, w: 8, h: 1.0, fontFace: F, fontSize: 21, color: NAVY,
    margin: 0, lineSpacingMultiple: 1.1, isTextBox: true,
  });
  s.addText("От задачи предприятия до внедрённой технологии", {
    x: MX, y: 4.05, w: 8, h: 0.35, fontFace: F, fontSize: 12.5, color: INKD,
    margin: 0, isTextBox: true,
  });
  // адресная плашка
  s.addShape("roundRect", {
    x: MX, y: 4.62, w: 6.4, h: 0.56, rectRadius: 0.07,
    fill: { color: ICEBG }, line: { color: LINE, width: 1 },
  });
  s.addText(d.addressee, {
    x: MX + 0.24, y: 4.62, w: 6.0, h: 0.56, fontFace: F_SB, fontSize: 12.5,
    color: NAVY, margin: 0, valign: "middle", isTextBox: true,
  });
  s.addText(
    "Ситалл: стекло, которому наука дала прочность. 1958 год, лаборатория Китайгородского",
    {
      x: MX, y: 5.7, w: 8.5, h: 0.3, fontFace: F, fontSize: 8, italic: true,
      color: GRAY, margin: 0, isTextBox: true,
    }
  );
  s.addText("Кирилл Уклонский · uklonskiy.k@ya.ru", {
    x: MX, y: 6.55, w: 6, h: 0.3, fontFace: F_SB, fontSize: 10.5, color: NAVY,
    margin: 0, isTextBox: true,
  });
  s.addShape("rect", { x: 0, y: H - 0.06, w: W, h: 0.06, fill: { color: NAVY } });
}

// -------------------------------------------------- слайд из четырёх карточек

function statGrid(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const cw = (W - 2 * MX - 0.3) / 2;
  const ch = 2.4;
  const y0 = 1.5;
  d.cards.forEach((c, i) => {
    statCard(s, {
      x: MX + (i % 2) * (cw + 0.3),
      y: y0 + Math.floor(i / 2) * (ch + 0.28),
      w: cw, h: ch, big: c.big, bigSize: c.bigSize, caption: c.caption,
      src: c.src, accent: i % 3 === 1,
    });
  });
  if (d.thesis) thesis(s, d.thesis, 6.6);
  footer(s, page, total);
  return s;
}

// три широкие карточки в ряд + тезис
function statRow3(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const cw = (W - 2 * MX - 0.6) / 3;
  const ch = 3.6;
  d.cards.forEach((c, i) => {
    statCard(s, {
      x: MX + i * (cw + 0.3), y: 1.55, w: cw, h: ch,
      big: c.big, bigSize: c.bigSize || 22, caption: c.caption, src: c.src,
      accent: i === 1,
    });
  });
  if (d.thesis) thesis(s, d.thesis, 5.7);
  footer(s, page, total);
}

// четыре нумерованные карточки 2х2
function numGrid(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const cw = (W - 2 * MX - 0.3) / 2;
  const ch = d.thesis ? 2.35 : 2.6;
  const y0 = 1.5;
  d.cards.forEach((c, i) => {
    numCard(s, {
      x: MX + (i % 2) * (cw + 0.3), y: y0 + Math.floor(i / 2) * (ch + 0.28),
      w: cw, h: ch, n: i + 1, head: c.head, sub: c.sub, accent: i % 2 === 1,
    });
  });
  if (d.thesis) thesis(s, d.thesis, 6.62);
  footer(s, page, total);
}

// --------------------------------------------------------------- схема цикла

function cycle(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const labX = MX;
  const labW = 1.55;
  const colW = (W - MX - 0.42 - labW - 0.66) / 4;
  const colX = (i) => labX + labW + 0.1 + i * (colW + 0.17);
  const steps = ["1. Задача", "2. Поиск и проверка", "3. Сделка и поддержка", "4. Внедрение"];
  steps.forEach((t, i) => {
    s.addShape("rect", { x: colX(i), y: 1.42, w: colW, h: 0.035, fill: { color: GOLD } });
    s.addText(t, {
      x: colX(i), y: 1.5, w: colW, h: 0.28, fontFace: F_SB, fontSize: 11,
      color: NAVY, margin: 0, isTextBox: true,
    });
  });
  const rows = [
    { lab: "ПРЕДПРИЯТИЕ", cells: d.rows[0], accent: false },
    { lab: "СИТАЛЛ", cells: d.rows[1], accent: true },
    { lab: "НАУКА", cells: d.rows[2], accent: false },
  ];
  const rh = 0.95;
  const ry = (r) => 1.95 + r * (rh + 0.18);
  rows.forEach((row, r) => {
    s.addText(row.lab, {
      x: labX, y: ry(r), w: labW, h: rh, fontFace: F_SB, fontSize: 9,
      color: row.accent ? NAVY : GRAY, charSpacing: 2, margin: 0,
      valign: "middle", isTextBox: true,
    });
    row.cells.forEach((cell, i) => {
      if (!cell) return;
      s.addShape("roundRect", {
        x: colX(i), y: ry(r), w: colW, h: rh, rectRadius: 0.07,
        fill: { color: "FFFFFF" },
        line: { color: row.accent ? NAVY : LINE, width: row.accent ? 1.75 : 1 },
      });
      s.addText(cell, {
        x: colX(i) + 0.14, y: ry(r), w: colW - 0.28, h: rh,
        fontFace: row.accent ? F_SB : F, fontSize: 10.5, color: INKD,
        margin: 0, valign: "middle", lineSpacingMultiple: 1.05, isTextBox: true,
      });
    });
  });
  s.addText("ГОСУДАРСТВЕННЫЕ ИНСТРУМЕНТЫ, КОТОРЫЕ МЫ ПОДКЛЮЧАЕМ", {
    x: MX, y: 5.55, w: W - 2 * MX, h: 0.24, fontFace: F_SB, fontSize: 9,
    color: GOLD, charSpacing: 2.5, margin: 0, isTextBox: true,
  });
  const tw = (W - 2 * MX - 0.9) / 4;
  d.tools.forEach((t, i) => {
    const x = MX + i * (tw + 0.3);
    s.addText(t[0], {
      x, y: 5.92, w: tw, h: 0.3, fontFace: F_SB, fontSize: 10.5, color: NAVY,
      margin: 0, isTextBox: true,
    });
    s.addText(t[1], {
      x, y: 6.24, w: tw, h: 0.5, fontFace: F, fontSize: 9.5, color: GRAY,
      margin: 0, lineSpacingMultiple: 1.05, isTextBox: true,
    });
  });
  footer(s, page, total);
}

// ---------------------------------------------------------------- инструмент

function instrument(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  statCard(s, {
    x: MX, y: 1.55, w: 4.1, h: 4.35, big: "5–7", bigSize: 48,
    caption: "проверенных решений под задачу предприятия.\nСрок: сутки вместо недель",
    src: null, accent: true,
  });
  // подпись в большой карточке ниже цифры
  const rx = MX + 4.4;
  const rw = W - rx - 0.42;
  d.feats.forEach((f, i) => {
    const y = 1.55 + i * 1.52;
    s.addShape("roundRect", {
      x: rx, y, w: rw, h: 1.32, rectRadius: 0.07, fill: { color: "FFFFFF" },
      line: { color: LINE, width: 1 },
    });
    s.addShape("rect", { x: rx + 0.26, y: y + 0.22, w: 0.3, h: 0.038, fill: { color: GOLD } });
    s.addText(f[0], {
      x: rx + 0.26, y: y + 0.32, w: rw - 0.52, h: 0.32, fontFace: F_SB,
      fontSize: 13, color: NAVY, margin: 0, isTextBox: true,
    });
    s.addText(f[1], {
      x: rx + 0.26, y: y + 0.66, w: rw - 0.52, h: 0.5, fontFace: F,
      fontSize: 10.5, color: INKD, margin: 0, isTextBox: true,
    });
  });
  thesis(s, d.thesis, 6.35);
  footer(s, page, total);
}

// --------------------------------------------- шесть карточек-направлений 3х2

function dirGrid(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const cw = (W - 2 * MX - 0.6) / 3;
  const ch = 2.15;
  d.cards.forEach((c, i) => {
    const x = MX + (i % 3) * (cw + 0.3);
    const y = 1.5 + Math.floor(i / 3) * (ch + 0.26);
    numCard(s, { x, y, w: cw, h: ch, n: i + 1, head: c[0], sub: c[1], accent: i % 3 === 1, subY: 1.22 });
  });
  s.addText(d.note, {
    x: MX, y: 6.35, w: W - 2 * MX, h: 0.3, fontFace: F, fontSize: 10.5,
    italic: true, color: GRAY, margin: 0, isTextBox: true,
  });
  footer(s, page, total);
}

// ----------------------------------------------------- три колонки «ценность»

function valueCols(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const cw = (W - 2 * MX - 0.6) / 3;
  d.cols.forEach((col, i) => {
    const x = MX + i * (cw + 0.3);
    const dark = i === 0;
    s.addShape("roundRect", {
      x, y: 1.5, w: cw, h: 4.9, rectRadius: 0.07,
      fill: { color: dark ? NAVY : "FFFFFF" },
      line: { color: dark ? NAVY : i === 1 ? GOLD : LINE, width: dark ? 0 : i === 1 ? 1.75 : 1 },
    });
    s.addText(col.head.toUpperCase(), {
      x: x + 0.28, y: 1.78, w: cw - 0.56, h: 0.3, fontFace: F_SB, fontSize: 10,
      color: dark ? GOLD : GRAY, charSpacing: 2, margin: 0, isTextBox: true,
    });
    s.addText(
      col.items.map((t, j) => ({
        text: t,
        options: { bullet: { code: "2022", indent: 10 }, breakLine: j < col.items.length - 1, paraSpaceAfter: 8 },
      })),
      {
        x: x + 0.28, y: 2.2, w: cw - 0.5, h: 4.0, fontFace: F, fontSize: 11,
        color: dark ? "FFFFFF" : INKD, margin: 0, lineSpacingMultiple: 1.12,
        valign: "top", isTextBox: true,
      }
    );
  });
  footer(s, page, total);
}

// ------------------------------------------------------- вопросы (4 карточки)

function qa(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const cw = (W - 2 * MX - 0.3) / 2;
  const ch = 2.55;
  d.items.forEach((it, i) => {
    const x = MX + (i % 2) * (cw + 0.3);
    const y = 1.5 + Math.floor(i / 2) * (ch + 0.28);
    const accent = i % 3 === 1;
    s.addShape("roundRect", {
      x, y, w: cw, h: ch, rectRadius: 0.07, fill: { color: "FFFFFF" },
      line: { color: accent ? GOLD : LINE, width: accent ? 1.75 : 1 },
    });
    s.addShape("ellipse", { x: x + 0.26, y: y + 0.26, w: 0.32, h: 0.32, fill: { color: accent ? GOLD : NAVY } });
    s.addText(String(i + 1), {
      x: x + 0.26, y: y + 0.26, w: 0.32, h: 0.32, fontFace: F_SB, fontSize: 12,
      color: "FFFFFF", align: "center", valign: "middle", margin: 0, isTextBox: true,
    });
    s.addText(it[0], {
      x: x + 0.72, y: y + 0.2, w: cw - 1.0, h: 0.86, fontFace: F_SB,
      fontSize: 12.5, color: NAVY, margin: 0, lineSpacingMultiple: 1.05,
      valign: "middle", isTextBox: true,
    });
    s.addText(it[1], {
      x: x + 0.26, y: y + 1.18, w: cw - 0.52, h: ch - 1.36, fontFace: F,
      fontSize: 10.5, color: INKD, margin: 0, lineSpacingMultiple: 1.12,
      valign: "top", isTextBox: true,
    });
  });
  footer(s, page, total);
}

// ---------------------------------------------------------------- предложение

function proposal(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const lw = 6.6;
  s.addText(d.leftHead.toUpperCase(), {
    x: MX, y: 1.4, w: lw, h: 0.26, fontFace: F_SB, fontSize: 9.5, color: GOLD,
    charSpacing: 2.5, margin: 0, isTextBox: true,
  });
  const rh = 0.66;
  d.leftRows.forEach((row, i) => {
    const y = 1.78 + i * (rh + 0.15);
    s.addShape("roundRect", {
      x: MX, y, w: lw, h: rh, rectRadius: 0.07, fill: { color: "FFFFFF" },
      line: { color: LINE, width: 1 },
    });
    s.addText(row[0], {
      x: MX + 0.26, y, w: lw - 2.0, h: rh, fontFace: F, fontSize: 12,
      color: INKD, margin: 0, valign: "middle", isTextBox: true,
    });
    s.addText(row[1], {
      x: MX + lw - 1.76, y, w: 1.5, h: rh, fontFace: F_SB, fontSize: 16,
      color: GOLD, align: "right", margin: 0, valign: "middle", isTextBox: true,
    });
  });
  s.addText(d.leftNote, {
    x: MX, y: 1.78 + d.leftRows.length * (rh + 0.15) + 0.08, w: lw, h: 0.3,
    fontFace: F_SB, fontSize: 11.5, color: NAVY, margin: 0, isTextBox: true,
  });
  const rx = MX + lw + 0.5;
  const rw = W - rx - 0.42;
  s.addText("ЧТО ПРОСИМ", {
    x: rx, y: 1.4, w: rw, h: 0.26, fontFace: F_SB, fontSize: 9.5, color: GOLD,
    charSpacing: 2.5, margin: 0, isTextBox: true,
  });
  d.asks.forEach((a, i) => {
    const y = 1.78 + i * 0.78;
    s.addShape("ellipse", { x: rx, y: y + 0.04, w: 0.32, h: 0.32, fill: { color: NAVY } });
    s.addText(String(i + 1), {
      x: rx, y: y + 0.04, w: 0.32, h: 0.32, fontFace: F_SB, fontSize: 12,
      color: "FFFFFF", align: "center", valign: "middle", margin: 0, isTextBox: true,
    });
    s.addText(a, {
      x: rx + 0.48, y: y - 0.05, w: rw - 0.48, h: 0.72, fontFace: F_SB,
      fontSize: 12, color: NAVY, margin: 0, lineSpacingMultiple: 1.05,
      valign: "middle", isTextBox: true,
    });
  });
  const nsY = 1.78 + d.asks.length * 0.78 + 0.18;
  s.addShape("roundRect", {
    x: rx, y: nsY, w: rw, h: 1.5, rectRadius: 0.07, fill: { color: "FFFFFF" },
    line: { color: GOLD, width: 1.75 },
  });
  s.addText("СЛЕДУЮЩИЙ ШАГ", {
    x: rx + 0.26, y: nsY + 0.18, w: rw - 0.52, h: 0.24, fontFace: F_SB,
    fontSize: 9, color: GOLD, charSpacing: 2.5, margin: 0, isTextBox: true,
  });
  s.addText(d.nextStep, {
    x: rx + 0.26, y: nsY + 0.46, w: rw - 0.52, h: 0.95, fontFace: F,
    fontSize: 10.5, color: INKD, margin: 0, lineSpacingMultiple: 1.12,
    valign: "top", isTextBox: true,
  });
  s.addText("Кирилл Уклонский · uklonskiy.k@ya.ru", {
    x: MX, y: 6.65, w: 6, h: 0.3, fontFace: F_SB, fontSize: 10.5, color: NAVY,
    margin: 0, isTextBox: true,
  });
  footer(s, page, total);
}

// ============================================================== содержание

const WHO = {
  kicker: "Кто мы",
  title: "Внешний офис трансфера технологий",
  cards: [
    { head: "От задачи, а не от разработки", sub: "Начинаем там, где предприятие теряет деньги" },
    { head: "Отвечаем деньгами за внедрение", sub: "Часть вознаграждения — только при результате" },
    { head: "Господдержку собираем сами", sub: "Заявки готовим мы, а не сотрудники предприятия" },
    { head: "Науке приводим заказ", sub: "Институты и инжиниринговые центры — партнёры и исполнители" },
  ],
};

const CYCLE_ROWS = [
  ["называет задачу\nи её цену", "выбирает из списка", "подписывает договор", "получает технологию"],
  ["переводит\nв техзадание", "ищет и проверяет\nс учёными", "собирает сделку\nи поддержку", "ведёт до пуска,\nотвечает за срок"],
  [null, "даёт решения", "исполняет НИОКР", "дорабатывает\nпод производство"],
];

const TOOLS_BASE = [
  ["Постановление № 218", "до 70% стоимости исследований"],
  ["Коэффициент 2", "по налогу на прибыль, без конкурса"],
  ["Грант на доращивание", "до 250 млн ₽"],
];

// -------------------------------------------------------------------- Дек А

function buildUrz() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.author = "Ситалл";
  pres.title = "Ситалл — для Югорского рыбоводного завода";
  const total = 10;

  cover(pres, { addressee: "Для АО «Югорский рыбоводный завод»" }, total);

  statGrid(pres, {
    kicker: "Контекст",
    title: "Спрос на воспроизводство растёт быстрее технологий",
    cards: [
      { big: "60 млн", caption: "мальков выпустил один «РН-Юганскнефтегаз» с начала 2026 года", src: ["Ведомости", "https://www.vedomosti.ru/press_releases/2026/07/29/rn-yuganskneftegaz-vipustil-v-reki-ob-irtishskogo-basseina-rekordnoe-kolichestvo-malkov-tsennih-porod-rib"] },
      { big: "×10–30", caption: "падение биомассы крупных сиговых Оби; вылов муксуна и нельмы запрещён с 2014 года", src: ["Fishnews", "https://fishnews.ru/news/46450"] },
      { big: "110 млн", caption: "шт. молоди в год — потребность, названная в Совете Федерации, при выпуске по Югре 24,7 млн шт. в 2025", src: ["ТАСС", "https://tass.ru/ekonomika/25588677"] },
      { big: "Без денег", bigSize: 26, caption: "программа восстановления сиговых по поручению Президента одобрена, финансирование не определено", src: ["СенатИнформ", "https://senatinform.ru/news/regiony_prosyat_rasshirit_programmu_vosstanovleniya_populyatsii_ryby_v_obi_i_irtyshe_/"] },
    ],
  }, 2, total);

  statRow3(pres, {
    kicker: "Вызовы отрасли",
    title: "Три технологических узких места аквакультуры Севера",
    cards: [
      { big: "до 90%", caption: "Корма. Столько кормов для ценных видов давал импорт; ветеринарные барьеры и срывы поставок остаются", src: ["Росрыболовство", "https://fish.gov.ru/news/2024/11/15/ilya-shestakov-rossijskie-predpriyatiya-narashhivayut-obemy-proizvodstva-rybnyh-kormov/"] },
      { big: "Дефицит", bigSize: 22, caption: "Посадочный материал. Полноценных маточных стад муксуна не хватает; наука требует генетического менеджмента", src: ["«Научная Россия»", "https://scientificrussia.ru/articles/kakova-tsena-tsennyh-vidov-ryb"] },
      { big: "~10%", caption: "Выживаемость личинки при выпуске. Зачёт растёт с навеской — нужны технологии подращивания", src: ["«Научная Россия»", "https://scientificrussia.ru/articles/kakova-tsena-tsennyh-vidov-ryb"] },
    ],
    thesis: "За каждым узким местом — готовые научные решения, которые пока не дошли до производства",
  }, 3, total);

  numGrid(pres, WHO, 4, total);

  cycle(pres, {
    kicker: "Как это работает",
    title: "Один цикл: от задачи до внедрения",
    rows: CYCLE_ROWS,
    tools: [...TOOLS_BASE, ["Субсидии Росрыболовства", "поддержка аквакультуры"]],
  }, 5, total);

  instrument(pres, {
    kicker: "Чем работаем",
    title: "Ситалл Скаут: подбор решений за сутки",
    feats: [
      ["Проверяет человек", "Инструмент даёт черновик, решение подтверждает учёный"],
      ["Открытые данные", "Публикации, патенты, гранты, российский и мировой веб"],
      ["Показываем в деле", "Демонстрация на вашей задаче — по запросу"],
    ],
    thesis: "Инструмент разработан и работает",
  }, 6, total);

  dirGrid(pres, {
    kicker: "Направления поиска",
    title: "Что мы можем найти под задачи завода",
    cards: [
      ["Корма", "Отечественные рецептуры для осетровых и сиговых"],
      ["Маточные стада", "Криоконсервация и генетическая паспортизация"],
      ["Подращивание молоди", "Высокие навески — рост зачётности выпусков"],
      ["Мониторинг инкубации", "Машинное зрение: счёт икры, контроль среды"],
      ["Ихтиопатология в УЗВ", "Диагностика, вакцины, пробиотики"],
      ["УЗВ для Арктики", "Энергоэффективные решения для холодного климата"],
    ],
    note: "Направления — примеры по открытым источникам. Точный список даёт диагностика задачи.",
  }, 7, total);

  numGrid(pres, {
    kicker: "Что получает завод",
    title: "Результат без собственного НИИ",
    cards: [
      { head: "Решение без своего R&D", sub: "Экспертизу дают привлечённые профильные учёные" },
      { head: "Господдержка", sub: "До 70% стоимости НИОКР по ПП-218; двойной вычет — без конкурса" },
      { head: "Один подрядчик", sub: "Вместо десяти переговоров с институтами" },
      { head: "Честный ответ за 3–4 недели", sub: "«Подходящей технологии нет» — тоже результат" },
    ],
  }, 8, total);

  qa(pres, {
    kicker: "Вопросы",
    title: "Четыре прямых ответа",
    items: [
      ["Зачем посредник? Пойдём в институт напрямую", "Напрямую — это один институт и его оценка своей же разработки. Мы сравниваем решения по стране и миру"],
      ["Сколько это стоит?", "Оплата поэтапная, у каждого этапа своя точка выхода. Условия — по итогам диагностики задачи"],
      ["А если субсидию не дадут?", "Экономику считаем без неё. Двойной вычет идёт без конкурса"],
      ["Кто отвечает за результат?", "Мы: часть вознаграждения получаем только при внедрении"],
    ],
  }, 9, total);

  proposal(pres, {
    kicker: "Предложение",
    title: "Пилот для Югорского рыбоводного завода",
    leftHead: "Этапы, каждый со своей точкой выхода",
    leftRows: [
      ["Диагностика задачи", "3–4 нед."],
      ["Подбор и научная проверка: 5–7 решений", "6–8 нед."],
      ["Дальше по выбору завода: сделка, господдержка, внедрение", "—"],
    ],
    leftNote: "Задачи завода не публикуются: NDA — до начала диагностики",
    asks: [
      "Одна технологическая задача завода",
      "Два часа ваших технологов на диагностику",
      "Рабочая встреча на площадке",
    ],
    nextStep: "Выбираем задачу пилота и согласуем соглашение о неразглашении. Условия пилота — по итогам диагностики.",
  }, 10, total);

  return pres.writeFile({ fileName: path.join(__dirname, "sitall-deck-urz.pptx") });
}

// -------------------------------------------------------------------- Дек Б

function buildDepnedra() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.author = "Ситалл";
  pres.title = "Ситалл — для Депнедра и природных ресурсов Югры";
  const total = 10;

  cover(pres, { addressee: "Для Депнедра и природных ресурсов Югры" }, total);

  statGrid(pres, {
    kicker: "Задача государства",
    title: "Деньги на науку есть. Внедрять их некому",
    cards: [
      { big: "2% ВВП", caption: "затраты на науку к 2030 году, сейчас около 1%", src: ["ИСИЭЗ НИУ ВШЭ", "https://issek.hse.ru/news/849710120.html"] },
      { big: "×2", caption: "рост частных вложений бизнеса в науку — цель к 2030 году", src: ["ИСИЭЗ НИУ ВШЭ", "https://issek.hse.ru/news/849710120.html"] },
      { big: "80–88%", caption: "патентов не доходят до применения", src: ["Роспатент", "https://rospatent.gov.ru/ru/news/na-pmef-2023-obsudili-tehnologicheskoe-predprinimatelstvo-15062023"] },
      { big: "1,9 трлн ₽", caption: "нацпроекты технологического лидерства, 2026–2028", src: ["ComNews", "https://www.comnews.ru/content/241396/2025-10-01/2025-w40/1008/19-trilliona-rubley-potratyat-nacproekty-tekhnologicheskogo-liderstva-tri-goda"] },
    ],
    thesis: "Роспатент подготовил 200+ технологических брокеров. Работать им негде",
  }, 2, total);

  statGrid(pres, {
    kicker: "Югра",
    title: "Югра: сильная база, незакрытое звено",
    cards: [
      { big: "14 млрд ₽", caption: "ИНТЦ «Юнити Парк» в Сургуте: наука для Севера и Арктики", src: ["ПП РФ № 1255", "https://www.consultant.ru/document/cons_doc_LAW_453831/"] },
      { big: "+14", caption: "позиций в рейтинге инновационного развития — один из девяти регионов с существенным ростом", src: ["ИСИЭЗ ВШЭ", "https://issek.hse.ru/news/949132842.html"] },
      { big: "1653", caption: "пункта локального экомониторинга на лицензионных участках недропользователей", src: ["360.ru", "https://360.ru/tekst/ekologiya/zagrjaznenie-irtysha-i-obi/"] },
      { big: "0", caption: "структур отвечает за путь от задачи предприятия до внедрённой технологии", src: ["наша оценка", "https://www.ugrasu.ru/university/university/"] },
    ],
    thesis: "Вся инфраструктура работает от разработки. Звено «от задачи — к внедрению» не закрыто нигде",
  }, 3, total);

  statGrid(pres, {
    kicker: "Задачи, которые упираются в технологии",
    title: "Показатели ведомства — это технологические задачи",
    cards: [
      { big: "2027", caption: "публичный срок ликвидации нефтезагрязнённых земель — при темпе рекультивации ~1000 га в год", src: ["НИА «Экология»", "https://nia.eco/2023/11/21/73340/"] },
      { big: "226", caption: "случаев высокого и экстремально высокого загрязнения Оби и Иртыша за 2023 год", src: ["«Известия»", "https://iz.ru/1664610/sergei-gurianov/zolotoe-techenie-v-obi-i-irtyshe-ne-nashli-chistoi-vody"] },
      { big: "Без денег", bigSize: 26, caption: "программа восстановления сиговых по поручению Президента одобрена, финансирование не определено", src: ["СенатИнформ", "https://senatinform.ru/news/regiony_prosyat_rasshirit_programmu_vosstanovleniya_populyatsii_ryby_v_obi_i_irtyshe_/"] },
      { big: "10,2 млрд ₽", caption: "новая госпрограмма «Экономика замкнутого цикла»: полигоны, переработка, «Генеральная уборка»", src: ["neft.media", "https://amp.neft.media/yugra/news/e6e2e3da-661f-4650-ae24-01844acc3c50"] },
    ],
    thesis: "За каждым показателем — задача, у которой уже есть научное решение",
  }, 4, total);

  numGrid(pres, {
    kicker: "Кто мы",
    title: "Внешний офис трансфера технологий",
    cards: [
      { head: "От задачи, а не от разработки", sub: "Начинаем там, где теряются деньги и показатели" },
      { head: "Отвечаем деньгами за внедрение", sub: "Часть вознаграждения — только при результате" },
      { head: "Господдержку собираем сами", sub: "Заявки готовим мы, а не сотрудники предприятий" },
      { head: "Госцентрам приводим заказчика", sub: "НАЦ им. Шпильмана, СурГУ, ЮГУ для нас партнёры и исполнители" },
    ],
    thesis: "Бюджет округа не тратит ни рубля. Платит предприятие — за решение своей задачи",
  }, 5, total);

  cycle(pres, {
    kicker: "Как это работает",
    title: "Один цикл: от задачи до внедрения",
    rows: CYCLE_ROWS,
    tools: [...TOOLS_BASE, ["«Экологическое благополучие»", "812 млрд ₽ до 2030 года"]],
  }, 6, total);

  instrument(pres, {
    kicker: "Чем работаем",
    title: "Ситалл Скаут: подбор решений за сутки",
    feats: [
      ["Проверяет человек", "Инструмент даёт черновик, решение подтверждает учёный"],
      ["Открытые данные", "Публикации, патенты, гранты, российский и мировой веб"],
      ["Готов к ГИС НИОКР", "Подключимся, когда система откроется для рынка"],
    ],
    thesis: "Инструмент разработан и работает. Показываем в деле по запросу",
  }, 7, total);

  valueCols(pres, {
    kicker: "Что получает каждая сторона",
    title: "Что получает каждая сторона",
    cols: [
      { head: "Ведомству", items: [
        "Технологические решения под показатели: рекультивация, аварийность, вода, отходы",
        "Частные деньги недропользователей идут в проверенные технологии",
        "Расходов бюджета нет",
        "Открытый отчёт по региону",
      ]},
      { head: "Предприятиям", items: [
        "Решение без собственного R&D",
        "Господдержка: заявки готовим мы",
        "Один подрядчик вместо десяти переговоров",
        "Ответ за 3–4 недели, а не за два года",
      ]},
      { head: "Науке региона", items: [
        "Оплаченные заказы для НАЦ им. Шпильмана, СурГУ, ЮГУ",
        "Перевод на язык бизнеса — за нами",
        "Участие бесплатно",
      ]},
    ],
  }, 8, total);

  qa(pres, {
    kicker: "Вопросы",
    title: "Четыре прямых ответа",
    items: [
      ["У нас есть НАЦ им. Шпильмана, СурГУ, технопарк. Зачем вы?", "Они — наука и экспертиза, работают от разработки. Мы — от задачи предприятия: собираем сделку и отвечаем деньгами за внедрение. Мы приводим им заказ"],
      ["Что просите у бюджета?", "Ничего. Соглашение, представление предприятиям и участие в площадках"],
      ["Зачем посредник? Предприятия пойдут напрямую", "Напрямую — это один институт и его оценка своей же разработки. Мы сравниваем решения по стране"],
      ["Чем рискует округ?", "Финансово ничем. Обязательства счётные, отчёт открытый, эксклюзивности нет"],
    ],
  }, 9, total);

  proposal(pres, {
    kicker: "Предложение",
    title: "Соглашение о сотрудничестве с Депнедра Югры",
    leftHead: "Что делаем за первый год, за свой счёт",
    leftRows: [
      ["Задачи предприятий-природопользователей", "10"],
      ["Подборов с научной проверкой", "5"],
      ["Заявок на софинансирование", "2"],
      ["Внедрённый проект", "1"],
      ["Открытый отчёт «Карта технологий для природопользования Югры»", "1"],
    ],
    leftNote: "Расходов бюджета нет",
    asks: [
      "Соглашение о сотрудничестве или меморандум",
      "Представление 5–7 предприятиям-недропользователям и учреждениям",
      "Участие в площадках округа — включая экспертный совет по инновациям",
    ],
    nextStep: "Рабочая встреча с профильным блоком; пилот на одной задаче — отчёт через шесть месяцев.",
  }, 10, total);

  return pres.writeFile({ fileName: path.join(__dirname, "sitall-deck-depnedra.pptx") });
}

Promise.all([buildUrz(), buildDepnedra()]).then((files) => {
  console.log("Собрано:", files.join(", "));
});
