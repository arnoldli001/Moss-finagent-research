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
| PRD-15 | 十五、分析层数据可见性与规则路径留痕（现行口径 · 2026-09-28 定型） | H2 | 已登记 | CHG-0057（报障驱动：白名单按 en_id / 规则路径留痕 / 复合问信号增补 / FRED 序列拆分） |
| PRD-15.1 | 15.1 报障原文与结论 | H3 | 已登记 | CHG-0057（用户原话逐字保留 + 库里数据条数现场） |
| PRD-15.2 | 15.2 三条现行口径 | H3 | 已登记 | CHG-0057（en_id 匹配 / 登记即可见 / 规则路径留痕 tokens=0） |
| PRD-15.3 | 15.3 五类静默故障（都不报错，都是本节的靶子） | H3 | 已登记 | CHG-0057（中文标签 / 兜底假绿 / watch_keywords 被挡 / 同 id 多值 / **作业靠函数调用时注册 → Celery 不跑 lifespan 故从不触发**） |
| PRD-15.4 | 15.4 复合问不得被压成单一视角 | H3 | 已登记 | CHG-0057（只增不减 + focus_stock_code 不写 target + 宁可不补） |
| PRD-15.5 | 15.5 真值来源与验收命令 | H3 | 已登记 | CHG-0057（四个常驻测试 + 7 个只读探针的归档位置） |
| PRD-16 | 十六、本地数据入口统一（现行口径 · 2026-09-28 定型） | H2 | 已登记 | CHG-0058（三层根因：清单看不见 / 取数写错库 / 数据不在库里）、CHG-0059（化石口径冲突，改写）、CHG-0060（受保护 store 与删除纪律）、CHG-0061（`LOCAL_QUOTE_DIR` 停用） |
| PRD-16.1 | 16.1 报障原文与结论 | H3 | 已登记 | CHG-0058 |
| PRD-16.2 | 16.2 五类本地存储与隔离语义（现行） | H3 | 已登记 | CHG-0058 |
| PRD-16.3 | 16.3 写路径：读统一、写归属（不建同名表） | H3 | 已登记 | CHG-0058（ATTACH 只读联查实测 4.6ms / 跨库 JOIN 10.0ms / 写全部被拒） |
| PRD-16.4 | 16.4 受保护 store 与删除纪律 | H3 | 已登记 | CHG-0060（`warehouse.db` 受保护、空表不删）、CHG-0061（`LOCAL_QUOTE_DIR` 曾指向 MariaDB datadir） |
| PRD-16.5 | 16.5 真值来源与验收命令 | H3 | 已登记 | CHG-0058（五条次数/布尔判据） |
| PRD-16.6 | 16.6 跨库回流实测：去重键必须是表自己的唯一约束 | H3 | 已登记 | CHG-0062（三条候选键两条会造成不可逆事故；实测插入 1,270,535 行 / 指标 722→1,029 / 缺口 0） |
| PRD-16.7 | 16.7 L1 交付：单一事实源与三份清单归一（含 16.7.1~16.7.5 四级小节） | H3 | 已登记 | CHG-0067（`configs/data_stores.yaml` + `data_stores.py`；资产 149 条/133 表/5 库+16 目录；反索引进行情仓；顺带修掉测试隔离泄漏）、CHG-0068（16.7.5：L2 跨库只读查询面 `query_across`/`available_stores`） |
| PRD-16.8 | 16.8 台账形状判据：字段数不符必须 ERROR | H3 | 已登记 | CHG-0068（补上"短行补空 / 长行截断"的静默假绿缺口 + self-test 第六条） |
| PRD-16.9 | 16.9 字面量收敛：67 → 31 | H3 | 已登记 | CHG-0070（四批迁移 + 逐条等价断言；四条纪律；刻意留原地 31 处的理由） |
| PRD-16.10 | 16.10 收口到 0：registry 同时表达两套布局（`CHG-0071`） | H3 | 已登记 | CHG-0071（承接 CHG-0070 的 31 处；由并发协作者落 PRD，本节由本轮对账补登） |
| PRD-17 | 十七、本地模型兜底：能力档位与思维链口径（现行口径 · 2026-09-28 定型） | H2 | 已登记 | CHG-0063（思维链根因与默认关）、CHG-0064（地板 8B→4B）、CHG-0065（并发闸保持 1 的口径）；**本节是"本地模型用什么、为什么"的唯一权威答案** |
| PRD-17.1 | 17.1 根因：思考型模型的思维链吃掉输出预算（不是显存不够） | H3 | 已登记 | CHG-0063 |
| PRD-17.2 | 17.2 本地模型档位（RTX 4060 8GB 实测） | H3 | 已登记 | CHG-0064（4B+1.5B 可同时常驻，余量 2.0GB） |
| PRD-17.3 | 17.3 换地板的准入判据（带标注语料，不是"看着差不多"） | H3 | 已登记 | CHG-0064（9 条标注语料 ×2 轮；完全正确率同为 89%，p95 5.58s vs 22.49s） |
| PRD-17.4 | 17.4 已知缺口（不省略） | H3 | 已登记 | CHG-0064（bear_stocks prompt 歧义、分批常数未重标定）、CHG-0065（并发闸 1） |
| PRD-17.5 | 17.5 真值来源与验收命令 | H3 | 已登记 | CHG-0063/0064/0065（四条可复跑命令） |
| PRD-17.6 | 17.6 真实语料 A/B：4B 与 8B 在"券商作文的多空分析"上谁强（`CHG-0071`） | H3 | 已登记 | CHG-0071（真实语料 24 条：方向产出率 4B 75% vs 8B 66.7%，分歧 2/24） |
| PRD-17.7 | 17.7 上下文预算：600 字正文占多少，以及**静默截断**的真实现场 | H3 | 已登记 | CHG-0076（600 字=795 token/4096；12000 字静默封顶在 2050） |
| PRD-18 | 十八、多环境数据管理（dev / test / pilot / prod）（现行口径 · 2026-09-28 定型） | H2 | 已登记 | CHG-0066（用户提供《数据库管理》规则；审计 7 项不合规，A1/A2/A3 已修、A4/A5/A6/A7 待办） |
| PRD-18.1 | 18.1 审计结论：7 项不合规，其中 3 项本轮已修 | H3 | 已登记 | CHG-0066（A1 test 无隔离 / A2 缓存共用 / A3 dev 漏访问审计 / A4 无网关 / A5 元数据无 env / A6 诊断无环境码 / A7 无 prod 且双写）；**CHG-0075：A6 状态 ⏳ → ✅ 已修** |
| PRD-18.2 | 18.2 本轮已修的判据（机器判据，不靠记得） | H3 | 已登记 | CHG-0066（含三条反向测试） |
| PRD-18.3 | 18.3 纪律：不假装完成 | H3 | 已登记 | CHG-0066（A6 的 5 个环境码刻意不定义：没有判据能产生它的码 = 设了不生效的开关）；**CHG-0075 已推翻该判断并就地留废止痕迹**（原文保留、加删除线 + 修正后的口径） |
| PRD-18.4 | 18.4 待用户裁定 | H3 | 已登记 | CHG-0066（A7 谁写谁读 / A5 元数据版本化） |
| PRD-18.5 | 18.5 本轮实测：三档隔离矩阵 | H3 | 已登记 | CHG-0066（三档路径互不相同，由测试断言） |
| PRD-19 | 十九、本地数据确定性流水线（现行口径 · 2026-09-29 定型） | H2 | 已登记 | CHG-0072（L1–L6 六段流水线 + 三跳取数）、CHG-0073（A12 溯源 + 生产者 + 白名单那一层）、CHG-0074（横截面维度还原）、CHG-0075（§18 A6 环境诊断码补定义，并留废止痕迹） |
| PRD-19.1 | 19.1 报障原文与结论：不要靠提示词，也不要让采集 Agent 自由写 SQL | H3 | 已登记 | CHG-0072（用户原话 + 工具优先级由编排层固定 + A08 由放行 4 种指标提升到 87~99 点） |
| PRD-19.2 | 19.2 L1 元数据目录：反向索引（`catalog/column_index.py`） | H3 | 已登记 | CHG-0072（consumed_columns 16,007→60 ms；冷建 1.7 s；反向查 0.029 ms；quant_daily_basic 错误注释作废） |
| PRD-19.3 | 19.3 L2 语义解析 / 实体链接（`catalog/synonym_dict.py`） | H3 | 已登记 | CHG-0072（154 指标别名 / 260 实体别名 / 最长匹配跨度 / ASCII 整词；推翻"按长度降序"的错判） |
| PRD-19.4 | 19.4 L3 查询计划与统一网关执行（`catalog/local_data.py`） | H3 | 已登记 | CHG-0072（ORDER BY DESC + 反转修掉"2007 年当成最新"的 8 倍偏差） |
| PRD-19.5 | 19.5 L4 空结果诊断：17 个码（不是 12 个） | H3 | 已登记 | CHG-0072（12 数据维 + 5 环境维；ENQUEUEABLE_CODES；NOT_APPLICABLE_FOR_ENTITY 不触发联网）、CHG-0075 |
| PRD-19.6 | 19.6 L5 联网兜底：三护栏 + 默认 fail-closed | H3 | 已登记 | CHG-0072（默认白名单为空 / ¥2.0 每日 / 30 次每小时 / 600s+1800s 冷却 / 熔断 3；触发∪不触发==全集且互斥；判据不写死码数） |
| PRD-19.7 | 19.7 L6 反馈治理 | H3 | 已登记 | CHG-0072（补采队列仅收 NO_DATA/NO_TABLE；假缺口清理脚本 + 护栏） |
| PRD-19.8 | 19.8 三跳（+兜底）取数：`supervisor._query_data` | H3 | 已登记 | CHG-0072（三跳 → 第四跳联网兜底；白名单 en_id/族前缀补齐） |
| PRD-19.9 | 19.9 ★ A12 合规：「没量到」不许伪装成「没风险」 | H3 | 已登记 | CHG-0073（伪造体检合格证的根因 + 四层修法 + 两层端到端验收 + 23 条护栏） |
| PRD-19.10 | 19.10 ★ 横截面指标：把「这个数是谁的」补回去 | H3 | 已登记 | CHG-0074（335 条同日成员无维度标签 + 任意截断不可复现；下限取 3 以保住 fed:policy_range 那类缺陷的可见性） |
| PRD-19.11 | 19.11 指标登记面：**把手写豁免表换成派生断言** | H3 | 已登记 | CHG-0072（indicators.yaml 78→97、catalog 70→81；手写豁免表退役换派生断言；过期检查逼出 5 张表的删除） |
| PRD-19.12 | 19.12 验收：用户那条问题的数据可得性实测 | H3 | 已登记 | CHG-0072（26 条需求 25 取到 / 0 空结果 / 1 取数失败；招商银行股息率TTM=4.9606%@2026-09-28） |
| PRD-19.13 | 19.13 已知缺口（不省略） | H3 | 已登记 | CHG-0072、CHG-0073（美国宏观 5 条停在 2025-07/08；社融停在 2026-04；质押阈值口径未标定；担保空结果待反证；13 条同类未登记） |
| PRD-19.14 | 19.14 真值来源与验收命令 | H3 | 已登记 | CHG-0072（9 个单一真值源 + 5 组可复跑命令） |

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
| CHG-0057 | 2026-09-28 | 新增 | PRD-15 | 用户原话：「请分析原因，为什么只用了deepseek／缺少联邦基金利率数据，数据库里有这个数据，为什么没查到？…纯模板导致直接跳过了精准哈希？／复杂问题，被击穿了一些规则路由，越级跳过了？」，随后裁定「帮我落地 全部修复！」。**三条根因**：①`_AGENT_DATA_WHITELIST` 按**中文标签**写（"非农"/"失业率"/"FedWatch"），而库里存的是 **en_id**（`us_nonfarm`/`us_fed_rate`/`fed:policy_range`）→ 子串匹配全落空，实测 A08 22 词在 719 种指标里只放行 **4** 种；②规则路径（`model_used="rule-only"`）**不写任何审计**，导致"走了模板"与"压根没跑"在审计里不可区分；③`analysis_type` 单值 + macro 剥离行业 Agent → 四段复合问共用一份 CPI/PPI。**附带修掉**：`fed:policy_range` 同一 indicator 同日 3 条点（上限/下限/有效利率），取"最新一条"会把**区间下限当成政策利率**报出去；跑全量时又暴露第 5 类静默故障 —— `catalog_calendar` 等作业由 `install_catalog_jobs()` 在 **API lifespan** 里注册，而 **Celery worker/beat 不跑 lifespan** → 从未进 `beat_schedule`、真实调度里一次都不触发 | 补写PRD | 是 | 已闭环 | docs/PRD.md §15（15.1~15.5）与 §13.1 三行、src/orchestration/supervisor.py::_AGENT_DATA_WHITELIST / query_needs_stock_resolution / augment_plan_by_query_signals、src/domain/agents/analysis/base.py::_audit_rule_only、src/domain/agents/analysis/macro/agent.py::_should_skip_llm、src/api/routes/research.py::focus_stock_code、src/scheduler/celery_app.py::_register_catalog_jobs_before_schedule、configs/indicators.yaml（fed:target_upper/target_lower/effr）、tests/unit/test_whitelist_coverage.py（23 条）、tests/unit/test_query_signal_routing.py（16 条）、tests/unit/test_fed_series_split.py（8 条）、tests/unit/test_macro_agent.py（19 条）、tests/unit/test_scheduler.py::test_catalog_jobs_are_registered_at_import_time |

