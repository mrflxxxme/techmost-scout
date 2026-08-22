const pptxgen = require("pptxgenjs");
const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.33 x 7.5

// ---------- Design tokens ----------
const INKBG = "10163A";
const ORB1  = "161D48";
const ORB2  = "1B2354";
const DSHELL = "171F4A";
const DCORE  = "1C2452";
const DHAIR  = "2C3670";

const NAVY  = "1E2761";
const INK   = "1B2140";
const MUTED = "6B7699";
const GOLD  = "C9A24B";
const ICE   = "CDDCF9";

const SHELL = "F2F5FC";
const CORE  = "FFFFFF";
const HAIR  = "E3EAF8";
const HAIR2 = "EDF2FB";

const F_SB = "Segoe UI Semibold";
const F    = "Segoe UI";

const W = 13.33, M = 0.7;
const CW = W - 2 * M;

// ---------- Sources ----------
const SRC = {
  rdworld: "https://www.rdworldonline.com/research-development-2024-global-rd-investment-forecast/",
  wharton: "https://mackinstitute.wharton.upenn.edu/wp-content/uploads/2019/08/FP0357b_WP_2019Jan.pdf",
  mckinsey: "https://www.mckinsey.com/capabilities/operations/our-insights/how-digital-manufacturing-can-escape-pilot-purgatory",
  issekInno: "https://issek.hse.ru/news/1083755537.html",
  issekFin: "https://issek.hse.ru/news/1089550497.html",
  issekSci: "https://issek.hse.ru/news/1082485052.html",
  govRu: "http://government.ru/news/59530/",
  comnews: "https://www.comnews.ru/content/241497/2025-09-30/2025-w40/1007/tekhnologicheskoe-liderstvo-uydet-19-trln-rub",
  rbc: "https://www.rbc.ru/economics/26/01/2026/6977cac89a7947009cdfec9d",
  kommersantVuz: "https://www.kommersant.ru/doc/8161522",
  rospatent: "https://rospatent.gov.ru/ru/news/rospatent-06122024",
  academcity: "https://academcity.org/content/barery-dlya-innovaciy",
  pp218: "https://pp218.ru/about/",
  consultant: "https://www.consultant.ru/law/hotdocs/86833.html",
  innosc: "https://inno-sc.ru/",
  gisp: "https://gisp.gov.ru/nmp/measure/10511015",
  vmr: "https://www.verifiedmarketreports.com/product/technology-transfer-services-market/",
  minobr: "https://minobrnauki.gov.ru/press-center/news/novosti-ministerstva/81234/",
  rta: "http://rta.gov.ru/engineering",
  yet2: "https://www.yet2.com/why-online-ip-marketplaces-fail/",
  ipwatchdog: "https://www.ipwatchdog.com/2017/10/09/the-changing-face-of-university-technology-transfer/id=88853/",
  law360: "https://www.law360.com/bankruptcy-authority/articles/1790070/patent-marketplace-co-ipwe-hits-ch-11-in-del-",
  yet2wiki: "https://en.wikipedia.org/wiki/Yet2.com",
  // Quote sources
  qKuleshov: "https://expert.ru/mnenie/vse-nachinaetsya-s-nauki/",
  qZubov: "https://rospatent.gov.ru/ru/news/rospatent-06122024",
  qVibe: "https://zhukovsky.life/news/19611/",
  qSekirinsky: "https://www.kommersant.ru/doc/8380748",
  qKrechetov: "https://www.atomic-energy.ru/articles/2026/07/17/167196",
  qFilimonov: "https://rg.ru/2024/03/01/nauka-idet-k-biznesu.html",
};

// ---------- Helpers ----------
function srcRun(name, url, dark) {
  return {
    text: name,
    options: {
      hyperlink: { url },
      fontSize: 8.5,
      color: dark ? ICE : MUTED,
      underline: { style: "sng" },
      italic: true,
    },
  };
}
function srcPrefix(dark) {
  return { text: "Источник: ", options: { fontSize: 8.5, color: dark ? ICE : MUTED, italic: true } };
}
function header(s, kickerText, titleText) {
  s.addText(kickerText.toUpperCase(), {
    x: M, y: 0.38, w: CW, h: 0.28,
    fontFace: F_SB, fontSize: 10, charSpacing: 3, color: GOLD, margin: 0,
  });
  s.addText(titleText, {
    x: M, y: 0.68, w: CW, h: 0.66,
    fontFace: F_SB, fontSize: 28, color: NAVY, margin: 0,
  });
}
function footer(s, n) {
  s.addText("ТехМост", { x: M, y: 7.12, w: 2.5, h: 0.26, fontFace: F_SB, fontSize: 8.5, color: MUTED, margin: 0 });
  s.addText(String(n).padStart(2, "0") + " / 13", {
    x: W - M - 1.2, y: 7.12, w: 1.2, h: 0.26, fontFace: F, fontSize: 8.5, color: MUTED, align: "right", margin: 0,
  });
}
function bezel(s, x, y, w, h, dark) {
  s.addShape("roundRect", {
    x, y, w, h, rectRadius: 0.14,
    fill: { color: dark ? DSHELL : SHELL },
    line: { color: dark ? DHAIR : HAIR, width: 0.75 },
  });
  const p = 0.07;
  s.addShape("roundRect", {
    x: x + p, y: y + p, w: w - 2 * p, h: h - 2 * p, rectRadius: 0.1,
    fill: { color: dark ? DCORE : CORE },
    line: { color: dark ? DHAIR : HAIR2, width: 0.5 },
  });
}
function orbs(s) {
  s.addShape("ellipse", { x: 8.6, y: -2.6, w: 7.4, h: 7.4, fill: { color: ORB1 }, line: { type: "none" } });
  s.addShape("ellipse", { x: 10.6, y: 3.4, w: 5.6, h: 5.6, fill: { color: ORB2 }, line: { type: "none" } });
  s.addShape("ellipse", { x: 11.8, y: 1.5, w: 3.4, h: 3.4, fill: { color: INKBG }, line: { color: DHAIR, width: 0.75 } });
}
function numDot(s, x, y, n, d, gold) {
  s.addShape("ellipse", { x, y, w: d, h: d, fill: { color: gold ? GOLD : NAVY }, line: { type: "none" } });
  s.addText(String(n), {
    x, y: y - 0.02, w: d, h: d, fontFace: F_SB, fontSize: d > 0.45 ? 16 : 14,
    color: "FFFFFF", align: "center", valign: "middle", margin: 0,
  });
}
// Live quote row: big gold « + italic quote + linked attribution
function quoteRow(s, y, qtext, attr, url, opts = {}) {
  s.addText("«", { x: M - 0.04, y: y - 0.16, w: 0.5, h: 0.8, fontFace: F_SB, fontSize: 44, color: GOLD, margin: 0 });
  const runs = [
    { text: qtext + "»  ", options: { fontFace: F, italic: true, color: INK, fontSize: opts.size || 12.5 } },
    { text: "— " + attr, options: { fontFace: F_SB, color: MUTED, fontSize: 10.5, hyperlink: { url }, underline: { style: "sng" } } },
  ];
  s.addText(runs, { x: M + 0.52, y, w: CW - 0.52, h: opts.h || 0.8, margin: 0, valign: "top", lineSpacingMultiple: 1.18 });
}

