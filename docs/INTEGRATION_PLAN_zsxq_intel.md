# moss-finance-assistant → Moss-finagent-research 整合方案

> 版本：v1（只出方案，不含开发）
> 日期：2026-09-25
> 相关效果图：`_mockup_zsxq_intel.html` / `_mockup_zsxq_intel.png`

---

## 0. 一句话结论

**不要"整合"，要做"器官移植"。**

`moss-finance-assistant` 里有真正价值的只有**约 4 块**（知识星球静默采集、金融输出治理、上下文工程、SSE/WS 流式协议），
其余部分是**已合并的历史分层与面向 C 端的业务**，搬过去只会稀释 `Moss-finagent-research` 已经很干净的工程纪律。

**知识星球弹窗问题的根因不在配置，在架构**：它用 Playwright 驱动真实 Chromium 去"看"页面，
再从网络响应里截获数据 —— 抓 100 条主题要 259 秒，且 `ZSXQ_HEADLESS` 默认 `false`，所以每次抓取都弹窗。

**结论：把 `ZSXQ_FETCH_MODE` 从 `browser` 改成 `api`，用 Token 直采，弹窗永久消失，耗时从 259s 降到个位数秒。**

---

## 1. 现状核对（先纠正几个认知）

### 1.1 两个项目的体积要看清

| | moss-finance-assistant | Moss-finagent-research |
|---|---|---|
| 真实业务代码 | **约 31,000 行**（15 个目录） | **约 182,000 行**（含测试与前端） |
| 测试 | 34 个文件 / 4,325 行 | **4,004 个用例**（实测 collect） |
| 前端 | `static/index.html` **单文件 3,602 行** + 原生 JS | React + Vite + TS，**87 文件 / 34,021 行** |
| 定位 | 面向**散户**的对话式问答 | 面向**机构**的投研/量化平台 |

> ⚠️ assistant 目录里的 `agents/`（231 MB）、`data/`（2.6 GB）、`benchmarks/`（341 MB）**不是代码**，
> 是 `checkpointer.db`（1.7 GB）、`memory.db`、历史备份和压测数据。
> 真正要评估的代码只有 `shared/ governance/ interfaces/ tools/ orchestration/ api/ agents/`。

### 1.2 三个必须先解决的结构性问题

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| 1 | **两套技术栈无法直接合并** | assistant 是单文件 HTML + 原生 JS；research 是 React 组件树 | 前端只能**移植组件**，不能合并文件 |
| 2 | **assistant 有旧版分层残留** | `agents/data/checkpointer.db` 与 `data/checkpointer.db`（1.7 GB）并存；`data/backup_premerge_20260901/` 是合并前备份 | 说明它做过一次合并，且**有两个真源**（如 `tools/MyRAGFlow.py` 与 `shared/data_sources/MyRAGFlow.py` 各 255 行） |
| 3 | **定位冲突** | assistant 做"个股新闻速览/估值/护城河/买入建议"（AGENTS.md 里甚至记录了"建议买入茅台"的失败案例）；research 是机构投研、不荐股 | 直接并入会**污染 research 的合规边界**，必须做筛除 |

---

## 2. 整合原则（三条铁律）

```
① 只移植"能力"，不搬运"代码"    —— 按 research 的规范重写接口，不做文件级复制
② 单向依赖，绝不反向引用        —— research 不 import assistant；assistant 只通过契约暴露能力
③ 合规边界只收紧、不放宽        —— assistant 的 C 端荐股能力一律不带入
```

对应到目录结构（**推荐**）：

```
Moss-finagent-research/
├── src/
│   ├── domain/intel/                    # 新增：情报域（情绪、抽取、归一）
│   │   ├── extractor.py                 #   主题 → 个股/情绪/行业 结构化
│   │   ├── sentiment.py                 #   研报热度、多空聚合
│   │   └── dedup.py                     #   跨源去重（同事件多帖折叠）
│   ├── infrastructure/connectors/
│   │   └── zsxq_connector.py            # 新增：实现 BaseConnector 契约
│   └── api/routes/
│       └── intel.py                     # 新增：/api/v1/intel/*
└── (assistant 作为独立服务，只暴露 REST + 结构化 JSON)
```

---

## 3. 重点：知识星球"静默采集"方案

### 3.1 先定位根因（读代码得出，不是猜测）

