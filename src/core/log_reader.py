"""读**混合编码**的日志：按行 UTF-8 → GBK 回退，永不抛。

## 为什么需要它（实测现场，2026-10-01）

`data/run/backend.log` 曾经**不是合法 UTF-8**：偏移 1510584 处的字节是
`QMT批量快照失败：…`（`\\xc5\\xfa\\xc1\\xbf` = "批量" 的 **GBK** 编码）。

根因不是"两个进程用了两种编码"，而是**一个进程按 Windows ANSI 码页写**：
`src/core/logging_setup.py` 用 `logging.StreamHandler()` 写 `sys.stderr`，
而守护进程的 `sys.stderr.encoding` 在中文 Windows 上是 **cp936(GBK)**。
ASCII 行在两种编码下逐字节相同，所以这份日志**看着像 UTF-8**
（前 1.5 MB 全是 ASCII），直到有人要读中文才发现读不了 —— 那正是排障时最要紧的时刻。

处置分两半（缺一不可）：

1. **写的那一半**：`configure_src_logging()` 现在把 stdio 显式改成 UTF-8
   （见那个模块的 docstring）⇒ 新写的行一律 UTF-8；
2. **读的那一半（本模块）**：**历史日志已经混了**，不可能回炉重写，
   而运维要能在凌晨三点 grep 它。所以读侧按行回退：
   先试 UTF-8，失败再试 GBK/CP936，再失败用 `errors="replace"` 兜住。

   按**行**回退而不是整文件回退：一份日志里可能两种编码都有
   （同一进程早期/晚期不同启动参数就会这样），整文件判定必然误判一半。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

#: 回退编码顺序（先 UTF-8，再中文 Windows 的 ANSI 码页）。
FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gbk", "cp936")


def decode_line(raw: bytes) -> str:
    """把一行的字节解成文本：UTF-8 优先，逐级回退，最后 replace 兜底。"""
    for enc in FALLBACK_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def read_lines(path: str | Path, *, tail: int | None = None) -> Iterator[str]:
    """逐行读取（不抛）。`tail=N` 只看最后 N 行。"""
    p = Path(path)
    if not p.is_file():
        return
    with p.open("rb") as fh:
        if tail is not None and tail > 0:
            from collections import deque

            buf: deque[bytes] = deque(maxlen=tail)
            for raw in fh:
                buf.append(raw)
            lines = list(buf)
        else:
            lines = list(fh)
    for raw in lines:
        yield decode_line(raw).rstrip("\r\n")


def grep(path: str | Path, needle: str, *, tail: int | None = None,
         limit: int = 200) -> list[str]:
    """按子串过滤（大小写敏感，与 `grep` 一致）。返回命中行（最多 `limit` 条）。"""
    hits: list[str] = []
    for line in read_lines(path, tail=tail):
        if needle in line:
            hits.append(line)
            if len(hits) >= limit:
                break
    return hits


def encoding_report(path: str | Path) -> dict[str, object]:
    """诊断一份日志的编码状况：每行判定它属于哪种编码。

    运维只需要看两个数：`utf8_lines` 与 `gbk_lines` ——
    两者同时非零就是"混编"，而 `bad_lines` 非零说明还有第三种编码。
    """
    from collections import Counter

    kinds: Counter[str] = Counter()
    total = 0
    for line in read_lines(path):
        total += 1
    p = Path(path)
    if not p.is_file():
        return {"exists": False, "lines": 0, "kinds": {}, "utf8_lines": 0,
                "gbk_lines": 0, "bad_lines": 0}
    with p.open("rb") as fh:
        for raw in fh:
            stripped = raw.rstrip(b"\r\n")
            if not stripped:
                kinds["blank"] += 1
                continue
            try:
                stripped.decode("utf-8")
                kinds["ascii" if stripped.isascii() else "utf8"] += 1
                continue
            except UnicodeDecodeError:
                pass
            try:
                stripped.decode("gbk")
                kinds["gbk"] += 1
            except UnicodeDecodeError:
                kinds["bad"] += 1
    return {
        "exists": True,
        "lines": total,
        "kinds": dict(kinds),
        "utf8_lines": kinds["utf8"] + kinds["ascii"],
        "gbk_lines": kinds["gbk"],
        "bad_lines": kinds["bad"],
    }


__all__ = ["FALLBACK_ENCODINGS", "decode_line", "encoding_report", "grep",
           "read_lines"]
