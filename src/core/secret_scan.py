"""**已跟踪内容的凭据扫描** —— 「哪些东西不该跟着代码出去」的单一所有者。

## 为什么单独一个模块，而不是写在两个地方

本模块出现之前，仓库里有**两处**互相不知道对方存在的判据：

- `tests/unit/test_compliance_gate.py`：只扫 `src/**/*.py`，只认 `键 = "值"`；
- `.github/workflows/ci.yml` 的「敏感信息扫描」：只扫 `.env.example` 与根目录 `*.yaml`。

实测后果（2026-10-02）：`scripts/_verify_admin_e2e.py` 的 docstring 里躺着
一句 `--password 'Z9u1KUb…'`（**已脱敏**：该值在撤回清单里，见下），随仓库**公开**
在 GitHub 上。它同时躲过两处判据，靠的是**两个各自独立的口子**：

1. **范围**：它不在 `src/` 下，也不在 `.env.example` 里 —— 谁都没扫过它；
2. **形态**：它是 `--password 值`（命令行参数），而两处判据只认 `键=值`。

单点修补解决不了这类问题：只把范围扩到全树、形态仍然是 `键=值`，下次换成
`export X=…` 或 `-p 值` 照样漏；只补形态、范围还是 `src/`，`scripts/` 照样漏。
所以这里把「**扫哪些文件**」与「**认哪些形态**」两件事收进同一个所有者，
判据（单测 + CI）只调用本模块，不再各写一份正则。

## 三层判据的分工（刻意不同，不是重复）

| 层 | 扫什么 | 什么时候红 | 为什么这样分 |
|---|---|---|---|
| `test_no_secrets_in_tracked_tree.py` | 工作区里**被 git 跟踪**的文件 | 改了没提交也红 | 开发者改完立刻要看到反馈 |
| CI「敏感信息扫描」 | CI 检出出来的树（= 已提交内容） | 提交进仓库就红 | 拦住"本地修了但推上去了" |
| `scripts/audit_tracked_secrets.py` | `git archive HEAD`（= GitHub 上那一份） | 只报告，不阻断 | 交付前核对线上真实状态 |

**为什么不把"扫 HEAD"也做成阻断判据**：本项目长期存在"连续几天不提交、几十个
文件在途"的工作方式（实测 45 项未提交），那种判据会长期常红，结局必然是被关掉
—— 而门禁一旦被关掉，等于没有。所以工作区判据负责**改得对**，HEAD 审计负责
**说得准**，CI 负责**拦得住**。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Iterable, Iterator

ROOT: Final = Path(__file__).resolve().parents[2]

#: 允许清单：每条都要写 `why`，且必须真的压住一条命中（见 `ScanResult.stale_allowlist`）。
ALLOWLIST_PATH: Final = ROOT / "configs" / "secret_scan_allowlist.yaml"

#: 占位符/示例词 —— 出现即不判违规。
#:
#: 这条**必须**足够宽：门禁误报会逼着人关掉门禁（`redaction.py` 里同款取舍）。
#: 但**不能宽到把真值吃掉** —— 2026-10-02 实测踩过三个，每一个都让真口令
#: 被静默放行（下面三个样例一律**脱敏**：它们是已撤回凭据，抄进注释就等于
#: 把泄漏再发布一次 —— 本模块自己的判据会当场报 `I_revoked_value`）：
#:   · `probe`：把真口令 `TmpProbe…` 判成了样例；
#:   · `n/?a`：裸写会匹配**任何**含 "na" 的词 —— `MultiTenant…` 里的 "na"
#:     就命中了；
#:   · `placeholder`：HTML 属性名 `input[placeholder="输入密码"]` 会把整行判成样例，
#:     连带同一行的真口令一起放过。
#: 教训：**"放行规则"过宽的后果不是误报，是静默失效** —— 门禁照样全绿。
#: 所以每条都收紧到"只能匹配它真正想放行的东西"。
PLACEHOLDER: Final = re.compile(
    r"(?i)(replace[_-]?me|change[_-]?me|your[_-]?|xxx+|example|"
    r"placeholder(?!\s*[=:])|"
    r"dummy|fake|wrong|invalid|"
    r"probe[_-]?(?:pass|pwd|password|token|secret|key|user|account)|"
    r"sample|redacted|not[_-]?a[_-]?|none|null|"
    r"test[_-]?(key|pwd|pass|token)|\bn/?a\b|todo|tbd|"
    r"\*\*\*|…|\.\.\.|<[^>]*>|\{[^}]*\}|\$[A-Za-z_{]|"
    r"你的|示例|占位|待填|请填|脱敏|掩码|省略|环境变量)"
)


@dataclass(frozen=True)
class Shape:
    """一类"不该出去的东西"。

    `value_group` 是命中串里"值"所在的捕获组序号（0 表示整段都是值；元组表示
    "哪个分支命中就用哪个组"，供 D 这种多分支形态使用）。
    取出来是为了做**值级**判断（像不像一个真值），而不是只做**形态级**匹配
    —— 少了这一步，`password=body.password` 这种表达式会被判成泄漏。

    ⚠️ 踩过的坑（2026-10-02）：B/C 两个形态的**第 1 组是键名、第 2 组才是值**，
    初版把 `value_group` 写成 1，于是"取出来的值"是字符串 `'password'`
    —— 值级判据当场把**所有**真值判成不合格，检测器看似在工作、实际一条都抓不到。
    这正是自证用例（`test_the_scanner_itself_can_still_fail`）存在的理由：
    没有它，这个 bug 会以"门禁全绿"的形态长期潜伏。现在另有 `_value_of()`
    的运行期绊线：取出来的值等于键名就直接抛错。
    """

    name: str
    why: str
    pattern: re.Pattern[str]
    value_group: int | tuple[int, ...] = 0
    #: `True`：值**必须**紧跟键名（取到键名就说明 `value_group` 配错了，当场抛错）。
    #: `False`：形如 `H_ctx_literal` 的"同行共现"形态 —— 取到键名只说明这行不是
    #: 泄漏（如 `"password": string`），跳过即可，抛错反而会误伤。
    strict_value: bool = True


SHAPES: Final[tuple[Shape, ...]] = (
    Shape(
        "A_known_prefix",
        "已知前缀的密钥（sk-/AKIA/ghp_/xox/glpat/私钥头）—— 误报率最低的一类",
        re.compile(
            r"(?:sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|"
            r"ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_\-]{16,}|"
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"),
    ),
    Shape(
        "B_kv",
        "键值形态的**字面量**凭据（password=\"…\"）—— 只认引号包起来的真值",
        re.compile(
            # `(?<![A-Za-z0-9])` 而不是 `\b`：`\b` 让 `admin_pwd = "…"`、
            # `shot_password = "…"` 这类**带前缀的变量名**全都匹配不到
            # （`_` 是词字符，`_pwd` 前没有词边界）。实测漏了 1 处真实口令。
            r"""(?i)(?<![A-Za-z0-9])(password|passwd|pwd|secret|token|"""
            r"""api[_-]?key|apikey|access[_-]?key|private[_-]?key|"""
            r"""auth[_-]?token)[A-Za-z0-9_]*"""
            r"""\s*[:=]\s*["']([^"'\n]{8,})["']"""),
        2,          # 组 1 = 键名，组 2 = 值
    ),
    Shape(
        "B2_tuple",
        "**元组赋值**形态（`USER, PWD = \"admin\", \"真口令\"`）—— 值不在键名后面",
        re.compile(
            # 这一形态漏得最狠：实测 4 处真口令（`_cal_probe.py` /
            # `_login_captcha_check.py` / `_unlock_ui_check.py` / `_cred_ring_check.py`）
            # 全写成 `USER, PWD = "账号", "口令"`，而旧判据只认"键名后紧跟的值"，
            # 于是它取到的是**用户名**（`"admin"`）并因"不像口令"而放行。
            r"""(?i)(?<![A-Za-z0-9])(?:password|passwd|pwd|secret|token|"""
            r"""api[_-]?key|apikey|access[_-]?key)[A-Za-z0-9_]*"""
            r"""\s*[:=]\s*(?:["'][^"'\n]{0,40}["']\s*,\s*)?["']([^"'\n]{8,})["']"""),
        1,
    ),
    Shape(
        "B3_env_default",
        "**环境变量兜底值**形态（`os.environ.get(\"X_PWD\", \"真口令\")`）",
        re.compile(
            # 最危险的一种：既是明文入库，又会在缺环境变量时**静默用真口令去试**
            # —— 实测 `_intel_mobile_check.py` 因此可能把账号锁掉
            # （`_cred_ring_check.py` 的注释里记着 admin 已被推到 failed_attempts 4/5）。
            r"""(?i)(?:environ\s*\.\s*get|getenv)\(\s*["'][A-Za-z0-9_]*"""
            r"""(?:PWD|PASSWORD|PASSWD|SECRET|TOKEN|KEY)["']\s*,\s*"""
            r"""["']([^"'\n]{8,})["']"""),
        1,
    ),
    Shape(
        "B4_ternary_default",
        "**三元兜底值**形态（`sys.argv[1] if … else \"真口令\"`）",
        re.compile(
            r"""(?i)(?<![A-Za-z0-9])(?:password|passwd|pwd|secret|token)"""
            r"""[A-Za-z0-9_]*\s*[:=][^\n]*?\belse\s+["']([^"'\n]{8,})["']"""),
        1,
    ),
    Shape(
        "C_cli_flag",
        "命令行参数形态的凭据（--password 值）—— 真实泄漏就是这一种",
        re.compile(
            r"""(?i)--(password|passwd|pwd|secret|token|api[-_]?key|apikey|"""
            r"""access[-_]?key)\b[\s=]+["']?([^\s"']{6,})"""),
        2,          # 组 1 = 键名，组 2 = 值
    ),
    Shape(
        "D_shell",
        "shell 导出 / URL 内嵌凭据 / curl -u",
        re.compile(
            r"(?i)(?:export\s+\w*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)\w*\s*=\s*"
            r"([^\s]{6,})|curl[^\n]{0,40}\s-u\s+\S+:([^\s]{4,})|"
            r"https?://[^/\s:@]{2,}:([^/\s@]{4,})@)"),
        (1, 2, 3),  # 三个分支各一个组，哪个命中用哪个
    ),
    Shape(
        "E_cjk",
        "中文文档里的口令写法（密码：真值）",
        re.compile(r"(?:密码|口令|密钥|令牌)\s*[:：=]\s*([^\s，。；、）)`\"']{6,})"),
        1,
    ),
    Shape(
        "F_source_id",
        "数据源标识（group_id/topic_id 等可复制资产，见 redaction.py 的取舍说明）",
        re.compile(r"(?i)(?:group_id|gid|topic_id|chat_id)[\"'\s:=]+(\d{8,})"),
        1,
    ),
    Shape(
        "G_pii",
        "手机号 / 身份证号（合规红线）",
        re.compile(
            r"((?<!\d)1[3-9]\d{9}(?!\d)|"
            r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
            r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]\b)"),
        1,
    ),
)

