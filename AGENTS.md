# Moss-FinAgent-Research 项目规则

## 项目定位
基于多Agent协作的AI辅助投研分析系统。面向二级市场提供投资分析与决策。

## 环境配置
- 本地运行Ollama（模型以 `configs/models.yaml` 为**唯一真值源**：`qwen2.5:1.5b-instruct-q4_K_M`（`local_light`）、**`qwen3.5:4b`**（`local_medium`）、`deepseek-r1:7b`（`local_reasoning`，默认不接线））
  - ⚠️ **2026-09-30 更正（`CHG-0112`）**：本行原文写的是 `qwen3:8b-q4_K_M`，那是**第二十三轮之前**的档位。
    第二十三轮把 `local_medium` 换成了 `qwen3.5:4b`（`CHG-0064`；准入判据与实测见 `docs/PRD.md` §17.2/§17.3）。
    判据写成**从配置现读**、不写死：`configs/models.yaml` 里 `local_medium.model_name == "qwen3.5:4b"`。
    `docs/PRD.md` §7.2 里"`qwen3.5:4b` 已被显式排除"那段是**更早**（CHG-0003 时期）的实测结论，
    已就地标注"被 `CHG-0064` 取代一半"——**看现行档位只认 §17.2 与配置文件**。
- 存储默认SQLite零依赖；PostgreSQL/Redis为可选（DATA_BACKEND=postgres / REDIS_CACHE_ENABLED=true）
- 本地行情：**2026-09-23 起 QMT 排在所有链路的「最后一位」且默认关闭**。原因：本机
  QMT 终端失去行情权限、不再运行（`127.0.0.1:58610` 拒连），且**短期内无法恢复** ——
  放在任何位置之前都只会贡献一次必然失败的 xtquant 连接等待（实测 4~5s）。
  - 现行日线链：AkShare → 腾讯 → Tushare → baostock → **[本地CSV 开关]（`LOCAL_QUOTE_DIR`，默认关闭）** → **[QMT 开关]（最末）**
    - ⚠️ **2026-09-28 起 `LOCAL_QUOTE_DIR` 留空**：它原指向 `D:/quantTrader/data`，
      而**那正好是本机 MariaDB 的 datadir**（`mysqld --defaults-file` 指向的
      `my.ini` 里写着 `datadir=D:/quantTrader/data`）。该目录下 `SH/`+`SZ/`
      两个 QMT CSV 子目录已删除（约 1.13 GiB，停在 2026-08-31）→ 本跳已从链上移除。
      **要恢复必须指向专用导出目录，不得再指向任何数据库 datadir** —— 否则下一次
      "清理导出目录"就会删掉一个正在运行的数据库（`ibdata1`/`undo*`/`wucai_trade`）。
    - 判据：`settings.local_quote_dir` 为空 → 两个链都**不注册** `LocalCsvConnector`
      （`src/api/runtime.py` 两处均由 `if settings.local_quote_dir:` 守卫）。
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
- **外部联网源（2026-09-30 登记；现行状态以 `check_external_sources.py` 为准）**

  | 源 | 角色 | 额度 | 现状 |
  |---|---|---|---|
  | **博查搜索**（`bocha_search`） | **搜索主源** | 一次性**总量 1000** | ✅ 可用（`CHG-0114` 起接为 **path C**） |
  | **百度千帆智能搜索**（`baidusearch`） | **搜索备用** | **每天 100** | ✅ 可用（`CHG-0117`；router 自动换手） |
  | **东财 Choice EMQuantAPI** | **数据连接器**（不是搜索源） | 按账号权限 | ⛔ **账号未开通「量化接口」权限**（`code:160`） |

  **接线前、以及每次排查，先跑这一条**（退出码 `0`=全部可用 / `3`=有源被拒 / `2`=探针自己坏了）：
  ```bash
  uv run python scripts/check_external_sources.py
  ```
  - **⚠️ 凭据必须写进项目根 `.env`，不能只写环境变量**：`bocha_search`（用户作用域）、
    `baidusearch`（机器作用域）实测**已在运行的进程都继承不到**，症状是
    "配置里明明有、生产路径报未配置"。`.env` 已 gitignore，与 `DEEPSEEK_API_KEY` 同一约定。
  - **三个坑看着都像"源坏了"，其实都不是**，别再查一遍：① `.pth` 缺失 ⇒
    `CDLL('')` ⇒ **`WinError 87 参数错误`**（包解压了但没装；用 SDK 自带的
    `installEmQuantAPI.py`，别自己造路径注入）；② key 在**用户/机器**作用域而
    **进程没继承** ⇒ 被误读成"未配置"（三档要分开读）；③ 代码里读了个**不存在的配置单例**
    再被 `except` 吞掉 ⇒ 也会伪装成"未配置"（真名是 `get_settings()`）。
  - **搜索只走后台路径**（主 15s + 备 20s 最坏），**绝不挂交互路径**（交互预算 10s）；
    两家闸门**故意不共享**（总量 vs 日配额语义不同，共享等于共享单点）。
  - **单测绝不允许有能力花真钱**：搜索类判据一律用**注入的假 transport/provider**，
    并把真实 provider 换成**会抛异常的哨兵**断言"没被调用"——本项目已因打错补丁
    **真实花过一次额度**（`CHG-0117` / PRD §19.33.7 自伤②）。
  - **东财是数据连接器、不是搜索源**：`dir(c)` 实测 43 个接口里**没有任何网页搜索**，
    `cfnquery`/`edbquery`/`pquery` 只是查**它自己库**的结构化内容。
  - **★ 东财"能不能用"的唯一判定处是 `src/infrastructure/connectors/choice_gate.py`**
    （**门，不是源**：不取数、不注册连接器）。六种状态 `ok`/`no_access`/`config_missing`/
    `sdk_missing`/`unreachable`/`probe_error` **两两可分**，且**只有 `ok` 允许接线**
    （`assert_wireable()` 拒绝时带"谁去做什么"）。
    ⚠️ 别把 `unreachable`/`probe_error` 读成"账号没权限" —— 那会让排查方向整个跑偏。
    **要在链上接 Choice，必须先让闸门 `ok`**；`tests/unit/test_choice_entitlement_gate.py`
    里有一条条件式判据会在你接线时自动去跑它。
  - 口径全文见 `docs/PRD.md` **§19.33**；盘点见台账 `CHG-0113`/`CHG-0114`/`CHG-0117`。

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
- **★★★ 清理临时文件：只按"名字"删，永远不按通配符，且先看 git 状态。**
  实测事故（2026-09-30，我造成的）：一条"清理临时探针"的命令
  `Get-ChildItem scripts\_*.py | Where-Object { $keep -notcontains $_.Name } | Remove-Item`
  一次删掉 **230 个**脚本 —— **已跟踪的被 `git checkout -- scripts/` 救回**，
  但**未跟踪的永久丢失**：`scripts/affected_tests.py`、`scripts/preflight_check.py`
  （两条 `AGENTS.md` 里当**必跑命令**引用的工具）以及 **80+ 个 `_probe_*` 证据脚本**。
  回收站 0 项、卷影 0 个、无仓库备份 ⇒ **不可恢复**。
  **为什么这个坑特别深**：本项目政策是 `/scripts/*` **不入库**，
  于是它们"在克隆里本来就不存在"—— 那句 skip 守卫读起来像"没关系"，
  而**在本机它们就是唯一副本**。
  三条纪律：① 只删**自己这一轮创建的文件**，逐个列名字（不写模式）；
  ② 删之前先 `git status --porcelain <目录>`：**未跟踪 = 删了找不回来**；
  ③ 拿不准就留着 —— 一个多余的文件，代价远小于一个丢失的证据脚本。
