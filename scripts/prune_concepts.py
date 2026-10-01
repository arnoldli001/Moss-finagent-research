"""按批次**池级剔除**概念板块：解析 → 核对真名 → 一次改三处（+ 备份）。

## 用户的三句话 = 一个开关

> 「也不放在主线挖掘监控，也不计算板块拥挤度和展示在前端。」

| 用户的话 | 机制 | 代码位置 |
|---|---|---|
| 不放在主线挖掘监控 | 板块不进 `ml_board`（打分池） | `import_crowding_pool()` |
| 不计算板块拥挤度 | 停止日更 | `refresh._drop_blacklisted()` |
| 不计算板块拥挤度 | 不算指标 | `metrics._sector_blacklist()` |
| 展示在前端 | 前端看板隐藏 | `sector_crowding_list.visible = 0` |
| （传递结果）不上报 | 不在池内 ⇒ 无告警 | — |

前三条挂在 `configs/sector_blacklist.yaml` 上；本脚本负责把批次清单
**追加**到那里，并置 `visible=0`。

## 为什么清单放在 `configs/concept_removal_batches.yaml`

用户已连发 5 批。清单内嵌进脚本的话每批都要改代码；放到配置里之后
「追加一段 YAML」就是全部工作，而且留下了谁/何时/以什么理由剔了什么的审计线。
本脚本只做「读清单 → 核对 → 落库」，不含任何名单常量。

## ⚠️ 池子一动，所有历史分数都要重算

分数是**截面百分位**，池子变了全部板块的分数都会变。所以每次落库之后
必须 `rescore_mainline.py --force`（约 80 分钟）。**尽量攒够一批再动。**

## ⚠️ 只删概念，不删个股

`_prune_boards()` 只 `DELETE FROM ml_board`；`ml_member` / `ml_member_pure` /
`ml_board_bar` / `ml_stock_*` 一条不动（用户特别强调过：一个个股可能关联
很多概念，删概念不该动个股）。本脚本每次落库前把 `ml_board` 备份成
`ml_board_bak_pre{batch}`，回滚只需删黑名单那一段 + 重跑 `import_crowding_pool()`。

用法：
    .venv\\Scripts\\python.exe scripts/prune_concepts.py --list
    .venv\\Scripts\\python.exe scripts/prune_concepts.py --batch 5
    .venv\\Scripts\\python.exe scripts/prune_concepts.py --batch 5 --apply
    .venv\\Scripts\\python.exe scripts/prune_concepts.py --all --apply
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ★ `CHG-0143`：拥挤度的**用户配置**表（`sector_crowding_list` 等）已从共享
#   遗留主库迁到**本环境应用库**（`app_db`：dev → `data/dev/moss_dev.db`、
#   pilot → `data/pilot/moss_pilot.db`）。所以路径**不能再写死**
#   `data/moss_finagent.db` —— 那会读到旧库那份、而写入静默落到没人看的地方。
#   统一走 registry 解析（与 `sector_crowding/config.py` 同一个来源）：
#      `SECTOR_CROWDING_DB=<路径>` 可显式覆盖（离线演练/临时副本用）。
import os as _os
from src.sector_crowding.config import load_config as _load_crowding_config
_CROWDING_CONFIG_DB = _os.environ.get("SECTOR_CROWDING_DB") or str(
    _load_crowding_config().config_db_path)


CACHE_DB = ROOT / "data" / "mainline_cache.db"

BLACKLIST = ROOT / "configs" / "sector_blacklist.yaml"
BATCHES = ROOT / "configs" / "concept_removal_batches.yaml"


def parse(raw: str) -> list[tuple[str, str]]:
    """按「名称 + 6 位代码」切；**也支持只给名称**（代码留空，稍后按真名解析）。

    ⚠️ 用户原文里混着**全角顿号、制表符、行首空格**（`动物疫苗 885846、\\t俄乌
    冲突概念`），手抄必错 —— 所以原样进正则。名称里可能有括号与点号
    （`车联网(车路协同)`、`Web3.0`），所以**有代码时用代码做锚点**。

    用户第六批只给了名称、没给代码。**不猜** —— 名称留空，由
    `resolve_names()` 拿 `ml_board` 的真实名录反查；查不到或有歧义就**报错
    拒绝落库**（"猜一个最像的"正是会静默删错板块的做法）。
    """
    text = str(raw).replace("、", "\n").replace("\t", " ")
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.search(r"^(.*?)\s*(\d{6})\s*,?\s*$", line)
        if match:
            out.append((match.group(1).strip(), match.group(2).strip()))
        else:
            out.append((line.rstrip("，,。").strip(), ""))
    return out


def resolve_names(pairs: list[tuple[str, str]],
                  by_code: dict[str, str]) -> tuple[list[tuple[str, str]], list[str]]:
    """把「只给名称」的条目按 `ml_board` 真名反查成代码。

    返回 `(补齐后的 pairs, 问题描述列表)`。**任何问题都不自动修**：
    * 名称查不到 → 可能是名字记错，也可能是它已经不在池内；
    * 名称对应多个代码 → 池内存在同名不同码（黑名单文档提过 `865xxx` 与
      `885xxx` 有换代同名），这时候**必须让人指定代码**。

    这两类都返回非空的问题列表，调用方据此拒绝落库。
    """
    name_to_codes: dict[str, list[str]] = {}
    for code, name in by_code.items():
        name_to_codes.setdefault(name, []).append(code)
    out: list[tuple[str, str]] = []
    problems: list[str] = []
    for name, code in pairs:
        if code:
            out.append((name, code))
            continue
        hits = name_to_codes.get(name) or []
        if len(hits) == 1:
            out.append((name, hits[0].replace(".TI", "")))
        elif not hits:
            problems.append(f"名称「{name}」在池内找不到对应板块")
        else:
            problems.append(f"名称「{name}」在池内对应 {len(hits)} 个代码：{hits}"
                            "（必须由人指定代码，不能猜）")
    return out, problems


def load_batches(path: Path) -> list[dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    batches = []
    for item in (raw.get("batches") or []):
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        pairs = parse(str(item.get("raw") or ""))
        batches.append({
            "id": int(item["id"]),
            "decided": str(item.get("decided") or ""),
            "label": " ".join(str(item.get("label") or "").split()),
            "note": " ".join(str(item.get("note") or "").split()),
            "pairs": pairs,
            # 同一批里重复写的（用户第五批把「空间计算」写了两遍）在这里去掉。
            # ⚠️ 只给名称的条目此时还没有代码（`""`），由 `resolve_names()`
            # 拿 `ml_board` 反查后补 —— 所以这里**不能**直接当最终清单用。
            "codes": sorted({f"{code}.TI" for _n, code in pairs if code}),
        })
    return sorted(batches, key=lambda b: b["id"])


#: 中文数字 → 阿拉伯数字。段头既有 `第 4 批` 也有 `第四批` 两种写法
#: （前者是本脚本生成的，后者是 `prune_pool_batch4.py` 早期写下的），
#: 解析必须两种都认 —— 否则会把**已落库的批次**当成没落而重复追加。
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(text: str) -> int:
    """把 `四` / `十` / `十二` / `二十` 这类中文数字转成整数。"""
    if not text:
        return 0
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        return (_CN_DIGITS.get(head, 1) * 10) + _CN_DIGITS.get(tail, 0)
    return _CN_DIGITS.get(text, 0) if len(text) == 1 else int(text or 0)


def applied_ids(text: str) -> set[int]:
    """黑名单文件里已经落过哪些批次。

    认两种段头：`# ---- … 第 4 批 …` 与 `# ---- … 第四批 …`。
    """
    out: set[int] = set()
    for match in re.finditer(r"第\s*([0-9]+|[一二三四五六七八九十]+)\s*批", text):
        token = match.group(1)
        out.add(int(token) if token.isdigit() else _cn_to_int(token))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="按批次池级剔除概念板块")
    parser.add_argument("--batches", default=str(BATCHES))
    parser.add_argument("--batch", type=int, default=0, help="要落库的批次号")
    parser.add_argument("--all", action="store_true", help="所有未落库的批次")
    parser.add_argument("--list", action="store_true", help="只看批次台账")
    parser.add_argument("--apply", action="store_true", help="真写；不加只干跑")
    args = parser.parse_args()

    batches = load_batches(Path(args.batches))
    text = BLACKLIST.read_text(encoding="utf-8")
    done = applied_ids(text)

    if args.list:
        print(f"{'批次':<5}{'日期':<12}{'条数':>5}  {'已落库':<7} 说明")
        for item in batches:
            # 只给名称的批次还没有代码，按条目数报
            unique = len(item["codes"]) or len({n for n, _c in item["pairs"]})
            dup = len(item["pairs"]) - unique
            print(f"{item['id']:<5}{item['decided']:<12}{unique:>5}  "
                  f"{'✅' if item['id'] in done else '—':<7} "
                  f"{item['label'][:52]}"
                  + (f"（原文重复 {dup} 条已去重）" if dup > 0 else ""))
        return 0

    targets = ([b for b in batches if b["id"] == args.batch] if args.batch
               else [b for b in batches if b["id"] not in done]
               if args.all else [])
    if not targets:
        print("❌ 用 --batch N / --all / --list 指定要做什么")
        return 2
    todo = [b for b in targets if b["id"] not in done]
    if not todo:
        print(f"批次 {[b['id'] for b in targets]} 都已落库，无需处理")
        return 0

    # ---------- 核对（**不许**跳过的三步）----------
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cache.row_factory = sqlite3.Row
    pool = {str(r["code"]): str(r["name"]) for r in
            cache.execute("SELECT code, name FROM ml_board")}
    cache.close()
    known_black = set(re.findall(r'code:\s*"([^"]+)"', text))
    print(f"`ml_board` 现有 {len(pool)} 个；黑名单已有 {len(known_black)} 条\n")

    # 先解析「只给名称」的条目（第六批起用户不再给代码），任何歧义都拒绝落库
    for batch in todo:
        named_only = [n for n, c in batch["pairs"] if not c]
        resolved, problems = resolve_names(batch["pairs"], pool)
        if problems:
            print(f"❌ 批次 {batch['id']} 名称解析失败，拒绝落库：")
            for item in problems:
                print(f"    {item}")
            return 1
        batch["pairs"] = resolved
        # 记下"哪些是反查出来的"，供落库前人工核对
        batch["named_only"] = [(n, c) for n, c in resolved if n in set(named_only)]
        batch["codes"] = sorted({f"{c}.TI" for _n, c in resolved})

    for batch in todo:
        pairs = batch["pairs"]
        bad_name = [(c, n, pool[f"{c}.TI"]) for n, c in pairs
                    if f"{c}.TI" in pool and pool[f"{c}.TI"] != n]
        not_in_pool = [(n, c) for n, c in pairs if f"{c}.TI" not in pool]
        fresh = [c for c in batch["codes"] if c not in known_black]
        print(f"=== 批次 {batch['id']}（{batch['decided']}）："
              f"解析 {len(pairs)} 条 → 去重 {len(batch['codes'])} 个 ===")
        resolved_only = batch.get("named_only") or []
        if resolved_only:
            print(f"    ⚠️ 本批原文**只给名称**，以下代码是按 ml_board 真名反查的，"
                  f"请核对：")
            for name, code in resolved_only:
                print(f"        {name}  →  {code}.TI")
        print(f"    在池内 {len(pairs) - len(not_in_pool)} / "
              f"不在池内 {len(not_in_pool)} / 名称不一致 {len(bad_name)} / "
              f"黑名单需新增 {len(fresh)}")
        if not_in_pool:
            print(f"    ⚠️ 不在池内：{not_in_pool}")
        if bad_name:
            print("    ⚠️ 名称与池内真名不一致（左=用户写的，右=真名）：")
            for code, given, actual in bad_name:
                print(f"        {code}: {given} ≠ {actual}")
        if bad_name or not_in_pool:
            print("    ❌ 核对不通过 —— 不落库（只按代码落库会静默删错板块）")
            return 1

    if not args.apply:
        print("\n（未加 --apply，未写任何东西）")
        return 0

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for batch in todo:
        # ---------- 1) 备份 ml_board ----------
        name = f"ml_board_bak_pre{batch['id']}"
        with sqlite3.connect(str(CACHE_DB), timeout=60.0) as cache:
            cache.execute(f"DROP TABLE IF EXISTS {name}")
            cache.execute(f"CREATE TABLE {name} AS SELECT * FROM ml_board")
            rows = cache.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"\n[批次 {batch['id']}] 已备份 `ml_board` → `{name}`（{rows} 行）")

        # ---------- 2) 黑名单追加 ----------
        names = {f"{c}.TI": n for n, c in batch["pairs"]}
        known = set(re.findall(r'code:\s*"([^"]+)"',
                               BLACKLIST.read_text(encoding="utf-8")))
        fresh = [c for c in batch["codes"] if c not in known]
        block = ["",
                 f"# ---- {batch['decided']} 第 {batch['id']} 批"
                 f"（{len(fresh)} 个）----",
                 "#",
                 f"# {batch['label']}"]
        for line in re.findall(r".{1,72}", batch["note"] or ""):
            block.append(f"# {line}")
        block.append("#")
        block.append("# ⚠️ 池子一动，截面百分位全变 → 必须 "
                     "`rescore_mainline.py --force`。")
        block.append("# 回滚：删掉本段 + 重跑 `import_crowding_pool()`"
                     "（bar / 成分股数据从未被删）。")
        block.append(f"# 落库时间：{now}")
        for code in fresh:
            block.append(f'  - {{ code: "{code}", name: "{names[code]}" }}')
        BLACKLIST.write_text(
            BLACKLIST.read_text(encoding="utf-8").rstrip("\n") + "\n"
            + "\n".join(block) + "\n", encoding="utf-8")
        print(f"[批次 {batch['id']}] 已追加黑名单 {len(fresh)} 条")

        # ---------- 3) 前端看板置 visible=0 ----------
        codes = batch["codes"]
        marks = ",".join("?" for _ in codes)
        with sqlite3.connect(str(_CROWDING_CONFIG_DB), timeout=60.0) as main:
            main.execute("PRAGMA busy_timeout=60000")
            before = main.execute(
                f"SELECT COUNT(*) FROM sector_crowding_list WHERE sector_code"
                f" IN ({marks}) AND visible = 1", tuple(codes)).fetchone()[0]
            main.execute(
                f"UPDATE sector_crowding_list SET visible = 0, pinned = 0,"
                f" updated_at = ? WHERE sector_code IN ({marks})",
                (now, *codes))
            main.commit()
            after = main.execute(
                "SELECT COUNT(*) FROM sector_crowding_list WHERE visible = 1"
            ).fetchone()[0]
        print(f"[批次 {batch['id']}] 前端看板：{before} 个置 visible=0；"
              f"现 visible=1 共 {after} 个")

    print("\n下一步（必须按序）：")
    print("  1. 重新入池：`import_crowding_pool()`（让 `ml_board` 收缩）")
    print("  2. `scripts/rescore_mainline.py --start 20231009 --end 20260918"
          " --force`（约 80 分钟）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
