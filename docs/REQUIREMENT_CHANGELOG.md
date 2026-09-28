# 需求变更台账（REQUIREMENT_CHANGELOG）

> **基线**：`docs/PRD.md`（用户 2026-09-12 提供的原始需求开发设计文档）
> **对账证据基座**：`docs/PRD_ALIGNMENT_AUDIT_20260928.md`（56 条确认漂移 + 3 条内部矛盾 + 7 条文档漂移 + 15 条待确认）
> **护栏**：`scripts/prd_sync_check.py` + `.trae/skills/requirement-prd-sync/SKILL.md`
> **机器判据**：`tests/unit/test_prd_sync_check.py`

用户要求（2026-09-28 原话）：

> 「给 trae 再加一个 skill 护栏：每次对话检查用户输入的需求与开发 pr 文档的出入，
>   **没有的补充进去，有差异的改动。**」

**这份台账就是"补充"和"改动"的落账处** —— 每一轮对话里用户说出的需求，
要么在 `docs/PRD.md` 里落了地，要么在这里挂着 `待办 / 待确认`。**没有第三条路。**

---

## 1. 规则（三条命令 + 两条硬约束）

```bash
uv run python scripts/prd_sync_check.py --keyword "<关键词>"   # 本轮需求与 PRD 的出入（三态）
uv run python scripts/prd_sync_check.py --ledger               # 本台账完整性（交付前必须 0 ERROR）
uv run python scripts/prd_sync_check.py --list-sections        # PRD 章节 → ID（现读，不手抄）
```

| 三态结论 | 含义 | 强制动作 |
|---|---|---|
| `OK` | PRD 与台账都提到 | 口径是否一致仍需人工看（脚本只查存在性） |
| `MISS_LEDGER` | PRD 有、台账无 | 本台账补一条（老需求没登记 / 本轮没落账） |
| `MISS_PRD` | **仓库做了、PRD 查不到** | ★ **补写 PRD + 登记本台账** |
| `TODO` | 三面都查不到 | 「没量到」≠「量到 0」→ 换关键词重扫后人工判断，**不许当通过** |

**★ 两条硬约束（脚本会报 ERROR）**：
① `类型 ∈ {新增, 冲突, 细化}` 且 `状态=已闭环` 时，`处置` **必须**是
   `补写PRD / 改写PRD`（**冲突类**多一种合法处置：`标注废止`）；
② `状态=已闭环` 且 `PRD 已更新=是` 时，`docs/PRD.md` 里**必须搜得到这个 CHG 号**
   —— **"我改了 ≠ 它生效了"**：不许在台账里单方面宣布改了 PRD。

**只登台账不改 PRD 的条目，状态只能停在 `待办`。** 这正是用户那两句话的机器判据。

**改口径必须留废止痕迹**：`docs/PRD.md` 的原文一律保留，就地加
`> ⚠️ 已废止（CHG-xxxx，日期）` 或 `~~旧~~ → 新`，**禁止直接删掉原文**。

---

## 2. 章节登记表（PRD 47 节，由 `--list-sections` 现读生成）

> `章节登记状态` 枚举：`已登记`（该节有需求出入，已在 §3 建条目）·
> `无差异`（已逐条核实与仓库现实一致，审计 B 表有证据）·
> `待确认`（**尚未核实，不等于没问题** —— 诚实登记）。