// ============ S1 — TITLE (dark) ============
{
  const s = pres.addSlide();
  s.background = { color: INKBG };
  orbs(s);
  s.addText("РАБОЧЕЕ НАЗВАНИЕ · АВГУСТ 2026 · КОНФИДЕНЦИАЛЬНО", {
    x: M, y: 0.6, w: CW, h: 0.3, fontFace: F_SB, fontSize: 10, charSpacing: 3, color: GOLD, margin: 0,
  });
  s.addText("ТехМост", {
    x: M - 0.05, y: 1.5, w: 10, h: 1.45, fontFace: F, bold: true, fontSize: 72, color: "FFFFFF", margin: 0,
  });
  s.addText("Внешний офис трансфера технологий\nдля промышленности", {
    x: M, y: 3.12, w: 9.5, h: 1.0, fontFace: F_SB, fontSize: 22, color: ICE, margin: 0, lineSpacingMultiple: 1.12,
  });
  s.addText(
    "Находим, проверяем и доводим до производства научные разработки под задачи вашего бизнеса — с привлечением господдержки, покрывающей до 70% стоимости исследований.",
    { x: M, y: 4.42, w: 8.6, h: 1.1, fontFace: F, fontSize: 14, color: "FFFFFF", margin: 0, lineSpacingMultiple: 1.25 }
  );
  s.addText("Для якорных клиентов, партнёров и инвесторов", {
    x: M, y: 5.82, w: 8, h: 0.32, fontFace: F, fontSize: 11.5, color: ICE, margin: 0,
  });
  s.addText("Кирилл Уклонский · uklonskiy.k@gmail.com", {
    x: M, y: 6.52, w: 8, h: 0.35, fontFace: F_SB, fontSize: 13.5, color: "FFFFFF", margin: 0,
  });
}

// ============ S2 — PROBLEM ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Проблема", "Денег в науке всё больше — отдачи всё меньше");
  const cw2 = (CW - 0.45) / 2, ch = 2.05;
  const cards = [
    [M, 1.58, "$2,53 трлн", "мировые расходы на исследования и разработки в 2024 году — рекорд за всю историю наблюдений", NAVY, ["R&D World", SRC.rdworld]],
    [M + cw2 + 0.45, 1.58, "80–88%", "патентов так и не доходят до применения в реальном продукте", GOLD, ["Wharton, Mack Institute", SRC.wharton]],
    [M, 1.58 + ch + 0.3, "70%", "промышленных компаний годами не могут вывести пилотные проекты в масштаб", GOLD, ["McKinsey", SRC.mckinsey]],
    [M + cw2 + 0.45, 1.58 + ch + 0.3, "6%", "выпуска российских предприятий приходится на инновационную продукцию", NAVY, ["ИСИЭЗ НИУ ВШЭ", SRC.issekInno]],
  ];
  for (const [x, y, num, label, numColor, src] of cards) {
    bezel(s, x, y, cw2, ch, false);
    s.addText(num, {
      x: x + 0.32, y: y + 0.14, w: cw2 - 0.64, h: 0.8,
      fontFace: F_SB, fontSize: 40, color: numColor, margin: 0, valign: "middle",
    });
    s.addText(label, {
      x: x + 0.32, y: y + 0.98, w: cw2 - 0.64, h: 0.68,
      fontFace: F, fontSize: 12, color: INK, margin: 0, valign: "top", lineSpacingMultiple: 1.1,
    });
    s.addText([srcPrefix(false), srcRun(src[0], src[1], false)], {
      x: x + 0.32, y: y + ch - 0.34, w: cw2 - 0.64, h: 0.28, margin: 0, fontFace: F,
    });
  }
  quoteRow(s, 6.3,
    "Все говорят: давайте мы подождём, попилотируем дальше, поиграемся в песочницы",
    "Александр Вибе, гендиректор компании-интегратора цифровых решений «Росатома», Иннопром-2026",
    SRC.qVibe, { h: 0.72 });
  footer(s, 2);
}

