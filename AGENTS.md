# Moss-FinAgent-Research 项目规则

## 项目定位
基于多Agent协作的AI辅助投研分析系统。面向二级市场提供投资分析与决策。

## 环境配置
- 本地运行Ollama（模型以 configs/models.yaml 为准：qwen2.5:1.5b-instruct-q4_K_M、qwen3:8b-q4_K_M）
- 存储默认SQLite零依赖；PostgreSQL/Redis为可选（DATA_BACKEND=postgres / REDIS_CACHE_ENABLED=true）
- 本地行情：**2026-09-23 起 QMT 排在所有链路的「最后一位」且默认关闭**。原因：本机
  QMT 终端失去行情权限、不再运行（`127.0.0.1:58610` 拒连），且**短期内无法恢复** ——
  放在任何位置之前都只会贡献一次必然失败的 xtquant 连接等待（实测 4~5s）。
  - 现行日线链：AkShare → 腾讯 → Tushare → baostock → 本地CSV → **[QMT 开关]（最末）**
  - 现行分钟/分时/快照链：腾讯 → 新浪 → 东财 → **[QMT 开关]（最末）**
  - ⚠️ **顺序与开关表达同一个意图**，两处都要改对：`configs/intraday.yaml` 的
    `data.intraday_sources` 把 `qmt` 写在**最后**、`data.qmt_enabled: false`。
    因为 `health.rank` 在样本 <3 次时**照抄配置顺序**当先验，所以"配置顺序 =
    默认顺序" —— 只改开关不改顺序，QMT 仍会顶到链首。
  - 将来权限恢复：`QMT_ENABLED=1` + `data.qmt_enabled: true` 即可在链尾兜底
  - 全市场全量下载：`scripts/download_market_data.py`（**取代** `scripts/download_qmt_data.py`，
    落盘到项目自己的 `data/quant/prices*/`，不再依赖 QMT 私有目录）
  - 东财可用性依赖 `MOSS_EM_DIRECT`（TLS SNI 阻断规避，可写 `.env`）；
    但该阻断会漂移，**东财只能当链尾备用**，见 `src/core/eastmoney_direct.py`
  - ⚠️ 竞价链的 `DataConfig.fallback` 已从 `"qmt"` 改为 `"none"`：竞价逐秒序列
    **没有免费替代源**（QMT tick 需权限、eltdx 只有盘中实时、Tushare 无竞价过程），
    如实登记"当前无备源"，不要留一个永远失败的 `"qmt"`
- 核心推理调用DeepSeek-V4-Flash API（DEEPSEEK_API_KEY 经环境变量注入）

## 架构规范
- 采用LangGraph StateGraph搭建Supervisor调度架构
- 18个专业Agent分5层：数据层(A01-A04)、信息层(A05-A07)、分析层(A08-A12)、行业层(A13-A16)、决策层(A17)、审计层(A18)
- Agent间通过标准JSON消息通信，基于MCP协议调用工具
- Demo阶段采用单进程分层架构（src layout），Agent以进程内模块注册到Supervisor，接口无状态；保留演进为独立FastAPI微服务的路径

## 编码规范
- Python 3.10+，使用类型注解
- 所有Agent必须实现统一的BaseAgent接口
- 所有数据访问必须通过统一数据层，禁止直接连接数据库
- 禁止硬编码敏感信息（API Key、数据库密码）
- 所有分析结论必须附带数据溯源标签和推理路径记录

## 数据溯源规范
- 每个数据点必须包含source_url、publish_time、fetch_time、raw_content_hash
- 数据处理链路必须记录每一步的Agent ID和操作类型
- LLM推理必须记录prompt_hash和token数
- 最终结论必须引用所有使用的数据ID

## 安全合规规范
- 所有输出附带免责声明
- 多租户数据通过RLS策略自动隔离
- 审计日志独立存储，不可篡改
- 敏感数据操作日志保存不低于3年

## Trae使用规范
- 使用Spec模式进行系统级开发
- 使用Task工具并行派发多个Agent开发任务
- 每个Agent独立开发、独立测试、独立部署

