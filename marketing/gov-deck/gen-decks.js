// Генератор адресных деков «Ситалл». Дизайн — по референсу владельца от
// 26.08.2026 (sitall-gov-deck-sverdlovsk.pptx): белый фон, карточки с мягкой
// тенью, NAVY/GOLD, Segoe UI, крупный кегль (текст от 16 pt, заголовки от
// 20 pt; служебные строки — источники, футер, кикеры — 12–13 pt).
// Тексты слайдов — в deck-texts.md соответствующего региона: правьте там,
// затем переносите сюда и пересобирайте.
// Запуск: node gen-decks.js  → ../regions/hmao/sitall-deck-urz.pptx,
// ../regions/sverdlovsk/sitall-deck-minprirody.pptx,
// ../regions/vladimir/sitall-deck-minselhoz.pptx и
// ../regions/moscow-oblast/sitall-deck-mpk.pptx

const pptxgen = require("pptxgenjs");
const path = require("path");

const NAVY = "1E2761";
const INKD = "1B2140";
const GRAY = "6B7699";
const GOLD = "C9A24B";
const LINE = "C9D3EA";
const ICEBG = "E3EAF8";
const F = "Segoe UI";
const F_SB = "Segoe UI Semibold";
const W = 13.333;
const H = 7.5;
const MX = 0.55;

const shadow = () => ({ type: "outer", angle: 90, blur: 14, offset: 4, color: "7E88A8", opacity: 0.3 });

// ---------------------------------------------------------------- помощники

function footer(slide, page, total) {
  slide.addText("Ситалл", {
    x: MX, y: H - 0.42, w: 2, h: 0.3, fontFace: F, fontSize: 12,
    color: GRAY, margin: 0, isTextBox: true,
  });
  slide.addText(`${String(page).padStart(2, "0")} / ${total}`, {
    x: W - 1.55, y: H - 0.42, w: 1, h: 0.3, fontFace: F, fontSize: 12,
    color: GRAY, align: "right", margin: 0, isTextBox: true,
  });
}

function kickerTitle(slide, kicker, title) {
  slide.addText(kicker.toUpperCase(), {
    x: MX, y: 0.24, w: W - 2 * MX, h: 0.3, fontFace: F_SB, fontSize: 13,
    color: GOLD, charSpacing: 3, margin: 0, isTextBox: true,
  });
  slide.addText(title, {
    x: MX - 0.02, y: 0.56, w: W - 2 * MX, h: 0.62, fontFace: F_SB, fontSize: 27,
    color: NAVY, margin: 0, isTextBox: true,
  });
  slide.addShape("rect", { x: MX, y: 1.24, w: 1.2, h: 0.07, fill: { color: GOLD } });
}