// ============ S3 — WHY NOW ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Почему сейчас", "Окно возможностей в России открылось в 2025–2026");
  const items = [
    [["Поручения премьер-министра, август 2026.", " Правительство переходит к управлению полным жизненным циклом технологий: дорожная карта — к октябрю, единая система учёта исследований — к декабрю 2026.  "], ["government.ru", SRC.govRu]],
    [["Национальная цель: наука — 2% ВВП к 2030.", " Вложения бизнеса в науку должны минимум удвоиться. Сегодня 65% исследований в стране оплачивает государство.  "], ["ИСИЭЗ НИУ ВШЭ", SRC.issekFin]],
    [["Нацпроекты технологического лидерства.", " Девять программ, около 1,9 трлн рублей в бюджете на 2026–2028 годы.  "], ["ComNews", SRC.comnews]],
    [["Дефицит кадров — 2,6 млн человек.", " Нанимать больше некого. Расти можно только за счёт производительности.  "], ["РБК", SRC.rbc]],
  ];
  let y = 1.62;
  for (let i = 0; i < items.length; i++) {
    numDot(s, M, y + 0.04, i + 1, 0.42, false);
    s.addText([
      { text: items[i][0][0], options: { fontFace: F_SB, color: NAVY } },
      { text: items[i][0][1], options: { fontFace: F, color: INK } },
      srcRun(items[i][1][0], items[i][1][1], false),
    ], { x: M + 0.64, y, w: 7.6, h: 1.1, fontSize: 12.5, margin: 0, valign: "top", lineSpacingMultiple: 1.16 });
    y += 1.17;
  }
  const rx = M + 8.7, rw = CW - 8.7;
  s.addShape("roundRect", { x: rx, y: 1.62, w: rw, h: 2.2, rectRadius: 0.14, fill: { color: INKBG }, line: { type: "none" } });
  s.addText("2% ВВП", { x: rx, y: 1.85, w: rw, h: 0.95, fontFace: F_SB, fontSize: 42, color: "FFFFFF", align: "center", margin: 0 });
  s.addText("целевые затраты на науку к 2030 году — сейчас около 1%", { x: rx + 0.35, y: 2.92, w: rw - 0.7, h: 0.75, fontFace: F, fontSize: 11.5, color: ICE, align: "center", margin: 0, lineSpacingMultiple: 1.15 });
  bezel(s, rx, 4.0, rw, 2.2, false);
  s.addText("×2", { x: rx, y: 4.23, w: rw, h: 0.95, fontFace: F_SB, fontSize: 42, color: GOLD, align: "center", margin: 0 });
  s.addText("рост частных вложений в науку — национальная цель", { x: rx + 0.35, y: 5.3, w: rw - 0.7, h: 0.75, fontFace: F, fontSize: 11.5, color: INK, align: "center", margin: 0, lineSpacingMultiple: 1.15 });
  quoteRow(s, 6.55,
    "Бизнес в полной мере осознал, что находится не в «технологическом супермаркете», где всё можно купить",
    "Денис Секиринский, замминистра науки и высшего образования, «Коммерсантъ»",
    SRC.qSekirinsky, { h: 0.55, size: 11.5 });
  footer(s, 3);
}

