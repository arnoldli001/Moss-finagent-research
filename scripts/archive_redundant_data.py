"""把**不复现的备份/过程表**从生产库搬进独立冷库，让生产库只剩"在用的表"。

## 为什么要有这一步

用户 2026-09-23 的诉求原文：

> 「帮我把数据库里和主线选股无关的冗余数据清除，我看很多备份数据、测试过程
> 数据，在调用时容易用错。」

真正的风险**不是磁盘**：两张库的 `freelist_count` 都是 0，删表并不会让文件变小。
风险是**用错** —— 实测有 **20 个分析脚本**把这批备份表当成**默认数据源**：

    mainline_score_bak_20260921_2015   被 12 个脚本引用
    mainline_score_bak_v26_prefloor    被  9 个脚本引用

也就是说，随手跑一个报告脚本，它读的是**某个历史时点的口径**，而结果看起来
和当前口径的报告一模一样。项目自己已经栽过同类跟头（§16.75 的"旧数据源
静默参与"），所以处置方式是：

1. **先归档**：备份内容一行不少地导进 `data/archive/` 下的独立库，
   带行数与内容指纹，归档后**回读校验**；
2. **再删表**：生产库只留在用的表；
3. **留台账**：`data/archive/LEDGER.md` 写清每张表原本叫什么、多少行、
   内容指纹、为什么归档 —— 以后想复现历史对照，知道去哪儿找。

## ⚠️ 归档不等于删除，这是刻意的

`mainline_score_bak_v26_prefloor` 是"V2.6 提高门槛前"的评分快照，
是**已经写进文档的历史对照基线**。直接删掉，那些结论就再也无法复现。
所以本工具**只搬不删**：搬进冷库后，用

    python scripts/archive_redundant_data.py --list-archive

就能看到里面有什么；`--restore <表名>` 可以按需搬回生产库。

## 用法

    # 预演（默认，只读，不改任何东西）
    python scripts/archive_redundant_data.py

    # 真的执行：归档 + 从生产库删表 + 写台账
    python scripts/archive_redundant_data.py --apply

    # 连文件类过程产物（logs_purify_*.txt、scripts/_*.txt、根目录 _probe_*.py）一起归档
    python scripts/archive_redundant_data.py --apply --files

    # 看冷库里有什么
    python scripts/archive_redundant_data.py --list-archive

    # 把某张表搬回生产库（例如要复现某个历史对照）
    python scripts/archive_redundant_data.py --restore mainline_score_bak_v26_prefloor

## ⚠️ 删表之后，20 个分析脚本的 `--table` 默认值会指向不存在的表

它们会**明确报错**（`no such table`），而**不是**静默读别的数据 ——
这个失败方式是刻意的：报错能被看见，"读到旧口径还以为是新口径"不能。
要复现历史对照，先把表 `--restore` 回来，或把脚本指到冷库：

    python scripts/board_false_positive_report.py \\
        --db data/archive/mainline_archive.db

前提是该脚本的 `--db` 走 `config`（见下面 `_patch_hint()`）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 生产库 → (归档源标识, 库路径)
DBS = {
    "mainline_cache": ROOT / "data" / "mainline_cache.db",
    "app": ROOT / "data" / "moss_finagent.db",
}

ARCHIVE_DIR = ROOT / "data" / "archive"
#: 一个库一个冷库文件（保持"哪个库来的"可追溯，且避免表名撞车）
ARCHIVE_FILES = {
    "mainline_cache": ARCHIVE_DIR / "mainline_cache_archive.db",
    "app": ARCHIVE_DIR / "mainline_archive.db",
}
LEDGER = ARCHIVE_DIR / "LEDGER.md"

#: 认定"备份/过程表"的判据：表名里带这些片段之一。
#: ⚠️ 刻意用**窄**判据而不是"名字里有 `_bak`"：`quant_bak_daily` 那种是
#: 业务别名（`src/quant/warehouse.py` 的 `_TABLES` 里有它），不能当备份删。
#: ⚠️ `_bak_purified` 必须列上：`ml_member_pure_bak_purified_task1` 是
#: Task 1 那版提纯名单（**比在用的表少一档 `relevant=1`**），
#: 正是"看着像在用名单、其实不是"的最危险一类。第一版判据漏了它，
#: dry-run 只列出 9 张而实际有 10 张 —— 判据要靠实测核对，不能靠推。
BACKUP_MARKERS = ("_bak_pre", "_bak_2026", "_bak_v26", "_bak_purge",
                  "_bak_purified")

#: 空的一次性探针库（内容为 0 行，直接归档后删除文件）
PROBE_FILES = ("data/_probe_mainline_cache.db",)

#: 文件类过程产物：根目录的调试探针与日志、`scripts/` 下的临时输出。
#: ⚠️ 只搬 **`_` 前缀**的临时产物；正常脚本（如 `audit_mainline_sources.py`）
#: 一律不动 —— 它们是被 `.gitignore` 刻意排除的私有资产，不是垃圾。
FILE_PATTERNS = (
    ("root_probe", "_probe_*.py"),
    ("root_probe", "_verify_*.py"),
    ("root_probe", "_snapshot_*.py"),
    ("root_log", "logs_*.txt"),
    ("scripts_tmp", "scripts/_*.txt"),
    ("scripts_tmp", "scripts/_*.log"),
)


@dataclass
class TableInfo:
    """一张待归档表的事实（归档前后都要能对上，所以全部落到台账）。"""

    db_key: str
    name: str
    rows: int
    columns: list[str] = field(default_factory=list)
    trade_span: str = ""
    fingerprint: str = ""


def _connect(path: Path, *, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                               timeout=30.0)
    else:
        conn = sqlite3.connect(path, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [str(r["name"]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        if not str(r["name"]).startswith("sqlite_")]


def _fingerprint(conn: sqlite3.Connection, table: str, columns: list[str]) -> str:
    """内容指纹（sha1 前 12 位）。

    ⚠️ 为什么必须有它：本项目已经踩过"两组备份其实内容完全相同"的坑
    （`pre_task1` 与 `purge10` 指纹一致，各存一份纯属浪费）。
    指纹让"这两张表是不是同一份快照"变成可验证的事实，而不是猜。
    """
    digest = hashlib.sha1()
    try:
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1, 2'):
            digest.update(("|".join("" if v is None else str(v)
                                    for v in tuple(row)) + ";").encode())
    except sqlite3.Error:
        return ""
    return digest.hexdigest()[:12]


def _is_backup(name: str) -> bool:
    return any(marker in name for marker in BACKUP_MARKERS)


def _survey() -> dict[str, list[TableInfo]]:
    """扫描两个生产库，列出所有备份/过程表。"""
    found: dict[str, list[TableInfo]] = {}
    for db_key, path in DBS.items():
        if not path.exists():
            continue
        conn = _connect(path)
        try:
            out: list[TableInfo] = []
            for name in _tables(conn):
                if not _is_backup(name):
                    continue
                columns = [str(r["name"]) for r in
                           conn.execute(f'PRAGMA table_info("{name}")')]
                rows = int(conn.execute(
                    f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                span = ""
                if "trade_date" in columns:
                    lo, hi = conn.execute(
                        f'SELECT MIN(trade_date), MAX(trade_date) FROM "{name}"'
                    ).fetchone()
                    span = f"{lo}~{hi}"
                out.append(TableInfo(db_key=db_key, name=name, rows=rows,
                                     columns=columns, trade_span=span))
            found[db_key] = out
        finally:
            conn.close()
    return found


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection,
                info: TableInfo) -> int:
    """把一张表原样复制进冷库（先 DROP 再建，保证可重复执行）。"""
    ddl = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (info.name,)).fetchone()[0]
    dst.execute(f'DROP TABLE IF EXISTS "{info.name}"')
    dst.execute(ddl)
    marks = ",".join("?" * len(info.columns))
    cols = ",".join(f'"{c}"' for c in info.columns)
    written = 0
    cursor = src.execute(f'SELECT * FROM "{info.name}"')
    while True:
        batch = cursor.fetchmany(2000)
        if not batch:
            break
        dst.executemany(
            f'INSERT INTO "{info.name}" ({cols}) VALUES ({marks})',
            [tuple(r) for r in batch])
        written += len(batch)
    dst.commit()
    return written


def _file_size_mb(path: Path) -> float:
    return path.stat().st_size / 1024 / 1024 if path.exists() else 0.0


def _archive_tables(apply: bool) -> int:
    survey = _survey()
    total_tables = sum(len(v) for v in survey.values())
    if not total_tables:
        print("✅ 生产库里已经没有备份/过程表，无需处理")
        return 0

    print(f"发现 {total_tables} 张备份/过程表：\n")
    infos: list[TableInfo] = []
    for db_key, items in survey.items():
        conn = _connect(DBS[db_key])
        try:
            for info in items:
                info.fingerprint = _fingerprint(conn, info.name, info.columns)
                infos.append(info)
        finally:
            conn.close()
        print(f"  【{db_key}】")
        for info in items:
            print(f"    {info.name:44s} {info.rows:>9,d} 行  "
                  f"{info.trade_span:>21s}  {info.fingerprint}")

    # 指纹相同的组：台账里点明，提醒"这几张是同一份快照"
    groups: dict[str, list[str]] = {}
    for info in infos:
        if info.fingerprint:
            groups.setdefault(info.fingerprint, []).append(info.name)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if dupes:
        print("\n⚠️ 内容完全相同的备份（同一份快照存了多份）：")
        for digest, names in dupes.items():
            print(f"    {digest}: {', '.join(names)}")

    if not apply:
        print("\n（预演模式，未做任何改动。加 --apply 执行归档 + 删表）")
        return 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n开始归档 → {ARCHIVE_DIR}")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    for db_key, path in DBS.items():
        items = [i for i in infos if i.db_key == db_key]
        if not items or not path.exists():
            continue
        target = ARCHIVE_FILES[db_key]
        before = _file_size_mb(target)
        src = _connect(path)
        dst = _connect(target, readonly=False)
        try:
            for info in items:
                written = _copy_table(src, dst, info)
                # ⚠️ 回读校验：行数对不上就不删源表。搬丢数据的备份比不搬更糟
                #    —— 它会让人以为"历史对照还能复现"，实际已经缺行。
                back = int(dst.execute(
                    f'SELECT COUNT(*) FROM "{info.name}"').fetchone()[0])
                ok = "✅" if back == info.rows == written else "❌"
                print(f"  {ok} {info.name}: 写 {written} / 回读 {back} / 源 {info.rows}")
                if back != info.rows or written != info.rows:
                    print(f"      ⚠️ 行数不一致 —— 保留源表，跳过删除：{info.name}")
                    info.rows = -1  # 标记为"不可删"
        finally:
            src.close()
            dst.close()
        print(f"  冷库 {target.name}: {before:.1f} MB → {_file_size_mb(target):.1f} MB")

        # 校验通过才删源表
        src = _connect(path, readonly=False)
        try:
            for info in items:
                if info.rows < 0:
                    continue
                src.execute(f'DROP TABLE IF EXISTS "{info.name}"')
                print(f"  🗑️ 生产库已删表：{info.name}")
            src.commit()
        finally:
            src.close()

    _write_ledger([i for i in infos if i.rows >= 0], stamp)
    print(f"\n✅ 归档完成。台账：{LEDGER}")
    print("   复现历史对照：先 python scripts/archive_redundant_data.py "
          "--restore <表名>")
    return 0


def _write_ledger(infos: list[TableInfo], stamp: str) -> None:
    lines = [
        "# 冷库台账（冗余数据归档记录）",
        "",
        f"> 生成时间：{stamp}　生成者：`scripts/archive_redundant_data.py`",
        "",
        "这些表**不是垃圾**，是「已经写进文档的历史对照基线」或「用户点名的池级变更"
        "前的快照」。归档的目的是让生产库只剩在用的表，避免分析脚本**静默读到历史"
        "口径**（实测 20 个脚本把它们当默认数据源）。",
        "",
        "## 怎么用",
        "",
        "```bash",
        "# 看冷库里有什么",
        "python scripts/archive_redundant_data.py --list-archive",
        "",
        "# 把某张表搬回生产库（复现历史对照）",
        "python scripts/archive_redundant_data.py --restore mainline_score_bak_v26_prefloor",
        "```",
        "",
        "## 归档清单",
        "",
        "| 来源库 | 表名 | 行数 | 交易日区间 | 内容指纹 |",
        "|---|---|---:|---|---|",
    ]
    for info in sorted(infos, key=lambda i: (i.db_key, i.name)):
        lines.append(f"| {info.db_key} | `{info.name}` | {info.rows:,} | "
                     f"{info.trade_span or '—'} | `{info.fingerprint}` |")
    groups: dict[str, list[str]] = {}
    for info in infos:
        if info.fingerprint:
            groups.setdefault(info.fingerprint, []).append(info.name)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if dupes:
        lines += ["", "## ⚠️ 内容重复的备份（同一份快照存了多份）", ""]
        for digest, names in dupes.items():
            lines.append(f"- `{digest}`：{', '.join(f'`{n}`' for n in names)}")
    lines += [
        "",
        "## 文件类过程产物",
        "",
        "`logs_purify_*.txt`、`scripts/_*.txt`、`scripts/_*.log`、根目录 "
        "`_probe_*.py` / `_verify_*.py` / `_snapshot_*.py` 归到 "
        "`data/archive/files/`。",
        "",
        "⚠️ **刻意不动** `scripts/audit_mainline_sources.py` 这类正常脚本，"
        "也不动 `docs/` 下的报告 —— 前者是被 `.gitignore` 排除的私有资产，"
        "后者是结论本身。",
        "",
    ]
    LEDGER.write_text("\n".join(lines), encoding="utf-8")


def _archive_files(apply: bool) -> int:
    """把文件类过程产物归到 `data/archive/files/`（搬走，不删）。"""
    import glob

    target_root = ARCHIVE_DIR / "files"
    moves: list[tuple[str, Path, Path]] = []
    for group, pattern in FILE_PATTERNS:
        for hit in sorted(glob.glob(str(ROOT / pattern))):
            src = Path(hit)
            if not src.is_file():
                continue
            rel = src.relative_to(ROOT)
            dst = target_root / group / rel.name
            moves.append((group, src, dst))
    if not moves:
        print("没有需要归档的文件")
        return 0
    total_mb = sum(s.stat().st_size for _g, s, _d in moves) / 1024 / 1024
    print(f"待归档文件 {len(moves)} 个（{total_mb:.2f} MB）→ {target_root}")
    for group, src, _dst in moves[:15]:
        print(f"  [{group}] {src.relative_to(ROOT)}")
    if len(moves) > 15:
        print(f"  … 其余 {len(moves) - 15} 个")
    if not apply:
        print("（预演模式，未做任何改动。加 --apply 执行）")
        return 0
    for _group, src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        shutil.move(str(src), str(dst))
    print(f"✅ 已归档 {len(moves)} 个文件到 {target_root}")
    return 0


def _probe_files(apply: bool) -> int:
    """空的一次性探针库：归档后删除（内容为 0 行，没有保价值）。"""
    found = [ROOT / p for p in PROBE_FILES if (ROOT / p).exists()]
    if not found:
        return 0
    for path in found:
        print(f"空探针库：{path.relative_to(ROOT)}")
    if not apply:
        print("（预演模式，未做任何改动。加 --apply 执行）")
        return 0
    for path in found:
        try:
            conn = _connect(path)
            try:
                rows = sum(int(conn.execute(
                    f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
                    for t in _tables(conn))
            finally:
                conn.close()
        except sqlite3.Error:
            rows = -1
        if rows == 0:
            path.unlink()
            print(f"🗑️ 已删除（0 行）：{path.relative_to(ROOT)}")
        else:
            print(f"⚠️ 非空（{rows} 行），保留：{path.relative_to(ROOT)}")
    return 0


def _list_archive() -> int:
    if not ARCHIVE_DIR.exists():
        print(f"冷库目录不存在：{ARCHIVE_DIR}")
        return 0
    for _db_key, path in ARCHIVE_FILES.items():
        if not path.exists():
            continue
        print(f"\n【{path.name}】 {_file_size_mb(path):.1f} MB")
        conn = _connect(path)
        try:
            for name in _tables(conn):
                n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                cols = [str(r["name"]) for r in
                        conn.execute(f'PRAGMA table_info("{name}")')]
                span = ""
                if "trade_date" in cols:
                    lo, hi = conn.execute(
                        f'SELECT MIN(trade_date), MAX(trade_date) FROM "{name}"'
                    ).fetchone()
                    span = f"{lo}~{hi}"
                print(f"    {name:44s} {int(n):>9,d} 行  {span}")
        finally:
            conn.close()
    if LEDGER.exists():
        print(f"\n台账：{LEDGER}")
    return 0


def _restore(table: str) -> int:
    """把一张表从冷库搬回生产库（覆盖同名表）。"""
    for db_key, archive_path in ARCHIVE_FILES.items():
        if not archive_path.exists():
            continue
        src = _connect(archive_path)
        try:
            exists = src.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not exists:
                continue
            columns = [str(r["name"]) for r in
                       src.execute(f'PRAGMA table_info("{table}")')]
            rows = int(src.execute(
                f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        finally:
            src.close()
        info = TableInfo(db_key=db_key, name=table, rows=rows, columns=columns)
        dst = _connect(DBS[db_key], readonly=False)
        src = _connect(archive_path)
        try:
            written = _copy_table(src, dst, info)
            back = int(dst.execute(
                f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            print(f"⬆️ {db_key}.{table}: 写 {written} / 回读 {back}")
        finally:
            src.close()
            dst.close()
        print(f"✅ 已还原到生产库（{DBS[db_key].name}）。"
              f"用完记得再归档：--apply")
        return 0
    print(f"❌ 冷库里找不到表：{table}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把备份/过程表归档到独立冷库，生产库只留在用的表")
    parser.add_argument("--apply", action="store_true",
                        help="真的执行（默认只预演，不做任何改动）")
    parser.add_argument("--files", action="store_true",
                        help="同时归档文件类过程产物（logs_purify_*.txt 等）")
    parser.add_argument("--list-archive", action="store_true",
                        help="列出冷库里已有的表")
    parser.add_argument("--restore", metavar="TABLE",
                        help="把冷库里的某张表搬回生产库")
    args = parser.parse_args()

    if args.list_archive:
        return _list_archive()
    if args.restore:
        return _restore(args.restore)

    code = _archive_tables(args.apply)
    code |= _probe_files(args.apply)
    if args.files:
        code |= _archive_files(args.apply)
    else:
        print("\n（文件类过程产物未处理 —— 加 --files 一起归档）")
    return code


if __name__ == "__main__":
    # ⚠️ 刻意**不做 VACUUM**：两张库的空闲页都是 0，VACUUM 不会缩小文件，
    #    却要在 6 GB 库上持写锁数分钟 —— 会把用户的手动刷新挤掉
    #    （项目已有"全量重打分占写锁 90 分钟"的教训）。要收缩空间请另行决定。
    raise SystemExit(main())
