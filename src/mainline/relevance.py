"""板块成分股提纯：走势相关性 + 主营业务相关性 + 市值门槛。

## 要解决的问题（实测现场）

主线挖掘的成分股来自 `ths_member`（概念），而**同花顺概念归集很松**：
`885959.TI PCB概念` 有 236 只成分股，抽查前 12 只只有 3 只主营业务真的与 PCB
有关，其余是 TCL 科技、铜陵有色、中钨高新这类被"蹭"进来的票。这会污染板块
资金流、景气度、筹码等所有下游维度。

## 提纯规则（用户口径 2026-09-20 修订）

一只股票要留在某概念板块里，该概念必须落在**这只股票自身**「最相关的概念前 3」
之内。**"相关"由两个信号共同决定**：

1. **走势相关性**（市场把这只股当什么在炒，权重 60%）；
2. **主营业务相关性**（公司实际做什么，权重 40%）。

另有三条硬门槛：**总市值 < 30 亿**、**ST / 退市**、**统计/事件/地域型假板块**。
"前 3"是**题材数的上限**，不是"只能留一个板块" —— 一只股可以真正属于多个概念，
它会在每个相关题材映射出的板块里都被保留。

## 目标：主要剔除"低相关但大市值、在板块中权重占比高"的票

这类票在市值加权口径下会**主导板块走势**，把板块指数变成一只与主题无关的股票
（这正是提纯要针对的目标）。相关性过滤正好命中它们：三花智控的"家电零部件"
就是典型 —— 市值大、相关性只排第 14（+0.547），会被剔掉。

### 为什么必须加"走势相关性"（这是修订的核心）

只看主营业务会得出**完全错误**的结论。实测三花智控 `002050`（主营制冷部件）
与其概念板块的走势相关性（240 交易日，共同日期对齐）：

    通用设备制造业指数 +0.732     ← 行业分类，最相似但过于宽泛
    人形机器人         +0.657     ← 用户点名的例子，机器人业务占比还很低
    汽车热管理         +0.623
    特斯拉概念         +0.608
    华为汽车           +0.605
    ...
    家电零部件         +0.547     ← 真实主营
    家用电器           +0.514

三花的机器人业务营收占比还很低，但**走势由人形机器人驱动**。纯主营口径会把
"人形机器人"砍掉、把"家用电器"留下来 —— 与使用者想要的正好相反。
（实测两个云层模型都把人形机器人排进前 3，见 `docs/MAINLINE_MINING.md` 10.6。）

### 但相关性也不能单独用

同一份实测里，**事件型/统计型/地域型板块靠巧合冲进前列**：

    近期解禁     +0.567   （第 7）
    次新股       +0.520
    杭州都市圈   +0.570   ← 同城共振，与产业完全无关
    绍兴市指数   +0.557

所以相关性定**排序与权重**，主营相关性做**证伪过滤** —— 两者互补，
谁单独用都会出错。

## 为什么本地文本匹配替代不了 LLM

三条纯文本路线都实测失败，记录在此避免后人重走：

| 方案 | 结果 |
|---|---|
| 主营文本子串匹配 | `半导体概念` 对中芯国际（主营"**集成电路**晶圆代工"）得分 **0** |
| 人工同义词表 | 2205 个概念名，不可维护 |
| 股票行业字段匹配（110 个 Tushare 行业） | 全量**只有 16%** 的概念能命中 |

根因是**概念题材名与公司主营描述用词体系不同**，是语义问题。
LLM 实测正确：中芯国际→集成电路制造/半导体/芯片、茅台→白酒、平安银行→股份制银行。

行情数据只能给出"市场怎么看"，给不出"业务是否真的沾边"；反过来主营文本给不出
"市场怎么看"。两个信号一个来自量化、一个来自语义，缺一不可。

## 同一题材的多重分类（必须去重）

实测茅台同时归入 `白酒 881273`、`白酒Ⅲ 884188`、`白酒概念 885525`，
**同一个题材会占掉多个前 3 名额**。全量统计：同名概念 248 组、去掉
「概念/指数/A股/Ⅲ」等后缀后近似重复 **404 组**（"家用电器"重复 4 次）。

所以打分只对**题材（归一化名）**排序，落库时把题材映射回它在各分类体系下的
全部板块代码 —— "白酒"这一个题材在前 3 里只占一个名额，下面挂几个板块都受益。

## 相关性离线、市值在线（避免前视偏差）

两件事的**时点语义完全不同**：

- **相关性**（走势 + 主营）依赖当前成分股快照与最新一期主营描述，本质是静态的
  → 离线批算一次落库。概念归属本身就没有历史（`ml_member` 是当前快照），
  这是主线模块已知的 survivorship 局限。
- **市值与 ST 必须按运行日取**。若把市值在离线时写死成"今天"，跑 2022 年回测
  就会用 2026 年的市值筛成分股 —— 自动选中了后来才长大的票，回测会好得离谱。
  ST 状态同样会变（"ST 舍得"后来摘帽），所以名称也按运行日取。

因此 `ml_member_clean` 只存**相关性结论**，市值 / ST 在 `clean_member_map()` 里
按传入的 `trade_date` 现场判定。

## 打分数量的两个陷阱（都实测踩过）

1. **必须限制打分范围**：`ml_member` 的概念成分股有 13116 个 code，但只有
   5557 个是真实 A 股 —— 其余是导出时产生的合成占位码（`00000A`…`00000J`）。
   用 `score_universe()` 过滤掉它们（详见该函数的 docstring）。
2. **`light` 路由层不可用**：本地 qwen2.5:1.5b 每题都给 85 分、理由是模板复制，
   且随机只答 1~6 个（候选 20 个）。默认用 `reasoning`（deepseek-flash）。

## 成本（实测 2026-09-20）

| 步骤 | 实测 |
|---|---|
| `business` 拉主营（`stock_company` 全量 6294 行） | 1.8 秒，零成本 |
| `corr` 走势相关性（纯本地） | 24 秒（110605 个有效对） |
| `score` LLM 逐股判定（4996 只，`reasoning` 层并发 16） | 约 83 分钟，约 12~25 元 |

结果落库并带 `prompt_sig` 签名：主营文本或候选题材集没变就直接跳过（重跑免费）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.errors import BRIEF_DEFAULT, brief
from src.core.exceptions import FinAgentError
from src.mainline.warehouse import open_warehouse

logger = logging.getLogger(__name__)

#: 行情仓是否需要走 `immutable=1`（一旦正常只读打开失败就置 True 并保持）。
#: 见 `_open_warehouse` 的说明：这是环境性故障，不会自愈，
#: 记住它可以避免每天重复付出失败连接的代价。
_WAREHOUSE_IMMUTABLE = False

#: 相关性门槛：该题材必须落在个股自身归属题材的前 N 名内
TOP_THEMES = 3
#: 总市值门槛（元）：低于它的成分股删除
MIN_TOTAL_MV = 3_000_000_000.0
#: 主营描述送入 prompt 的字符上限（防单次 prompt 膨胀）
BUSINESS_CHARS = 900
#: 走势相关性权重（其余归主营业务相关性）
CORR_WEIGHT = 0.6
#: 走势相关性的回看交易日数
CORR_WINDOW = 240
#: 候选题材上限：按相关性排序后只把前 N 个交给 LLM（省 token、避免长尾干扰）
MAX_CANDIDATES = 20
#: 少于这么多个有效收益率样本就认为相关性不可置信
MIN_CORR_SAMPLES = 60
#: **新板块**（自身行情还不足 `MIN_CORR_SAMPLES` 个交易日）的最低样本数。
#:
#: 为什么需要：门槛卡的是"股票与板块的共同交易日"，而共同交易日**不可能多于
#: 板块自己的行情长度**。所以一个刚上市两个月的概念，它的**每一只**成分股都
#: 凑不够 60 天 —— 整块概念的提纯一次都没做，却和"某只新股没历史"记成同一类
#: （实测 886111 玻璃基板 56/56、886112 MLCC概念 33/33 全部 corr=NULL）。
#:
#: 放到 20 天是有依据的：实测这两个板块在 34~58 个共同交易日上，
#: 成分股与板块的 corr 中位数 0.77~0.80、最低 0.23 —— 用 0.55 卡它，
#: n=34 时 p 值约 3e-4，仍然是一个很强的条件，足够把不跟板块走的沾边股摘掉
#: （886111 57→39 只、886112 35→24 只，见
#: `scripts/young_board_purification_report.py`）。
#:
#: 仍然保留一个下限而不是"有多少用多少"：3~5 天的相关性是纯噪声。
MIN_CORR_SAMPLES_YOUNG = 20
#: 占位题材名：该股票与**所有**候选题材都无实质关联时写入，
#: 用于让"确实都不相关"与"这次调用失败了"在库里可区分（见 `_score_body`）。
MISSING_THEME = "<NONE>"


#: 打分调用的输出上限。**必须显式给足**。
#:
#: ⚠️ 这是本次排查出的**最隐蔽的一个坑**：DeepSeek 的**推理 token 计入输出上限**，
#: 所以默认的 4096 会先被思维链吃光 —— 实测约 20% 的调用 `tokens_out` 正好等于
#: 4096、`content` 为**空字符串**（推理做完但正文一个字都没输出）。
#:
#: 为什么难查：
#:
#: - 表现为"偶发空响应"，看着像提供商抖动，但**重试同样失败**（确定性而非抖动）；
#: - 单独探测某只股票往往成功（那次推理恰好短），只有批量跑才暴露比例；
#: - 失败是静默的：没有记录、也不报错（见 `score_stocks` 的重试说明）。
#:
#: 抬到 32768 后实测失败率从 ~20% 降到 ~0（`tokens_out` 观察到 890~7653，
#: 长推理的会更高；给足预算比反复重试便宜 —— 重试要重跑整条思维链）。
#: 注意 `configs/models.yaml` 里的 `max_tokens`（4096）仍是**普通调用**的默认值，
#: 只有这里显式覆盖的调用才用更高预算；全局护栏见
#: `Settings.llm_max_tokens_hard_cap`。
SCORE_MAX_TOKENS = 32768

#: 本模块 LLM 缓存条目的存活小时数（**按调用覆盖**全局的 24h）。
#:
#: 为什么该比全局长得多：本模块的 prompt 只由「公司名/代码/主营文本/题材集」
#: 拼成，**没有任何时间戳**；而主营描述与题材归属是**季度级**才变的内容。
#: 用全局 24h 的后果是"隔天再跑同一批 prompt 全部重新计费"——实测这一家
#: 独占全库计费输出 token 的约 92%（8.6M 输入 / 23.3M 输出）。
#:
#: 真正决定"要不要重算"的是 `ml_stock_theme.prompt_sig`（主营文本或题材集
#: 变了就重算），LLM 缓存只是它的第二道防线。所以这里可以放心给长 TTL：
#: 想强制重算时走 `force=True`（会带 `use_cache=False` 穿透缓存）。
RELEVANCE_CACHE_TTL_HOURS = 24.0 * 30


class RelevanceError(FinAgentError):
    """提纯流程失败。"""


# ======================================================================
# 题材归一化与假板块
# ======================================================================

#: 归一化时要剥掉的体系后缀 —— 剥掉后同名即视为**同一个题材**
_THEME_NOISE = (
    "概念", "板块", "指数", "行业", "题材",
    "(A股)", "（A股）", "A股",
    "Ⅰ", "Ⅱ", "Ⅲ",
)

#: 统计型/事件型/风格型"假板块"：不是产业题材。
#:
#: 两类来源：
#: 1. **行情特征池**（"昨日涨停""低价股"）—— 动辄上千只成员，本身就是筛选结果；
#: 2. **事件/数据型**（"近期解禁""次新股""机构重仓"）—— 实测这些会靠**巧合**
#:    冲进个股相关性前列（三花的"近期解禁"+0.567 排第 7、"次新股"+0.520 排
#:    第 14），必须排除，否则会挤掉真正的主线题材。
FAKE_BOARD_PATTERNS = (
    # 行情特征/风格
    r"涨幅", r"跌幅", r"涨停", r"跌停", r"打板", r"炸板", r"连板", r"首板",
    r"高贝塔", r"低贝塔", r"贝塔值", r"换手率", r"振幅", r"量比",
    r"市盈率", r"市净率", r"市销率", r"高价股", r"低价股", r"微盘", r"小盘",
    r"小市值", r"大盘股", r"超大盘", r"全收益", r"^(高|低)ROE",
    r"^(高|低)股息", r"破净", r"破发", r"预增", r"预亏",
    # 指数/样本
    r"样本股", r"成份股", r"成分股", r"等权", r"加权", r"标的证券",
    r"^同花顺", r"新质50", r"全成分",
    # 事件/数据
    r"解禁", r"减持", r"增持", r"回购", r"质押", r"龙虎榜", r"高送转",
    r"除权", r"分红", r"配股", r"定增", r"并购重组", r"举牌", r"摘帽",
    r"商誉", r"次新", r"st股", r"^ST", r"退市", r"复牌", r"异动",
    r"强势", r"新高", r"新低", r"热股",
    # 国民经济行业分类的制造业大类 + 宽泛行业/风格标签。
    #
    # ⚠️ 实测这类名字会**成组**吃掉整个前 3：贵州茅台的前 3 被判成
    # 「酒、饮料和精制茶制造业 / 酒饮料和精制茶制造业 / 饮料」—— 前两个是
    # 同一个东西的两种写法，结果「白酒」概念被挤出去，
    # 白酒板块反而把泸州老窖/五粮液/贵州茅台/洋河股份这些**最该留的**全剔了。
    #
    # 它们的共同特征：**能映射到板块，但对"该买哪只票"没有区分度** ——
    # 半个市场都算"日常消费品"。所以按假板块排除。
    r"制造业", r"^酒饮料",
    r"^饮料$", r"^食品、饮料与烟草$", r"^酿酒商与葡萄酒商$", r"^日常消费品$",
    r"^超级品牌$", r"^行业龙头$", r"^茅$",
    # 持仓/资金统计
    r"重仓股", r"成交前十", r"资金前十", r"机构重仓", r"证金持股", r"百元股",
    r"陆股通", r"互联互通", r"融资融券",
    # 股东/财报统计
    r"股东户数", r"股东人数", r"十大股东", r"股权集中",
    # 地域板块：按注册地/办公地归集，不是产业题材。实测三花智控与
    # "杭州都市圈" +0.570、"绍兴市指数" +0.557 —— 同城共振纯属地缘，与产业无关。
    r"都市圈", r"城市群", r"^.{2,6}市指数$", r"^.{2,4}板块$", r"经济区",
    r"自贸区", r"^.{2,4}省", r"长三角", r"珠三角", r"京津冀",
    # 时间前缀
    r"^昨日", r"^今日", r"^近期",
)
_FAKE_RE = re.compile("|".join(FAKE_BOARD_PATTERNS), re.IGNORECASE)

#: **保护名单**：`^同花顺` 会把「同花顺算力主题精选」「同花顺果指数」这类
#: 真实题材一起误杀（它们的名字带前缀但内容是产业主题）。
#: 命中这里的关键字就直接判定为真板块，优先于一切假板块规则。
#:
#: ⚠️ 实测「同花顺出海50」也是风格篮子（按出海收入筛的股票池），但它同样
#: 被 `^同花顺` 命中 —— 这里**刻意不放进来**，让它按统计型排除。
PROTECTED_KEYWORDS = ("算力", "果指数", "中特估")


def is_fake_board(name: str) -> bool:
    """是否统计型/事件型/风格型假板块（不是产业题材）。

    保护名单优先：`同花顺算力主题精选` 这类名字带指数前缀但内容是产业主题的，
    必须判为真板块，否则真实题材会被规则误杀。
    """
    text = str(name or "").strip()
    if any(word in text for word in PROTECTED_KEYWORDS):
        return False
    return bool(_FAKE_RE.search(text))


def normalize_theme(name: str) -> str:
    """概念名 → 题材标识：剥掉分类体系后缀后同名即同一题材。"""
    text = str(name or "").strip()
    changed = True
    while changed:
        changed = False
        for noise in _THEME_NOISE:
            if text.endswith(noise) and len(text) > len(noise):
                text = text[: -len(noise)]
                changed = True
    return text.strip()


# ======================================================================
# 落库
# ======================================================================

_SCHEMA = """
-- 主营描述缓存（来自 stock_company 批量接口，零成本、可全量刷新）
CREATE TABLE IF NOT EXISTS ml_company_business (
    code TEXT PRIMARY KEY,
    ts_code TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    business TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);