## 前端/接口改动规范
- 改 `web/src`（样式或组件）或配套接口前，**先读** `@.trae/skills/frontend-change-guardrails/SKILL.md`
  —— 它是从 2026-09-26~27 的真实报障里提炼的护栏：顶栏冻结的 16px 渐隐带、
  flex 挤压把一级页签挤折行、视图键三处枚举、删重复提示的正确姿势，
  以及"单请求耗时表会骗人，必须按并发与多宽度验收"的纪律。

## 性能硬约束（改任何会出现在首屏/交互路径上的代码前先读）
- **禁止新增串行往返。** 任何"必须等上一个请求回来才发得出去"的请求都算缺陷。
  门控数据（权限 / 清单 / 配置）一律**并进上一趟响应**，或首帧读本地缓存。
  本项目的基准：这条链路上**一次冷请求的固定开销约 0.85~0.9 秒，且与体积几乎无关**
  （实测 15 KB 与 64 KB 的分块耗时接近）—— 所以要减少的是**次数**，不是字节。
- 首屏启动路径的**串行往返上限 = 1**。新增请求必须同步更新时延预算表
  （见 `@.trae/skills/e2e-latency-budget/SKILL.md` §4.1）。
- 首屏入口 JS **≤ 120 KB(gzip)**。新面板**必须**代码分割，**并且**把它的分块加进预热列表
  —— 只分割不预热会让"首次点击"变慢，体感反而更差（这两件事是一对）。
- 面板数据**必须**复用既有的 cache / prefetch / keepalive 机制，**不得自造一套**。
  预取的续期间隔必须**小于**缓存 TTL；页面隐藏时跳过；失败静默。
- 改动完成后**必须**打真实产物验收：`manage.py build` → `manage.py ship-frontend` → 打公网入口。
  **本地 dev 快不等于线上快**；本项目真实发生过"源码早已优化并构建，但对外实例跑的是
  6 小时前的旧构建"。
- 判据写成**次数与 KB**，不要写毫秒 —— 毫秒换条线路就不成立，次数与 KB 才可 review、可断言。

## 告警/通知硬约束（新建任何通知渠道必读）
- 三层闸门缺一不可：**事件级去重**（同一件事别重复）+ **渠道级速率/合并**（一天很多件事别淹人）
  + **接收者级静默**（该不该在这个时间打扰）。只有第一层就会以"用户抱怨邮件太多"的形式漏水。
- 闸门状态**必须落盘**（放进程内存里的冷却，重启即失效）。
- 阈值有**单一权威默认值**；`.env` 覆盖必须在文档里登记差异。
- suppressed 的原因必须是**人话 + 剩余时间**（"冷却中，还剩 12 分钟"），不是枚举值。
- 详见 `@.trae/skills/alert-noise-and-first-paint/SKILL.md`。

## AI 首轮编码硬约束（改「花钱 / 并发 / 统计 / 权限 / 换模型」前必读）
- **上限必须写进代码，不能留在注释里。** 任何"一般不会超过…"都是缺陷：
  成本上限、并发上限、输出预算、显存占用，都要有**实测数字 + 超限行为**。
- **默认值即护栏**：安全的一侧做成默认（本地优先 / 限流 / fail-closed）。
  靠"每个调用点记得传 `local_only=True`"必然漏 —— 本项目实测因此漏过一次付费兜底。
- **同一判断只允许一份实现**：改判据前先全局搜同类实现；能抽常量就抽，
  并补一条"两条路径结果必须相等"的测试（在线人数 3≠1 就是这么来的）。
- **"没量到"与"量到 0"必须分开显示**；读不到数据时显示"未量到/无法统计"，
  绝不用 0 糊过去（宁可不显示，也不显示假绿）。
- **口径与局限随数据一起下发**（`basis_notes`），界面上标注"这个数字准不准"
  （精确 vs 推断）——否则用户会把估算当账单。
- **优化前后对比必须先清测量路径**：禁缓存 / 隔离环境 / 同输入 / 留原始输出。
  本项目实测踩过：LLM 缓存 scope 不含 provider/model，"换模型对比"三个模型输出逐字节相同。
