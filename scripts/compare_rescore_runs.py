"""对比两轮重打分的**采样告警量**：换口径之后告警是变多还是变少。

## 为什么需要它

`rescore_mainline.py` 每 20 天打印一行 `[i/N] 日期 板块 X 告警 Y`。这行里的
`Y` 就是**当轮口径下的告警条数**，而且它是在 `mainline_alert` 被污染之前
（第一轮没清表）唯一干净的口径来源 —— 表里的行会跨轮累积，日志不会。

改层权重/阈值之后，"告警量变了多少"是判断**标定有没有做对**的第一手证据：
如果阈值重标定正确，告警量应当与原轮接近；如果翻倍或腰斩，说明标定错了，
而指标对比也就失去意义（比较的其实是两个不同的告警政策）。

## 两个已经踩过的坑（所以这个脚本才写成这样）

1. **日志是 UTF-16LE**：PowerShell 的 `*>` 重定向默认写 UTF-16LE，用
   `encoding="utf-8"` 读会得到乱码、正则一条都匹配不上。这里按 BOM 嗅探编码。
2. **空集合上的 `all()` 恒为 True**：第一版内联脚本解析出 0 条样本，
   却打印了"板块数是否一致：True"—— 看起来像通过，其实是**什么都没比**。
   所以这里在样本为 0 时**直接报错退出**，绝不静默给结论。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: `  [80/696] 20240129 板块 296 告警 8 | 已用 8.2 分，…`
LINE = re.compile(
    r"\[(\d+)/(\d+)\]\s+(\d{8})\s+板块\s+(\d+)\s+告警\s+(\d+)")


def read_text(path: Path) -> str:
    """按 BOM 嗅探编码读取（PowerShell 重定向常见 UTF-16LE）。"""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-16", errors="replace")


def samples(path: Path) -> dict[str, tuple[int, int]]:
    """`{交易日: (板块数, 告警数)}`（只含被打印的采样点）。"""
    out: dict[str, tuple[int, int]] = {}
    for line in read_text(path).splitlines():
        match = LINE.search(line)
        if match:
            out[match.group(3)] = (int(match.group(4)), int(match.group(5)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="对比两轮重打分的采样告警量")
    parser.add_argument("old", help="旧一轮的重打分日志")
    parser.add_argument("new", help="新一轮的重打分日志")
    parser.add_argument("--old-label", default="旧")
    parser.add_argument("--new-label", default="新")
    args = parser.parse_args()

    old_path, new_path = ROOT / args.old, ROOT / args.new
    for path in (old_path, new_path):
        if not path.exists():
            print(f"❌ 找不到日志：{path}")
            return 2
    old, new = samples(old_path), samples(new_path)
    # ⚠️ 解析不到样本时必须报错：空集合上做比较会给出"看起来通过"的假结论。
    if not old or not new:
        print(f"❌ 解析到的采样点太少（{args.old_label} {len(old)} 个、"
              f"{args.new_label} {len(new)} 个）—— 停止，不给结论。"
              "先确认日志编码与行格式。")
        return 2

    common = sorted(set(old) & set(new))
    if not common:
        print(f"❌ 两个日志没有共同采样日（{args.old_label} "
              f"{min(old)}~{max(old)}；{args.new_label} "
              f"{min(new)}~{max(new)}）—— 停止。")
        return 2

    print(f"共同采样日 {len(common)} 个（原始 {args.old_label} {len(old)} / "
          f"{args.new_label} {len(new)}）")
    print(f"  {'交易日':<10}{'板块(旧)':>9}{'板块(新)':>9}"
          f"{'告警(旧)':>9}{'告警(新)':>9}{'差':>6}")
    sum_old = sum_new = 0
    board_mismatch: list[str] = []
    for day in common:
        board_old, alert_old = old[day]
        board_new, alert_new = new[day]
        print(f"  {day:<10}{board_old:>9}{board_new:>9}"
              f"{alert_old:>9}{alert_new:>9}{alert_new - alert_old:>+6}")
        sum_old += alert_old
        sum_new += alert_new
        if board_old != board_new:
            board_mismatch.append(day)

    days = len(common)
    print()
    print(f"告警合计：{args.old_label} {sum_old} / {args.new_label} {sum_new}"
          f"（均值 {sum_old / days:.2f} vs {sum_new / days:.2f} 条/采样日）")
    if sum_old:
        ratio = sum_new / sum_old
        print(f"倍数：{ratio:.2f}x", end="")
        if ratio < 0.7 or ratio > 1.4:
            print("  ⚠️ 偏离较大 —— 阈值重标定可能没做到「保持覆盖率」，"
                  "两轮的告警政策不同，指标不可直接对比")
        else:
            print("  ✅ 与原轮接近，可作为「同一告警政策、不同排序」的对比")
    else:
        print("⚠️ 旧一轮告警为 0，无法算倍数")
    if board_mismatch:
        print(f"⚠️ 有 {len(board_mismatch)} 个采样日板块数不同"
              f"（例如 {board_mismatch[0]}）—— 两轮的口径不止改了权重")
    else:
        print(f"板块数在所有 {days} 个采样日一致 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
