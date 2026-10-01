# 本会话问题清单 · 根因 · 解决方案

> 会话范围：**情报中心 / 平台热议 / 投资日历 / 事件告警桥 / 登录图形码 / 权限矩阵 / 侧边栏效果图**
> 日期：2026-09-27　｜　姊妹文档：`SESSION_ROOTCAUSE_AND_FIXES_20260927.md`（另一会话，主题不同，勿混读）
>
> 本文只收**本会话真实发生并有证据**的问题。每条格式统一为：
> **症状 → 根因 → 修法 → 证据**。凡"我推断的"都会写明是推断。

---

## 0. 一句话结论

本会话 29 个问题里，**真正烧时间的不是"写错了代码"，而是三类"看起来对、其实没生效"**：

| 类别 | 条数 | 代表 |
|---|---|---|
| **A. 护栏/规则写了，但从未触发** | 5 | 中性新闻从未隐藏、登录图形码永不渲染、调度作业从未运行 |
| **B. 改了共享物，没审它的其它消费者** | 4 | 收容组饿死分析任务、删 FEATURES 会让管理员也 403 |
| **C. 确定性任务交给了概率模型** | 3 | 用 1.5B 从快讯里抽股票 → 抽出 0 只 |

这三类的共同点是：**代码能跑、测试能过、日志没报错，但用户看到的东西是错的或缺的**。
排查它们花的力气远超写代码本身。

---

## 1. 数据正确性

### 1.1 宏观日历的"前值"是别的国家的数字
- **症状**：CPI 前值显示 `112.32`、PMI 前值 `49.0`、GDP 前值 `361511.0`
- **根因**：`_norm_name` 归一后的 dict 把不同口径的记录**塌缩成一条**（中国 CPI 指数 vs 台湾 CPI 指数同名），
  且匹配用了**双向包含**（"PMI" 命中"非制造业PMI"）；GDP 命中的是"总量"而非增速
- **修法**：改成**记录列表 + include/exclude 契约**（每个指标声明"必须含/必须不含"的词），
  加 `_DATE_TOLERANCE_DAYS=2` 容忍发布日与数据日不同，并给取值打 `src_date` 标签
- **证据**：修后实测 17.6 / 49.8 / 0.8 / 3.8 / 4.7（`src/domain/intel/calendar.py`）

### 1.2 增量采集静默丢数据（156 个主题只取到 1 个）
- **症状**：知识星球增量采集"成功"，但只落了 1 条
- **根因**：把**水位线当成了两个边界**（既当"取到这里为止"又当"从这里开始"），
  于是每轮只取到边界那一条
- **修法**：语义改成"**已处理的最老边界**"，每轮 `end_time=watermark`；
  把边界那条**预置进去重集合**（`end_time` 是闭区间）；`len(seen)==before` 即停；
  上游偶发返回空（实测约 1/6 概率）→ `_EMPTY_RETRY=2`
- **证据**：1 → **204** 条（`src/infrastructure/connectors/zsxq_incremental.py`）

### 1.3 情报流被 2017 年的研报刷屏
- **症状**：一页 268 条里 76 条研报，时间跨度 **2017-01-02 → 2026-09-23**
- **根因**：东财研报接口**按个股返回该股全部历史报告**，采集侧没有时效约束；
  于是"今天的情报流"里混着九年前的点评，把当天内容挤出 `limit`
- **修法**：`FEED_WINDOW_DAYS = 7` 硬窗口（用户口径"超过7天的都过滤不要"），
  且**某一类全部落在窗口外时记一条数据缺口**（"研究笔记最近 7 天无更新（已归档 18 条过期内容）"）
  —— 静默消失正是用户此前问"研究笔记怎么看不到了"的原因
- **证据**：broker_report 76 → 1；research_note 全部过期并如实报缺口

### 1.4 限售解禁明细点开是空的
- **症状**："限售股解禁 显示 15 家，点开却没有股票名称与解禁市值"
- **根因**：后端用的是 `stock_restricted_release_summary_em`（**按日汇总**），
  个股名与占比在那个接口里**根本不存在** —— 前端没有可渲染的数据