- 详见 `@.trae/skills/ai-first-pass-defect-guard/SKILL.md`；
  本会话的 14 条缺陷复盘见 `docs/AI_FIRST_PASS_DEFECT_REVIEW_20260926.md`。

## 红灯纪律（容易违反，但代价最高）
- **不接受"已知的红色基线测试"。** 红灯只有三种合法归宿：修好 / 删除并说明为什么不再需要 /
  显式标记为预期差异**并链接到具体待办**。
- 本项目实测代价：9 条关于告警阈值的测试长期红灯，被当成背景噪音写进文档；
  它们其实是**配置漂移的哨兵**（测试断言 `alert_confidence_min=0.7`，而 `.env` 实际是 `0.6`），
  被无视两天后，同一个漂移以"客户抱怨邮件太多"的形式重新出现。
- 推论：**任何"既能在代码里写默认值、又能被环境变量覆盖"的阈值都必然漂移** ——
  所以要么把生效值纳入测试，要么在启动自检里对比并告警。

## AI 协作与编辑安全硬约束（改脚本 / 与另一个 agent 并行改同一仓库前必读）
- **能自动化的检查就不要写成提醒。** `.ps1` 的 UTF-8 BOM 是**文件隐藏属性、diff 里看不见**，
  任何"读进来再写回去"的工具（编辑器 / AI 改写 / 批处理）都可能丢它。丢了的症状是
  **解析失败即退出**：任务 `LastTaskResult=1`，却**一行日志都不写** ——
  与"健康时静默"完全无法区分。本项目实测：`scripts/tunnel_watchdog.ps1` 因此**长期没跑起来**，
  CF 那条链路等于**一直没有值守**。护栏：`tests/unit/test_ps1_encoding.py`
  （含非 ASCII 的 `.ps1` 必须有 BOM + 所有 `.ps1` 必须零错误解析 + `.vbs` 保持纯 ASCII）。
- **验证必须走真实触发路径。** `Start-ScheduledTask` 手动触发与 svchost 定时触发**不等价**：
  本项目实测手动触发"零可见窗口"，而真实触发每 5 分钟弹一个黑窗（曾被据此差点错误结案）。
- **健康判据要端到端，且"静默"必须与"已死"可区分。**"端口在听""进程还在""返回 0"
  都曾经是假判据（实测：端口在听但 SSH transport 已烂 → 请求首字节 20 秒不来）。
- **改之前先查最便宜的判据。** 本轮两个假设（`manage.py` 按项目路径误杀进程、
  未知等级→空清单）都在"查一下"这一步就被推翻。先查后改，比改完再回滚便宜一个数量级。
- **同一仓库可能有并发协作者。** 动手前看文件 mtime、避开对方正在改的文件；
  新增逻辑优先放进自己新建的文件（实测：把分块预热放进 `panelPrefetch.ts` 而不动 `App.tsx`）；
  临时文件用 `_` 前缀并在收尾时删除。
- 详见 `@.trae/skills/ai-collaboration-guardrails/SKILL.md`；
  本轮 14 条问题的症状/根因/修法/证据复盘见 `docs/SESSION_ROOTCAUSE_AND_FIXES_20260927.md`。

## 交互理解与编辑安全硬约束（一句话需求 / 增量改动前必读）
- **歧义时"选一个"不算错，"不说"才是错。** 用户一句话要求改行为时，
  先列出"**要改哪几处才能产生这个可观察行为**"。本项目实测：
  「事件告警最多保留三天，超过3天自动溢出删除」由**两个**字段共同决定 ——
  `alert_expire_days`（不再显示）与 `retention_alert_days`（真正 DELETE）。
  只改一个等于"没生效"，而两边都会以为自己对了。
- **"优化"类需求先出分解表，再谈方案。** 分解表本身常常已经回答了
  "该优化哪里"，而且它是**可证伪**的（本项目实测：分解后才发现 68% 花在一个 Agent 上）。
- **用户给的是症状，不是病灶。** 回答时必须先答他的问题、再给真正病灶、
  并给出证据（哪个文件、什么数字）——不要为了顺着用户而假装他的猜测成立。
- **"慢"必须分四组量**（冷/热/首次/重复）。本项目实测：冷 2.1s / 热 0.01s ——
  不分开量会去优化一个本来不慢的东西。