| CHG-0058 | 2026-09-28 | 新增 | PRD-16 | 用户原话：「quant_daily（15,410,692 行日线）+ quant_daily_basic（11,837,692 行）是 dev 独有的平行数据集。我的pilot库 https://hk.wujiaitool.cn/ 所有本地数据接口都来自哪些库？」／「现在我投研分析依赖本地数据，但是查找这些数据查不到，因为数据分散到多个库里。想彻底解决这个数据分散问题」／「可以按第一个建议，但如果只读，后续怎么写进去呢，要在pilot里新写入同名表吗？逐渐过渡？」。**三层根因（逐层实测）**：①清单看不见 —— `assets.py` 的资产登记表只扫 `settings.sqlite_path` **一个库**，`ColumnIndex` 递归 `data/**.db` 但**按名字跳过 warehouse.db** *且*受 8 GiB 上限（行情仓 14.36 GiB），**双重跳过**；②取数写错库 —— `_quant_column_points` 从**应用库**读 `quant_daily_basic`，而该表在**行情仓**（pilot 报 `no such table`、dev 报"期间内 0 行"）；③数据不在库里 —— 逐股 `PE(TTM):*`/`PB:*` 等 **307 个指标只在遗留主库**，pilot 完全没有。**同时存在 3 套库路径解析器、51 个文件 77 处 `data/` 字面量**。**写路径口径**：读统一（只读 ATTACH 跨库）、写归属（唯一 store、不许跨库写）、**不在 pilot 建同名表**、过渡走"观测→双写+读切换→裁剪"三段 | 补写PRD | 是 | 已闭环 | docs/PRD.md §16.1~16.3、src/infrastructure/catalog/assets.py:279,300-305、src/infrastructure/catalog/column_index.py:99-116,190,254、src/infrastructure/connectors/akshare_connector.py:909、scripts/_probe_pilot_paths.py、scripts/_probe_cross_db.py（ATTACH 4.6ms / 跨库 JOIN 10.0ms / 三处写入全部被拒 `attempt to write a readonly database`）、scripts/_probe_point_coverage.py |
| CHG-0059 | 2026-09-28 | 冲突 | PRD-16 | 代码内既有口径：「`quant_daily_basic` **11,837,850 行**、**只存在于 dev 库**（主库/试点库完全没有）→ **生产上永远取不到**、**停在 2023-11-10**」（`column_index.py:13-17`、`local_data.py:12`、`akshare_connector.py:382,494,896,901`、`tests/unit/test_column_index.py:13-15`）。**实测与之矛盾**：共享行情仓 `data/quant/warehouse.db` 有**同名 19 列**、**15,426,153 行**、**到 20260928**，而 **pilot 读的正是它**（`WarehouseConfig.from_env()` 实测解析为 `sqlite:///data/quant/warehouse.db`）。那个 11,837,850 是 `data/dev/moss_dev.db` 里的**化石副本**（成因见 `src/quant/warehouse.py:198-210`：2026-09-23 之前行情仓也认 `MOSS_SQLITE_PATH`）。**后果**：`akshare_connector.py:382` 据此**决定不走本地、改走网络自算股息率** —— 为一个已不成立的前提长期付网络成本 | 改写PRD | 是 | 已闭环 | docs/PRD.md §16.1（含"行数一律现算，禁止写进注释"）、scripts/_probe_quant_columns.py（warehouse 15,426,322/15,426,153 到 20260928 vs dev 15,410,692/11,837,850 停在 20231110，列数同为 19）、scripts/_probe_family_a.py（600036 在行情仓 4,981 点：turnover_rate=0.2637 / dv_ratio=4.9213 @20260928） |
| CHG-0060 | 2026-09-28 | 新增 | PRD-16 | 用户原话：「顺便清理下数据库里的过程临时数据、非最后一次的回测数据、空数据。」／「data/quant/warehouse.db 14.36 GiB 这是我行情数据库 不能随便删，前端要用的」→ **裁定四条**：①`warehouse.db` 登记为**受保护 store**（`protected: true` / `deletable: false`），且本节方向相反 —— 让**更多**代码只读它；②**空表 33 张不删**（只占页、可回收 **0 字节**，删掉只制造"表不存在"的新故障面，应改为登记审计）；③**库里可删 0 行** —— 复用 `retention_passes` 口径 dry-run，三库逐表全为 0（唯一非零是遗留主库 `fact_alerts` 7 行全 expired），`0 = 不清理` 是**有意**的（`sector_crowding/refresh.py`：删历史不可逆）；④`data/dev/moss_dev.db` **是 dev 的整个应用库**，处置为 `DROP TABLE` 两张化石表 + `VACUUM`，**不删文件** | 补写PRD | 是 | 已闭环 | docs/PRD.md §16.4、scripts/_probe_cleanup_inventory.py（逐库空表/备份表/留存 dry-run）、scripts/_probe_dev_db_anatomy.py（dbstat：化石两表+8 索引 **5,708.3 MiB = 82.2%**，其余 28 张非空表 1,234.8 MiB 含 **1,248,146** 行数据点）、scripts/_probe_backtest_inventory.py（`data/backtest` 1,264 文件/1,058.1 MiB，按时间戳分组 **0 组** → "非最后一次"由**实验变体名**区分，需用户指定，不猜） |
| CHG-0062 | 2026-09-28 | 新增 | PRD-16 | 用户裁定「回流到 pilot，不动原库」的**执行记录与正确去重键**。**动手前拦下两次不可逆事故**：①按 `data_id` 去重 → 两库 `data_id` **0% 重合**（实测交集 28,567 点、相同 0），会把 legacy 全部 1,998,608 行判为"新"→ **注入 727,373 行重复点**；②按 `(indicator, period_date)` 去重 → 申万三级估值等**截面指标**一行一个行业（维度在 `extra_json`，实测 `ind:sw_third_pb:all @2026-09-20` 有 335 行 / 232 个不同值）→ **静默丢掉 766,437 行**。**正确键是表自己的唯一约束 `UNIQUE(indicator, period_date, raw_content_hash)`**（读 DDL 得到）。实测插入 **1,270,535** 行 / 88 秒（每批 2,000 + sleep 0.02s）→ pilot `1,233,156 → 2,503,691` 行、指标种类 **722 → 1,029**、`UNIQUE` 重复组 **0**、「同日同 extra_json 多值」**0**、真缺口 **0**。**幂等复查报 700**，查明全部为"同值不同标注"（legacy 那份带 `validation_issues` 且 conf 0.45，pilot 已有 conf 0.80 同值）→ 无信息损失。**由此定下四条工程纪律**：先 `VACUUM INTO` 备份（运行中 WAL 库的一致快照）、分批写 + 批间 sleep、**幂等复查是判据**、回滚清单落盘 | 补写PRD | 是 | 已闭环 | docs/PRD.md §16.6、scripts/_backfill_points_to_pilot.py、scripts/_probe_backfill_premise.py（data_id 0% 重合）、scripts/_probe_backfill_delta.py、scripts/_probe_period_format.py（两库 period_date 格式一致）、scripts/_probe_entity_discriminator.py（619,731 个多行键全部由 extra_json 区分，"真有重复行"=0）、scripts/_probe_backfill_leftover.py（700 行的真身与第二个唯一约束）、scripts/_probe_backfill_verify.py、data/backups/pilot_before_backfill_20260928-230330.db、data/backups/pilot_backfill_ids_20260928-230348.txt |
| CHG-0061 | 2026-09-28 | 新增 | PRD-16 | 用户原话：「D:/quantTrader/data/这个文件下数据可以删除」。**核实结论与用户判断不同，已停手上报**：该目录是**正在运行的 MariaDB 的 datadir** —— `mysqld` PID 3508 监听 `0.0.0.0:3306`，其 `--defaults-file` 指向的 `my.ini` 内容为 `datadir=D:/quantTrader/data`；同目录含 `ibdata1` / `ib_logfile0`(96 MiB) / `undo001-003` / `aria_log*` / `mysql` / `sys` / **`wucai_trade`（845.6 MiB，15 个 .ibd/.frm，属另一个项目）**。删它会毁掉一个运行中的数据库实例。**且 `.env` 的 `LOCAL_QUOTE_DIR` 正指向它** —— 等于把数据库目录写成了"行情导出目录"（连接器只往下找 `SH/`/`SZ/` 子目录，所以一直没出事）。**裁定**：只删 `SH/`+`SZ/` 两个 QMT CSV 子目录（20,347 文件 / 1,128.7 MiB，停在 2026-08-31；**先 tar.gz 归档并校验条目数==20,347 再删**）；`LOCAL_QUOTE_DIR` **留空**（该跳从日线链移除，`config.py` 口径"空则不启用"）；**将来恢复必须指向专用导出目录，不得指向任何数据库 datadir**。用户中途自行删除又回退一次，最终确认维持删除 | 补写PRD | 是 | 已闭环 | docs/PRD.md §16.2 ⑤、§16.4、§13.1「行情数据源」行、docs/PRD.md:98（§3.1 补充）、AGENTS.md:12-23、src/core/config.py:238-247、src/api/runtime.py:172-177、src/api/data_health.py:423-427、docs/DATA_SOURCE_ROUTING.md:18、scripts/_archive_qmt_csv.py（含"拒绝触碰数据库文件"闸门）、data/archive/qmt_csv_20260831.tar.gz |
| CHG-0063 | 2026-09-28 | 新增 | PRD-17 | 用户原话：「还是想解决本地模型因8G硬件显存不足导致的**掷硬币**问题，想本地模型能够兜底多agent并支持并发」+ 一份来自其他大模型的建议（换 Qwen2.5-3B、`OLLAMA_NUM_PARALLEL=3`、`num_ctx=4096`、`local_light` 设 15s 超时）。**实测把根因换了**：掷硬币**不是显存不够**，而是**思考型模型的思维链吃光输出预算** —— 真实 prompt（`tone.build_prompt`+`extraction_schema`，`num_predict=2048`）各 3 次：`qwen3:8b` 默认 p50 18.3s/729 tok → `think=false` **9.1s/321 tok（能力不变）**；`qwen3.5:4b` 默认 **空正文 100%**（2048 全被思考吃掉）→ `think=false` **4.8s/184 tok、键齐全 100%**。**关键对照：4B 权重比 8B 小一半（显存宽裕得多）却比 8B 更容易空返回 —— "显存不足"解释不了它**。**裁定**：本地（Ollama）调用一律 `think=false`（默认值即护栏；`MOSS_LOCAL_THINK=1` 可开回，`ModelSpec.think` 可逐任务覆盖；云端不受影响） | 补写PRD | 是 | 已闭环 | docs/PRD.md §17.1、src/infrastructure/llm/providers.py::_resolve_local_think、src/infrastructure/llm/models.py（`ModelSpec.think`）、tests/unit/test_local_think_switch.py（14 条，含"think 必须真的进 payload"的接线判据）、data/run/tone_think_ab.json、data/run/tone_think_ab_4b.json |
| CHG-0064 | 2026-09-28 | 新增 | PRD-17 | 承接 CHG-0063：**换本地地板模型**（用户授权"按你的建议来…做好测试"）。`local_medium` 从 `qwen3:8b-q4_K_M` 换成 **`qwen3.5:4b`**（**键名不变** —— 键的语义是"中等层本地地板"，不是"8B"；改名会漏改 16 处引用）。依据是**带标注的字段级测评**（9 条真实形态语料 × 2 轮 × `think=false`，每条带人工期望值）：完全正确率 **89% vs 89%**、空返回均 0%、p50 **4.44s vs 5.40s**、p95 **5.58s vs 22.49s**、`codes`/`brokers`/`analysts`/`bull_stocks` 命中率**两者均 100%**、`tone` 86% vs 100%（唯一差异是歧义样本：减持+问询 与 分析师维持增持 同时出现时 4B 读"偏多/中性"、8B 读"偏空"）。**显存**：4B 实测驻留 **2983MB**（8B 5578MB）→ **4B+1.5B 可同时常驻**（交替请求 3 轮后两边都还在，余量 2.0GB），而 8B+1.5B 只剩 ~1.2GB。**裁定换成 4B**：该样本是真歧义（prompt 已写明 `tone` = 原文自己的语气）、`tone` 有词表口径交叉核对可复核、而 p95 从 22.5s 降到 5.6s 正是**消灭超时型掷硬币**的关键。**已知缺口如实登记**：①`bear_stocks` 两模型都填 0%（都往 `bull_stocks` 放，与 `tone` 一致）→ 是 prompt 措辞歧义（"利空股票" vs "这条消息看空的股票"），**未修**（要单独 A/B）；②`alerts/analyzer.py` 的 `STAGE1_BATCH`/`STAGE2_BATCH` 按"含思考 token"算，`think=false` 后只用一半预算，**刻意不动**（安全侧），待生产实测重标定 | 补写PRD | 是 | 已闭环 | docs/PRD.md §17.2/§17.3/§17.4、configs/models.yaml（顶部组合表 + `local_medium`）、tests/unit/test_intel_vocab.py::_assert_capable_local_floor（含"被裁成 1.5B 必须报错"的自证）、src/domain/intel/tone_job.py::EXTRACT_TIER、src/domain/alerts/analyzer.py::STAGE1_BATCH、data/run/tone_graded_final.json、data/run/tone_graded_4b.json、data/run/tone_graded_8b.json |
| CHG-0065 | 2026-09-28 | 冲突 | PRD-17 | **与其他大模型建议的口径冲突，予以否证（保留原建议与其依据）**：建议「`OLLAMA_NUM_PARALLEL=3`（或 5）、把并发从'跑满显存'降到'跑稳'」。**实测否证**：Ollama 在本机**只有 1 个计算槽位** —— 最近 4000 行 server.log 里 `slot ... id` **只出现过 `0`**，`slot print_timing: tg = 45.2 t/s` 单请求就打满 GPU。**调大并发不提吞吐**，只把多个请求切成更慢的片段 → **那正是掷硬币的制造者**。且应用侧**早已是并发 1**（`local_gate.py` 默认 `MOSS_LOCAL_LLM_CONCURRENCY=1`，含 90s 排队上限，把排队挪到应用侧、超时只计生成时间）。**同一条建议里的 `low_vram` 也否证**：它让 KV Cache 落 CPU → 走 CPU offload，本仓库实测"慢一个数量级"。**⚠️ 自我更正**：本轮我先用 3 线程并发打 1.5B，墙钟 2.52s（串行应 6.93s）**看起来像真并行** —— 日志证明不是，那是 1.5B 太快（2.2s/条）落在同一测量窗口里的假象。**裁定：并发闸保持 1，`MOSS_LOCAL_THINK=0` 才是"不掷硬币"的那一项** | 补写PRD | 是 | 已闭环 | docs/PRD.md §17.4 ③、configs/models.yaml 第一节、src/infrastructure/llm/local_gate.py、`%LOCALAPPDATA%\Ollama\server.log`（slot id 仅 0） |
| CHG-0067 | 2026-09-28 | 新增 | PRD-16 | 承接 CHG-0058（三层根因）与 CHG-0066（A4「无统一网关」），交付 **L1：单一事实源 + 三份清单归一**。①**新增** `configs/data_stores.yaml`（20 条登记；字段 `kind`/`isolation`/`writer`/`writable`/**`protected`**/`role`/`time_column`/`note`）与 `src/infrastructure/catalog/data_stores.py`（`resolve_store`/`store_path`/`open_readonly`/`writable_here`/`unregistered_databases`/`describe`）；②**三份清单改为读它**：`assets.DataAssetCatalog` 从"只扫 `settings.sqlite_path` 一个库 + 手写 4 个目录"→ **149 条资产 / 133 张表 / 5 个库 + 16 个目录**；`ColumnIndex` 扫描范围由 registry 决定 → `find_column("dv_ratio")` 命中 **`warehouse.db / quant_daily_basic`（row≈15,426,153、authority=99）**（原先被 `SKIP_DB_PATTERNS` 的名字 + 8 GiB 体积**双重跳过**）；目录的"在哪里"归 registry、"多久更新"归 `DIR_FREQUENCY`，**职责分开**；③**顺带抓出并修掉一个既有的测试隔离泄漏**：`local_data._refresh_index()` 无条件把索引换成扫**真实仓库**的全局单例 → 调用方给 `project_root=<tmp>` 假库树时**作用域被悄悄扩大，单测读到真实行情仓**（反证：两个 `test_local_data_executor` 用例此前是**靠巧合通过**的，行情仓一进范围立刻变红）。修法＝给了显式索引就**就地重建它自己**；判据同步反写（原 `calls == [True]` **把泄漏当契约**，现为"就地重建发生 **且** 不得调用全局单例"）；④**借覆盖判据清掉一个游离空库** `data/warehouse.db`（1 页 / 0 表）——它是已修缺陷的残留（`WarehouseConfig.from_env(root="data/quant")` 取 `<root>/../warehouse.db`，root 传浅一层就在错位置建库）；⑤**交付护栏** `tests/unit/test_store_registry.py` 8 条（库清单覆盖实扫 / 行情仓真的进得了反向索引 / 受保护存储完好 / 不得当清理目标 / 行数常量=0 / 字面量棘轮**精确相等** / **棘轮降到 0 后必须删掉自身** / 写权限必须带人话理由）。**判据现状**：① 路径字面量 77→**67（棘轮锁定，只许减）**⏳ ② 未登记库 **= 0** ✅ ③ pilot 与 dev 上 600036 `turnover_rate` **均 664 点 / 2026-09-28 / staleness_days=0** ✅ ④ 行数常量 **= 0** ✅ | 补写PRD | 是 | 已闭环 | docs/PRD.md §16.7（16.7.1~16.7.4）、configs/data_stores.yaml、src/infrastructure/catalog/data_stores.py、src/infrastructure/catalog/column_index.py（`_discover_from_registry` / `_discover_on_filesystem` 双分支）、src/infrastructure/catalog/assets.py（`_scan_all_sync` registry 驱动 + `_LARGE_TABLES`/`_rowid_lower_bound`）、src/infrastructure/catalog/local_data.py（`_refresh_index` 作用域纪律）、tests/unit/test_store_registry.py（8 条）、tests/unit/test_local_data_executor.py（自愈判据反写）、scripts/_probe_store_registry.py、scripts/_probe_assets_coverage.py、scripts/_probe_index_sees_warehouse.py、153 passed（catalog 受影响面）+ 65 passed（核心面） |
| CHG-0066 | 2026-09-28 | 新增 | PRD-18 | 用户原话：「参考这个数据库管理经验规则，看下我这个项目当前dev、pilot、test数据库的管理有哪些问题？把能改的改了」+ 一份《数据库管理》规则（核心原则「物理隔离、权限最小、单向同步、接口统一、元数据版本化共享」；§6 反模式第一条「同库同账号，靠 `env` 字段区分」）。**逐条审计出 7 项不合规**：**A1** `--env test` **没有任何隔离分支** → 落 `MOSS_SQLITE_PATH` 默认值 `data/moss_finagent.db`（`manage.py:488` 自标"生产用"），而 `manage.py test` 走 `build_test_env()` 临时目录 → **同一个 `test` 两套语义**（比反模式更糟：连"不同 database"都没做到）；**A2** `llm_cache_dir` 是裸字面量 `data/llm_cache`，三实例共用（且本项目缓存 scope 不含 provider/model → 跨环境对比必然失真）；**A3** dev 只重定向 `LLM_AUDIT_DIR`、**漏了 `MOSS_AUDIT_DIR`**（两者默认同为 `data/audit`）→ 访问审计写在共用目录；**A4** Agent 全线直连库（`src/` 77 处 `data/` 字面量 / 51 文件），无统一网关；**A5** 元数据无 `env` 列与版本化发布流程；**A6** `local_data.py::DiagCode` 缺规则要求的 5 个环境诊断码；**A7** 无 prod，dev 与 pilot 各自采集且**都写共享行情仓**（pilot `quant_data_sync` 成功 41 次）、"谁写谁读"未裁决。**本轮修 A1/A2/A3**：新增 `TEST_ROOT`+`test_isolation_env()` 并接进 `--env test` 分支；`llm_cache_dir` 改 `_env_field("LLM_CACHE_DIR", ...)`；dev 补 `MOSS_AUDIT_DIR`；`assert_environment_consistency` 新增「test 路径必须含 test」判据；`describe_environment` 给 test 印真实限制。**A6 刻意不做**：没有判据能产生那些码就是"设了但不生效的开关"（同 `MOSS_SCHEDULER_ENABLED` 的教训）。**A7/A5 留待用户裁定** | 补写PRD | 是 | 已闭环 | docs/PRD.md §18（18.1~18.5）、manage.py（`TEST_ROOT` / `test_isolation_env` / `dev_isolation_env` 补 `MOSS_AUDIT_DIR` 与 `LLM_CACHE_DIR` / `pilot_isolation_env` 补 `LLM_CACHE_DIR` / `--env test` 分支与横幅）、src/core/config.py（`llm_cache_dir` 改 `_env_field`、§⑤ 新增 test 隔离判据、`describe_environment` test 档）、tests/unit/test_env_guard.py（`test_start_test_env_is_isolated_and_self_consistent` / `test_test_env_pointing_at_default_db_is_rejected` / `test_all_three_isolation_envs_isolate_cache_and_audit`，121 passed） |