- 详见 `@.trae/skills/ai-collaboration-guardrails/SKILL.md`；
  本轮 14 条问题的症状/根因/修法/证据复盘见 `docs/SESSION_ROOTCAUSE_AND_FIXES_20260927.md`。
- 详见 `@.trae/skills/ai-defect-and-misread-guard/SKILL.md`（判据五维 / 无痕死亡取证 / 并发安全 / 编辑与并发写 / 横切改动的作用域维与类型维 / **结构变更自带静默失效** / **生效层的求值与通道**）与 `@.trae/skills/ai-session-hygiene-and-concurrency-guard/SKILL.md`（会话卫生 / 测试台架即生产 / 测量与被测同层）。

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

## 分析层数据可见性硬约束（改指标 / 白名单 / 分析 Agent prompt 前必读）

> 报障（2026-09-28）：用户问「预测下一年美国的加息、降息节奏」，
> 系统答「**缺少联邦基金利率数据，无法判定方向**」——
> 而库里 `fed:policy_range` 有 3 条、`us_fed_rate` 72 条、
> `us_unemployment`/`us_nonfarm` 各 107 条。**数据到了，Agent 看不见。**

- **白名单按 `indicator` 的 en_id / 族前缀匹配，不按中文标签。**
  库里存的是 `us_nonfarm` / `fed:effr`；写 `"非农"` / `"FedWatch"`
  **一条都放不过去**（匹配是子串）。实测：A08 的 22 个词在 719 种指标里
  **只放行 4 种**。→ 优先写 `us_` / `fed:` / `mkt:` / `idx_val:` 这类**族前缀**。
  **单一真值源**：`src/orchestration/supervisor.py::_AGENT_DATA_WHITELIST`。
- **★ 新采一个指标 → 先问"谁看得见"。** 「落库 + 登记索引 + SmartFetcher 取到」
  **不等于**「分析层看得见」——中间隔着白名单这一层，漏了**不报错**，
  只表现为"结论里没有这一维"。跑 `uv run python scripts/preflight_check.py --keyword "<前缀>"`。
- **"白名单命中 0 条走兜底"是假绿。** `_filter_points_for_agent` 在 0 命中时
  返回前 N 条 → Agent 拿到的是**任意数据**，看起来"有输入"。
  判据要写成**精确命中数 == 预期数**，不是"有没有出现"。
- **行业 Agent 有双层过滤，白名单在前。** 白名单挡掉后
  `watch_keywords` 永远看不到 → `industry/base.py::_skip_reason()` 判
  "无本行业关注指标" → **静默跳过 LLM**，界面显示"没什么可分析的"。
