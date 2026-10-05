"""存储登记的**治理口径**判据：同一物理文件的三条登记必须有结构与权威（`CHG-0144`）。

## 为什么需要这个文件（一次"看起来是三个库"的现场）

`configs/data_stores.yaml` 里 `app_db` / `legacy_main` / `crowding_shared`
**三条登记指向同一个物理文件** `data/moss_finagent.db`
（`app_db` 只在未注入 `MOSS_SQLITE_PATH` 时经 `main_path` 回落到它）。实测代价：

* `ColumnIndex` 把该库每张表扫 **3 次**（191 行资产里 48 个 `asset_id` 重复；
  多路径列虚报成 345，去重后只有 119）；
* 同一个文件的 `protected` 一处 `true` 一处 `false`、
  `writer` 一处 `main` 一处 `dev` —— **各写一份，互相矛盾而没人发现**
  （`scripts/_audit_store_governance.py` 原先正好报这 2 处）。

## 用户的裁定（本文件把它机器化）

> 「同路径先看下线上展示的数据取自谁，谁就更权威，应该是 legacy_main」

判据不是"谁的 note 写得更全"，而是**线上展示实际取谁**，实测（2026-09-30）：

| 面 | 读哪个文件 | 用什么 key |
|---|---|---|
| pilot（对外实例）的 `fact_data_points` / `fact_alerts` / `mainline_alert` / `sector_crowding_list` | `data/pilot/moss_pilot.db` | `app_db` |
| pilot 的 `sector_crowding_daily` / `sector_crowding_max_ma5`（前端拥挤度看板） | `data/moss_finagent.db` | **`legacy_main`** |
| 主实例 / 离线脚本的应用库 | `data/moss_finagent.db` | `app_db`（`main_path` 回落） |

`manage.py` 三档隔离注入的是 `data/<env>/moss_<env>.db`（:683 / :741 / :1161），
所以"线上展示落回 `data/moss_finagent.db`"这条路**只**经 `legacy_main` 这个 key
（`platform_data_connector.py:258` / `:271` 的首选候选）。

## 本文件钉住什么

| 判据 | 防的是什么失效 |
|---|---|
| ① 同路径要么指向同一个 canonical、要么 `protected` 一致 | 同一个文件"既受保护又不保护"，清理口径据此误删 |
| ② `alias_of` 目标必须存在 + canonical 唯一 | 指向幽灵条目；一个文件两个"权威"（等于没有权威） |
| ③ `writer` 不同必须有 `writer_scope` | 两处各留一个裸字段，"谁写哪张表"没人说得清 |
| ④ 字段类型/枚举校验 | `protected: "false"` 被 `bool()` 读成 **True** —— 坏配置静默放行 |
| ⑤ 审计脚本口径同步且退出码 0 | 判据绿、脚本红（或反之）的双份口径 |
| ⑥ 上面的判据**会红**（自证） | 判据太严/太松而永远绿 —— 最隐蔽的假绿 |
| ⑦ canonical 就是**线上首选**的那个 key | "权威"标签与实际取数脱钩 |

⚠️ 本文件只读原始 YAML（不经 `data_stores.Store`）—— `Store` 不认识
`canonical_for_file` / `alias_of` / `writer_scope`，所以治理面**零运行期行为变更**。
真实解析路径由 `tests/unit/test_store_registry.py` 负责钉住。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "data_stores.yaml"
AUDIT_SCRIPT = ROOT / "scripts" / "_audit_store_governance.py"

#: 已知的同物理文件现场（本判据存在的理由，也是"判据不是空转"的证据）。
SAME_FILE_PATH = "data/moss_finagent.db"
EXPECTED_CANONICAL = "legacy_main"


def _load_audit_module():
    """把审计脚本当模块加载 —— **口径只有一份实现**，判据不另抄一遍。

    为什么不用 subprocess 读它的人话输出：那份输出是给人看的，格式随时会改；
    判据要的是**同一份规则函数**。subprocess 那条路另有判据 ⑤ 单独走一遍
    （真实路径：退出码 + `--json`）。
    """
    spec = importlib.util.spec_from_file_location("_audit_store_governance", AUDIT_SCRIPT)
    assert spec and spec.loader, f"加载不了审计脚本：{AUDIT_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load_audit_module()
ROWS: list[dict] = audit.load_rows(CONFIG)
BY_NAME: dict[str, dict] = {str(r.get("name")): r for r in ROWS}


def _row(name: str, **kw: Any) -> dict:
    """构造一条合成登记项（自证用）—— 只关心治理字段。"""
    base: dict[str, Any] = {"name": name, "kind": "sqlite", "path": f"data/{name}.db",
                            "isolation": "shared", "writer": "main",
                            "writable": True, "protected": True, "role": "source"}
    base.update(kw)
    return base


def _groups(rows: list[dict]) -> dict[str, dict]:
    """按审计脚本的同一口径分组（`main_path` 也算同路径）。"""
    return {g["path"]: g for g in audit.evaluate(rows)["groups"]}


def _primary(rows: list[dict], path: str) -> dict[str, dict]:
    return {str(r["name"]): r for r in rows
            if r.get("path") and audit.norm_path(r["path"]) == path}


def _write_variant(tmp_path: Path, mutate: Callable[[dict], None]) -> Path:
    """把**真实配置**改坏一份写到临时目录（自证用；绝不改仓库里的那份）。"""
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    mutate(raw)
    out = tmp_path / "data_stores_variant.yaml"
    out.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    return out


def _run_audit(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(AUDIT_SCRIPT), *args],
                          cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _audit_json(config: Path | None = None) -> tuple[int, dict]:
    args = ["--json"] + (["--config", str(config)] if config else [])
    proc = _run_audit(*args)
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return proc.returncode, payload


# ======================================================================
# 判据 ① 同一路径：要么同一个 canonical，要么 protected 一致
# ======================================================================

def test_same_path_members_point_at_one_canonical_or_agree_on_protected() -> None:
    """★ 用户裁定的机器化：同一物理文件**只许有一个权威**，`protected` 只许一处声明。

    两种合法形态（都必须显式声明，不许"看起来是两个库"）：

      · `alias_of: <canonical>` —— 就是同一份文件的另一张表范围视图，
        文件级属性**继承**，因此本条**不得**再写一份 `protected`；
      · 不是别名 ⇒ 必须自己写 `protected` 且与 canonical **一致**。
    """
    groups = _groups(ROWS)
    assert SAME_FILE_PATH in groups, (
        f"配置里应当至少有一组同物理文件的多条登记（{SAME_FILE_PATH}）；"
        f"实际分组：{sorted(groups)} —— 分组口径是不是坏了？")

    for path, group in groups.items():
        canonical = group["canonical"]
        assert canonical, (
            f"{path} 没有 canonical 声明点（canonical_for_file 恰好一条）—— "
            f"同路径 {group['members']} 的口径没人负责")
        canon_row = BY_NAME[canonical]
        members = _primary(ROWS, path)
        for name, row in members.items():
            if name == canonical:
                continue
            if row.get("alias_of") == canonical:
                assert "protected" not in row, (
                    f"{name} 已经用 alias_of 声明了「同一份文件」，却又写了一份 "
                    f"protected: {row.get('protected')!r} —— 同一个文件不能既受保护"
                    f"又不保护；文件级属性只在 canonical（{canonical}）上声明一次")
                continue
            assert row.get("protected") == canon_row.get("protected"), (
                f"{name} 既不是 {canonical} 的别名，protected 又与它不一致："
                f"{row.get('protected')!r} vs {canon_row.get('protected')!r}")
        # 反向也钉住：同路径上的 protected 声明点必须**恰好**是 canonical 一条。
        declared = sorted(n for n, r in members.items() if "protected" in r)
        assert declared == [canonical], (
            f"{path} 的 protected 声明点是 {declared}，应当只有 canonical "
            f"（{canonical}）一条 —— 多一个声明点就多一个矛盾源")


# ======================================================================
# 判据 ② alias_of 目标必须存在 + canonical 唯一
# ======================================================================

def test_relation_targets_exist_and_canonical_is_unique() -> None:
    """`alias_of` / `main_path_alias_of` 必须指向**存在**的登记；canonical 唯一。"""
    names = set(BY_NAME)
    for row in ROWS:
        name = str(row.get("name"))
        for field in ("alias_of", "main_path_alias_of"):
            target = row.get(field)
            if target is None:
                continue
            assert isinstance(target, str) and target.strip(), (
                f"{name}.{field} 必须是登记名，实际 {target!r}")
            assert target in names, (
                f"{name}.{field} = {target!r} 指向**不存在**的登记（幽灵）—— "
                f"已登记：{sorted(names)[:8]}…")
            assert target != name, f"{name}.{field} 自指（{target!r}）不成关系"

    per_path: dict[str, list[str]] = defaultdict(list)
    for row in ROWS:
        if row.get("canonical_for_file") is True:
            per_path[audit.norm_path(row["path"])].append(str(row["name"]))
    for path, owners in per_path.items():
        assert len(owners) == 1, (
            f"{path} 上有 {len(owners)} 条 canonical 声明（{owners}）—— "
            f"一个路径不许两个「权威」，那等于没有权威")

    for path, group in _groups(ROWS).items():
        assert group["canonical"] in per_path.get(path, []), (
            f"{path} 有多条登记，却没有一条 canonical_for_file: true —— "
            f"「哪条是权威」这件事没有任何声明点")


# ======================================================================
# 判据 ② 的姊妹条：`main_path` 回落必须声明关系、且只在 per_env 上生效
# ======================================================================

def test_main_path_fallback_inherits_canonical_protection() -> None:
    """`app_db` 的 `main_path` 回落：**声明**同文件关系，保护口径由 canonical 承担。

    ## 口径（这是"不改变运行期行为"与"不许含糊"同时成立的那个点）

    `app_db.protected: false` **只描述它自己的 `path`**
    （`data/<env>/moss_<env>.db` 那三个文件确实不受保护）；一旦回落到
    `main_path`（`data/moss_finagent.db`），那个文件受不受保护由它的 canonical
    （`legacy_main.protected: true`）说了算 —— 所以两个值不同**不是**矛盾，
    但**必须**由 `main_path_alias_of` 把这件事说清楚。裸写一个 `main_path`
    就是"看起来是另一个库"。

    ## 机械口径（代码事实）

    `Store.resolved()` 只在 `isolation == "per_env"` 时才看 `main_path`
    （src/infrastructure/catalog/data_stores.py:106-108）—— 所以在别的 isolation
    上写 `main_path` 是一条**永不生效的谎话**，本判据当场报错。
    """
    from src.infrastructure.catalog import data_stores as ds

    seen = 0
    for row in ROWS:
        if not row.get("main_path"):
            continue
        name = str(row["name"])
        assert row.get("isolation") == "per_env", (
            f"{name} 的 isolation={row.get('isolation')!r} 却写了 main_path —— "
            f"`Store.resolved()` 只在 per_env 下读它，这条声明永不生效")
        assert str(row["main_path"]).strip(), f"{name}.main_path 是空串"
        seen += 1
    assert seen >= 1, "本机配置里应有 per_env + main_path 的现场（app_db）"

    groups = _groups(ROWS)
    fallback_groups = {p: g for p, g in groups.items() if g["fallbacks"]}
    assert fallback_groups, (
        f"审计口径没把 `main_path` 算进同路径判定：{ {p: g for p, g in groups.items()} }")
    for path, group in fallback_groups.items():
        canonical = group["canonical"]
        canon_row = BY_NAME[canonical]
        # 该文件的保护口径**只有一个声明点** = canonical（回落到它时生效的就是这份）。
        assert "protected" in canon_row, (
            f"{canonical} 是 {path} 的权威，却没声明 protected —— "
            f"那这个文件受不受保护就没有答案了")
        for name in group["fallbacks"]:
            row = BY_NAME[name]
            assert row.get("main_path_alias_of") == canonical, (
                f"{name}.main_path = {path} 与 canonical {canonical} 是同一个文件，"
                f"却没声明 main_path_alias_of —— 回落到同一份这件事没人说")
    # 运行期一致性：清单里声明的 `main_path` 必须真的被 `Store` 读到
    # （否则这条判据只是在读自己构造的字符串）。
    stored = ds.get_store("app_db")
    assert (stored.isolation, stored.main_path) == ("per_env", SAME_FILE_PATH), (
        f"`Store` 读到的 app_db 与清单声明不一致：isolation={stored.isolation!r} "
        f"main_path={stored.main_path!r}")


# ======================================================================
# 判据 ③ writer 差异必须有范围解释（裸差异即红）
# ======================================================================

def test_writer_difference_must_be_explained_by_scope() -> None:
    """同一物理文件上 `writer` 不同时，**每个**成员都要写 `writer_scope` 且互不相同。"""
    groups = _groups(ROWS)
    scope_seen = 0
    for path, group in groups.items():
        members = _primary(ROWS, path)
        writers = {n: r.get("writer") for n, r in members.items()}
        if len(set(writers.values())) <= 1:
            continue
        scopes: dict[str, str] = {}
        for name, row in members.items():
            scope = str(row.get("writer_scope") or "").strip()
            assert scope, (
                f"{path}: {name}.writer={row.get('writer')!r} 与同路径其它登记不同，"
                f"却没写 writer_scope —— 两处各留一个裸字段，"
                f"「谁写哪张表」没人说得清")
            scopes[name] = scope
            scope_seen += 1
        assert len(set(scopes.values())) == len(scopes), (
            f"{path} 的 writer_scope 有重复：{scopes} —— 范围必须互不相同"
            f"（否则同一批表有两个写者）")
    assert scope_seen >= 2, (
        f"本机配置里应有 writer 差异 + 范围解释的现场（实测 {scope_seen} 处）")


# ======================================================================
# 判据 ④ 类型 / 枚举校验：坏配置要**响亮**
# ======================================================================

def test_governance_field_types_are_validated_loudly() -> None:
    """真实配置必须零类型错；且**坏配置会被抓出来**（不是被默默解释）。"""
    assert audit.check_types(ROWS) == [], audit.check_types(ROWS)

    # ★ 最贵的那个陷阱：`protected: "false"` 是字符串，而 `Store` 侧是
    #   `bool(item.get("protected", False))` ⇒ 读成 **True**。
    #   一个不受保护的库会被显示成"受保护"，或反之 —— 清理口径据此走错。
    bad = audit.check_types([_row("probe", protected="false")])
    assert any("protected" in e and "bool" in e for e in bad), bad
    bad = audit.check_types([_row("probe", writable=1)])
    assert any("writable" in e for e in bad), bad
    # 枚举
    assert any("writer" in e for e in audit.check_types([_row("probe", writer="prod")]))
    assert any("kind" in e for e in audit.check_types([_row("probe", kind="database")]))
    assert any("role" in e for e in audit.check_types([_row("probe", role="whatever")]))
    assert any("isolation" in e
               for e in audit.check_types([_row("probe", isolation="per-env")]))
    # canonical_for_file 只许 true —— 写成 false 会让"权威"变成可静默关闭的开关
    bad = audit.check_types([_row("probe", canonical_for_file=False)])
    assert any("canonical_for_file" in e for e in bad), bad
    # 重名（`Store` 侧会直接抛 ValueError）
    bad = audit.check_types([_row("probe"), _row("probe", path="data/other.db")])
    assert any("重名" in e for e in bad), bad


# ======================================================================
# 判据 ⑤ 审计脚本口径同步 + 真实配置退出码 0
# ======================================================================

def test_audit_script_exits_zero_and_counts_main_path_as_same_file() -> None:
    """`scripts/_audit_store_governance.py` 必须**跟着改口径**并在真实配置上退出 0。

    两个方向都钉：

      · `main_path` 也算同路径（`app_db` 必须进这一组）；
      · `alias_of` 认成"同一份"（`crowding_shared` 与 `legacy_main` 同组）；
      · 权威必须是用户裁定的那一条（`legacy_main`）。
    """
    assert AUDIT_SCRIPT.exists(), AUDIT_SCRIPT
    code, payload = _audit_json()
    assert code == 0, f"审计脚本在真实配置上退出 {code}：{payload}"
    assert payload["errors"] == [], payload["errors"]

    group = next((g for g in payload["groups"] if g["path"] == SAME_FILE_PATH), None)
    assert group is not None, f"审计脚本没把 {SAME_FILE_PATH} 分为一组：{payload}"
    assert set(group["members"]) == {"legacy_main", "crowding_shared"}, group
    assert group["fallbacks"] == ["app_db"], (
        f"`main_path` 没被算进同路径判定：{group} —— 三条登记指向同一文件这件事"
        f"就少了一条")
    assert group["canonical"] == EXPECTED_CANONICAL, group
    assert group["protected"] is True, group

    human = _run_audit()
    assert human.returncode == 0, human.stdout + human.stderr
    assert "app_db" in human.stdout and SAME_FILE_PATH in human.stdout, human.stdout


# ======================================================================
# 判据 ⑥ 自证：判据必须真的会红（假护栏比没有护栏更糟）
# ======================================================================

def test_self_proof_missing_canonical_marker_is_red(tmp_path: Path) -> None:
    """去掉 `legacy_main` 的权威标记 ⇒ ①/② 必红（**走真实脚本**，不是模拟）。"""
    def mutate(raw: dict) -> None:
        for item in raw["stores"]:
            if item.get("name") == EXPECTED_CANONICAL:
                item.pop("canonical_for_file", None)

    variant = _write_variant(tmp_path, mutate)
    rows = audit.load_rows(variant)
    errors = audit.evaluate(rows)["errors"]
    assert any("canonical" in e for e in errors), errors
    assert not _groups(rows)[SAME_FILE_PATH]["canonical"], "去掉标记后不该还有权威"

    code, payload = _audit_json(variant)
    assert code == 1, payload
    assert any("canonical" in e for e in payload["errors"]), payload["errors"]


def test_self_proof_alias_to_ghost_name_is_red(tmp_path: Path) -> None:
    """`alias_of` 指向不存在的名字 ⇒ ② 必红（防指向幽灵）。"""
    def mutate(raw: dict) -> None:
        for item in raw["stores"]:
            if item.get("name") == "crowding_shared":
                item["alias_of"] = "no_such_store"

    variant = _write_variant(tmp_path, mutate)
    code, payload = _audit_json(variant)
    assert code == 1, payload
    assert any("alias_of" in e and "不存在" in e for e in payload["errors"]), payload


def test_self_proof_two_canonicals_on_one_path_is_red(tmp_path: Path) -> None:
    """一个路径两条"权威" ⇒ ② 必红（canonical 唯一）。

    ★ 这条自证**不依赖基础配置是否健康**：它把两条都显式置为 true，
    所以无论仓库里那份配置当下是什么状态，变体里都恰好是"两条权威"。
    """
    def mutate(raw: dict) -> None:
        for item in raw["stores"]:
            if item.get("name") in (EXPECTED_CANONICAL, "crowding_shared"):
                item["canonical_for_file"] = True
            if item.get("name") == "crowding_shared":
                item.pop("alias_of", None)

    variant = _write_variant(tmp_path, mutate)
    rows = audit.load_rows(variant)
    owners = [str(r["name"]) for r in rows if r.get("canonical_for_file") is True]
    assert len(owners) == 2, f"自证前提没造出来（canonical 声明 {owners}）"
    code, payload = _audit_json(variant)
    assert code == 1, payload
    assert any("恰好一条" in e for e in payload["errors"]), payload


def test_self_proof_bare_writer_difference_is_red(tmp_path: Path) -> None:
    """把 `writer_scope` 摘掉、只留两个不同的 `writer` ⇒ ③ 必红（裸差异）。"""
    def mutate(raw: dict) -> None:
        for item in raw["stores"]:
            if item.get("name") in ("legacy_main", "crowding_shared"):
                item.pop("writer_scope", None)

    variant = _write_variant(tmp_path, mutate)
    code, payload = _audit_json(variant)
    assert code == 1, payload
    assert any("writer_scope" in e for e in payload["errors"]), payload


def test_self_proof_second_protected_declaration_is_red(tmp_path: Path) -> None:
    """在别名条目上再写一份 `protected` ⇒ ① 必红（同一个文件两个口径）。"""
    def mutate(raw: dict) -> None:
        for item in raw["stores"]:
            if item.get("name") == "crowding_shared":
                item["protected"] = True

    variant = _write_variant(tmp_path, mutate)
    code, payload = _audit_json(variant)
    assert code == 1, payload
    assert any("protected" in e for e in payload["errors"]), payload


# ======================================================================
# 判据 ⑦ canonical 必须是**线上展示首选**的那个 key
# ======================================================================

def test_canonical_is_the_store_key_the_display_face_reads_first() -> None:
    """★ 把用户裁定的**判据本身**机器化：权威 = 线上展示实际首选取谁。

    代码事实：前端的概念/行业拥挤度看板走 `platform_data_connector`，
    它按 `_TABLE_CANDIDATES[表][0]` 选存储（`_table_store()` 取第一个存在该表的
    候选，见 platform_data_connector.py:560-578）。所以本判据断言：

        指向某个物理文件的**首选**候选里，必须有一条就是该文件的 canonical。

    它是一道**棘轮**：谁把首选候选换掉（等于把"线上实际取谁"改了），
    这条就红，逼着人重新裁定 canonical —— 而不是让 `canonical_for_file`
    这个标签与实际取数悄悄脱钩。
    """
    from src.infrastructure.connectors import platform_data_connector as pdc

    groups = _groups(ROWS)
    first_users: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for table, candidates in pdc._TABLE_CANDIDATES.items():
        assert candidates, f"{table} 没有候选存储"
        row = BY_NAME.get(str(candidates[0]))
        assert row is not None, (
            f"{table} 的首选候选 {candidates[0]!r} 不在登记表里 —— "
            f"线上取的是一条未登记的存储")
        keys = {audit.norm_path(v) for v in (row.get("path"), row.get("main_path"))
                if v}
        for key in keys & set(groups):
            first_users[key][str(candidates[0])].append(table)

    assert SAME_FILE_PATH in first_users, (
        f"没有任何一张表的**首选**存储落在 {SAME_FILE_PATH} —— "
        f"那说明线上展示已经不从那里取数，canonical 该重新裁定了")
    for path, users in first_users.items():
        canonical = _groups(ROWS)[path]["canonical"]
        assert canonical in users, (
            f"{path} 的 canonical={canonical!r} 不是任何一张表的线上首选存储 —— "
            f"权威标签与实际取数脱钩。实测线上首选："
            f"{ {n: sorted(t) for n, t in users.items()} }")
    # 现场证据：拥挤度那两张表的首选必须就是 canonical（用户裁定的依据）。
    for table in ("sector_crowding_daily", "sector_crowding_max_ma5"):
        assert pdc._TABLE_CANDIDATES[table][0] == EXPECTED_CANONICAL, (
            f"{table} 的线上首选不再是 {EXPECTED_CANONICAL} —— "
            f"用户裁定的依据变了，本判据需要重新裁定而不是放宽")


def test_judge_reads_the_raw_yaml_not_the_runtime_dataclass() -> None:
    """治理字段必须**只**由判据读 —— `Store` 不认识它们（零运行期行为变更）。

    为什么钉这条：若哪天把 `canonical_for_file` 塞进 `Store`，它就进了
    `to_dict()` / `/health` / `all_stores()` 的契约，本判据的"零行为变更"承诺
    会静默失效；那时必须**同步**改这条判据与 `test_store_registry.py`。
    """
    import dataclasses

    from src.infrastructure.catalog import data_stores as ds

    field_names = {f.name for f in dataclasses.fields(ds.Store)}
    for key in ("canonical_for_file", "alias_of", "main_path_alias_of", "writer_scope"):
        assert key not in field_names, (
            f"{key} 已经进了 `Store` —— 治理面与运行期契约混在一起了，"
            f"要改就得同步改 test_store_registry.py 与 /health 契约")
    # 而且解析结果**不受**这些字段影响：真实注册表里三条登记仍然指向同一文件。
    resolved = {ds.resolve_store(n).name
                for n in ("app_db", "legacy_main", "crowding_shared")}
    assert resolved == {"moss_finagent.db"}, (
        f"主实例下三条登记必须解析到同一个文件（实测 {resolved}）—— "
        f"这正是不声明关系就会读成「三个库」的那个现场")


@pytest.mark.parametrize("field", ["canonical_for_file", "alias_of", "writer_scope"])
def test_governance_fields_are_documented_in_the_yaml_header(field: str) -> None:
    """治理字段必须写进 `data_stores.yaml` 的字段口径 —— 否则下一个人只能猜。"""
    text = CONFIG.read_text(encoding="utf-8")
    assert field in text, f"{field} 没有出现在清单里"
    head = text.split("stores:", 1)[0]
    assert field in head, (
        f"{field} 只出现在条目里、没写进字段口径说明 —— "
        f"「自解释」的要求是给下一个人看的，不是给解析器看的")
