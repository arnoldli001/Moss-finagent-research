"""情报源**主备链** —— 多渠道互备，避免单点断供。

## 用户口径（2026-09-25）

> 设计的时候数据源要考虑多渠道 主备互用，避免单一数据源断了，功能不能用了
> 要考虑 token 费用问题，能做成免费获取和分析的就做成免费的，
> 少用云端付费大模型

## 两条设计原则

### ① 主备互用：每个类别 ≥2 个来源

类别与链（全部**免费**接口，无 API Key）：

    newswire       东财 → 同花顺 → 新浪 → 财联社 → 富途
    broker_report  东财 → 财新要闻 → 财联社
    policy         新闻联播 → 上期所新闻 → 财联社
    research_note  知识星球（唯一，token 会过期）→ 财新要闻兜底

链路语义是**降级**不是**合并**：主源成功就用主源，**失败才走备源**。
理由是合并会让同一事件出现多份（去重成本高、还会虚增热度），
而降级能让"谁在供数据"保持确定 —— 出问题时可归因。

⚠️ **`research_note` 是唯一真单点**：知识星球没有等价替代（研报小作文
是它的独有内容）。token 过期时用财新要闻**降级兜底**，并明确标注
"研究笔记来源当前不可用" —— 不假装还有。

### ② 免费优先：能免费获取与分析的就免费

获取侧：本模块所有链路**全部是免费公开接口**，无一处需要付费 Key。

分析侧（见 `llm_policy.py`）：默认**本地 Ollama**，只在需要深度推理时
才允许升级到云端，并且要有明确的成本闸门。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Final

logger = logging.getLogger(__name__)


@dataclass
class ChainAttempt:
    """一次尝试的记录。**不含来源标识**（可进日志/响应）。"""

    source: str          # 内部名（不出接口）
    ok: bool
    ms: float = 0.0
    error: str = ""


@dataclass
class ChainResult:
    """主备链的执行结果。"""

    kind: str
    items: list[Any] = field(default_factory=list)
    used: str = ""                       # 实际生效的内部源名
    attempts: list[ChainAttempt] = field(default_factory=list)
    all_failed: bool = False

    @property
    def used_backup(self) -> bool:
        """是否走了备源（第一个尝试就成功 = 用了主源）。"""
        return bool(self.attempts) and not self.attempts[0].ok


#: 内部源名 → 可读标签。**只用于管理员侧**，不出普通用户界面。
SOURCE_LABELS: Final[dict[str, str]] = {
    "newswire_em": "东财快讯",
    "newswire_ths": "同花顺快讯",
    "newswire_sina": "新浪快讯",
    "newswire_cls": "财联社电报",
    "newswire_futu": "富途快讯",
    "broker_em": "东财研报",
    "broker_cx": "财新要闻",
    "policy_cctv": "新闻联播",
    "policy_shmet": "上期所新闻",
    "research_zsxq": "知识星球",
    "calendar_em": "东财日历",
}


def _fetch_with_chain(
    kind: str,
    chain: list[tuple[str, Callable[[], list[Any]]]],
    *,
    stop_after_first_ok: bool = True,
) -> ChainResult:
    """按顺序尝试，**首个成功即停**（降级语义，不是合并）。"""
    from src.core.redaction import sanitize_error

    res = ChainResult(kind=kind)
    for name, fn in chain:
        t0 = time.perf_counter()
        try:
            items = fn()
            ms = (time.perf_counter() - t0) * 1000
            if not items:
                # 成功但空：**不算成功**。空结果可能意味着上游改版把字段
                # 挪走了，静默当成"今天没内容"会掩盖故障。
                res.attempts.append(ChainAttempt(name, False, ms, "空结果"))
                logger.warning("情报源 %s 返回空结果，尝试备源", name)
                continue
            res.attempts.append(ChainAttempt(name, True, ms))
            res.items = items
            res.used = name
            if stop_after_first_ok:
                break
        except Exception as exc:  # noqa: BLE001 单源失败必须隔离
            ms = (time.perf_counter() - t0) * 1000
            msg = sanitize_error(exc)
            res.attempts.append(ChainAttempt(name, False, ms, msg))
            logger.warning("情报源 %s 失败（%s），尝试备源", name, msg)

    res.all_failed = not res.used
    return res


def chain_summary(res: ChainResult) -> dict[str, Any]:
    """给管理员看的链路摘要。**用户侧不用它。**"""
    return {
        "kind": res.kind,
        "source": SOURCE_LABELS.get(res.used, res.used) if res.used else "",
        "used_backup": res.used_backup,
        "tried": len(res.attempts),
        "all_failed": res.all_failed,
    }


__all__ = [
    "SOURCE_LABELS",
    "ChainAttempt",
    "ChainResult",
    "chain_summary",
]