-- 逐 (股票, 板块) 的走势相关性（离线计算；事件型板块已排除）
CREATE TABLE IF NOT EXISTS ml_member_corr (
    board_code TEXT NOT NULL,
    code TEXT NOT NULL,
    corr REAL,
    samples INTEGER NOT NULL DEFAULT 0,
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    computed_at TEXT NOT NULL,
    PRIMARY KEY (board_code, code)
);
CREATE INDEX IF NOT EXISTS idx_ml_member_corr_code ON ml_member_corr(code);

-- 逐股票：确定的归属题材 + 两个信号的分项得分（LLM 打分产物）
CREATE TABLE IF NOT EXISTS ml_stock_theme (
    code TEXT NOT NULL,
    rank INTEGER NOT NULL,
    theme TEXT NOT NULL,
    raw_name TEXT NOT NULL DEFAULT '',
    business_score REAL NOT NULL DEFAULT 0,
    corr REAL,
    final_score REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_sig TEXT NOT NULL DEFAULT '',
    scored_at TEXT NOT NULL,
    PRIMARY KEY (code, rank)
);
CREATE INDEX IF NOT EXISTS idx_ml_stock_theme_theme ON ml_stock_theme(theme);

-- 题材 → 各分类体系下的板块代码（一个题材可能对应多个板块）
CREATE TABLE IF NOT EXISTS ml_theme_board (
    theme TEXT NOT NULL,
    board_code TEXT NOT NULL,
    board_name TEXT NOT NULL DEFAULT '',
    board_kind TEXT NOT NULL DEFAULT '',
    members INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (theme, board_code)
);

-- 逐 (板块, 股票) 的**相关性**结论（只存离线事实；市值不在此表）
CREATE TABLE IF NOT EXISTS ml_member_clean (
    board_code TEXT NOT NULL,
    code TEXT NOT NULL,
    relevant INTEGER NOT NULL DEFAULT 0,
    rank_in_stock INTEGER,
    theme TEXT NOT NULL DEFAULT '',
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY (board_code, code)
);
CREATE INDEX IF NOT EXISTS idx_ml_member_clean_rel
    ON ml_member_clean(board_code, relevant);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_code(ts_code: str) -> str:
    return str(ts_code or "").split(".")[0].strip()