// ============ S4 — THE GAP ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Ниша", "Между наукой и производством — разрыв");
  const by = 1.72, bh = 3.85, bw = 3.72;
  // Science
  bezel(s, M, by, bw, bh, false);
  s.addText("НАУКА", { x: M + 0.3, y: by + 0.24, w: bw - 0.6, h: 0.35, fontFace: F_SB, fontSize: 13, charSpacing: 2, color: NAVY, margin: 0 });
  s.addText([
    { text: "339 тысяч исследователей; вузы подают 27% патентных заявок страны ", options: { bullet: { code: "2022" }, color: INK } },
    srcRun("ИСИЭЗ", SRC.issekSci, false),
    { text: "", options: { breakLine: true } },
    { text: "Разработки выходят на лабораторной стадии — до завода их никто не доводит", options: { bullet: { code: "2022" }, breakLine: true, color: INK } },
    { text: "Продавать не умеет: лишь 12% затрат вузов приходится на стадию разработок ", options: { bullet: { code: "2022" }, color: INK } },
    srcRun("Коммерсантъ", SRC.kommersantVuz, false),
  ], { x: M + 0.3, y: by + 0.72, w: bw - 0.58, h: bh - 0.95, fontFace: F, fontSize: 12, margin: 0, paraSpaceAfter: 11, valign: "top", lineSpacingMultiple: 1.16 });
  // Gap
  const gx = M + bw + 0.42;
  s.addShape("roundRect", { x: gx, y: by, w: bw, h: bh, rectRadius: 0.14, fill: { color: "FFFFFF" }, line: { color: GOLD, width: 1.25, dashType: "dash" } });
  s.addText("ЗДЕСЬ НИКОГО НЕТ", { x: gx + 0.3, y: by + 0.24, w: bw - 0.6, h: 0.35, fontFace: F_SB, fontSize: 13, charSpacing: 2, color: GOLD, margin: 0 });
  s.addText([
    { text: "Коммерческих брокеров технологий в России нет как рынка — наш вывод из исследования", options: { bullet: { code: "2022" }, breakLine: true, color: INK } },
    { text: "Роспатент подготовил больше 200 технологических брокеров — им негде работать ", options: { bullet: { code: "2022" }, color: INK } },
    srcRun("Роспатент", SRC.rospatent, false),
    { text: "", options: { breakLine: true } },
    { text: "Госцентры учат и ведут учёт, но за сделку и внедрение не отвечает никто", options: { bullet: { code: "2022" }, color: INK } },
  ], { x: gx + 0.3, y: by + 0.72, w: bw - 0.58, h: bh - 0.95, fontFace: F, fontSize: 12, margin: 0, paraSpaceAfter: 11, valign: "top", lineSpacingMultiple: 1.16 });
  // Business
  const bx = gx + bw + 0.42;
  s.addShape("roundRect", { x: bx, y: by, w: bw, h: bh, rectRadius: 0.14, fill: { color: INKBG }, line: { type: "none" } });
  s.addText("БИЗНЕС", { x: bx + 0.3, y: by + 0.24, w: bw - 0.6, h: 0.35, fontFace: F_SB, fontSize: 13, charSpacing: 2, color: "FFFFFF", margin: 0 });
  s.addText([
    { text: "Покупает только то, что уже работает на производстве", options: { bullet: { code: "2022" }, breakLine: true, color: ICE } },
    { text: "Половина предприятий сотрудничает с учёными разово, от случая к случаю ", options: { bullet: { code: "2022" }, color: ICE } },
    srcRun("government.ru", SRC.govRu, true),
    { text: "", options: { breakLine: true } },
    { text: "Главный страх — экономический риск внедрения; его называет каждая четвёртая компания ", options: { bullet: { code: "2022" }, color: ICE } },
    srcRun("Академгородок", SRC.academcity, true),
  ], { x: bx + 0.3, y: by + 0.72, w: bw - 0.58, h: bh - 0.95, fontFace: F, fontSize: 12, margin: 0, paraSpaceAfter: 11, valign: "top", lineSpacingMultiple: 1.16 });
  // arrows
  s.addShape("rightArrow", { x: M + bw + 0.05, y: by + bh / 2 - 0.14, w: 0.34, h: 0.28, fill: { color: MUTED }, line: { type: "none" } });
  s.addShape("rightArrow", { x: gx + bw + 0.05, y: by + bh / 2 - 0.14, w: 0.34, h: 0.28, fill: { color: MUTED }, line: { type: "none" } });
  quoteRow(s, 5.95,
    "Институты и прежде всего люди, которые умеют превращать научный прототип в промышленный продукт, почти исчезли",
    "Александр Кулешов, ректор Сколтеха, академик РАН, «Эксперт»",
    SRC.qKuleshov, { h: 0.85 });
  footer(s, 4);
}

// ============ S5 — SOLUTION LADDER ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Решение", "Лестница услуг: от задачи до работающей технологии");
  const steps = [
    ["Диагностика", "Разбираемся, где предприятие теряет деньги и какие задачи можно закрыть внешней разработкой", "3–4 недели"],
    ["Поиск и проверка", "Приносим короткий список решений из сети институтов. Каждое проверено учёными: зрелость, команда, применимость", "6–8 недель"],
    ["Сделка и господдержка", "Собираем договор и софинансирование: субсидия по 218-му постановлению, двойной налоговый вычет, гранты на доращивание", "2–4 месяца"],
    ["Внедрение", "Ведём проект до результата как генеральный подрядчик — руками партнёров: инжиниринговых центров и самих институтов. Клиент получает одного подрядчика", "6–18 месяцев"],
  ];
  const sw = (CW - 3 * 0.38) / 4, sy = 2.1, sh = 3.55;
  for (let i = 0; i < 4; i++) {
    const x = M + i * (sw + 0.38);
    numDot(s, x + sw / 2 - 0.25, 1.62, i + 1, 0.5, i === 3);
    if (i === 3) {
      s.addShape("roundRect", { x, y: sy + 0.32, w: sw, h: sh, rectRadius: 0.14, fill: { color: INKBG }, line: { type: "none" } });
    } else {
      bezel(s, x, sy + 0.32, sw, sh, false);
    }
    const dark = i === 3;
    s.addText(steps[i][0], { x: x + 0.26, y: sy + 0.56, w: sw - 0.52, h: 0.8, fontFace: F_SB, fontSize: 15, color: dark ? "FFFFFF" : NAVY, margin: 0, valign: "top", lineSpacingMultiple: 1.05 });
    s.addText(steps[i][1], { x: x + 0.26, y: sy + 1.4, w: sw - 0.52, h: 1.95, fontFace: F, fontSize: 11.5, color: dark ? ICE : INK, margin: 0, valign: "top", lineSpacingMultiple: 1.16 });
    s.addShape("line", { x: x + 0.26, y: sy + sh - 0.42, w: sw - 0.52, h: 0, line: { color: dark ? DHAIR : HAIR, width: 0.75 } });
    s.addText(steps[i][2], { x: x + 0.26, y: sy + sh - 0.34, w: sw - 0.52, h: 0.3, fontFace: F_SB, fontSize: 10.5, color: dark ? GOLD : MUTED, margin: 0 });
  }
  quoteRow(s, 6.25,
    "Чтобы наука и бизнес нашли друг друга, нужен технологический брокер",
    "Юрий Зубов, руководитель Роспатента",
    SRC.qZubov, { h: 0.6 });
  footer(s, 5);
}