- **修法**：换 `stock_restricted_release_detail_em`（按个股），
  `scope.stocks[]` 带 `code/name/market_cap/pct_of_float/share_type`，按市值倒序
- **证据**：2026-09-28 显示 40 家 / 591.08 亿；前端表头 编码·股票·解禁市值·占流通股比例·解禁类型

### 1.5 热门个股聚合抽出 0 只（本地模型返回语法合法的垃圾）
- **症状**：`{"batches":2, "stocks":1, "rejected":{"bad_json":1}}`，且唯一那只还是港股
- **根因**：`json_mode=True` 只保证"**是 JSON**"，不保证"**是你要的 JSON**"。
  实测 `qwen2.5:1.5b` 返回 54 字符 `{"1. 【CC电新】液冷金帝...": -1.1e6}` ——
  `json.loads` 抛错 → 整批 0 条 → **页面空白，看起来像"上游没数据"**
- **修法**：给网关加 **JSON-Schema 受约束解码**，一路打通
  `providers.OllamaProvider.chat(json_schema=...) → payload["format"]=<schema> → gateway.complete(json_schema=...) → hot_job`
  ；schema 的 `sentiment` 用 `enum` 由采样器保证取值；schema 指纹进缓存 scope
- **证据**：修后输出合法 JSON（`src/infrastructure/llm/providers.py`、
  `src/domain/intel/hot_topics.py::OUTPUT_SCHEMA`）

### 1.6 模型抽股票这件事本身就不该交给模型
- **症状**：即使有了 schema，一批 40 条快讯仍只抽出 1 只票，且是港股
- **根因**：快讯里绝大多数是宏观/海外内容（IMF、诺基亚、巴基斯坦股市），
  A 股名字**很稀疏** —— 这种输入上 1.5B 模型基本是抛硬币。
  而"哪些票被提到了"**根本不需要模型判断**：本地名录里有 5567 只 A 股的准确名字
- **修法**：新增 `hot_scan.py` 做**确定性名录匹配**（零模型、毫秒级、可解释）；
  规则**宁可漏不可错**：
  - 名称 ≥4 字直接命中（4734/5567 是 4 字）
  - **3 字名必须有代码佐证**，白名单除外 —— 因为 3 字名里混着大量日常词
  - **券商名必须同条出现代码才认**（`中信证券` 确实是 A 股 600030，但"中信证券指出…"是机构署名）
- **证据**：真实快讯扫出 17 只 A 股，每条带原文标题可核对；规则断言 0 失败

---

## 2. 权限与隐私

### 2.1 `source_alias` 明文泄漏数据源
- **症状**：响应里出现 `newswire-em` / `policy-cctv` / `broker-太平洋`
- **根因**：`source_pseudonym()` 早就写好了，但 **`to_public()` 从没调用它**。
  隐私测试只测了 helper 本身，**没测真实输出** —— 所以测试全绿而泄漏一直在
- **修法**：在契约层做假名（保证同一来源永远同一假名，前端才能按源去重）；
  测试改成**对真实输出断言**，不再只测 helper
- **证据**：`tests/unit/test_intel_source_privacy.py`（输出键集合白名单断言）

### 2.2 历史告警把来源名与 URL 发给所有登录用户
- **症状**：541 条事件 / 27 条告警里，`source_name` 与 `source_url` 原样下发
- **根因**：告警链路是**先于**隐私规范写的，从未走脱敏
- **修法**：新增 `alert_to_public` / `event_to_public`（白名单构造）；
  WebSocket 广播**按连接**脱敏；`raw_data` 只放行 `{"importance"}`
- **证据**：`src/api/routes/alerts.py`；`_RAW_DATA_PUBLIC_KEYS`

### 2.3 知识星球富文本把平台域名带出去
- **症状**：正文里出现 `<e type="web" href="https%3A%2F%2Fwx.zsxq.com%2F...">`
- **根因**：URL 是**百分号编码 + 协议相对**的，绕过了 `https?://` 正则
- **修法**：在契约层加 `_strip_rich_tags()`，同时处理自闭合 / 被截断 / 夹在正文中间三种形态
- **证据**：`src/infrastructure/connectors/intel_sources.py`