#: 路径级豁免。**每条都必须能说出"为什么这个目录整体可以豁免"**，
#: 且只豁免指定形态 —— 不做"整个文件全豁免"（那等于给文件开天窗）。
#: 唯一的例外是最后一条：那个文件的**全部内容**就是样例值，逐形态豁免等于没豁。
EXEMPT_PATH_RULES: Final[tuple[tuple[str, re.Pattern[str], frozenset[str], str], ...]] = (
    (
        "测试夹具",
        re.compile(r"^tests/"),
        frozenset({"B_kv", "B2_tuple", "B3_env_default", "B4_ternary_default",
                   "C_cli_flag", "D_shell", "G_pii"}),
        "测试必须能写 '一个显然假的口令' 来验登录/校验逻辑，也必须能写"
        "'一行形似泄漏的样例'来证明检测器有效；"
        "但**已知前缀密钥**(A)、**真实数据源标识**(F) 与**已撤回凭据**(I) 不豁免 —— "
        "那几类没有'假'的说法：撤回清单说的是'这个值永远不许再出现'，"
        "与它出现在哪个目录无关。",
    ),
    (
        "探针转储",
        re.compile(r"^docs/_[^/]*\.(txt|json|log)$"),
        frozenset({"G_pii"}),
        "一次性的上游响应转储，正文是接口字段与数字；里面的 11 位数字是成交量/"
        "时间戳而不是手机号（实测 G 类命中全部是行情字段）。"
        "**但 F（真实 group_id）不豁免** —— 转储里那个 ID 是真的（实测 "
        "`docs/_zsxq_api_probe.txt` 就是），按 `redaction.py` 的取舍它属于"
        "可复制资产。这类文件本身也不该留在已跟踪集合里，见 privacy 判据。",
    ),
    (
        "检测器自证用例",
        re.compile(r"^tests/unit/test_no_secrets_in_tracked_tree\.py$"),
        frozenset({"A_known_prefix", "B_kv", "C_cli_flag", "D_shell", "E_cjk",
                   "F_source_id", "G_pii"}),
        "该文件的**全部内容**就是'形似凭据的样例值'：没有它们就无法证明检测器"
        "真的会红（本仓库已经踩过'检测器静默失效但门禁全绿'的坑）。"
        "豁免范围精确到**一个文件**，且这些样例值全部是编造的。",
    ),
)