function card(slide, x, y, w, h, accent) {
  slide.addShape("roundRect", {
    x, y, w, h, rectRadius: 0.09,
    fill: { color: "FFFFFF" },
    line: { color: accent ? GOLD : LINE, width: accent ? 1.75 : 1 },
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
    fill: { color: accent ? GOLD : NAVY },
  });
  slide.addText(o.big, {
    x: o.x + pad, y: o.y + 0.36, w: o.w - 2 * pad, h: 0.78,
    fontFace: F_SB, fontSize: o.bigSize || 44, color: accent ? GOLD : NAVY,
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
    fill: { color: accent ? GOLD : NAVY },
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
  slide.addText(o.sub, {
    x: o.x + pad, y: o.y + 0.78 + (o.headH || 0.4) + 0.08,
    w: o.w - 2 * pad, h: o.h - 0.78 - (o.headH || 0.4) - 0.24,
    fontFace: F, fontSize: 16, color: INKD, margin: 0,
    lineSpacingMultiple: 1.08, isTextBox: true, valign: "top",
  });
}

// Тезис-полоса: вертикальная навигационная черта + фраза.
function thesis(slide, text, y, h) {
  slide.addShape("rect", { x: MX, y: y, w: 0.07, h: h || 0.7, fill: { color: NAVY } });
  slide.addText(text, {
    x: MX + 0.28, y: y - 0.03, w: W - 2 * MX - 0.4, h: (h || 0.7) + 0.06,
    fontFace: F_SB, fontSize: 20, color: NAVY, margin: 0, valign: "middle",
    lineSpacingMultiple: 1.05, isTextBox: true,
  });
}

// Цитата: золотая черта, глиф «, курсив, ссылка на автора.
function quote(slide, text, author, url, y) {
  slide.addShape("rect", { x: MX, y: y, w: 0.07, h: 1.5, fill: { color: GOLD } });
  slide.addText("«", {
    x: MX + 0.24, y: y - 0.1, w: 0.7, h: 0.9, fontFace: F_SB, fontSize: 48,
    color: GOLD, margin: 0, isTextBox: true,
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

// ------------------------------------------------------------------ обложка

function cover(pres, d, total) {
  const s = pres.addSlide();
  s.addShape("ellipse", { x: 9.2, y: -1.8, w: 5.8, h: 5.8, fill: { type: "none" }, line: { color: LINE, width: 1.25 } });
  s.addShape("ellipse", { x: 11.0, y: 0.9, w: 3.6, h: 3.6, fill: { type: "none" }, line: { color: GOLD, width: 1.5 } });
  s.addShape("ellipse", { x: 9.9, y: 4.2, w: 4.8, h: 4.8, fill: { type: "none" }, line: { color: LINE, width: 1.25 } });
  s.addText(`${d.month || "АВГУСТ 2026"} · КОНФИДЕНЦИАЛЬНО`, {
    x: MX, y: 0.4, w: 7, h: 0.3, fontFace: F_SB, fontSize: 13, color: GOLD,
    charSpacing: 3, margin: 0, isTextBox: true,
  });
  s.addText("Ситалл", {
    x: MX - 0.06, y: 1.05, w: 9, h: 1.7, fontFace: F_SB, fontSize: 80,
    color: NAVY, margin: 0, isTextBox: true,
  });
  s.addShape("rect", { x: MX, y: 3.0, w: 1.5, h: 0.09, fill: { color: GOLD } });
  s.addText("Технологический брокер для промышленности", {
    x: MX - 0.02, y: 3.35, w: 9.5, h: 0.55, fontFace: F_SB, fontSize: 24,
    color: NAVY, margin: 0, isTextBox: true,
  });
  s.addText("Работаем от задачи предприятия до внедрённой технологии", {
    x: MX, y: 3.98, w: 9.5, h: 0.42, fontFace: F, fontSize: 18, color: INKD,
    margin: 0, isTextBox: true,
  });
  const addrW = d.addrW || 7.6;
  s.addShape("roundRect", {
    x: MX, y: 4.75, w: addrW, h: d.addrH || 0.66, rectRadius: 0.09,
    fill: { color: ICEBG }, line: { color: LINE, width: 1 },
  });
  s.addText(d.addressee, {
    x: MX + 0.3, y: 4.75, w: addrW - 0.5, h: d.addrH || 0.66, fontFace: F_SB, fontSize: 18,
    color: NAVY, margin: 0, valign: "middle", lineSpacingMultiple: 1.05, isTextBox: true,
  });
  s.addText("Кирилл Уклонский · uklonskiy.k@ya.ru", {
    x: MX, y: 6.5, w: 7, h: 0.36, fontFace: F_SB, fontSize: 16, color: NAVY,
    margin: 0, isTextBox: true,
  });
}

// ------------------------------------------------- четыре карточки с цифрами

function statGrid(pres, d, page, total) {
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
      src: c.src, accent: i === 1 || i === 2,
    });
  });
  if (d.thesis) thesis(s, d.thesis, 6.48, 0.6);
  footer(s, page, total);
}

// --------------------------------------- три карточки в ряд + тезис/цитата

function statRow3(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.34;
  const cw = (W - 2 * MX - 2 * gap) / 3;
  const ch = 3.35;
  d.cards.forEach((c, i) => {
    statCard(s, {
      x: MX + i * (cw + gap), y: 1.62, w: cw, h: ch,
      big: c.big, bigSize: c.bigSize || 34, caption: c.caption, src: c.src,
      accent: i === 1,
    });
  });
  if (d.quote) quote(s, d.quote[0], d.quote[1], d.quote[2], 5.35);
  else if (d.thesis) thesis(s, d.thesis, 5.5, 0.9);
  footer(s, page, total);
}

// ------------------------------------------- четыре нумерованные карточки 2х2

function numGrid(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.34;
  const cw = (W - 2 * MX - gap) / 2;
  const ch = (7.0 - 1.62 - gap) / 2;
  d.cards.forEach((c, i) => {
    numCard(s, {
      x: MX + (i % 2) * (cw + gap), y: 1.62 + Math.floor(i / 2) * (ch + gap),
      w: cw, h: ch, n: i + 1, head: c.head, sub: c.sub,
      accent: i === 1 || i === 3, headH: c.headH,
    });
  });
  footer(s, page, total);
}

// --------------------------------------------------------------- схема цикла

function cycle(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const labW = 1.85;
  const gap = 0.22;
  const colW = (W - 2 * MX - labW - 0.15 - 3 * gap) / 4;
  const colX = (i) => MX + labW + 0.15 + i * (colW + gap);
  const steps = ["1. Задача", "2. Поиск и проверка", "3. Сделка и поддержка", "4. Внедрение"];
  steps.forEach((t, i) => {
    s.addShape("rect", { x: colX(i), y: 1.58, w: colW, h: 0.05, fill: { color: GOLD } });
    s.addText(t, {
      x: colX(i), y: 1.7, w: colW, h: 0.62, fontFace: F_SB, fontSize: 16,
      color: NAVY, margin: 0, lineSpacingMultiple: 1.0, isTextBox: true, valign: "top",
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
  footer(s, page, total);
}

// ---------------------------------------------------------------- инструмент

function instrument(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const bigW = 4.5;
  card(s, MX, 1.62, bigW, 5.15, true);
  s.addShape("rect", { x: MX + 0.36, y: 1.98, w: 0.5, h: 0.06, fill: { color: GOLD } });
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
    s.addShape("rect", { x: rx + 0.32, y: y + 0.24, w: 0.42, h: 0.05, fill: { color: GOLD } });
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
  footer(s, page, total);
}

// --------------------------------------------- шесть карточек-направлений 3х2

function dirGrid(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.3;
  const cw = (W - 2 * MX - 2 * gap) / 3;
  const ch = 2.35;
  d.cards.forEach((c, i) => {
    numCard(s, {
      x: MX + (i % 3) * (cw + gap), y: 1.58 + Math.floor(i / 3) * (ch + 0.3),
      w: cw, h: ch, n: i + 1, head: c[0], sub: c[1],
      accent: i === 1 || i === 3 || i === 5, headH: 0.42,
    });
  });
  s.addText(d.note, {
    x: MX, y: 6.62, w: W - 2 * MX, h: 0.36, fontFace: F, fontSize: 16,
    italic: true, color: GRAY, margin: 0, isTextBox: true,
  });
  footer(s, page, total);
}

// ----------------------------------------------------- три колонки «ценность»

function valueCols(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.34;
  const cw = (W - 2 * MX - 2 * gap) / 3;
  d.cols.forEach((col, i) => {
    const x = MX + i * (cw + gap);
    card(s, x, 1.62, cw, 5.15, i === 0);
    s.addShape("rect", { x: x + 0.32, y: 1.9, w: 0.42, h: 0.05, fill: { color: i === 0 ? GOLD : NAVY } });
    s.addText(col.head.toUpperCase(), {
      x: x + 0.32, y: 2.06, w: cw - 0.64, h: 0.34, fontFace: F_SB, fontSize: 14,
      color: i === 0 ? GOLD : GRAY, charSpacing: 2, margin: 0, isTextBox: true,
    });
    s.addText(
      col.items.map((t, j) => ({
        text: t,
        options: {
          bullet: { code: "2022", indent: 12 },
          color: INKD,
          breakLine: j < col.items.length - 1,
          paraSpaceAfter: 12,
        },
      })),
      {
        x: x + 0.34, y: 2.52, w: cw - 0.62, h: 4.1, fontFace: F, fontSize: 16,
        margin: 0, lineSpacingMultiple: 1.1, valign: "top", isTextBox: true,
      }
    );
  });
  footer(s, page, total);
}

// ------------------------------------------------------------------- команда

function team(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const gap = 0.34;
  const cw = (W - 2 * MX - 2 * gap) / 3;
  d.people.forEach((p, i) => {
    const x = MX + i * (cw + gap);
    card(s, x, 1.62, cw, 5.15, i === 1);
    s.addShape("rect", { x: x + 0.32, y: 1.9, w: 0.42, h: 0.05, fill: { color: i === 1 ? GOLD : NAVY } });
    s.addText(p.name, {
      x: x + 0.32, y: 2.04, w: cw - 0.64, h: 0.82, fontFace: F_SB, fontSize: 22,
      color: NAVY, margin: 0, lineSpacingMultiple: 1.0, isTextBox: true, valign: "top",
    });
    s.addText(p.role.toUpperCase(), {
      x: x + 0.32, y: 2.9, w: cw - 0.64, h: 0.3, fontFace: F_SB, fontSize: 13,
      color: GOLD, charSpacing: 2, margin: 0, isTextBox: true,
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
  footer(s, page, total);
}

// ---------------------------------------------------------------- предложение

function proposal(pres, d, page, total) {
  const s = pres.addSlide();
  kickerTitle(s, d.kicker, d.title);
  const lw = 6.7;
  s.addText(d.leftHead.toUpperCase(), {
    x: MX, y: 1.56, w: lw, h: 0.3, fontFace: F_SB, fontSize: 13, color: GOLD,
    charSpacing: 2.5, margin: 0, isTextBox: true,
  });
  const n = d.leftRows.length;
  const rh = n > 4 ? 0.72 : 0.92;
  const rgap = n > 4 ? 0.16 : 0.24;
  d.leftRows.forEach((row, i) => {
    const y = 1.96 + i * (rh + rgap);
    card(s, MX, y, lw, rh, false);
    s.addText(row[0], {
      x: MX + 0.3, y, w: lw - 1.7, h: rh, fontFace: F, fontSize: 17,
      color: INKD, margin: 0, valign: "middle", lineSpacingMultiple: 1.0, isTextBox: true,
    });
    s.addText(row[1], {
      x: MX + lw - 1.5, y, w: 1.2, h: rh, fontFace: F_SB, fontSize: 26,
      color: GOLD, align: "right", margin: 0, valign: "middle", isTextBox: true,
    });
  });
  s.addText(d.leftNote, {
    x: MX, y: 1.96 + n * (rh + rgap) + 0.06, w: lw, h: 0.6,
    fontFace: F_SB, fontSize: 16, color: NAVY, margin: 0,
    lineSpacingMultiple: 1.05, isTextBox: true,
  });
  const rx = MX + lw + 0.55;
  const rw = W - rx - MX;
  s.addText("ЧТО ПРОСИМ", {
    x: rx, y: 1.56, w: rw, h: 0.3, fontFace: F_SB, fontSize: 13, color: GOLD,
    charSpacing: 2.5, margin: 0, isTextBox: true,
  });
  d.asks.forEach((a, i) => {
    const y = 1.96 + i * 1.0;
    s.addShape("ellipse", { x: rx, y: y + 0.08, w: 0.42, h: 0.42, fill: { color: NAVY } });
    s.addText(String(i + 1), {
      x: rx, y: y + 0.08, w: 0.42, h: 0.42, fontFace: F_SB, fontSize: 16,
      color: "FFFFFF", align: "center", valign: "middle", margin: 0, isTextBox: true,
    });
    s.addText(a, {
      x: rx + 0.6, y: y - 0.06, w: rw - 0.6, h: 0.92, fontFace: F_SB,
      fontSize: 17, color: NAVY, margin: 0, lineSpacingMultiple: 1.05,
      valign: "middle", isTextBox: true,
    });
  });
  if (d.nextStep) {
    const nsY = 1.96 + d.asks.length * 1.0 + 0.2;
    card(s, rx, nsY, rw, 1.7, true);
    s.addText("СЛЕДУЮЩИЙ ШАГ", {
      x: rx + 0.3, y: nsY + 0.2, w: rw - 0.6, h: 0.28, fontFace: F_SB,
      fontSize: 13, color: GOLD, charSpacing: 2.5, margin: 0, isTextBox: true,
    });
    s.addText(d.nextStep, {
      x: rx + 0.3, y: nsY + 0.52, w: rw - 0.6, h: 1.05, fontFace: F,
      fontSize: 16, color: INKD, margin: 0, lineSpacingMultiple: 1.08,
      valign: "top", isTextBox: true,
    });
  }
  s.addText("Кирилл Уклонский · uklonskiy.k@ya.ru", {
    x: d.nextStep ? MX : rx, y: 6.6, w: 6, h: 0.34, fontFace: F_SB,
    fontSize: 16, color: NAVY, margin: 0, isTextBox: true,
  });
  footer(s, page, total);
}

// ============================================== общие блоки «о компании»

const WHO = {
  kicker: "Кто мы",
  title: "Внешний офис трансфера технологий",
  cards: [
    { head: "От задачи, а не от разработки", sub: "Начинаем там, где предприятие теряет деньги" },
    { head: "Отвечаем деньгами за внедрение", sub: "Часть вознаграждения — только при результате" },
    { head: "Господдержку собираем сами", sub: "Документацию готовим мы, а не сотрудники предприятия" },
    { head: "Госцентрам приводим заказчика", sub: "НОЦ, ЦТТ и фонды для нас партнёры и исполнители" },
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
      role: "Региональный партнёр",
      items: [
        "Привлечение спонсирования для спортивных мероприятий федерального и международного уровня",
        "Инициатор проектов по созданию и развитию индустриальных парков в Свердловской области",
      ],
    },
  ],
};

function scout(gis) {
  return {
    kicker: "Чем работаем",
    title: "Ситалл Скаут: платформа подбора решений",
    feats: [
      ["Проверяет человек", "Инструмент даёт варианты, подтверждает профильный специалист"],
      ["Открытые данные", "Публикации, патенты, гранты, российский и мировой веб"],
      gis
        ? ["Готовы к ГИС НИОКР", "Подключимся, когда система откроется для рынка"]
        : ["Показываем в деле", "Демонстрация на вашей задаче — по запросу"],
    ],
  };
}

// ============================== Дек 1: АО «Югорский рыбоводный завод» (ХМАО)

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
      { big: "×10–30", caption: "падение биомассы крупных сиговых Оби; вылов муксуна и нельмы запрещён", src: ["Fishnews", "https://fishnews.ru/news/46450"] },
      { big: "110 млн", caption: "шт. молоди в год нужно бассейну — при выпуске 24,7 млн шт. в 2025", src: ["ТАСС", "https://tass.ru/ekonomika/25588677"] },
      { big: "Без денег", bigSize: 40, caption: "программа сиговых по поручению Президента одобрена — денег не определено", src: ["СенатИнформ", "https://senatinform.ru/news/regiony_prosyat_rasshirit_programmu_vosstanovleniya_populyatsii_ryby_v_obi_i_irtyshe_/"] },
    ],
  }, 2, total);

  statRow3(pres, {
    kicker: "Вызовы отрасли",
    title: "Три технологических узких места аквакультуры Севера",
    cards: [
      { big: "до 90%", caption: "Корма. Столько кормов для ценных видов давал импорт; барьеры и срывы поставок остаются", src: ["Росрыболовство", "https://fish.gov.ru/news/2024/11/15/ilya-shestakov-rossijskie-predpriyatiya-narashhivayut-obemy-proizvodstva-rybnyh-kormov/"] },
      { big: "Дефицит", bigSize: 34, caption: "Посадочный материал. Маточных стад муксуна не хватает; нужен генетический менеджмент", src: ["«Научная Россия»", "https://scientificrussia.ru/articles/kakova-tsena-tsennyh-vidov-ryb"] },
      { big: "~10%", caption: "Выживаемость личинки при выпуске. Зачёт растёт с навеской — нужно подращивание", src: ["«Научная Россия»", "https://scientificrussia.ru/articles/kakova-tsena-tsennyh-vidov-ryb"] },
    ],
    thesis: "За каждым узким местом — готовые научные решения, которые пока не дошли до производства",
  }, 3, total);

  numGrid(pres, WHO, 4, total);
  cycle(pres, CYCLE, 5, total);
  instrument(pres, scout(false), 6, total);

  dirGrid(pres, {
    kicker: "Направления поиска",
    title: "Что мы можем найти под задачи завода",
    cards: [
      ["Корма", "Отечественные рецептуры для осетровых и сиговых"],
      ["Маточные стада", "Криоконсервация и генетическая паспортизация"],
      ["Подращивание", "Высокие навески — рост зачётности выпусков"],
      ["Инкубация", "Машинное зрение: счёт икры, контроль среды"],
      ["Ихтиопатология", "Диагностика, вакцины и пробиотики в УЗВ"],
      ["УЗВ для Арктики", "Энергоэффективные решения для холодного климата"],
    ],
    note: "Направления — примеры по открытым источникам. Точный список даёт диагностика задачи.",
  }, 7, total);

  numGrid(pres, {
    kicker: "Что получает завод",
    title: "Результат без собственного НИИ",
    cards: [
      { head: "Решение без своего R&D", sub: "Экспертизу дают привлечённые профильные учёные" },
      { head: "Экономика с господдержкой", sub: "На 100 млн ₽ проекта — 15–30 млн ₽ своих: ПП-218 и двойной вычет" },
      { head: "Один управляющий подрядчик", sub: "Вместо десяти переговоров с институтами" },
      { head: "Ответ за 3–4 недели", sub: "«Подходящей технологии нет» — тоже результат" },
    ],
  }, 8, total);

  team(pres, TEAM, 9, total);

  proposal(pres, {
    kicker: "Предложение",
    title: "Пилот для Югорского рыбоводного завода",
    leftHead: "Этапы, каждый со своей точкой выхода",
    leftRows: [
      ["Диагностика задачи", "3–4 нед."],
      ["Подбор и проверка: 5–7 решений", "6–8 нед."],
      ["Дальше по выбору завода: сделка,\nгосподдержка, внедрение", "—"],
    ],
    leftNote: "Задачи завода не публикуются: NDA — до начала диагностики",
    asks: [
      "Одна технологическая задача завода",
      "Два часа ваших технологов на диагностику",
      "Рабочая встреча на площадке",
    ],
    nextStep: "Выбираем задачу пилота и согласуем соглашение о неразглашении. Условия — по итогам диагностики.",
  }, 10, total);

  return pres.writeFile({
    fileName: path.join(__dirname, "..", "regions", "hmao", "sitall-deck-urz.pptx"),
  });
}

// ================== Дек 2: Минприроды Свердловской области

function buildMprso() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.author = "Ситалл";
  pres.title = "Ситалл — для Минприроды Свердловской области";
  const total = 10;

  cover(pres, {
    addressee: "Для Министерства природных ресурсов и экологии\nСвердловской области",
    addrH: 0.92,
  }, total);

  statGrid(pres, {
    kicker: "Задача государства",
    title: "Деньги на науку есть. Внедрять технологии некому",
    cards: [
      { big: "2% ВВП", caption: "цель по затратам на науку к 2030 году", src: ["ИСИЭЗ НИУ ВШЭ", "https://issek.hse.ru/news/849710120.html"] },
      { big: "×2", caption: "рост частных вложений бизнеса в науку — цель к 2030 году", src: ["ИСИЭЗ НИУ ВШЭ", "https://issek.hse.ru/news/849710120.html"] },
      { big: "1,9 трлн ₽", caption: "нацпроекты технологического лидерства к 2028 года", src: ["ComNews", "https://www.comnews.ru/content/241396/2025-10-01/2025-w40/1008/19-trilliona-rubley-potratyat-nacproekty-tekhnologicheskogo-liderstva-tri-goda"] },
      { big: "2,6 млн", caption: "дефицит кадров. Ставка на производительность", src: ["РБК", "https://www.rbc.ru/economics/24/12/2024/676a7e719a79473dc5db384f"] },
    ],
  }, 2, total);

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
  }, 3, total);

  statGrid(pres, MPRSO_AGENDA, 4, total);

  numGrid(pres, WHO, 5, total);
  cycle(pres, CYCLE, 6, total);
  instrument(pres, scout(true), 7, total);

  valueCols(pres, {
    kicker: "Ценность",
    title: "Что получает каждая сторона",
    cols: [
      { head: "Министерству", items: [
        "Технологические решения под показатели: вода, отходы, накопленный вред",
        "Частные деньги природопользователей идут в проверенные технологии",
        "Расходов областного бюджета нет",
        "Открытый отчёт по региону",
      ]},
      { head: "Предприятиям", items: [
        "Решение без своего R&D",
        "На 100 млн ₽ проекта — только 15–30 млн ₽ своих",
        "Один управляющий подрядчик на весь проект",
        "Ответ за 3–4 недели, а не за два года",
      ]},
      { head: "Науке", items: [
        "Оплаченные заказы от предприятий",
        "Перевод на язык бизнеса — за нами",
        "Участие бесплатно",
      ]},
    ],
  }, 8, total);

  team(pres, TEAM, 9, total);

  proposal(pres, {
    kicker: "Предложение",
    title: "Соглашение с Минприроды Свердловской области",
    leftHead: "Что делаем за первый год, за свой счёт",
    leftRows: [
      ["Обработанные задачи предприятий", "10"],
      ["Подборов с проверкой", "5"],
      ["Заявок на софинансирование", "2"],
      ["Внедрённый проект", "1"],
      ["Открытый отчёт по региону", "1"],
    ],
    leftNote: "Расходов областного бюджета нет",
    asks: [
      "Соглашение о сотрудничестве",
      "Представление 5–7 предприятиям-природопользователям",
      "Участие в площадках региона как партнёра",
    ],
    nextStep: null,
  }, 10, total);

  return pres.writeFile({
    fileName: path.join(__dirname, "..", "regions", "sverdlovsk", "sitall-deck-minprirody.pptx"),
  });
}

// Слайд «повестка министерства»: карточки заполняются из разведки
// regions/sverdlovsk/research-minprirody-*.md — правьте вместе с ней.
const MPRSO_AGENDA = {
  kicker: "Задачи, которые упираются в технологии",
  title: "Показатели министерства — это технологические задачи",
  cards: [
    { big: "25 млрд ₽", bigSize: 40, caption: "программа из 13 объектов обращения с ТКО — концессии ждут проверенных технологий", src: ["Накануне.RU", "https://www.nakanune.ru/articles/124741/"] },
    { big: "2027", bigSize: 40, caption: "старт очистки Шарташа; в очереди — Верх-Исетский пруд, Таватуй, Балтым", src: ["УрБК", "https://urbc.ru/1068143381-denis-mamontov-ochistka-ozera-shartash-nachnetsja-v-2027-godu.html"] },
    { big: "Треть", bigSize: 40, caption: "потребности покрывает нынешняя нейтрализация шахтных вод Левихи", src: ["ФедералПресс", "https://fedpress.ru/article/2551376"] },
    { big: "800 га", bigSize: 40, caption: "крупнейший лесной пожар мая 2026; ставка — на раннее обнаружение", src: ["Областная газета", "https://oblgazeta.ru/society/fire-mode/2026/05/148591/"] },
  ],
  thesis: "За каждым показателем — задача, у которой уже есть научное решение",
};

// ================== Дек 3: Минсельхоз Владимирской области

// Слайд «повестка министерства» и слайд «наука рядом»: карточки заполняются
// из разведки regions/vladimir/research-minselhoz-*.md — правьте вместе с ней.
const MSHVO_AGENDA = {
  kicker: "Задачи, которые упираются в технологии",
  title: "Показатели министерства — это технологические задачи",
  cards: [
    { big: "9 722 кг", bigSize: 40, caption: "надой на корову в год: на 14% выше среднего по стране", src: ["АиФ Владимир", "https://vlad.aif.ru/society/proizvodstvo-moloka-vo-vladimirskoy-oblasti-vyroslo-na-53-za-10-let"] },
    { big: "83,5 млн $", bigSize: 38, caption: "экспорт продукции АПК за полугодие, почти +30% к прошлому году", src: ["Про Город 33", "https://progorod33.ru/news/132440"] },
    { big: "1 млрд ₽", bigSize: 40, caption: "субсидий аграриям за полугодие; ещё 2 млрд ₽ кредитов под 7,5%", src: ["Про Город 33", "https://progorod33.ru/news/132440"] },
    { big: "~7 тыс. га", bigSize: 38, caption: "обработано от борщевика с 2019 года; на 2026-й — ещё 21 млн ₽", src: ["Про Город 33", "https://progorod33.ru/news/129237"] },
  ],
  thesis: "За каждым показателем — задача, у которой уже есть научное решение",
};

const MSHVO_SCIENCE = {
  kicker: "Наука рядом",
  title: "Три федеральных научных центра АПК — в самой области",
  cards: [
    { big: "ВНИИЗЖ", bigSize: 30, caption: "Федеральный центр охраны здоровья животных, Владимир: 1 200 сотрудников. Разработчик «Хорриота»", src: ["ВетИС", "https://help.vetrf.ru/wiki/Компонент_Хорриот"] },
    { big: "ФИЦВиМ", bigSize: 30, caption: "Центр вирусологии и микробиологии, Вольгинский: особо опасные инфекции животных", src: ["ficvim.ru", "https://ficvim.ru/about/"] },
    { big: "ФАНЦ", bigSize: 30, caption: "Верхневолжский аграрный центр, Суздальский район: семена зерновых и картофеля", src: ["vnish.org", "https://vnish.org/o-nas/"] },
  ],
  thesis: "Науку в область везти не нужно. Нужен тот, кто переведёт её\nна язык хозяйства и доведёт до фермы",
};

function buildMshvo() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.author = "Ситалл";
  pres.title = "Ситалл — для Минсельхоза Владимирской области";
  const total = 12;

  cover(pres, {
    addressee: "Аликебедову Роману Камалутдиновичу\nМинистерство сельского хозяйства Владимирской области",
    addrH: 0.92, addrW: 8.7, month: "СЕНТЯБРЬ 2026",
  }, total);

  statGrid(pres, {
    kicker: "Задача государства",
    title: "Деньги на технологии в АПК есть. Внедрять их некому",
    cards: [
      { big: "19,8 млрд ₽", bigSize: 38, caption: "нацпроект «Технологическое обеспечение продовольственной безопасности» на 2026 год", src: ["Интерфакс", "https://www.interfax.ru/russia/1067412"] },
      { big: "66,7%", caption: "целевой уровень технологической независимости в продовольственной безопасности к 2030 году", src: ["Интерфакс", "https://www.interfax.ru/russia/1067412"] },
      { big: "2% ВВП", caption: "цель по затратам на науку к 2030 году", src: ["ИСИЭЗ НИУ ВШЭ", "https://issek.hse.ru/news/849710120.html"] },
      { big: "2,6 млн", caption: "дефицит кадров в экономике. Ставка на производительность", src: ["РБК", "https://www.rbc.ru/economics/24/12/2024/676a7e719a79473dc5db384f"] },
    ],
  }, 2, total);

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
  }, 3, total);

  statGrid(pres, MSHVO_AGENDA, 4, total);
  statRow3(pres, MSHVO_SCIENCE, 5, total);

  numGrid(pres, WHO, 6, total);
  cycle(pres, CYCLE, 7, total);
  instrument(pres, scout(true), 8, total);

  dirGrid(pres, {
    kicker: "Направления поиска",
    title: "Что мы можем найти под задачи АПК области",
    cards: [
      ["Корма", "Рационы, консерванты, отечественный белок"],
      ["Генетика стада", "Геномная оценка, эмбриотрансфер, племенное ядро"],
      ["Здоровье животных", "Диагностика, вакцины, биобезопасность фермы"],
      ["Переработка молока", "Глубокая переработка, сроки хранения, качество"],
      ["Семена и картофель", "Отечественные сорта, элитное семеноводство"],
      ["Земля и борщевик", "Мониторинг с воздуха, биометоды, ввод в оборот"],
    ],
    note: "Направления — примеры по открытым источникам. Точный список даёт диагностика задачи.",
  }, 9, total);

  valueCols(pres, {
    kicker: "Ценность",
    title: "Что получает каждая сторона",
    cols: [
      { head: "Министерству", items: [
        "Технологические решения под показатели: молоко, переработка, экспорт, земля",
        "Частные деньги хозяйств идут в проверенные технологии",
        "Расходов областного бюджета нет",
        "Открытый отчёт по АПК региона",
      ]},
      { head: "Хозяйствам и переработчикам", items: [
        "Решение без своего R&D",
        "На 100 млн ₽ проекта — только 15–30 млн ₽ своих",
        "Один управляющий подрядчик на весь проект",
        "Ответ за 3–4 недели, а не за два года",
      ]},
      { head: "Науке области", items: [
        "Оплаченные заказы от хозяйств",
        "Перевод на язык бизнеса — за нами",
        "Участие бесплатно",
      ]},
    ],
  }, 10, total);

  team(pres, TEAM, 11, total);

  proposal(pres, {
    kicker: "Предложение",
    title: "Соглашение с Минсельхозом Владимирской области",
    leftHead: "Что делаем за первый год, за свой счёт",
    leftRows: [
      ["Обработанные задачи хозяйств и переработчиков", "10"],
      ["Подборов с проверкой", "5"],
      ["Заявок на софинансирование", "2"],
      ["Внедрённый проект", "1"],
      ["Открытый отчёт по АПК региона", "1"],
    ],
    leftNote: "Расходов областного бюджета нет",
    asks: [
      "Соглашение о сотрудничестве",
      "Представление 5–7 хозяйствам и переработчикам",
      "Участие в площадках региона как партнёра",
    ],
    nextStep: null,
  }, 12, total);

  return pres.writeFile({
    fileName: path.join(__dirname, "..", "regions", "vladimir", "sitall-deck-minselhoz.pptx"),
  });
}