// ============ S6 — GOV SUPPORT ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Ценность для клиента", "Господдержка делает внедрение кратно дешевле");
  const tools = [
    [["Постановление № 218", " — государство возмещает промышленному заказчику до 70% стоимости исследований, заказанных у вуза или института.  "], ["pp218.ru", SRC.pp218]],
    [["Двойной налоговый вычет", " — затраты на исследования уменьшают налог на прибыль с коэффициентом 2. При ставке 25% это ощутимо.  "], ["КонсультантПлюс", SRC.consultant]],
    [["Гранты на доращивание", " — до 250 млн рублей на доработку технологии под запрос конкретной корпорации.  "], ["inno-sc.ru", SRC.innosc]],
    [["Специальный инвестконтракт", " — налоговые льготы до 15–20 лет за внедрение технологий из правительственного перечня.  "], ["ГИСП", SRC.gisp]],
  ];
  let y = 1.62;
  for (const [parts, src] of tools) {
    bezel(s, M, y, 7.45, 1.0, false);
    s.addText([
      { text: parts[0], options: { fontFace: F_SB, color: NAVY } },
      { text: parts[1], options: { fontFace: F, color: INK } },
      srcRun(src[0], src[1], false),
    ], { x: M + 0.3, y: y + 0.06, w: 6.9, h: 0.88, fontSize: 11.5, margin: 0, valign: "middle", lineSpacingMultiple: 1.1 });
    y += 1.13;
  }
  const rx = M + 7.95, rw = CW - 7.95;
  s.addShape("roundRect", { x: rx, y: 1.62, w: rw, h: 4.52, rectRadius: 0.14, fill: { color: INKBG }, line: { type: "none" } });
  s.addText("ПРИМЕР СДЕЛКИ · НАША ОЦЕНКА", { x: rx + 0.36, y: 1.86, w: rw - 0.72, h: 0.32, fontFace: F_SB, fontSize: 9.5, charSpacing: 2, color: GOLD, margin: 0 });
  s.addText([
    { text: "Исследовательский проект: 100 млн ₽", options: { breakLine: true, fontFace: F_SB, color: "FFFFFF" } },
    { text: "Субсидия по 218-му: до −70 млн ₽", options: { breakLine: true, fontFace: F, color: ICE } },
    { text: "Двойной вычет: ещё −7–15 млн ₽ налога", options: { fontFace: F, color: ICE } },
  ], { x: rx + 0.36, y: 2.28, w: rw - 0.72, h: 1.4, fontSize: 12.5, margin: 0, paraSpaceAfter: 8, valign: "top" });
  s.addShape("line", { x: rx + 0.36, y: 3.78, w: rw - 0.72, h: 0, line: { color: DHAIR, width: 0.75 } });
  s.addText("≈ 15–30 млн ₽", { x: rx + 0.36, y: 3.95, w: rw - 0.72, h: 0.8, fontFace: F_SB, fontSize: 33, color: GOLD, margin: 0 });
  s.addText("реальная стоимость технологии для клиента, если сделка собрана грамотно", { x: rx + 0.36, y: 4.85, w: rw - 0.72, h: 1.1, fontFace: F, fontSize: 11.5, color: ICE, margin: 0, lineSpacingMultiple: 1.2 });
  quoteRow(s, 6.42,
    "Без внешнего содействия малые технологические компании обычно используют 30–40% положенных им преференций",
    "Станислав Кречетов, гендиректор «Иннохаба Росатома»",
    SRC.qKrechetov, { h: 0.62, size: 12 });
  footer(s, 6);
}

// ============ S7 — BUSINESS MODEL ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Бизнес-модель", "Проектные гонорары плюс комиссия за успех");
  const hdr = (t) => ({ text: t, options: { fontFace: F_SB, color: "FFFFFF", fontSize: 12, fill: { color: NAVY } } });
  const cell = (t) => ({ text: t, options: { fontFace: F, color: INK, fontSize: 12 } });
  const rows = [
    [hdr("Продукт"), hdr("Чек"), hdr("Срок")],
    [cell("Диагностика технологических задач"), cell("0,8–1,5 млн ₽"), cell("3–4 нед.")],
    [cell("Подбор и научная проверка решений"), cell("1,5–4 млн ₽"), cell("6–8 нед.")],
    [cell("Структурирование сделки и господдержки"), cell("2–5 млн ₽ + комиссия"), cell("2–4 мес.")],
    [cell("Сопровождение внедрения (генподряд)"), cell("10–20% бюджета проекта"), cell("6–18 мес.")],
    [cell("Комиссия за успех от суммы сделки"), cell("около 10%, с потолком"), cell("—")],
  ];
  s.addTable(rows, {
    x: M, y: 1.68, w: 7.9, h: 4.2,
    colW: [4.4, 2.2, 1.3],
    border: { type: "solid", color: HAIR, pt: 0.75 },
    fill: { color: "FFFFFF" },
    valign: "middle", margin: 0.1, autoPage: false, rowH: 0.7,
  });
  s.addText([
    { text: "Цены и комиссия — наша оценка; ориентир по комиссии — практика yet2. ", options: { fontFace: F, italic: true, color: MUTED, fontSize: 9.5 } },
    srcRun("yet2.com", SRC.yet2, false),
  ], { x: M, y: 6.0, w: 7.9, h: 0.3, margin: 0 });
  const rx = M + 8.35, rw = CW - 8.35;
  const pts = [
    ["Деньги — с первого квартала", "выручка от проектов страхует длинный цикл больших сделок (6–12 месяцев)"],
    ["Подписка со второго года", "«внешний исследовательский офис»: 1,5–3 млн ₽ в квартал плюс комиссия"],
    ["Доли в компаниях — год 3+", "5–10% в новых компаниях, когда появятся поток сделок и репутация"],
    ["Ноль — на 2 проектах в квартал", "точка безубыточности при команде из трёх человек (наша оценка)"],
  ];
  let y = 1.68;
  for (const [h, d] of pts) {
    bezel(s, rx, y, rw, 1.13, false);
    s.addText(h, { x: rx + 0.28, y: y + 0.1, w: rw - 0.56, h: 0.36, fontFace: F_SB, fontSize: 12.5, color: NAVY, margin: 0 });
    s.addText(d, { x: rx + 0.28, y: y + 0.48, w: rw - 0.56, h: 0.6, fontFace: F, fontSize: 10.5, color: INK, margin: 0, lineSpacingMultiple: 1.08, valign: "top" });
    y += 1.27;
  }
  footer(s, 7);
}

