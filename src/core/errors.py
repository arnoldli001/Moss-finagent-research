"""异常转文案的**统一入口**。

## 为什么需要它

代码里此前有 **255 处** `str(exc)[:N]`，`N` 有 **13 种**取值（60/80/100/120/140/
150/160/180/200/220/300/400/500）。问题不在"数字多"，而在**限额是随手写的**：

- 同一个文件里 `[:120]` 与 `[:200]` 混用（`intraday/service.py` 23 处）；
- 没人说得清 120 与 150 的区别；
- 想统一调长（比如排查线上问题时）要改 255 个地方。

正确做法是让限额由**消息去哪里**决定，而不是由调用点决定 —— 三档足够：

| 档位 | 去处 | 长度 | 理由 |
|---|---|---|---|
| `BRIEF_TIGHT` | 紧凑摘要（gap / notes / 前端标签） | 120 | 只用来"认出是什么错" |
| `BRIEF_DEFAULT` | API 响应、异常消息 | 200 | 让调用方有足够上下文自行判断 |
| `BRIEF_LOG` | 日志 | 500 | 排查要看完整堆栈外的现场信息，且日志不外泄 |

## 与脱敏的关系

异常消息可能带 URL（含 token）、文件路径（含用户名）。所以 `brief()`
默认走一遍 `redaction.redact()` —— 异常文案是**最常见的意外泄漏通道**之一
（第三方库的报错里经常带完整请求 URL）。
"""

from __future__ import annotations

from typing import Final

#: 紧凑摘要（gap / notes / 前端标签）
BRIEF_TIGHT: Final = 120
#: 默认（API 响应、异常消息）
BRIEF_DEFAULT: Final = 200
#: 日志
BRIEF_LOG: Final = 500


def brief(exc: object, limit: int = BRIEF_DEFAULT, *, redact: bool = True) -> str:
    """异常 → 截断后的文案。`str(exc)[:limit]` 的带脱敏替代品。

    传入非异常对象时退化为 `str()`，便于在 `except` 分支外复用。
    """
    text = str(exc) if exc is not None else ""
    if len(text) > limit:
        text = text[:limit] + "…"
    if not redact:
        return text
    from src.core.redaction import redact as _redact

    return _redact(text)


def describe(exc: object, limit: int = BRIEF_DEFAULT, *,
             redact: bool = True) -> str:
    """`类型名: 消息` —— 收敛 `f"{type(exc).__name__}: {str(exc)[:N]}"` 这个惯用写法。

    带上类型名是必要的：`DataFetchError: timeout` 与 `ValueError: timeout`
    该走的处置完全不同。
    """
    return f"{type(exc).__name__}: {brief(exc, limit, redact=redact)}"


__all__ = ["BRIEF_DEFAULT", "BRIEF_LOG", "BRIEF_TIGHT", "brief", "describe"]