### 2.4 "每人已读"从来不存在
- **症状**：A 用户标记已读，B 用户的未读数也清零
- **根因**：`DEFAULT_TENANT="tenant_001"` 硬编码，pilot 里 47 条告警全在同一租户；
  读状态是**全局**的，13 个 vip 用户互相踩
- **修法**：新增 `user_alert_read(user_id, alert_id, read_at)` 表（幂等 CREATE TABLE IF NOT EXISTS），
  `mark_read/mark_all_read/count_unread/list_alerts` 全部带 `user_id`；
  `status IN ('active','read')` 兼容历史全局已读行
- **证据**：A 标记 47 条 → A 未读 0、B 仍 47

### 2.5 "公开平台可暴露"与"私域必须藏"不是矛盾
- **背景**：用户口径——公开数据平台（AkShare/腾讯/新浪/东财/QMT）可以暴露；
  平台/群身份（知识星球调研、群 id）必须藏
- **修法**：`PUBLIC_PLATFORMS` **白名单映射**（6 个公开源 → 平台名），
  表外来源（`research-note-zsxq` / `broker-*`）一律给**空串**；
  新增用例逐个断言"表外来源拿不到平台名"
- **理由**：**"我从哪个公开网站抓的"不是壁垒**（谁都能抓），
  **"我有一份付费/私域的信息源"才是壁垒**

---

## 3. "接上了，但从来没跑"

> 这一类是本会话最贵的：**代码在、测试过、日志不报错，功能就是不生效。**

### 3.1 ★ 中性新闻从未被隐藏（在写入侧实现了读取侧的需求）
- **症状**：用户两次要求"倾向中性的信息就不要显示了"，规则写了、测试也过了，**中性一条没少**
- **根因**：`tone_job` 对中性条目 `return {}`（**不落库**，设计意图是"存储里没有，统计就不可能算进去"）。
  但 `build_feed` 判断"要不要隐藏中性"**读的正是这份存储** ——
  存储里没有记录 → 该条被判成"尚未抽取" → **照常显示**。
  即：**它是一个读取侧的需求，我却从写入侧去实现**
- **修法**：中性**落库并打 `neutral=True`**；统计防线改由**结构**保证
  （`tone_dist` 只累加 `has_tone=True`，而中性的 `has_tone` 恒为 False）
- **证据**：修后 176 条抽取 → 中性 112 条正确隐藏；
  `{中性:112, 未定:59, 偏多:5}` 的分布才第一次与现实一致
- **推广**：**任何"隐藏/过滤"的需求，先问"读取侧凭什么认出它"** ——
  认不出就等于没做。写入侧删除是最不可靠的实现方式。

### 3.2 ★ 登录图形码永不渲染 —— 把用户硬锁在门外
- **症状**：页面红字"为确认是你本人操作，请先完成图形验证码"，
  而**页面上根本没有图形码可填**；刷新、换浏览器都没用（服务端按 IP 记状态）
- **根因**：判据写的是
  ```ts
  if (which === "login" && /captcha_required/.test(String(e))) { setLoginNeedsCaptcha(true); }
  ```
  而后端 400 的 `detail.code` 虽是 `captcha_required`，
  **`ApiError.message` 已经被换成给用户看的中文**，`String(e)` 得到
  `"ApiError: 为确认是你本人操作…"` —— **错误码一个字都不在里面**。
  另一条 `/captcha/i` 也匹配不到："图形验证码"里没有 `captcha` 这个词
- **修法**：直接读 `ApiError.code`（`captcha_required` / `captcha_failed`），
  文案匹配降为兜底且把 code 拼进被搜索的字符串
- **证据**：Playwright 真实路径复现 —— 第 2 次"账号或密码错误"，
  第 5 次出现图形码输入框，第 6 次图片加载；空值时提交按钮禁用、三项填齐后可用
- **推广**：**判据永远认"机器可读的标识"（错误码/枚举/状态位），
  不认"给人看的文案"。** 文案会被本地化、会被改写，判据会静默失效。

### 3.3 两个情报调度作业从未运行
- **症状**：`intel_zsxq_collect` / `intel_token_alert` 每次触发都在运行记录里记
  `未知作业类型 ... failed`；表现为"数据不更新"
