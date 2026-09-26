"""导出「概念板块 → 提纯后股票池」到 Excel（每个概念一个 sheet）。

## 数据来源与口径

主表 `data/mainline_cache.db` 的 **`ml_member_pure`** —— 概念→成分股提纯的产物
（`scripts/purify_members.py` 写入）。提纯 = 走势相关性（本地算，免费）
+ 主营业务相关度（LLM 判定）+ 市值门槛。

术语必须说准，否则会导出错东西（这是本项目花过代价的一处区分）：

| 情形 | `relevant` | 含义 | 本次导出 |
|---|---|---|---|
| 在提纯表里且 `relevant=1` | 1 | **保留**（判定相关） | ✅ 导出 |
| 在提纯表里且 `relevant=0` | 0 | **显式剔除**（判定不相关） | ❌ 不导出 |
| 不在提纯表里 | 无行 | **未评估**（≠ 不相关） | ❌ 不导出 |

第三条的坑：`apply_pure_pool` 对"未评估"是**保留**的（未评估不等于不相关，
见 `datastore.apply_pure_pool` 的教训记录）。而本导出面向"用户要一份**提纯后**
的股池清单"，所以采用 `relevant=1` —— 得到的是**已被判定相关**的那批，
语义干净。若要看"打分实际用的池子"（含未评估），那是另一份口径。

## 覆盖度（2026-09-22 实测，导出时会打印实时值）

    139 个概念板块全部有提纯结果（无空池）
    提纯表覆盖成分股名单 83.8%
    relevant=1: 9,358 对 / 2,917 只去重股票 / 中位 42 只每板块

## 输出

    data/quant/concept_pools.xlsx

  - 第 1 个 sheet `_总览`：139 个概念的规模、覆盖率、刷新时间（便于导航与验收）
  - 其后每个概念一个 sheet，按 `rank_in_board` 升序（提纯排名，1 = 最相关）
  - 列：股票代码 / 股票名称 / 板块内排名 / 走势相关性 / 主营相关分 / 综合分 /
        入池依据 / 入池来源

## 用法

    .venv\\Scripts\\python.exe scripts/export_concept_pools.py
    .venv\\Scripts\\python.exe scripts/export_concept_pools.py --out docs/concept_pools.xlsx
    .venv\\Scripts\\python.exe scripts/export_concept_pools.py --board 芯片概念 --board 军工
    .venv\\Scripts\\python.exe scripts/export_concept_pools.py --no-overview
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "mainline_cache.db"
DEFAULT_OUT = ROOT / "data" / "quant" / "concept_pools.xlsx"

#: Excel sheet 名禁用字符（`[]:*?/\`）与 31 字符上限
_SHEET_BAD = set(r"[]:*?/\\")
_SHEET_MAX = 31
#: 总览 sheet 名（下划线开头，排在所有概念前面，便于一眼找到）
OVERVIEW_SHEET = "_总览"

#: 导出列（顺序即表头顺序）。`reason` 是入池依据，`source` 是判定通道。
COLUMNS: list[tuple[str, str]] = [
    ("code", "股票代码"),
    ("name", "股票名称"),
    ("rank_in_board", "板块内排名"),
    ("corr", "走势相关性"),
    ("business_score", "主营相关分"),
    ("final_score", "综合分"),
    ("reason", "入池依据"),
    ("source", "入池来源"),
]

#: `ml_member_pure.source` 的中文口径（前端与文档一致）
SOURCE_LABELS = {
    "llm": "LLM主营判定",
    "corr": "走势相关性",
    "new": "新成员(未评估)",
    "llm_theme": "LLM题材判定",
}


def safe_sheet_name(name: str, used: set[str]) -> str:
    """板块名 → 合法且**唯一**的 Excel sheet 名。

    三件事必须做，缺一个 openpyxl 就会抛异常或静默串表：
      1. 去掉 `[]:*?/\\`（Excel 保留字符）；
      2. 截断到 31 字符（Excel 硬上限）—— 实测本库板块名最长 11 个汉字，不会触发，
         但依赖"当前数据刚好合规"是脆的，名单一变就炸；
      3. **去重**：截断后可能撞名，加数字后缀。重名会让后写的 sheet 直接失败。
    """
    cleaned = "".join(ch for ch in str(name) if ch not in _SHEET_BAD).strip()
    cleaned = cleaned or "未命名"
    if len(cleaned) > _SHEET_MAX:
        cleaned = cleaned[:_SHEET_MAX]
    if cleaned not in used:
        used.add(cleaned)
        return cleaned
    for index in range(2, 1000):
        suffix = f"~{index}"
        candidate = cleaned[: _SHEET_MAX - len(suffix)] + suffix
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise RuntimeError(f"无法为板块 {name!r} 生成唯一 sheet 名")


def load_boards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """概念板块 → 提纯后的股票池（只取 relevant=1，按板块内排名升序）。

    名称取自 `ml_member`（`ml_stock_meta.name` 在本库是空的，实测 5,562 行全空；
    `ml_member` 对提纯保留对只有 18 行缺名，缺失时回落到 `ml_company_business`
    的工商全称会让"招商银行"变成"招商银行股份有限公司"，故只用它兜底那 18 行）。
    """
    rows = conn.execute("""
        SELECT p.board_code, b.name AS board_name, p.code, p.corr,
               p.business_score, p.final_score, p.rank_in_board, p.relevant,
               p.source, p.reason, p.refreshed_at,
               COALESCE(NULLIF(m.name, ''), '') AS member_name,
               (SELECT COUNT(*) FROM ml_member mm WHERE mm.board_code = p.board_code)
                   AS member_n,
               (SELECT COUNT(*) FROM ml_member_pure pp WHERE pp.board_code = p.board_code)
                   AS pure_n
        FROM ml_member_pure p
        JOIN ml_board b ON b.code = p.board_code
        LEFT JOIN ml_member m ON m.board_code = p.board_code AND m.code = p.code
        WHERE p.relevant = 1
        ORDER BY b.name, p.rank_in_board, p.code
    """).fetchall()

    # 名称兜底表：仅用于 ml_member 缺名的那几行
    fallback: dict[str, str] = {}
    try:
        for row in conn.execute(
                "SELECT code, name FROM ml_company_business"
                " WHERE name IS NOT NULL AND name <> ''"):
            fallback.setdefault(str(row["code"]), str(row["name"]))
    except sqlite3.Error:
        pass

    boards: dict[str, dict[str, Any]] = {}
    for row in rows:
        board = boards.setdefault(str(row["board_code"]), {
            "board_code": str(row["board_code"]),
            "board_name": str(row["board_name"] or ""),
            "member_n": int(row["member_n"] or 0),
            "pure_n": int(row["pure_n"] or 0),
            "refreshed_at": str(row["refreshed_at"] or ""),
            "stocks": [],
        })
        name = str(row["member_name"] or "") or fallback.get(str(row["code"]), "")
        board["stocks"].append({
            "code": str(row["code"]),
            "name": name,
            "rank_in_board": row["rank_in_board"],
            "corr": row["corr"],
            "business_score": row["business_score"],
            "final_score": row["final_score"],
            "reason": str(row["reason"] or ""),
            "source": SOURCE_LABELS.get(str(row["source"] or ""),
                                        str(row["source"] or "")),
        })
    return sorted(boards.values(), key=lambda b: (-len(b["stocks"]), b["board_name"]))


def build_overview(boards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """总览行：每个概念的池子规模与覆盖率（便于一眼看出哪个板块被剔得最狠）。"""
    out = []
    for board in boards:
        member_n = board["member_n"]
        kept, pure_n = len(board["stocks"]), board["pure_n"]
        out.append({
            "概念板块": board["board_name"],
            "板块代码": board["board_code"],
            "提纯后只数": kept,
            "成分股名单总数": member_n,
            "已评估只数": pure_n,
            "未评估只数": max(0, member_n - pure_n),
            "判定不相关(已剔除)": max(0, pure_n - kept),
            "提纯保留率": (round(kept / member_n, 4) if member_n else None),
            "提纯表刷新时间": board["refreshed_at"],
        })
    return out


def write_workbook(boards: list[dict[str, Any]], out: Path, *,
                   with_overview: bool = True) -> dict[str, Any]:
    """写 Excel；返回统计（供 CLI 打印与验收）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="305496")
    header_align = Alignment(horizontal="center", vertical="center")

    book = Workbook()
    book.remove(book.active)          # 去掉默认空 sheet，避免多出一个 "Sheet"
    used: set[str] = set()
    written: list[tuple[str, int]] = []

    if with_overview:
        used.add(OVERVIEW_SHEET)
        sheet = book.create_sheet(OVERVIEW_SHEET)
        overview = build_overview(boards)
        headers = list(overview[0].keys()) if overview else ["概念板块"]
        sheet.append(headers)
        for item in overview:
            sheet.append([item.get(key) for key in headers])
        _style_header(sheet, headers, header_font, header_fill, header_align)
        _autosize(sheet, headers, get_column_letter)
        sheet.freeze_panes = "A2"
        written.append((OVERVIEW_SHEET, len(overview)))

    headers = [label for _, label in COLUMNS]
    for board in boards:
        sheet = book.create_sheet(safe_sheet_name(board["board_name"], used))
        sheet.append(headers)
        for stock in board["stocks"]:
            sheet.append([
                _cell(stock[key]) if key in ("corr", "business_score", "final_score")
                else stock[key]
                for key, _ in COLUMNS
            ])
        _style_header(sheet, headers, header_font, header_fill, header_align)
        _autosize(sheet, headers, get_column_letter)
        # 数值列统一保留 4 位小数（相关性/综合分都是 0~1 的小数，默认格式会显示成 1）
        for column, (key, _) in enumerate(COLUMNS, start=1):
            if key in ("corr", "business_score", "final_score"):
                for cell in sheet[get_column_letter(column)][1:]:
                    cell.number_format = "0.0000"
        sheet.freeze_panes = "A2"
        written.append((board["board_name"], len(board["stocks"])))

    out.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再**原子替换**目标。两个理由：
    #   1. 目标被 Excel 打开时直接写会 `PermissionError`，而此时 Workbook 已经
    #      建好 —— 中途失败会留下一个**部分写入**的坏 xlsx，比不写更糟；
    #   2. 大工作簿（140 sheet）保存要几秒，期间被中断同样会留坏文件。
    tmp = out.with_name(f".{out.stem}.tmp{out.suffix}")
    try:
        book.save(tmp)
        tmp.replace(out)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise PermissionError(
            f"目标文件被占用，无法写入：{out}\n"
            f"  → 请先关闭正在打开它的 Excel / WPS，然后重跑本脚本。\n"
            f"  → 或改用 --out 指定另一个路径。") from exc
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return {
        "out": out,
        "sheets": len(written),
        "rows": sum(count for name, count in written if name != OVERVIEW_SHEET),
        "details": written,
    }


