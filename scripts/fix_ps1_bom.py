"""给 `.ps1` 补 UTF-8 BOM（Windows PowerShell 5.1 的硬要求）。

## 为什么需要

Windows PowerShell 5.1 读脚本文件时，**没有 BOM 就按本地代码页解码**
（中文系统 = GBK）。于是脚本里的中文字面量在**解析阶段**就已经是乱码 ——
写进日志变成「绛夊緟 140 池重打分结束」这种。逻辑不受影响（路径和命令都是
ASCII），但审计日志不可读。

本项目已多次记录 PowerShell + 非 ASCII 的坑（见 AGENTS 约定），这是又一例。
`write` 工具写的是 UTF-8 **无 BOM**，所以落盘后必须过一遍本脚本。

## 用法

    python scripts/fix_ps1_bom.py scripts/*.ps1
    python scripts/fix_ps1_bom.py --check scripts/*.ps1   # 只检查，返回码即结论
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"


def has_bom(path: Path) -> bool:
    return path.read_bytes()[:3] == BOM


def add_bom(path: Path) -> bool:
    """补 BOM；返回是否真的改了（已带 BOM 或本来就是纯 ASCII 则不动）。

    ⚠️ **纯 ASCII 文件刻意不补**：那些文件没有编码歧义，补了反而让
    `git diff` 多出一处无意义的二进制变更。
    """
    raw = path.read_bytes()
    if raw[:3] == BOM:
        return False
    try:
        raw.decode("ascii")
    except UnicodeDecodeError:
        pass
    else:
        return False
    path.write_bytes(BOM + raw)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="给 .ps1 补 UTF-8 BOM")
    parser.add_argument("paths", nargs="+", help="文件或通配（由 shell 展开）")
    parser.add_argument("--check", action="store_true", help="只检查，不修改")
    args = parser.parse_args()

    bad = 0
    for item in args.paths:
        path = Path(item)
        if not path.is_file():
            print(f"  跳过（不是文件）：{item}")
            continue
        raw = path.read_bytes()
        try:
            raw.decode("ascii")
            ascii_only = True
        except UnicodeDecodeError:
            ascii_only = False
        if has_bom(path):
            status = "✅ 已有 BOM"
        elif ascii_only:
            status = "－ 纯 ASCII，无需 BOM"
        elif args.check:
            status = "❌ 缺 BOM（含非 ASCII，PS 5.1 会按 GBK 解析成乱码）"
            bad += 1
        else:
            add_bom(path)
            status = "🔧 已补 BOM"
        print(f"  {path.name}: {status}")
    if args.check and bad:
        print(f"\n❌ {bad} 个文件缺 BOM")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