- **根因**：作业在 `JOB_REGISTRY` 里**声明了**，但 `execute_job` 的分发链里
  **没有对应分支** → 每次都是"未知作业类型"
- **修法**：补 handler + 新增**覆盖性测试**：遍历 registry 的 `spec.kind`，
  断言每个 kind 都有 handler（注意要遍历 `spec.kind` 而不是 registry 的 key，
  因为 `snapshot_macro → graph_snapshot` 这种映射会让 key 与 kind 不等）
- **证据**：`tests/unit/test_scheduler.py::test_every_registry_kind_has_a_handler`

### 3.4 `hot_job.run_once()` 从来没有被任何调度任务调用
- **症状**：页面上"热门个股 / 热议事件"永远是空的或极旧
- **根因**：函数写好了，但**只有调试脚本 `_dbg_hot.py` 跑过它**；
  空的原因**看起来像"上游没数据"**，完全不像"这条链路没接上"（我因此白查了一轮数据源）
- **修法**：新增 `intel_hot_topics` 作业（cron `27 */2 * * *`，排在倾向抽取之后）
- **推广**：**"我写了一个函数"与"这个函数会被调用"是两件事**。
  收尾时对每个新函数问一句"谁在什么时机调它"，并把它写进调度/覆盖率测试。

### 3.5 收容组饿死了三个分析任务
- **症状**：收容组上线后，倾向抽取出 `considered: 2, extracted: 0`
- **根因**：`build_feed` 同时是 `/feed` 接口**与**三个内部消费者
  （倾向抽取 / 平台热议聚合 / 热议扫描）的取数入口。
  我把"未定条目收成一行"加在了它身上 —— 于是内部消费者看到的"全部条目"变成
  **2 条**（1 条信号 + 1 个组），抽取当轮只处理 2 条就收工，
  而日志显示"抽取 0 条"，**看起来像"没有可抽的"**
- **修法**：加 `group_undetermined: bool = True` 参数，三个内部消费者全部显式传 `False`；
  因为这是异步任务里的一行参数，行为测试要造一整套 runtime，
  所以用**源码级断言**锁住（"这三处必须出现 `group_undetermined=False`"）
- **推广**：**收容/折叠/截断属于展示层**。改一个共享函数的**输出形状**前，
  先 `grep` 它的**全部调用方**，逐个判断"它要的是展示视图还是事实全集"。

---

## 4. UI 与渲染

### 4.1 ★ 可信度的椭圆（以及"颜色长在边框上"）
- **症状**：可信度分数的外圈是**椭圆**（约 90×38）
- **根因**：`border-radius: 50%` + `width/height: 38px` 的圆环元素作为 flex 子项**被横向拉伸**；
  另外**分档色写在 `border-color` 上** —— 意味着"去掉边框"会**连带丢掉五档配色**
- **修法**：去掉环，改"纯数字 + 分档颜色"；分档色从 `border-color` 落到 **`color`**；
  `border-radius` **显式归零**（全局 `button{border-radius:8px}` 仍在，
  现在 `border:0`+透明底看不见，但只要将来有人给个背景色就会变成一颗胶囊 —— 同一个问题换个形状回来）
- **证据**：用**线上 CSS 产物**渲染同构 DOM 量几何：
  `borderRadius=0px / borderTop=0px / borderLeft=0px / leftGap=0 / centerDelta=0`

### 4.2 解禁明细点开没有表头
- **症状**：明细区 5 列数字没有表头，"1.2 亿"到底是解禁市值还是流通市值、百分数是占谁的比例，用户只能猜
- **根因**：只渲染了 `<li>` 行，没有表头
- **修法**：加表头（编码·股票·解禁市值·占流通股比例·解禁类型），
  且**表头与数据行共用同一套 grid 列宽**（分开写的话，窄屏媒体查询改了行、忘了改表头就会错位）；
  表头 `position: sticky` 钉在滚动区顶部（明细区自己滚动，表头不跟就会"滚到第 20 行又不知道哪列是什么"）；
  顺带把「占流通股比例」列 60px → 96px（6 个字原来折成两行）

### 4.3 投资日历一条占三行 + 日期说两遍
- **症状**：`宏观发布 规模以上工业企业利润` / `仅前值 上期17.6` / `暂无一致预期值…` 三行；
  且 `2026-09-27 后天` 表头下面紧跟一行 `09-27 后天`