- **规则式路径也必须落审计**（`provider="rule"`、`tokens=0`）。
  审计原先只在网关里写，规则路径直接 `return` → **"走了模板"与"压根没跑"
  在审计里长得一模一样**，排查方向被带偏。见
  `src/domain/agents/analysis/base.py::_audit_rule_only`。
- **同一个 indicator 不许有多值语义。** 实测 `fed:policy_range` 同日 3 条点
  （上限/下限/有效利率），取"最新一条"**把区间下限当成政策利率**报出去
  —— 数字看着有据、语义是错的，比"缺数据"更危险。已拆成
  `fed:target_upper` / `fed:target_lower` / `fed:effr` 三个**单值序列**。
- **护栏（改完必跑，4 个文件都是常驻契约）**：
  ```bash
  uv run python -m pytest tests/unit/test_whitelist_coverage.py \
      tests/unit/test_macro_agent.py tests/unit/test_query_signal_routing.py \
      tests/unit/test_fed_series_split.py -q
  ```
  `test_whitelist_coverage.py` 是**三向**断言：登记↔白名单↔Agent 域
  （既有的 `test_indicator_prefix_wiring.py` 只验"我声明的前缀有没有贯通"，
  **对"没人声明过的指标"完全无感** —— 这就是本条能活到今天的原因）。
- 现行口径全文见 `docs/PRD.md` **§十五**（`CHG-0057`）。

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

## 本地数据确定性流水线硬约束（改「找数据 / 空结果 / 指标登记 / 联网兜底」前必读）

> 口径全文：`docs/PRD.md` **§十九**（`CHG-0072`~`CHG-0075`）。
> 触发它的用户原话：「不要指望靠提示词让 Agent『优先查本地库』，也不要让采集
> Agent 直接连数据库自由写 SQL。要把『找数据』做成一条**确定性流水线**。」

链路与单一真值源（**别另写一套**）：

```
① validated_points → ② catalog/local_data.py → ③ ConnectorRouter → ④ network_fallback.py
   元数据目录 catalog/column_index.py   语义解析 catalog/synonym_dict.py
```

- **★ 「没量到」有三种情形，不是两种。** 本项目已为此付过三次代价：
  ①**没量到**（上下文里没有这条指标）；②**量到 0**（值是 0.0，是有效读数）；
  ③**源给了 0，但那个 0 在语义上不成立**（实测：新浪财务分析模板对
  600036/000001 的 `产权比率(%)` 列**非空率 10/10、值就是 `[0,0,0]`**，
  而 600519 = 17.7969 —— 银行的杠杆不可能是 0%）。
  三者**必须分开处置**，且 ③ **不许用「整列为 0 就当空值」这类启发式**修 ——
  真有一批公司的某个比率本来就是 0（如无有息负债），启发式会把假绿换成假红。
  判据与证据：`scripts/_probe_chanquan_zero.py`、`tests/unit/test_compliance_input_provenance.py`
  （`test_zero_is_a_measurement_not_a_gap` 逐族参数化）。
- **★ A12 合规的等级有四个取值：`高 / 中 / 无 / 未量到`，后两个绝不许合并。**
  原始缺陷：`evaluate_compliance` 的 `else` 分支把「6 个族都量到了、都没超阈值」
  与「**6 个族一条都没量到**」输出成**一字不差**的「无 + 未见明显合规风险信号 +
  `confidence=high`」，而且因 `level=="无"` 走纯规则路径**跳过 LLM**
  ⇒ **整条链路没有任何环节报错**。这是一张**伪造的体检合格证**。
  判据：无输入时必须给 `LEVEL_UNMEASURED` + `FLAG_UNMEASURED`，
  且 `_rule_only_reason ∈ {measured_clean, no_input}` 落进结果
  （没有它，"走了纯规则"与"压根没跑"在审计里长得一样）。
- **★ 同一缺陷会在多层重复出现，改完一层必须问「还有哪一层会把它吃掉」。**
  实测同一个"数据到了但用不上"的缺陷在本项目里出现过**四个位置**：
  白名单（`_AGENT_DATA_WHITELIST` 按 en_id/族前缀匹配，中文标签一条都放不过）·
  连接器（`supports()` 里根本没有这个指标）· 登记表（`indicators.yaml` 没登记
  → SmartFetcher 判未登记 → 每次联网）· **prompt**（数据进得了上下文，
  但 Agent 的 `system_prompt` 从没提过它 ⇒ 模型不知道那是信号）。
  最近一次实测：A12 的 5 个合规族生产者落地后，白名单仍挡掉
  `商誉`/`货币资金`/`有息负债` ⇒ 商誉档与存贷双高（需两个输入同时到手）
  **恒不触发**。**收尾自问：这条数据从产生到被消费，中间有几道闸？逐道验。**