- **★ 成对分隔符：替换串必须包含完整配对，改完立刻编译。**
  本轮同类错误犯了 3 次：`"""` 被替换掉 → 中文散文变裸代码；
  registry 插入时**误删上一项的 `name=`**（编译器抓不到）；
  JSX 三元链丢 `? (` → 连续 3 次编译失败才定位。
  **结构性文件改完后必须跑一条"形状自检"**（如 registry 的 key 必须等于条目的 name）。
- **破坏性编辑前先备份。** 本项目工作区常有**别人的**未提交改动，
  你甚至无法用 `git checkout` 恢复 —— 回滚成本远高于 `Copy-Item` 到 temp。
- **修复的最小化是纪律**：注入式改动 > 新增文件式 > 重构。
  "顺手优化一下"在 AI 编码里是**风险放大器**（本轮实测：10 行能改完的事
  变成改三元链并编译失败 3 次）。
- **自己的检查脚本必须先自证**：喂一个"已知答案"的输入，确认它报 0。
  本轮实测：自写正则报 6 个假告警，差点去修没坏的东西。
- **测试失败时先问"这是我想要的语义吗"**，再问"代码错了吗"。
  本轮实测：差点去改一个正确的实现（未超期的孤儿事件**必须保留**）。
- 详见 `@.trae/skills/ai-interaction-and-edit-safety/SKILL.md`；
  本会话全部问题（含我自己引入的）见 `docs/PROBLEM_INVENTORY_20260926.md`。

## 护栏保真与效果验证硬约束（改判定条件 / 共享函数 / 配置键前必读）
- **"我改了"是意图，"它生效了"才是事实。** 两者之间隔四道门：
  **判据认不认得出 → 有没有人调它 → 有没有被层叠吃掉 → 有没有打到真实产物验证**。
  本项目实测三个最贵的问题分别卡在第二、一、三道门上，且**都不报错**：
  · 登录图形码判据用 `/captcha_required/.test(String(e))` 匹配 —— 而 `ApiError.message`
    已被换成中文、`String(e)` 里没有错误码 → 前端**永不渲染**图形码输入框，用户被锁在门外；
  · 调度作业在 `JOB_REGISTRY` 声明了，但执行器分发链没有分支 → 每次记"未知作业类型"失败；
  · 新增 CSS **前置**被后面的原始定义覆盖 → 规则在文件里、**根本不生效**
    （所以"搜字符串确认改动在产物里"这一层验证**会照样通过**，必须量 computed value）。
- **判据只认机器可读的标识（错误码 / 枚举 / 状态位），不认给人看的文案。**
  文案会被本地化、会被"优化得更友好"，每一次都会让某个隐藏分支静默失效。
- **隐藏 / 过滤 / 排除类需求，不要用"不写入"实现。** 本项目实测：中性新闻过滤是**读取侧**需求
  （显示时排除），却从**写入侧**实现（中性不落库）→ 展示层读不到记录、判成"未抽取"、照常显示。
  规则写了、测试也过了，**从未生效**。正确做法是落库并打标记，防线由**结构**保证。
- **改共享函数的输出形状前，`grep` 它的全部调用方。**
  本项目实测：给 `build_feed` 加"未定条目收成一行"后，三个内部消费者（倾向抽取/聚合/扫描）
  看到的"全部条目"变成 2 条，抽取当轮只处理 2 条就收工，日志显示"抽取 0 条"——
  **看起来像"没有可抽的"**。收容/折叠/截断属于**展示层**，必须做成显式开关。
- **同一个 key 写在 N 处，必有一处被漏改。** 本项目实测：功能 key `intel.radar` 同时写在
  `FEATURES`（权限矩阵）、`VIEW_FEATURE`（页签可见性）、`intel.py` 的 `FEATURE_*`（403 判据）。
  只改第一处 → **情报 5 个端点对所有人 403，包括管理员**。收尾必跑一致性脚本。
- **"收紧权限"必须配一条"该看到的人还看得到"的反向测试。**
  实测：把 `metrics` 移出 `FEATURES` 后 `visible_views` 的旧判据恒为 False →
  **连管理员都看不到「运行指标」**。故障方向是"管理员自己少了功能"，而没人会去测管理员。
