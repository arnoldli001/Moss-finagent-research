"""合规文案整改 · 第一批（**纯文案，不动任何计算逻辑**）。

## 这份映射来自哪

一次全项目合规自查（用户要求"规避合规风险"）里 A 档的清单，
用户批准后执行。判据只有一条：**这句话读起来是不是在给买卖建议**。
所以改的都落在"指令性动作词"上，且**只改用户可见的值，不改字段名/键**
（`low_buy`、`high_sell`、`forced_exit` 这些标识符一律不动 ——
它们不出现在界面上，改了只会把数据契约搞乱）。

## 明确**不在**本批范围

- `低吸` / `高抛` **单独出现**时（`低吸线`/`高抛线`/图例）**不动** ——
  是否替换这两个行业术语属于产品/法务口径，留到第三批由用户拍板。
  本批只处理它们与"做T"组合成的**动作短语**（`低吸做T` → `回踩区间提示`）。
- 任何计算、阈值、信号判定逻辑。

用法：
    .venv\\Scripts\\python.exe scripts\\_compliance_copy_batch1.py [--apply]
不带 `--apply` 只**预览**（dry-run），带才真写。
"""

from __future__ import annotations

import pathlib
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

#: 顺序有意义：**长词在前**，否则短词会先把长词咬掉一半。
MAPPING: list[tuple[str, str]] = [
    # ① 「建议 XXX 价」——最接近"具体投资建议"的表述，去掉"建议"二字
    ("止损 · 强制卖出警告", "止损线触发（风控提示）"),
    ("建议买入价", "触发价"),
    ("建议买点", "触发价"),
    ("建议买价", "触发价"),
    # ③ 信号库 → 条件库（"买入信号库"读起来像选股指令）
    ("买入信号库", "多方条件"),
    ("买入信号", "多方条件"),
    ("卖出信号", "离场条件"),
    ("卖出 / 风控", "风险与离场条件"),
    # ④ 「强制卖出」是**指令**，不是数据
    ("强制卖出警告", "止损提示"),
    ("强制卖出", "止损触发"),
    ("止损硬约束生效", "止损线约束生效"),
    # ⑤ 动作短语 → 区间提示（推送模板也在其中，那是唯一出应用的通道）
    ("低吸做T", "回踩区间提示"),
    ("高抛做T", "冲高区间提示"),
    # ⑥ 「推荐」指的是参数而不是标的，改成「预填」避免被读成荐股
    ("按股性推荐", "按股性预填"),
    ("股性自动推荐", "股性自动预填"),
    ("推荐模板", "预填模板"),
    ("推荐权重", "预填权重"),
    ("推荐档位", "预填档位"),
]

#: ⚠️ 只对**前端**生效的规则："买点/卖点"是图上的标记词，改了对用户有意义；
#: 但后端散文里 `真卖点触发 0 次`、`核心卖点`（**营销词**）这类同一字形
#: 会被咬坏 —— 实测 dry-run 就命中了 `做T是核心卖点`，那跟买卖无关。
#: 所以标记类规则只在前端跑；`买卖点`（复合词）必须排在这两条之前，
#: 否则 `买卖点` 会变成 `买空方触发`。
WEB_ONLY: list[tuple[str, str]] = [
    ("买卖点", "进出场条件"),
    ("买点", "多方触发"),
    ("卖点", "空方触发"),
]

ROOTS = ["web/src", "src/intraday", "src/api", "src/domain"]
SUFFIXES = (".ts", ".tsx", ".py")


def main() -> int:
    apply = "--apply" in sys.argv
    changed: dict[str, int] = {}
    for root in ROOTS:
        base = pathlib.Path(root)
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            original = text
            hits = 0
            rules = (list(MAPPING) + WEB_ONLY if root == "web/src"
                     else list(MAPPING))
            for old, new in rules:
                if old in text:
                    hits += text.count(old)
                    text = text.replace(old, new)
            if text != original:
                changed[str(path)] = hits
                if apply:
                    path.write_text(text, encoding="utf-8")

    total = sum(changed.values())
    mode = "已写入" if apply else "（dry-run，未写入）"
    print(f"命中 {len(changed)} 个文件、共 {total} 处 {mode}")
    for name, n in sorted(changed.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {name}")
    if not apply:
        print("\n要落盘请加 --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