- **★ 豁免表要派生，不要手写。** 手写豁免表**不会自己长大** ——
  下一个「白名单声称了、连接器也取得到、就是没人登记」的指标它一个字都不会说。
  本项目已把这张表**退役**，换成派生断言
  `test_whitelist_keywords_a_connector_accepts_are_registered`
  （遍历白名单 × `supports()`，**零清单零豁免**，断言里明写"不要给它加豁免"）。
  同理 `test_contract_consistency.py` 的四向断言、`test_rule_family_visibility.py`
  的族可见性断言（族清单**现读** `logic._RULE_FAMILIES`）。
- **★ 幻影关键词肉眼看不出来。** 白名单写 `"GDP同比"`（无冒号）而登记的 id 是
  `GDP:同比`（有冒号）→ 子串匹配不到 ⇒ **一条都放不过去**，而注释里写着
  「GDP 同比我放行了」。**修法是改白名单，不是留着豁免**（留豁免等于把这句假话
  登记成合法状态）。判据：`test_whitelist_coverage.py`（关键词必须能在
  `indicators.yaml` 里匹配到登记指标 / 必须能被 `supports()` 认）。
- **★ 横截面不是时间序列。** `:all` 类指标（申万行业、指数快照）实测 335 条
  **同一天**、每条属于不同成员；而 `context_max_periods` 会把它当"最近 N **期**"
  截断 ⇒ 同一天 335 个成员被**任意**留下 N 个，**两次运行答案不同且无法复现**。
  判据：同一 `period_date` ≥3 条**且** `extra` 里存在能区分成员的键 → 按
  `(期 desc, 值 desc, 维度标签)` 全序排序 + 每行前缀 `[成员名]` + 头部明写
  「同日 N 个成员 / 展示 M 条 / **已截断**」。
  ⚠️ 下限取 3 不取 2：`fed:policy_range`（同日上限/下限/有效利率、
  **无任何字段能区分**）是"一个 indicator 多值语义"的**缺陷**，
  用"同一天多条"当横截面判据会把它**掩盖**掉。
  护栏：`tests/unit/test_cross_section_context.py`（含"打乱输入 → 输出逐字节相同"）。
- **联网兜底必须 fail-closed，且三护栏缺一不可**：合法源白名单（**默认空 = 全禁**）·
  预算上限（默认 ¥2.0/日、30 次/时）· 失败可观测（冷却 600s / 限流 1800s / 熔断 3 次）。
  `FALLBACK_TRIGGER_CODES ∪ _NO_FALLBACK_REASONS == DiagCode 全集` **且互斥**，
  判据**不写死码数**（写 `len(real) == 12` 会在补第 13 个码时把真信息盖掉）。
  `NOT_APPLICABLE_FOR_ENTITY`（银行没有流动比率）与 5 个环境码**都不触发联网** ——
  联网也拿不到，硬试只是烧钱。**要开启必须由用户显式确认白名单与预算数字。**
- **★ 本机探针脚本自己会错，先自证再下结论。** 本轮同一个坑踩了三次：
  ①按 `supports()`/同步 `fetch()` 探 `ConnectorRouter` —— 它**没有** `supports()`、
  `fetch()` 是 **async** ⇒ 10 条全报"取不到"，**看起来像数据缺失，实际是探针错**；
  ②`print("✅ …")` 在中文 Windows 控制台（GBK）直接 `UnicodeEncodeError` 崩，
  **看起来像检查脚本挂了** ⇒ 任何 print 非 GBK 字符的本机脚本**开头必须**
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；
  ③判据写宽了会自己造假绿（匹配串构造错 → 报"无人放行"，
  用真实判据复算其实是 `True`）。**权威入口只有一个**：
  `build_runtime().agents["A01_data_collector"]._backend`。
- **★ 「我改了」到「它生效了」隔四道门，本轮卡在第三道。**
  `NetworkFallback`（护栏齐全、30 条测试全绿）在仓库里**零个生产调用方** ——
  用户要的"最差兜底"**从未发生过一次**。任何新模块收尾时必须回答：
  **谁调它？在那条链路的哪一行？拿掉它哪条测试会红？**
- **★★★ 回答"平台有没有这个数据"时，禁止只搜 schema —— 我连错三次，全部是把"我没找到"说成"平台没有"。**
  用户说「这个数据也有啊」时，**先假设他对**，再去把数据的**真实路径**追出来。
  三次实测（2026-09-29，同一天内）：

  | 我说过 | 真相 | 我漏查的那一层 |
  |---|---|---|
  | 「平台没有个股级解禁数据」（我搜了所有库的表名/列名，`unlock/解禁/lift` 零命中） | **有**：`fact_data_points.extra_json.top_stocks` 带 `code/name/market_cap/pct_of_float/share_type` | **JSON 列**（列名叫 `extra_json`，按业务词搜列名永远搜不到）；以及**接口层** `domain/intel/calendar.py::fetch_unlock_schedule()`（东财 `stock_restricted_release_detail_em` 按个股给） |
  | 「个股→概念相关度排序不可靠」（我只查了 `ml_member_pure`：600036 仅 1 条且 `relevant=0`） | **可靠**：`ml_stock_theme` 覆盖 **4,996 只**（`final_score` 全非空、带 `reason` 人话依据）；`ml_member_corr` 覆盖 **5,215 只**（带 `corr/samples/日期区间`） | **选错了表**：`ml_member_pure` 是**以板块为键的候选池**，不是全市场逐票映射 |
  | 「估值水位要自己按分位定档」 | **平台已有权威实现** `src/intraday/valuation.py`（前端「估值透支/估值合理偏贵」就是它给的；复用后实测与界面**逐字一致**） | **代码层**：只搜了数据表，没搜"这个判断是不是已经有人实现了" |

  **判据（一条命令式的自检，写下来比记着可靠）** —— 下结论"没有"之前必须过完这四层：
  1. **表/列**：`PRAGMA table_info` 全库扫（但**列名可能不含业务词**）；
  2. **JSON 列**：把每个 `*_json` / `extra*` / `payload` 列的值抽样看一遍（业务词通常在这里面）；
  3. **接口/服务层**：`grep` 业务词（`解禁`/`拥挤度`/`相关性`）在 `src/**` 的**函数名与路由**里 —— 数据可能是**实时取的**而不是落库的；
  4. **已有实现**：`grep` 那个**结论词**（`估值透支`/`合理偏贵`）—— 若有现成实现，**必须复用**，否则同一个判断会有两份实现，界面与 Agent 会给出不同答案。
  **"我没搜到"≠"平台没有"；把后者说出口之前，先说出你搜了哪四层、每层的原始输出是什么。**