| CHG-0068 | 2026-09-28 | 新增 | PRD-16 | 承接 CHG-0067，交付 **L2（跨库只读查询面）** 与 **台账形状判据**。①`LocalDataExecutor.query_across(stores, sql, params)`：**一份只读连接**跨多个存储查询，库名直接写进 SQL（`warehouse.quant_daily_basic` / `main.map_quant_sector_stock`），`mode=ro` + `PRAGMA query_only=1`；`LocalDataExecutor.available_stores()` 给出与 registry **同源**的存储清单 + 写权限报告。**写路径刻意不走它** —— SQLite 无跨库事务（一次写两库只能半提交），写必须恰好落一个库（判据 `writable_here`）。②**补上台账形状判据**：`_table_rows()` 对短行**静默补空单元格**、对长行被 `dict(zip(..., strict=False))` **静默截断** —— 两种都不报错，于是同一天里台账被我改坏三次（把 `CHG-0061` 行首替换掉、毁掉 `CHG-0065` 结尾、把 `CHG-0067` 与 `CHG-0066` 粘成一行使字段数 9→14），**`--ledger` 每次都报通过**。新增 `_table_shape_issues()`：任何表格行字段数必须与表头**严格相等**（只认**未转义**的 `\|`），并在 `validate_ledger` **解析之前**先跑（否则下游判据全建立在被静默截断的行上）。自证补第六条：喂一个"两行粘成一行"的已知坏输入，必须报出"字段数" | 补写PRD | 是 | 已闭环 | docs/PRD.md §16.7.5、§16.8、src/infrastructure/catalog/local_data.py（`query_across` / `available_stores`）、scripts/prd_sync_check.py（`_split_unescaped` / `_table_shape_issues` / self_test 第六条）、tests/unit/test_store_registry.py（`test_cross_store_readonly_query_works_and_rejects_writes` / `test_available_stores_exposes_registry_view`）、50 passed（store_registry + local_data_executor + column_index）、`--self-test` 六类坏输入全报出、`--ledger` 0 ERROR（68 行 CHG，字段数自检 0 异常） |
| CHG-0069 | 2026-09-28 | 新增 | PRD-17 | 承接 CHG-0064 的端到端验收，**推翻了自己上一版的归因**：`tone_job` 落库后 4/5 条 `summary` 为空，第一版归因成"4B 惜字撞 `no_number`"（错，已作废并留更正痕迹）。逐层二分定位真因 —— **语义缓存串答案**：抽取 prompt 的固定骨架（schema+指令 ≈700 字）占绝大部分，而语义相似度拿**整个 prompt** 算 → 任意两条不同资讯的 prompt 相似度 **0.93~0.94 > 阈值 0.85**（只看正文只有 **0.014~0.055**）→ 第 2 条起必然复用第 1 条的答案。判据链：模型直连 ✅ 各不相同／网关 `use_cache=False` ✅ 各不相同／网关默认 ❌ `cache_hit=True kind=semantic` 答案全同／`_ask_segment` 置 `gw._cache=None` ✅ **5/5 摘要可落库、各条针对自己正文、中位 27 字**。**与模型无关 ⇒ 8B 时代同样存在，是长期潜伏、不是本轮引入**（同一模型下关缓存三条不同、开缓存三条全同）。**未修**：属缓存键口径变更，三条候选（正文进相似度／双条件命中／抽取类显式禁缓存）各有代价，需单独裁定与 A/B | 补写PRD | 是 | 已闭环 | docs/PRD.md §17.4-1、src/infrastructure/llm/cache.py::cache_key、src/domain/intel/tone_job.py::_ask_segment、scripts/_diag_semantic_sim.py（0.9328/0.9348/0.9371 vs 阈值 0.85）、scripts/_diag_bisect_same_answer.py（三层对照）、scripts/_diag_cache_pre_existing.py（开关缓存对照）、scripts/_verify_summary_nocache.py（5/5）、data/run/summary_4b_nocache.json |

