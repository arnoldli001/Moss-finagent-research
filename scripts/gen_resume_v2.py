"""改进版简历生成器：复用用户的 gen_resume.py 逻辑 + 修复抬头表格 gridCol。

根因：gen_resume.py 只设了 cell.width（tcW=14/3.6），但没设 gridCol
（Word 渲染 fixed 布局时用 gridCol）——gridCol 仍是均分 8.8/8.8，
导致左列被压到 8.8cm、右列 8.8cm 但照片只占 2.4cm → 中间 6.4cm 白区。

本脚本：调 gen_resume.build() 生成 docx → 立即修正 gridCol → 导出 PDF → 渲染首页 PNG
输出到临时目录 D:\code\_resume_out\（避免与 WPS 占用的文件冲突）
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

from docx import Document
from docx.shared import Cm
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

TWIPS_PER_CM = 567
GEN_SCRIPT = Path(r"C:\Users\Administrator\Desktop\简历\正式简历\gen_resume.py")
MATERIAL = Path(r"C:\Users\Administrator\Desktop\简历\求职材料_2026")
OUT_DIR = Path(r"D:\code\_resume_out")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- 复用用户脚本 ----
spec = importlib.util.spec_from_file_location("gen_resume", str(GEN_SCRIPT))
gr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gr)


def fix_header_table_grid(docx_path: Path, widths=(14.0, 3.6)) -> int:
    """修正 2 列抬头表格的 tblW / tblLayout / gridCol / tcW（四者统一）。"""
    doc = Document(str(docx_path))
    fixed = 0
    for tbl in doc.tables:
        if len(tbl.columns) != 2:
            continue
        tbl_el = tbl._tbl
        tblPr = tbl_el.tblPr
        # tblW
        tblW = tblPr.find(qn('w:tblW'))
        if tblW is None:
            tblW = OxmlElement('w:tblW'); tblPr.append(tblW)
        tblW.set(qn('w:w'), str(int(sum(widths) * TWIPS_PER_CM)))
        tblW.set(qn('w:type'), 'dxa')
        # tblLayout
        layout = tblPr.find(qn('w:tblLayout'))
        if layout is None:
            layout = OxmlElement('w:tblLayout'); tblPr.append(layout)
        layout.set(qn('w:type'), 'fixed')
        # gridCol（★ Word 实际渲染依据）
        grid = tbl_el.find(qn('w:tblGrid'))
        if grid is not None:
            for i, gc in enumerate(grid.findall(qn('w:gridCol'))):
                if i < len(widths):
                    gc.set(qn('w:w'), str(int(widths[i] * TWIPS_PER_CM)))
        # tcW
        for row in tbl.rows:
            for i, cell in enumerate(row.cells):
                if i < len(widths):
                    cell.width = Cm(widths[i])
        fixed += 1
    if fixed:
        doc.save(str(docx_path))
    return fixed


def export_pdf(docx_path: Path) -> tuple[Path | None, str]:
    """Word COM 导出 PDF + 页数。"""
    pdf = docx_path.with_suffix(".pdf")
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
    out = ((r.stdout or "") + (r.stderr or ""))
    pages = ""
    for line in out.splitlines():
        if line.startswith("PAGES="):
            pages = line.split("=", 1)[1].strip()
    return (pdf if pdf.exists() else None), pages


def render_first_page(pdf_path: Path, png_path: Path, dpi_zoom: float = 2.0) -> bool:
    """PDF 首页 → PNG（供人工/模型视觉验证）。"""
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        page = doc[0]
        mat = fitz.Matrix(dpi_zoom, dpi_zoom)
        pix = page.get_pixmap(matrix=mat)
        pix.save(str(png_path))
        doc.close()
        return True
    except Exception as e:
        print(f"render failed: {e}")
        return False


JOBS = [
    ("13b_附件简历_排版稿_AI应用架构师_修订版.md",     "李浩_AI应用架构师_修订版v2"),
    ("14b_附件简历_排版稿_AI应用开发工程师_修订版.md", "李浩_AI应用开发工程师_修订版v2"),
    ("15b_附件简历_排版稿_AI量化工程师_修订版.md",     "李浩_AI量化工程师_修订版v2"),
    ("16b_附件简历_排版稿_AI产品经理_修订版.md",       "李浩_AI产品经理_修订版v2"),
]

log = []
def w(s=""): log.append(str(s))

w("=" * 72)
w("改进版生成（含 gridCol 修复）")
w("=" * 72)

for md_name, out_name in JOBS:
    md = MATERIAL / md_name
    docx = OUT_DIR / f"{out_name}.docx"
    if not md.exists():
        w(f"❌ 源 md 不存在: {md_name}")
        continue
    # 1) 用用户脚本生成（compact 档）
    gr.use_compact()
    gr.build(md.read_text(encoding="utf-8"), str(docx))
    # 2) 修 gridCol
    n = fix_header_table_grid(docx)
    # 3) 导 PDF
    pdf, pages = export_pdf(docx)
    # 4) 渲染首页
    png = OUT_DIR / f"{out_name}_p1.png"
    rendered = render_first_page(pdf, png) if pdf else False
    w(f"\n  {'✅' if pdf else '❌'} {out_name}")
    w(f"     修复表格 {n} 个 | 页数 {pages or '?'} | PDF {'OK' if pdf else 'FAIL'} | PNG {'OK' if rendered else 'FAIL'}")

out = "\n".join(log)
Path(r"D:\code\Moss-finagent-research\docs\_gen_v2_log.txt").write_text(out, encoding="utf-8")
print(out)
