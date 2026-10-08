"""CHG 交叉引用护栏（`CHG-0205` 的落点）—— 纯静态、不联网。

## 为什么需要它（一天之内同形事故**四次**）

本会话里 CHG 编号漂移了四次，形状完全相同 —— **号是"猜"的，不是"算"的**：

| # | 事故 | 后果 |
|---|---|---|
| 1 | 猜 `CHG-0189`，被协作者占用 ⇒ 守卫**静默跳过** | 登记行与 PRD 都写了，**只有变更行没写** |
| 2 | 猜 `CHG-0190`，同样被占用 | 同上 |
| 3 | 把"账单"写成占位号，而 `max+1` 恰好算到同一个号 | **8 处引用全部指错行** |
| 4 | 修 #3 的脚本**只护住"ID 格"、没护住"标题格"** | 改了登记行自己的号 ⇒ **两处标题不一致** |

⇒ 加两条**机器判据**，把"记得别写错"变成"写错会红"：

    ① PRD 章节标题的 CHG 号  ∩  该章节登记行标题的 CHG 号  ≠  空集
       （抓 #4 —— 它当时 `--ledger` 是**绿的**，因为校验器按章节 ID 匹配、不看标题）
    ② `src/` 与 `tests/` 里引用的 CHG 号必须**落在真实编号区间内**
       （抓 #1/#2 那类"引用了一个从没写入的号"）

⚠️ **两者都抓不住 #3**（号存在、但指错行）—— 那需要语义判断，
本判据**不做**，也**不假装做**（诚实边界）。
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
PRD = REPO / "docs" / "PRD.md"
LEDGER = REPO / "docs" / "REQUIREMENT_CHANGELOG.md"

#: 标题里的 `` （`CHG-0203`） `` / `` （`CHG-0182` / `CHG-0183`） `` 两种写法都要认
_CHG = re.compile(r"CHG-\d{4}")


def _prd_section_titles() -> dict[str, tuple[str, str]]:
    """`章节号 → (标题全文, 行号)`，只收"标题里带 CHG 号"的那些。"""
    out: dict[str, tuple[str, str]] = {}
    for i, ln in enumerate(PRD.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"#{2,3} (?:(PRD-)?(\d+(?:\.\d+)*))[ .、]", ln)
        if not m or "CHG-" not in ln:
            continue
        out[f"PRD-{m.group(2)}"] = (ln, str(i))
    return out


def _ledger_registry_title_chgs() -> dict[str, set[str]]:
    """台账「章节登记表」里 `章节 ID → 该行标题格出现的 CHG 集合`。

    ⚠️ 用 `setdefault().update()` 而不是"后写覆盖"：
    §19.40 实测有**两条**登记行（一条引 `CHG-0146`、一条引 `CHG-0144`）——
    "后写覆盖"会把前一条的号丢掉，让判据对"重复行"视而不见。
    """
    out: dict[str, set[str]] = {}
    for ln in LEDGER.read_text(encoding="utf-8").splitlines():
        if not ln.startswith("| PRD-"):
            continue
        cells = ln.split("|")
        if len(cells) > 2:
            out.setdefault(cells[1].strip(), set()).update(_CHG.findall(cells[2]))
    return out


def test_every_referenced_chg_exists() -> None:
    """★★ `src/` 与 `tests/` 里引用的 CHG 号必须**落在真实编号区间内**。

    抓的是"引用了一个**从没写入**的号" —— 编号靠猜、
    而插入守卫发现重号后**静默跳过**时，就是这个形状（本会话发生过两次）。

    ⚠️ **只查"接近当前最大号"的缺失**，不是"任何缺失"：
    测试夹具里**故意**写假号（`tests/unit/test_prd_sync_check.py` 用 `CHG-0777`
    验证校验器能不能报出"幻影章节"）⇒ 把夹具也算进来会得到一条**必然红**的判据，
    而"必然红"的判据会被当噪音关掉（本项目为此付过代价）。
    而**猜出来的号一定紧邻最大号**（`max+1`、`max+2`…）——
    ⇒ 用「`max_id + 30` 以内」这个界把夹具排除掉，不需要维护允许清单。
    """
    led = LEDGER.read_text(encoding="utf-8")
    existing = {int(x) for x in re.findall(r"^\| CHG-(\d{4}) \|", led, re.M)}
    assert existing, "台账里一条 CHG 行都没有 —— 判据本身失效了"
    ceiling = max(existing) + 30

    missing: list[str] = []
    for sub in ("src", "tests"):
        for p in (REPO / sub).rglob("*.py"):
            try:
                t = p.read_text(encoding="utf-8")
            except OSError:
                continue
            for raw in sorted(set(_CHG.findall(t))):
                n = int(raw[4:])
                if n not in existing and n <= ceiling:
                    missing.append(f"{p.relative_to(REPO).as_posix()} 引用了 {raw}")
    assert not missing, (
        "以下位置引用了**台账里不存在**（且落在真实编号区间内）的 CHG 号：\n  "
        + "\n  ".join(sorted(missing))
        + "\n\n⇒ 多半是「猜号」留下的悬空引用：改成真实号，"
          "或把内容里的号写成占位符、插入时替换。")


def test_prd_title_chg_matches_ledger_registry_row() -> None:
    """★★ PRD 章节标题的 CHG 号，必须与该章节登记行标题的 CHG 号**至少有一个重合**。

    抓的是"两处各写一遍、只改了一处" —— 本会话第 4 次事故就是这个形状
    （§41.24 的 PRD 标题 `CHG-0203` vs 登记行 `CHG-0202`），
    而且当时 `--ledger` **是绿的**（校验器按章节 **ID** 匹配，不看标题文字）。

    ## 为什么判据是「交集非空」而不是「完全相等」

    第一版写成"两边集合必须相等"，跑出 **16 条假阳性** ——
    很多历史章节的 PRD 标题引了多个 CHG（如 §19.21 引 `CHG-0097` / `CHG-0098`），
    而登记行只引**主号**。那是**既有文风**，不是漂移。
    ⇒ 判据的正确强度是：**两份标题指向的 CHG 必须至少有一个重合**
      （重合 = 说的是同一件事；完全不重合 = 其中一份指错了行）。
    这样既抓住"改了一处忘另一处"，又**不误伤**"登记行只引主号"的写法。
    """
    titles = _prd_section_titles()
    reg = _ledger_registry_title_chgs()
    assert titles, "PRD 里找不到任何带 CHG 号的章节标题 —— 判据本身失效了"

    bad: list[str] = []
    for sid, (prd_title, line) in titles.items():
        if not reg.get(sid):
            continue          # 登记行标题不带号 = 历史文风，不报
        a = set(_CHG.findall(prd_title))
        if not (a & reg[sid]):
            bad.append(f"{sid}（PRD:{line}）PRD 标题 {sorted(a)} "
                       f"与登记行 {sorted(reg[sid])} **没有任何重合**")
    assert not bad, (
        "PRD 章节标题与台账登记行**指向了不同的 CHG**（完全不重合）：\n  "
        + "\n  ".join(bad)
        + "\n\n⚠️ `--ledger` 是绿的也可能出现这种不一致 —— "
          "它按章节 ID 匹配、**不看标题文字**。")