// ============ S8 — MARKET ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Рынок", "Ниша растёт вместе с национальной целью");
  s.addText([
    { text: "Частные вложения в науку России, млрд ₽ в год  ", options: { fontFace: F_SB, color: INK, fontSize: 12 } },
    srcRun("ИСИЭЗ НИУ ВШЭ", SRC.issekFin, false),
  ], { x: M, y: 1.66, w: 6.2, h: 0.32, margin: 0 });
  s.addChart("bar", [
    { name: "Вложения бизнеса в науку", labels: ["2024, факт", "2030, цель ×2"], values: [608, 1216] },
  ], {
    x: M, y: 2.02, w: 6.2, h: 3.65,
    barDir: "col",
    chartColors: [NAVY],
    barGapWidthPct: 60,
    showValue: true, dataLabelPosition: "outEnd", dataLabelColor: NAVY, dataLabelFontSize: 13, dataLabelFontBold: true, dataLabelFontFace: F_SB,
    showLegend: false, showTitle: false,
    catAxisLabelColor: INK, catAxisLabelFontSize: 11.5, catAxisLabelFontFace: F,
    valAxisLabelColor: MUTED, valAxisLabelFontSize: 9.5, valAxisLabelFontFace: F,
    valGridLine: { color: "EDF2FB", size: 1 },
    catGridLine: { style: "none" },
    valAxisMaxVal: 1400,
  });
  s.addText("К 2030 году в науке должно появиться плюс 600 млрд рублей частных денег ежегодно. Этим деньгам нужны проводники.", {
    x: M, y: 5.85, w: 6.2, h: 0.8, fontFace: F_SB, fontSize: 12.5, color: NAVY, margin: 0, lineSpacingMultiple: 1.18,
  });
  const rx = M + 6.75, rw = CW - 6.75;
  const blocks = [
    ["$1,8 → 4,3 млрд", "мировой рынок услуг трансфера технологий, 2024 → 2033. Оценка отраслевых агентств, берём как порядок величины.  ", ["Verified Market Reports", SRC.vmr], false],
    ["1,88 трлн ₽", "весь объём исследований и разработок в России за 2024 год. Наши первые отрасли: нефтегаз и энергетика, фарма, агропром.  ", ["ИСИЭЗ НИУ ВШЭ", SRC.issekSci], false],
    ["~0", "коммерческих брокеров-принципалов в России сегодня — наш вывод из исследования рынка. Первый системный игрок задаёт стандарт.", null, true],
  ];
  let y = 1.66;
  for (const [n, d, src, dark] of blocks) {
    if (dark) {
      s.addShape("roundRect", { x: rx, y, w: rw, h: 1.6, rectRadius: 0.14, fill: { color: INKBG }, line: { type: "none" } });
    } else {
      bezel(s, rx, y, rw, 1.6, false);
    }
    s.addText(n, { x: rx + 0.3, y: y + 0.1, w: rw - 0.6, h: 0.58, fontFace: F_SB, fontSize: 24, color: dark ? GOLD : NAVY, margin: 0 });
    const runs = [{ text: d, options: { fontFace: F, color: dark ? ICE : INK, fontSize: 10.5 } }];
    if (src) runs.push(srcRun(src[0], src[1], dark));
    s.addText(runs, { x: rx + 0.3, y: y + 0.7, w: rw - 0.6, h: 0.84, margin: 0, lineSpacingMultiple: 1.12, valign: "top" });
    y += 1.78;
  }
  footer(s, 8);
}

