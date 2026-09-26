"""主线挖掘结果的**备份 / 还原**（`mainline_score` + `mainline_alert`）。

## 为什么需要它

池子一改（删板块、`purify_members` 提纯成分股），**所有历史评分都会变** ——
因为分数里有横截面分位，成分股一动手，六维里的资金流/拥挤度也跟着动。
而全量重打分要 ~90 分钟，跑完就没有"改之前"可比了。

用户的要求是「不要影响我原有的主线挖掘效果」。这条要求只能靠
**先备份、再改动、再对比、不行就还原**来满足 —— 口头保证没有意义。

## 约定

沿用在用的表命名习惯（库里已有 `mainline_score_bak_20260921_2015`、
`mainline_score_bak_v26_prefloor`），备份就是**同库的旁表**：

    mainline_score_bak_<tag>  /  mainline_alert_bak_<tag>

同库旁表的理由：不占额外磁盘布局、不需要跨文件 attach、还原是一条
`INSERT ... SELECT`。⚠️ 但它**不能替代异地备份** —— 库文件损坏时旁表一起没。

## 用法

    python scripts/backup_mainline_results.py --list
    python scripts/backup_mainline_results.py --tag pre_task1
    python scripts/backup_mainline_results.py --restore pre_task1
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "moss_finagent.db"
CACHE = ROOT / "data" / "mainline_cache.db"
#: `(库, 源表, 备份表前缀)`。
#: ⚠️ `ml_member_pure` 是**打分的输入**（六维里的资金流/拥挤度按成分股聚合）。
#: `purify_members.py --replace` 会**先清空再写**，中途失败就留下一张半空的表，
#: 而打分读它会静默产出偏掉的分数 —— 所以它必须和结果表一起备份。
PAIRS = (("mainline_score", "mainline_score_bak_"),
         ("mainline_alert", "mainline_alert_bak_"))
CACHE_PAIRS = (("ml_member_pure", "ml_member_pure_bak_"),)
_SAFE = re.compile(r"^[A-Za-z0-9_]+$")


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _backups(conn: sqlite3.Connection) -> list[tuple[str, str, int, str]]:
    """`[(源表, 备份表, 行数, 日期范围)]`。"""
    out: list[tuple[str, str, int, str]] = []
    all_tables = set(_tables(conn))
    for source, prefix in PAIRS:
        for name in sorted(all_tables):
            if not name.startswith(prefix):
                continue
            n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            span = ""
            try:
                lo, hi = conn.execute(
                    f'SELECT MIN(trade_date), MAX(trade_date) FROM "{name}"').fetchone()
                span = f"{lo}~{hi}"
            except sqlite3.Error:
                span = "（无 trade_date 列）"
            out.append((source, name, n, span))
    return out


def _promote(other: Path) -> int:
    """把 `other` 库里的两张结果表换进生产库（换前先备份生产库）。

    为什么要有这一步：重型重打分跑在**副本**上（`MOSS_SQLITE_PATH`），
    跑完必须先对照指标确认"没把效果搞坏"，确认通过才把结果换进生产库。
    换之前自动打一个 `pre_promote` 还原点 —— 这样即使换完才发现问题，
    也还有一步可退。
    """
    if not other.exists():
        print(f"❌ 找不到 {other}")
        return 2
    if other.resolve() == DB.resolve():
        print("❌ --promote 的库不能是生产库自己")
        return 2

    # 1) 先备份生产库（存在就拒绝，避免覆盖上一个还原点）
    existing = set(_tables(conn_probe(DB)))
    for _source, prefix in PAIRS:
        if f"{prefix}pre_promote" in existing:
            print(f"⚠️ {prefix}pre_promote 已存在 —— 先处理它再 promote")
            return 1
    code = _snapshot(tag="pre_promote", cache=False)
    if code != 0:
        return code

    # 2) 从副本库读出来、写进生产库
    src = sqlite3.connect(f"file:{other.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(DB, timeout=120)
    for source, _prefix in PAIRS:
        rows = src.execute(f'SELECT * FROM "{source}"').fetchall()
        cols = [r[1] for r in src.execute(f'PRAGMA table_info("{source}")')]
        dst.execute(f'DELETE FROM "{source}"')
        marks = ",".join("?" * len(cols))
        dst.executemany(
            f'INSERT INTO "{source}" ({",".join(cols)}) VALUES ({marks})', rows)
        after = dst.execute(f'SELECT COUNT(*) FROM "{source}"').fetchone()[0]
        print(f"⬆️ {source}: 换入 {len(rows)} 行（生产库现有 {after} 行）")
    dst.commit()
    src.close()
    dst.close()
    print("\n✅ 已换入生产库。还原："
          "python scripts/backup_mainline_results.py --restore pre_promote")
    return 0


def conn_probe(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _snapshot(*, tag: str, cache: bool = True) -> int:
    """把生产库的两张结果表存成 `*_bak_<tag>`。"""
    if not _SAFE.match(tag):
        print(f"❌ 标签非法：{tag}")
        return 2
    conn = sqlite3.connect(DB, timeout=60)
    existing = set(_tables(conn))
    stamp = time.strftime("%Y%m%d_%H%M")
    for source, prefix in PAIRS:
        name = f"{prefix}{tag}"
        if name in existing:
            print(f"⚠️ {name} 已存在 —— 不覆盖（避免毁掉还原点）")
            conn.close()
            return 1
        conn.execute(f'CREATE TABLE "{name}" AS SELECT * FROM "{source}"')
        n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        print(f"💾 {source} → {name}（{n} 行，备份于 {stamp}）")
    conn.commit()
    conn.close()
    if cache and CACHE.exists():
        cconn = sqlite3.connect(CACHE, timeout=60)
        cex = set(_tables(cconn))
        for source, prefix in CACHE_PAIRS:
            name = f"{prefix}{tag}"
            if name in cex:
                print(f"⚠️ {name} 已存在 —— 换标签")
                cconn.close()
                return 1
            cconn.execute(f'CREATE TABLE "{name}" AS SELECT * FROM "{source}"')
            n = cconn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            print(f"💾 {source} → {name}（{n} 行，备份于 {stamp}）")
        cconn.commit()
        cconn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="主线挖掘结果备份/还原")
    parser.add_argument("--tag", default="", help="备份标签（字母数字下划线）")
    parser.add_argument("--restore", default="", help="从该标签还原")
    parser.add_argument("--list", action="store_true", help="列出已有备份")
    parser.add_argument("--cache-only", action="store_true",
                        help="只备份 mainline_cache.db 里的 ml_member_pure"
                             "（结果表已有还原点时用）")
    parser.add_argument("--promote", default="", metavar="DB",
                        help="把另一个库里已验证的 mainline_score/mainline_alert"
                             "换进生产库（换之前**自动**先备份生产库，标签 pre_promote）")
    args = parser.parse_args()

    if args.promote:
        return _promote(Path(args.promote))

    if not DB.exists():
        print(f"❌ 找不到 {DB}")
        return 2
    conn = sqlite3.connect(DB, timeout=60)

    if args.cache_only:
        if not args.tag or not _SAFE.match(args.tag):
            print("❌ --cache-only 需要合法的 --tag")
            conn.close()
            return 2
        conn.close()
        cconn = sqlite3.connect(CACHE, timeout=60)
        cex = set(_tables(cconn))
        for source, prefix in CACHE_PAIRS:
            name = f"{prefix}{args.tag}"
            if name in cex:
                print(f"⚠️ {name} 已存在 —— 换标签")
                cconn.close()
                return 1
            cconn.execute(f'CREATE TABLE "{name}" AS SELECT * FROM "{source}"')
            n = cconn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            print(f"💾 {source} → {name}（{n} 行）")
        cconn.commit()
        cconn.close()
        return 0

    if args.list or (not args.tag and not args.restore):
        items = _backups(conn)
        if not items:
            print("（没有任何备份）")
        for source, name, n, span in items:
            print(f"  {source:16s} → {name:44s} {n:>7} 行  {span}")
        conn.close()
        return 0

    if args.restore:
        tag = args.restore
        if not _SAFE.match(tag):
            print(f"❌ 标签非法：{tag}")
            return 2
        for source, prefix in PAIRS:
            name = f"{prefix}{tag}"
            if name not in set(_tables(conn)):
                print(f"⚠️ {name} 不存在，跳过")
                continue
            before = conn.execute(f'SELECT COUNT(*) FROM "{source}"').fetchone()[0]
            conn.execute(f'DELETE FROM "{source}"')
            conn.execute(f'INSERT INTO "{source}" SELECT * FROM "{name}"')
            after = conn.execute(f'SELECT COUNT(*) FROM "{source}"').fetchone()[0]
            print(f"↩️ {source}: {before} 行 → 还原为 {after} 行（来自 {name}）")
        conn.commit()
        conn.close()
        if CACHE.exists():
            cconn = sqlite3.connect(CACHE, timeout=60)
            for source, prefix in CACHE_PAIRS:
                name = f"{prefix}{tag}"
                if name not in set(_tables(cconn)):
                    print(f"⚠️ {name} 不存在，跳过")
                    continue
                before = cconn.execute(f'SELECT COUNT(*) FROM "{source}"').fetchone()[0]
                cconn.execute(f'DELETE FROM "{source}"')
                cconn.execute(f'INSERT INTO "{source}" SELECT * FROM "{name}"')
                after = cconn.execute(f'SELECT COUNT(*) FROM "{source}"').fetchone()[0]
                print(f"↩️ {source}: {before} 行 → 还原为 {after} 行（来自 {name}）")
            cconn.commit()
            cconn.close()
        print("\n✅ 已还原。⚠️ 记得确认服务端缓存/前端已刷新。")
        return 0

    tag = args.tag
    if not _SAFE.match(tag):
        print(f"❌ 标签非法（只允许字母数字下划线）：{tag}")
        conn.close()
        return 2
    stamp = time.strftime("%Y%m%d_%H%M")
    existing = set(_tables(conn))
    for source, prefix in PAIRS:
        name = f"{prefix}{tag}"
        if name in existing:
            print(f"⚠️ {name} 已存在 —— 先删掉它或换个标签（不覆盖，避免毁掉上一次的还原点）")
            conn.close()
            return 1
        conn.execute(f'CREATE TABLE "{name}" AS SELECT * FROM "{source}"')
        n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        print(f"💾 {source} → {name}（{n} 行，备份于 {stamp}）")
    conn.commit()
    conn.close()

    if CACHE.exists():
        cconn = sqlite3.connect(CACHE, timeout=60)
        cex = set(_tables(cconn))
        for source, prefix in CACHE_PAIRS:
            name = f"{prefix}{tag}"
            if name in cex:
                print(f"⚠️ {name} 已存在 —— 换标签")
                cconn.close()
                return 1
            cconn.execute(f'CREATE TABLE "{name}" AS SELECT * FROM "{source}"')
            n = cconn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            print(f"💾 {source} → {name}（{n} 行，备份于 {stamp}）")
        cconn.commit()
        cconn.close()

    print(f"\n✅ 备份完成（tag={tag}）。还原："
          f"python scripts/backup_mainline_results.py --restore {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