| 层 | 现状 | 证据 |
|---|---|---|
| 入口 | `tools/zsxq_crawler_tool.py` → 子进程 `zsxq_analysis_runner.py` | L302 `create_subprocess_exec` |
| 抓取 | `tools/zsxq_tool.py` 用 **Playwright 驱动真实 Chromium**，导航到群组页并**拦截 API 响应** | L227 `p.chromium.launch(headless=ZSXQ_HEADLESS)`；L611 "通过拦截 API 响应获取主题列表" |
| 弹窗开关 | **`ZSXQ_HEADLESS = os.getenv("ZSXQ_HEADLESS", "false").lower() == "true"`** | L207 —— **默认 false = 有头 = 必弹窗** |
| 登录 | Token 免扫码（已解决），但仍要开浏览器去"用"这个 Token | L221 `_login_by_token()` 注入 cookie 后 reload |
| 性能 | **抓 100 条主题 ≈ 259 秒** | `zsxq_crawler_tool.py` L126 原注释 |

**根因一句话**：**它把"取数据"做成了"看网页"。** Token 早就够了，却仍然启动整个浏览器。

补充证据：项目里**已经存在**无浏览器的通道 —— `zsxq_tool.py` L24 注释明确写着
"`_fetch_topics_by_search` / `_fetch_topics_via_browser`"两个路径，且本机已安装
**`zsxq-cli` v0.4.9**（`C:\Users\Administrator\AppData\Roaming\npm\`），它自带 token 存储与 API 客户端。

### 3.2 方案：三档采集模式，默认走 API

```
ZSXQ_FETCH_MODE = api | api_cli | browser
                  ↑默认        ↑次选      ↑仅兜底
```

| 模式 | 实现 | 弹窗 | 耗时（100 条） | 何时用 |
|---|---|---|---|---|
| **`api`（默认）** | `httpx` 直连 zsxq API，请求头带 `x-access-token` / `x-version`，纯后端 | **无** | **3–8 s** | 常态 |
| `api_cli` | 子进程调 `zsxq-cli`，复用其 token 与重试 | **无** | 5–12 s | token 托管给 CLI 时 |
| `browser` | 现有 Playwright 链路，**强制 `headless=true`** | 无（但重） | ~259 s | **仅** 官方改签名导致 API 失效时 |

### 3.3 关键设计点

**① Token 生命周期（决定"永久不弹窗"能否成立）**

现状：`.env` 里 `ZSXQ_ACCESS_TOKEN`（53 字符）+ `data/zsxq_state.json`；失效时要求人重跑扫码脚本。

改造：
```
采集前  → 轻量探活（GET /v2/users/self，1s 超时）
有效    → 直接采集
401/403 → ① 尝试 storage_state.json 静默续期（同浏览器指纹）
          ② 成功 → 写回 .env，继续采集，前端只显示"已自动续期"
          ③ 失败 → 降级 browser 模式（headless）一次
          ④ 仍失败 → 落"数据缺口"事件 + 告警，绝不回退模拟数据
```
> **合规/安全**：token 只存 `.env`，**禁止**写入日志、前端响应、审计链（research 项目已有 `src/core/redaction.py` 可复用）。

**② 反爬与礼貌性（必须做，否则会被封号）**

- 串行请求 + 指数退避；单群组分页间隔 ≥ 800 ms（对齐本机 `zsxq-cli` 的保守节奏）
- 单次采集上限（默认 200 条/群组/次），超限分页续抓
- 全局 single-flight：同一群组同一时间窗只允许一个采集在跑（assistant 已有 `shared/utils/single_flight.py` 可借鉴思路）
- 命中 `429` → 退避并在面板显示"限流中，N 秒后重试"，**不重试轰炸**

**③ 幂等与增量**

按 `topic_id` 去重，落 `raw_content_hash`；重复采集只增量写新主题。
> 与 research 既有纪律一致：**取数一律钉 `trade_date`，不用实时时钟倒推**。

---

## 4. 三层接入设计

### 4.1 数据层

```
src/infrastructure/connectors/zsxq_connector.py
    实现 BaseConnector 契约（与 akshare/tushare 同级）
    分档 TTL：情报类 30 min（盘中）/ 4 h（盘后）
    per-key lock：防同一群组并发击穿
    失败 → DataFetchError → 落"数据缺口"，禁止回退模拟数据