- **★ 平台自己的功能板块 = 第四条链，不要重写它的取数。** 「板块拥挤度 / 主线挖掘 /
  个股估值打分 / 投资日历解禁 / 板块资金流 / 行业轮动日报」这些**早就在平台上跑着**
  （各有 `src/<模块>/` + REST 路由 + 调度作业 + 前端面板），但 PRD/Agent 侧可能是**零登记**
  —— 2026-09-29 实测：「板块资金流」「行业轮动」两个词的 `prd_sync_check --keyword`
  结论都是 **MISS_PRD**（仓库做了、PRD 查不到）。
  于是"接进投研分析"在文档层面看起来像**新建能力**，实际只是**接线**。
  纪律三条：
  1. **只调用既有入口**（provider/service/store），**不绕过它自建 SQL** ——
     否则会出现"界面一个数、Agent 另一个数"（§19.16 纪律一的同一个失败模式）；
  2. 口径差异（实时 vs 历史、报告落伍几天、含不含同业对标）**随 extra 下发**，
     不许沉默地选一个；
  3. 同一判断（如"估值透支/合理偏贵"）**只有一份实现** —— `grep` 结论词，有就复用。
- **★★ 「找不到」的下一步是「去联网找」，不是「不提示」。**（口径 2026-09-29 变更）
  用户先说过「行业↔概念板块**如果找不到就不提示未找到数据**」，同一天又改成
  「**"找不到就不提示"改成 找不到就去联网搜索找**」—— 后者是**现行口径**。
  - 旧口径（**已废止**）的落法是"规划期先问本地有没有，没有就**不排**这个指标"，
    于是采集不跑、界面干净 —— 但它**永远不会联网**（当时采集链上没有联网兜底），
    等于"这个族对用户彻底不存在"。**废止理由就写在这**：静默不是能力。
  - 现行：**照排**；本地/连接器拿不到 → 采集链上的**联网兜底**
    （`supervisor._network_lookup_for_collection`，走 `NetworkFallback` 的
    源白名单 + 预算 + 冷却熔断，**默认 fail-closed**）；联网也没有才按缺口如实上报。
  - 因此**三处文档必须一起改**（本项目实测过"同一个 key 写在 N 处，必有一处漏改"）：
    `docs/PRD.md`（§19.17 旧口径已标注废止 + §19.19 现行口径）、
    `AGENTS.md` 本条、`configs/indicators.yaml` 的族注释 —— 判据见
    `tests/unit/test_platform_data_connector.py::test_plan_augmentation_always_plans_crowding`。
- **★★★ 交互路径必须有「防撞钟」**（用户 2026-09-29：「加个防止撞钟的设置，
  **10 秒钟找不到就自动终止**」）。单一真值源
  `src/core/intel_limits.py::QUERY_DEADLINE_SEC`（默认 10s，`.env` 用
  `MOSS_QUERY_DEADLINE_SEC` 覆盖），**只管交互路径**：
  A01 采集（`_collect_one` 传 `deadline_sec` + 外层硬上限）与 A17 的 `query_data`
  连接器跳；**定时作业/预热路径不传**（重活正是要在那里做，掐掉它们
  "预热养缓存"就永远养不起来 —— 那是"修一个坏一个"）。
  实测依据（定 10s 而不是拍脑袋）：能用的族最慢 **6.7s**（板块资金流冷启动），
  10s 只切掉"不可达"（CME 23.4s）与"病态慢"（质押整表 **251.4s**，实测占了一轮
  275s 墙钟的 92%）；惩罚是**超时的那条指标这次没有数据**，而不是整轮失败。
  ⚠️ **超时不算"源坏了"**：不许记失败冷却（否则一个只是慢的源会被永久踢出链）。
  护栏：`tests/unit/test_query_deadline.py`（7 条，含"不传预算时行为不变"）。