- **验收打到真实产物**：`manage.py build` → `ship-frontend` → 比对**线上产物文件名与本地发布件是否一致**
  → 用**线上 CSS** 渲染同构 DOM 量几何/数值。判据写成**次数 / KB / 像素 / 布尔**，不写毫秒。
- **探针脚本不许有"把生产弄坏"的能力。** 实测：用生产 admin 反复登录验证，
  失败 4 次（阈值 5）、且密码当天已被改过 —— **再错一次就锁号**。
  探测只用测试账号；试错前先查"阈值是多少、已用掉几次"；发现不可逆后果就**立刻停手并上报**。
- 详见 `@.trae/skills/guard-fidelity-and-effect-verification/SKILL.md`；
  本会话问题复盘见 `docs/SESSION_ROOTCAUSE_AND_FIXES_INTEL_20260927.md`。

## 需求闭环与影响半径硬约束（**新增任何"一类东西"前必读**）

> 用户批评（2026-09-28 原话）：
> 「你的 AI 编程能力只是说一个做一个，缺少业务连续性、修改点对其他业务关联
>   模块的影响延伸分析、缺少下次再有这个场景能否不再出这个问题，你没有考虑
>   后续功能需要，缺少维护视角、业务功能需求连续性视角。」

- **先问"这是一个点还是一类"。** 用户说「`spot_summary` 没走索引」时，
  真实问题是**43 条指标里 32 条从未同步**。只修被点到的那个 = 把同一个 bug
  留到下次报障。**动手前先 `grep` 出全部同类现场，逐个给结论（修 / 不修+理由）。**
- **★ 开工前跑一条命令**（把"问 1"从提醒变成动作 —— 光有纪律挡不住，
  本轮实测：skill 里写着"先问是一类还是一个点"，我仍然只教了 A11、
  漏了同类的 A12–A16，**下一轮才补上**）：

  ```bash
  # 我要改的"概念"还散落在哪些地方？要动的"指标前缀"有哪些贯通点？
  uv run python scripts/preflight_check.py --keyword "<关键词>" --prefix "<前缀:>"
  # 我要改的文件，谁在用（影响半径）？
  uv run python scripts/preflight_check.py --file <path/to/file.py>
  ```

  输出三块：**同类现场**（按 ①配置/②领域/③编排/④接口/⑤测试/⑥文档/⑦脚本分层）、
  **贯通点逐项状态**（含每个 Agent 的 prompt 是否已教）、**影响半径**。
  贯通点清单与白名单**从代码读**（`NEW_INDICATOR_TOUCHPOINTS` /
  `_AGENT_DATA_WHITELIST`），不硬编码，所以它不会自己漂移。
  护栏见 `tests/unit/test_preflight_check.py` ——
  其中 `test_preflight_detects_missing_teaching` 就是本轮漏教的机器复现。
- **新增"一类东西"必须过贯通点清单**（单一事实源：
  `src/orchestration/supervisor.py::NEW_INDICATOR_TOUCHPOINTS`）。
  本项目实测漏的那一层是 **`_AGENT_DATA_WHITELIST`（分析层可见性）** ——
  数据落库了、索引登记了、SmartFetcher 取到了，**Agent 依然看不见**。
- **警惕"假绿"**：白名单命中 0 条会走 fallback（返回前 200 条），
  于是验证时"看起来保留了"，实际是数据**碰巧**混在里面；
  通用子串（如 `stock_`）还会**巧合**匹配到新指标。
  判据必须写成 **"精确命中数 == 预期数"**，不是"有没有出现"。
- **修 bug 必须配护栏**，三层至少做一层：断言测试 / 代码内结构清单 /
  可复跑审计脚本。**没加护栏的修复 = 下次换个地方再犯。**
- **交付时必须附"业务连续性推演"**：本次解锁了什么、还差什么、
  已知缺口是什么（诚实登记，不许省略）。
- **收尾自问**：**"下一个 AI 只读仓库、不读这段对话，能不能不犯同一个错？"**
  答不出"能"，就还没交付完。
