"""免费档**限流熔断**护栏 —— 落盘、可观测、自动恢复。

## 为什么必须做（已被登记四轮，是当前最关键的一项）

`light` 层首位已切到 `qwen-siliconflow-7b`（占 **82%** 调用量），而它是
**免费档、有限流机制**（官方 Rate Limits 文档确认有 RPM/TPM 与超限处理）。

**没有这条护栏的后果**：

    siliconflow 静默限流 → 每次调用先白撞一次 429 → 再落到 dashscope

这个过程**没有任何可见面**：延迟上升、dashscope 配额被额外消耗
（它的额度本来就只有 23.9 天），而你只会在事后从账单或体感上发现。

## 判据

| 事件 | 动作 |
|---|---|
| 某模型连续 **3** 次 429 | **锁定该模型 10 分钟**，期间直接从降级链里跳过它 |
| 锁定期内任意一次成功 | —— （锁定期不尝试，所以不会发生） |
| 锁定期满 | 自动解锁并**清零计数**（半开重试，避免永久弃用） |
| 任意一次成功 | 计数清零（"连续"的语义） |

## 两条容易被做错的细节（都有机器判据钉住）

① **判据优先用 HTTP 状态码，不认文案**。异常文本是 `httpx` 的实现细节；
   它一改写法，只做文案匹配的护栏就**静默失效**（`total_429` 永远是 0，
   而没有任何报错）。见 `looks_rate_limited(..., http_status=)`。

② **状态文件必须按实例隔离**（`resolve_state_path()`）。写死
   `data/run/free_tier_429.json` 会让 dev 的调试把 pilot/生产的同一跳一起锁掉
   —— `data/run/` 是三个实例共用的目录。

## 硬约束（AGENTS.md）

- **状态必须落盘**：放进程内存的冷却，重启即失效 —— 本项目实测踩过。
- **"没量到"与"量到 0"必须分开**：`snapshot()` 里 `locked_until` 为空
  表示"未锁定"；若文件读不到则**整体不可用**，不能假装"没限流"。
- `snapshot()` 已接进 `/api/v1/health`（`llm.rate_limit_guard` 段）——
  这是"siliconflow 被静默限流"唯一可见的面。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

from src.infrastructure.catalog.data_stores import store_rel  # noqa: E402

#: 无隔离配置时的兜底路径（与 backend.pid / incidents 同目录，运维一眼能找到）。
DEFAULT_STATE_PATH: str = store_rel("run_dir") + "/free_tier_429.json"

#: 连续多少次 429 就锁定（"连续"= 期间无成功）
LOCK_THRESHOLD: int = 3

#: 锁定分钟数。取 10：短于它的限流窗口通常是瞬时的，
#: 长于它会让"其实已经恢复"的免费档被白白闲置。
LOCK_MINUTES: float = 10.0

#: 429 的机器可读判据。**只认这些标识，不认文案**（AGENTS.md 硬约束：
#: 文案会被本地化，每一次都会让某个隐藏分支静默失效）。
#:
#: ⚠️ 这里是**兜底**判据，主判据是 `http_status`（HTTP 状态码，见
#: `looks_rate_limited(..., http_status=)`）。为什么需要兜底：自定义
#: Provider / 测试替身可能只抛一条带 429 字样的消息，拿不到结构化字段。
RATE_LIMIT_MARKERS: tuple[str, ...] = ("429", "Too Many Requests",
                                       "rate limit", "rate_limit",
                                       "AllocationQuota", "Throttling")

#: 默认识别为"限流"的 HTTP 状态码。**限量类语义只有 429**；
#: 503 属"服务不可用"，由熔断器处理，不要混进来（混进来会把限流熔断
#: 变成"全故障熔断"，锁定期还会跳过本来正常的模型）。
RATE_LIMIT_STATUS: frozenset[int] = frozenset({429})


def resolve_state_path() -> str:
    """状态文件路径 —— **必须按实例隔离**。

    ## 为什么不能写死 `data/run/free_tier_429.json`（实测缺陷）

    `data/run/` 是**三个实例共用**的目录。写死它意味着：

        dev(8100) 里 3 次 429(测试/调试) → 锁 10 分钟
        → **pilot(8110) 和生产的同一条链也一起跳过这一跳**，
          而它们完全无辜（`is_locked` 按模型名判断，跨库跨实例共享一份状态）。

    故障方向是"一个实例的调试把线上的免费档关掉" —— 而且**不报错**，
    只表现为"线上突然不用免费档了、全落到下一跳、延迟和配额悄悄上升"。

    这与 `manage.py` 里 `MOSS_SQLITE_PATH` / `SCHEDULER_DIR` / `LLM_AUDIT_DIR`
    的隔离是同一件事：**凡是写盘的进程级状态，都要跟着实例走**。

    优先级：
      1. `MOSS_RATE_GUARD_PATH`（显式指定，最高优先）
      2. `LLM_AUDIT_DIR` 同级目录（dev/pilot 隔离**已经**在设这个变量，
         所以不需要在 `manage.py` 里再加一处 —— 少一个 key 就少一处漏改）
      3. `data/run/free_tier_429.json`（无隔离配置时的兜底，即现状）
    """
    explicit = (os.environ.get("MOSS_RATE_GUARD_PATH") or "").strip()
    if explicit:
        return explicit
    audit_dir = (os.environ.get("LLM_AUDIT_DIR") or "").strip()
    if audit_dir:
        return str(Path(audit_dir) / "free_tier_429.json").replace("\\", "/")
    return DEFAULT_STATE_PATH


def looks_rate_limited(err: object, *,
                       http_status: int | None = None) -> bool:
    """该异常是否是"限流"（而不是超时/网络/鉴权）。

    ★ 判据优先级：**结构化 HTTP 状态码 > 文案**。

    为什么必须让状态码排第一（2026-09-28 实测）：异常文本来自
    `httpx.HTTPStatusError` 的默认文案（`Client error '429 Too Many
    Requests' for url ...`）。**这句话是 httpx 的实现细节** ——
    它换一种写法、或中间加一层包装，判据就静默失效，
    表现是"护栏装好了但从来不锁"（`total_429` 永远是 0），
    而**没有任何报错**。状态码是协议层的，不会因为文案改版而变。

    ⚠️ 只在拿不到状态码时才回退到文案匹配。
    """
    if http_status is not None and int(http_status) in RATE_LIMIT_STATUS:
        return True
    text = f"{type(err).__name__}: {err}"
    return any(m.lower() in text.lower() for m in RATE_LIMIT_MARKERS)



@dataclass
class _Entry:
    consecutive_429: int = 0
    locked_until: float = 0.0      # epoch 秒；0 = 未锁定
    total_429: int = 0
    total_success: int = 0
    last_reason: str = ""


@dataclass
class RateLimitGuard:
    """落盘的限流熔断。线程/进程内共享同一份文件。

    ⚠️ 多进程（uvicorn worker > 1）下会有写竞争。本项目是 SQLite 单写者
    架构、单实例部署，所以用"读-改-写 + 原子替换"够用；若将来多副本，
    需要换成带锁的实现（这里显式登记该边界，不假装它是通用的）。
    """

    path: str = field(default_factory=resolve_state_path)
    threshold: int = LOCK_THRESHOLD
    lock_seconds: float = LOCK_MINUTES * 60.0
    _cache: dict[str, _Entry] | None = field(default=None, repr=False)

    # ---------- 落盘 ----------

    def _load(self) -> dict[str, _Entry]:
        if self._cache is not None:
            return self._cache
        out: dict[str, _Entry] = {}
        p = Path(self.path)
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                for k, v in (raw.get("models") or {}).items():
                    out[k] = _Entry(**v)
            except Exception:  # noqa: BLE001 坏文件不该让链路崩
                logger.warning("限流熔断状态文件损坏，按空处理：%s", self.path,
                               exc_info=True)
        self._cache = out
        return out

    def _save(self, data: dict[str, _Entry]) -> None:
        p = Path(self.path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(
                {"models": {k: vars(v) for k, v in data.items()}},
                ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)          # 原子替换，避免读到半截文件
        except OSError:
            logger.warning("限流熔断状态落盘失败：%s", self.path, exc_info=True)
        # 缓存与磁盘同时更新，避免"本进程看得见、别的进程看不见"的错觉
        self._cache = data

    # ---------- 查询 / 记录 ----------

    def is_locked(self, model: str, *, now: float | None = None) -> bool:
        """该模型是否处于锁定期（锁定期内应**直接从降级链里跳过**）。"""
        e = self._load().get(model)
        if e is None:
            return False
        return (now if now is not None else time.time()) < e.locked_until

    def record_success(self, model: str) -> None:
        """成功 → 连续计数清零（这就是"连续"的语义）。"""
        data = self._load()
        e = data.setdefault(model, _Entry())
        e.consecutive_429 = 0
        e.total_success += 1
        e.last_reason = ""
        self._save(data)

    def record_rate_limited(self, model: str, reason: str = "",
                            *, now: float | None = None) -> bool:
        """记录一次 429。达到阈值则锁定并返回 True。"""
        now = now if now is not None else time.time()
        data = self._load()
        e = data.setdefault(model, _Entry())
        e.consecutive_429 += 1
        e.total_429 += 1
        e.last_reason = reason[:200]
        locked = False
        if e.consecutive_429 >= self.threshold:
            e.locked_until = now + self.lock_seconds
            e.consecutive_429 = 0     # 锁定后清零，解锁即半开重试
            locked = True
            logger.warning(
                "免费档 %s 连续 %d 次限流 → 锁定 %.0f 分钟（后续直接从链上跳过）",
                model, self.threshold, self.lock_seconds / 60.0)
        self._save(data)
        return locked

    # ---------- 可观测 ----------

    def snapshot(self, *, now: float | None = None) -> dict:
        """给 /health 用。

        ⚠️ 「未量到」与「量到 0」分开：`remaining_s` 为 None = 未锁定，
        而不是 0；`locked` 是布尔判据，不做"看起来像数字"的糊弄。
        """
        now = now if now is not None else time.time()
        data = self._load()
        return {
            "path": self.path,
            "threshold": self.threshold,
            "lock_minutes": self.lock_seconds / 60.0,
            "models": {
                k: {
                    "locked": now < v.locked_until,
                    "remaining_s": (round(v.locked_until - now, 1)
                                    if now < v.locked_until else None),
                    "consecutive_429": v.consecutive_429,
                    "total_429": v.total_429,
                    "total_success": v.total_success,
                    "last_reason": v.last_reason,
                } for k, v in data.items()
            },
        }


_GUARD: RateLimitGuard | None = None


def get_guard() -> RateLimitGuard:
    """进程级单例（这样"连续"计数与缓存不会因为每次新建对象而丢失）。

    ⚠️ 路径在**首次调用时**由 `resolve_state_path()` 解析并固定。
    生产/ dev / pilot 的环境变量都是在进程启动前注入的，所以这没有副作用；
    测试若要换路径，请**显式构造** `RateLimitGuard(path=...)`
    （`tests/unit/test_rate_limit_guard.py` 就是这么做的），
    或先 `reset_guard()`。

    为什么要写这一行说明：原实现是 `if _GUARD is None or path is not None`，
    即"传了 path 就换实例" —— 那等于把单例语义悄悄取消掉，
    而且换个实例会把内存里已累计的"连续 429"计数丢掉。
    """
    global _GUARD
    if _GUARD is None:
        _GUARD = RateLimitGuard()
    return _GUARD


def reset_guard() -> None:
    """丢弃单例（**仅测试用**）。"""
    global _GUARD
    _GUARD = None