#: 明显是编造的样例值：顺序数字/顺序字母/同一数字重复。
#:
#: 为什么需要它：`test_compliance_gate.py` 必须写下"密钥长什么样"才能验证
#: 检测器真的有效，于是仓库里必然存在形似密钥的样例串
#: （`sk-abcdefghijklmnopqrstuvwxyz`）。用允许清单逐条豁免也能做到，但那种
#: 豁免会随样例增删而漂移；"顺序字母表"是**值本身**携带的信号，判起来更稳。
_OBVIOUSLY_FAKE: Final = re.compile(
    r"0123456789|1234567890|abcdefghij|^(\d)\1+$|^([A-Za-z])\2+$")


@dataclass(frozen=True)
class Finding:
    shape: str
    path: str
    line: int
    text: str
    value: str

    def render(self) -> str:
        return f"[{self.shape}] {self.path}:{self.line}: {self.text}"


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    suppressed: list[Finding] = field(default_factory=list)
    #: 被**路径豁免**（而非允许清单）吃掉的命中。
    #: 为什么单独记一笔：路径豁免是"整类形态+整个目录"，最容易变成看不见的假绿
    #: —— 数量必须一直可见，才不会"扩了一条豁免规则，从此再没人知道漏了多少"。
    exempted: list[Finding] = field(default_factory=list)
    scanned_files: int = 0
    skipped_binary: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def stale_allowlist(self) -> list[str]:
        """允许清单里"什么都没压住"的条目 —— 棘轮：过期条目必须删掉。"""
        return sorted(set(self.notes))