// ============= Дек 4: ЗАО «Московская пивоваренная компания» (Мытищи)

// Цифры слайдов «контекст» и «вызовы отрасли» — из разведки
// regions/moscow-oblast/research-mpk-*.md, правьте вместе с ней.

function buildMpk() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.author = "Ситалл";
  pres.title = "Ситалл — для Московской пивоваренной компании";
  const total = 10;

  cover(pres, {
    addressee: "Для ЗАО «Московская пивоваренная компания»",
    month: "СЕНТЯБРЬ 2026",
  }, total);

  statGrid(pres, {
    kicker: "Контекст",
    title: "Рынок пива сжимается — компания растёт",
    cards: [
      { big: "39,3 млн дал", bigSize: 38, caption: "выпуск компании в 2025 году, +11,6% к прошлому — пятое место в стране", src: ["Коммерсантъ", "https://www.kommersant.ru/doc/8401216"] },
      { big: "40,1 млрд ₽", bigSize: 40, caption: "выручка 2025 года, +9,5%; чистая прибыль выросла вдвое", src: ["Аудит-ит", "https://www.audit-it.ru/contragent/1075029007855_zao-mpk"] },
      { big: "−15,7%", caption: "розничные продажи пива в стране за 2025 год", src: ["Ведомости", "https://www.vedomosti.ru/business/articles/2026/07/10/1212619-proizvodstvo-piva-v-rossii-previsilo-potreblenie"] },
      { big: "33 ₽/л", caption: "акциз на пиво в 2026 году — третье повышение подряд", src: ["Ведомости", "https://www.vedomosti.ru/business/articles/2026/07/10/1212619-proizvodstvo-piva-v-rossii-previsilo-potreblenie"] },
    ],
  }, 2, total);

  statRow3(pres, {
    kicker: "Вызовы отрасли",
    title: "Три узких места, которые не закрыть внутри завода",
    cards: [
      { big: "2–5%", caption: "Хмель. Столько потребности страна закрывает своим сырьём; около 7 000 т в год приходит по импорту", src: ["Грейнрус", "https://grainrus.com/novosti-kompanii/articles/mozhet-li-rossiya-obespechivat-sebya-pivovarennym-syrem/"] },
      { big: "+40%", caption: "Солод. Настолько подорожал импортный солод; отечественный — на 15%", src: ["Грейнрус", "https://grainrus.com/novosti-kompanii/articles/mozhet-li-rossiya-obespechivat-sebya-pivovarennym-syrem/"] },
      { big: "10–12%", caption: "Безалкогольное. Рост категории в год при доле 2,4% выпуска — самая быстрая ниша рынка", src: ["Коммерсантъ", "https://www.kommersant.ru/doc/8418080"] },
    ],
    thesis: "За каждым узким местом — российские научные заделы, которые пока не дошли до производства",
  }, 3, total);

  numGrid(pres, WHO, 4, total);
  cycle(pres, CYCLE, 5, total);
  instrument(pres, scout(false), 6, total);

  dirGrid(pres, {
    kicker: "Направления поиска",
    title: "Что мы можем найти под задачи компании",
    cards: [
      ["Хмель", "Отечественные сорта, гранулы и экстракты"],
      ["Дрожжи", "Свой банк штаммов, стабильность брожения"],
      ["Безалкогольное", "Деалкоголизация и вкус без спирта"],
      ["Солод", "Специальные солода под свои рецептуры"],
      ["Глубокий передел", "Дробина и осадки: белок выше кормового"],
      ["Качество", "Экспресс-микробиология, зрение на линии"],
    ],
    note: "Направления — примеры по открытым источникам. Точный список даёт диагностика задачи.",
  }, 7, total);

  numGrid(pres, {
    kicker: "Что получает компания",
    title: "Результат без собственного НИИ",
    cards: [
      { head: "Решение без своего R&D", sub: "Экспертизу дают привлечённые профильные учёные" },
      { head: "Экономика с господдержкой", sub: "На 100 млн ₽ проекта — 15–30 млн ₽ своих: ПП-218 и двойной вычет" },
      { head: "Один управляющий подрядчик", sub: "Вместо десяти переговоров с институтами" },
      { head: "Ответ за 3–4 недели", sub: "«Подходящей технологии нет» — тоже результат" },
    ],
  }, 8, total);

  team(pres, TEAM, 9, total);

  proposal(pres, {
    kicker: "Предложение",
    title: "Пилот для Московской пивоваренной компании",
    leftHead: "Этапы, каждый со своей точкой выхода",
    leftRows: [
      ["Диагностика задачи", "3–4 нед."],
      ["Подбор и проверка: 5–7 решений", "6–8 нед."],
      ["Дальше по выбору компании: сделка,\nгосподдержка, внедрение", "—"],
    ],
    leftNote: "Задачи компании не публикуются: NDA — до начала диагностики",
    asks: [
      "Одна технологическая задача компании",
      "Два часа ваших технологов на диагностику",
      "Рабочая встреча на площадке в Мытищах",
    ],
    nextStep: "Выбираем задачу пилота и согласуем соглашение о неразглашении. Условия — по итогам диагностики.",
  }, 10, total);

  return pres.writeFile({
    fileName: path.join(__dirname, "..", "regions", "moscow-oblast", "sitall-deck-mpk.pptx"),
  });
}

Promise.all([buildUrz(), buildMprso(), buildMshvo(), buildMpk()]).then((files) => {
  console.log("Собрано:", files.join(", "));
});