- **根因**：① 指标块是**块级**子元素、`dt/dd` 是**竖排**、提示语有 `width:100%`（强制换行）；
  ② 日期由分组表头给了一次，行内**又给一次**
- **修法**：`.cal-line` 改 `nowrap` + 唯一弹性项是标题（超长省略 + `title` 悬停看全文）；
  `dt/dd` 并排；提示语收成短标签；分组内**不再渲染日期列**（省下的 74px 全给内容）；
  窄屏仍允许换行（"尽可能不换行"≠"强制不换行"）
- **证据**：Playwright 审计 52 行 —— 桌面 **0 行堆叠**（`body` 高度 == `line` 高度逐行确认）

### 4.4 7 列表格塞不进 390px
- **症状**：手机上标题被压成每行 5 个字
- **根因**：固定 px 列宽（66+82+70+110+70=398px）已占满屏宽，标题只剩 142px
- **修法**：竖屏**砍次要列**（`.hide-m`：来源类型/关联标的）+ 横向滚动 +
  **显式文字提示**"← 表格可左右滑动查看全部列"
  —— 手机滚动条不可见，不写提示用户不知道右边还有内容
  （**这正是本项目顶栏页签当时的问题**：9 个页签横向滚动 + 滚动条被隐藏）

### 4.5 CSS 前置块被后面的规则覆盖（死 CSS）
- **症状**：我加了一整套 `.cal-line` 新规则，**没有生效**
- **根因**：我把新规则**前置**了，而原始定义在后面 —— 同优先级下后者胜
- **修法**：改**原始定义**，并删掉那段前置的死 CSS
- **推广**：CSS 改动**必须确认自己那条是最终生效的**（看层叠顺序，
  或直接量 computed style）。"我写了" ≠ "它生效"。

### 4.6 一级导航 9~14 个扁平页签
- **症状**：`.tabs` 在 ≤720px 是 `flex-wrap:nowrap; overflow-x:auto` +
  `::-webkit-scrollbar{display:none}` —— 手机上要**横向划**才能找到页签，
  而滚动条被藏了，用户**根本看不出右边还有东西**（"事件告警"排最后，基本没人发现）
- **修法（未落地，仅出效果图）**：4 组分组侧边栏 + 手机三形态
  （竖屏抽屉 / 横屏 54px 图标轨道 / 平板图标轨道）。
  横屏那一档是**净收益**：横屏稀缺的是高度，导航挪到左边缘**把高度还回来**
- **产物**：`docs/_mockup_appshell.html` + 20 张渲染图（`scripts/_render_appshell_mockup.py`）

---

## 5. 权限矩阵

### 5.1 删一个 feature key 会让**管理员也** 403
- **风险点**：`intel.py` 里 **7 个端点**用的是常量 `FEATURE_RADAR = "intel.radar"`。
  只改 `FEATURES`（可售卖项）不改这里 → `feature_enabled_for_tier` 恒 False →
  **情报流/日历/热榜/来源健康对所有人 403，包括管理员**，
  而日志里只有一句 `feature_disabled`
- **修法**：`FEATURE_HOT = "intel.hot"` + 7 处替换；
  另写**三处一致性校验脚本**（`FEATURES` / `VIEW_FEATURE` / `FEATURE_*`）
- **推广**：**同一个 key 写在 N 处，就有一处会被漏改。** 收尾必跑一致性脚本。

### 5.2 管理员自己丢了两个页签
- **风险点**：`visible_views` 原判据是 `enabled.get(feature)`，而 `enabled` 是
  **按 `FEATURES` 构造**的。把 `scheduler`/`metrics` 移出 `FEATURES`（= 权限矩阵不再显示、不可勾选）
  之后那个判据**恒为 False → 连管理员都看不到「运行指标」「调度管理」**
- **修法**：`ADMIN_ONLY_VIEWS` 直接给管理员、**不经矩阵**；
  并补一条**反向断言**测试：`test_admin_still_sees_both_admin_only_tabs`
- **推广**：**"收紧权限"的改动必须同时有一条"该看到的人还看得到"的反向测试** ——
  否则故障方向是"管理员自己少了功能"，而没人会去测管理员