def _cell(value: Any) -> Any:
    """数值列的空值处理：None → 空单元格（不写 0，`0` 与"没算过"必须能区分）。"""
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return value


def _style_header(sheet: Any, headers: list[str], font: Any, fill: Any,
                  align: Any) -> None:
    if not headers:
        return
    for cell in sheet[1]:
        cell.font = font
        cell.fill = fill
        cell.alignment = align


def _autosize(sheet: Any, headers: list[str], get_column_letter: Any) -> None:
    """按内容估宽（中文按 2 个字符宽算）。

    不调 `sheet.column_dimensions[...].auto_size`：它要求字体度量库，在无 GUI 环境下
    会抛或给出离谱宽度。这里按字符宽度估，够用且确定。
    """
    for index, header in enumerate(headers, start=1):
        width = _display_width(str(header))
        for row in sheet.iter_rows(min_col=index, max_col=index, min_row=2):
            for cell in row:
                width = max(width, _display_width("" if cell.value is None
                                                  else str(cell.value)))
        sheet.column_dimensions[get_column_letter(index)].width = min(
            48, max(9, width + 2))


def _display_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="导出「概念板块 → 提纯后股票池」到 Excel（一概念一 sheet）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"主线库（默认 {DEFAULT_DB}）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"输出 xlsx（默认 {DEFAULT_OUT}）")
    parser.add_argument("--board", action="append", default=[],
                        help="只导出指定概念（可重复；默认全部）")
    parser.add_argument("--no-overview", action="store_true", help="不写 _总览 sheet")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[错误] 主线库不存在：{db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        boards = load_boards(conn)
    finally:
        conn.close()

    if args.board:
        wanted = [str(x).strip() for x in args.board]
        boards = [b for b in boards
                  if b["board_name"] in wanted or b["board_code"] in wanted]
        if not boards:
            print(f"[错误] 没有匹配的概念：{wanted}", file=sys.stderr)
            return 2

    empty = [b["board_name"] for b in boards if not b["stocks"]]
    if empty:
        # 不静默跳过：空池本身是提纯结论的一部分，必须让人看见
        print(f"[提示] {len(empty)} 个概念提纯后为空池，已跳过 sheet：{empty[:10]}")
        boards = [b for b in boards if b["stocks"]]
    if not boards:
        print("[错误] 没有任何概念有提纯保留的股票", file=sys.stderr)
        return 2

    stats = write_workbook(boards, Path(args.out), with_overview=not args.no_overview)
    total_stocks = len({s["code"] for b in boards for s in b["stocks"]})
    print(f"已写出：{stats['out']}"
          f"（{stats['out'].stat().st_size / 1024:.0f} KB）")
    print(f"  sheet 数      : {stats['sheets']}"
          f"{'（含 _总览）' if not args.no_overview else ''}")
    print(f"  概念板块数    : {len(boards)}")
    print(f"  股票池条目    : {stats['rows']:,}")
    print(f"  去重股票数    : {total_stocks:,}")
    sizes = sorted((len(b["stocks"]) for b in boards))
    print(f"  每板块规模    : 最小 {sizes[0]} / 中位 {sizes[len(sizes)//2]} / 最大 {sizes[-1]}")
    print(f"  生成时间      : {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Top 5: " + ", ".join(
        f"{b['board_name']}({len(b['stocks'])})" for b in boards[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
