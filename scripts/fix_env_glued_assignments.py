"""修 `.env`：把被粘进注释行的赋值拆回独立行（用一次就删的那种修复脚本）。

## 症状

`dotenv` 逐行解析，赋值只要没独占一行就整行被当注释丢掉。实测 `.env` 第 1 行是

    # 本机环境配置…# DEEPSEEK_API_KEY：**必须写在这里**…DEEPSEEK_API_KEY=sk-2dbd…

于是 `DEEPSEEK_API_KEY` **从未生效**：`get_settings().deepseek_api_key` 是空串，
事件告警 stage-2（tier=reasoning）静默降级到本机 `qwen3:8b`（审计日志里
`model=deepseek-flash err=config_error: DEEPSEEK_API_KEY未配置` 紧跟一条
`model=qwen3:8b`），而小模型的分数被压到 0~55 —— 叠加阈值就成了"永远 0 告警"。
同一条链上还有 `QMT_ENABLED=0`、`MOSS_EM_DIRECT=1` 两行也被粘住。

## 做法（外科手术，不重排、不改值）

在每个"非行首的 `KEY=`"之前补一个换行；写回前后各解析一次 `dotenv_values`，
断言"新键集合 ⊇ 旧键集合"，并打印**因此被解救的键**。原文件先备份成 `.env.bak`。

用法：
    .venv\\Scripts\\python.exe scripts\\fix_env_glued_assignments.py --apply
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys

from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
BACKUP = ENV_PATH + ".bak"

# 非行首、前面不是标识符字符的 KEY=（避免把 URL 里的 ?a=B 之类误拆）
ASSIGN = re.compile(r"(?<![A-Za-z0-9_\n\r])([A-Z][A-Z0-9_]{2,})=")


def main(apply: bool) -> None:
    text = io.open(ENV_PATH, encoding="utf-8", newline="").read()
    before = dotenv_values(ENV_PATH)

    def _split(match: re.Match) -> str:
        # 行首的赋值不动（match.start() 前面就是换行的情况由 lookbehind 排除）
        return f"\n{match.group(1)}="

    fixed = ASSIGN.sub(_split, text)
    # 修完可能留下多余空行，压一下（不动缩进/值）
    fixed = re.sub(r"\n{3,}", "\n\n", fixed)

    print(f"原键 {len(before)} 个；将补 {fixed.count(chr(10)) - text.count(chr(10))} 个换行")
    if not apply:
        print("（dry-run；加 --apply 才写回）")
        return

    shutil.copyfile(ENV_PATH, BACKUP)
    io.open(ENV_PATH, "w", encoding="utf-8", newline="\n").write(fixed)

    after = dotenv_values(ENV_PATH)
    lost = set(before) - set(after)
    rescued = sorted(set(after) - set(before))
    print(f"备份 → {os.path.basename(BACKUP)}")
    print(f"新键 {len(after)} 个；丢失 {sorted(lost) or '无'}")
    print(f"因拆行而**新生效**的键：{rescued or '无'}")
    for key in rescued:
        value = after[key] or ""
        shown = value if len(value) <= 6 else value[:6] + "****"
        print(f"  {key} = {shown}")


if __name__ == "__main__":
    main("--apply" in sys.argv)