### 5.3 管理员专属页签从"勾了也不给"升级为"连勾都勾不上"
- **用户口径**："不用加在功能权限设置的选项里"、"其他用户都没这个权限且不可选择"
- **做法**：把两项移出 `FEATURES` → 矩阵**渲染不出这两行**；
  直接打接口想勾也会被参数校验拒掉（`PUT .../tiers/trial {"features":{"scheduler":true}}` → 400 未知功能）
- **测试语义随之变强**：原 `test_non_admin_can_never_see_the_tab_even_if_the_matrix_grants_it`
  → 改名 `test_admin_only_views_cannot_even_be_granted`，
  断言"配不出来 / 配了也不给 / 给了也调不动"三层

---

## 6. 工程与协作

### 6.1 探针脚本差点把生产管理员账号锁掉
- **经过**：我用 `admin` / 旧密码反复登录 pilot 做验证，失败 4 次
  （阈值 5）。查库（只读）才发现：`password_updated_at` = 当天 17:45，
  **密码已被改过**，而 `failed_attempts=4` —— **再错一次就锁号**
- **修法**：① 立刻停止用 admin 试密码；② 探针改用测试账号；
  ③ 验证方式改为**不登录**：取**线上 CSS/JS 产物**断言改动已发布 +
  用同一份 CSS 在本地渲染同构 DOM **量几何**
- **推广**：**验证脚本不该有"把生产管理员锁掉"的能力。**
  用生产凭据做探测前先想清楚"最坏会留下什么后果"。

### 6.2 偶发失败 vs 真失败，必须用证据分类
- **现象**：全量单测里 `test_event_analyzer` 5 条失败，而**隔离运行 19/19 通过**、
  与相邻 9 个文件一起跑也通过
- **做法**：重跑全集 → 那 5 条**不再出现**，最终 `8 failed / 4710 passed`，
  8 条全是既有的告警阈值配置漂移（`alert_risk_high=70` vs 断言 75）
- **纪律**：不用"pre-existing"糊过去，也不把偶发当真实缺陷 —— **分类要有证据**
  （隔离运行 + 子集运行 + 重跑），并在汇报里写明是哪一类

### 6.3 用户点名的文件不存在 → 先核对存在性，再按**内容**找
- **经过**：用户说"你设计的效果图 `docs/_mockup_zsxq_intel.html` 还没实现"。
  全盘（`D:\code` 3576 个文件）搜 `*mockup*zsxq*` / `*zsxq*intel*` **零命中**；
  按**内容**搜（"知识星球情报台"等 6 个关键词）才定位到真实效果图
  `docs/_mockup_intel_01_intel_radar.png`，而它就是用户截图那张的上半部分
- **推广**：用户给的文件名/路径**可能记错**。先断言存在性，
  不存在就**按内容搜**（关键词/标题/特征串），不要凭"我记得有这么个文件"下结论，也不要直接说"没有"

### 6.4 用户说"左右布局好看"，真需求是"分组"
- **经过**：用户说效果图的左右布局比现系统好看，并问"手机端是不是难适配"
- **分析**：好看的真正来源是**分组**（4 组 13 项）而不是"在左边"；
  现系统是 9~14 个**扁平**页签。手机端不是"难适配"而是"需要三种形态"，
  且横屏那一档**比现状更好**（省回约 100px 高度）
- **推广**：用户给的是**方案**（放左边），要还原成**诉求**（信息分组），
  否则会照着方案做一个更差的版本。同时**先量现状**——原布局在手机上**已经是坏的**

### 6.5 环境摩擦（本轮重复踩）
- **PowerShell 无 heredoc**：`<<'PY'` 直接语法错误 → 复杂脚本一律
  **先用 write 工具写成 `.py` 文件再执行**（本轮 >15 次）
- **PowerShell 重定向会打乱 UTF-8 中文**：`>` 出来的文件读回来是乱码/UTF-16
  → 让 **Python 自己 `open(...,encoding='utf-8')` 写文件**，不要让 shell 重定向
- **`Select-String` 的正则不能用 `\"` 转义**：内联 Python 里带引号的正则会被 PS 解析器撕碎
  → 同上，写文件