def is_placeholder(text: str) -> bool:
    return bool(PLACEHOLDER.search(text))


def _looks_like_secret(value: str) -> bool:
    """值级判据：它像不像一个**真值**。

    没有这一步，`password=body.password`、`token=view.token`、`pwd=sys.argv[1]`
    全会被判成泄漏（实测 B 类原始命中 136 处，其中真值只有个位数）。
    判据被噪声淹没的结局就是被关掉，所以宁可在这里窄一点。
    """
    if len(value) < 8 or is_placeholder(value):
        return False
    if not re.search(r"[A-Za-z]", value):
        return False
    if not re.search(r"[0-9]", value):
        return False
    # 属性访问 / 函数调用 / 下标 —— 是表达式不是字面量
    if re.search(r"[.()\[\]]", value):
        return False
    # 全大写+下划线 = 环境变量**名**（`MOSS_VERIFY_AUTH_E2E_PWD`）。
    # 踩过的坑：`_PWD_ENV = "MOSS_VERIFY_AUTH_E2E_PWD"` 里的 `E2E` 含数字，
    # 于是"像真值"成立 —— 把**变量名**当成口令报了 6 处。变量名不是凭据。
    if re.fullmatch(r"[A-Z0-9_]+", value):
        return False
    return True


def _exempt(path: str, shape: str) -> bool:
    return any(rule.search(path) and shape in shapes
               for _name, rule, shapes, _why in EXEMPT_PATH_RULES)


