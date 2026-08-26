# Конвертер записки посреднику из note-broker-2026-08.md в DOCX (внутренний
# документ, адресатам не передаётся). Правки вносить в Markdown и
# пересобирать: python3 gen_note_docx.py → sitall-note-broker-2026-08.docx.

import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor

HERE = Path(__file__).parent
SRC = HERE / "note-broker-2026-08.md"
OUT = HERE / "sitall-note-broker-2026-08.docx"
FONT = "PT Serif"
NAVY = RGBColor(0x1E, 0x27, 0x61)

MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*")


def add_runs(p, text: str, size: float) -> None:
    """Разложить строку с **жирным** на форматированные куски."""
    text = MD_LINK.sub(r"\1", text)  # ссылки на файлы каталога — просто имена
    pos = 0
    for m in BOLD.finditer(text):
        if m.start() > pos:
            r = p.add_run(text[pos : m.start()])
            r.font.name, r.font.size = FONT, Pt(size)
        r = p.add_run(m.group(1))
        r.font.name, r.font.size, r.bold = FONT, Pt(size), True
        pos = m.end()
    if pos < len(text):
        r = p.add_run(text[pos:])
        r.font.name, r.font.size = FONT, Pt(size)


def build() -> None:
    src = SRC.read_text(encoding="utf-8")
    doc = Document()
    s = doc.sections[0]
    s.page_width, s.page_height = Cm(21.0), Cm(29.7)
    s.left_margin, s.right_margin = Cm(2.0), Cm(1.5)
    s.top_margin, s.bottom_margin = Cm(1.4), Cm(1.2)
    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(10)
    normal.paragraph_format.space_after = Pt(5)
    normal.paragraph_format.line_spacing = 1.0

    def para(align=WD_ALIGN_PARAGRAPH.JUSTIFY, after=5, before=0):
        p = doc.add_paragraph()
        p.alignment = align
        p.paragraph_format.space_after = Pt(after)
        p.paragraph_format.space_before = Pt(before)
        return p

    for raw in src.split("\n\n"):
        block = raw.strip()
        if not block or block == "---":
            continue
        line = " ".join(part.strip() for part in block.splitlines())
        if block.startswith("# "):
            p = para(WD_ALIGN_PARAGRAPH.LEFT, after=2)
            r = p.add_run(line[2:])
            r.font.name, r.font.size, r.bold = FONT, Pt(15), True
            r.font.color.rgb = NAVY
        elif block.startswith("## "):
            p = para(WD_ALIGN_PARAGRAPH.LEFT, after=3, before=8)
            r = p.add_run(line[3:])
            r.font.name, r.font.size, r.bold = FONT, Pt(12), True
            r.font.color.rgb = NAVY
        elif re.match(r"^(- |\d+\. )", block.splitlines()[0]):
            # блок-список: пункты с висячим отступом
            item: list[str] = []
            for ln in block.splitlines() + [""]:
                if re.match(r"^(- |\d+\. )", ln) or ln == "":
                    if item:
                        p = para(after=2)
                        p.paragraph_format.left_indent = Cm(0.6)
                        add_runs(p, " ".join(item), 10)
                    item = [re.sub(r"^- ", "— ", ln.strip())] if ln else []
                else:
                    item.append(ln.strip())
        else:
            add_runs(para(), line, 10)

    doc.save(OUT)
    print(f"Собрано: {OUT.name}")


if __name__ == "__main__":
    build()