---

## 7. 遗留与未决（诚实清单）

| # | 事项 | 状态 |
|---|---|---|
| 1 | 侧边栏改版**只有效果图**，未实现 | 等确认"横屏/平板用图标轨道还是抽屉" |
| 2 | 「热点&研报小作文」「投资日历」两个一级页签**共用一个功能 key** `intel.hot` | 如需分开售卖，说一声即可拆 |
| 3 | 事件→告警桥（`alert_bridge` + `intel_signal_alert` 作业）**代码与测试完成、实测跑通**（1 条候选 → 引擎判 `opportunity/high`），但**未部署到 pilot**（后端要 `--replace` 重启） | 用户未确认重启 |
| 4 | 8 条既有红灯（告警阈值配置漂移：配置 `alert_risk_high=70`/`alert_confidence_min=0.6`，测试断言 75/0.7） | 未被授权修改，**未动** |
| 5 | 未实现效果图里的：多源共振三态（含"背离"）、KPI 卡一排 7 个、自研因子与参数面板、平台信号交叉验证 | 已给出建议优先级 |
| 6 | 雪球（3 个接口全挂）与韭研公社（无免费接口）**拿不到**，已如实登记不接 | 平台覆盖 4/6 |
| 7 | 顶部导航未分组（9~14 扁平页签） | 效果图已出，待实现 |

---

## 8. 本会话变更清单（可核对）

**后端**
```
src/infrastructure/llm/providers.py       + json_schema（Ollama format=<schema>）
src/infrastructure/llm/gateway.py         + json_schema 参数 + schema 进缓存 scope
src/domain/intel/hot_topics.py            + OUTPUT_SCHEMA / 代码→中文名 / 券商名拦截
src/domain/intel/hot_scan.py              新增：名录确定性匹配（零模型）
src/domain/intel/stock_names.py           新增：代码↔中文名（本地名录，零网络）
src/domain/intel/hot_job.py               输入改为平台快讯 + 时效窗口 + 平铺池
src/domain/intel/hot_rank.py              主源改千股千评关注指数（含中文名）
src/domain/intel/service.py               + FEED_WINDOW_DAYS=7 / 未定收容组 / 中性隐藏修复
src/domain/intel/tone_job.py              中性落库并打 neutral=True
src/domain/intel/alert_bridge.py          新增：情报→告警（四道闸门）
src/domain/alerts/service.py              + ingest_assessed / 弹窗时段闸门（可注入）
src/infrastructure/connectors/intel_sources.py  + 财联社/富途两个源 + PUBLIC_PLATFORMS
src/api/routes/intel.py                   FEATURE_HOT=intel.hot；/brief 下架；+ heat 区块
src/domain/platform/config.py             FEATURES 收敛为 9 项可售卖
src/api/routes/my_features.py             ADMIN_ONLY_VIEWS + metrics；管理员专属不经矩阵
configs/platform_tiers.json               intel.radar→intel.hot；删 intel.brief
src/scheduler/{registry,jobs}.py          + intel_hot_topics / intel_signal_alert
```

**前端**
```
web/src/App.tsx                           删「情报中心」一级页签；+两个一级页签
web/src/components/intel/IntelPanel.tsx   删子页签，改 section prop
web/src/components/intel/IntelHeatBlock.tsx 新增：平台热议区块
web/src/intel.css                         可信度去环+靠左居中；日历一行；解禁表头；热议区块样式
web/src/components/intel/IntelCalendarTab.tsx 表头/一行布局/隐日期
web/src/components/LoginScreen.tsx        图形码判据改读 ApiError.code
web/src/components/AdminPermissionPanel.tsx ADMIN_ONLY_FEATURES + metrics
web/src/styles.css                        .app padding 5% → 3%
```

**效果图 / 文档 / 脚本**
```
docs/_mockup_appshell.html                侧边栏改版效果图（可渲染）
scripts/_render_appshell_mockup.py        渲染 20 张 PNG
scripts/_verify_*.py                      6 个验证脚本（一致性/几何/标签/宽度/桥/闸门）
tests/unit/test_intel_feed_window_group.py   9 条
tests/unit/test_intel_alert_bridge.py        25 条
```