// ============ S9 — COMPETITION ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Конкуренция", "Поле игроков: место принципала свободно");
  const hdr = (t) => ({ text: t, options: { fontFace: F_SB, color: "FFFFFF", fontSize: 12, fill: { color: NAVY } } });
  const cell = (t) => ({ text: t, options: { fontFace: F, color: INK, fontSize: 11.5 } });
  const rows = [
    [hdr("Кто"), hdr("Что делают"), hdr("Кто они для нас")],
    [cell("Госцентры: центры трансфера технологий при вузах (38), Роспатент, Агентство по техразвитию"), cell("Ведут учёт разработок, учат брокеров, раздают гранты. За сделку и внедрение не отвечают"), cell("Партнёры: источник разработок и кадров")],
    [cell("Научно-технические центры корпораций"), cell("Инновации для себя. Есть только у крупнейших: Газпром нефть, СИБУР, Северсталь"), cell("Клиенты: мы дополняем их команды, а не соревнуемся с ними")],
    [cell("Инжиниринговые центры при вузах (72)"), cell("Исполняют работы. Выручка сектора выше 25 млрд ₽ — вчетверо больше вложенных субсидий"), cell("Партнёры по внедрению")],
    [cell("Мировые аналоги: yet2, IN-PART, Halo"), cell("Доказали, что корпорации платят за подбор технологий. В России не работают"), cell("Готовый плейбук: 25 лет чужих проб и ошибок")],
  ];
  s.addTable(rows, {
    x: M, y: 1.68, w: CW, h: 4.0,
    colW: [3.9, 4.8, 3.23],
    border: { type: "solid", color: HAIR, pt: 0.75 },
    valign: "middle", margin: 0.1, autoPage: false, rowH: 0.78,
  });
  s.addText([
    srcPrefix(false),
    srcRun("Минобрнауки", SRC.minobr, false),
    { text: " · ", options: { fontSize: 8.5, color: MUTED } },
    srcRun("rta.gov.ru", SRC.rta, false),
    { text: " · ", options: { fontSize: 8.5, color: MUTED } },
    srcRun("yet2.com", SRC.yet2, false),
  ], { x: M, y: 5.78, w: CW, h: 0.28, margin: 0 });
  quoteRow(s, 6.28,
    "Необходимо насытить инновационную экосистему активными и компетентными свободными агентами: техноброкерами и технопредпринимателями",
    "Алексей Филимонов, исполнительный директор Национальной ассоциации трансфера технологий",
    SRC.qFilimonov, { h: 0.72, size: 12 });
  footer(s, 9);
}

// ============ S10 — TEAM ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Команда", "Доверие обеих сторон рынка — в одной команде");
  const cards = [
    ["Кирилл Уклонский, основатель", "Продаёт и ведёт консалтинговые проекты, знает госпрограммы и институты развития изнутри. Личная сеть руководителей в нефтегазе и энергетике, фарме, агропроме", true],
    ["Научный со-основатель", "Учёный с именем в своей области: проверяет решения, открывает двери лабораторий, приводит поток разработок", false],
    ["Сеть экспертов", "Отраслевые специалисты подключаются под конкретный проект; юрист по интеллектуальной собственности — по контракту. Постоянных издержек на старте почти нет", false],
  ];
  const cw3 = (CW - 2 * 0.42) / 3;
  for (let i = 0; i < 3; i++) {
    const x = M + i * (cw3 + 0.42);
    const dark = cards[i][2];
    if (dark) {
      s.addShape("roundRect", { x, y: 1.78, w: cw3, h: 3.9, rectRadius: 0.14, fill: { color: INKBG }, line: { type: "none" } });
    } else {
      bezel(s, x, 1.78, cw3, 3.9, false);
    }
    s.addText(cards[i][0], { x: x + 0.3, y: 2.06, w: cw3 - 0.6, h: 0.78, fontFace: F_SB, fontSize: 15.5, color: dark ? "FFFFFF" : NAVY, margin: 0, valign: "top", lineSpacingMultiple: 1.05 });
    s.addText(cards[i][1], { x: x + 0.3, y: 2.9, w: cw3 - 0.6, h: 2.6, fontFace: F, fontSize: 12.5, color: dark ? ICE : INK, margin: 0, valign: "top", lineSpacingMultiple: 1.25 });
  }
  s.addText([
    { text: "Бенчмарк: у yet2, IN-PART и Halo в первый год работало по 2–4 человека — «переводчик» между наукой и бизнесом плюс научный авторитет. ", options: { fontFace: F_SB, color: NAVY, fontSize: 12.5 } },
    srcRun("история yet2", SRC.yet2wiki, false),
  ], { x: M, y: 6.05, w: CW, h: 0.6, margin: 0, lineSpacingMultiple: 1.15 });
  footer(s, 10);
}

// ============ S11 — ROADMAP ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Дорожная карта", "Деньги вкладываем только после доказательств");
  const phases = [
    ["ФАЗА 0 · ДО ДЕК. 2026", "Проверка спроса", "15–20 разговоров с руководителями об их реальных задачах; витрина 20–30 разработок; предложения пилотов по тёплой сети", "Ворота: 1–2 оплаченных пилота"],
    ["ФАЗА 1 · 2027", "Первые сделки", "Компания и команда из 3–4 человек; 3–5 проектов подбора; первая сделка с господдержкой; якорный клиент", "Выручка 15–40 млн ₽"],
    ["ФАЗА 2 · 2028", "Масштабирование", "Подписка «внешний исследовательский офис»; зарубежное юрлицо; выход на дружественные рынки", "Выручка 60–120 млн ₽"],
    ["ФАЗА 3 · 2029+", "Слои роста", "Доли в новых компаниях; база данных сделок и разработок как платформа; фонд доращивания технологий", "Апсайд"],
  ];
  const pw = (CW - 3 * 0.38) / 4;
  for (let i = 0; i < 4; i++) {
    const x = M + i * (pw + 0.38);
    const dark = i === 0;
    if (dark) {
      s.addShape("roundRect", { x, y: 1.78, w: pw, h: 4.35, rectRadius: 0.14, fill: { color: INKBG }, line: { type: "none" } });
    } else {
      bezel(s, x, 1.78, pw, 4.35, false);
    }
    s.addText(phases[i][0], { x: x + 0.26, y: 2.02, w: pw - 0.52, h: 0.32, fontFace: F_SB, fontSize: 9, charSpacing: 1.5, color: dark ? GOLD : MUTED, margin: 0 });
    s.addText(phases[i][1], { x: x + 0.26, y: 2.38, w: pw - 0.52, h: 0.55, fontFace: F_SB, fontSize: 16.5, color: dark ? "FFFFFF" : NAVY, margin: 0 });
    s.addText(phases[i][2], { x: x + 0.26, y: 3.0, w: pw - 0.52, h: 2.35, fontFace: F, fontSize: 11.5, color: dark ? ICE : INK, margin: 0, valign: "top", lineSpacingMultiple: 1.22 });
    s.addShape("line", { x: x + 0.26, y: 5.48, w: pw - 0.52, h: 0, line: { color: dark ? DHAIR : HAIR, width: 0.75 } });
    s.addText(phases[i][3], { x: x + 0.26, y: 5.58, w: pw - 0.52, h: 0.5, fontFace: F_SB, fontSize: 11.5, color: dark ? GOLD : NAVY, margin: 0, valign: "top" });
  }
  s.addText("Сейчас — Фаза 0: без увольнения и почти без затрат. Планы и цифры фаз — наша оценка.", {
    x: M, y: 6.4, w: CW, h: 0.4, fontFace: F, italic: true, fontSize: 11, color: MUTED, margin: 0,
  });
  footer(s, 11);
}