- 详见 `@.trae/skills/requirement-closure-and-impact/SKILL.md`；
  本会话的可执行审计见 `@docs/DATA_INDEX_REQUIREMENTS_AUDIT.md` 与
  `scripts/audit_data_index.py`。

## 需求—PRD 对账硬约束（**每次对话都要做；改需求 / 动手写代码前必读**）

> 用户要求（2026-09-28 原话）：
> 「给 trae 再加一个 skill 护栏：每次对话检查用户输入的需求与开发 pr 文档的
>   出入，**没有的补充进去，有差异的改动**。」

- **每轮对话三步**：T0 把用户输入拆成**可判定的条目**（需求原话不得改写 +
  可观察行为 + 关键词 + 命中的 PRD 章节 + 判定）→ T1 跑对账命令 →
  T2 落账（补写 PRD / 改写 PRD / 只登台账但停在待办）。**没走完 T2 不算交付。**
- **★ 一条命令回答"和 PRD 有什么出入"**：

  ```bash
  uv run python scripts/prd_sync_check.py --keyword "<关键词>"   # 三态结论
  uv run python scripts/prd_sync_check.py --ledger               # 台账完整性（交付前必须 0 ERROR）
  uv run python scripts/prd_sync_check.py --self-test            # 检查器本身还可信
  ```

- **三态结论各有强制动作**（脚本直接给出，不许自己另起一套）：
  `OK`（PRD + 台账都有；口径是否一致仍需人工看）·
  `MISS_LEDGER`（PRD 有、台账无 → 补台账）·
  `MISS_PRD`（**仓库做了、PRD 查不到 → 补写 PRD**）·
  `TODO`（三面都没量到 → **「没量到」≠「量到 0」**，换关键词重扫后人工判断，**绝不当通过**）。
- **★ 落 PRD 才算闭环（两条机器判据）**：
  ① `类型 ∈ {新增, 冲突, 细化}` 且 `状态=已闭环` 而
     `处置 ∉ {补写PRD, 改写PRD}`（**冲突类**多一种合法处置 `标注废止`）→ 脚本报 ERROR；
  ② `状态=已闭环` 且 `PRD 已更新=是`，但 `docs/PRD.md` 里**搜不到这个 CHG 号** → 脚本报 ERROR
     —— **"我改了 ≠ 它生效了"**：不许在台账里单方面宣布改了 PRD。
  **只登台账不改 PRD，等于用户那两句话一句都没做到。**
- **改口径必须留废止痕迹**：不许直接删旧口径，写成
  `~~旧~~ → 新（CHG-xxxx，日期）` 或 `> ⚠️ 已废止（CHG-xxxx）：改为 …`。
  **变更史本身就是需求** —— 删掉它，下一个人会把旧口径当"从没提过"再提一遍。
- **需求改了又改 → 折叠，不删除**（用户 2026-09-28 裁定）：正文只留**现行口径**，
  被取代的原文**整段折进** `docs/PRD.md` **§13.3 归档区**，原处留一行指针；
  **§13.1 是唯一的"现行口径"入口** —— 任何"现在到底是多少"必须能在那里找到**数值**，
  而不是"看代码吧"。归档用 `####` 四级标题（不产生新章节 ID）。
- **交付前门禁在 CI，不只在你这台机器上**：`.github/workflows/ci.yml` 的
  「需求—PRD 对账门禁」跑 `--self-test` + `--ledger`（台账未随仓库发布时
  打 `::warning::` **显式跳过**，不静默绿灯）。脚本与 skill 已在 `.gitignore` 白名单内
  （`CHG-0047` / `CHG-0048`）—— 这样**换开发工具也生效**：
  可执行层（脚本/台账）与工具无关，纪律层靠根 `AGENTS.md` +
  转发层（`CLAUDE.md`、`.cursor/rules/requirement-prd-sync.mdc`、
  `.github/copilot-instructions.md`，**只转发不复制** —— 复制必然漂移）。
- **只在 PRD 末尾新增章节**：插在中间会让后面所有 H2 重编号，台账里
  `PRD-7.2` 这类引用**集体错位**（脚本只报"幻影章节"，人却会以为台账写错了）。