| 章节 ID | PRD 标题 | 层级 | 章节登记状态 | 备注 |
|---|---|---|---|---|
| PRD-1 | 一、项目概述 | H2 | 已登记 | CHG-0044（护栏发布范围） |
| PRD-1.1 | 核心目标 | H3 | 待确认 | 7 条目标未逐条核实（能力型描述） |
| PRD-2 | 二、系统架构设计 | H2 | 已登记 | CHG-0002、CHG-0005 ~ CHG-0016 |
| PRD-2.1 | 2.1 整体架构 | H3 | 已登记 | CHG-0002、CHG-0005 ~ CHG-0007 |
| PRD-2.2 | 2.2 Agent全景清单（18个Agent + 2个基础模块） | H3 | 已登记 | CHG-0008 ~ CHG-0013（含内部矛盾 S1） |
| PRD-2.3 | 2.3 通信协议 | H3 | 已登记 | CHG-0014 ~ CHG-0016 |
| PRD-3 | 三、数据源与数据分层 | H2 | 已登记 | CHG-0001、CHG-0017 ~ CHG-0021 |
| PRD-3.1 | 3.1 数据源清单 | H3 | 已登记 | CHG-0001、CHG-0017 ~ CHG-0020 |
| PRD-3.2 | 3.2 数据更新频率分层 | H3 | 无差异 | 审计 B：DS-4 实时行情 54ms 达标（docs/DATA_SOURCE_ROUTING.md:110） |
| PRD-3.3 | 3.3 数据分级（四级制） | H3 | 已登记 | CHG-0021（审批/物理隔离为声明式） |
| PRD-4 | 四、关键技术设计 | H2 | 已登记 | CHG-0022 ~ CHG-0033 |
| PRD-4.1 | 4.1 数据溯源体系 | H3 | 待确认 | CHG-0022：溯源 vs 来源保密边界待用户定（C2） |
| PRD-4.2 | 4.2 推理路径Trace | H3 | 无差异 | 审计 B：TRACE-2 已实现（src/core/schemas.py:55-63） |
| PRD-4.3 | 4.3 自迭代能力 | H3 | 待确认 | CHG-0023：EDV 是放弃还是仍要建（C14） |
| PRD-4.4 | 4.4 多租户隔离与权限 | H3 | 无差异 | 审计 B：SEC-1/SEC-2/SEC-3/SEC-4 全部实现 |
| PRD-4.5 | 4.5 分析引擎设计（核心Skill清单） | H3 | 已登记 | CHG-0024、CHG-0025 |
| PRD-4.6 | 4.6 数据缓存规范 | H3 | 已登记 | CHG-0026、CHG-0027 |
| PRD-4.7 | 4.7 定时任务调度规范 | H3 | 已登记 | CHG-0028（Celery 定位待定，C10） |
| PRD-4.8 | 4.8 Token节约优化规则 | H3 | 已登记 | CHG-0029 ~ CHG-0031 |
| PRD-4.9 | 4.9 系统响应性能优化规则 | H3 | 已登记 | CHG-0032（最大口径冲突）、CHG-0033 |
| PRD-4.10 | 4.10 前端设计与体验规范 | H3 | 已登记 | CHG-0034 |
| PRD-5 | 五、开发规则与规范 | H2 | 已登记 | CHG-0035、CHG-0036 |
| PRD-5.1 | 5.1 AI开发规则（ai-dev-rules） | H3 | 已登记 | CHG-0036（单文件/单函数行数，C8） |
| PRD-5.2 | 5.2 开发规范（dev-standards） | H3 | 已登记 | CHG-0035（覆盖率门禁，C9） |
| PRD-6 | 六、目录结构与命名规范 | H2 | 已登记 | CHG-0037、CHG-0038 |
| PRD-6.1 | 6.1 顶层目录结构 | H3 | 已登记 | CHG-0037 |
| PRD-6.2 | 6.2 各层目录命名规范 | H3 | 已登记 | CHG-0038 |
| PRD-6.3 | 6.3 依赖规则 | H3 | 待确认 | 单向依赖未逐条核实（无审计判据） |
| PRD-7 | 七、模型选型与部署（Demo版） | H2 | 已登记 | CHG-0003、CHG-0039 |
| PRD-7.1 | 7.1 硬件配置 | H3 | 待确认 | 硬件（RTX 4060 8GB / 32GB）仓库不可判 |
| PRD-7.2 | 7.2 模型分层策略 | H3 | 已登记 | CHG-0003（已改写） |
| PRD-7.3 | 7.3 本地模型部署方案 | H3 | 已登记 | CHG-0003（已改写） |
| PRD-7.4 | 7.4 云端API成本估算 | H3 | 已登记 | CHG-0039（3~6 倍差距，C7） |
| PRD-8 | 八、部署与团队配置（Demo版） | H2 | 已登记 | CHG-0002（§8.1 已改写） |
| PRD-8.1 | 8.1 部署环境 | H3 | 已登记 | CHG-0002（已改写） |
| PRD-8.2 | 8.2 团队配置 | H3 | 待确认 | 「你 + Trae SOLO」无仓库判据 |
| PRD-9 | 九、开发路线图与里程碑（Demo版） | H2 | 已登记 | CHG-0040 |
| PRD-10 | 十、Trae开发指导 | H2 | 已登记 | CHG-0004、CHG-0041、CHG-0045 |
| PRD-10.1 | 10.1 AGENTS.md配置 | H3 | 已登记 | CHG-0004（整节标注废止，已改写）、CHG-0045（补写门禁 skill） |
| PRD-10.2 | 10.2 使用Spec模式 | H3 | 已登记 | CHG-0041 |
| PRD-10.3 | 10.3 使用Task工具并行开发 | H3 | 已登记 | CHG-0041 |
| PRD-11 | 十一、面试演示策略 | H2 | 待确认 | 策略性内容，无可执行判据 |
| PRD-12 | 十二、待确认信息（已确认） | H2 | 已登记 | CHG-0042 |
| PRD-13 | 十三、现行口径（Latest）与已废止归档 | H2 | 已登记 | CHG-0046（折叠归档机制） |
| PRD-13.1 | 13.1 现行口径速查（tombstone 表） | H3 | 已登记 | CHG-0001 ~ CHG-0004、CHG-0045 的 tombstone |
| PRD-13.2 | 13.2 待落条目（口径冲突已确认，尚未改写正文） | H3 | 已登记 | 指向本台账 §3 的 待办/待确认 |
| PRD-13.3 | 13.3 已废止归档区（原文逐字保留，仅供追溯） | H3 | 已登记 | CHG-0046（§10.1 模板已折入 13.3.1） |
| PRD-14 | 十四、模型路由：五层降级链（现行口径 · 2026-09-28 定型） | H2 | 已登记 | CHG-0051（五条链定型）、CHG-0053/0054（限流护栏两条实现约束）；**本节是"当前走哪条链"的唯一权威答案** |
| PRD-14.1 | 14.1 五条链的厂商序列（逐跳固定） | H3 | 已登记 | CHG-0051 |
| PRD-14.2 | 14.2 三条选型约束（不是偏好，是硬约束） | H3 | 已登记 | CHG-0051（零同厂商相邻 / 1.5B vs 8B / 8B 只做地板） |
| PRD-14.3 | 14.3 配额分布（为什么必须分散） | H3 | 已登记 | CHG-0051（百炼跑道 6.4 → 23.9 天） |
| PRD-14.4 | 14.4 免费档限流熔断护栏（没有它，限流是不可见的） | H3 | 已登记 | CHG-0053（状态文件按实例隔离）、CHG-0054（判据用状态码不用文案） |
| PRD-14.5 | 14.5 真值来源与验收命令 | H3 | 已登记 | CHG-0051（单一真值源表 + 可复跑验收命令） |