- **★★★ 「库里的字段可达」是离线判据，不是"记得去读"**（用户 2026-09-29 原话：
  「连接器确保数据库里的所有字段（至少相关表都要遍历到）**可达数据库、可匹配获取到**
  （不走云端大模型花，**不花钱**的测试用例）」）。三层判据在
  `tests/unit/test_connector_field_reachability.py`（**6 条 / 2.3s / 零 LLM**）：
  **A 列级**（判据**从 `PRAGMA table_info` 现读** ⇒ 库里加一列自动进入判据，
  **不维护任何清单**）· **B 表级**（声明的表必须被真的读到；源码里读的表必须在声明里）·
  **C 行为级**（**抄真库 DDL 建临时库 + 哨兵值**，逐族 `fetch()` 必须产点且
  **哨兵值**出现在结果里 —— 只有它能抓住"代码提到了但取不出来"）。
  **三条纪律**：① **判据红了就真修，不许登记豁免**（`_UNMAPPED_COLUMNS` 至今为空；
  本轮因此真的接上 10 列 → `估值水位.extra.raw_inputs`（13 列 + 人话口径）
  与 `概念拥挤度.extra.themes[].provenance`）；
  ② **A 只是必要条件** —— 把列名写进"口径说明字典"也算"引用"，A 照样绿
  ⇒ **必须与 C 成对读**；C 的哨兵判据**只认值、不认名字**
  （`"samples"` 里就含 `ps` ⇒ 名字判据会让 `ps` 永远"可达"，这是**自造假绿**）；
  ③ **附加字段不许把族打挂**：`raw_inputs` 取不到时**如实缺 + 带原因 + 不填 0**，
  断言见 `test_raw_inputs_degrades_without_breaking_the_family`
  （列清单同样**现读 schema**，写死列名会在缺列的环境 `no such column`）。
  **写这类判据的三个实测坑**：假红（Windows asyncio 的 `socketpair` **自管道**
  ⇒ loopback 必须放行且**透传**，否则每个族都在建事件循环时就红）·
  老化（哨兵日期**相对今天算**，写死会被 30/90 天窗口过滤掉，症状像"库里有数据却取不到"）·
  崩溃（未给哨兵的列按**声明类型**兜值，一律填字符串会 `datatype mismatch`）。
- **★★★ 计划里的「名字」有两种错，方向相反，必须都挡**（用户 2026-09-29 两次报障）：
  ① **后缀加多了**：宏观指标被拼上个股代码
  （`fed:effr:300068` / `us_cpi_yoy:300068` …**九条全废**）⇒ 判据
  `supervisor.strip_bogus_code_suffix()`：**派生自登记表**
  （整串查不到登记 + 掐掉尾段 6 位数字能查到登记 + 登记形态无 `{code}` 占位符 ⇒ 掐掉）。
  ② **后缀不能少**：裸名（`每股净资产`）进计划 ⇒ `无连接器支持指标`。
  判据 `_needs_code_suffix()` 是**两张表求并集**：显式集 ∪
  **现读目录里写了"需带代码后缀"的那些** —— 实测它们漂移过
  （目录 **40 条**声明 vs 判据 **24 个**），护栏
  `test_catalog_suffix_declarations_are_enforced`（参数化，加一条目录自动扩展）。
  ⚠️ **只按登记表判会修一个坏一个**：`商誉占净资产比`/`大股东质押比例`/`ROE`
  在 `indicators.yaml` 里登记的**就是裸名字**（文件自己登记了这条既有缺口），
  它们**必须带后缀**，得留豁免。
- **★★★ 「用户填的标的」与「问句点名的标的」冲突时，以问句为准**（用户原话：
  「可能是我问的文字内容是招商银行，但是前端输入的标的却是 300068（ST南都）导致报错。
  **这个要做一个防错机制吗？**」——**要，而且已经做了**）：
  `api/routes/research.resolve_analysis_subject()` 是**唯一判定处** ——
  输入标的是**裸 6 位代码**且问句能解析出**不同的**股票 ⇒ 以问句为准，
  且 **`target` 一起改**（它是规划 prompt 的「用户指定标的」与补后缀依据；
  只改 `focus_stock_code` 等于没改）；改判说明进 `state["progress"]`
  （`operator.add` ⇒ 时间线首条）+ `POST /analyze` 的 `subject_note` + warning 日志。
  **问句里没有股票名时一律不改**（"点一只票再问通用问题"是合法用法）。
  判据：`tests/unit/test_research_subject_resolution.py`（7 条，含两条"不许改"的反向用例）。
- **★ 必然失败的指标不许留在喂给 LLM 的菜单里**（实测：`股息率` 的合成口径源
  `stock_history_dividend_detail` 间歇性 `RemoteDisconnected`，两次端到端 +
  用户两次报障**每次都失败**）—— 菜单里放着它 = **自己制造缺口**
  （"没量到"会被读成"没有股息"）。同源且稳定的 `股息率TTM`（本地行情仓）保留。
