"""拥挤度功能的剔除：解析清单 → **从库里取代码** → 生成配置 → 置 visible=0。

## ⚠️ 为什么代码必须从库里取、绝对不能手写（2026-09-22 差点犯的错）

第一版我按"最相近名称"把 6 个未精确命中的条目的代码**手打**进配置，
回查后发现 **4 个是错的**，而且错得极具破坏性：

    用户写的            我以为的代码        该代码真实是什么
    股权转让(并购重组)   885540.TI          **三胎概念**
    食品添加剂          885406.TI          **食品安全**
    个人防护用品        875186.TI          （清单里根本没有）
    商品服务与用品      875201.TI          **元宇宙**
    2026中报预增        886109.TI          **2026一季报预增**（不是中报）

也就是说：**手写代码会把「三胎概念 / 食品安全 / 元宇宙」静默删掉**，
而清单看上去"执行成功"。这正是本项目反复强调的那类失败方式。
所以本脚本只做一件事：**名称→代码的解析全部走库**，代码一个字都不手写。

## 顺带发现：同名不同码

`幽门螺杆菌概念` 在清单里有**两个代码**：`875216.TI`（visible=1，现行）
与 `885952.TI`（visible=0，旧的 875xxx 体系，本仓库黑名单文档记过这种换代同名）。
用户说的是"这个概念"，所以**两个都收**。脚本按名称取**全部**命中，
而不是 `LIMIT 1` —— 取一条会漏掉另一半。

用法：
    python scripts/prune_crowding.py                  # 只解析并核对
    python scripts/prune_crowding.py --emit-config    # 生成 configs/crowding_exclusions.yaml
    python scripts/prune_crowding.py --apply          # 置 visible=0（不改配置文件）
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from difflib import get_close_matches
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_DB = ROOT / "data" / "moss_finagent.db"
CONFIG = ROOT / "configs" / "crowding_exclusions.yaml"

RAW = """
粤港澳大湾区、股权转让（并购重组）、京津冀一体化、中俄贸易概念、肝炎概念、POE胶膜、
国企改革、AIGC概念、摘帽、蓝筹地产股、生物医药B类、网红经济、自由贸易港、新股与次新股、
航空货运与物流、两会概念、海上运输、油门螺旋杆菌、次新股、互联网出海、紫光系、
客运航空公司、天津自贸区、雄安概念股、纺织服装与奢侈品、饮料、牙科医疗、苹果概念、
现代服务业、医疗保健技术、钛白粉概念、生物质能发电、举牌、特斯拉、信息安全、视频添加剂、
资本市场、SaaS概念、物联网、京东概念、数字经济、个人护理产品、证金持股、金融科技、烟草、
移动互联网、建筑材料、大数据、智慧城市、创投概念、腾讯概念、内地房地产、彩票概念股、
纸业股、商品服务与用品、百货业、同花顺果指数、226中报预增
"""

#: 用户原文 → 库里真实名称。**只写"用户写的"和"库里叫什么"，
#: 代码由脚本查库得到** —— 手写代码会删错板块（见模块 docstring）。
FUZZY = {
    "股权转让（并购重组）": "股权转让(并购重组)",
    "油门螺旋杆菌": "幽门螺杆菌概念",
    "视频添加剂": "食品添加剂",
    "个人护理产品": "个人护理用品",
    "商品服务与用品": "商业服务与用品",
    "226中报预增": "2026中报预增",
}


def parse(raw: str) -> list[str]:
    text = raw.replace("、", "\n").strip()
    return [line.strip() for line in text.splitlines() if line.strip()]


def resolve(conn: sqlite3.Connection, names: list[str]) -> tuple[list[dict], list[str]]:
    """名称 → `[{code, name, visible, note}]`；返回 (命中, 未命中名单)。

    ⚠️ 一个名称取**全部**命中（同名不同码要一起收），且**校验名称回读一致**。
    """
    out: list[dict] = []
    miss: list[str] = []
    seen: set[str] = set()
    for given in names:
        real = FUZZY.get(given, given)
        if given not in FUZZY:
            # 精确匹配优先；括号全半角差异再试一次
            rows = conn.execute(
                "SELECT sector_code, sector_name, visible FROM sector_crowding_list"
                " WHERE sector_name = ?", (real,)).fetchall()
            if not rows:
                stripped = re.sub(r"[（(].*?[)）]", "", given).strip()
                rows = conn.execute(
                    "SELECT sector_code, sector_name, visible FROM sector_crowding_list"
                    " WHERE REPLACE(REPLACE(sector_name,'(','（'),')','）') = ?"
                    "    OR sector_name = ?", (given, stripped)).fetchall()
        else:
            rows = conn.execute(
                "SELECT sector_code, sector_name, visible FROM sector_crowding_list"
                " WHERE sector_name = ?", (real,)).fetchall()
        if not rows:
            miss.append(given)
            continue
        for row in rows:
            code = str(row["sector_code"])
            if code in seen:
                continue
            seen.add(code)
            # 名称回读校验：库里那一行的名字必须等于我们解析出来的名字
            assert str(row["sector_name"]) == real, (code, row["sector_name"], real)
            out.append({"code": code, "name": real,
                        "visible": int(row["visible"]),
                        "note": ("用户原文精确命中" if given not in FUZZY
                                 else f"用户写「{given}」，按唯一最相近名称推定")})
    return out, miss


def main() -> int:
    parser = argparse.ArgumentParser(description="拥挤度剔除清单")
    parser.add_argument("--emit-config", action="store_true",
                        help="把解析结果写成 configs/crowding_exclusions.yaml")
    parser.add_argument("--apply", action="store_true",
                        help="把命中板块置 visible=0（不改配置文件）")
    args = parser.parse_args()

    names = parse(RAW)
    conn = sqlite3.connect(str(MAIN_DB), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    rows, miss = resolve(conn, names)
    print(f"清单 {len(names)} 项 → 命中 **{len(rows)} 个板块代码**"
          f"（含同名不同码）")
    if miss:
        print(f"⚠️ 仍未命中 {len(miss)} 项：{miss}")
        conn.close()
        return 1
    dup = [r["name"] for r in rows if sum(1 for x in rows if x["name"] == r["name"]) > 1]
    if dup:
        print(f"⚠️ 同名不同码（都会收）：{sorted(set(dup))}")
    for row in rows:
        flag = "" if row["visible"] == 1 else "（已隐藏）"
        print(f"  {row['code']:<12}{row['name']}{flag}")

    if args.emit_config:
        body = [
            "# 板块拥挤度功能的**剔除清单**（不显示 / 不下载 / 不算指标）",
            "#",
            "# ⚠️ 本文件由 `scripts/prune_crowding.py --emit-config` **生成**，",
            "# 代码全部由库里查得 —— 手写代码会把「三胎概念/食品安全/元宇宙」",
            "# 这类无关板块静默删掉（2026-09-22 差点犯）。要改清单请改脚本里的",
            "# 原始文本或 `FUZZY` 映射，然后重新生成，不要直接编辑本文件。",
            "#",
            "# 与 `sector_blacklist.yaml` 的分工：那是**主线池级**排除（不进",
            "# `ml_board`）；这是**拥挤度功能**排除。用户 2026-09-22 明确说的是",
            "# 「板块拥挤度功能里」，所以不写进黑名单 —— 那会顺带踢出主线打分池。",
            "#",
            "# 三个机制共用本清单：`refresh._drop_blacklisted`（停日更下载）、",
            "# `metrics.compute_all_metrics`（不算指标）、`db.query_metrics` /",
            "# `query_all_latest_water_level`（前端不显示）。`visible = 0` 是",
            "# 额外一步。只挡更新、不删历史。",
            "version: 1",
            f'decided: "2026-09-22"',
            f"count: {len(rows)}",
            "entries:",
        ]
        for row in rows:
            body.append(f'  - {{ code: "{row["code"]}", name: "{row["name"]}" }}')
        CONFIG.write_text("\n".join(body) + "\n", encoding="utf-8")
        print(f"\n配置 → {CONFIG}（{len(rows)} 条）")

    if args.apply:
        codes = [r["code"] for r in rows]
        marks = ",".join("?" for _ in codes)
        before = conn.execute(
            f"SELECT COUNT(*) FROM sector_crowding_list WHERE sector_code"
            f" IN ({marks}) AND visible = 1", tuple(codes)).fetchone()[0]
        conn.execute(
            f"UPDATE sector_crowding_list SET visible = 0, pinned = 0,"
            f" updated_at = datetime('now','localtime')"
            f" WHERE sector_code IN ({marks})", tuple(codes))
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM sector_crowding_list WHERE visible = 1"
        ).fetchone()[0]
        print(f"已置 visible=0：{before} 个；现 visible=1 共 {after} 个")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
