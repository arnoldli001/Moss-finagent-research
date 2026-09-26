"""分析链编排：把「重打分之后的全部派生分析」固化成一条可复现命令。

## 为什么需要它

主线挖掘每次改口径（提纯阈值、权重、反向维度）都要重跑一遍派生分析：
IC 报告 → 告警逐笔统计 → 策略回测 → 参数扫描 → 情节报告 → 召回精度
→ 自动建议 → 迭代快照。八步里任何一步漏跑，结论就会**引用上一版口径的
数字**而不自知 —— 本项目已经踩过：IC 报告是 V2.2 的、交易统计还是旧权重
的，两页文档互相矛盾。

所以这里把顺序、参数、产物路径全部写死在一个地方，并逐步记录：

- **退出码**（非 0 立即停下，不让"半截结论"进入文档）；
- **耗时**（重打分花了 90 分钟，派生分析也很贵，要能看见）；
- **产物是否真的生成了**（脚本返回 0 但没写出 Excel 是可能的）。

不负责等待重打分：那个由外层等待（重打分是 `rescore_mainline.py`，很慢，
用后台任务盯）。本脚本假定 `mainline_score` 已经是目标口径。

⚠️ 重打分必须用 `--force`（它会先清空区间内的 `mainline_score` 与
`mainline_alert`）。否则旧配置的告警等级会和新结果混在同一张表里，
本链算出的"告警胜率"是两套配置的混合值（见 `rescore_mainline.py` 模块文档）。

用法：
    .venv\\Scripts\\python.exe scripts\\run_analysis_chain.py --label pure055
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
LOG = ROOT / "scripts" / "_analysis_chain.log"


@dataclass
class Step:
    """一步分析：名字、参数、以及"跑完必须存在"的产物。"""

    name: str
    argv: list[str]
    outputs: list[str] = field(default_factory=list)
    #: 允许产物缺失（例如某步可能因为样本不足而只打印不落盘）
    optional: bool = False


#: ⚠️ 顺序有依赖：`auto_recommend` 读 IC 结果，`iterate_mainline --snapshot`
#: 又读 `auto_recommend` 与两个 Excel 的结论。不能在中间插入"重算"的步骤，
#: 否则同一份快照里会混进两种口径。
STEPS: tuple[Step, ...] = (
    Step("IC 报告（全区间 + 两窗口）",
         ["scripts/factor_ic_report.py"],
         ["docs/MAINLINE_IC_REPORT.md"]),
    Step("层权重决策实验（候选池内取头部的超额收益）",
         ["scripts/layer_weight_sim.py"],
         [], optional=True),
    Step("覆盖率验收（覆盖率不该有预测能力；防 §16.39 回归）",
         # ⚠️ 必须显式指定 `mainline_score`：脚本默认读的是**基线备份表**
         # （做三窗口分析时用的），放到链里会每轮都报同一份旧数据。
         ["scripts/layer_coverage_report.py", "--table", "mainline_score",
          "--out", "docs/MAINLINE_COVERAGE_REPORT.md"],
         ["docs/MAINLINE_COVERAGE_REPORT.md"]),
    Step("告警逐笔统计（Excel）",
         ["scripts/alert_trade_stats.py"],
         ["docs/MAINLINE_ALERT_TRADES.xlsx"]),
    Step("策略回测（用户口径：3 笔 ×1 成、60 日、止损 7%、每 10% 减半）",
         ["scripts/alert_strategy_backtest.py"],
         ["docs/MAINLINE_STRATEGY_BACKTEST.xlsx"]),
    Step("策略参数扫描（止损 × 止盈台阶）",
         ["scripts/alert_strategy_backtest.py", "--sweep"],
         ["docs/MAINLINE_STRATEGY_SWEEP.xlsx"]),
    Step("情节报告（同一板块的连续提醒合并）",
         ["scripts/alert_episode_report.py"],
         ["docs/MAINLINE_ALERT_EPISODES.md"]),
    Step("召回精度（新窗口）",
         ["scripts/alert_precision_report.py", "--start", "20251001"],
         ["docs/MAINLINE_ALERT_PRECISION.md"]),
    Step("召回精度（旧窗口）",
         ["scripts/alert_precision_report.py", "--start", "20231009",
          "--end", "20240806",
          "--report", "docs/MAINLINE_ALERT_PRECISION_OLD.md"],
         ["docs/MAINLINE_ALERT_PRECISION_OLD.md"]),
    Step("自动改进建议",
         ["scripts/auto_recommend.py"],
         ["docs/MAINLINE_AUTO_ADVICE.md", "docs/MAINLINE_AUTO_ADVICE.json"]),
)


def run_step(step: Step) -> dict:
    """跑一步，返回 `{name, exit, seconds, missing}`。"""
    started = time.time()
    print(f"\n{'=' * 78}\n▶ {step.name}\n  {' '.join(step.argv)}\n{'=' * 78}",
          flush=True)
    argv = [PY, *step.argv]
    try:
        code = subprocess.run(argv, cwd=str(ROOT), check=False).returncode
    except OSError as exc:                      # 解释器/脚本不存在
        print(f"  ✖ 无法启动：{exc}", flush=True)
        code = -1
    seconds = time.time() - started
    # 产物支持通配（快照文件名带序号：`01_pure055.json`）
    missing: list[str] = []
    for pattern in step.outputs:
        hits = list(ROOT.glob(pattern)) if "*" in pattern \
            else ([ROOT / pattern] if (ROOT / pattern).exists() else [])
        if not hits:
            missing.append(pattern)
    if missing and step.optional:
        print(f"  ⚠ 产物缺失（可接受）：{missing}", flush=True)
    print(f"  {'✔' if code == 0 else '✖'} exit={code}，{seconds / 60:.1f} 分",
          flush=True)
    return {"name": step.name, "exit": code, "seconds": round(seconds, 1),
            "missing": missing}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="",
                        help="迭代快照标签（会作为 --label 传给 iterate_mainline）")
    parser.add_argument("--skip", default="",
                        help="逗号分隔的步骤序号（1 起）跳过，用于只补跑某几步")
    parser.add_argument("--snapshot", action="store_true", default=True,
                        help="最后打一个迭代快照（默认开）")
    args = parser.parse_args()

    skip = {int(x) for x in args.skip.split(",") if x.strip().isdigit()}
    label = args.label or time.strftime("chain_%Y%m%d_%H%M")

    steps = [s for i, s in enumerate(STEPS, 1) if i not in skip]
    if args.snapshot:
        steps.append(Step(f"迭代快照（{label}）",
                          ["scripts/iterate_mainline.py", "--snapshot",
                           "--label", label],
                          [f"docs/mainline_iterations/*_{label}.json"],
                          optional=True))

    print(f"分析链开始，共 {len(steps)} 步，标签 {label}", flush=True)
    results: list[dict] = []
    for step in steps:
        item = run_step(step)
        results.append(item)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"label": label, **item},
                                    ensure_ascii=False) + "\n")
        if item["exit"] != 0:
            print(f"\n✖ 在「{step.name}」停下 —— 后续步骤的数字会引用不完整"
                  "口径，宁可不生成。", flush=True)
            break

    failed = [r for r in results if r["exit"] != 0]
    missing = [f"{r['name']} → {m}" for r in results for m in r["missing"]]
    print(f"\n{'=' * 78}")
    print(f"完成 {len(results)}/{len(steps)} 步；失败 {len(failed)} 步"
          f"；缺失产物 {len(missing)} 个")
    for line in missing:
        print(f"  ⚠ {line}")
    print(f"日志 → {LOG}")
    if failed or missing:
        return 1
    print("全部产物已就绪，可以进入下一轮迭代。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