| CHG-0070 | 2026-09-28 | 新增 | PRD-16 | 承接 CHG-0067/0068，交付**字面量收敛 67 → 31**（判据①）。**分四批，每批先断言「行为等价」再跑测试**：**B0** 判据修正 —— 排除 3 个 **FastAPI 路由路径**（`/data/macro`、`/data/status`、`/data/sync`）误报（67→64）；**B1** 7 个仓储的构造默认值 + 概念池 + 竞价 + 拥挤度三处模块路径默认值（64→49，15/15 逐条相等）；**B2** 共享只读存储的权威定义（行情仓/分区根/价格/基本面/主线缓存，49→36，15/15）；**B3** 情报缓存三条路径 + `DIR_FREQUENCY` 改按**存储名**索引（36→31，prewarm 3/3 逐字相等）。**四条纪律**：①**默认值即护栏** —— 7 个仓储原默认 `data/moss_finagent.db`（三档隔离**共用**的遗留主库），一次漏传就让隔离档写到共享库；现默认取`default_app_db()`（= `settings.sqlite_path`），拿不到才退 `legacy_main`；②**★ 默认值改成 `None` 必须在函数体补回落** —— 本轮漏了 `mainline/sources.py`与 `quant/stock_directory.py`，`Path(None)` 直接打红 **11 个 mainline 用例**；③**等价性逐条断言**（不是「看起来一样」），它同时证明 registry 的声明与代码旧值一致；④**相对路径不要「优化」成绝对路径** —— `prewarm` 我一度返回绝对路径，那会**提高**「忘记隔离就写到生产」的概率（相对路径至少还依赖 CWD），已改回相对仓库根、与旧字面量逐字一致。**刻意留原地 31 处并登记原因**：`core/config.py` 路径默认值 5 处（它们定义**主实例**布局，而 registry 把审计/缓存声明为 `per_env`；改默认值会把已有**哈希链搬走**使链校验断 —— 属 A7 类决策）、审计/缓存/运行目录单点默认值 12 处（同上）、路由与健康检查展示路径 7 处、其余 7 处待下轮 | 补写PRD | 是 | 已闭环 | docs/PRD.md §16.9、src/infrastructure/catalog/data_stores.py（`store_rel` / `default_app_db`）、src/sector_crowding/config.py、src/auction_select/config.py、src/quant/concept_repo.py、src/infrastructure/repositories/*.py（6 个）+ src/quant/quant_select_repo.py、src/mainline/{warehouse,member_pure,sources,config}.py、src/quant/{warehouse,dataset_store,pit,price_panel,panels,stock_directory}.py、src/domain/intel/prewarm.py、src/infrastructure/catalog/assets.py（`dir_frequency`）、scripts/_probe_migration_equivalence.py（15/15）、scripts/_probe_batch3_equivalence.py（3/3）、scripts/_migrate_*.py（三个带断言的迁移脚本）、tests/unit/test_store_registry.py（棘轮 31）、1040 passed（B2 受影响面）+ 144 passed（B3 受影响面） |
| CHG-0071 | 2026-09-28 | 非需求 | PRD-17 | 用户原话：「清掉吧」（承接「8b在本项目里有其他功能在用…」的核对结论）。清理 `src/domain/intel/llm_policy.py` 的两个**死常量** `LOCAL_MODEL = "qwen3:8b"` / `LOCAL_MODEL_SMALL = "qwen2.5:1.5b"`。**删之前先证明它们是死的**：AST 精确判定生产代码引用次数 **0**（全仓库只有"测试函数名里带 LOCAL_MODEL"与文档提过），所以删除**不改任何行为**；而它们的值本身也已过期（配置里真实登记的是带量化后缀的 `qwen3:8b-q4_K_M` / `qwen2.5:1.5b-instruct-q4_K_M`，且本地地板已换成 `qwen3.5:4b`）⇒ 留着就是**过期路标**：下一个人会照着它去找一个不在路由链上的模型。**裁定：本地模型的名字只写在 `configs/models.yaml` 一处**，并加护栏 `tests/unit/test_local_model_single_source.py`（5 条：①llm_policy 不许再定义模型名常量（用 AST 判，不把文档里的历史记录误判成违规）②`local_light`/`local_medium` 两个键必须存在且 provider 是本机 ③路由链上每个键都必须在配置里登记 ④被删的常量真的没了 ⑤判据 1 的非空自证）。顺带修正两处过期注释（`tone_job._ask_segment`、`test_intel_vocab` 的"本地主模型是 8B"） | 仅登记台账 | 否 | 已闭环 | src/domain/intel/llm_policy.py（删常量 + 模块 docstring 指向配置）、tests/unit/test_local_model_single_source.py、src/domain/intel/tone_job.py:228、tests/unit/test_intel_vocab.py:351、AST 判定"生产引用 0 次"、109 passed（intel_vocab + routing_contract + llm_gateway + think_switch + event_analyzer + 新护栏） |
| CHG-0076 | 2026-09-28 | 新增 | PRD-17 | 用户提问：「券商作文的输入长度最大 600 个中文字，如果超过了 4b 模型允许的大小，会发生什么？」**实测回答**：①600 字正文（含 404 token 骨架）实测 `prompt_eval_count=795`，只占 `num_ctx=4096` 的 **19%**，余 3301 给输出（输出预算 `max_tokens=2048`）⇒ 装得下；②**生产路径根本到不了上限** —— `segment_text` 的`MAX_TEXT_CHARS=600` 切段 + `build_prompt` 的 `[:600]` 兜底是两道硬闸；③**真去撞上限是静默截断**：12000 字正文（12761 字符 prompt）只处理 **2050 token** 就封顶、`done_reason=length`、**不报错**，而 `prompt_eval_count` 原先**没有任何地方读** ⇒ 截断完全不可见（表现只是"没抽全"）；④封顶值**不是常量**（换 num_ctx=16384 的临时模型后为 8194~9152）⇒ 不能写死判据。**动作（只加观测不改行为）**：`providers._warn_if_prompt_truncated` 在 `prompt_eval_count < 0.3 × 字符数` 时 WARNING；取 0.3 因实测中文密度 0.64；**只告警不重试**（不替调用方决定）；当前 600 字上限永不触发，它是给"将来放宽上限"准备的安全网。护栏 3 条：封顶必报 / 生产上限不报（否则噪音）/ 读数缺失不报（没量到≠量到 0） | 补写PRD | 是 | 已闭环 | docs/PRD.md §17.7、src/infrastructure/llm/providers.py::_warn_if_prompt_truncated、tests/unit/test_local_think_switch.py（截断告警 3 条，17 passed）、scripts/_probe_ctx_budget.py、scripts/_probe_truncation.py、scripts/_probe_ctx_ceiling.py |
| CHG-0072 | 2026-09-29 | 新增 | PRD-19 | 用户原话（2026-09-28）：「不要指望靠提示词让 Agent『优先查本地库』，也不要让采集 Agent 直接连数据库自由写 SQL。要把『找数据』做成一条**确定性流水线**：元数据目录 → 语义解析 → 实体/指标链接 → 查询计划 → 统一网关执行 → **空结果诊断** → 反馈治理。」同轮追加：「要全面补充常见金融和股票财务指标词汇…构建**精准哈希索引**。避免数据库有的却连接不到…连接器找不到、指标等级里无 id、`INDICATOR_CATALOG` 目录里没有，都要**自动去联网获取数据，做最差的兜底，一定要找到数据**。」交付 **L1 反向索引**（`catalog/column_index.py`：全库表字段枚举 + `best_for_column` 排序「有数据→最新时间→权威→行数」+ `consumed_columns` **16,007 ms→60 ms** + 单例与启动后台预热）、**L2 语义解析**（`catalog/synonym_dict.py`：154 指标别名 / 260 实体别名 / **最长匹配跨度** / ASCII 必须整词）、**L3 查询执行**（`local_data.py`：`metric_series` / 别名候选链 / 实体归一 / `best_for_column` 选存储 / 索引失效自愈；★ `ORDER BY time ASC LIMIT 400` 曾把 **2007 年**当成"最新"返回、偏差 8 倍且无异常 → 改 `DESC + 反转`）、**L4 诊断 17 码**（12 数据维 + 5 环境维，每码带 `DIAG_NEXT_STEP`，`ENQUEUEABLE_CODES={NO_DATA,NO_TABLE}`；`NOT_APPLICABLE_FOR_ENTITY` **不触发联网**）、**L5 联网兜底**（`network_fallback.py`：白名单**默认空=fail-closed** / ¥2.0 每日 / 30 次每小时 / 600s+1800s 冷却 / 熔断 3 次 / 状态按环境落盘；`FALLBACK_TRIGGER_CODES ∪ _NO_FALLBACK_REASONS == DiagCode 全集` 且互斥）、**L6 反馈治理**（补采队列 + `clean_gap_queue.py` 假缺口标 skipped）、**三跳→四跳取数**（`supervisor._query_data`：validated_points → LocalDataExecutor → ConnectorRouter → 联网兜底）。**登记面**：`indicators.yaml` 78→**97**、`INDICATOR_CATALOG` 70→**81**（行情仓 6 列族 / 截面入口 / `M2`+`社融`+`PMI`+`GDP`）。★★ **本轮唯一的防复发改动**：把"白名单声称了、连接器也取得到、就是没人登记"的**手写豁免表退役**，换成**派生断言** `test_whitelist_keywords_a_connector_accepts_are_registered`（遍历白名单 × `supports()`，**零清单零豁免**）—— 手写表不会自己长大，下一个 `PMI`/`GDP` 式缺口它一个字都不会说。**过期检查逼出的删除**（全部由测试自己点名，不许绕过）：`_UNREGISTERED_CONNECTOR_PREFIXES` 删 8 条、`_CATALOG_NOT_REGISTERED` **整表退役**、`_KNOWN_UNPRODUCED_RULE_FAMILIES` 6 删 5、`_ALLOWED_PHANTOM_PREFIXES` 删 5（含 `A08_macro:GDP同比`：白名单写无冒号的 `"GDP同比"` 而 id 是 `GDP:同比` → **幻影关键词**，修法是改白名单而不是留豁免）。**错误事实更正并留痕**：旧注释「`quant_daily_basic` 停在 2023-11-10」**是错的** —— 实测权威副本在 `data/quant/warehouse.db`，as-of 2026-09-28 rows=**15,426,153**、trade_date `20060104..20260928`、600036 `dv_ttm=4.9606`；**错误的理由比没有理由更危险**（它让 6 个已实现指标长期挂着僵尸表豁免）。顺带修 `scripts/affected_tests.py` 两处**静默少选/崩溃**（GBK 解码崩溃；`__init__.py` 被算成 `X.__init__` 致传递闭包断链 → 改 `compliance/logic.py` 时选中数由 **2** 纠正为 **26**） | 补写PRD | 是 | 已闭环 | docs/PRD.md §十九（19.1~19.14）、src/infrastructure/catalog/{column_index,synonym_dict,local_data,network_fallback,data_stores}.py、src/orchestration/supervisor.py（`_query_data` 三跳 + 白名单 en_id/族前缀）、src/api/main.py（`column-index-warm`）、src/api/runtime.py、configs/indicators.yaml（97 条，dup=[]）、src/orchestration/planner.py（81 条）、tests/unit/{test_column_index,test_local_data_executor,test_synonym_dict,test_network_fallback,test_contract_consistency,test_whitelist_coverage,test_gap_queue_false_positive,test_fed_series_split,test_query_signal_routing,test_macro_extra_connector}.py、scripts/affected_tests.py、**验收** `scripts/_probe_acceptance_data.py`（26 条需求：**25 取到 / 0 空结果 / 1 取数失败**；`股息率TTM:600036`=**4.9606%@2026-09-28**）、`scripts/_probe_compliance_families.py`、`scripts/_probe_a12_whitelist_acceptance.py`、`scripts/_probe_pmi_gdp_recheck.py` |
| CHG-0073 | 2026-09-29 | 细化 | PRD-19 | 承接"数据到了 Agent 看不见"这一族缺陷，**修掉其中最危险的一张：A12 伪造的体检合格证**。**症状**：A12 对任何个股都输出 `compliance_level="无"`、旗标 `["未见明显合规风险信号"]`、`confidence="high"`，且因 `level=="无"` 走纯规则路径**跳过 LLM**（省 18,500 tokens/轮）⇒ **整条链路没有任何环节报错**。**根因**：`compliance/logic.py` 最后三行把两种**语义相反**的处境收进同一个 `else` —— 「6 个族都量到了、都没超阈值」与「**6 个族一条都没量到**」输出**一字不差**。**实测**（走 `build_runtime()` 里 A01 真实持有的 `ConnectorRouter`）：**6 个族零生产者**，即线上一直走的是第二行。**四层修法（缺一层就白修）**：①**规则层** `_RULE_FAMILIES` 逐族记录有没有拿到值（`None`=没量到 / `0.0`=量到 0），无输入 → `LEVEL_UNMEASURED`「未量到」+ `FLAG_UNMEASURED`（明写"不等于无风险"）；②**Agent 层** `_build_rule_only_result()` 两分支文案完全不同并各带机器可读的 `_rule_only_reason`（`measured_clean`/`no_input` —— 没有它"走了纯规则"与"压根没跑"在审计里一样）；`_requirements()` 补禁令且枚举加 `未量到`；③**采集层** 新建 `ComplianceFinConnector`（商誉/货币资金/有息负债/质押/担保 五族，`关联交易` **刻意不支持** —— 免费源只有公告标题无金额，且**不许退化成计数口径**：规则按子串取值，`关联交易公告数:{code}` 会被当百分数去比 `>30`）并注册进 `runtime.py`；④**★ 白名单层（最容易漏的一层）** `_AGENT_DATA_WHITELIST["A12_compliance"]` 原先只有 `担保`/`质押`/`关联交易` 能**巧合**匹配新指标名，`商誉`/`货币资金`/`有息负债` **全部被挡** ⇒ 商誉档与存贷双高（需两个输入同时到手）**恒不触发** —— 与"数据在库里 Agent 看不见"**完全同类，只是换了一层**。**两层端到端验收**：`_probe_compliance_acceptance.py`（路由层取到 3 族；`evaluate_compliance` → `compliance_measured=True`、已量到族 `['商誉','有息负债','质押']`）＋ `_probe_a12_whitelist_acceptance.py`（**白名单过滤后 3 点仍是 3 点**，A12 真拿到这三条）⇒ 现在的「无」是**挣来的**。仍缺的 3 族原因**各不相同**必须分开：`关联交易`（免费源无比率口径）/ `担保`（窗口内无公告=真结论）/ `货币资金`（**银行模板无此科目 → 口径不适用**，不是缺陷） | 补写PRD | 是 | 已闭环 | docs/PRD.md §19.9、src/domain/agents/analysis/compliance/logic.py（`_RULE_FAMILIES`/`LEVEL_UNMEASURED`/`FLAG_*`/溯源三件套）、src/domain/agents/analysis/compliance/agent.py（`_rule_only_reason`/`_requirements` 禁令/`_enrich_result` 溯源）、src/infrastructure/connectors/compliance_fin_connector.py、src/api/runtime.py（注册行）、src/orchestration/supervisor.py（A12 白名单 +`商誉`/`货币资金`/`有息负债`）、configs/indicators.yaml（5 条，附口径 notes）、tests/unit/test_compliance_input_provenance.py（23 条，含"量到 0 ≠ 没量到"逐族参数化与"别把假绿换成假红"的反向断言）、tests/unit/test_compliance_agent.py（2 条旧断言按新语义改写并留更正说明）、`scripts/_probe_compliance_acceptance.py`、`scripts/_probe_a12_whitelist_acceptance.py`、144 passed（白名单+契约+宏观+preflight+前缀贯通 116 + 合规 23 + 5） |
| CHG-0074 | 2026-09-29 | 新增 | PRD-19 | 承接 CHG-0072 的登记面补齐，修掉**横截面指标在 LLM 上下文里没有维度**这一缺陷。**现场**：`ind:sw_third_pe_ttm:all` / `ind:sw_third_dividend_yield:all` 实测 **335 条**、`period_date` **全部等于 2026-09-29**、每条属于一个不同申万三级行业；而 `_build_context()` 渲染成 `- {指标} {期}={值} c{置信} {来源}`、**不带 `extra`** ⇒ 模型收到 60 行 `...=80.53` 的无主数字，只能随手挑一个当"该行业 PE"，或干脆编一个。**第二个更隐蔽**：`context_max_periods = 60` 把它当"最近 60 **期**"截断，而这里**没有"期"** —— 同一日期 335 个成员被**任意**留下 60 个（SQL 返回顺序不稳定）⇒ **同一个问题两次答案不一样且无法复现**。**修法（只加信息不减信息）**：判据是**行为判据**、不猜字段名 —— ①同一 `period_date` ≥ **3** 条；②这些条目的 `extra` 里存在一个键其取值 ≥2 个不同非空值（**能被区分**才算横截面）。然后排序改成**完全确定**的 `(期 desc, 值 desc, 维度标签)`；每行前缀 `[银行业]`；头部明写「同日 335 个成员（按 industry_name 区分），按值降序展示 60 条（**已截断**）」/（全量）。**★ 为什么下限是 3 而不是 2**：`fed:policy_range` 的事故正是**同一天 2~3 条**而**没有任何字段能区分** —— 那不是横截面，是"一个 indicator 多值语义"的缺陷，正确修法是**拆成单值序列**（已拆 `fed:target_upper`/`fed:target_lower`/`fed:effr`）；用"同一天多条"当横截面判据会把那个缺陷**掩盖**掉，故专门留一条反向测试盯住。**顺带**：`value is None` 原先渲染成字面量 `None`（`dict.get(k, 默认)` 只在**键不存在**时给默认值），模型会读成 0 —— 现在渲染成 `缺失` | 补写PRD | 是 | 已闭环 | docs/PRD.md §19.10、src/domain/agents/analysis/base.py（`_CROSS_SECTION_*` / `_numeric` / `_dim_label` / `_cross_section_dim` + 头部与逐行渲染）、tests/unit/test_cross_section_context.py（**10 条**：值↔标签**配对**断言、打乱输入输出**逐字节相同**、截断**显式**、全量不许谎称截断、时间序列不被误判、**无区分字段的多值不许被掩盖**、缺失值不许渲染成 0、字符串数字按数值排序）、186 passed（分析层受影响面） |
| CHG-0075 | 2026-09-29 | 冲突 | PRD-18 | **推翻 §18.3 原判断并就地留废止痕迹**。原文：「A6 的 5 个环境诊断码**本轮刻意不定义**…**没有判据能产生它的错误码，就是"设了但不生效的开关"**」。**顾虑本身是对的**，被推翻的是"因此不定义"这个结论：L1 registry（`configs/data_stores.yaml` + `catalog/data_stores.py`，CHG-0067/0070）落地后，"这个存储在当前环境里可不可见"有了**单一事实源**，5 个码各自都有判据能产生它。故 `local_data.py::DiagCode` 由 **12 → 17**（补 `ENV_NOT_COVERED`/`PROD_ONLY`/`DEV_ONLY`/`DEV_SYNC_DELAY`/`PROD_PERMISSION_DENIED`），每码带 `DIAG_NEXT_STEP`，并**显式排除在联网兜底的触发表之外**（环境差异不是数据缺口，联网也拿不到；`FALLBACK_TRIGGER_CODES ∩ _NO_FALLBACK_REASONS == ∅` 由测试断言）。**处理方式照本项目硬约束**：§18.3 原文**保留**（加删除线 + `⚠️ 已废止（CHG-0075）`）、修正后的口径写在下方，§18.1 的 A6 行状态 ⏳→✅ —— **变更史本身就是需求**，删掉它下一个人会把旧口径当"从没提过"再提一遍 | 改写PRD | 是 | 已闭环 | docs/PRD.md §18.1（A6 行）、§18.3（废止痕迹 + 修正口径）、§19.5（17 码两维表）、src/infrastructure/catalog/local_data.py（`DiagCode` 17 码 / `DIAG_NEXT_STEP` / `ENQUEUEABLE_CODES`）、src/infrastructure/catalog/network_fallback.py（`_NO_FALLBACK_REASONS` = 5 个环境码）、tests/unit/test_local_data_executor.py（`test_all_diag_codes_defined_and_documented` 断言**两个维度集**而不是写死 17）、tests/unit/test_network_fallback.py（`test_trigger_codes_cover_every_diag_code_without_silent_gaps` 改成**并集相等 + 交集为空**，删掉 `assert len(real) == 12`） |

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
