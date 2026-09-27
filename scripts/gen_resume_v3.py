"""排版引擎 v3 —— 支持二级/三级缩进 + 加粗克制 + 段落分层的简历生成器。

相比 gen_resume.py 的改进：
  1. **二级缩进**：`- ` = 一级（▪ 顶格）；`  - ` = 二级（· 缩进）；`    - ` = 三级（– 更深）
  2. **抬头表格 gridCol 修复**（列宽 14.0/3.6，消除中间白区）
  3. **加粗克制**：渲染时保留 `**` 语法，但内容侧已按"每段 ≤2 处"重写
  4. **段间距可控**：一级 bullet 前留 1.5pt，二级 0.5pt（视觉分组）

用法：
  python gen_resume_v3.py <md> <out.docx> [--compact] [--pdf]
"""
import io
import os
import re
import subprocess
import sys

from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

sys.stdout.reconfigure(encoding="utf-8")

PHOTO_JPG = r"C:\Users\Administrator\Desktop\简历\个人照片.jpg"
FONT_CN = "微软雅黑"
FONT_EN = "Segoe UI"
FONT_BODY_CN = "宋体"
FONT_BODY_EN = "Calibri"
INK = RGBColor(0x1A, 0x1A, 0x1A)
ACCENT = RGBColor(0x0B, 0x4F, 0x8A)
GREY = RGBColor(0x59, 0x59, 0x59)

STYLE = {"body": 10.5, "line": 1.16, "gap": 1.5, "title": 12.0, "name": 22.0, "contact": 9.0}
STYLE_COMPACT = {"body": 10.0, "line": 1.10, "gap": 1.0, "title": 11.5, "name": 21.0, "contact": 8.5}
#: 更紧一档（长简历压 2 页用）
STYLE_TIGHT = {"body": 9.5, "line": 1.04, "gap": 0.7, "title": 11.0, "name": 20.0, "contact": 8.0}
TWIPS_PER_CM = 567

# 缩进层级样式：(bullet_char, left_indent_cm, before_pt, after_pt)
LEVEL_STYLE = [
    ("▸ ", 0.25, None, None),      # L1：模块名加粗 + 主题色
    ("•  ", 0.70, 0.3, 0.3),       # L2：子点（比 · 更醒目）
    ("– ",  1.15, 0.15, 0.15),     # L3：更深一级
]


def use_compact():
    STYLE.update(STYLE_COMPACT)


def use_tight():
    STYLE.update(STYLE_TIGHT)


def set_run(run, *, size=10.5, bold=False, color=INK, cn=FONT_BODY_CN,
            en=FONT_BODY_EN, italic=False):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    run.font.name = en
    rPr = run._element.get_or_add_rPr()
    rf = rPr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts"); rPr.append(rf)
    rf.set(qn("w:ascii"), en)
    rf.set(qn("w:hAnsi"), en)
    rf.set(qn("w:eastAsia"), cn)


def para_fmt(p, *, before=0, after=0, line=None, align=None, left=0.0,
             first=0.0, hanging=0.0, rule=True):
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    if line:
        pf.line_spacing = line
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    if align is not None:
        p.alignment = align
    pf.left_indent = Cm(left)
    if hanging:
        pf.first_line_indent = Cm(-hanging)
    elif first:
        pf.first_line_indent = Cm(first)
    if rule:
        pPr = p._element.get_or_add_pPr()
        sl = OxmlElement("w:snapToGrid"); sl.set(qn("w:val"), "0"); pPr.append(sl)
    return p


def hline(p, *, sz=8, color="0B4F8A", space=1):
    pPr = p._element.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(sz))
    bottom.set(qn("w:space"), str(space))
    bottom.set(qn("w:color"), color)
    pbdr.append(bottom)
    pPr.append(pbdr)


def shade(cell, hexcolor):
    tcPr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear"); sh.set(qn("w:color"), "auto"); sh.set(qn("w:fill"), hexcolor)
    tcPr.append(sh)