---

## 3. 变更台账

> 枚举（脚本强校验）：`类型 ∈ {新增, 冲突, 细化, 废止, 非需求}` ·
> `处置 ∈ {补写PRD, 改写PRD, 标注废止, 仅登记台账, 待办}`（`标注废止` 只在 `冲突` 类可作闭环处置）·
> `PRD 已更新 ∈ {是, 否}` · `状态 ∈ {已闭环, 待办, 待确认}`。
> `需求原话` 一栏：用户说过的就写用户原话；审计发现的漂移写 `PRD:「原文截断」`（**不得改写语义**）。

| 变更 ID | 日期 | 类型 | PRD 章节 | 需求原话 | 处置 | PRD 已更新 | 状态 | 证据 |
|---|---|---|---|---|---|---|---|---|
| CHG-0000 | 2026-09-28 | 非需求 | PRD-1 | 用户选定：PRD 作需求基线，缺的补进去、冲突的就地改（不另立存档） | 仅登记台账 | 否 | 已闭环 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0001 | 2026-09-28 | 新增 | PRD-3.1 | PRD:「数据源清单」全表无 QMT，而仓库 QMT 实现面 440 处 | 补写PRD | 是 | 已闭环 | docs/PRD.md、configs/intraday.yaml:228-231、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0002 | 2026-09-28 | 冲突 | PRD-2.1、PRD-8.1 | PRD:「数据层·PostgreSQL + SQLite + Redis + Neo4j（可选）」「数据存储：PostgreSQL + SQLite + Redis，全部本地运行」 | 改写PRD | 是 | 已闭环 | AGENTS.md:8、docs/PRD.md、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0003 | 2026-09-28 | 冲突 | PRD-7.2、PRD-7.3 | PRD:「Qwen3.5-4B（Q4量化）」「Qwen3.5-4B 或 Gemma 4 E4B」「ollama pull qwen3.5:4b」 | 改写PRD | 是 | 已闭环 | configs/models.yaml:114,121、AGENTS.md:7、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0004 | 2026-09-28 | 冲突 | PRD-10.1 | PRD 内嵌模板:「每个Agent封装为独立FastAPI微服务」「本地运行PostgreSQL + Redis」 | 标注废止 | 是 | 已闭环 | AGENTS.md:30-32、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0005 | 2026-09-28 | 冲突 | PRD-2.1 | PRD:「认证层·Keycloak（Demo可简化） + OPA」 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0006 | 2026-09-28 | 冲突 | PRD-2.1 | PRD:「Agent层·核心Agent（6-8个优先）」 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0007 | 2026-09-28 | 冲突 | PRD-2.1 | PRD:「Agent层·LangChain + FastAPI·各自独立任务处理」 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0008 | 2026-09-28 | 冲突 | PRD-2.2 | PRD:「A05 信息去伪Agent·信息层·P1」（agents.yaml 为 P0，registry 为 P1） | 待办 | 否 | 待确认 | configs/agents.yaml、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0009 | 2026-09-28 | 冲突 | PRD-2.2 | PRD:「A12 合规爆雷Agent·分析层·P2」（agents.yaml 为 P1 且名为合规风险Agent） | 待办 | 否 | 待确认 | configs/agents.yaml、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0010 | 2026-09-28 | 冲突 | PRD-2.2 | PRD:「A07 P2」「A13-A16 微调金融模型 / 云端API·P2」 | 待办 | 否 | 待办 | configs/agents.yaml、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0011 | 2026-09-28 | 废止 | PRD-2.2 | PRD:「微调金融模型 / 云端API」—— 全库无任何微调产物 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0012 | 2026-09-28 | 冲突 | PRD-2.2 | PRD:「A17 云端API（DeepSeek-V4-Pro / Claude Opus 5）」 | 待办 | 否 | 待办 | configs/models.yaml:96-109、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0013 | 2026-09-28 | 冲突 | PRD-2.2 | PRD:「Agent全景清单（18个Agent + 2个基础模块）」—— 前者与实际 19 个不符，后者从未定义 | 待办 | 否 | 待确认 | src/api/runtime.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0014 | 2026-09-28 | 冲突 | PRD-2.3 | PRD:「MCP（Model Context Protocol）：作为工具调用层」—— 未实现 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0015 | 2026-09-28 | 冲突 | PRD-2.3 | PRD:「内部消息格式：JSON，包含 message_id、sender、receiver、message_type、timestamp、payload、metadata」 | 待办 | 否 | 待办 | src/orchestration/message_bus.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0016 | 2026-09-28 | 非需求 | PRD-2.3 | PRD:「A2A（Agent-to-Agent）」—— 已核实一致，登记以免重复劳动 | 仅登记台账 | 否 | 已闭环 | src/orchestration/message_bus.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0017 | 2026-09-28 | 废止 | PRD-3.1 | PRD:「券商研报·慧博投研」「大宗商品·生意社」「厄尔尼诺·NOAA」—— 三源均无连接器 | 待办 | 否 | 待确认 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0018 | 2026-09-28 | 冲突 | PRD-3.1 | PRD:「宏观经济·国家统计局·API/网页」「央行数据·www.pbc.gov.cn 网页」—— 实际经 AkShare 封装 | 待办 | 否 | 待办 | src/infrastructure/connectors/akshare_connector.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0019 | 2026-09-28 | 冲突 | PRD-3.1 | PRD:「A股行情·AkShare / Tushare Pro / BaoStock」—— 实际五源链且 QMT 在最末 | 待办 | 否 | 待办 | AGENTS.md:12、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0020 | 2026-09-28 | 新增 | PRD-3.1 | PRD 全表缺失的外部源：知识星球、同花顺快讯、财联社、巨潮、CME FedWatch、中电联 CECI 等 | 待办 | 否 | 待办 | configs/indicators.yaml、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0021 | 2026-09-28 | 冲突 | PRD-3.3 | PRD:「核心数据·本地化存储、双人审批、物理隔离」—— 审批为声明式、物理隔离无实现 | 待办 | 否 | 待办 | docs/SECURITY_COMPLIANCE.md、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0022 | 2026-09-28 | 冲突 | PRD-4.1 | PRD:「每个数据点必须携带溯源元数据：source_url…」—— 情报链路刻意剔除 source_url | 待办 | 否 | 待确认 | src/core/schemas.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0023 | 2026-09-28 | 废止 | PRD-4.3 | PRD:「参考 EDV（Execute-Distill-Verify）范式…经验库…更新Skill Hub」—— 全库 0 处 | 待办 | 否 | 待确认 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0024 | 2026-09-28 | 冲突 | PRD-4.5 | PRD:「Skill 1–20」表（政策解读…投研建议生成）—— 与实际 20 个技能目录结构命名都不同 | 待办 | 否 | 待办 | src/domain/skills/library.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0025 | 2026-09-28 | 废止 | PRD-4.5 | PRD:「Altman Z-Score、Beneish M-Score」「风险平价、Black-Litterman」—— 全库 0 处 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0026 | 2026-09-28 | 冲突 | PRD-4.6 | PRD:「四级缓存：L1内存 → L2本地文件 → L3 Redis → L4数据库」—— 无四级层叠实现 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0027 | 2026-09-28 | 废止 | PRD-4.6 | PRD:「综合命中率目标≥85%」—— 无该指标监控 | 待办 | 否 | 待办 | docs/DATA_INDEX_REQUIREMENTS_AUDIT.md、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0028 | 2026-09-28 | 冲突 | PRD-4.7 | PRD:「分布式调度框架（Demo可用Celery Beat），禁止硬编码crontab」—— Demo 默认进程内调度 | 待办 | 否 | 待确认 | src/scheduler/celery_app.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0029 | 2026-09-28 | 冲突 | PRD-4.8 | PRD:「所有Prompt必须经过压缩，上限4000 token」—— 实际硬上限 6000 字符 | 待办 | 否 | 待办 | src/core/config.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0030 | 2026-09-28 | 废止 | PRD-4.8 | PRD:「保留最近3轮完整对话 + 更早摘要」—— 未找到该裁剪实现 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0031 | 2026-09-28 | 冲突 | PRD-4.8 | PRD:「语义缓存：命中率目标≥50%」「记录 input_tokens、output_tokens、cache_hit、cost_estimate」 | 待办 | 否 | 待办 | src/infrastructure/llm/cache.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0032 | 2026-09-28 | 冲突 | PRD-4.9 | PRD:「响应时间分级：简单查询0.5-2秒，中等2-8秒，复杂5-10秒」—— 实测热 25s / 冷 33-38s | 待办 | 否 | 待确认 | docs/PR_CHECKLIST_LATENCY_AND_MODEL_20260928.md、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0033 | 2026-09-28 | 废止 | PRD-4.9 | PRD:「向量检索优化：使用向量数据库，响应时间目标50ms」「慢查询日志」 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0034 | 2026-09-28 | 冲突 | PRD-4.10 | PRD:「Design Token系统、深色模式」「色盲安全调色板」「三端适配」 | 待办 | 否 | 待办 | web/src/styles.css、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0035 | 2026-09-28 | 冲突 | PRD-5.2 | PRD:「单元测试覆盖率≥80%，集成测试≥60%，E2E关键路径100%」—— CI 无覆盖率门禁 | 待办 | 否 | 待确认 | pyproject.toml、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0036 | 2026-09-28 | 冲突 | PRD-5.1 | PRD:「单文件≤300行，单函数≤50行」—— supervisor.py 1815 行 | 待办 | 否 | 待确认 | src/orchestration/supervisor.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0037 | 2026-09-28 | 废止 | PRD-6.1 | PRD 目录结构列出「configs/data_sources.yaml」「docs/DEPLOYMENT.md」—— 均不存在 | 待办 | 否 | 待办 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0038 | 2026-09-28 | 冲突 | PRD-6.2 | PRD:「domain/skills：{domain}-{action}/，包含 SKILL.md、scripts/、references/、assets/」 | 待办 | 否 | 待办 | src/domain/skills/library.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0039 | 2026-09-28 | 冲突 | PRD-7.4 | PRD:「推荐方案（混合）：100-200元/月」—— 仓库日预算 20 元，实测单月 618 元 | 待办 | 否 | 待确认 | src/core/budget.py、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0040 | 2026-09-28 | 冲突 | PRD-9 | PRD:「第一阶段·第1-2周·本地环境搭建（Ollama + PostgreSQL + Redis）」—— 与现状不匹配 | 待办 | 否 | 待办 | docs/DEVELOPMENT_ROADMAP.md、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0041 | 2026-09-28 | 冲突 | PRD-10.2、PRD-10.3 | PRD:「使用 /Spec 模式」「在单条消息中调用多次Task工具」—— 现行是脚本 + skill 体系 | 待办 | 否 | 待办 | AGENTS.md、docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0042 | 2026-09-28 | 冲突 | PRD-12 | PRD:「用户规模·个人使用（Demo）」「移动端·响应式Web + 微信小程序（后续）」 | 待办 | 否 | 待确认 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0043 | 2026-09-28 | 非需求 | PRD-2.1 | 仓库侧文档漂移：ARCHITECTURE.md / DATA_SOURCE_ROUTING.md / README.md / CACHE_DESIGN.md 传播过时口径 | 待办 | 否 | 待确认 | docs/PRD_ALIGNMENT_AUDIT_20260928.md |
| CHG-0044 | 2026-09-28 | 非需求 | PRD-1 | 本护栏自身的发布范围：`.trae/`、`scripts/*` 被 .gitignore 排除，测试以 skip 守卫知名降级。**用户 2026-09-28 裁定：加入发布白名单**（见 CHG-0047） | 仅登记台账 | 否 | 已闭环 | .gitignore、tests/unit/test_shipped_deps.py |
| CHG-0045 | 2026-09-28 | 新增 | PRD-10.1 | 用户原话：「把以下开发skill，增加到trae的dev-test-guardrails的SKILL里，也加到deepseek hardness的代码开发skill里」 | 补写PRD | 是 | 已闭环 | docs/PRD.md:378-388、AGENTS.md:333、.trae/skills/dev-test-guardrails/SKILL.md |
| CHG-0046 | 2026-09-28 | 新增 | PRD-13 | 用户原话：「如果我需求改了又改，历史PRD需求是不是可以自动删除？」→ 裁定 **折叠归档（tombstone），不删除**：正文只留现行口径，被取代的原文整段折进 §13.3，CHG 号仍在本文件可见 | 补写PRD | 是 | 已闭环 | docs/PRD.md |
| CHG-0047 | 2026-09-28 | 非需求 | PRD-13 | 用户原话：「把 scripts + skill 加入发布白名单」→ `.gitignore` 放行 `scripts/prd_sync_check.py`（白名单 16→17）与 `.trae/skills/requirement-prd-sync/` 反选链 | 仅登记台账 | 否 | 已闭环 | .gitignore、scripts/prd_sync_check.py |
| CHG-0048 | 2026-09-28 | 非需求 | PRD-13 | 用户原话：「把 `--ledger` 接进 CI」→ CI 合规门禁新增对账步骤；台账未发布时打 `::warning::` 显式跳过（知名降级，不静默） | 仅登记台账 | 否 | 已闭环 | .github/workflows/ci.yml、scripts/prd_sync_check.py |
| CHG-0049 | 2026-09-28 | 非需求 | PRD-13 | 用户原话：「修判据 1 的洞」→ `test_shipped_deps.py` 判据 1 对**未发布脚本**豁免（改用 `_tracked()` 判定），克隆/CI 不再必红；已补反向测试自证 | 仅登记台账 | 否 | 已闭环 | tests/unit/test_shipped_deps.py |
| CHG-0050 | 2026-09-28 | 非需求 | PRD-13 | 用户原话：「加跨工具转发层（CLAUDE.md / .cursor/rules / copilot-instructions）」→ 三处**只转发、不复制**，指向同一份 SKILL.md 与三条命令 | 仅登记台账 | 否 | 已闭环 | CLAUDE.md、.cursor/rules/requirement-prd-sync.mdc、.github/copilot-instructions.md |
| CHG-0051 | 2026-09-28 | 新增 | PRD-14 | 用户原话（本轮定义"投研分析 功能，当前多agent五条链的最终形态"）：「planning 阿里→深度求索→本机；light 硅基→阿里→本机；medium 阿里→硅基→深度求索→本机；reasoning 深度求索→阿里→硅基→本机；decision 深度求索→阿里→本机；**零同厂商相邻**；light/planning 用 1.5B，其余三层用 8B 做地板；8B 只做地板不做主力」。**`local_only` 的口径同时定型**：只能声明在**调用点**（`complete(local_only=True)`），不再声明在层上 —— `configs/models.yaml` 里 `light` 的 `local_only: true` 已移除（免费档不花钱，风险从"悄悄花钱"变成"悄悄限流"，由限流护栏接管，见 CHG-0053） | 补写PRD | 是 | 已闭环 | docs/PRD.md §十四、configs/models.yaml:58-240、scripts/_verify_routing_chains.py、tests/unit/test_llm_routing_contract.py、tests/unit/test_llm_gateway.py |
| CHG-0052 | 2026-09-28 | 冲突 | PRD-13.1 | PRD:「云端API成本估算」（付费单价口径）→ 仓库把**免费云端也当成了云端**（`PAID_PROVIDERS` 与"是否本地"混用），实用口径是 `PAID_PROVIDERS={deepseek}` / `LOCAL_PROVIDERS={ollama}` 两个谓词分离 | 改写PRD | 是 | 已闭环 | src/infrastructure/llm/gateway.py:44-60、docs/PRD.md §13.1 |
| CHG-0053 | 2026-09-28 | 新增 | PRD-14 | 本机实测缺陷（非用户提出）：限流熔断状态文件**写死** `data/run/free_tier_429.json` —— 而 `data/run/` 是 dev/pilot/生产**共用**目录，dev 调试出的 3 次 429 会把线上同一跳锁 10 分钟（不报错）。裁定按实例隔离 | 补写PRD | 是 | 已闭环 | src/infrastructure/llm/rate_limit_guard.py::resolve_state_path、tests/unit/test_llm_gateway.py::test_rate_guard_state_path_is_isolated_per_instance、docs/PRD.md §14.4 |
| CHG-0054 | 2026-09-28 | 新增 | PRD-14 | 本机实测缺陷（非用户提出）：限流判据原先**只做文案匹配**（在异常消息里找 "429"），而那句话是 `httpx.HTTPStatusError` 的实现细节 —— 改写法即静默失效（`total_429` 恒 0）。裁定判据优先用**结构化 HTTP 状态码** | 补写PRD | 是 | 已闭环 | src/core/exceptions.py:32-56、src/infrastructure/llm/rate_limit_guard.py::looks_rate_limited、tests/unit/test_llm_gateway.py::test_rate_limited_locks_the_hop_even_without_429_in_the_text、docs/PRD.md §14.4 |
| CHG-0055 | 2026-09-28 | 新增 | PRD-14 | 用户原话：「限流护栏的 snapshot() 还没接进 /health —— 数据已经能取，但运维界面上还看不到，这是"可观测"落地的最后一步」（后端段本轮已接，补的是**界面**可见面）| 补写PRD | 是 | 已闭环 | docs/PRD.md §14.4、src/api/routes/research.py::_rate_limit_guard、web/src/rateGuardView.ts、web/scripts/rateGuardViewCheck.ts（npm run check:rateguard）、tests/unit/test_health_rate_limit_guard_contract.py |
| CHG-0056 | 2026-09-28 | 非需求 | PRD-13、PRD-14 | 用户原话：「scripts/probe_free_providers.py 等本轮新增脚本都不在 .gitignore 白名单里 → 克隆环境不存在」。**核实结论与用户判断不同**：一次性探针（`probe_*` / `audit_*` / `verify_*`）按既定政策**不该**入库，白名单也不需要它们；真正的缺陷是**反向**的 —— `prd_sync_check.py` 在白名单里却**从未 git add**，导致 CI 对账门禁每次静默跳过（假绿）。新增判据 4（白名单=承诺，`git ls-files`=事实）+ 兑现 3 条 `AGENTS.md` 当命令教过的脚本 | 仅登记台账 | 否 | 已闭环 | .gitignore:83-105、tests/unit/test_shipped_deps.py::test_whitelisted_scripts_are_actually_tracked、commit 0143a82 |