// ============ S12 — LESSONS ============
{
  const s = pres.addSlide();
  s.background = { color: "FFFFFF" };
  header(s, "Устойчивость модели", "Мы строим по урокам выживших — и погибших");
  const lessons = [
    ["Маркетплейсы технологий умирают", "Из ~30 патентных бирж 1999 года выжил один yet2, и то после разворота в сервис. Мы начинаем с сервиса; платформа — усилитель, а не продукт.  ", ["yet2.com", SRC.yet2]],
    ["Платит тот, у кого есть деньги", "73% университетских офисов трансфера в мире убыточны. Наш плательщик — корпорация, для науки участие бесплатно.  ", ["IPWatchdog", SRC.ipwatchdog]],
    ["Деньги — с первого квартала", "Патентная платформа IPwe обанкротилась в 2024 году: длинные циклы продаж съели капитал. Нашу кассу закрывает проектная выручка.  ", ["Law360", SRC.law360]],
    ["Доверие и данные не копируются", "Нейросети удешевляют поиск технологий. Ценность уходит в проверку, историю сделок и отношения — их не собрать промптом (наш вывод).", null],
  ];
  const cw2 = (CW - 0.45) / 2, ch = 2.35;
  for (let i = 0; i < 4; i++) {
    const x = M + (i % 2) * (cw2 + 0.45);
    const y = 1.72 + Math.floor(i / 2) * (ch + 0.32);
    bezel(s, x, y, cw2, ch, false);
    s.addText(lessons[i][0], { x: x + 0.32, y: y + 0.22, w: cw2 - 0.64, h: 0.42, fontFace: F_SB, fontSize: 14.5, color: NAVY, margin: 0 });
    const runs = [{ text: lessons[i][1], options: { fontFace: F, color: INK, fontSize: 12 } }];
    if (lessons[i][2]) runs.push(srcRun(lessons[i][2][0], lessons[i][2][1], false));
    s.addText(runs, { x: x + 0.32, y: y + 0.72, w: cw2 - 0.64, h: ch - 0.95, margin: 0, valign: "top", lineSpacingMultiple: 1.22 });
  }
  footer(s, 12);
}

// ============ S13 — ASK (dark) ============
{
  const s = pres.addSlide();
  s.background = { color: INKBG };
  orbs(s);
  s.addText("ПРИГЛАШАЕМ В ПРОЕКТ", {
    x: M, y: 0.55, w: CW, h: 0.32, fontFace: F_SB, fontSize: 10.5, charSpacing: 3, color: GOLD, margin: 0,
  });
  s.addText("Три способа войти первыми", {
    x: M, y: 0.98, w: CW, h: 0.85, fontFace: F_SB, fontSize: 36, color: "FFFFFF", margin: 0,
  });
  const offers = [
    ["Якорным клиентам", "Пилот «диагностика + подбор» под вашу производственную задачу: 6–8 недель, 1,5–4 млн ₽. Первым двум клиентам — условия основателя и приоритет в очереди разработок"],
    ["Партнёрам", "Институтам, центрам трансфера и инжиниринговым центрам: приводим платящих корпоративных заказчиков и даём стандарт упаковки разработок. Вы делаете науку и исполнение — мы приводим рынок"],
    ["Инвесторам", "После первых оплаченных пилотов поднимаем 10–40 млн ₽ на ускорение: команда и запас на 12–18 месяцев работы. Хотите раньше — присоединяйтесь к пилотной фазе советником"],
  ];
  const cw3 = (CW - 2 * 0.42) / 3;
  for (let i = 0; i < 3; i++) {
    const x = M + i * (cw3 + 0.42);
    bezel(s, x, 2.15, cw3, 3.6, true);
    s.addText(offers[i][0], { x: x + 0.32, y: 2.44, w: cw3 - 0.64, h: 0.5, fontFace: F_SB, fontSize: 16, color: GOLD, margin: 0 });
    s.addText(offers[i][1], { x: x + 0.32, y: 3.02, w: cw3 - 0.64, h: 2.55, fontFace: F, fontSize: 12, color: "FFFFFF", margin: 0, valign: "top", lineSpacingMultiple: 1.25 });
  }
  s.addText("Кирилл Уклонский · uklonskiy.k@gmail.com · «ТехМост» (рабочее название)", {
    x: M, y: 6.35, w: CW, h: 0.4, fontFace: F_SB, fontSize: 13.5, color: ICE, margin: 0,
  });
}

pres.writeFile({ fileName: "pitch-deck.pptx" }).then(() => console.log("OK: pitch-deck.pptx"));
