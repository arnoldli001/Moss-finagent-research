"""行情数据「同步缺口」判定：**分区**与**仓库**各在哪一天，差在哪一步。

## 为什么需要它（2026-09-23 真实事故）

量化选股报「数据滞后：本次用的是 20260917 的行情，最近一个已收盘交易日是
20260922」。排查结果不是选股逻辑错，而是：

| 层 | `stk_limit` 的日期 |
|---|---|
| 下载分区（CSV） | **20260922** ← 早就是新的 |
| 本地仓库（SQLite） | **20260917** ← 差五天 |

也就是说数据**早就下载好了，只是没人灌进库**。而选股的涨停/情绪特征要读
`stk_limit`，于是整条链被它一个人卡在 0917。

## 为什么以前看不见

数据健康面板**分别**报了这两层：

- `tushare.datasets[].last` —— 分区覆盖（遍历 3.5 万个分区目录算出来）
- `warehouse.tables[].last` —— 仓库各表最新日

两列都摆在页面上，但**从来没有互相比较过**，也没有和「应该是哪一天」
（`latest_complete_trade_date()`）比较。三个数字各自看都"正常"，
缺口只在**它们之间**，所以没人发现。

本模块把这件事变成一次显式判定：对每个日频数据集给出
`ok / 只需灌库 / 需先下载 / 未知`，并汇总成一句能直接展示的话。

## 设计要点

- **纯函数、零 I/O**：输入就是上面那两份**已经有落盘缓存**的统计，
  输出判定。所以放进健康度请求路径不会新增任何遍历（那两份缓存本来就有）。
- **缺数据不猜**：任一侧读不到就把该数据集标成 `unknown`，而不是当成"已同步"
  （与「诚实数据」一致：判不了就说判不了）。
"""

from __future__ import annotations

from typing import Any

#: 参与「日频新鲜度」判定的数据集。
#:
#: ⚠️ **刻意排除** `stock_basic`（静态名录，分区键是 `static`）与
#: `fina_indicator_vip`（季度财报，最新报告期天然落后一个季度）——
#: 拿「最近交易日」去要求它们必然长期报红，那种告警会被学会忽略。
#:
#: 这张表同时是**同步作业该覆盖哪些数据集**的依据：2026-09-23 事故里
#: 作业只同步 4 个（daily/daily_basic/stk_limit/moneyflow），
#: 而 `panels.py` 实际还要读 `adj_factor`（复权因子）、`index_daily`（相对强度基准）、
#: `suspend_d`（停牌）、`bak_daily`（内盘外盘等特色字段）。
#: 少同步的后果是**静默降级**：`adj_factor` 缺数据时价格未复权，
#: 面板照算不误，只在除权日出现假跳空。
DAILY_DATASETS: tuple[str, ...] = (
    "daily", "daily_basic", "adj_factor", "stk_limit",
    "moneyflow", "index_daily", "suspend_d", "bak_daily",
)

#: 各状态的中文说明（前端直接展示，不在前端拼字符串）。
_STATE_LABEL = {
    "ok": "已同步",
    "need_ingest": "已下载未灌库",
    "need_download": "分区与仓库都落后",
    "unknown": "判定不了（缺统计）",
}


def build_sync_gap(*, expected: str,
                   partition_last: dict[str, str],
                   warehouse_last: dict[str, str],
                   datasets: tuple[str, ...] | list[str] = DAILY_DATASETS
                   ) -> dict[str, Any]:
    """逐数据集比较「分区 / 仓库 / 应该有」三层，汇总缺口。

    - `partition_last`：`{dataset: 分区最新日期}`（来自 Tushare 覆盖统计）
    - `warehouse_last`：`{dataset: 仓库最新日期}`（来自仓库统计）
    - `expected`：`latest_complete_trade_date()`；为空表示日历不可用 → 不做判定
    """
    rows: list[dict[str, Any]] = []
    expect = str(expected or "")
    for name in datasets:
        part = str(partition_last.get(name) or "")
        house = str(warehouse_last.get(name) or "")
        if not expect or not house:
            state = "unknown"
        elif house >= expect:
            state = "ok"
        elif part and part > house:
            # 分区比仓库新 → 数据已经在本地，只差一步 ingest。
            state = "need_ingest"
        else:
            # 分区也不比仓库新 → 得先联网下载。
            state = "need_download"
        rows.append({
            "dataset": name,
            "partition_last": part,
            "warehouse_last": house,
            "state": state,
            "label": _STATE_LABEL[state],
        })

    behind = [row for row in rows if row["state"] in ("need_ingest", "need_download")]
    unknown = [row for row in rows if row["state"] == "unknown"]
    #: ⚠️ `synced` 的语义是「**已确认**全部同步」，不是「没发现落后」。
    #: 两者在"全都判定不了"时会分叉：`behind` 为空不等于同步 —— 那只是没查出来。
    #: （这个 bug 是写完测试才发现的：缺统计时它会报 `synced=True`，
    #: 于是启动自检认为一切正常、连补偿都不做。）
    #: 把 unknown 也算作"未确认"，代价是偶尔多跑一次幂等的同步作业，
    #: 换来的是永远不会在没证据时声称"已同步"。
    confirmed = bool(expect) and not behind and not unknown
    return {
        "expected": expect,
        "checked": bool(expect),
        "synced": confirmed,
        "confirmed": confirmed,
        "behind": [row["dataset"] for row in behind],
        "need_ingest": [row["dataset"] for row in behind
                        if row["state"] == "need_ingest"],
        "need_download": [row["dataset"] for row in behind
                          if row["state"] == "need_download"],
        "unknown": [row["dataset"] for row in unknown],
        "rows": rows,
        "note": _note(expect, behind, unknown),
    }