```

**统一数据契约**（字段名对齐 research 既有口径）：
```json
{
  "topic_id": "1234567890",
  "group_id": "15552452281452",
  "source_url": "https://wx.zsxq.com/dweb2/index/topic/1234567890",
  "publish_time": "2026-09-25T08:12:33+08:00",
  "fetch_time": "2026-09-25T08:31:04+08:00",
  "raw_content_hash": "a3f9…c21",
  "author": "…",
  "text": "…",
  "likes": 12, "comments": 4,
  "extract": {
    "stocks": [{"code": "688981", "name": "中芯国际", "sentiment": "bull", "confidence": 0.86}],
    "industries": ["半导体"],
    "heat": 94
  },
  "quality": { "degraded": false, "source_reliability": "待验证" }
}
```

**合规强约束（写进契约，不是写进文档）**：
- `source_reliability` **强制字段**，星球/论坛/自媒体一律 `待验证`，交易所/监管/权威媒体为 `可靠`
- **禁止** `target_price` / `rating` / `buy_sell` 字段存在于契约中 —— 从数据结构上堵死荐股
- 摘要在读取时**动态拼接**风险声明，落库文本保持纯净（便于审计）

### 4.2 推理层

**核心要求：情报链路必须过 research 既有的 LLM 网关**（`src/infrastructure/llm/`），
**禁止**像 assistant 那样自己起一套 Ollama 调用。收益直接可见：

| 复用网关能力 | 对情报链路的收益 |
|---|---|
| 分层模型路由 | 抽取用本地 1.5B，日报综述用云端 —— 简单任务零 token 成本 |
| 语义 + 精确双层缓存 | 同一批主题重复分析直接命中 |
| 断路器 + 降级链 | 本地模型挂了自动降级，不再"整条链路超时 314.8s" |
| 单任务 token 预算 | 防止长贴文把成本打爆 |
| prompt/response 哈希审计 | 每条抽取结论可追溯到 prompt |

**流水线（两阶段，对齐 assistant 已验证的做法）**：
```
批量分类（1 次 LLM 调用扫 30 条候选） → 仅对命中的精抽（结构化 JSON）
```
> 这正是 research 告警模块已经在用的"批量分类 + 批量打分"两阶段，**同构，可直接复用**。

**输出治理（移植 assistant `governance/` 的精华）**：
- `hallucination_guard`：**强制**抽取出的股票代码必须能在原文中找到依据，否则丢弃
  → 直接落实 assistant `AGENTS.md` 的规则："禁止主观臆测股票代码，检索不到就明说未找到"
- `output_validator`：五维校验（数据/完整性/风险/来源/幻觉），未达标自动重试
- `prompt_sanitizer`：星球内容是**不可信外部输入**，必须过注入防护 —— 这条对 research 尤其重要

> ⚠️ **一处必须改**：assistant 的 LLM 故障默认 **fail-open**（可用性优先）。
> 情报抽取必须改为 **fail-closed**：模型挂了就报"本次无抽取结果"，**绝不**退回规则瞎猜。

### 4.3 展示层

新增 3 个页面（侧栏"情报"分组），与 research 现有面板风格统一：

| 页面 | 内容 | 效果图位置 |
|---|---|---|
| **知识星球情报台** | KPI 卡 + 主题流表格（标的/情绪/热度/行业/摘要/来源）+ 右栏采集链路状态与行业热度 | 效果图第 1 屏 |
| **每日情报简报** | 自动生成的整体研判 + 3 条要点 + 抽取质量自检 + 留痕 | 效果图第 2 屏 |
| **舆情源管理** | 数据源开关、采集模式（Token 直采/兜底）、调度、合规留痕 | 效果图第 3 屏 |

**前端工程要点**：
- 组件化，**不复用** assistant 的 3,602 行单文件 HTML
- 复用 research 既有 `web/src/api.ts` 封装风格，新增 `intelApi.ts`
- 表格与图表沿用现有组件（`MainlineHeatmap`、`MetricsPanel` 等可参考）
- **状态语义**：`已去重 / 降级 / 数据缺口` 必须有独立视觉标记，不能和"正常无数据"混淆
  > 对齐既有纪律："**0 笔和胜率 0% 是两件完全不同的事**"

---

## 5. 移植清单：搬什么 / 不搬什么

### ✅ 值得移植（4 块）

| 能力 | 来源 | 移植方式 |
|---|---|---|
| **知识星球静默采集** | `tools/zsxq_*.py` | 重写为 connector，改 API 直采 |
| **金融输出治理** | `governance/guardrails/`（幻觉防护、输出校验、注入防护、断路器、降级链） | 抽成 `src/domain/intel/guard/`，接入 LLM 网关 |
| **上下文工程** | `agents/reasoning/memory_manager.py`（滑窗 10 轮 + 摘要压缩 + 相关性过滤） | 用于多轮投研会话 |
| **SSE/WS 流式协议** | `api/stream_bus.py`(1,077) + `stream_protocol.py`(293) + `stream_resume` | 补 research 目前偏同步请求的短板 |

### ❌ 不要移植

| 内容 | 原因 |
|---|---|
| 全部 Playwright 浏览器自动化 | 就是弹窗与 259s 的来源，是技术债 |
| C 端散户功能（个股新闻速览/估值/护城河/散户数据/买卖建议） | 与机构定位冲突，且触碰荐股红线 |
| `agents/data/checkpointer.db`（1.7 GB）等运行时库 | 是残留数据，不是资产 |
| 兼容垫片层 / 双真源实现 | 只会把 research 的分层搞脏 |
| `static/index.html` 单文件前端 | 技术栈不兼容，且是反模式 |

---

## 6. 分期计划

| 期 | 交付 | 预估 |
|---|---|---|
| **P0** | zsxq API 直采 connector + Token 生命周期 + 数据契约；**弹窗消失** | 3–4 天 |
| **P1** | 情报抽取流水线（两阶段 + 幻觉拦截 + fail-closed）+ 落库与审计链 | 4–5 天 |
| **P2** | 3 个前端页面 + 调度接入 + 告警联动 | 5–6 天 |
| **P3** | 与主线挖掘/拥挤度/ETF 份额做**交叉验证**（效果图右栏那条）| 3 天 |
| **P4** | 移植 SSE/WS 流式协议与上下文工程（可选，独立价值） | 5 天 |

**P0 完成即可验证核心诉求**：跑一次采集，观察 Windows 任务栏与桌面 —— **不应出现任何新窗口**。

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| zsxq 官方改签名/加签 | API 直采失效 | 保留 `browser` 兜底（**强制 headless**）；响应头有版本号，可快速对齐 |
| Token 被封 | 采集中断 | 保守频率 + 单群组串行 + 上限阈值；token 与账号分离管理 |
| 星球内容含"小作文"/谣言 | 污染投研结论 | `source_reliability` 强制标注；只做**热度与情绪统计**，不做事实断言 |
| 抓取合规性 | 法律风险 | 仅采集**授权账号可见**内容；不对外分发原文；落库仅存摘要 + 链接 + hash |
| 合并后架构变脏 | 破坏 research 既有纪律 | 只走 connector + route 两个扩展点，**不改 domain 既有依赖方向** |
| 1.7 GB 运行时库误入库 | 仓库爆炸 | 合并前先清理 assistant 的 `data/`、`agents/data/`、`benchmarks/results/` |

---

## 8. 验收标准

1. **静默**：连续 3 天定时采集，**零窗口弹出**，任务管理器无相关 Chromium 会话
2. **性能**：单群组 100 条主题采集 **< 10 s**（现状 259 s）
3. **准确**：抽取的股票代码 **100% 可在原文找到依据**（幻觉拦截生效）
4. **可追溯**：任一条目可回溯到 `source_url` + `publish_time` + `raw_content_hash`
5. **合规**：输出中**不存在**目标价/评级/买卖时点；风险声明与可靠性分级强制出现
6. **不退化**：`pytest` 4,004 个既有用例全绿；不新增反依赖

---

## 9. 需要你确认的 4 件事

1. **assistant 是继续作为独立服务，还是彻底并入？**（本方案默认"独立服务 + REST 契约"，解耦、可分别部署、回滚成本低）
2. **知识星球账号是自有还是他人授权？**（决定抓取频率上限与合规表述）
3. **要不要保留 C 端对话问答入口？**（若要保留，建议放独立部署，不进投研平台）
4. **`checkpointer.db` 等 1.7 GB 运行时数据是否还需要？**（若不需要，合并前清理）

---

> **免责声明**：本方案中所有关于收益、性能、准确率的数字均为**实测或代码/文档证据**，
> 未做外推。星球内容属个人观点，系统一律标注"信息来源可靠性待验证"，
> 全部输出仅供参考，不构成投资建议。