def no_borders(table):
    tbl = table._tbl
    tblPr = tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement("w:" + edge)
        el.set(qn("w:val"), "none"); el.set(qn("w:sz"), "0"); el.set(qn("w:space"), "0")
        borders.append(el)
    tblPr.append(borders)


def fix_table_grid(tbl, widths_cm):
    """统一 tblW / tblLayout / gridCol / tcW（Word 用 gridCol 渲染 fixed 布局）。"""
    tbl_el = tbl._tbl
    tblPr = tbl_el.tblPr
    tblW = tblPr.find(qn('w:tblW'))
    if tblW is None:
        tblW = OxmlElement('w:tblW'); tblPr.append(tblW)
    tblW.set(qn('w:w'), str(int(sum(widths_cm) * TWIPS_PER_CM)))
    tblW.set(qn('w:type'), 'dxa')
    layout = tblPr.find(qn('w:tblLayout'))
    if layout is None:
        layout = OxmlElement('w:tblLayout'); tblPr.append(layout)
    layout.set(qn('w:type'), 'fixed')
    grid = tbl_el.find(qn('w:tblGrid'))
    if grid is not None:
        for i, gc in enumerate(grid.findall(qn('w:gridCol'))):
            if i < len(widths_cm):
                gc.set(qn('w:w'), str(int(widths_cm[i] * TWIPS_PER_CM)))
    for row in tbl.rows:
        for i, cell in enumerate(row.cells):
            if i < len(widths_cm):
                cell.width = Cm(widths_cm[i])


BOLD_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def render(p, text, *, size=10.5, color=INK, cn=FONT_BODY_CN, en=FONT_BODY_EN,
           base_bold=False, bold_color=None):
    for seg in BOLD_RE.split(text):
        if not seg:
            continue
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            r = p.add_run(seg[2:-2])
            set_run(r, size=size, bold=True, color=bold_color or color, cn=cn, en=en)
        elif seg.startswith("`") and seg.endswith("`") and len(seg) > 2:
            r = p.add_run(seg[1:-1])
            set_run(r, size=size - 0.5, color=RGBColor(0xA3, 0x1D, 0x1D),
                    cn="Consolas", en="Consolas")
        else:
            r = p.add_run(seg)
            set_run(r, size=size, bold=base_bold, color=color, cn=cn, en=en)