def _note(expected: str, behind: list[dict[str, Any]],
          unknown: list[dict[str, Any]]) -> str:
    """一句能直接展示的话（空串 = 无需提示）。"""
    if not expected:
        return "交易日历不可用，无法判断行情是否同步（不猜）"
    if not behind:
        if unknown:
            return (f"日频数据集已同步到 {expected}；但 "
                    f"{'、'.join(row['dataset'] for row in unknown)} 缺统计，"
                    "无法确认（不当作已同步）")
        return ""
    ingested = [row["dataset"] for row in behind if row["state"] == "need_ingest"]
    download = [row["dataset"] for row in behind if row["state"] == "need_download"]
    parts: list[str] = []
    if ingested:
        parts.append(f"{'、'.join(ingested)} 已下载但未灌库")
    if download:
        parts.append(f"{'、'.join(download)} 分区与仓库都落后")
    return (f"行情仓库未同步到 {expected}：" + "；".join(parts)
            + "。量化选股/回测会用到旧行情 —— 服务启动时会自动补，"
              "也可手动跑 scripts/quant_sync.py download + "
              "scripts/quant_warehouse.py ingest")


def partition_last_map(tushare_payload: dict[str, Any]) -> dict[str, str]:
    """从健康度里的 `tushare` 段落抽出 `{dataset: 分区最新日期}`。"""
    out: dict[str, str] = {}
    for item in (tushare_payload or {}).get("datasets") or []:
        name = str(item.get("dataset") or "")
        if name:
            out[name] = str(item.get("last") or "")
    return out


def warehouse_last_map(warehouse_payload: dict[str, Any]) -> dict[str, str]:
    """从健康度里的 `warehouse` 段落抽出 `{dataset: 仓库最新日期}`。"""
    out: dict[str, str] = {}
    for item in (warehouse_payload or {}).get("tables") or []:
        name = str(item.get("dataset") or "")
        if name:
            out[name] = str(item.get("last") or "")
    return out


# ======================================================================
# 启动自检的「做过没有」记录（**落盘**）
# ======================================================================
#
# 为什么需要：自检成功时只能打 `logger.info`，而 uvicorn 默认让应用侧 logger
# 停在 WARNING —— 于是"启动时检查过了、结论是已同步"**在日志里根本看不到**，
# 用户没法确认这个功能到底有没有跑（"没消息"与"没运行"分不清）。
#
# 为什么**落盘**而不是只放内存：内存里的记录有两个洞 ——
#   1. 进程外看不到（我第一版就是这样，结果修完无法自证"自检跑过了"）；
#   2. `warm_data_health` 在启动时就把健康度载荷缓存了 5 分钟，
#      而自检要延迟 30 秒才写记录 → 那 5 分钟内的 `/health` 拿到的
#      `startup_check` 永远是空的，看起来像"没跑"。
# 落盘以后：跨进程、跨重启都读得到，页面与本机排查看到的是同一份事实。

_CHECK_FILE_NAME = "sync_check.json"

#: 进程内缓存的那一份（写盘的同一份内容）。有它就不必每个请求都读文件。
_LAST_CHECK: dict[str, Any] = {}


def _check_file() -> Any:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    return root / "data" / "quant" / _CHECK_FILE_NAME


def record_check(outcome: dict[str, Any]) -> None:
    """记录最近一次启动自检的结论（由 `lifespan` 调用；原子写、失败不影响启动）。"""
    import json
    import os
    import tempfile

    _LAST_CHECK.clear()
    _LAST_CHECK.update(outcome)
    target = _check_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".sync_check.", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(outcome, stream, ensure_ascii=False, indent=1)
        os.replace(tmp_name, target)
    except (OSError, ValueError):
        # ⚠️ 必须连 `ValueError` 一起接住：路径里有非法字符（如内嵌 NUL）时
        # `mkdir` 抛的是 ValueError 而不是 OSError —— 只接 OSError 会让
        # "写不进盘"变成"启动崩掉"。这是写完测试才发现的。
        # 写不进盘不影响自检本身：内存里那份仍然有效。
        pass


def last_check() -> dict[str, Any]:
    """最近一次启动自检的结论；没有记录返回空 dict（**不编造**）。

    进程内没有时回退读落盘的那份：这样"刚启动、自检还没跑完"与
    "上一次启动查过什么"都能如实回答。
    """
    if _LAST_CHECK:
        return dict(_LAST_CHECK)
    import json

    try:
        raw = _check_file().read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "DAILY_DATASETS",
    "build_sync_gap",
    "last_check",
    "partition_last_map",
    "record_check",
    "warehouse_last_map",
]