---

## 4. 已核实一致（**下一轮不要重查这些**）

见 `docs/PRD_ALIGNMENT_AUDIT_20260928.md` §B：28 条（ARCH-1~6、DS-1~4、SEC-1~6、
TRACE-1~3、TOKEN-1~2、PERF-1~3、DEV-1~3、MIL-1），每条都带可复跑的 `path:line`。

## 5. 待人工确认（15 + 2 条，机器判不了，**不许替人拍板**）

见 `docs/PRD_ALIGNMENT_AUDIT_20260928.md` §C。最关键的三条：

1. **C6 性能口径**：PRD 写"复杂 5-10 秒"，实测热缓存 ~25s / 冷启 ~33-38s ——
   是改 PRD 还是改实现？（最大单条口径冲突）
2. **C7 成本口径**：PRD 写 100–200 元/月，仓库日预算 20 元、实测单月 618 元 ——
   是 PRD 写低了，还是预算配置该收紧？
3. **C7 之外的前置**：C1 已由用户在本会话裁定（PRD 作基线，见 CHG-0000），
   其余 14 条（C2 ~ C15）仍待确认。

**本轮新增两条待确认**：

- **C16（原）已由用户裁定**：加入发布白名单 —— 见 CHG-0047（`.gitignore` 放行脚本与 skill）。
- **C17（发布面 · 未裁定）**：本台账与 `docs/PRD_ALIGNMENT_AUDIT_20260928.md`
  **是否随仓库发布？** 两者都含**内部数字**（实测单月成本、多租户试点、逐条待确认项），
  而仓库现行政策把 `docs/INTERVIEW_LLM_COST_GOVERNANCE.md` 一类文档**排除发布**。
  ⚠️ **未裁定前不要 `git add` 这两份文档** —— git 历史不可逆。
  耦合关系：`tests/unit/test_prd_sync_check.py` 会断言台账存在；若台账不发布，
  CI 里的对账门禁只能打 `::warning::` 显式跳过（见 §6）。