- **★ 两套分类体系的行业名要逐条复核，不要模糊匹配**：名录（Tushare 110 个）
  与平台板块池（东财 457/2510 个）名字不同（`电气设备` ↔ `电力设备`）。
  实测**否决**了字符相似度兜底：≥0.5 的 63 对里 `电气设备→火电设备`、
  `铁路→钢铁`、`保险→环保`、`白酒→啤酒` 全是错的 ⇒
  **错配的板块比"没数据"更危险**（数字看着有据）。只认
  `_BOARD_NAME_ALIASES`（逐条复核 + **双向过期检查**：
  源名必须是名录行业、目标名必须在当前板块池里），档位随数据下发
  （`match_tier="alias"`）。
- **★★★ 采集侧必须有"缺口日志"，而且要先确认 INFO **真的落盘****（用户 2026-09-29：
  「数据采集 agent 要记录**任何未能获取到的信息日志**，展示在**后端日志**里」）。
  本仓库**全仓库没有** `logging.basicConfig()`/`dictConfig()` ⇒ root 无 handler
  ⇒ Python 的 last-resort **只兜 WARNING 及以上**，**INFO 一律被丢弃**
  （本项目为此付过代价：`SchedulerService.start()` 的 INFO 横幅在 `data/run/*.log`
  里逐字节 **0 处**，而同文件 WARNING 级在）。所以：
  `src/core/logging_setup.py` 给 **`src` 命名空间**挂 handler（`api/main.py` 导入期调用，
  级别取 `MOSS_LOG_LEVEL`，默认 INFO），A01 记三类行 ——
  `[采集成功]` INFO · `[采集缺口]` **空=WARNING / 异常=ERROR**（`kind=empty|error`）·
  `[采集汇总]` INFO（计划/成功/空/失败 + **缺口清单**）。
  **收尾自问：这行日志在 `data/run/backend.log` 里 grep 得到吗？**（答不出就是没接）
- **★★★ 定期数据维护：判据要判"**停了**"，不是"不新鲜"**（用户 2026-09-29：
  「**定期维护数据**」）。作业 `data_freshness_audit`（每日 07:45）+
  `src/scheduler/maintenance.py`：名单←`IndicatorRegistry`、新鲜度←
  `DataFreshnessEvaluator`、运行时态←`CatalogRepository`、补救←**入既有缺口队列**
  （补取交给既有 `gap_drain`，本作业**不自己取数**）。
  ⚠️ **第一版判据是错的，实测自证**：直接用 `lagging/stale` ⇒ **862 条里报 835 条**
  （连"昨天刚更新的 `PB:600036`"都算）—— 那是**给上下文加权**的连续衰减口径。
  正确口径：`days_since > 3 × 期望周期`（周期**从登记的 `frequency` 读**：
  日 3 天 / 月 90 天 / 季 270 天）+ `expired`（硬截止）直判；
  并且**先对齐索引与事实**再判（实测 `stock_close:600036` 索引比事实早 5 天，
  那是**另一类根因**）。**首次运行就查出四件没人知道的事**：
  `us_unemployment`/`us_nonfarm`/`us_pce`/`us_core_cpi`/`us_fed_rate` **停在 2025-07/08**、
  `社融` 停在 2026-04、`stock_close` 家族 **487 只**停在 2026-09-14、
  **11 条季频数据被登记成日频**。判据写在
  `tests/unit/test_collection_logging_and_maintenance.py`（21 条，含"已知答案自证"）。
- **★ 路径不许自己数 `parents[N]`**（实测代价）：`GapQueue` 用
  `Path(__file__).resolve().parents[3]` 当根 ⇒ 从 `src/domain/agents/decision/` 往上
  三层是 **`src/`** ⇒ 队列写在 **`src/data/gap_queue.jsonl`**（含 **44 条真实缺口**），
  而 `configs/data_stores.yaml` 登记的 `data/gap_queue.jsonl` **成了幻影**。
  **一律用 `data_stores.PROJECT_ROOT`**（canonical 常量）——
  数错一层**不报错**，只是写歪；护栏
  `test_gap_queue_is_not_inside_the_source_tree`。
- **★★★ 「没实现的概念」走派生流水线，不要每次人工写死**（用户 2026-09-29：
  「银行息差 未实现，这种未能实现的概念，可以先**联网搜索概念的意义及公式**，
  根据公式里**设计的数据名称**，再去找**本地数据库和联网数据**，最后**计算**」）。
  四步各自的单一真值源：① 概念→公式 = `configs/derived_indicators.yaml`
  （**`sources` 出处是硬要求**，缺失即加载报错 —— 公式错会产出"看着有据的错误数字"）；
  ② 公式→数据名 = 同文件 `inputs` 的**候选名序列**（由具体到近似，
  `formula_symbols()` 从公式现读并用测试对齐）；③ 数据名→取数 = 既有确定性流水线
  （本模块**不自己连库、不自己联网**，取数器由调用方注入）；
  ④ 计算 = `src/domain/indicators/derive.py::safe_eval`（**AST 白名单**，
  只认数字/括号与 `+ - * / // % **`）。
  **三条不许越过的线**：公式必须带出处 · 取不到就是**缺口**（连"只差一个输入"
  也是缺口，绝不用 0 兜，并把候选名一起报出来） · **不执行任意代码**
  （用户说的"编码 agent"落在**第①步写注册表**：可评审、可 diff、可回归，
  而不是运行时执行模型生成的代码）。
  退化要写进 `bias`（实测：用「总资产」代替「生息资产」当分母 ⇒ 净息差**系统性偏低**）。
  首条条目 `净息差:{code}` 真机实测：四个候选名**全部无连接器支持** ⇒ 报缺口 +
  **入既有补采队列**。**这就是"未实现"的正确形态：缺什么、缺在哪一层、谁去补，全都机器可读。**
  判据：`tests/unit/test_derived_indicators.py`（17 条，四组对应四步）。

