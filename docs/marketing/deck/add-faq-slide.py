# -*- coding: utf-8 -*-
"""Add FAQ slide as #13 to the TechMost pitch deck, renumber footers 13->14."""
import copy, re
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn

PATH = r"C:\Users\KUklonskiy\DBH\docs\rd-commercialization\ТехМост (питчдек v1).pptx"

NAVY = RGBColor(0x1E, 0x27, 0x61)
INK = RGBColor(0x1B, 0x21, 0x40)
GOLD = RGBColor(0xC9, 0xA2, 0x4B)
MUTED = RGBColor(0x6B, 0x76, 0x99)
CARD_BG = RGBColor(0xF2, 0xF5, 0xFC)
CARD_LN = RGBColor(0xE3, 0xEA, 0xF8)
INNER_LN = RGBColor(0xED, 0xF2, 0xFB)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
FONT = "Segoe UI"
FONT_SB = "Segoe UI Semibold"

prs = Presentation(PATH)
tpl = prs.slides[11]  # slide 12 — same look

# card corner radii as in slide 12 (outer id=4, inner id=5)
adj = {}
for sh in tpl.shapes:
    g = sh._element.find(qn('a:prstGeom')) if sh._element.find(qn('a:prstGeom')) is not None else None
    sp = sh._element.find('.//' + qn('a:prstGeom'))
    if sp is not None and sp.get('prst') == 'roundRect':
        gd = sp.find('.//' + qn('a:gd'))
        if gd is not None:
            adj[sh.shape_id] = gd.get('fmla')
print("template adj:", adj)
ADJ_OUTER = int((adj.get(4) or 'val 4525').split()[1])
ADJ_INNER = int((adj.get(5) or 'val 5957').split()[1])

slide = prs.slides.add_slide(tpl.slide_layout)

# white background, same as other slides
bg_src = tpl._element.find(qn('p:cSld')).find(qn('p:bg'))
if bg_src is not None:
    slide._element.find(qn('p:cSld')).insert(0, copy.deepcopy(bg_src))

def set_adj(shape, val):
    geom = shape._element.find('.//' + qn('a:prstGeom'))
    av = geom.find(qn('a:avLst'))
    for gd in list(av):
        av.remove(gd)
    gd = av.makeelement(qn('a:gd'), {'name': 'adj', 'fmla': f'val {val}'})
    av.append(gd)

def textbox(x, y, w, h, anchor=MSO_ANCHOR.TOP):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    return tf

def run(par, text, font=FONT, size=11, color=INK, spc=None, align=None):
    r = par.add_run()
    r.text = text
    r.font.name = font
    r.font.size = Pt(size)
    r.font.color.rgb = color
    if spc is not None:
        r.font._rPr.set('spc', str(spc))
    if align is not None:
        par.alignment = align
    return r

# header
tf = textbox(0.70, 0.38, 11.93, 0.28, MSO_ANCHOR.MIDDLE)
run(tf.paragraphs[0], "ВОПРОСЫ И ВОЗРАЖЕНИЯ", FONT_SB, 10, GOLD, spc=300)
tf = textbox(0.70, 0.68, 11.93, 0.66, MSO_ANCHOR.MIDDLE)
run(tf.paragraphs[0], "Шесть вопросов, которые задают первыми", FONT_SB, 28, NAVY)