# ======================================================================
# 撤回清单：**已经泄漏过的具体值**，一律不许再出现在任何地方
# ======================================================================
#
# 为什么需要它，而不是再加几条正则：
#
# 2026-10-02 实测的 22 处口令里，有两处**根本没有键名**（值一律**脱敏**：
# 抄真值进注释等于把泄漏再发布一次，本模块的 `I_revoked_value` 会当场报出来）：
#   · `pg.fill('input[placeholder="输入密码"]', "MossDev…")`
#   · docstring 里的「实测 `admin` / `MossPilot…` 通过 API 登录」
# 任何"键名 + 值"的形态规则都够不着它们。我试过用"同一行既有凭据语境、
# 又有形似口令的字面量"来兜（H_ctx_literal），实测在 1457 个已跟踪文件上
# 报出 **71 处**，其中真值 2 处、其余是 `deepseek-v4-flash`、`CHG-0113`
# 这类散文与编号 —— 噪声比 35:1。判据被噪声淹没的结局就是被关掉。
#
# 所以换一个方向：**枚举已经泄漏过的那几个值**。精确、零误报，而且语义
# 正好是安全上真正要的那句话 ——「这些凭据已经作废，永远不许再出现」。
#
# 为什么存**哈希**而不是明文：把明文写进这个文件，等于把泄漏再发布一次
# （而且这次还带上了"这是口令"的标注）。存哈希既能匹配，又不复制秘密。
REVOKED_PATH: Final = ROOT / "configs" / "revoked_secrets.yaml"

#: 从文本里切出"可能是一个口令/密钥"的候选 token。
#: 覆盖 `MossPilot…`、`TmpProbe…`、`Z9u1KUb…` 这类形态（**样例一律脱敏**：
#: 它们是撤回清单里的已泄漏凭据，写全值会被本模块自己的判据抓住）。
_TOKEN: Final = re.compile(r"[A-Za-z][A-Za-z0-9!#$%*_@\-]{7,}")


def _token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def load_revoked(path: Path | None = None) -> dict[str, str]:
    """读撤回清单：`{sha256 前 16 位: 人类可读标签}`。文件缺失即空清单。"""
    target = path or REVOKED_PATH
    if not target.exists():
        return {}
    import yaml

    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for row in data.get("entries", []) or []:
        digest = str(row.get("sha256_16") or "")
        if len(digest) != 16:
            raise ValueError(f"撤回清单条目缺少合法的 sha256_16：{row!r}")
        out[digest] = str(row.get("label") or "未命名")
    return out


def scan_revoked(text: str, *, path: str, revoked: dict[str, str],
                 exempted: list[Finding] | None = None) -> list[Finding]:
    """扫"已泄漏过的具体值"。

    这条**不吃路径豁免**：撤回清单说的是"这个值永远不许出现"，
    与它出现在哪个目录无关（测试夹具里同样不许抄真口令）。
    """
    out: list[Finding] = []
    for match in _TOKEN.finditer(text):
        label = revoked.get(_token_hash(match.group(0)))
        if not label:
            continue
        out.append(Finding(
            shape="I_revoked_value",
            path=path,
            line=text.count("\n", 0, match.start()) + 1,
            text=f"<已撤回凭据：{label}>",
            value=match.group(0),
        ))
    return out


#: 键名本身 —— 取出来的"值"等于它就说明 `value_group` 配错了。
_KEYWORDS: Final = frozenset({
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "access_key", "private_key", "auth_token", "api-key", "access-key",
})


#: 值尾部的标点 —— 无引号形态（D_shell）会把 `"` `)` 一起吃进值里，
#: 导致值级判据因为"含括号"而误判成表达式。踩过的坑：测试里写的
#: `"export MOSS_TOKEN=…")` 因此**扫不出来**，而同一个值在别处能扫出来。
_TRAILING_PUNCT: Final = re.compile(r"""["')\],;:}]+$""")


def _value_of(match: re.Match[str], shape: Shape) -> str:
    """取出命中串里的"值"，并对"指到键名"这种配置错误**当场出声**。

    静默地把键名当成值 → 值级判据全部不通过 → 检测器一条都抓不到却全绿。
    这类"改了≠生效了"必须抛错而不是降级。
    """
    if not shape.value_group:
        return match.group(0)
    groups = (shape.value_group if isinstance(shape.value_group, tuple)
              else (shape.value_group,))
    for index in groups:
        got = match.group(index)
        if got:
            value = _TRAILING_PUNCT.sub("", got.strip().strip("\"'"))
            if value.lower() in _KEYWORDS:
                if not shape.strict_value:
                    return ""       # 同行共现形态：这行不是泄漏，跳过
                raise ValueError(
                    f"形态 {shape.name} 的 value_group={shape.value_group} 指到了"
                    f"**键名** {value!r} 而不是值 —— 检测器配置错误，"
                    f"它会让所有真值都被判成'不像密钥'从而静默失效")
            return value
    return match.group(0)