def bullet_level(line: str) -> int:
    """返回 bullet 层级：0=一级 1=二级 2=三级；非 bullet 返回 -1。"""
    stripped = line.lstrip()
    if not (stripped.startswith("- ") or stripped.startswith("* ")):
        return -1
    spaces = len(line) - len(stripped)
    # 每 2 空格 = 一级（兼容 0/2/4；也兼容 1/3 缩进）
    return min(2, spaces // 2)


def build(md_text, out_path):
    body = re.sub(r"^<!--.*?-->\s*", "", md_text, flags=re.S)
    body = body.split("# 第二部分")[0]
    body = body.rstrip().rstrip("-").rstrip()
    lines = body.splitlines()

    doc = Document()
    st = doc.styles["Normal"]
    st.font.size = Pt(10.5); st.font.name = FONT_BODY_EN
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_BODY_CN)

    sec = doc.sections[0]
    sec.page_height = Cm(29.7); sec.page_width = Cm(21.0)
    sec.top_margin = Cm(1.35); sec.bottom_margin = Cm(1.2)
    sec.left_margin = Cm(1.7); sec.right_margin = Cm(1.7)
    sec.header_distance = Cm(0.8); sec.footer_distance = Cm(0.8)

    PHOTO_W = Cm(2.4)
    i, n = 0, len(lines)
    first_block_done = False

    while i < n:
        raw = lines[i].rstrip()
        s = raw.strip()

        if not s:
            i += 1
            continue

        # ---- 抬头 ----
        if s.startswith("# ") and not first_block_done:
            name = s[2:].strip()
            job, contact = "", []
            j = i + 1
            while j < n:
                t = lines[j].strip()
                if t.startswith("#"):
                    break
                if t and not t.startswith(("#", ">", "|", "-")):
                    if not job:
                        job = t
                    else:
                        contact.append(t)
                j += 1

            tbl = doc.add_table(rows=1, cols=2)
            tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
            no_borders(tbl)
            tbl.autofit = False
            cL, cR = tbl.rows[0].cells
            cL.width = Cm(14.0); cR.width = Cm(3.6)
            # ★ 关键：修正 gridCol，否则 Word 按均分渲染（中间白区）
            fix_table_grid(tbl, [14.0, 3.6])

            pL = cL.paragraphs[0]
            para_fmt(pL, before=2, after=0, line=1.0)
            r = pL.add_run(name); set_run(r, size=STYLE["name"], bold=True, color=ACCENT, cn=FONT_CN, en=FONT_EN)
            pJ = cL.add_paragraph(); para_fmt(pJ, before=1, after=STYLE["gap"] + 1.5, line=1.0)
            r = pJ.add_run(job.replace("*", "")); set_run(r, size=STYLE["title"], bold=True, color=INK, cn=FONT_CN, en=FONT_EN)
            for c in contact:
                pc = cL.add_paragraph(); para_fmt(pc, before=0, after=STYLE["gap"] * 0.6, line=1.05)
                render(pc, c, size=STYLE["contact"], color=GREY, cn=FONT_CN, en=FONT_EN)

            pR = cR.paragraphs[0]
            para_fmt(pR, before=0, after=0, line=1.0, align=WD_ALIGN_PARAGRAPH.RIGHT)
            run = pR.add_run()
            if os.path.exists(PHOTO_JPG):
                run.add_picture(PHOTO_JPG, width=PHOTO_W)

            pl = doc.add_paragraph(); para_fmt(pl, before=0, after=4, line=1.0)
            pl.add_run("").font.size = Pt(1)
            hline(pl, sz=12, color="0B4F8A")

            first_block_done = True
            i = j
            continue

        # ---- 章标题 ----
        if s.startswith("## "):
            title = s[3:].strip()
            p = doc.add_paragraph(); para_fmt(p, before=STYLE["gap"] * 3.5, after=STYLE["gap"] * 1.6, line=1.0)
            p.paragraph_format.keep_with_next = True   # ★ 防标题孤立在页尾
            r = p.add_run(title); set_run(r, size=STYLE["title"], bold=True, color=ACCENT, cn=FONT_CN, en=FONT_EN)
            hline(p, sz=8, color="9DC3E6")
            i += 1
            continue

        # ---- 表格 ----
        if s.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                cols = max(len(r) for r in rows)
                t = doc.add_table(rows=0, cols=cols)
                t.style = "Table Grid"
                for ri, rw in enumerate(rows):
                    cells = t.add_row().cells
                    for ci in range(cols):
                        txt = rw[ci] if ci < len(rw) else ""
                        cp = cells[ci].paragraphs[0]
                        para_fmt(cp, before=STYLE["gap"], after=STYLE["gap"], line=1.02)
                        render(cp, txt, size=STYLE["body"] - 1.0,
                               color=ACCENT if ri == 0 else INK,
                               cn=FONT_CN if ri == 0 else FONT_BODY_CN)
                        if ri == 0:
                            shade(cells[ci], "EAF1F9")
            continue

        # ---- 引用块 ----
        if s.startswith(">"):
            q = []
            while i < n and lines[i].strip().startswith(">"):
                q.append(lines[i].strip().lstrip(">").strip())
                i += 1
            tbl = doc.add_table(rows=1, cols=1); no_borders(tbl)
            cell = tbl.rows[0].cells[0]
            shade(cell, "F2F6FB")
            cell.width = Cm(17.6)
            first = True
            for ql in q:
                if not ql:
                    continue
                cp = cell.paragraphs[0] if first else cell.add_paragraph()
                para_fmt(cp, before=STYLE["gap"] if first else STYLE["gap"] * 0.4,
                         after=STYLE["gap"] if first else STYLE["gap"] * 0.4,
                         line=1.06, left=0.15)
                render(cp, ql, size=STYLE["body"] - 0.8, color=RGBColor(0x24, 0x3B, 0x52), cn=FONT_CN, en=FONT_EN)
                first = False
            sp = doc.add_paragraph(); para_fmt(sp, before=0, after=0, line=1.0)
            sp.add_run("").font.size = Pt(2)
            continue

        # ---- ★ 分级 bullet ----
        lvl = bullet_level(raw)
        if lvl >= 0:
            ch, left, b4, af = LEVEL_STYLE[lvl]
            indent = left + lvl * 0.12
            if lvl == 0:
                before = STYLE["gap"] * 1.6
                after = STYLE["gap"] * 0.5
                size = STYLE["body"] + 0.3          # 一级略大
            else:
                before = b4
                after = af
                size = STYLE["body"] - 0.3          # 二级略小但可读
            p = doc.add_paragraph()
            para_fmt(p, before=before, after=after, line=STYLE["line"],
                     left=indent, hanging=indent * 0.5)
            if lvl == 0:
                p.paragraph_format.keep_with_next = True   # ★ 模块名不与首个子点分离
            # bullet 符号
            r = p.add_run(ch)
            set_run(r, size=size,
                     color=ACCENT if lvl == 0 else RGBColor(0x8A, 0x8A, 0x8A),
                     cn=FONT_BODY_CN,
                     bold=(lvl == 0))
            text = raw.lstrip()[2:]
            if lvl == 0:
                # ★ 一级：冒号前的模块名加粗 + 主题色；冒号后正常
                if "：" in text:
                    head, tail = text.split("：", 1)
                    rh = p.add_run(head + "：")
                    set_run(rh, size=size, bold=True, color=ACCENT,
                            cn=FONT_CN, en=FONT_EN)
                    render(p, tail, size=size, color=INK)
                else:
                    rh = p.add_run(text)
                    set_run(rh, size=size, bold=True, color=ACCENT,
                            cn=FONT_CN, en=FONT_EN)
            else:
                render(p, text, size=size, color=RGBColor(0x33, 0x33, 0x33))
            i += 1
            continue

        # ---- 普通段落 ----
        p = doc.add_paragraph(); para_fmt(p, before=STYLE["gap"], after=STYLE["gap"], line=STYLE["line"])
        render(p, s, size=STYLE["body"])
        i += 1

    doc.save(out_path)
    return out_path