- **清单从 PRD 现读，不硬编码**：章节 ID 由 `--list-sections` 现算；
  PRD 加了章节而台账没跟上，`--ledger` 立刻报"未登记"。
- 现状证据（可复跑）：`docs/PRD.md` 原始版最后修改 **2026-09-14**，此后仓库长出大量
  PRD 里查不到的需求 —— `QMT`（PRD **0** 处 / 实现面 **440** 处）、
  `竞价`（0 / 580）、`拥挤度`（0 / 270）、`ETF`（0 / **1926**）。
  2026-09-28 首轮对账已落 4 条（`CHG-0001`~`CHG-0004`），其余 52 条 + 3 条内部矛盾
  以 `待办 / 待确认` 登记在台账，全量证据见 `@docs/PRD_ALIGNMENT_AUDIT_20260928.md`。
- 护栏：`tests/unit/test_prd_sync_check.py` —— 其中
  `test_missing_prd_entry_is_detected_end_to_end`（仓库做了但 PRD 查不到 → 必须
  报 `MISS_PRD`）、`test_new_requirement_must_land_in_prd_to_close`
  （未落 PRD 却标已闭环 → 必须 ERROR）、
  `test_claiming_prd_updated_without_trace_is_error`（声称改了 PRD 但 PRD 里没有
  CHG 号 → 必须 ERROR）就是上面三条纪律的机器复现。
- 台账：`@docs/REQUIREMENT_CHANGELOG.md`；详见
  `@.trae/skills/requirement-prd-sync/SKILL.md`。

## 交付完整性硬约束（**新建测试 / 脚本 / 文档引用前必读**）

> 本轮实测（2026-09-28）：我这一轮交付的 **9 个脚本全部不会入库**
> （`.gitignore:84 /scripts/*`，政策是"研究/运维/验证脚本不入库"），
> 而 `tests/unit/test_preflight_check.py` **会**入库却 import 了它们 ——
> **开发机 5212 passed 全绿，克隆下来必然红灯。** 本地一条测试都不会提醒你。
> 同一分钟内，并发协作者新增的 `test_prd_sync_check.py` 犯了**一模一样**的错；
> 该护栏上线即抓到。

- **引用关系必须与发布范围对齐。** 会入库的测试/文档引用一个不会入库的
  文件，是本项目最容易漏的一类缺陷 —— 因为**在开发机上它永远不报错**。
  ```bash
  uv run python -m pytest tests/unit/test_shipped_deps.py -q   # 三条判据
  ```
  ① 测试引用的 `scripts/x.py` 必须存在；② 未入库的脚本，引用它的测试**必须**
  有 `pytest.skip(..., allow_module_level=True)` 守卫（**知名降级**，不是静默红）；
  ③ 文档里的 `python scripts/<脚本名>.py` **命令**必须真实存在。
  判据只认"**调用**"不认"提及" —— 文档写「**新增** `scripts/x.py`」是待办提案，
  拿它报错就是误报（本轮实测踩过）。**占位符名（`x` / `foo` / `your_script` 等）
  由 `_PLACEHOLDER_NAMES` 排除 —— 判据要适应文档，不要让文档迁就判据。**
  判据 1 只对**会发布**的脚本要求存在性（未跟踪的脚本在克隆里本就不存在，
  按存在性判会让判据 1 在 CI **必红**）—— 豁免用 `_tracked()` 判定，**自带过期语义**
  （脚本一入库，存在性检查自动恢复，不需要维护会腐烂的豁免名单）。
- **发布与否是政策，工程只负责"让缺失不致命且可见"。**
  `scripts/` 默认不入库是既定政策；**但被 `AGENTS.md` 当命令引用的脚本必须发布**
  —— 2026-09-28 裁定：`scripts/prd_sync_check.py` 与 `.trae/skills/requirement-prd-sync/`
  已加入 `.gitignore` 白名单（台账 `CHG-0047`），并已把 `--ledger` 接进 CI（`CHG-0048`），
  因为"克隆环境里命令指向不存在的文件"正是本条硬约束要防的缺陷。
  其余未发布的脚本仍按"加 skip 守卫 + 已知降级"处理。