## 6. 本护栏的发布范围（诚实登记）

| 文件 | 是否会随仓库发布 | 说明 |
|---|---|---|
| `docs/PRD.md` | ✅ | 需求基线 + §13 现行口径/归档区 |
| `docs/REQUIREMENT_CHANGELOG.md` | ⏳ **待裁定（C17）** | 本台账。含内部数字 → 未 `git add`，见 §5 C17 |
| `docs/PRD_ALIGNMENT_AUDIT_20260928.md` | ⏳ **待裁定（C17）** | 对账证据基座。同上，含内部数字 |
| `tests/unit/test_prd_sync_check.py` | ⏳ 随台账定 | 依赖下行的脚本与台账；台账不发布时该文件不应单独入库 |
| `scripts/prd_sync_check.py` | ✅ **已改**（CHG-0047） | `.gitignore:84 /scripts/*` → 已在白名单加 `!/scripts/prd_sync_check.py`（16→17）；`git add` 后即发布 |
| `.trae/skills/requirement-prd-sync/SKILL.md` | ✅ **已改**（CHG-0047） | `.gitignore:116 /.trae/` → 已加反选链（`!/.trae/` + `/.trae/*` + 逐级放行），只放行这一条链路 |
| `.trae/skills/dev-test-guardrails/SKILL.md` | ❌ | 同上目录（CHG-0045）；**未**在本轮反选链里，仍不发布 |
| `~/.agents/skills/dev-test-guardrails/SKILL.md` | ❌ | **仓库外**（DeepSeek Harness 用户级 skill 根，rank 500）—— 只存在于本机，克隆后不可得；与上一行**必须 SHA-256 相等** |
| `.github/workflows/ci.yml` | ✅ **已改**（CHG-0048） | 新增「需求—PRD 对账门禁」步骤：脚本在 → 跑 `--self-test` + `--ledger`；台账不在 → `::warning::` **显式跳过**（不静默） |
| `CLAUDE.md`、`.cursor/rules/requirement-prd-sync.mdc`、`.github/copilot-instructions.md` | ✅ 新增（CHG-0050） | 跨工具**转发层**：只指向同一份 SKILL.md，不复制内容 |
| `tests/unit/test_shipped_deps.py` | ✅（他人文件，本轮只修判据 1，CHG-0049） | 判据 1 对**未发布脚本**豁免，克隆/CI 不再必红 |

> **含义（改后）**：脚本与 skill 会随仓库发布 → **克隆环境里"三条命令对账"可执行**。
> 剩下唯一未定的是**台账/审计文档是否公开**（C17）：
> - 若公开 → CI 门禁真生效（`--ledger` 在 CI 里跑）；
> - 若不公开 → CI 打 `::warning::` 跳过，机器保证仍只在开发机成立
>   —— **这是政策选择，不是工程遗漏，所以显式出声而不是静默绿灯**。
>
> **★ 三者是一套，必须同进同退**：`tests/unit/test_prd_sync_check.py` 读台账，
> 台账的证据列又指向审计文档 —— 缺任何一件，`--ledger` 就会报"证据路径不存在"。
> 所以测试顶部按**一整套**判定并显式 `pytest.skip`（不是红灯、也不是静默绿灯）。

---

免责声明：本台账是对账记录，不构成任何投资建议。
