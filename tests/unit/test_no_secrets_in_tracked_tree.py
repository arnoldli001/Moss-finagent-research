"""**判据：已跟踪的文件里不得有未脱敏的凭据**（2026-10-02，CHG-0159）。

## 这条判据为什么存在

2026-10-02 实测：仓库 `arnoldli001/Moss-finagent-research` 是 **public**，
而 `scripts/_verify_admin_e2e.py` 的 docstring 里躺着一句真实开发管理员口令
`--password 'Z9u1KUb…'`（提交 `3c7c43d`，2026-09-26 就在了；**此处已脱敏** ——
写全值会被本文件自己的 `I_revoked_value` 判据抓住）。
同一轮全树扫描还查出 **9 个**硬编码在探针脚本里的明文口令，以及
**1 个真实知识星球 group_id**（写进了 3 个文件）。

它躲过当时的两处判据，靠的是两个**各自独立**的口子：

1. **范围**：CI 的「敏感信息扫描」只扫 `.env.example` + 根目录 `*.yaml`；
   `tests/unit/test_compliance_gate.py` 只扫 `src/**/*.py`。`scripts/` 从没被扫过。
2. **形态**：两处都只认 `键=值` / `键: 值`，不认命令行参数形态 `--password 值`。

所以本判据扫的是**「被 git 跟踪的文件」这个集合本身**（而不是"某个目录"），
规则本体在 `src/core/secret_scan.py`（单一所有者，判据与 CI 共用一份）。

## 它拦不住什么（写在明处，避免读的人以为它是万能的）

- **历史**：修复只让"当前这份"干净，已推送的旧提交里那个 blob 还在。
  要清历史必须 `git filter-repo` + 强推（需要人工决策）。
- **工作区 vs HEAD**：本判据读**工作区**，所以文件改完还没提交时它是绿的，
  而 GitHub 上仍是旧的。这是刻意的：本项目长期连续几天不提交，若判据盯着
  HEAD 就会长期常红、结局必然是被关掉。HEAD 侧由
  `scripts/_audit_tracked_secrets.py`（交付前核对）与 CI（提交后拦截）覆盖。
- **非文本文件**：`.xlsx`/`.db`/图片按后缀跳过（5 MiB 以上也跳过）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.core import secret_scan

ROOT = Path(__file__).resolve().parents[2]


def _git_available() -> bool:
    return shutil.which("git") is not None


@pytest.fixture(scope="module")
def tracked_scan() -> secret_scan.ScanResult:
    if not _git_available():
        pytest.skip("环境里没有 git —— 无法枚举'已跟踪集合'，本判据不适用")
    try:
        subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(ROOT),
                       capture_output=True, check=True)
    except Exception:  # noqa: BLE001
        pytest.skip("当前目录不是 git 工作区（例如从源码包运行）")
    return secret_scan.scan_tracked_tree(ROOT, source="worktree")


def test_tracked_tree_has_no_unredacted_secrets(
        tracked_scan: secret_scan.ScanResult) -> None:
    """**红线**：已跟踪集合里不得出现未脱敏的凭据 / 数据源标识 / PII。

    失败时打印**每一处**命中（路径:行号 + 命中串），便于直接定位修改。
    """
    assert tracked_scan.scanned_files > 200, (
        f"只扫到 {tracked_scan.scanned_files} 个文件，明显不对劲 —— "
        f"判据可能已经失效（假绿），先修判据再谈结果")
    assert not tracked_scan.findings, (
        f"已跟踪集合里发现 {len(tracked_scan.findings)} 处未脱敏内容"
        f"（公开仓库 = 全世界可见）：\n  "
        + "\n  ".join(f.render() for f in tracked_scan.findings[:30])
        + "\n\n处理方式：改成环境变量 / 占位符；确属假值请在 "
          "configs/secret_scan_allowlist.yaml 登记并写明理由。")


def test_allowlist_has_no_stale_entries(
        tracked_scan: secret_scan.ScanResult) -> None:
    """**棘轮**：允许清单的条目必须真的压住命中，压不住就是历史残留。

    没有这条，豁免清单只会单调变长 —— 最后没人知道哪些还在起作用。
    """
    assert not tracked_scan.stale_allowlist, (
        "允许清单里有已失效的条目，请删除：\n  "
        + "\n  ".join(tracked_scan.stale_allowlist))


def test_path_exemptions_stay_bounded(
        tracked_scan: secret_scan.ScanResult) -> None:
    """**棘轮**：路径豁免放行的命中数不得超过当前基线。

    路径豁免是"整类形态 + 整个目录"，最容易变成看不见的假绿。基线写死在这里：
    要放宽必须改这个数字 —— 而改数字会在 code review 里被看见。
    当前基线 46 来自 **2026-10-05** 实测，构成（全部在 `tests/**` 与 1 个探针记录）：
      · 测试**自己创建**的账号口令与"证明检测器会红"的样例行
        （随用例临时存在，不是任何真实账号的凭据）；
      · 假手机号夹具（`13812345678`）与行情字段里形似手机号的 11 位数字
        （成交量 `18992416600`），实测全部是误报；
      · 1 条来自检测器自证用例文件（整文件豁免，见 EXEMPT_PATH_RULES）。

    ⚠️ **40 → 46 的来历（必须写清楚，否则调基线就是"把红灯调绿"）**：
    本文件原先**从未 `git add`**（`CHG-0164` 那轮才入库），而判据扫的是
    "**已跟踪集合**" ⇒ 上面那 6 条夹具（本文件自己的 `PASSWORD = "…"` 样例行）
    **从来没被算进来过**。入库后第一次全量：40 → 46，逐文件核对确认
    新增的 6 条**全部来自本文件自身**，其余 40 条与 2026-10-02 基线逐条一致
    （`exempted` 是按路径分布：test_auth_service 14 / test_compliance_gate 11 /
    本文件 6 / admin_routes 3 / auth_routes 3 / 其余各 1）。
    所以这不是"放宽"，是**把一直没被看见的那部分如实计入**。

    注意：**已撤回凭据（I_revoked_value）不吃任何豁免** —— 见下一条用例。
    """
    EXEMPTED_BASELINE = 46
    n = len(tracked_scan.exempted)
    assert n <= EXEMPTED_BASELINE, (
        f"路径豁免放行了 {n} 条命中，超过基线 {EXEMPTED_BASELINE}：\n  "
        + "\n  ".join(f.render() for f in tracked_scan.exempted[:30])
        + "\n\n要么把值改成明显的假值，要么在 EXEMPT_PATH_RULES 里写明为什么"
          "整个目录可以豁免，要么（最后手段）上调本基线并在台账里说明。")


def test_revoked_values_are_gone_everywhere(tracked_scan: secret_scan.ScanResult,
                                            ) -> None:
    """**撤回清单**：已经泄漏过的具体凭据值，在任何文件里都不许再出现。

    这条与上面"形态扫描"是**两种不同的判据**，缺一不可：
      · 形态扫描问的是"这像不像一个凭据"——它必须靠键名或语境，因此
        实测漏掉过 2 处**裸字面量**（`pg.fill(…, "MossDev…")` 与 docstring 里
        提到的那个口令；两处值均**已脱敏**）；
      · 撤回清单问的是"这是不是**已经泄漏过的那几个值**"——精确字符串比对，
        零误报，且**不吃任何路径豁免**（测试夹具里同样不许抄真口令）。

    轮换口令后，旧值请加进 `configs/revoked_secrets.yaml`（只存哈希）。
    """
    revoked = [f for f in tracked_scan.findings
               if f.shape == "I_revoked_value"]
    assert not revoked, (
        f"发现 {len(revoked)} 处**已撤回凭据**重新出现在已跟踪文件里：\n  "
        + "\n  ".join(f.render() for f in revoked[:20])
        + "\n\n这些值已经公开泄漏过、必须视为永久作废；"
          "任何位置（含测试）都不许再抄。")


def test_revoked_scan_itself_can_still_fail() -> None:
    """**自证**：撤回清单比对必须真的会红（否则它只是装饰）。"""
    table = secret_scan.load_revoked()
    assert table, (
        "撤回清单为空 —— 2026-10-02 实测有 13 个值必须永久作废，"
        "空清单意味着这条判据什么都没在管")
    # 取一个已知撤回值（哈希反查不到明文，这里用生成脚本来验证机制）
    import hashlib

    probe = "RevocationProbe#2026x"
    digest = hashlib.sha256(probe.encode("utf-8")).hexdigest()[:16]
    fake = {digest: "自证样例"}
    hits = secret_scan.scan_revoked(f'x = "{probe}"', path="tests/x.py",
                                    revoked=fake)
    assert [h.shape for h in hits] == ["I_revoked_value"], (
        f"撤回清单没抓住已知值：{hits}")
    # 而且它不吃路径豁免：同一个值出现在 tests/ 下同样算违规
    tree_hit = secret_scan.scan_revoked(f'PWD = "{probe}"', path="tests/x.py",
                                        revoked=fake)
    assert tree_hit, "撤回清单被路径豁免吃掉了 —— 那它就不叫'永远不许出现'"
    """**自证**：判据的检测器必须真的会红。

    没有这条，上面几条断言可能只是"永远为真"的空断言（安全幻觉）。
    这里用**本次真实泄漏的那一行**做红例。
    """
    # 本次真实泄漏的形态（值已改写为假值，形态与真实泄漏完全一致）
    cli = "python x.py --admin admin --password 'Zq7xKUb9t08asT4w'"
    hits = secret_scan.scan_text(cli, path="scripts/_verify_admin_e2e.py")
    assert "C_cli_flag" in [h.shape for h in hits], (
        f"命令行参数形态的口令没被抓住 —— 检测器失效：{hits}")

    kv = 'PWD = "MossFix2027z"'
    hits = secret_scan.scan_text(kv, path="scripts/_dump_tabs.py")
    # 同一字面量可以同时命中多条形态（B_kv 与 B2_tuple）；去重发生在
    # `scan_items` 层，这里只要求"至少有一条抓住它"。
    assert "B_kv" in [h.shape for h in hits], f"键值形态没被抓住：{hits}"

    gid = 'group_id=48848484411449'
    hits = secret_scan.scan_text(gid, path="docs/x.md")
    assert [h.shape for h in hits] == ["F_source_id"], f"数据源标识没被抓住：{hits}"

    # 无引号形态（D）：值后面紧跟 `")` 这种标点也必须抓住
    export = '    "export MOSS_TOKEN=Zq7xKUb9t08asT4w")'
    hits = secret_scan.scan_text(export, path="scripts/x.py")
    assert [h.shape for h in hits] == ["D_shell"], f"shell 导出形态没被抓住：{hits}"

    # 反向：正确写法与占位符**不许**被判违规（误报会把门禁逼死）
    for ok in ('--password "$MOSS_VERIFY_PWD"',
               'pwd = os.environ.get("MOSS_DUMP_TABS_PWD", "")',
               "password=body.password",
               "token=view.token",
               'password: "***"',
               "group_id=12345678901234"):
        assert not secret_scan.scan_text(ok, path="scripts/x.py"), (
            f"误报：{ok!r} 被判成违规 —— 误报会让门禁被关掉")
