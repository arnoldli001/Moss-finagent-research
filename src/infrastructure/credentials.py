"""数据源凭证 —— **热加载**存储（改完立即生效，不需重启）。

## 为什么不能只写 `.env`

生产的启动方式是：

    pilot_autostart.ps1  →  注入 .env 到进程环境  →  manage.py start --daemon

`.env` 只在**进程启动那一刻**被读进 `os.environ`。之后改 `.env`：
**已运行的进程看不到** —— 表现为"我明明更新了 token，还是 401"，
然后被迫重启（而你明确要求不要重启）。

所以凭证必须存在**每次调用都重新读**的地方。本模块就是那个地方：
落盘 `data/credentials/`（该目录已被 `.gitignore` 的 `/data/` 覆盖），
读取时**不缓存**（或按 mtime 失效），于是写完即生效。

## 与 `.env` 的关系（两者都保留）

    查找顺序： 热加载文件  →  .env / 环境变量

- 热加载文件优先：`zsxq_authorize.py` 刷新后写它 → 立即生效、无需重启；
- `.env` 兜底：保证"还没跑过刷新脚本"时也能用，向后兼容。

## 安全

- 文件权限收到 **仅当前用户可读写**（Windows ACL / POSIX 0600）；
- 目录在 `data/` 下，已进 `.gitignore`（不会误提交）；
- **不打印任何明文**：状态查询只回是否配置、来源与时间戳。
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 热加载凭证目录（在 data/ 下，已被 .gitignore 覆盖）
CRED_DIR = Path("data") / "credentials"

#: 知识星球凭证文件名
ZSXQ_FILE = "zsxq.json"

#: 保守的到期预警窗口（天）。
#:
#: 实测 token 有效期为 7–14 天（opaque token，**无 exp 字段可读**），
#: 所以只能按"最后刷新时间 + 保守阈值"判断，不能精确算。取 7 天为
#: 保守下界：满 7 天即提示"该安排了"，留出缓冲。
STALE_AFTER_DAYS = 7
#: 提前提醒阈值：到这个天数就开始提示，避免"到期当天才发现"。
WARN_AFTER_DAYS = 5


@dataclass(frozen=True)
class Credential:
    """一条热加载凭证。`value` 是明文 —— **绝不可进日志/响应**。"""

    name: str
    value: str
    refreshed_at: str = ""
    note: str = ""

    def age_days(self, *, now: datetime | None = None) -> float | None:
        """距最后刷新多少天。无法解析返回 `None`（不猜）。"""
        if not self.refreshed_at:
            return None
        try:
            ts = datetime.fromisoformat(self.refreshed_at)
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ref = now or datetime.now(timezone.utc)
        return (ref - ts).total_seconds() / 86400.0


def _path(name: str, *, root: Path | None = None) -> Path:
    base = root or Path.cwd()
    return base / CRED_DIR / name


def _restrict_permissions(path: Path) -> None:
    """把凭证文件权限收到"仅当前用户"。失败只告警，不阻断（Windows 上偶有怪异）。"""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return
    except OSError:
        pass
    if os.name == "nt":  # pragma: no cover - 平台相关
        try:
            import subprocess

            user = os.environ.get("USERNAME", "")
            if user:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r",
                     "/grant:r", f"{user}:F"],
                    check=False, capture_output=True, timeout=10)
        except Exception as exc:  # noqa: BLE001 权限收紧失败不该让刷新失败
            logger.warning("收紧凭证文件权限失败：%s", type(exc).__name__)


def save(name: str, value: str, *, note: str = "",
         root: Path | None = None) -> Path:
    """写凭证（**原子替换** + 收紧权限）。写完立即对读取方生效。"""
    p = _path(name, root=root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "value": value,
        # 带微秒：同一秒内连续刷新（脚本 + 测试常见）也要能区分先后。
        # 用秒级精度时，"刷新前后时间戳相同"会让自动化验证无法判断是否真的更新了。
        "refreshed_at": datetime.now(timezone.utc).astimezone()
        .isoformat(timespec="microseconds"),
        "note": note,
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)          # 原子：读取方要么看到旧的、要么看到新的
    _restrict_permissions(p)
    logger.info("凭证已更新：%s（来源=热加载文件）", name)
    return p


def load(name: str, *, root: Path | None = None) -> Credential | None:
    """读凭证。**每次都读盘**（不缓存）—— 这是"改完即生效"的实现方式。

    文件小（~200 字节），一次读盘可忽略；换来的是"刷新后无需重启"。
    """
    p = _path(name, root=root)
    if not p.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("凭证文件无法解析（%s）：%s", name, type(exc).__name__)
        return None
    value = str(data.get("value") or "")
    if not value:
        return None
    return Credential(name=name, value=value,
                      refreshed_at=str(data.get("refreshed_at") or ""),
                      note=str(data.get("note") or ""))


def status(name: str, *, root: Path | None = None) -> dict[str, Any]:
    """凭证状态 —— **不含明文**，可安全进日志 / 管理员界面。

    返回 `state ∈ missing|fresh|expiring|stale`，供管理员界面做分级提示：
      · 普通用户侧**永远看不到这个**（他们只看到"该来源暂无更新"）
      · 管理员侧据此提前安排重新授权
    """
    cred = load(name, root=root)
    if cred is None:
        return {"name": name, "state": "missing", "source": "",
                "refreshed_at": "", "age_days": None}
    age = cred.age_days()
    if age is None:
        state = "unknown"
    elif age >= STALE_AFTER_DAYS:
        state = "stale"
    elif age >= WARN_AFTER_DAYS:
        state = "expiring"
    else:
        state = "fresh"
    return {
        "name": name,
        "state": state,
        "source": "hot-file",
        "refreshed_at": cred.refreshed_at,
        "age_days": None if age is None else round(age, 1),
    }


__all__ = [
    "CRED_DIR",
    "STALE_AFTER_DAYS",
    "WARN_AFTER_DAYS",
    "ZSXQ_FILE",
    "Credential",
    "load",
    "save",
    "status",
]
