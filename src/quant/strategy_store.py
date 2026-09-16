"""策略库：把回测出来的策略**一键存成文件**，可列出、读取、删除、重跑。

## 为什么"保存策略"是一件需要认真设计的事

回测里最容易发生的事故不是算错，而是**把一次侥幸的结果当成发现**：
试了 30 组参数，挑出收益最高的那组存下来，然后以为找到了圣杯。
所以这里的保存动作刻意带上三样东西：

1. **完整参数快照**（含费用、撮合口径、训练比例）—— 不存下来就没法复现；
2. **当时的业绩指标 + 数据区间 + 交易笔数** —— 下次重跑结果不同时，
   能立刻判断是"数据补了"还是"策略失效了"；
3. **自检与警告**（样本量是否够、样本外是否明显衰减、自检是否通过）。

判定"值得保存"时**不看全样本收益**，只看样本外收益与交易笔数：
单股票只有几千个交易日，全样本上的高收益几乎必然来自过拟合。

## 去重

内容哈希由「标的 + 入场 + 出场 + 全部可调项」算出。同一套参数重复保存
**不新建文件**，而是更新已有记录的时间戳与指标 —— 否则试参数时会攒出
几十个只有一处不同的文件，策略库很快就没法用了。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path("data/quant/strategies")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{4,64}$")
_HASH_LENGTH = 8

# 自动保存的门槛。刻意要求**样本外为正 + 交易笔数足够**：
# 只看全样本收益会让"试 30 组挑最好"必然通过。
DEFAULT_THRESHOLDS: dict[str, float] = {
    "min_trades": 20.0,          # 全样本交易笔数下限
    "min_oos_trades": 5.0,       # 样本外交易笔数下限
    "min_oos_return_pct": 0.0,   # 样本外收益必须为正
    "min_sharpe": 0.3,           # 全样本夏普下限
}


class StrategyError(RuntimeError):
    """策略库操作失败。"""


@dataclass
class StrategyRecord:
    """一条已保存的策略（全部字段都可 JSON 化）。"""

    id: str
    name: str
    code: str
    entry: str
    exit: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    segments: dict[str, Any] = field(default_factory=dict)
    data_range: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    content_hash: str = ""
    source: str = "api"
    auto_saved: bool = False
    created_at: str = ""
    updated_at: str = ""
    saved_count: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "code": self.code,
            "entry": self.entry, "exit": self.exit, "config": self.config,
            "metrics": self.metrics, "segments": self.segments,
            "data_range": self.data_range, "warnings": self.warnings,
            "content_hash": self.content_hash, "source": self.source,
            "auto_saved": self.auto_saved, "created_at": self.created_at,
            "updated_at": self.updated_at, "saved_count": self.saved_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StrategyRecord:
        known = {item.name for item in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in payload.items()
                      if key in known})


# ==================================================================
# 内容哈希与判定
# ==================================================================


def spec_hash(code: str, entry: str, exit_condition: str,
              config: dict[str, Any] | None = None) -> str:
    """参数指纹：用于判断"这是不是同一个策略"。

    只取**影响回测结果**的字段，刻意排除 `name`（改名不该产生新策略）
    与 `initial_cash`（它只改变资金规模，不改变策略逻辑；但确实会影响
    整手取整的精度，所以仍然保留在 config 里、参与哈希）。
    """
    keys = ("initial_cash", "position_pct", "stop_loss_pct",
            "take_profit_pct", "max_hold_days", "min_hold_days",
            "t_plus_1", "respect_price_limits", "respect_suspension",
            "train_ratio", "costs")
    stable = {key: (config or {}).get(key) for key in keys}
    payload = json.dumps(
        {"code": str(code).zfill(6), "entry": entry.strip(),
         "exit": (exit_condition or "").strip(), "config": stable},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_HASH_LENGTH]


def verdict(result: dict[str, Any],
            thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    """一条回测结果是否"值得保存"（返回判定 + 逐条原因）。

    **为什么以样本外为主**：单股票回测的参数寻优空间很小，几千个交易日里
    挑出全样本收益最高的组合几乎必然成功，而它在样本外通常就失效了。
    所以判定只认样本外收益与交易笔数，全样本指标只作为辅助。
    """
    rules = dict(DEFAULT_THRESHOLDS)
    rules.update(thresholds or {})
    metrics = result.get("metrics", {}) or {}
    segments = result.get("segments", {}) or {}
    oos = segments.get("oos", {}) or {}
    warnings = result.get("warnings", []) or []

    trades = float(metrics.get("trade_count") or 0)
    oos_trades = float(oos.get("trades") or 0)
    oos_return = float(oos.get("return_pct") or 0.0)
    sharpe = metrics.get("sharpe")
    sharpe_value = float(sharpe) if sharpe is not None else 0.0
    self_check_failed = any("自检失败" in item for item in warnings)

    checks = [
        {"name": "交易笔数", "value": trades,
         "threshold": f"≥ {rules['min_trades']:.0f}",
         "passed": trades >= rules["min_trades"]},
        {"name": "样本外交易笔数", "value": oos_trades,
         "threshold": f"≥ {rules['min_oos_trades']:.0f}",
         "passed": oos_trades >= rules["min_oos_trades"]},
        {"name": "样本外收益(%)", "value": round(oos_return, 2),
         "threshold": f"> {rules['min_oos_return_pct']:.2f}",
         "passed": oos_return > rules["min_oos_return_pct"]},
        {"name": "夏普", "value": round(sharpe_value, 2),
         "threshold": f"≥ {rules['min_sharpe']:.2f}",
         "passed": sharpe_value >= rules["min_sharpe"]},
        {"name": "回测自检", "value": "失败" if self_check_failed else "通过",
         "threshold": "通过", "passed": not self_check_failed},
    ]
    failed = [f"{item['name']}（{item['value']} 未达 {item['threshold']}）"
              for item in checks if not item["passed"]]
    return {
        "worthy": not failed,
        "checks": checks,
        "reasons": failed,
        "summary": ("通过全部门槛，值得保存" if not failed
                    else "未达门槛：" + "；".join(failed)),
        "thresholds": rules,
    }


# ==================================================================
# 存储
# ==================================================================


class StrategyStore:
    """策略库（一个策略一个 JSON 文件，内容哈希去重）。"""

    def __init__(self, root: str | Path = DEFAULT_DIR) -> None:
        self.root = Path(root)

    # ---------- 基础 ----------

    def _path(self, strategy_id: str) -> Path:
        # 目录穿越防护：id 只允许字母数字与 -_（文件名由本模块生成，
        # 但 list/load 的入参来自 HTTP，必须当成不可信输入）
        if not _ID_PATTERN.match(strategy_id):
            raise StrategyError(f"非法策略 ID：{strategy_id!r}")
        return self.root / f"{strategy_id}.json"

    def available(self) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            logger.warning("策略目录不可用：%s", str(exc)[:120])
            return False

    # ---------- 保存 ----------

    def save(self, *, code: str, entry: str, exit_condition: str = "",
             name: str = "", config: dict[str, Any] | None = None,
             result: dict[str, Any] | None = None, source: str = "api",
             auto_saved: bool = False,
             thresholds: dict[str, float] | None = None) -> StrategyRecord:
        """保存策略；同一套参数已存在时**更新**而不是新建。"""
        if not self.available():
            raise StrategyError("策略目录不可写，无法保存")
        code = str(code).zfill(6)
        if not entry.strip():
            raise StrategyError("入场条件不能为空 —— 没有条件的策略无法复现")
        if auto_saved and result is not None:
            judgement = verdict(result, thresholds)
            if not judgement["worthy"]:
                raise StrategyError(f"未达自动保存门槛：{judgement['summary']}")

        digest = spec_hash(code, entry, exit_condition, config)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        existing = self._find_by_hash(digest)
        if existing is not None:
            record = existing
            record.name = name or record.name
            record.saved_count += 1
            record.updated_at = now
            record.source = source
            record.auto_saved = record.auto_saved or auto_saved
        else:
            strategy_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{digest}"
            record = StrategyRecord(
                id=strategy_id, name=name or f"{code} 策略", code=code,
                entry=entry.strip(), exit=(exit_condition or "").strip(),
                config=dict(config or {}), content_hash=digest,
                source=source, auto_saved=auto_saved, created_at=now,
                updated_at=now)

        if result is not None:
            record.metrics = dict(result.get("metrics", {}) or {})
            record.segments = dict(result.get("segments", {}) or {})
            record.data_range = {
                "start": (result.get("dates") or [""])[0],
                "end": (result.get("dates") or [""])[-1],
                "trading_days": len(result.get("dates") or []),
            }
            record.warnings = list(result.get("warnings", []) or [])
        self._write(record)
        logger.info("策略已保存：%s（%s %s）", record.id, record.code,
                    record.name)
        return record

    def _find_by_hash(self, digest: str) -> StrategyRecord | None:
        for record in self.list():
            if record.content_hash == digest:
                return record
        return None

    def _write(self, record: StrategyRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{record.id}.json"
        # 原子写：先写临时文件再替换。直接写目标文件时，进程中途退出
        # 会留下半个 JSON，策略库就此损坏且没有任何提示。
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(record.as_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8")
        os.replace(temporary, path)

    # ---------- 读取 ----------

    def list(self) -> list[StrategyRecord]:
        if not self.root.exists():
            return []
        records: list[StrategyRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                records.append(StrategyRecord.from_dict(payload))
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                logger.warning("策略文件损坏，跳过 %s：%s", path.name,
                               str(exc)[:100])
        records.sort(key=lambda item: item.updated_at or item.created_at,
                     reverse=True)
        return records

    def load(self, strategy_id: str) -> StrategyRecord:
        path = self._path(strategy_id)
        if not path.exists():
            raise StrategyError(f"策略不存在：{strategy_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StrategyError(
                f"策略文件损坏：{strategy_id}（{str(exc)[:80]}）") from exc
        return StrategyRecord.from_dict(payload)

    def delete(self, strategy_id: str) -> bool:
        path = self._path(strategy_id)
        if not path.exists():
            return False
        path.unlink()
        logger.info("策略已删除：%s", strategy_id)
        return True


def strategy_store(root: str | Path = DEFAULT_DIR) -> StrategyStore:
    return StrategyStore(root)


def save_from_result(result: dict[str, Any], *, name: str = "",
                     root: str | Path = DEFAULT_DIR,
                     source: str = "api",
                     auto_saved: bool = False,
                     thresholds: dict[str, float] | None = None) -> StrategyRecord:
    """从一次回测结果直接保存（前端「一键保存」/ 达标自动保存的入口）。

    `auto_saved=True` 时会把**门槛检查**一并带上：自动保存必须自己把关，
    否则"试 30 组参数挑最好的一组"会被无条件写进策略库。
    （这里曾经漏传这个参数，等于自动保存形同虚设。）
    """
    config = dict(result.get("config", {}) or {})
    return strategy_store(root).save(
        code=str(result.get("code", "")), entry=str(config.get("entry", "")),
        exit_condition=str(config.get("exit", "")), name=name or
        str(result.get("name", "")), config=config, result=result,
        source=source, auto_saved=auto_saved, thresholds=thresholds)


__all__ = [
    "DEFAULT_DIR",
    "DEFAULT_THRESHOLDS",
    "StrategyError",
    "StrategyRecord",
    "StrategyStore",
    "save_from_result",
    "spec_hash",
    "strategy_store",
    "verdict",
]