def scan_text(text: str, *, path: str,
              exempted: list[Finding] | None = None) -> list[Finding]:
    """扫一段文本，返回命中（已剔除占位符、表达式与豁免路径）。

    `exempted` 非空时，被路径豁免吃掉的命中会追加进去 —— 用于**统计可见性**，
    它们不会被当成违规。
    """
    out: list[Finding] = []
    for shape in SHAPES:
        skip = _exempt(path, shape.name)
        for match in shape.pattern.finditer(text):
            value = _value_of(match, shape)
            if not value:
                continue
            # 占位符判的是**值**，不是整段命中：`H_ctx_literal` 的 group(0)
            # 覆盖整行，用整行判会让"同一行里出现 HTML 属性名 placeholder"
            # 之类的无关词把真口令一起放过（实测漏 1 处）。
            if is_placeholder(value):
                continue
            if _OBVIOUSLY_FAKE.search(value):
                continue
            if shape.name in ("B_kv", "B2_tuple", "B3_env_default",
                              "B4_ternary_default", "C_cli_flag", "D_shell",
                              "E_cjk") and not _looks_like_secret(value):
                continue
            if shape.name == "H_ctx_literal" \
                    and not _looks_like_generated_secret(value):
                continue
            finding = Finding(
                shape=shape.name,
                path=path,
                line=text.count("\n", 0, match.start()) + 1,
                text=match.group(0).strip()[:120],
                value=value,
            )
            if skip:
                if exempted is not None:
                    exempted.append(finding)
                continue
            out.append(finding)
    return out


# ======================================================================
# 扫描对象：**被 git 跟踪的文件**（不是"某个目录"）
# ======================================================================

#: 这些后缀当文本读；其余后缀（图片/二进制/表格）跳过。
TEXT_SUFFIXES: Final = frozenset({
    ".py", ".md", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".json", ".txt",
    ".sh", ".ps1", ".bat", ".cmd", ".html", ".css", ".js", ".ts", ".tsx",
    ".vue", ".sql", ".csv", ".gitignore", ".gitattributes", ".editorconfig",
    ".example", ".env", ".spec", ".xml", ".ipynb",
})

#: 单文件上限：超过就当二进制跳过（避免把几 MB 的录像带读进内存）。
MAX_BYTES: Final = 2 * 1024 * 1024


def tracked_paths(root: Path | str = ROOT) -> list[str]:
    """`git ls-files` 的结果 —— **这就是"会上传到 GitHub 的集合"的定义**。"""
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(root),
                          capture_output=True, check=True)
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def iter_tracked_text(root: Path | str = ROOT) -> Iterator[tuple[str, str]]:
    """逐个产出 (相对路径, 文本)。读**工作区**内容；工作区没有时回落到索引。"""
    root = Path(root)
    for rel in tracked_paths(root):
        path = root / rel
        suffix = Path(rel).suffix.lower()
        if suffix and suffix not in TEXT_SUFFIXES:
            continue
        if path.is_file():
            if path.stat().st_size > MAX_BYTES:
                continue
            raw = path.read_bytes()
        else:
            proc = subprocess.run(["git", "show", f":{rel}"], cwd=str(root),
                                  capture_output=True)
            if proc.returncode != 0:
                continue
            raw = proc.stdout
        if b"\x00" in raw[:4096]:
            continue
        yield rel, raw.decode("utf-8", "replace")