```bash
# 契约四面 + 白名单三向 + 族可见性（改指标/白名单/登记表必跑）
uv run python -m pytest tests/unit/test_contract_consistency.py \
    tests/unit/test_whitelist_coverage.py tests/unit/test_indicator_prefix_wiring.py \
    tests/unit/test_rule_family_visibility.py -q
# 流水线四件套 + 合规溯源 + 横截面
uv run python -m pytest tests/unit/test_column_index.py tests/unit/test_local_data_executor.py \
    tests/unit/test_synonym_dict.py tests/unit/test_network_fallback.py \
    tests/unit/test_compliance_input_provenance.py tests/unit/test_cross_section_context.py -q
# 真值探针（需网络；判据是"取到没取到"，不是"看着像不像"）
uv run python scripts/_e2e_query_acceptance.py           # 六子问题 23 口径
uv run python scripts/_probe_a12_whitelist_acceptance.py # A12 白名单那一跳
uv run python scripts/_probe_cross_section_real.py       # 真实横截面的维度标签
```

## 共享数据写权限硬约束（改「调度作业 / 行情仓 / 多实例」前必读）

> 触发它的是用户裁定（2026-09-29 原话）：
> 「共享行情仓，dev 读，pilot 写和读。同时看下更新数据是谁负责的，
> **谁负责更新数据谁有写权限**。」
> 口径全文：`docs/PRD.md` **§18.6**（`CHG-0087`）。

- **写权限跟着「更新责任」走，且只有一处声明**：`configs/data_stores.yaml`
  的 `warehouse.writer`（现行 = `pilot`）。**不要**在代码里再写一份"谁能写"。
- **★ 裁剪清单必须派生，不许手写黑名单。** 更新作业在 `JobSpec.updates` 里
  声明"我会写哪些存储"，裁剪读 `updates × writable_here()`
  （`scheduler.registry.schedulable_jobs()`，`start()` 与 `_tick()` **同源**）。
  手写黑名单**不会自己长大**：本项目实测 —— 三个实例（main/dev/pilot）的调度器
  **都在**跑 `quant_data_sync`，dev 与 pilot 在**同一分钟**往同一个 14.36 GiB
  的 SQLite 里 upsert；而"本该关掉它"只写在两处注释里，靠一个
  **全仓库零处读取**的 `MOSS_SCHEDULER_ENABLED=0` 表达，启动横幅还印着
  「定时任务：已关闭」。**提醒与事实相反时，没有任何人会去查。**
- **写闸门要 fail-closed 两层**：① 显式 `assert_writable()` 抛出**含"谁是写者 +
  照抄就能用的命令"**的错误；② 连接级 `PRAGMA query_only=1`（SQLite 自己的开关）
  —— 后者覆盖**我还没枚举到的写路径**（本仓库有 5 处开写事务的地方，
  以后还会有第 6 处）。只做①的失败模式是"新写路径忘了加判断"，而它**不报错**。
- **`decided=False`（口径未裁定）只报告、不阻断，也不拿来关作业。**
  让没人拍过板的口径静默停掉生产任务，是"设了但不生效的开关"的**镜像**错误。
- **改口径时先看旧判据会不会红**：`test_store_registry.py` 里写死
  `writer == "main"` 的断言在本次改归属时**立刻红了**（这是它该有的行为）；
  护栏要**从 registry 推导**（"可写性 == 本实例是不是那个写者"），
  不要写死具体值，否则每次改口径都在测上一版。
- **手工补数 / 离线脚本要声明身份**（否则被闸门拒）：
  ```bash
  MOSS_ENV=pilot uv run python scripts/quant_warehouse.py ingest --dataset daily
  ```
  错误消息里就带着这条命令 —— **拒绝要给出路**，不能只说"你没权限"。
- **临时开关只留一个**：`MOSS_SCHEDULER_DENY`（逗号分隔作业名）。
  它必须把**拼错的名字**单独 warning（`unknown_denied_names()`），
  否则"写错一个字母"与"本来就不需要禁"在日志里长得一模一样。
  长期差异**必须**落进 `data_stores.yaml` 的 `writer` 或 `JobSpec.updates`。
- 护栏：`tests/unit/test_warehouse_write_ownership.py`（14 条，含"写者写得进去"
  与"只读实例读得到"两个**反向**判据、`_tick()` 真跑一遍看执行器有没有被调用的
  **行为**判据）。

## Skills引用
@.trae/skills/backup-path-availability/SKILL.md
@.trae/skills/data-gap-closed-loop/SKILL.md
@.trae/skills/deterministic-over-prompt/SKILL.md
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