@dataclass
class CleanStats:
    """一轮提纯的统计（用于汇报与验收）。"""

    stocks_scored: int = 0
    stocks_cached: int = 0
    stocks_failed: int = 0
    corr_pairs: int = 0
    corr_boards: int = 0
    themes: int = 0
    theme_boards: int = 0
    boards_fake_excluded: int = 0
    pairs_before: int = 0
    pairs_after: int = 0
    dropped_mv: int = 0
    dropped_irrelevant: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        keep = (self.pairs_after / self.pairs_before) if self.pairs_before else 0.0
        return (f"提纯后成分股归属 {self.pairs_before} → {self.pairs_after}"
                f"（保留 {keep:.0%}）；其中市值不足剔除 {self.dropped_mv}、"
                f"相关性不足剔除 {self.dropped_irrelevant}；"
                f"走势相关性 {self.corr_pairs} 对 / {self.corr_boards} 个板块，"
                f"题材 {self.themes} 个，排除假板块 {self.boards_fake_excluded} 个")


class RelevanceStore:
    """提纯结果的读写（与主线主库共用同一个 sqlite 文件）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    # ---------------- 主营描述 ----------------

    def save_business(self, conn: sqlite3.Connection,
                      rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        stamp = _now()
        conn.executemany(
            "INSERT INTO ml_company_business"
            "(code, ts_code, name, business, fetched_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(code) DO UPDATE SET"
            " ts_code=excluded.ts_code, name=excluded.name,"
            " business=excluded.business, fetched_at=excluded.fetched_at",
            [(str(r["code"]), str(r.get("ts_code") or ""),
              str(r.get("name") or ""), str(r.get("business") or ""), stamp)
             for r in rows])
        conn.commit()
        return len(rows)

    def business_map(self, conn: sqlite3.Connection
                     ) -> dict[str, tuple[str, str]]:
        """`{code: (name, business)}`。"""
        return {str(r["code"]): (str(r["name"] or ""), str(r["business"] or ""))
                for r in conn.execute(
                    "SELECT code, name, business FROM ml_company_business")}

    # ---------------- 走势相关性 ----------------

    def save_corr(self, conn: sqlite3.Connection,
                  rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        stamp = _now()
        conn.executemany(
            "INSERT INTO ml_member_corr"
            "(board_code, code, corr, samples, start_date, end_date, computed_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(board_code, code) DO UPDATE SET"
            " corr=excluded.corr, samples=excluded.samples,"
            " start_date=excluded.start_date, end_date=excluded.end_date,"
            " computed_at=excluded.computed_at",
            [(str(r["board_code"]), str(r["code"]), r.get("corr"),
              int(r.get("samples") or 0), str(r.get("start_date") or ""),
              str(r.get("end_date") or ""), stamp) for r in rows])
        conn.commit()
        return len(rows)

    def corr_map(self, conn: sqlite3.Connection
                 ) -> dict[str, dict[str, float]]:
        """`{code: {board_code: corr}}`（只含有有效样本的）。"""
        out: dict[str, dict[str, float]] = {}
        for row in conn.execute(
                "SELECT code, board_code, corr FROM ml_member_corr"
                " WHERE corr IS NOT NULL"):
            out.setdefault(str(row["code"]), {})[str(row["board_code"])] = float(
                row["corr"])
        return out

    def corr_stats(self, conn: sqlite3.Connection) -> dict[str, int]:
        row = conn.execute(
            "SELECT COUNT(*) AS pairs, COUNT(DISTINCT board_code) AS boards,"
            " COUNT(DISTINCT code) AS codes FROM ml_member_corr"
            " WHERE corr IS NOT NULL").fetchone()
        return {"pairs": int(row["pairs"] or 0), "boards": int(row["boards"] or 0),
                "codes": int(row["codes"] or 0)}

    # ---------------- 打分结果 ----------------

    def cached_signatures(self, conn: sqlite3.Connection) -> dict[str, str]:
        """`{code: prompt_sig}` —— 签名一致就跳过，候选集/主营文本变了自动重算。"""
        return {str(r["code"]): str(r["prompt_sig"] or "")
                for r in conn.execute(
                    "SELECT code, MIN(prompt_sig) AS prompt_sig"
                    " FROM ml_stock_theme GROUP BY code")}

    def save_scores(self, conn: sqlite3.Connection, code: str,
                    scored: Sequence[dict[str, Any]], *, model: str,
                    signature: str) -> int:
        stamp = _now()
        conn.execute("DELETE FROM ml_stock_theme WHERE code = ?", (str(code),))
        conn.executemany(
            "INSERT INTO ml_stock_theme(code, rank, theme, raw_name,"
            " business_score, corr, final_score, reason, model, prompt_sig,"
            " scored_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(str(code), int(item["rank"]), str(item["theme"]),
              str(item.get("raw_name") or ""),
              float(item.get("business_score") or 0.0), item.get("corr"),
              float(item.get("final_score") or 0.0),
              str(item.get("reason") or ""), str(model or ""), signature, stamp)
             for item in scored])
        conn.commit()
        return len(scored)

    def clear_scores(self, conn: sqlite3.Connection) -> int:
        cursor = conn.execute("DELETE FROM ml_stock_theme")
        conn.commit()
        return int(cursor.rowcount or 0)

    def score_coverage(self, conn: sqlite3.Connection) -> dict[str, int]:
        row = conn.execute(
            "SELECT COUNT(DISTINCT code) AS codes, COUNT(*) AS pairs"
            " FROM ml_stock_theme").fetchone()
        return {"codes": int(row["codes"] or 0), "pairs": int(row["pairs"] or 0)}

    # ---------------- 题材 → 板块 ----------------

    def rebuild_theme_boards(self, conn: sqlite3.Connection) -> int:
        """按归一化题材重建 `ml_theme_board`（排除统计型/事件型假板块）。"""
        conn.execute("DELETE FROM ml_theme_board")
        rows = conn.execute(
            "SELECT b.code, b.name, b.kind, COUNT(m.code) AS members "
            "FROM ml_board b LEFT JOIN ml_member m ON m.board_code = b.code "
            "GROUP BY b.code, b.name, b.kind").fetchall()
        payload: list[tuple[str, str, str, str, int]] = []
        for row in rows:
            name = str(row["name"] or "")
            if not name or is_fake_board(name):
                continue
            payload.append((normalize_theme(name), str(row["code"]), name,
                            str(row["kind"] or ""), int(row["members"] or 0)))
        conn.executemany(
            "INSERT INTO ml_theme_board"
            "(theme, board_code, board_name, board_kind, members)"
            " VALUES(?,?,?,?,?) ON CONFLICT(theme, board_code) DO UPDATE SET"
            " board_name=excluded.board_name, board_kind=excluded.board_kind,"
            " members=excluded.members", payload)
        conn.commit()
        return len(payload)

    def theme_board_map(self, conn: sqlite3.Connection) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for row in conn.execute("SELECT theme, board_code FROM ml_theme_board"):
            out.setdefault(str(row["theme"]), []).append(str(row["board_code"]))
        return out

    def board_kinds(self, conn: sqlite3.Connection) -> dict[str, str]:
        return {str(r["board_code"]): str(r["board_kind"] or "")
                for r in conn.execute(
                    "SELECT board_code, board_kind FROM ml_theme_board")}


# ======================================================================
# 主营描述同步
# ======================================================================

def sync_business(*, store: RelevanceStore, tushare: Any = None) -> int:
    """批量拉 `stock_company` 并缓存主营描述（实测 6294 行 1.4 秒）。"""
    if tushare is None:
        from src.quant.tushare_source import TushareClient, resolve_token
        tushare = TushareClient(token=resolve_token())
    frame = tushare.call("stock_company")
    if frame is None or len(frame) == 0:
        raise RelevanceError("stock_company 返回空")
    rows: list[dict[str, Any]] = []
    for row in frame.to_dict("records"):
        code = _parse_code(str(row.get("ts_code") or ""))
        if not code:
            continue
        parts = [str(row.get(col) or "").strip()
                 for col in ("main_business", "business_scope", "introduction")]
        business = " ".join(p for p in parts if p)
        rows.append({"code": code, "ts_code": str(row.get("ts_code") or ""),
                     "name": str(row.get("com_name") or ""),
                     "business": business[:4000]})
    conn = store.connect()
    try:
        return store.save_business(conn, rows)
    finally:
        conn.close()


# ======================================================================
# 走势相关性（离线）
# ======================================================================

def _log_returns(series: Sequence[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for index in range(1, len(series)):
        date, close = series[index]
        _prev_date, prev = series[index - 1]
        if close and prev and prev > 0 and close > 0:
            out[date] = math.log(close / prev)
    return out


def _pearson(xs: Sequence[float], ys: Sequence[float], *,
             min_samples: int = MIN_CORR_SAMPLES) -> float | None:
    n = len(xs)
    if n < min_samples:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy)


def _tail(series: Sequence[tuple[str, float]], window: int
          ) -> dict[str, float]:
    """取序列末尾 `window` 个交易日并转成日对数收益率 `{日期: 收益率}`。"""
    if len(series) > window + 1:
        series = series[-(window + 1):]
    return _log_returns(series)


def _common_tail(sret: dict[str, float], bret: dict[str, float],
                 window: int) -> list[str]:
    """取两个收益率序列**共同日期**的最后 `window` 个交易日。

    ⚠️ **不能各自截取末尾 N 个再求交集**（这是踩过的坑）：

    `ml_board_bar` 各板块的**数据末端差别很大** —— 实测 `700055.TI 茅指数`
    止于 2025-11-26，而个股 `000333` 止于 2026-09-17。若两边各取"最后 240 个
    交易日"，得到的是两个几乎不相交的时间段（交集仅 12 天），
    于是 9 万+ 个"股票×板块"组合里只有 1035 个算出相关性。

    正确做法是**先按共同日期对齐、再从对齐后的序列末尾截取**，
    这样无论某条序列在什么时候断掉，都能拿到它俩真正重叠的那段历史。
    """
    dates = sorted(sret.keys() & bret.keys())
    return dates[-window:]


def compute_correlations(*, store: RelevanceStore,
                         warehouse_path: str | Path,
                         window: int = CORR_WINDOW,
                         kinds: Sequence[str] = ("concept",),
                         progress: Any = None) -> CleanStats:
    """计算每只成分股与其所属板块的**日收益率相关性**。

    ## 为什么按"每只股票自己的交易日"回溯，而不是用统一日历

    ⚠️ **`ml_board_bar` 的交易日历是稀疏且分层的**（实测）：板块覆盖随时间
    台阶式变化 —— 2023 年只有 91 个板块有 K 线，2024-02 起 1400+，
    2024-09 起 1700+，2025-03 降到 1355，2026 年只剩 57 个"常青"板块，
    2026-09 才又回到 1667 个。

    所以**不能**用"板块日历里第 N 个日期"当全局窗口起点：那个日期会落在
    稀疏区，导致绝大多数"股票 × 板块"组合的重叠天数远少于预期
    （实测这样只有 1035 对成功，而应有 9 万+）。

    正确做法是**以每只股票自身的交易日为基准回溯**，再与板块序列求交集：
    两个序列在同一天都开市才有收益率可比，交集天然处理了停牌、
    板块数据缺失、日历口径差异。

    只算 `kinds` 指定的板块类型（默认概念）且**排除假板块** —— 事件型板块
    （"近期解禁"）会靠巧合产生高相关，算出来只会污染排序。

    纯本地计算，无网络调用。
    """
    started = time.perf_counter()
    stats = CleanStats()
    conn = store.connect()
    try:
        store.rebuild_theme_boards(conn)
        kinds_of = store.board_kinds(conn)
        scoped = {code for code, kind in kinds_of.items() if kind in set(kinds)}
        rows = conn.execute("SELECT board_code, code FROM ml_member").fetchall()
        by_code: dict[str, list[str]] = {}
        for row in rows:
            board, code = str(row["board_code"]), str(row["code"])
            if board in scoped:
                by_code.setdefault(code, []).append(board)
        stats.pairs_before = sum(len(v) for v in by_code.values())

        wh = _open_warehouse(warehouse_path)
        try:
            # 最早需要的日期：从行情仓自己的最新交易日往前取足 window*2 个
            horizon = [str(r[0]) for r in wh.execute(
                "SELECT DISTINCT trade_date FROM quant_daily"
                " ORDER BY trade_date DESC LIMIT ?", (window * 2,))]
            floor = horizon[-1] if horizon else "00000000"
            latest = horizon[0] if horizon else "99999999"
            logger.info("走势相关性：个股日历回溯至 %s（最新 %s）", floor, latest)

            # 每只股票 / 每个板块各自保留其末端 `window` 个收益率。
            # ⚠️ 各板块数据末端差别很大（有的止于 2025-11），所以必须让两边都
            # 保留**足够长**的历史，再按共同日期对齐截取 —— 见 `_common_tail`。
            boards = sorted(scoped)
            board_ret: dict[str, dict[str, float]] = {}
            for index, board in enumerate(boards, 1):
                series = [(str(r["trade_date"]), float(r["close"] or 0.0))
                          for r in conn.execute(
                              "SELECT trade_date, close FROM ml_board_bar"
                              " WHERE board_code = ? AND trade_date >= ?"
                              " ORDER BY trade_date", (board, floor))]
                board_ret[board] = _tail(series, window)
                if progress is not None and index % 200 == 0:
                    progress(index, len(boards), board)
            with_data = sum(1 for value in board_ret.values() if value)
            logger.info("板块收益率序列就绪：%d/%d 个有数据", with_data, len(boards))

            payload: list[dict[str, Any]] = []
            for index, (code, codes_boards) in enumerate(by_code.items(), 1):
                stock = [(str(r["trade_date"]), float(r["close"] or 0.0))
                         for r in wh.execute(
                             "SELECT trade_date, close FROM quant_daily"
                             " WHERE code = ? AND trade_date >= ?"
                             " ORDER BY trade_date", (code, floor))]
                sret = _tail(stock, window)
                if not sret:
                    continue
                for board in codes_boards:
                    bret = board_ret.get(board) or {}
                    if not bret:
                        continue
                    dates = _common_tail(sret, bret, window)
                    # 共同交易日不可能多于**板块自己的**行情长度，所以新板块
                    # 必须走更低的门槛，否则整块概念一只都算不出相关性
                    # （见 `MIN_CORR_SAMPLES_YOUNG`）。
                    min_needed = (MIN_CORR_SAMPLES if len(bret) >= MIN_CORR_SAMPLES
                                  else MIN_CORR_SAMPLES_YOUNG)
                    if len(dates) < min_needed:
                        continue
                    value = _pearson([sret[d] for d in dates],
                                     [bret[d] for d in dates],
                                     min_samples=min_needed)
                    if value is None:
                        continue
                    payload.append({
                        "board_code": board, "code": code,
                        "corr": round(value, 6), "samples": len(dates),
                        "start_date": dates[0], "end_date": dates[-1]})
                if progress is not None and index % 500 == 0:
                    progress(index, len(by_code), code)
            if payload:
                store.save_corr(conn, payload)
            stats.corr_pairs = len(payload)
            stats.corr_boards = len({p["board_code"] for p in payload})
            stats.seconds = time.perf_counter() - started
            stats.notes.append(
                f"走势相关性：回溯 {window} 交易日，"
                f"少于 {MIN_CORR_SAMPLES} 个共同交易日的不计"
                f"（板块自身行情不足 {MIN_CORR_SAMPLES} 日的，"
                f"门槛降到 {MIN_CORR_SAMPLES_YOUNG} 日）")
            return stats
        finally:
            wh.close()
    finally:
        conn.close()


# ======================================================================
# LLM 打分（主营业务判定）
# ======================================================================

SYSTEM_PROMPT = (
    "你是A股题材分类专家。你的任务是判断个股主营业务与概念题材是否**真的有"
    "产业关联**，只输出严格 JSON。要求：\n"
    "1. 依据公司主营的产品、技术、服务是否直接属于该题材所指的产业，"
    "或处于其明确的上游/下游；\n"
    "2. **不要**因为题材名含通用词就判为相关；\n"
    "3. **不要**因为股价走势、市场热度、板块归属就判为相关；\n"
    "4. 若公司业务只是概念边缘的间接沾边（参股、意向合作、传闻），"
    "给低分而不是直接排除。"
)

USER_PROMPT = """公司：{name}（{code}）
主营业务：{business}
所属行业：{industry}