def iter_head_text(root: Path | str = ROOT) -> Iterator[tuple[str, str]]:
    """产出 **HEAD（= GitHub 上那一份）**的内容，用 `git archive` 取。

    这是"线上真实状态"的定义：工作区改了但没提交，这里仍然是旧的 ——
    这正是"改了≠生效了"要能被看见的地方。
    """
    import io
    import tarfile

    root = Path(root)
    proc = subprocess.run(["git", "archive", "--format=tar", "HEAD"],
                          cwd=str(root), capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or member.size > MAX_BYTES:
                continue
            suffix = Path(member.name).suffix.lower()
            if suffix and suffix not in TEXT_SUFFIXES:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            raw = handle.read()
            if b"\x00" in raw[:4096]:
                continue
            yield member.name, raw.decode("utf-8", "replace")


@dataclass(frozen=True)
class AllowEntry:
    path: str
    shape: str
    why: str


def load_allowlist(path: Path | None = None) -> list[AllowEntry]:
    """读允许清单。**缺字段即报错**（不允许"写个路径就免检"）。"""
    target = path or ALLOWLIST_PATH
    if not target.exists():
        return []
    import yaml

    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    out: list[AllowEntry] = []
    for row in data.get("entries", []) or []:
        missing = [k for k in ("path", "shape", "why") if not row.get(k)]
        if missing:
            raise ValueError(
                f"允许清单条目缺字段 {missing}：{row!r} —— "
                f"豁免必须写明'哪条路径的哪类形态、为什么'")
        if len(str(row["why"]).strip()) < 8:
            raise ValueError(f"允许清单的 why 太短，等于没写理由：{row!r}")
        out.append(AllowEntry(str(row["path"]), str(row["shape"]),
                              str(row["why"])))
    return out


def scan_items(items: Iterable[tuple[str, str]],
               allowlist: Iterable[AllowEntry] | None = None,
               revoked: dict[str, str] | None = None) -> ScanResult:
    """核心扫描：对 (路径, 文本) 序列应用全部形态、豁免规则与撤回清单。"""
    entries = list(allowlist if allowlist is not None
                   else load_allowlist())
    revoked_set = load_revoked() if revoked is None else revoked
    by_key: dict[tuple[str, str], AllowEntry] = {
        (e.path, e.shape): e for e in entries}
    used: set[tuple[str, str]] = set()
    #: 同一个字面量会被多条形态同时命中（`PWD = "x"` 既合 B_kv 又合 B2_tuple），
    #: 按 (路径, 行号, 值) 去重 —— 报两遍不会更安全，只会让报告变吵。
    seen: set[tuple[str, int, str]] = set()
    result = ScanResult()
    for rel, text in items:
        result.scanned_files += 1
        for finding in scan_text(text, path=rel, exempted=result.exempted):
            dedup_key = (finding.path, finding.line, finding.value)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            key = (finding.path, finding.shape)
            if key in by_key:
                used.add(key)
                result.suppressed.append(finding)
                continue
            result.findings.append(finding)
        # 撤回清单：不吃豁免，任何文件里出现都算违规
        for finding in scan_revoked(text, path=rel, revoked=revoked_set):
            dedup_key = (finding.path, finding.line, finding.value)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            result.findings.append(finding)
    for key, entry in by_key.items():
        if key not in used:
            # 棘轮：豁免条目必须真的压住一条命中，否则它只是历史残留。
            result.notes.append(
                f"允许清单条目已过期（现在压不住任何命中）："
                f"{entry.path} :: {entry.shape} —— {entry.why}")
    # 路径豁免的明细也去重（同一字面量会被多条形态重复记）
    deduped: list[Finding] = []
    seen_ex: set[tuple[str, int, str]] = set()
    for f in result.exempted:
        ex_key = (f.path, f.line, f.value)
        if ex_key in seen_ex:
            continue
        seen_ex.add(ex_key)
        deduped.append(f)
    result.exempted = deduped
    return result


def scan_tracked_tree(root: Path | str = ROOT,
                      allowlist: Iterable[AllowEntry] | None = None,
                      revoked: dict[str, str] | None = None,
                      *, source: str = "worktree") -> ScanResult:
    """扫整个已跟踪集合。

    `source="worktree"` 读工作区（开发者改完立刻看到反馈）；
    `source="head"` 读 HEAD（= 已经上传到 GitHub 的那一份）。
    """
    if source == "worktree":
        items: Iterable[tuple[str, str]] = iter_tracked_text(root)
    elif source == "head":
        items = iter_head_text(root)
    else:
        raise ValueError(f"未知来源 {source!r}（只支持 worktree / head）")
    return scan_items(items, allowlist, revoked)


__all__ = [
    "ALLOWLIST_PATH",
    "EXEMPT_PATH_RULES",
    "REVOKED_PATH",
    "AllowEntry",
    "Finding",
    "PLACEHOLDER",
    "SHAPES",
    "ScanResult",
    "iter_head_text",
    "iter_tracked_text",
    "load_allowlist",
    "load_revoked",
    "scan_items",
    "scan_revoked",
    "scan_text",
    "scan_tracked_tree",
    "tracked_paths",
]
