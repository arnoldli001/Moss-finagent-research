"""ETF ↔ 概念板块映射：导出待填模板 / 应用人工填好的映射。

## 概念板块在哪个库、哪张表

    文件：data/mainline_cache.db          ← 主线专用本地仓（不是结果库）
    表：  ml_board                        ← 全部 2418 个板块
    分析池：WHERE source = 'sector_crowding:list'   ← **324 个概念板块**

    ml_board 的列：code | name | kind | members | source | list_date | updated_at

⚠️ 别查到 `data/moss_finagent.db`（那只存评分/告警结果，没有板块名录）。

## 人工映射的工作流

    # 1) 导出模板（324 行，含当前自动映射结果）
    .venv\\Scripts\\python.exe scripts\\etf_mapping_review.py --export

    # 2) 用 Excel 打开 docs/ETF_MAPPING_TEMPLATE.tsv，填「你的ETF代码」列
    #    多个代码用逗号分隔，例如：562500,159530
    #    留空 = 该板块不配 ETF（那就不会有 ETF 加分）

    # 3) 应用（会校验 ETF 是否存在、是否场内、板块是否在池内）
    .venv\\Scripts\\python.exe scripts\\etf_mapping_review.py --apply docs\\ETF_MAPPING_TEMPLATE.tsv

    # 4) 复核覆盖率与规模
    .venv\\Scripts\\python.exe scripts\\build_etf_mapping.py --report docs\\ETF_BOARD_MAPPING.md

为什么要有这个人工通道：自动映射靠"跟踪指数名包含板块名"，实测只能覆盖
36/324 = 11.1%。而真值事件里最常出现的板块 `886069 人形机器人`（45 个事件里
占 7 个）恰恰**没有**自动映射 —— 它对应的是 `885517 机器人概念`。
这种"意思对但名字不一样"的对应只有人能判断。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402
from src.mainline.etf import load_mapping  # noqa: E402

CACHE_DB = ROOT / "data" / "mainline_cache.db"
TEMPLATE = ROOT / "docs" / "ETF_MAPPING_TEMPLATE.tsv"
HEADER = ("board_code\tboard_name\t成员数\t当前自动映射ETF\t"
          "你的ETF代码(逗号分隔)\t备注")


def export_template() -> int:
    mapping = load_mapping()
    conn = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # 当前映射：板块名 → [ETF 短代码]。`overrides` 是 `代码 → [板块]`（一对多），
    # 所以要**反转**着收集，不能假设值是一个字符串。
    current: dict[str, list[str]] = {}
    for short, boards in mapping.overrides.items():
        for board_name in boards:
            current.setdefault(str(board_name), []).append(str(short))
    # ETF 短代码 → 名称（填模板时看得懂）
    names = {str(r["code"]).split(".")[0]: str(r["name"] or "")
             for r in conn.execute("SELECT code, name FROM ml_etf_meta")}

    rows = conn.execute(
        "SELECT code, name, members FROM ml_board"
        " WHERE source = 'sector_crowding:list' ORDER BY code").fetchall()
    # `ml_board.members` 是**脏字段**（实测大量为 0），别拿它当"这个板块有多少票"。
    # 真正的成分数从提纯股池 `ml_member_pure` 数，没有时回落原始 `ml_member`。
    counts: dict[str, int] = {}
    for table in ("ml_member_pure", "ml_member"):
        try:
            for r in conn.execute(
                    f"SELECT board_code, COUNT(*) n FROM {table}"
                    " GROUP BY board_code"):
                counts.setdefault(str(r["board_code"]), int(r["n"]))
        except sqlite3.Error:
            continue
    lines = [HEADER]
    for r in rows:
        name = str(r["name"])
        codes = sorted(current.get(name, []))
        cell = ",".join(codes)
        note = "；".join(f"{c}={names.get(c, '?')}" for c in codes)
        lines.append(f"{r['code']}\t{name}\t{counts.get(str(r['code']), 0)}"
                     f"\t{cell}\t\t{note}")
    TEMPLATE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn.close()
    mapped = sum(1 for r in rows if current.get(str(r["name"])))
    print(f"模板已导出：{TEMPLATE}")
    print(f"   板块 {len(rows)} 个，其中当前自动映射到 ETF 的 {mapped} 个"
          f"（{mapped / max(1, len(rows)) * 100:.1f}%）")
    print("   请填写第 5 列「你的ETF代码」，多个用英文逗号分隔；留空表示不配。")
    return 0


def apply_template(path: Path) -> int:
    if not path.exists():
        print(f"❌ 找不到 {path}")
        return 2
    text = path.read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        print("❌ 文件为空")
        return 2
    start = 1 if lines[0].startswith("board_code") else 0

    store = MainlineDataStore(config=load_config())
    pool = {str(r["code"]): str(r["name"]) for r in store._read(  # noqa: SLF001
        "SELECT code, name FROM ml_board WHERE source = 'sector_crowding:list'")}
    # 可用的场内 ETF：代码 → 名称
    known = {str(r["code"]).split(".")[0]: str(r["name"] or "")
             for r in store._read(  # noqa: SLF001
                 "SELECT code, name FROM ml_etf_meta")}

    resolved: dict[str, list[str]] = {}
    problems: list[str] = []
    for line in lines[start:]:
        cells = line.split("\t")
        if len(cells) < 5:
            problems.append(f"列数不足（需要 6 列）：{line[:60]}")
            continue
        code, name = cells[0].strip(), cells[1].strip()
        wanted = [item.strip() for item in cells[4].split(",") if item.strip()]
        if not wanted:
            continue
        if code not in pool:
            problems.append(f"{code} {name} 不在 324 池内，已跳过")
            continue
        keep: list[str] = []
        for short in wanted:
            short = short.split(".")[0]
            if short not in known:
                problems.append(f"{code} {name} 的 ETF {short} 不在 ml_etf_meta 里")
                continue
            keep.append(short)
        if keep:
            resolved[name] = keep

    print(f"解析出 {len(resolved)} 个板块的人工映射，"
          f"共 {sum(len(v) for v in resolved.values())} 只 ETF")
    if problems:
        print(f"⚠️ {len(problems)} 个问题：")
        for item in problems[:20]:
            print(f"   · {item}")
    if not resolved:
        print("❌ 没有任何可用的人工映射（第 5 列是否填了？）")
        return 1

    # 写一份**独立**的人工映射文件，不覆盖自动生成的那份 ——
    # 两份合并的优先级与冲突处理放在下一步（`build_etf_mapping`）明确写出来。
    out = ROOT / "configs" / "mainline_etf_mapping_manual.yaml"
    body = ["# 人工提供的 ETF ↔ 板块映射（由 scripts/etf_mapping_review.py --apply 生成）\n",
            "# 优先级高于自动映射：同一个板块两边都有时，以本文件为准。\n",
            "version: 1\n", "overrides:\n"]
    for name in sorted(resolved):
        body.append(f"  # ---- {name} ----\n")
        for short in resolved[name]:
            body.append(f"  - [{short}, {name}]   # {known[short]}\n")
    out.write_text("".join(body), encoding="utf-8")
    print(f"✅ 已写入 {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 映射的人工审核通道")
    parser.add_argument("--export", action="store_true", help="导出待填模板")
    parser.add_argument("--apply", default="", help="应用填好的 TSV")
    args = parser.parse_args()
    if args.apply:
        return apply_template(Path(args.apply))
    return export_template()


if __name__ == "__main__":
    raise SystemExit(main())
