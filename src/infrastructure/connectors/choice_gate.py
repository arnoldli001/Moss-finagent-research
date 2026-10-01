"""Choice EMQuantAPI 的**能力闸门**：没权限就不许接线（`CHG-0119`）。

## 说清职责边界（免得被当成连接器）

本模块**不取数**、**不定义任何指标口径**、**不注册任何连接器**。
它是**门**，不是**源**。它只回答一个问题并把答案变成**机器可读**的状态：

    「这个账号现在能不能用 Choice？如果不能，是哪一种不能？」

## 为什么必须有这道门（目标里的一句话，此前只是散文）

本轮目标里明确写着：

> **在授权到位前不得把它们声明为可用源。**

在此之前，这条纪律只存在于 `AGENTS.md` 与 `docs/PRD.md` 的**人话段落**里 ——
而本项目反复验证过一件事：**写在文档里的纪律会被忽略，写成判据的不会**
（`AGENTS.md`《护栏保真与效果验证》：判据要认机器可读的标识）。
本模块把它落成 `probe()` 的状态码 + `assert_wireable()` 的拒绝，
并由 `tests/unit/test_choice_entitlement_gate.py` 守住。

## ★ 六种状态必须分开（「没量到」≠「量到 0」）

| 状态 | 含义 | 该去做什么 |
|---|---|---|
| `ok` | 登录成功 | 可以接线 |
| `no_access` | **服务端按账号权限拒绝**（`code:160` / `10001003`） | 找东财客户经理**开通量化接口权限** |
| `config_missing` | 本机没有 `userInfo` 令牌 | 跑 `LoginActivator.exe` 激活 |
| `sdk_missing` | SDK/DLL 加载不了（典型：`.pth` 缺失 ⇒ `WinError 87`） | 跑 SDK 自带的 `installEmQuantAPI.py` |
| `unreachable` | 登录服务器连不上 | 查网络/代理 |
| `probe_error` | **探针自己坏了** | **先修探针 —— 不许据此判定"没权限"** |

最后一行是重点：把"我没量到"说成"账号没权限"，会让排查方向整个跑偏
（本项目为此花过两轮：`api_key()` 的 `ImportError` 被吞成"未配置凭据"）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: SDK 的**规范安装位置**（单一真值源；`check_external_sources.py` 从这里取）。
SDK_HOME = r"C:\EMQuantAPI_Python\python3"
#: 令牌文件名（`LoginActivator.exe` 生成，设备绑定、与路径无关 —— 实测过）。
TOKEN_NAME = "userInfo"

#: 状态码（机器可读；判据只认这些，不认人话）
STATE_OK = "ok"
STATE_NO_ACCESS = "no_access"
STATE_CONFIG_MISSING = "config_missing"
STATE_SDK_MISSING = "sdk_missing"
STATE_UNREACHABLE = "unreachable"
STATE_PROBE_ERROR = "probe_error"

STATES = (STATE_OK, STATE_NO_ACCESS, STATE_CONFIG_MISSING, STATE_SDK_MISSING,
          STATE_UNREACHABLE, STATE_PROBE_ERROR)

#: 服务端"账号无该 API 权限"的可识别标识（实测：`code:160` / `ErrorCode=10001003`）。
NO_ACCESS_CODES = ("10001003",)
NO_ACCESS_HINTS = ("no access", "has no access", "code:160")


@dataclass(frozen=True)
class ChoiceStatus:
    """一次能力探测的结果。`state` 是**唯一**可编程的判据。"""

    state: str
    detail: str = ""
    raw_code: str = ""

    @property
    def wireable(self) -> bool:
        """能不能拿它接线？**只有 `ok` 能。**"""
        return self.state == STATE_OK

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail,
                "raw_code": self.raw_code, "wireable": self.wireable}


def token_path() -> Path:
    return Path(SDK_HOME) / "libs" / "windows" / TOKEN_NAME


def _classify_start_failure(code: str, msg: str) -> ChoiceStatus:
    """把 `c.start()` 的失败码翻译成状态（**顺序重要：先认权限，再认网络**）。"""
    text = f"{code} {msg}".lower()
    if str(code) in NO_ACCESS_CODES or any(h in text for h in NO_ACCESS_HINTS):
        return ChoiceStatus(
            STATE_NO_ACCESS,
            f"服务端按账号权限拒绝：ErrorCode={code} {msg}",
            raw_code=str(code))
    if any(k in text for k in ("connect", "timeout", "network", "refused", "timed out")):
        return ChoiceStatus(STATE_UNREACHABLE,
                            f"登录服务器不可达：{msg or code}", raw_code=str(code))
    return ChoiceStatus(STATE_PROBE_ERROR,
                        f"未识别的失败（**不要**当成没权限）：ErrorCode={code} {msg}",
                        raw_code=str(code))


def probe(*, start: Any = None, importer: Any = None) -> ChoiceStatus:
    """探一次"现在能不能用 Choice"。**两个依赖都可注入**（离线单测不碰 SDK）。

    `start`：`(options: str) -> 带 ErrorCode/ErrorMsg 的对象`（缺省用真 SDK）。
    `importer`：`() -> 模块`（缺省 `import EmQuantAPI`）。
    """
    # ① SDK 能不能进来（`.pth` 缺失就在这里炸，症状是 WinError 87）
    if importer is None:
        def importer():  # type: ignore[no-redef]
            import EmQuantAPI  # noqa: F401

            from EmQuantAPI import c

            return c
    try:
        c = importer()
    except OSError as exc:
        #: WinError 87 = `CDLL('')` ⇒ `EmQuantAPI.pth` 不在 site-packages 里
        return ChoiceStatus(
            STATE_SDK_MISSING,
            f"SDK/DLL 加载失败（多半是 `.pth` 缺失）：{type(exc).__name__}: {exc}",
            raw_code=str(getattr(exc, "winerror", "") or ""))
    except Exception as exc:  # noqa: BLE001
        return ChoiceStatus(STATE_SDK_MISSING,
                            f"导入 SDK 失败：{type(exc).__name__}: {exc}")

    # ② 令牌在不在（它是**设备绑定**的，与路径无关 —— 实测从临时目录复制后仍被接受）
    if not token_path().exists():
        return ChoiceStatus(STATE_CONFIG_MISSING,
                            f"缺令牌文件 {token_path()}（跑 LoginActivator.exe 激活）")

    # ③ 真登录
    fn = start if start is not None else getattr(c, "start", None)
    if fn is None:
        return ChoiceStatus(STATE_PROBE_ERROR, "SDK 里没有 start() —— 结构变了")
    try:
        res = fn("ForceLogin=1")
    except OSError as exc:
        return ChoiceStatus(STATE_SDK_MISSING,
                            f"DLL 调用失败：{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        return ChoiceStatus(STATE_PROBE_ERROR,
                            f"start() 抛异常：{type(exc).__name__}: {exc}")

    code = str(getattr(res, "ErrorCode", "") or "")
    msg = str(getattr(res, "ErrorMsg", "") or "")
    if code == "0":
        try:
            stop = getattr(c, "stop", None)
            if callable(stop):
                stop()
        except Exception as exc:  # noqa: BLE001 登出失败不影响"能登录"这个结论
            logger.warning("Choice 探针 c.stop() 失败（忽略）: %s", exc)
        return ChoiceStatus(STATE_OK, "登录成功", raw_code=code)
    return _classify_start_failure(code, msg)


def assert_wireable(status: ChoiceStatus) -> None:
    """`wireable` 为假时**抛出**，消息里带"为什么"与"谁去做什么"。

    `AGENTS.md`：**拒绝要给出路**，不能只说"你没权限"。
    """
    if status.wireable:
        return
    advice = {
        STATE_NO_ACCESS: "账号未开通「量化接口(EMQuantAPI)」权限 ⇒ 找东财 Choice "
                         "客户经理/服务群开通；设备已激活、令牌有效，开通后无需改代码",
        STATE_CONFIG_MISSING: "跑 `LoginActivator.exe` 完成设备激活（生成 userInfo）",
        STATE_SDK_MISSING: f"跑 `uv run python {SDK_HOME}\\installEmQuantAPI.py` "
                           "（SDK 自带 installer，写 .pth；别自己造路径注入）",
        STATE_UNREACHABLE: "查网络/代理后重试",
        STATE_PROBE_ERROR: "**先修探针**，不要据此判定账号没权限",
    }.get(status.state, "按状态码排查")
    raise RuntimeError(
        f"Choice 当前不可接线（state={status.state}）：{status.detail}；{advice}")


__all__ = [
    "NO_ACCESS_CODES",
    "SDK_HOME",
    "STATES",
    "STATE_CONFIG_MISSING",
    "STATE_NO_ACCESS",
    "STATE_OK",
    "STATE_PROBE_ERROR",
    "STATE_SDK_MISSING",
    "STATE_UNREACHABLE",
    "ChoiceStatus",
    "assert_wireable",
    "probe",
    "token_path",
]