def to_pdf(docx_path):
    pdf = os.path.splitext(docx_path)[0] + ".pdf"
    ps = f'''
$ErrorActionPreference="Continue"
try {{
  $w = New-Object -ComObject Word.Application
  $w.Visible=$false; $w.DisplayAlerts=0
  $d = $w.Documents.Open("{docx_path}", $false, $true)
  Start-Sleep -Milliseconds 700
  $p = $d.ComputeStatistics(2)
  $d.SaveAs([ref]"{pdf}", [ref]17)
  $d.Close(0)
  try {{ $w.Quit() }} catch {{}}
  Write-Output "PAGES=$p"
}} catch {{ Write-Output "ERR=$($_.Exception.Message)" }}
'''
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"PAGES=(\d+)", out)
    return pdf, (int(m.group(1)) if m else None), out


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    if "--tight" in sys.argv:
        use_tight()
        print("排版档位: tight")
    elif "--compact" in sys.argv:
        use_compact()
        print("排版档位: compact")
    md = io.open(src, encoding="utf-8").read()
    build(md, dst)
    print("已生成:", dst)
    if "--pdf" in sys.argv:
        pdf, pages, log = to_pdf(os.path.abspath(dst))
        print("已生成:", pdf)
        print("Word 实测页数:", pages)
