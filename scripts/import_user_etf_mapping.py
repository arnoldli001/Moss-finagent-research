"""导入**人工提供**的 ETF ↔ 概念板块映射（用户 CSV），并接线到 ETF 加分项。

## 输入格式

用户给的是 **GBK 编码、逗号分隔** 的 CSV（不是 UTF-8，直接 read_text 会报
`invalid UTF-8 text`），三列：

    code,name,ETF
    886053.TI,BC电池,SH561910、SZ159796、SZ159755、SZ159566
    886112.TI,MLCC概念,159819、SZ159381、159363、159246

`ETF` 列用**顿号**分隔，代码前缀**不统一**：`SH`/`SZ` 可能缺、也可能不缺。
所以不能直接拿去对表，必须先规范化。

## 规范化与校验（四道，任何一道不过都**报告而不是静默丢弃**）

1. **去前缀**：`SH561910` / `SZ159796` / `sh561910` → `561910`
2. **补零**：`159819` 已是 6 位；`1` → `000001`
3. **必须存在于 `ml_etf_meta`**：本地 ETF 名录（2955 只），否则该代码无法取行情
4. **必须是场内 ETF**：用代码前缀白名单（与 `build_etf_mapping` 同一套），
   排除分级基金 / LOF / 联接基金 —— 它们行情普遍止于 2020 年

## 冲突处理（**一对多是允许的，不是冲突**）

- **一个 ETF 被多个板块声明**：`EtfMapping.overrides` 是 `代码 → [板块名]`，
  人工映射表内部**允许一对多** —— 实测用户的文件里 14 个电池子概念共用同一组
  4 只电池 ETF，这是有意的（"这个主题族就用这几只"）。脚本只统计不丢弃。
  代价要清楚：**同一组 ETF 的信号对这 14 个板块完全相同**，加分只能把整个族
  抬起来，分不出族内谁更强。
- **多个板块的 ETF 列表完全相同**：通常是同一主题族（预期），但也会掩盖
  填表错误 —— 实测 `中国AI 50` 被放进了电池组，脚本会把这些组列出来供核对。
- **自动表的同代码归属会被人工表整只覆盖**，避免一只 ETF 同时算到两个板块。

用法：
    .venv\\Scripts\\python.exe scripts\\import_user_etf_mapping.py --check
    .venv\\Scripts\\python.exe scripts\\import_user_etf_mapping.py --import <csv>
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_etf_mapping import ETF_PREFIXES  # noqa: E402
from src.mainline.config import load_config  # noqa: E402
from src.mainline.datastore import MainlineDataStore  # noqa: E402

RAW_COPY = ROOT / "docs" / "etf_mapping_user_source.csv"
MANUAL_YAML = ROOT / "configs" / "mainline_etf_mapping_manual.yaml"


def normalize(code: str) -> str:
    """`SH561910` / `sz159796` / `159819` → 6 位数字代码。"""
    text = str(code or "").strip().upper()
    for prefix in ("SH", "SZ", "SS"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.strip()
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def read_csv(path: Path) -> list[tuple[str, str, list[str]]]:
    """读 GBK/逗号 CSV，返回 `[(board_code, board_name, [原始ETF代码])]`。"""
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "gbk", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        raise ValueError("无法解码（试过 utf-8-sig / gbk / gb18030）")
    rows: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines()[1:]:
        line = line.strip().strip("\r")
        if not line:
            continue
        cells = [item.strip() for item in line.split(",")]
        if len(cells) < 3:
            continue
        codes = [item for item in cells[2].replace("，", "、").split("、")
                 if item.strip()]
        rows.append((cells[0], cells[1], codes))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="导入人工 ETF 映射")
    parser.add_argument("--import", dest="source", default="",
                        help="用户 CSV 路径（同时会存一份到 docs/）")
    parser.add_argument("--check", action="store_true", help="只校验不写文件")
    args = parser.parse_args()

    store = MainlineDataStore(config=load_config())
    pool = {str(r["code"]): str(r["name"]) for r in store._read(  # noqa: SLF001
        "SELECT code, name FROM ml_board WHERE source = 'sector_crowding:list'")}
    etf_name = {str(r["code"]).split(".")[0]: str(r["name"] or "")
                for r in store._read(  # noqa: SLF001
                    "SELECT code, name FROM ml_etf_meta")}
    print(f"本地 ETF 名录 {len(etf_name)} 只；分析池 {len(pool)} 个板块")

    if args.source:
        src = Path(args.source)
        RAW_COPY.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, RAW_COPY)
        print(f"原始文件已存一份 → {RAW_COPY}（溯源用）")
        rows = read_csv(src)
    else:
        if not RAW_COPY.exists():
            print(f"❌ 没有 {RAW_COPY}，请用 --import <csv> 指定用户文件")
            return 2
        rows = read_csv(RAW_COPY)
    print(f"解析出 {len(rows)} 行映射")

    # ⚠️ 一只 ETF 归多个板块是**允许的**（主题族共用一组 ETF，见
    # `EtfMapping` 的说明）。这里只统计不丢弃 —— 早先版本按"代码→板块"
    # 一对一处理，把 71 行砍成 36 个板块，等于静默丢掉用户一半的映射。
    claimed: dict[str, list[str]] = {}    # ETF 短代码 → [板块名, ...]
    unknown: list[str] = []               # 不在 ml_etf_meta
    offsite: list[str] = []               # 非场内（分级/LOF/联接）
    not_in_pool: list[str] = []           # 板块不在 324 池内
    lists: dict[tuple[str, ...], list[str]] = defaultdict(list)

    for board_code, board_name, raw_codes in rows:
        if board_code not in pool:
            not_in_pool.append(f"{board_code} {board_name}")
            continue
        normalized: list[str] = []
        for raw in raw_codes:
            code = normalize(raw)
            if code not in etf_name:
                unknown.append(f"{board_name} → {raw}（规范化为 {code}）")
                continue
            if code[:3] not in ETF_PREFIXES:
                offsite.append(f"{board_name} → {code} {etf_name[code]}")
                continue
            normalized.append(code)
        if normalized:
            lists[tuple(sorted(normalized))].append(board_name)
        for code in normalized:
            slot = claimed.setdefault(code, [])
            if board_name not in slot:
                slot.append(board_name)

    shared = {code: boards for code, boards in claimed.items()
              if len(boards) > 1}
    dup_groups = {key: names for key, names in lists.items() if len(names) > 1}
    boards_covered = {name for boards in claimed.values() for name in boards}

    print()
    print("=" * 78)
    print("校验结果")
    print("-" * 78)
    print(f"  可用的 板块→ETF 映射    {len(boards_covered)} 个板块 / "
          f"{len(claimed)} 只 ETF / "
          f"{sum(len(v) for v in claimed.values())} 条 (板块,ETF) 关系")
    for label, items in (("板块不在 324 池内", not_in_pool),
                         ("代码不在 ml_etf_meta", unknown),
                         ("非场内 ETF（分级/LOF/联接）", offsite)):
        print(f"\n  ⚠️ {label}：{len(items)}")
        for item in items[:15]:
            print(f"     · {item}")
        if len(items) > 15:
            print(f"     …还有 {len(items) - 15} 条")

    if shared:
        print(f"\n  ℹ️ 一只 ETF 被多个板块共用：{len(shared)} 只"
              "（**这是允许的**：主题族共用一组 ETF）")
        for code, boards in sorted(shared.items(),
                                   key=lambda kv: -len(kv[1]))[:6]:
            print(f"     {code} {etf_name[code]}  →  {len(boards)} 个板块："
                  f"{'、'.join(boards[:6])}"
                  f"{'…' if len(boards) > 6 else ''}")
    if dup_groups:
        print(f"\n  ℹ️ ETF 列表完全相同的板块组：{len(dup_groups)} 组"
              "（同一主题族，预期如此；若其中有不该同组的请改源 CSV）")
        for key, names in list(dup_groups.items())[:8]:
            print(f"     {'、'.join(names)}  ←  {'、'.join(key)}")

    print()
    print("=" * 78)
    print("人工映射 vs 自动映射")
    print("-" * 78)
    from src.mainline.etf import load_mapping
    auto = load_mapping()
    auto_codes = set(auto.overrides)
    print(f"  自动映射 {len(auto_codes)} 只 ETF / "
          f"{len({b for bs in auto.overrides.values() for b in bs})} 个板块")
    print(f"  人工映射 {len(claimed)} 只 ETF / {len(boards_covered)} 个板块")
    print(f"  只在人工里（新增覆盖）{len(set(claimed) - auto_codes)} 只")
    print(f"  只在自动里（保留补缺）{len(auto_codes - set(claimed))} 只")
    overlap = set(claimed) & auto_codes
    if overlap:
        diff = {c for c in overlap
                if sorted(claimed[c]) != sorted(auto.overrides[c])}
        print(f"  两边都有：{len(overlap)} 只，其中归属不同 {len(diff)} 只"
              " —— **以人工为准**")
        for code in sorted(diff)[:8]:
            print(f"     {code}: 人工={'、'.join(claimed[code])} / "
                  f"自动={'、'.join(auto.overrides[code])}")

    if args.check:
        print("\n（--check：没有写文件）")
        return 0

    # ---------- 写人工映射文件 ----------
    body = ["# 人工提供的 ETF ↔ 概念板块映射\n",
            "# 来源：用户 CSV（原始文件留档在 docs/etf_mapping_user_source.csv）\n",
            "# 由 scripts/import_user_etf_mapping.py 生成，**不要手改** —— 改源 CSV 后重跑。\n",
            "#\n",
            "# 优先级：本文件**高于**自动生成的主映射表。`load_mapping` 先读主表、\n",
            "# 再用本文件**整只 ETF 覆盖**（不是追加），人工表内部允许一对多\n",
            "# （一个主题族的多个板块共用同一组 ETF，这是有意的）。\n",
            "version: 1\n", "overrides:\n"]
    by_board: dict[str, list[str]] = defaultdict(list)
    for code, boards in claimed.items():
        for board_name in boards:
            by_board[board_name].append(code)
    for board_name in sorted(by_board):
        body.append(f"  # ---- {board_name} ----\n")
        for code in sorted(by_board[board_name]):
            body.append(f"  - [{code}, {board_name}]   # {etf_name[code]}\n")
    MANUAL_YAML.write_text("".join(body), encoding="utf-8")
    print(f"\n✅ 已写入 {MANUAL_YAML}"
          f"（{len(claimed)} 只 ETF / {len(by_board)} 个板块 / "
          f"{sum(len(v) for v in by_board.values())} 条 (板块,ETF) 关系）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
