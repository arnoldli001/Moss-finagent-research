"""需求 ↔ 开发 PRD 文档对账：把「每轮对话都要核对一次」变成一条命令。

## 为什么需要它（用户 2026-09-28 原话）

> 「给 trae 再加一个 skill 护栏：每次对话检查用户输入的需求与开发 pr 文档的
>   出入，**没有的补充进去，有差异的改动**。」

**问题是真实的、可量的**：`docs/PRD.md` 是用户 2026-09-12 提供的原始需求
（文件最后修改时间 2026-09-14），而此后仓库长出了大量 PRD 里查不到的
需求（QMT 降位、数据索引清单、缓存预热/keepalive、竞价链、ETF、拥挤度…）。
对比一行就能看出来：

    PRD 命中 0 处，仓库实现面命中几十处

**根因**：skill 是**事后可读**的提醒，而"对账"这件事发生在**每轮对话**里 ——
靠"记得核对"必然漏。所以把它做成命令。

## 三种模式

    # ① 本轮用户提的这个需求词，与 PRD / 台账 的出入是什么？
    uv run python scripts/prd_sync_check.py --keyword "数据索引"

    # ② 台账完整性：PRD 每个章节都登记了吗？证据路径都还在吗？
    uv run python scripts/prd_sync_check.py --ledger      # 默认模式

    # ③ 台账表格骨架（章节 ID 从 PRD 现读，不手写）
    uv run python scripts/prd_sync_check.py --list-sections

    # ④ 自证：喂坏输入，必须报错（不然就是假绿）
    uv run python scripts/prd_sync_check.py --self-test

## 设计纪律（抄给别的项目也成立）

| 纪律 | 做法 |
|---|---|
| **清单从 PRD 读，不硬编码** | 章节 ID 由 `docs/PRD.md` 的标题现算 —— PRD 改了章节，台账缺哪节立刻报出来 |
| **自证 + 反向测试** | `--self-test` 必须能对"已知缺失"报 MISS；只会报 OK 的检查等于没有检查 |
| **"没量到" ≠ "量到 0"** | 关键词查不到时报 `TODO`（可能用词不同，人工判断），**绝不报 OK** |
| **落 PRD 才算闭环** | `类型=新增/冲突/细化` 而 `处置∉{补写PRD,改写PRD}`（冲突类可 `标注废止`）却标"已闭环" → 报错。这就是"没有的补充进去，有差异的改动"的机器判据 |
| **改了 ≠ 生效了** | 台账写「PRD 已更新=是」，但 `docs/PRD.md` 里**搜不到这个 CHG 号** → 报错。防止"在台账里宣布改了 PRD"而 PRD 根本没动 |
| **不静默降级** | 文件不存在、表格解析不到、章节数为 0 —— 一律报错，不许当成"全部合规" |
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

#: 需求基线（用户提供的原始需求，**只读对账基准**）
PRD_PATH = "docs/PRD.md"
#: 变更台账（每轮对话的需求增删改都落在这里）
LEDGER_PATH = "docs/REQUIREMENT_CHANGELOG.md"

#: 台账里两张表的定位标记（标题里含这个词就算）
LEDGER_SECTION_TABLE_MARK = "章节登记表"
LEDGER_ENTRY_TABLE_MARK = "变更台账"

#: 枚举（**只认机器可读的标识，不认给人看的文案**）
KIND_ENUM = ("新增", "冲突", "细化", "废止", "非需求")
ACTION_ENUM = ("补写PRD", "改写PRD", "标注废止", "仅登记台账", "待办")
UPDATED_ENUM = ("是", "否")
STATUS_ENUM = ("已闭环", "待办", "待确认")
SECTION_STATUS_ENUM = ("已登记", "无差异", "待确认")

#: 必须落到 PRD 才算闭环的类型（用户要求："没有的补充进去，有差异的改动"）
MUST_LAND_IN_PRD = ("新增", "冲突", "细化")
#: 每种类型"落 PRD"的合法处置。
#: `冲突` 多一种：**标注废止**（把旧口径标成废止并指向新口径）也是落 PRD ——
#: 但同样要求 PRD 里能看到 CHG 号（见 validate_ledger 的"改了≠生效了"判据）。
LANDING_ACTIONS_FOR: dict[str, tuple[str, ...]] = {
    "新增": ("补写PRD", "改写PRD"),
    "冲突": ("补写PRD", "改写PRD", "标注废止"),
    "细化": ("补写PRD", "改写PRD"),
}
#: 兼容旧引用（子集：不含 标注废止）
LANDING_ACTIONS = ("补写PRD", "改写PRD")

#: 实现面（"仓库真的做了"的证据）
CODE_GLOBS = (
    "src/**/*.py", "configs/**/*.yaml", "configs/**/*.yml",
    "web/src/**/*.ts", "web/src/**/*.tsx", "apps/**/*.py",
)
#: 纪律面（AGENTS.md / skill —— 说明"这是被登记过的项目要求"）
RULE_GLOBS = (".trae/skills/**/*.md", "AGENTS.md")

SKIP_PARTS = (".venv", "__pycache__", ".git", "node_modules", "dist", "data/")

#: 证据里可复跑的路径
_PATH_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:py|md|ya?ml|json|toml|tsx?|ps1|sql|txt)")


# ============================================================
# Markdown 解析（PRD 章节 / 台账两张表）
# ============================================================


@dataclass(frozen=True)
class PrdSection:
    id: str
    level: int
    title: str
    line: int


def parse_prd_sections(prd_text: str) -> list[PrdSection]:
    """按出现顺序给 PRD 章节编号：H2 → `PRD-n`，H3 → `PRD-n.m`。

    ★ 必须跳过**围栏代码块**：PRD §10.1 里嵌了一整段 ```markdown 模板，
    里面的 `## 项目定位`、`## 环境配置` 不是章节。不跳就会被当成真章节，
    于是台账"登记"了一堆幻影章节 —— 而且是**静默**的。
    """
    out: list[PrdSection] = []
    h2 = h3 = 0
    in_fence = False
    for i, line in enumerate(prd_text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("### "):
            h3 += 1
            out.append(PrdSection(f"PRD-{h2}.{h3}", 3, line[4:].strip(), i))
        elif line.startswith("## "):
            h2 += 1
            h3 = 0
            out.append(PrdSection(f"PRD-{h2}", 2, line[3:].strip(), i))
    return out


def _table_rows(text: str, heading_contains: str) -> tuple[list[str], list[dict[str, str]]]:
    """取"标题含 `heading_contains` 的那一节里的第一个 markdown 表格"。

    Returns: `(表头, [行字典])`；找不到返回 `([], [])` —— 调用方**必须**把它
    当错误报出来，不许当成"没有条目=合规"。
    """
    lines = text.splitlines()
    start = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if (s.startswith("## ") or s.startswith("### ")) and heading_contains in s:
            start = i + 1
            break
    if start < 0:
        return [], []

    header: list[str] = []
    rows: list[dict[str, str]] = []
    for line in lines[start:]:
        s = line.strip()
        if s.startswith(("## ", "### ")) and rows:
            break
        if not s.startswith("|"):
            if header and rows:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(set(c) <= {"-", ":", " "} and c for c in cells):
            continue  # 分隔行
        if not header:
            header = cells
            continue
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        rows.append(dict(zip(header, cells, strict=False)))
    return header, rows


def _cell(row: dict[str, str], col: str) -> str:
    """按列名取值（容忍列名里带 markdown 修饰）。"""
    if col in row:
        return row[col]
    for k, v in row.items():
        if col in k or k in col:
            return v
    return ""


# ============================================================
# 台账校验
# ============================================================


@dataclass
class Issue:
    level: str      # ERROR / WARN
    where: str      # 章节表 / 条目 CHG-xxxx
    detail: str
    fix: str = ""


@dataclass
class KeywordReport:
    keyword: str
    prd: int = 0
    ledger: int = 0
    code: dict[str, int] = field(default_factory=dict)
    rule: dict[str, int] = field(default_factory=dict)

    @property
    def code_total(self) -> int:
        return sum(self.code.values())

    @property
    def rule_total(self) -> int:
        return sum(self.rule.values())

    @property
    def in_repo(self) -> bool:
        return self.code_total > 0 or self.rule_total > 0

    @property
    def verdict(self) -> str:
        """三态里**只有三种是终态**，且 `TODO` 绝不算通过。

        | PRD | 台账 | 仓库 | 结论 |
        |---|---|---|---|
        | 有 | 有 | — | `OK` 已对账 |
        | 有 | 无 | — | `MISS_LEDGER` 老需求没登记（或本轮没落账）|
        | 无 | — | 有 | `MISS_PRD` ★ 做了但 PRD 查不到 → **补进 PRD** |
        | 无 | — | 无 | `TODO` 没量到 —— 可能用词不同，人工判断 |
        """
        if self.prd > 0:
            return "OK" if self.ledger > 0 else "MISS_LEDGER"
        if self.in_repo:
            return "MISS_PRD"
        return "TODO"


def _iter_files(globs: tuple[str, ...]) -> list[Path]:
    seen: dict[str, Path] = {}
    for g in globs:
        for p in ROOT.glob(g):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT).as_posix()
            if any(s in rel for s in SKIP_PARTS):
                continue
            seen[rel] = p
    return [seen[r] for r in sorted(seen)]


def _count_in(path: Path, keyword: str) -> int:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return 0
    return text.lower().count(keyword.lower())


def scan_keyword(keyword: str) -> KeywordReport:
    """在三个面（PRD / 台账 / 仓库）里数同一个需求词。

    仓库面再分两层，因为两层的含义不同：
    · **实现面** —— 真的做了（src / configs / web / apps）
    · **纪律面** —— 被写进 AGENTS.md / skill 的要求（说明它已经是一类约束）
    """
    rep = KeywordReport(keyword=keyword)
    prd = ROOT / PRD_PATH
    ledger = ROOT / LEDGER_PATH
    rep.prd = _count_in(prd, keyword) if prd.exists() else 0
    rep.ledger = _count_in(ledger, keyword) if ledger.exists() else 0
    for p in _iter_files(CODE_GLOBS):
        n = _count_in(p, keyword)
        if n:
            top = p.relative_to(ROOT).parts[0]
            rep.code[top] = rep.code.get(top, 0) + n
    for p in _iter_files(RULE_GLOBS):
        n = _count_in(p, keyword)
        if n:
            rel = p.relative_to(ROOT).as_posix()
            key = "AGENTS.md" if rel == "AGENTS.md" else ".trae/skills"
            rep.rule[key] = rep.rule.get(key, 0) + n
    return rep


def _evidence_paths(cell: str) -> list[str]:
    """从"证据"单元格里抠出候选路径（忽略 http 链接）。"""
    out: list[str] = []
    for m in _PATH_RE.finditer(cell):
        cand = m.group(0).replace("\\", "/")
        if cand.startswith("http"):
            continue
        out.append(cand)
    return out


def validate_ledger(prd_text: str, ledger_text: str,
                    *, root: Path | None = None) -> list[Issue]:
    """台账完整性 + 可复跑性 + **"落 PRD 才算闭环"**。

    这是本 skill 的核心判据：用户要求"没有的补充进去，有差异的改动"，
    所以 `类型=新增/冲突/细化` 的条目如果 `处置` 不是补写/改写 PRD，
    就**不许**标成"已闭环" —— 只登台账不改 PRD 正是要防的那个漏。
    """
    root = root or ROOT
    issues: list[Issue] = []

    prd_sections = parse_prd_sections(prd_text)
    if not prd_sections:
        # 「没量到」≠「量到 0」：解析器坏掉时**必须报错**，
        # 否则"0 个章节"会自动变成"全部覆盖"（假绿）。
        issues.append(Issue(
            "ERROR", "PRD 解析",
            f"从 {PRD_PATH} 里解析出 0 个章节 —— 解析器或文件坏了",
            "先修解析器；不要在这个状态下下'章节全覆盖'的结论"))
        return issues

    sec_header, sec_rows = _table_rows(ledger_text, LEDGER_SECTION_TABLE_MARK)
    if not sec_rows:
        issues.append(Issue(
            "ERROR", "章节登记表",
            f"台账里找不到「{LEDGER_SECTION_TABLE_MARK}」表格（或表为空）",
            f"在 {LEDGER_PATH} 用 `--list-sections` 的输出重建该表"))
    ent_header, ent_rows = _table_rows(ledger_text, LEDGER_ENTRY_TABLE_MARK)
    if not ent_rows and not ent_header:
        issues.append(Issue(
            "ERROR", "变更台账",
            f"台账里找不到「{LEDGER_ENTRY_TABLE_MARK}」表格",
            "该表必须有表头（哪怕暂无条目，也要留一行示例/空表头）"))

    # ① 章节覆盖：PRD 现读的章节 vs 台账登记的章节
    registered = {_cell(r, "章节 ID") for r in sec_rows}
    registered = {r for r in registered if r}
    prd_ids = {s.id for s in prd_sections}
    for s in prd_sections:
        if s.id not in registered:
            issues.append(Issue(
                "ERROR", "章节登记表",
                f"{s.id}（{s.title}，{PRD_PATH}:{s.line}）未登记",
                "补一行，登记状态填 已登记/无差异/待确认"))
    for rid in sorted(registered - prd_ids):
        issues.append(Issue(
            "ERROR", "章节登记表",
            f"{rid} 在 PRD 里不存在（PRD 改过章节或编号漂移）",
            "删掉该行或按当前 PRD 重新编号"))
    if sec_rows:
        for r in sec_rows:
            st = _cell(r, "登记状态")
            if st and st not in SECTION_STATUS_ENUM:
                issues.append(Issue(
                    "ERROR", f"章节表 {_cell(r, '章节 ID')}",
                    f"登记状态 `{st}` 不在枚举 {SECTION_STATUS_ENUM} 内",
                    "改成枚举值"))

    # ② 条目逐列校验
    seen_ids: set[str] = set()
    for r in ent_rows:
        cid = _cell(r, "变更 ID")
        if not cid:
            continue
        where = f"条目 {cid}"
        if not re.fullmatch(r"CHG-\d{4,}", cid):
            issues.append(Issue("ERROR", where, f"ID 格式非法：`{cid}`",
                                "用 CHG-0001 形式"))
        if cid in seen_ids:
            issues.append(Issue("ERROR", where, "ID 重复", "换一个未占用的编号"))
        seen_ids.add(cid)

        date = _cell(r, "日期")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            issues.append(Issue("ERROR", where,
                                f"日期 `{date}` 不是 YYYY-MM-DD", "填真实日期"))

        kind = _cell(r, "类型")
        if kind not in KIND_ENUM:
            issues.append(Issue("ERROR", where,
                                f"类型 `{kind}` 不在 {KIND_ENUM} 内", "改成枚举值"))
        action = _cell(r, "处置")
        if action not in ACTION_ENUM:
            issues.append(Issue("ERROR", where,
                                f"处置 `{action}` 不在 {ACTION_ENUM} 内", "改成枚举值"))
        updated = _cell(r, "PRD 已更新")
        if updated not in UPDATED_ENUM:
            issues.append(Issue("ERROR", where,
                                f"PRD 已更新 `{updated}` 不在 {UPDATED_ENUM} 内",
                                "改成 是/否"))
        status = _cell(r, "状态")
        if status not in STATUS_ENUM:
            issues.append(Issue("ERROR", where,
                                f"状态 `{status}` 不在 {STATUS_ENUM} 内", "改成枚举值"))

        # ★ 核心 1：新增/冲突/细化 必须落到 PRD 才算闭环
        allowed_landing = LANDING_ACTIONS_FOR.get(kind, LANDING_ACTIONS)
        if (kind in MUST_LAND_IN_PRD and status == "已闭环"
                and action not in allowed_landing):
            issues.append(Issue(
                "ERROR", where,
                f"类型={kind} 且 状态=已闭环，但处置={action or '(空)'} "
                f"—— 需求没有落到 {PRD_PATH} 就宣布闭环",
                "补写/改写 PRD 后把处置改成 补写PRD/改写PRD"
                "（冲突类也可写 标注废止）；或把状态改回 待办"))
        # ★ 核心 2：「PRD 已更新=是」只是一句自述 —— 必须**在 PRD 里看得见 CHG 号**。
        # 这就是本项目最贵的教训「我改了 ≠ 它生效了」的机器判据：
        # 台账说改了 PRD，而 PRD 里搜不到这条 CHG → 那条"改动"根本不存在。
        if status == "已闭环" and updated == "是" and cid not in prd_text:
            issues.append(Issue(
                "ERROR", where,
                f"台账声称「PRD 已更新=是」，但 {PRD_PATH} 里搜不到 `{cid}` "
                "—— 改了 ≠ 生效了",
                f"在 PRD 里就地写上 `{cid}`（补写内容或加"
                "「⚠️ 已废止（CHG-xxxx，日期）」注），不要只在台账里宣布"))
        if updated == "否" and action in allowed_landing:
            issues.append(Issue(
                "ERROR", where,
                "处置写的是补写/改写 PRD，但「PRD 已更新」= 否",
                "两者取其一（真改了就把标志改成 是）"))
        if status == "待办" and action != "待办":
            issues.append(Issue(
                "ERROR", where,
                f"状态=待办 但 处置={action or '(空)'}",
                "待办条目处置也填 待办，闭环后再改"))
        if status == "已闭环" and action == "待办":
            issues.append(Issue("ERROR", where,
                                "状态=已闭环 但 处置=待办", "闭环必须写明是怎么落地的"))

        # 章节引用必须真实存在
        sec_ref = _cell(r, "PRD 章节")
        for token in re.findall(r"PRD-\d+(?:\.\d+)?", sec_ref):
            if token not in prd_ids:
                issues.append(Issue("ERROR", where,
                                    f"引用了不存在的章节 `{token}`",
                                    "按当前 PRD 的章节 ID 填"))

        # 需求原话不得为空（用户输入是本台账的输入，不是概括）
        quote = _cell(r, "需求原话") or _cell(r, "需求")
        if not quote:
            issues.append(Issue("ERROR", where, "「需求原话」为空",
                                "填用户原话（可截断，不得改写语义）"))

        # 证据必须可复跑：至少一个路径真实存在
        ev_cell = _cell(r, "证据")
        paths = _evidence_paths(ev_cell)
        existing = [p for p in paths if (root / p).exists()]
        if not paths:
            issues.append(Issue("ERROR", where, "证据里没有任何文件路径",
                                "写 `path:line`，让人能复跑"))
        elif not existing:
            issues.append(Issue("ERROR", where,
                                f"证据路径一个都不存在：{paths[:3]}",
                                "改成现存的路径（或删掉失效引用）"))
    return issues


# ============================================================
# 自证（喂坏输入必须报错 —— 否则就是假绿）
# ============================================================


def self_test() -> list[str]:
    """返回失败列表（空 = 通过）。

    每条都针对一种"检查器本身失效"的方式：
    · 关键词判据坏掉 → 永远报 OK（假绿）
    · 围栏代码块没跳过 → 幻影章节（静默）
    · 解析器坏掉 → 0 章节 = "全覆盖"（假绿）
    · 证据校验没跑 → 失效路径照样过
    """
    fails: list[str] = []

    # ① PRD 里绝不存在的词，必须报 MISS_PRD（而不是 OK）
    rep = scan_keyword("market_wide_breadth_term_zzz_nonexistent")
    if rep.verdict == "OK":
        fails.append("关键词判据失效：不存在的词却报 OK（假绿）")
    if rep.verdict not in ("TODO", "MISS_PRD"):
        fails.append(f"不存在的词返回了意外结论：{rep.verdict}")

    # ② 围栏代码块里的 `## xxx` 不许被当成章节
    fake = "## 真章节\n\n```markdown\n## 伪造章节\n### 伪造子节\n```\n\n### 真子节\n"
    secs = parse_prd_sections(fake)
    titles = [s.title for s in secs]
    if "伪造章节" in titles or any("伪造" in t for t in titles):
        fails.append("围栏代码块没跳过：把模板里的标题当成了 PRD 章节")

    # ③ 解析器坏掉必须报错，不能"0 章节 = 全覆盖"
    issues = validate_ledger("# 空 PRD", "## 1. 章节登记表\n")
    if not any("0 个章节" in i.detail for i in issues):
        fails.append("PRD 解析出 0 章节时没有报错（假绿通道）")

    # ④ 缺失章节必须被报出来
    prd = "## 一\n### 1.1\n## 二\n"
    ledger = ("## 1. 章节登记表\n\n| 章节 ID | PRD 标题 | 登记状态 |\n|---|---|---|\n"
              "| PRD-1 | 一 | 无差异 |\n")
    issues = validate_ledger(prd, ledger)
    if not any("PRD-1.1" in i.detail for i in issues):
        fails.append("漏登记的章节没被报出来")

    # ⑤ 幻影章节 ID 必须被报出来
    ledger2 = ("## 1. 章节登记表\n\n| 章节 ID | PRD 标题 | 登记状态 |\n|---|---|---|\n"
               "| PRD-1 | 一 | 无差异 |\n| PRD-99.9 | 幻影 | 无差异 |\n")
    issues = validate_ledger(prd, ledger2)
    if not any("PRD-99.9" in i.detail for i in issues):
        fails.append("台账里的幻影章节 ID 没被报出来")

    # ⑥ ★ 核心判据：新增/冲突 未落 PRD 却标"已闭环" → 必须报错
    ledger3 = (
        "## 1. 章节登记表\n\n| 章节 ID | PRD 标题 | 登记状态 |\n|---|---|---|\n"
        "| PRD-1 | 一 | 无差异 |\n| PRD-1.1 | 1.1 | 无差异 |\n| PRD-2 | 二 | 无差异 |\n\n"
        "## 3. 变更台账\n\n"
        "| 变更 ID | 日期 | 类型 | PRD 章节 | 需求原话 | 处置 | PRD 已更新 | 状态 | 证据 |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
        "| CHG-0001 | 2026-09-28 | 新增 | PRD-1 | 要求 X | 仅登记台账 | 否 | 已闭环 | "
        f"{PRD_PATH}:1 |\n")
    issues = validate_ledger(prd, ledger3)
    if not any("没有落到" in i.detail for i in issues):
        fails.append("★ 新增需求未落 PRD 却标已闭环 —— 没被报出来（护栏失效）")

    # ⑦ 证据路径失效必须被报出来
    ledger4 = ledger3.replace(f"{PRD_PATH}:1", "docs/绝不存在的文件_zzz.md:1")
    issues = validate_ledger(prd, ledger4)
    if not any("一个都不存在" in i.detail for i in issues):
        fails.append("失效的证据路径没被报出来")

    # ⑧ ★「PRD 已更新=是」但 PRD 里搜不到 CHG 号 → 必须报错。
    # 这是"我改了 ≠ 它生效了"的机器判据：台账宣布改了 PRD，PRD 里却没有痕迹。
    ledger5 = (
        "## 1. 章节登记表\n\n| 章节 ID | PRD 标题 | 登记状态 |\n|---|---|---|\n"
        "| PRD-1 | 一 | 无差异 |\n| PRD-1.1 | 1.1 | 无差异 |\n| PRD-2 | 二 | 无差异 |\n\n"
        "## 3. 变更台账\n\n"
        "| 变更 ID | 日期 | 类型 | PRD 章节 | 需求原话 | 处置 | PRD 已更新 | 状态 | 证据 |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
        "| CHG-0777 | 2026-09-28 | 新增 | PRD-1 | 要求 Y | 补写PRD | 是 | 已闭环 | "
        f"{PRD_PATH}:1 |\n")
    issues = validate_ledger(prd, ledger5)          # prd 里没有 CHG-0777
    if not any("改了 ≠ 生效了" in i.detail for i in issues):
        fails.append("★ 声称改了 PRD 但 PRD 里没有 CHG 号 —— 没被报出来（假闭环）")

    return fails


# ============================================================
# 输出
# ============================================================


def _print_keyword(rep: KeywordReport) -> None:
    print(f"\n{'─' * 88}")
    print(f"需求对账：关键词 `{rep.keyword}`")
    print("─" * 88)
    print(f"  {'PRD 面（需求基线）':<28}{rep.prd:>4} 处   {PRD_PATH}")
    print(f"  {'台账面（变更登记）':<28}{rep.ledger:>4} 处   {LEDGER_PATH}")
    code = "、".join(f"{k}:{v}" for k, v in sorted(rep.code.items())) or "无"
    rule = "、".join(f"{k}:{v}" for k, v in sorted(rep.rule.items())) or "无"
    print(f"  {'实现面（真做了吗）':<28}{rep.code_total:>4} 处   {code}")
    print(f"  {'纪律面（写进规则吗）':<28}{rep.rule_total:>4} 处   {rule}")

    verdict = rep.verdict
    print(f"\n  结论：{verdict}")
    if verdict == "OK":
        print("    PRD 与台账都提到了它 —— 仍需人工确认口径是否一致（脚本只查存在性）")
    elif verdict == "MISS_LEDGER":
        print("    ⚠️ PRD 有、台账无：可能是老需求从未登记，也可能是本轮没落账。")
        print("       → 在台账补一条（类型=细化/非需求 按实际情况），别让需求史断档。")
    elif verdict == "MISS_PRD":
        print("    ❌ 仓库做了、PRD 查不到 —— 这正是「没有的补充进去」的现场。")
        print(f"       → 补写 {PRD_PATH} 对应章节，并登记 CHG-xxxx。")
    else:
        print("    ⚠️ TODO（没量到 ≠ 量到 0）：三个面都没有这个说法。")
        print("       → 可能用户用词不同，或需求还没实现。人工判断，**不许当通过**。")


def _print_sections() -> None:
    prd = ROOT / PRD_PATH
    if not prd.exists():
        print(f"❌ 找不到 {PRD_PATH}")
        return
    secs = parse_prd_sections(prd.read_text(encoding="utf-8-sig", errors="replace"))
    print(f"# {PRD_PATH} 章节 → ID（由脚本现读，用在台账「章节登记表」里）\n")
    print("| 章节 ID | PRD 标题 | 层级 | 章节登记状态 | 备注 |")
    print("|---|---|---|---|---|")
    for s in secs:
        lvl = "H2" if s.level == 2 else "H3"
        print(f"| {s.id} | {s.title} | {lvl} | 待确认 | {PRD_PATH}:{s.line} |")
    print(f"\n共 {len(secs)} 节（"
          f"{sum(1 for s in secs if s.level == 2)} 个 H2 / "
          f"{sum(1 for s in secs if s.level == 3)} 个 H3）")


def _print_ledger() -> int:
    prd = ROOT / PRD_PATH
    ledger = ROOT / LEDGER_PATH
    print(f"\n{'─' * 88}")
    print("台账完整性检查")
    print("─" * 88)
    if not prd.exists():
        print(f"  ❌ 找不到 {PRD_PATH} —— 需求基线缺失，无法对账")
        return 2
    if not ledger.exists():
        print(f"  ❌ 找不到 {LEDGER_PATH} —— 变更台账缺失，无法对账")
        return 2
    issues = validate_ledger(
        prd.read_text(encoding="utf-8-sig", errors="replace"),
        ledger.read_text(encoding="utf-8-sig", errors="replace"))
    if not issues:
        print(f"  ✅ 台账 {LEDGER_PATH} 通过（章节全覆盖 / 枚举合法 / "
              "证据可复跑 / 新增与冲突都已落到 PRD）")
        return 0
    for i in issues:
        print(f"  [{i.level}] {i.where}：{i.detail}")
        if i.fix:
            print(f"          → {i.fix}")
    errs = sum(1 for i in issues if i.level == "ERROR")
    print(f"\n  ❌ {errs} 个 ERROR —— 台账未达标（这就是「需求与 PRD 的出入」没落账）")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="需求 ↔ 开发 PRD 文档对账（每轮对话都要跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  uv run python scripts/prd_sync_check.py --keyword 数据索引\n"
            "  uv run python scripts/prd_sync_check.py --ledger\n"
            "  uv run python scripts/prd_sync_check.py --list-sections\n"
            "  uv run python scripts/prd_sync_check.py --self-test\n"))
    ap.add_argument("--keyword", default="",
                    help="本轮用户需求的关键词（可逗号分隔多个）")
    ap.add_argument("--ledger", action="store_true",
                    help="校验台账完整性（默认动作）")
    ap.add_argument("--list-sections", action="store_true",
                    help="打印 PRD 章节 → ID 映射表")
    ap.add_argument("--self-test", action="store_true",
                    help="自证：喂坏输入必须报错")
    args = ap.parse_args()

    if args.self_test:
        fails = self_test()
        if fails:
            print("❌ 自证失败（检查器本身不可信，先修它）：")
            for f in fails:
                print(f"   · {f}")
            return 1
        print("✅ 自证通过：已知缺失 / 幻影章节 / 失效证据 / 未落 PRD 的闭环 / "
              "假闭环，五类坏输入都能报出来")
        return 0

    if args.list_sections:
        _print_sections()
        return 0

    rc = 0
    if args.keyword:
        for kw in [k.strip() for k in args.keyword.split(",") if k.strip()]:
            rep = scan_keyword(kw)
            _print_keyword(rep)
            if rep.verdict in ("MISS_PRD", "MISS_LEDGER"):
                rc = 1
    if args.ledger or not args.keyword:
        rc = max(rc, _print_ledger())

    print(f"\n{'=' * 88}")
    print("对账纪律（来自 .trae/skills/requirement-prd-sync）")
    print("=" * 88)
    print(f"""
  1. **用户输入即需求**：把本轮输入拆成条目，逐条判 新增 / 冲突 / 细化 / 已覆盖。
  2. **没有的补充进去**：仓库做了而 PRD 没有 → 补写 {PRD_PATH}。
  3. **有差异的改动**：口径冲突 → 改写 PRD，**旧口径标注废止并留 CHG 号**，不许直接删。
  4. **落 PRD 才算闭环**：只登台账不改 PRD 的条目，状态必须停在 待办。
  5. **不许静默**：查不到就报 TODO（没量到 ≠ 量到 0），人工判断后再落账。
""")
    return rc


if __name__ == "__main__":
    sys.exit(main())