CARDS = [
    ("Зачем посредник? Пойдём к разработчику напрямую",
     "Напрямую вы получите один знакомый институт и его мнение о собственной технологии. Мы приносим на выбор 5–7 решений со всего поля, проверенных независимыми учёными, собираем господдержку и отвечаем за проект до производства. Знакомство — малая часть работы; денег «за контакт» не берём, обходить нас нет смысла."),
    ("Есть бесплатные госцентры и НАТТ — зачем платить вам?",
     "Госконтур учит, знакомит и ведёт учёт, но за сделку не отвечает никто. Мы — принципал: подписываемся под срок и результат, часть вознаграждения получаем только при успехе внедрения. Роспатент подготовил 200+ брокеров — им негде работать: рынок исполнителя пуст."),
    ("Вас двое. Потянете промышленный проект?",
     "Вдвоём и не надо. Внедрение идёт генподрядом — руками инжиниринговых центров и самих институтов, клиент видит одного подрядчика. yet2, IN-PART и Halo начинали командой из 2–4 человек в той же связке: «переводчик» между наукой и бизнесом плюс научный авторитет."),
    ("Господдержка — долго, а 70% могут и не дать",
     "Сделку считаем без субсидии. Двойной налоговый вычет применяется без всякого конкурса, а 218-е постановление — бонус, который делает проект кратно дешевле. Заявки готовим мы, а не ваши сотрудники. Не дадут субсидию — экономика проекта всё равно сходится."),
    ("70% пилотов не выходят в масштаб. Почему у вас иначе?",
     "Потому что вы не платите за всё сразу. Лестница из четырёх ступеней, у каждой — свой результат и точка выхода. Диагностика за 3–4 недели — дешёвый первый шаг: если честный ответ — «подходящей технологии пока нет», вы узнаете это до больших денег."),
    ("Что с конфиденциальностью наших задач?",
     "NDA до начала диагностики, работа в контуре клиента, задачи предприятия не попадают в открытые витрины. Для международных кейсов — обязательная экспертиза по 183-ФЗ до любой передачи данных за периметр."),
]

COLS_X = [0.70, 6.89]
ROWS_Y = [1.50, 3.28, 5.06]
W, H = 5.74, 1.64

for i, (q, a) in enumerate(CARDS):
    x = COLS_X[i % 2]
    y = ROWS_Y[i // 2]
    outer = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(W), Inches(H))
    outer.fill.solid(); outer.fill.fore_color.rgb = CARD_BG
    outer.line.color.rgb = CARD_LN; outer.line.width = Emu(9525)
    outer.shadow.inherit = False
    set_adj(outer, ADJ_OUTER)
    inner = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x + 0.07), Inches(y + 0.07), Inches(W - 0.14), Inches(H - 0.14))
    inner.fill.solid(); inner.fill.fore_color.rgb = WHITE
    inner.line.color.rgb = INNER_LN; inner.line.width = Emu(6350)
    inner.shadow.inherit = False
    set_adj(inner, ADJ_INNER)
    tf = textbox(x + 0.32, y + 0.15, 5.10, 0.32, MSO_ANCHOR.MIDDLE)
    p0 = tf.paragraphs[0]
    run(p0, f"{i+1:02d}", FONT_SB, 12.5, GOLD)
    run(p0, "  ·  ", FONT_SB, 12.5, GOLD)
    run(p0, q, FONT_SB, 12.5, NAVY)
    tf = textbox(x + 0.32, y + 0.52, 5.10, 1.04)
    run(tf.paragraphs[0], a, FONT, 10.5, INK)

# footer
tf = textbox(0.70, 7.12, 2.50, 0.26, MSO_ANCHOR.MIDDLE)
run(tf.paragraphs[0], "ТехМост", FONT_SB, 8.5, MUTED)
tf = textbox(11.43, 7.12, 1.20, 0.26, MSO_ANCHOR.MIDDLE)
run(tf.paragraphs[0], "13 / 14", FONT, 8.5, MUTED, align=PP_ALIGN.RIGHT)

# move new slide (last) to position 13 (index 12), before the invitation slide
sldIdLst = prs.slides._sldIdLst
ids = list(sldIdLst)
sldIdLst.remove(ids[-1])
sldIdLst.insert(12, ids[-1])

# renumber footers NN / 13 -> NN / 14
changed = 0
for s in prs.slides:
    for sh in s.shapes:
        if sh.has_text_frame:
            for par in sh.text_frame.paragraphs:
                for r in par.runs:
                    if re.fullmatch(r"\d\d / 13", r.text):
                        r.text = r.text.replace("/ 13", "/ 14")
                        changed += 1
print("footers renumbered:", changed)

prs.save(PATH)
print("saved, slides:", len(prs.slides))