以下是该股被归入、且**走势相关性最高**的题材（已按相关性排序）：
{candidates}

请判断每个题材与该公司主营业务的**产业关联强度**，0-100 整数：
  80-100 主营核心业务直接属于该题材
  60-79  主营明确涉及该题材，或处于其直接上下游
  40-59  业务有实质交叉，但不是主营重点（如新产品/新业务占比仍低）
  20-39  间接沾边（参股、意向、供应关系较弱）
   0-19  基本无关（蹭概念、纯市场情绪、宽泛风格）

只输出 JSON，不要任何解释，且**必须覆盖全部 {n} 个题材**：
{{"scores": [{{"name": "题材名", "score": 85, "reason": "不超过15字依据"}}]}}"""


@dataclass
class ScoreTask:
    """一只股票的打分任务（候选题材已按走势相关性排好序）。"""

    code: str
    name: str
    business: str
    industry: str
    themes: list[str]
    #: `{题材名: 该股票在该题材下的最高走势相关性}`
    corr: dict[str, float] = field(default_factory=dict)
    signature: str = ""


def _open_warehouse(path: str | Path) -> sqlite3.Connection:
    """打开行情仓的**只读**连接 —— 委托给唯一入口 `warehouse.open_warehouse`。

    ## 为什么曾经需要这个函数（保留说明，避免有人再自己开连接）

    2026-09-21 全量重打分期间，机器上有 **5 个 `uvicorn --port 8100`** 后端进程
    同时打开着 15 GiB 的 WAL 行情仓，读者会拿到 `disk I/O error`。
    本模块原来**有 4 处各自 `sqlite3.connect(..., mode=ro)`**
    （`score_universe` / `_industries` / `member_fundamentals`），没有兜底，于是：

        member_fundamentals 抛 disk I/O error
          → clean_member_map 整条抛错
          → service.py 按设计**兜底放行**（"提纯失败不该让整页 500"）
          → 龙头算法跑在**未提纯的原始同花顺成分股**上
          → 「锂电池概念」608 只成分股里，风华高科（MLCC）成了龙头

    同一类 bug 后来在 `member_pure.load_market_caps` 又出现一次，
    所以统一收进 `src/mainline/warehouse.py` —— **不要再自己开连接**。
    """
    return open_warehouse(path, timeout=60.0)


def score_universe(warehouse_path: str | Path, *,
                   since: str = "20240101",
                   exclude_st: bool = True) -> dict[str, str]:
    """打分范围：**真实 A 股 且 有日线（可选排除 ST）** → `{code: name}`。

    ## 为什么必须限制范围（实测）

    `ml_member` 的概念成分股里有 **13116 个 code，但只有 5557 个是真实 A 股**，
    其余是导出时产生的合成占位码：`00000A`…`00000J`、`000003`…（既不在
    `quant_stock_directory` 里，也没有日线）。给它们打分是纯浪费。

    三个条件缺一不可：

    1. **在 `quant_stock_directory` 里** —— 样本外的代码一律不是真实标的；
    2. **在 `since` 之后有日线** —— 长期停牌/已退市的票没有走势可比，相关性无意义；
    3. （可选）**名称不是 ST** —— 反正后续会被剔除，没必要先花钱打分。
    """
    path = Path(warehouse_path)
    if not path.exists():
        raise RelevanceError(f"行情仓不存在：{path}")
    conn = _open_warehouse(path)
    try:
        rows = conn.execute(
            "SELECT d.code, COALESCE(d.name, '') AS name"
            " FROM quant_stock_directory d"
            " WHERE EXISTS (SELECT 1 FROM quant_daily q"
            "               WHERE q.code = d.code AND q.trade_date >= ?)",
            (since,)).fetchall()
    finally:
        conn.close()
    if not exclude_st:
        return {str(r["code"]): str(r["name"]) for r in rows}
    from src.mainline.pool import is_st  # 复用已验证的 ST 判定

    return {str(r["code"]): str(r["name"]) for r in rows
            if not is_st(str(r["name"]))}


def _parse_llm_json(body: str) -> Any:
    text = (body or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) > 1:
            text = parts[1]
        text = text.removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {}


def _norm_corr(value: float | None, values: Sequence[float]) -> float:
    """把相关性映射到 0-1（按本股票候选集内的 min-max）。

    用**本股票内部**的极值而不是全市场绝对阈值：不同股票的板块相关性分布
    宽窄差别很大，绝对阈值会让"板块普遍温和相关"的股票全都算低分。
    """
    if value is None or not values:
        return 0.0
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


async def _score_one(gateway: Any, task: ScoreTask, *, tier: str, top: int,
                     sem: asyncio.Semaphore | None,
                     force: bool = False) -> list[dict[str, Any]]:
    """给一只股票打分：候选题材 → 最终前 N 个（相关性 60% + 主营 40%）。

    `sem=None` 表示由调用方自行控制并发（`score_stocks` 用工作池时的走法）。
    解析不出条目时返回**空列表**（把"空响应算不算失败"的判断留给调用方：
    `_score_one_nowait` 当失败，测试与调试路径当空结果）。
    `force=True` 穿透 LLM 缓存（见 `_score_body`）。
    """
    if sem is not None:
        async with sem:
            return await _score_body(gateway, task, tier=tier, top=top,
                                     force=force) or []
    return await _score_body(gateway, task, tier=tier, top=top,
                             force=force) or []


async def _score_body(gateway: Any, task: ScoreTask, *, tier: str,
                      top: int, force: bool = False) -> list[dict[str, Any]] | None:
    """打分主体。返回 `None` 表示**解析不出任何条目**（调用方据此决定是否重试）。

    `force=True` 时 `use_cache=False` —— 这条很关键：本模块的 prompt 是
    **确定性**的（只含公司名/代码/主营文本/题材集），所以磁盘缓存里躺着上一次
    的答案。重打分（`score_stocks(force=True)`）如果不穿透到网关，
    就会"全部命中缓存"→ **重打分等于什么都没做**，前端看到的是旧结果。
    """
    corr_values = list(task.corr.values())
    candidates = "\n".join(
        f"  {index + 1}. {theme}"
        f"（走势相关性 {task.corr.get(theme, 0.0):+.2f}）"
        for index, theme in enumerate(task.themes))
    prompt = USER_PROMPT.format(
        name=task.name or task.code, code=task.code,
        business=(task.business or "（无主营描述）")[:BUSINESS_CHARS],
        industry=task.industry or "（未标注）",
        candidates=candidates, n=len(task.themes))
    resp = await gateway.complete(
        tier, SYSTEM_PROMPT, prompt, agent_id="mainline_relevance",
        json_mode=True, max_tokens=SCORE_MAX_TOKENS,
        use_cache=not force, cache_ttl_hours=RELEVANCE_CACHE_TTL_HOURS)
    data = _parse_llm_json(resp.content or "")
    entries = data.get("scores") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not entries:
        return None
    # 题材名 → 主营分（LLM 偶尔用原名而非归一化名，两边都收）
    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("name") or "").strip()
        if not raw:
            continue
        try:
            score = float(entry.get("score"))
        except (TypeError, ValueError):
            continue
        payload = {"score": max(0.0, min(100.0, score)),
                   "reason": str(entry.get("reason") or "")[:60]}
        by_name[raw] = payload
        by_name.setdefault(normalize_theme(raw), payload)

    scored: list[dict[str, Any]] = []
    for theme in task.themes:
        hit = by_name.get(theme)
        if hit is None:
            continue      # LLM 漏答的题材不猜分，直接不作为候选
        business = float(hit["score"])
        corr = task.corr.get(theme)
        final = (CORR_WEIGHT * 100.0 * _norm_corr(corr, corr_values)
                 + (1.0 - CORR_WEIGHT) * business)
        scored.append({
            "theme": theme, "raw_name": theme, "business_score": business,
            "corr": corr, "final_score": round(final, 2),
            "reason": str(hit["reason"] or "")})
    # 主营分为 0 视为"基本无关"：即使相关性最高也不进前 N（防事件型巧合）
    usable = [item for item in scored if item["business_score"] > 0]
    usable.sort(key=lambda item: (-item["final_score"], item["theme"]))
    picked = [{"rank": index + 1, **item}
              for index, item in enumerate(usable[:top])]
    if not picked:
        # 打分有结果、但该股与**任何**题材都无实质关联（主营分全 0 或全漏答）。
        # 写一条占位记录，让"确实都不相关"与"这次调用失败了"在库里可区分 ——
        # 否则两者都表现为"没有记录"，每次重跑都会重算这些不可能有结果的股票。
        return [{"rank": 0, "theme": MISSING_THEME, "raw_name": "",
                 "business_score": 0.0, "corr": None, "final_score": 0.0,
                 "reason": "与所有候选题材均无实质关联"}]
    return picked


def _industries(warehouse_path: str | Path | None,
                codes: Iterable[str]) -> dict[str, str]:
    """从行情仓取个股所属行业（`quant_stock_basic.industry`），作为 prompt 的补充上下文。"""
    if not warehouse_path:
        return {}
    path = Path(warehouse_path)
    if not path.exists():
        return {}
    wanted = sorted({str(c) for c in codes if c})
    if not wanted:
        return {}
    conn = _open_warehouse(path)
    try:
        out: dict[str, str] = {}
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT code, industry FROM quant_stock_basic"
                f" WHERE code IN ({marks})", chunk).fetchall()
            for row in rows:
                out[str(row["code"])] = str(row["industry"] or "")
        return out
    finally:
        conn.close()


def build_tasks(*, store: RelevanceStore,
                warehouse_path: str | Path | None = None,
                allowed: Iterable[str] | None = None,
                max_candidates: int = MAX_CANDIDATES) -> list[ScoreTask]:
    """构造逐股票的打分任务：候选题材按走势相关性降序、只取前 N 个。

    ## `allowed`：打分范围（强烈建议传）

    实测 `ml_member` 的概念成分股里有 **13116 个 code，但只有 5557 个是真实
    A 股** —— 其余是导出时产生的合成占位码（`00000A`…`00000J`、`000003`…），
    既不在 `quant_stock_directory` 里，也没有日线。给它们打分纯属浪费调用。

    调用方应传「真实 A 股 且 非 ST 且 有日线」的集合（见 `score_universe()`）。

    ## 只把**能映射到板块的题材**交给 LLM（关键）

    ⚠️ 候选集必须是**产业题材**，不能把申万行业指数混进来 —— 否则前 3 名额会
    被浪费掉。实测两个反例：

        中芯国际  前 3 = 集成电路制造 / 半导体产品 / 半导体产品与设备（全是行业分类）
                  → 保留 0 个概念板块，"芯片概念"这个最该留的反而被挤出去了
        三花智控  前 3 里"通用设备制造业指数"占一席
                  → "机器人概念"被挤掉

    所以这里用 `ml_theme_board` 做过滤：**只保留能映射到至少一个板块的题材**。
    行业分类（`申万`）不在这个映射表里，自然被排除，名额就留给了产业题材。

    ## 候选题材为什么只取前 `max_candidates` 个

    1. 省 token（一只股可属 50 个题材，降到 20 个）；
    2. 相关性低的题材本来就不可能进前 N，让 LLM 判它们只增加噪声与漏答概率。

    `signature` 由「主营文本 + 候选题材集」哈希而成：任一变化都会导致重算，
    两个输入都没动时重跑直接命中缓存（免费）。
    """
    wanted = {str(c) for c in allowed} if allowed is not None else None
    conn = store.connect()
    try:
        business = store.business_map(conn)
        corr = store.corr_map(conn)
        theme_of_board = {
            str(r["board_code"]): str(r["theme"])
            for r in conn.execute(
                "SELECT board_code, theme FROM ml_theme_board")}
        # 能映射到板块的题材白名单：行业分类不在其中，会被自然排除
        mappable = {str(r["theme"]) for r in conn.execute(
            "SELECT DISTINCT theme FROM ml_theme_board")}
        rows = conn.execute("SELECT board_code, code FROM ml_member").fetchall()
        # 股票 → 题材 → 最高相关性（同一题材挂在多个板块时取最大）
        candidates: dict[str, dict[str, float]] = {}
        for row in rows:
            theme = theme_of_board.get(str(row["board_code"]))
            if not theme or theme not in mappable:
                continue
            code = str(row["code"])
            if wanted is not None and code not in wanted:
                continue
            value = (corr.get(code) or {}).get(str(row["board_code"]))
            if value is None:
                continue
            slot = candidates.setdefault(code, {})
            if value > slot.get(theme, -9.0):
                slot[theme] = value
        industries = _industries(warehouse_path, candidates)

        tasks: list[ScoreTask] = []
        for code, themes in candidates.items():
            ordered = sorted(themes.items(), key=lambda kv: (-kv[1], kv[0]))
            ordered = ordered[:max_candidates]
            names = [theme for theme, _ in ordered]
            name, text = business.get(code, ("", ""))
            tasks.append(ScoreTask(
                code=code, name=name, business=text,
                industry=industries.get(code, ""), themes=names,
                corr=dict(ordered),
                signature=_signature(code, text, names)))
        tasks.sort(key=lambda item: item.code)
        return tasks
    finally:
        conn.close()


def _signature(code: str, business: str, themes: Sequence[str]) -> str:
    """缓存签名：主营文本或候选题材集变了就必须重算。"""
    body = "\x1f".join([code, business or "", *themes])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


async def score_stocks(*, store: RelevanceStore, gateway: Any,
                       tasks: Sequence[ScoreTask], tier: str = "decision",
                       top: int = TOP_THEMES, concurrency: int = 10,
                       force: bool = False, stall_seconds: float = 300.0,
                       retries: int = 3, retry_pause: float = 1.0,
                       progress: Any = None, cost_guard: Any = None) -> CleanStats:
    """批量打分并落库。签名一致就跳过（重跑免费）。

    ## 为什么用"工作池"而不是分批 `gather`

    分批写法（每批 N 个、`gather` 后进下一批）有个**屏障代价**：每批都要等
    最慢的那一只，而延迟分布很散（实测 decision 层 min 26.5s / max 55.2s，
    2 倍差距）—— 每批 10 个就要空等几十秒，累计起来是数小时级的浪费。

    工作池让 N 个消费者持续从队列取任务，慢的那只只占住它自己的槽位，
    其余槽位照常推进。

    ## 卡死守卫（`stall_seconds`）

    实测踩过：某次 4996 只的重打分在跑到 ~3900 只时**静默卡住** —— 进程还在
    （占着 CPU），但数据库一个多小时没有新写入，而当时的 LLM 实测延迟只有
    3.7 秒。没有守卫就只能靠人盯着看，或者一直等到天荒地老。

    所以每完成一只就更新心跳；若连续 `stall_seconds` 秒没有任何完成，
    判定为卡死并抛 `RelevanceError`，把已落库的部分保住、让人重跑续算
    （缓存命中，不重复花钱）。设 0 关闭守卫。

    ## 落库为什么在"每次完成后立即"

    不是攒批写：中断（Ctrl+C / 进程被杀）时已完成的必须留住，
    否则重跑要从头再烧一遍钱。单只失败只记日志、不中断整轮，
    下一次重跑会自动补上它（因为没落库）。

    ## 重试（`retries`）

    ⚠️ **必须重试**：`reasoning`（deepseek-flash）实测会**偶发返回空响应** ——
    同一 prompt 立刻重发就正常。实测有一次 1027 只集中返回空，而同一批 prompt
    换 `decision`（deepseek-v4-pro）全部正常返回。即提供商抖动，不是 prompt 的问题。

    没有重试的后果很隐蔽：这些股票**既没落库、也不会被标记失败**，
    在库里表现为"没有记录"（茅台就这么丢过一次记录，而它显然该有"白酒"）。

    `retries=0` 关闭重试（仅调试用）。

    ## 成本护栏（`cost_guard`）

    传一个 `src.core.budget.ScriptCostGuard` 进来后，**每取一只股票之前**
    先问它一次；超限就抛 `ScriptCostError` 中止整轮。为什么护栏放在这一层
    而不是调用方：调用方（`scripts/mainline_relevance.py`）在入口只能按
    **预计**花费拦一次，而真实花费取决于返回的 token 数 —— 实测同一条 prompt
    的输出长度能差几倍，"预计没问题"不等于"跑完没问题"。

    检查点在**取任务之前**、且在重试的 `try` 之外：放在里面会让
    `except Exception` 把它当成"这只股票调用失败"而**重试三次**，
    把一个"该停下来"的信号变成三次无谓的调用。

    中止时已完成的部分**已经落库**（每次完成即写），直接重跑同一命令
    会命中缓存签名、不重复计费。
    """
    stats = CleanStats()
    conn = store.connect()
    try:
        cached = {} if force else store.cached_signatures(conn)
        pending = [t for t in tasks
                   if cached.get(t.code) != t.signature or not t.signature]
        stats.stocks_cached = len(tasks) - len(pending)
        total = len(tasks)
        workers = max(1, min(int(concurrency), len(pending) or 1))
        logger.info("相关性打分：待打分 %d 只（缓存命中 %d，共 %d），并发 %d，"
                    "重试 %d 次", len(pending), stats.stocks_cached, total,
                    workers, max(0, retries))
        if not pending:
            return stats

        index = 0
        lock = asyncio.Lock()
        #: 心跳：最后一次"有任务完成"的时间点
        beat = {"at": time.monotonic(), "done": 0}
        #: 所有 worker 真正退出时置位（守卫据此退出，见 guard 的说明）
        drained = asyncio.Event()

        async def worker() -> None:
            nonlocal index
            try:
                while True:
                    async with lock:
                        if index >= len(pending):
                            return
                        task = pending[index]
                        index += 1
                    if cost_guard is not None:
                        # ⚠️ 必须在重试循环**之外**：放进去会被当成
                        # "这只股票调用失败"重试三次，把停下来的信号变成花费。
                        cost_guard.check_running()
                    picked: list[dict[str, Any]] | None = None
                    last = ""
                    for attempt in range(max(0, retries) + 1):
                        try:
                            picked = await _score_one_nowait(gateway, task,
                                                             tier=tier, top=top,
                                                             force=force)
                            break
                        except Exception as exc:  # noqa: BLE001 重试后仍失败才计
                            last = brief(exc, BRIEF_DEFAULT)
                            if attempt < max(0, retries):
                                await asyncio.sleep(retry_pause * (attempt + 1))
                    if picked is None:
                        stats.stocks_failed += 1
                        logger.warning("打分失败 %s（重试 %d 次后仍失败）：%s",
                                       task.code, max(0, retries), last)
                        beat["at"] = time.monotonic()
                        continue
                    store.save_scores(conn, task.code, picked, model=tier,
                                      signature=task.signature)
                    stats.stocks_scored += 1
                    beat.update(at=time.monotonic(),
                                done=stats.stocks_scored)
                    if progress is not None:
                        progress(stats.stocks_scored + stats.stocks_cached,
                                 total, task.code)
            finally:
                # ⚠️ 用"全部 worker 退出"而不是"任务全被取走（index 到底）"
                # 作为收尾信号：`index` 到底只说明活**派完了**，worker 可能还在
                # 跑最后几只。实测就栽在这里 —— 守卫看到 index 到底立刻退出，
                # 此后若 worker 卡住就**再也没有人看守**，整轮永久挂起
                # （`_probe_guard5` 精确复刻了这个场景）。
                if index >= len(pending):
                    drained.set()

        async def guard() -> None:
            """卡死守卫：心跳超过 stall_seconds 且仍有活没跑完时中止整轮。

            退出条件必须是 `drained`（worker 全部结束），**不能**用
            `index >= len(pending)`：那只表示活派完了，worker 可能正卡在
            最后几只的调用上 —— 守卫若此时退出，卡死就没人管了。

            检查间隔取 `stall_seconds / 10` 并夹在 [0.05, 30] 秒：
            **不能设 5 秒下限**（第一版就是"至少 sleep 5 秒"），
            那会让 `stall_seconds < 5` 的调用永远等不到检查点。
            """
            interval = min(30.0, max(0.05, stall_seconds / 10.0))
            while True:
                try:
                    await asyncio.wait_for(drained.wait(), timeout=interval)
                    return          # worker 都结束了，守卫使命完成
                except asyncio.TimeoutError:
                    pass
                idle = time.monotonic() - beat["at"]
                if idle > stall_seconds:
                    raise RelevanceError(
                        f"打分卡死：已 {idle:.0f} 秒没有任何任务结束"
                        f"（已完成 {stats.stocks_scored} 只、失败 "
                        f"{stats.stocks_failed} 只、已派发 {index}/"
                        f"{len(pending)}）。已落库的部分保留，"
                        f"直接重跑同一命令即可续算（命中缓存不重复计费）")

        # ⚠️ 并发编排的两个坑（都实测踩过）：
        #
        # 1. **不能用 `asyncio.gather(..., return_exceptions)`**：守卫抛出
        #    RelevanceError 时 gather 会立刻把异常传出，但其余 worker 任务仍挂在
        #    事件循环里，而 `asyncio.run()` 退出前会等待所有 pending 任务 ——
        #    整个进程永久挂起（实测 pytest 直接超时 10 分钟）。
        # 2. **收尾不能只靠"取消未完成的"**：worker 全部正常结束后，`runners` 里的
        #    守卫任务还在空转（它要等下一个 sleep 周期才看到 index 派完），
        #    于是 `await gather(*runners)` 会一直等它 —— 同样卡住。
        #
        # 正确做法：用 `wait(..., FIRST_COMPLETED)` 循环，一旦**非守卫**的任务
        # 全部结束就主动取消守卫；守卫抛异常则取消所有 worker 后重新抛出。
        workers_runners = [asyncio.ensure_future(worker()) for _ in range(workers)]
        guard_runner = (asyncio.ensure_future(guard())
                        if stall_seconds > 0 else None)
        all_runners = workers_runners + ([guard_runner] if guard_runner else [])

        async def shutdown() -> None:
            for item in all_runners:
                if not item.done():
                    item.cancel()
            await asyncio.gather(*all_runners, return_exceptions=True)

        try:
            while True:
                pending_tasks = [t for t in all_runners if not t.done()]
                if not pending_tasks:
                    break
                done, _ = await asyncio.wait(
                    pending_tasks, return_when=asyncio.FIRST_COMPLETED)
                failure = None
                for item in done:
                    if item.cancelled():
                        continue
                    exc = item.exception()
                    if exc is not None and failure is None:
                        failure = exc
                if failure is not None:
                    await shutdown()
                    raise failure
                if all(item.done() for item in workers_runners):
                    # worker 干完了：守卫没事可做，取消它再收尾
                    if guard_runner is not None and not guard_runner.done():
                        guard_runner.cancel()
                    break
        finally:
            await shutdown()
        if stats.stocks_failed:
            stats.notes.append(
                f"{stats.stocks_failed} 只打分失败（重跑会自动补算）")
        return stats
    finally:
        conn.close()


async def _score_one_nowait(gateway: Any, task: ScoreTask, *, tier: str,
                            top: int,
                            force: bool = False) -> list[dict[str, Any]]:
    """`_score_one` 的无信号量版本（并发由 `score_stocks` 的工作池控制）。

    ⚠️ 这里把"**解析不出任何条目**"当成失败抛出，而不是当成"该股无相关题材"：

    实测踩过 —— LLM 偶发返回空响应（同一个 prompt 重试就正常），当时被当成
    合法结果，于是**既没落库、也不会重试**，那只股票静默丢失
    （茅台就是这么没有记录的，而它显然该有"白酒"）。

    判据的分界很清楚：

        解析不出条目（空响应/坏 JSON）→ 失败 → 不落库 → 下次重跑自动补
        解析出了条目但主营分全为 0 → 合法结果 → 落库（记录"确实都不相关"）

    后者会写入 `<MISSING>` 占位行，见 `_score_body`。
    """
    body = await _score_body(gateway, task, tier=tier, top=top, force=force)
    if body is None:
        raise RelevanceError(
            f"{task.code}：LLM 未返回可解析的评分（空响应或坏 JSON）")
    return body


# ======================================================================
# 相关性落库
# ======================================================================

def rebuild_clean(*, store: RelevanceStore) -> CleanStats:
    """把「题材前 N」落成 `ml_member_clean`（逐 (板块,股票) 的相关性结论）。

    **只写相关性，不写市值** —— 市值必须在运行期按 `trade_date` 现算（见模块
    docstring 的时点设计）。耗时 1~2 秒（无 LLM 调用）。
    """
    started = time.perf_counter()
    stats = CleanStats()
    conn = store.connect()
    try:
        stats.theme_boards = store.rebuild_theme_boards(conn)
        total_boards = int(conn.execute(
            "SELECT COUNT(*) FROM ml_board").fetchone()[0])
        mapped = int(conn.execute(
            "SELECT COUNT(DISTINCT board_code) FROM ml_theme_board"
        ).fetchone()[0])
        stats.boards_fake_excluded = total_boards - mapped
        board_of = store.theme_board_map(conn)
        stats.themes = len(board_of)

        rows = conn.execute(
            "SELECT code, rank, theme FROM ml_stock_theme ORDER BY code, rank"
        ).fetchall()
        relevant: dict[tuple[str, str], tuple[int, str]] = {}
        for row in rows:
            code, theme = str(row["code"]), str(row["theme"])
            for board in board_of.get(theme, []):
                key = (board, code)
                prev = relevant.get(key)
                if prev is None or int(row["rank"]) < prev[0]:
                    relevant[key] = (int(row["rank"]), theme)

        pairs = conn.execute(
            "SELECT m.board_code, m.code FROM ml_member m "
            "JOIN ml_theme_board t ON t.board_code = m.board_code").fetchall()
        stats.pairs_before = len(pairs)
        stamp = _now()
        payload: list[tuple] = []
        for pair in pairs:
            board, code = str(pair["board_code"]), str(pair["code"])
            hit = relevant.get((board, code))
            rank = hit[0] if hit else None
            theme = hit[1] if hit else ""
            if rank is not None:
                stats.pairs_after += 1
            else:
                stats.dropped_irrelevant += 1
            payload.append((board, code, 1 if rank is not None else 0, rank,
                            theme, stamp))
        conn.execute("DELETE FROM ml_member_clean")
        if payload:
            conn.executemany(
                "INSERT INTO ml_member_clean(board_code, code, relevant,"
                " rank_in_stock, theme, refreshed_at) VALUES(?,?,?,?,?,?)",
                payload)
        conn.commit()
        stats.seconds = time.perf_counter() - started
        stats.notes.append("ml_member_clean 只含相关性结论，市值门槛在运行期判定")
        return stats
    finally:
        conn.close()


# ======================================================================
# 运行期：按市值门槛 + ST 过滤成分股池
# ======================================================================

def resolve_basic_date(warehouse_path: str | Path,
                       trade_date: str) -> tuple[str, bool]:
    """把请求日解析成 `quant_daily_basic` 里**真有市值数据的最近交易日**。

    返回 `(生效日期, 是否发生了回退)`。

    为什么需要这一步：`quant_daily_basic` 常比行情主表晚一天（2026-09-21
    实测：请求 `20260918`，仓库最新只到 `20260917`）。而
    `member_fundamentals` 是按等值 `trade_date` 查的，查不到就整批返回
    `None`，紧接着 `mv_ok = cap is None or cap >= min_total_mv` 会**静默
    放行所有小票** —— 市值门槛实际失效，却既不报错也不出现在 gaps 里。
    「数据缺失」被伪装成「全部合格」是提纯里最危险的一类静默降级，
    所以这里显式回退，并让调用方把回退写进 gap 说明。
    """
    if not trade_date:
        return trade_date, False
    path = Path(warehouse_path)
    if not path.exists():
        raise RelevanceError(f"行情仓不存在：{path}")
    conn = _open_warehouse(path)
    try:
        exact = conn.execute(
            "SELECT 1 FROM quant_daily_basic WHERE trade_date = ? LIMIT 1",
            (trade_date,)).fetchone()
        if exact is not None:
            return trade_date, False
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM quant_daily_basic"
            " WHERE trade_date <= ?", (trade_date,)).fetchone()
        if row is None or row["d"] is None:
            return trade_date, False
        return str(row["d"]), True
    finally:
        conn.close()


def member_fundamentals(warehouse_path: str | Path, codes: Sequence[str],
                        trade_date: str) -> dict[str, tuple[float | None, str]]:
    """取成分股的 `总市值` 与 `股票名称`：`{code: (total_mv, name)}`。

    一次查询同时拿到市值与名称（两者都按**运行日**取，不是离线快照）：

    - 市值用于剔小票（`min_total_mv`）；
    - 名称用于剔 ST / 退市股（`exclude_st`）—— ST 的状态会随年份变化
      （"ST 舍得"后来摘帽），所以必须用运行日的名称，不能离线写死。

    名称优先取 `quant_stock_basic.name`（权威、全量），缺失时回落到
    `quant_stock_directory.name`。市值缺失的 code 仍会在返回值里（值为 None），
    调用方据此判断"数据缺失"而不是"不合格"。
    """
    if not codes or not trade_date:
        return {}
    path = Path(warehouse_path)
    if not path.exists():
        raise RelevanceError(f"行情仓不存在：{path}")
    conn = _open_warehouse(path)
    try:
        out: dict[str, tuple[float | None, str]] = {}
        unique = sorted({str(c) for c in codes if c})
        for start in range(0, len(unique), 400):   # SQLite 变量上限 999
            chunk = unique[start:start + 400]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT code, total_mv FROM quant_daily_basic"
                f" WHERE trade_date = ? AND code IN ({marks})",
                [trade_date, *chunk]).fetchall()
            for row in rows:
                value = row["total_mv"]
                out[str(row["code"])] = (
                    float(value) if value is not None else None, "")
            names: dict[str, str] = {}
            for table in ("quant_stock_basic", "quant_stock_directory"):
                need = [c for c in chunk if not names.get(c)]
                if not need:
                    break
                marks = ",".join("?" * len(need))
                try:
                    rows = conn.execute(
                        f"SELECT code, name FROM {table}"
                        f" WHERE code IN ({marks})", need).fetchall()
                except sqlite3.OperationalError:
                    continue
                for row in rows:
                    if row["name"]:
                        names.setdefault(str(row["code"]), str(row["name"]))
            for code in chunk:
                cap = out.get(code, (None, ""))[0]
                out[code] = (cap, names.get(code, ""))
        return out
    finally:
        conn.close()


@dataclass
class CleanOutcome:
    """运行期过滤的结果与说明（供面板/日志解释"为什么少了这么多票"）。"""

    members: dict[str, list[str]] = field(default_factory=dict)
    detail: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled: bool = False
    trade_date: str = ""
    note: str = ""
    gaps: list[str] = field(default_factory=list)

    @property
    def before_total(self) -> int:
        return sum(item["before"] for item in self.detail.values())

    @property
    def after_total(self) -> int:
        return sum(item["after"] for item in self.detail.values())

    @property
    def dropped(self) -> dict[str, int]:
        """按原因汇总剔除数（跨板块）。一个 pair 可能同时命中多个原因。"""
        out = {"mv": 0, "st": 0, "irrelevant": 0}
        for item in self.detail.values():
            out["mv"] += int(item.get("dropped_mv") or 0)
            out["st"] += int(item.get("dropped_st") or 0)
            out["irrelevant"] += int(item.get("dropped_irrelevant") or 0)
        return out

    def summary(self) -> str:
        if not self.enabled:
            return "成分股提纯未启用（使用板块全部成分股）"
        if not self.before_total:
            return "成分股提纯：无可过滤的成分股"
        parts = self.dropped
        reason = "、".join(
            f"{label} {parts[key]}"
            for key, label in (("mv", "市值不足"), ("st", "ST/退市"),
                               ("irrelevant", "相关性不足")) if parts[key])
        return (f"成分股提纯 @{self.trade_date}：{self.before_total} → "
                f"{self.after_total} 只"
                f"（保留 {self.after_total / self.before_total * 100:.0f}%"
                + (f"；剔除 {reason}" if reason else "") + "）")


def clean_member_map(member_map: dict[str, list[str]], *,
                     store: RelevanceStore,
                     warehouse_path: str | Path,
                     trade_date: str,
                     kinds: Sequence[str] = ("concept",),
                     min_total_mv: float = MIN_TOTAL_MV,
                     exclude_st: bool = True,
                     top: int = TOP_THEMES) -> CleanOutcome:
    """按「相关性前 N + 总市值门槛 + 排除 ST」过滤成分股池。

    ## 为什么按板块类型（`kinds`）而不是"全部板块"

    实测 `ml_board.kind` 只有两种：`concept` 2205 个（同花顺概念）与
    `sw_l1` 31 个（申万一级行业）。后者是**行业分类**不是题材，成分股按行业
    归类本来就是"主业归属"，套用"前 3 题材"会把一个 300 只成分股的行业砍到
    只剩几十只，纯粹是数据损失。因此默认只过滤 `concept`，`sw_l1` 原样保留。

    ## 三条独立的剔除理由

    1. **相关性不足**：该题材不在这只股票自身的前 N 个相关题材里；
    2. **市值不足**：总市值 < `min_total_mv`；
    3. **ST / 退市**：ST 股涨跌幅限制（±5%）与正常股不同，资金动作与板块主线
       无关。复用 `pool.is_st`（已处理「STAR股份」被误判成 ST 这类边界情况）。

    三者**分别计数**（`dropped_mv` / `dropped_st` / `dropped_irrelevant`），
    便于回答"这些票到底因为什么被剔掉"。

    ## 保守性

    - 某板块**没有**任何相关性记录（未打分/不在 `ml_theme_board`）→ 原样保留，
      并在 `gaps` 里如实标注。数据缺口不该被当成"全部不合格"而静默清空板块。
    - 市值缺失（新股/停牌）→ **保守保留**，不等于"不合格"。但请求日整个
      没有市值数据时会先回退到最近交易日（`resolve_basic_date`）并在
      `gaps` 里记录，避免"全都没有"退化成"全都合格"。
    - 名称缺失 → 无法判定 ST，**保守保留**。
    """
    from src.mainline.pool import is_st  # 复用已验证的 ST 判定（含边界情况）

    cfg_kinds = {str(k) for k in kinds}
    outcome = CleanOutcome(
        enabled=True, trade_date=str(trade_date), members=dict(member_map))
    if not member_map:
        return outcome

    conn = store.connect()
    try:
        kinds_of = store.board_kinds(conn)
        # `scored` = **至少有一个成分股被判定为相关**的板块。
        #
        # ⚠️ 不能用"在 ml_member_clean 里有行"当判据：`rebuild_clean` 会给
        # **每个**话题板块都写行（含 relevant=0 的），所以那样写会让
        # 「整板被剔空」和「这个板块从没打过分」两种情况无法区分，
        # 而它们的正确处置正好相反 —— 前者应当剔空（这是提纯结果），
        # 后者应当原样保留（这是数据缺口，不该被静默清空）。
        scored = {str(r["board_code"]) for r in conn.execute(
            "SELECT DISTINCT board_code FROM ml_member_clean WHERE relevant = 1")}
        rel: dict[tuple[str, str], tuple[int, str]] = {}
        for row in conn.execute(
                "SELECT board_code, code, rank_in_stock, theme"
                " FROM ml_member_clean WHERE relevant = 1"):
            rel[(str(row["board_code"]), str(row["code"]))] = (
                int(row["rank_in_stock"] or 0), str(row["theme"] or ""))
    finally:
        conn.close()

    scoped = [code for code in member_map
              if kinds_of.get(code, "") in cfg_kinds]
    outside = [code for code in member_map if code not in scoped]
    if outside:
        outcome.gaps.append(
            f"{len(outside)} 个板块不在过滤范围（{'/'.join(kinds) or '无'}），"
            "成分股原样保留")
    missing = [code for code in scoped if code not in scored]
    if missing:
        outcome.gaps.append(
            f"{len(missing)} 个板块没有相关性数据（未提纯，原样保留）")

    wanted = sorted({str(c) for code in scoped for c in member_map[code]})
    # ⚠️ 先解析"真有市值数据的交易日"：否则请求日落在仓库覆盖之外时，
    # 下面 `mv_ok` 会因为 cap 全为 None 而静默放行（详见 resolve_basic_date）。
    effective_date, fell_back = resolve_basic_date(warehouse_path, trade_date)
    if fell_back:
        outcome.gaps.append(
            f"{trade_date} 无总市值数据，已回退到最近交易日 {effective_date} "
            "的市值口径（否则市值门槛会静默失效）")
    facts = member_fundamentals(warehouse_path, wanted, effective_date)
    if wanted and not any(item[0] is not None for item in facts.values()):
        outcome.gaps.append(
            f"{trade_date} 无总市值数据（市值门槛未生效，仅按相关性 / ST 过滤）")

    for board, members in member_map.items():
        codes = [str(c) for c in members if c]
        if board not in scoped or board not in scored:
            outcome.members[board] = codes
            outcome.detail[board] = {"before": len(codes), "after": len(codes),
                                     "kept": codes, "dropped": [],
                                     "reason": "不在过滤范围"}
            continue
        kept: list[str] = []
        dropped: list[str] = []
        dropped_mv = dropped_st = dropped_rel = 0
        for code in codes:
            rel_ok = (board, code) in rel
            cap, name = facts.get(code, (None, ""))
            mv_ok = cap is None or cap >= min_total_mv
            st_ok = not (exclude_st and is_st(name))
            if rel_ok and mv_ok and st_ok:
                kept.append(code)
                continue
            dropped.append(code)
            if not rel_ok:
                dropped_rel += 1
            if not mv_ok:
                dropped_mv += 1
            if not st_ok:
                dropped_st += 1
        outcome.members[board] = kept
        outcome.detail[board] = {
            "before": len(codes), "after": len(kept), "kept": kept,
            "dropped": dropped, "dropped_mv": dropped_mv,
            "dropped_st": dropped_st, "dropped_irrelevant": dropped_rel,
            "reason": (f"相关性前 {top} 题材 + 总市值≥{min_total_mv / 1e8:.0f}亿"
                       + ("（排除 ST）" if exclude_st else ""))}
    outcome.note = outcome.summary()
    return outcome


__all__ = [
    "BUSINESS_CHARS",
    "CORR_WINDOW",
    "CleanOutcome",
    "CleanStats",
    "FAKE_BOARD_PATTERNS",
    "MAX_CANDIDATES",
    "MIN_CORR_SAMPLES",
    "MIN_CORR_SAMPLES_YOUNG",
    "MIN_TOTAL_MV",
    "MISSING_THEME",
    "PROTECTED_KEYWORDS",
    "RelevanceError",
    "RelevanceStore",
    "SCORE_MAX_TOKENS",
    "ScoreTask",
    "TOP_THEMES",
    "build_tasks",
    "clean_member_map",
    "compute_correlations",
    "is_fake_board",
    "member_fundamentals",
    "normalize_theme",
    "rebuild_clean",
    "score_stocks",
    "sync_business",
]