- **判断"代码里有什么"要用语法树，不要用正则。** 本轮正则版踩了三个坑
  （捕获组编号写错、`[\w,\s]+` 的 `\s` 含换行导致**跨行贪婪吞并** 3 条 import、
  文档字符串里的示例代码被当成真引用）—— 全部是**静默少算 / 假告警**。改用 `ast`。
- **豁免必须带过期检查。** `_KNOWN_UNGUARDED` 这类"已知在飞项"登记表，
  必须配一条测试断言"修好后条目要被删掉"，否则"显式登记"会退化成
  **永久豁免** —— 正是红灯纪律想防的那个失败模式。
- **并发协作者正在改的文件不要代改**（比对文件 mtime 与当前时间），
  登记 + 告知，而不是替他动手。

## 测试运行经济性硬约束（**跑测试前必读**）

> 用户质问（2026-09-28 原话）：
> 「为什么你刚刚做了全量测试，是什么规则让你这么做的？加一条护栏规则，
>   只有提交github时才跑全量测试，其他场景只运行本次修改相关的单元测试」

**诚实回答：当时没有任何规则要求跑全量 —— 那是习惯。** 根因是
**不知道哪些测试与改动相关**，"全量"是不知道时唯一能保证不漏的做法。
代价可量化：本轮跑了 **8 次全量 × 约 11 分钟 ≈ 90 分钟**纯等待。

- **日常只跑本次改动相关的测试**（规则要立得住，前提是"相关"可判定）：
  ```bash
  uv run python scripts/affected_tests.py              # 本次改动影响哪些测试
  uv run python scripts/affected_tests.py --run        # 直接跑它们
  uv run python scripts/affected_tests.py --explain    # 每个测试为何被选中
  ```
  判据三层：**① 测试 import 了该模块**（`ast` 解析）· **② 传递引用**（有界闭包）·
  **③ 测试里出现了该资源路径字符串**（如 `configs/models.yaml`）。
  实测：改 `planner.py` → 选中 **26** 个测试文件（全量是 300+ 文件）；
  纯文档改动 → **0** 个。
- **全量只在两个时机跑**：① 提交/推送前（CI 也会跑，见
  `.github/workflows/ci.yml` 的 `pytest tests/ -q`）；② 改到**高扇出文件**时。
  选中器会**主动提示**高扇出，不靠你记得：
  `src/infrastructure/llm/models.py`（层字面量）·
  `src/orchestration/supervisor.py`（白名单/贯通点清单）·
  `configs/models.yaml`（每层路由）。
- **为什么"高扇出"必须显式列出**：本轮实测 —— `configs/models.yaml` 与
  `models.py` 的改动被**三条我事先不知道的协作者护栏**抓到
  （`test_glm_concurrency_cap_in_config`、`test_routing_fallbacks_are_cross_provider`、
  `test_intel_vocab` 的层解析）。只跑"相关测试"会漏掉它们，
  所以这两类文件必须回到全量，而不是靠运气。
- **禁止把"跑全量"当默认动作**：它挤掉的是验证质量本身
  （本轮为等测试牺牲的时间，本可用于多想一个问题）。

## Skills引用
@.trae/skills/ai-dev-rules/SKILL.md
@.trae/skills/dev-standards/SKILL.md
@.trae/skills/dev-standards/references/architecture-naming-spec.md
@.trae/skills/frontend-change-guardrails/SKILL.md
@.trae/skills/e2e-latency-budget/SKILL.md
@.trae/skills/alert-noise-and-first-paint/SKILL.md
@.trae/skills/ai-first-pass-defect-guard/SKILL.md
@.trae/skills/ai-collaboration-guardrails/SKILL.md
@.trae/skills/ai-interaction-and-edit-safety/SKILL.md
@.trae/skills/claim-evidence-consistency/SKILL.md
@.trae/skills/guard-fidelity-and-effect-verification/SKILL.md
@.trae/skills/requirement-closure-and-impact/SKILL.md
@.trae/skills/requirement-prd-sync/SKILL.md
@.trae/skills/dev-test-guardrails/SKILL.md
@.trae/skills/silent-degradation-and-false-green/SKILL.md
