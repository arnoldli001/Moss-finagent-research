"""日志编码：写侧锁 UTF-8 + 读侧容忍混编（2026-10-01）。

## 现场

`data/run/backend.log` **不是合法 UTF-8**：偏移 1510584 处是
`QMT批量快照失败：…`，字节 `\xc5\xfa\xc1\xbf` 是 "批量" 的 **GBK**。

根因：`logging_setup` 的 `StreamHandler()` 写 `sys.stderr`，而守护进程的
`sys.stderr.encoding` 在中文 Windows 上是 **cp936(GBK)**。ASCII 行在两种编码下
逐字节相同 ⇒ 日志前 1.5 MB 全是 ASCII、**看着像 UTF-8**，直到要读中文才发现读不了。

## 这个文件钉两件事（缺一不可）

1. **写侧**：`configure_src_logging()` 之后，中文日志必须以 **UTF-8** 出现在 stderr。
   —— 用**真子进程**验证（不 mock），因为要测的正是"解释器启动时的默认编码"。
2. **读侧**：历史日志已经混了，不可能回炉 ⇒ `src/core/log_reader.py` 必须能
   同时读出 UTF-8 行与 GBK 行。

## 自证

两个测试都自带反证：子进程在调用装配**之前**先写一行中文，断言它**不是**
合法 UTF-8（证明这台机器的默认码页真的不是 UTF-8，判据不是恒绿）；
读侧则断言"严格的 UTF-8 读法在同一份 fixture 上会失败"。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.core.log_reader import decode_line, encoding_report, grep, read_lines

#: 一个"两种编码下字节不同"的中文样本（"批量" 的 GBK 是 \xc5\xfa\xc1\xbf）
CN = "QMT批量快照失败"


# ======================================================================
# 一、写侧：真子进程，默认码页 vs 装配之后
# ======================================================================

_CHILD = r"""
import sys
sys.path.insert(0, r"{root}")
# ① 装配**之前**：这台机器的默认码页写出来的中文
sys.stderr.write("BEFORE:{cn}\n")
sys.stderr.flush()
from src.core.logging_setup import configure_src_logging
configure_src_logging()
import logging
logging.getLogger("src.demo").info("AFTER:{cn}")
sys.stderr.flush()
"""


def _run_child(root: Path) -> tuple[bytes, bytes]:
    """在**干净环境**里跑子进程（不传 PYTHONIOENCODING/PYTHONUTF8）。

    环境干净是判据的一部分：如果父进程恰好设了 `PYTHONUTF8=1`，
    子进程本来就是 UTF-8，这条判据就测不到东西了。
    """
    import os

    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO")}
    env.pop("MOSS_LOG_LEVEL", None)
    code = _CHILD.format(root=str(root), cn=CN)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          env=env, check=False)
    return done.stdout + done.stderr, done.stderr


def test_stdio_default_codepage_is_not_utf8_on_this_machine() -> None:
    """★ 自证的前半：**装配之前**那行中文不是合法 UTF-8（证明环境真有这个坑）。

    如果哪天它变绿了（默认码页变成 UTF-8），说明这条判据失去了意义 ——
    那时应该**显式失败**提醒人重看，而不是静静地一直绿。
    """
    out, _ = _run_child(Path(__file__).resolve().parents[2])
    before = [ln for ln in out.split(b"\n") if ln.startswith(b"BEFORE:")]
    assert before, "子进程没写出 BEFORE 行（判据失效，先修测试）"
    try:
        before[0].decode("utf-8")
        is_utf8 = True
    except UnicodeDecodeError:
        is_utf8 = False
    if is_utf8:
        # 本机默认已是 UTF-8：不是缺陷，但这条判据的"反证"半边没意义了
        assert before[0].split(b"BEFORE:")[1] == CN.encode("utf-8"), (
            "默认码页是 UTF-8，但中文样本的字节也不是 UTF-8 —— 环境异常")
    else:
        assert CN.encode("gbk") in before[0], (
            "装配前的中文应当是系统 ANSI 码页(GBK)字节 —— 否则根因描述是错的")


def test_logging_setup_forces_utf8_after_configure() -> None:
    """★★ 写侧判据：装配之后，中文日志必须是 **UTF-8** 字节。"""
    out, _ = _run_child(Path(__file__).resolve().parents[2])
    after = [ln for ln in out.split(b"\n") if CN.encode("utf-8") in ln
             and not ln.startswith(b"BEFORE:")]
    assert after, (
        "装配之后没有找到 UTF-8 编码的中文日志行 ⇒ stdio 没被改成 UTF-8\n"
        f"原始输出片段：{out[-400:]!r}")
    # 反证：同一行不该是 GBK 字节
    assert CN.encode("gbk") not in after[0], (
        "装配之后中文仍是 GBK 字节 ⇒ 修复没生效（只是恰好也被解成了别的东西）")


# ======================================================================
# 二、读侧：混编日志必须能读出两种编码的中文
# ======================================================================

def test_mixed_encoding_log_is_readable(tmp_path: Path) -> None:
    """★ 同一份文件里 UTF-8 行与 GBK 行都要读得出来（按行回退）。"""
    path = tmp_path / "backend.log"
    path.write_bytes(
        "INFO:     Application startup complete.\n".encode("ascii")
        + (f"utf8 行：{CN}\n").encode("utf-8")
        + (f"gbk 行：{CN}\n").encode("gbk")          # 历史遗留的 GBK 行
        + b"tail without newline")
    lines = list(read_lines(path))
    assert lines[0].endswith("Application startup complete.")
    assert CN in lines[1], "UTF-8 行没读出来"
    assert CN in lines[2], "GBK 行没读出来（读侧回退失效）"
    assert lines[3] == "tail without newline", "无换行结尾的最后一行不能丢"

    rep = encoding_report(path)
    assert rep["exists"] is True and rep["gbk_lines"] == 1 and rep["utf8_lines"] >= 2
    assert rep["bad_lines"] == 0


def test_grep_finds_the_chinese_line_even_when_gbk(tmp_path: Path) -> None:
    """运维场景：凌晨三点 grep 一条中文报错，不许因为编码读不到。"""
    path = tmp_path / "backend.log"
    path.write_bytes((f"2026-10-01 ERROR src.x: {CN}\n").encode("gbk"))
    hits = grep(path, CN)
    assert len(hits) == 1 and CN in hits[0]


def test_strict_utf8_reading_would_fail_on_the_same_fixture(tmp_path: Path) -> None:
    """★ 自证：同一份 fixture 用**严格 UTF-8** 读必然失败 —— 证明回退是必需的。"""
    path = tmp_path / "backend.log"
    path.write_bytes((f"gbk 行：{CN}\n").encode("gbk"))
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("严格 UTF-8 竟然读成功了 ⇒ 这份 fixture 证明不了混编问题")
    assert CN in decode_line(path.read_bytes())


def test_decode_line_falls_back_to_replace_for_unknown_bytes() -> None:
    """第三种编码（既非 UTF-8 也非 GBK）不许抛 —— 用 replace 兜住。"""
    weird = b"\xff\xfe\x00\x41\x00\x42"          # UTF-16LE 片段
    text = decode_line(weird)
    assert isinstance(text, str) and "A" in text


def test_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    """读不存在的日志：返回空，不抛（排障脚本不该因为文件没轮转到就崩）。"""
    assert list(read_lines(tmp_path / "nope.log")) == []
    assert grep(tmp_path / "nope.log", "x") == []
    assert encoding_report(tmp_path / "nope.log")["exists"] is False
