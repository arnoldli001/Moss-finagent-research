---
name: frontend-change-guardrails
description: "改本仓库前端（web/src 的样式与组件）及配套接口时的护栏与验收纪律。覆盖：顶栏冻结留下的 16px 渐隐带、flex 挤压把一级页签挤折行、brand/disclaimer 连排的取舍、视图键三处枚举漂移、删除重复提示的正确姿势、以及「单请求耗时表会骗人」的并发验收。当修改 web/src/styles.css、App.tsx、任何面板组件，或排查「页面被遮挡 / 首屏长时间不出来 / 后端服务当前不可达」时使用。"
license: MIT
compatibility: "React 18 + Vite, FastAPI(uvicorn 单进程), Playwright 验收脚本, Python 3.10+"
metadata:
  version: "1.0.0"
  project: "Moss-FinAgent-Research"
  derived_from: "会话 2026-09-26~27：brand-sub 连排、页签一行、顶栏渐隐带、未知视图红框、ETF 门控去重、首屏事件循环阻塞"
---

# 前端改动护栏（本仓库专用）

> 这份不是通用前端规范，而是**这个仓库踩过的坑** + **改完必须怎么验**。
> 每条都对应一次真实报障或一次真实回归，括号里是证据所在。

## 〇、先记住三条本仓库的特殊性

1. **顶栏是 `sticky` 且 `margin-bottom: 0`** —— 它下面那 16px 视觉留白改由
   `.header::before` 的**渐隐带**承担（见 styles.css 的"顶栏冻结"一节）。
   所以**内容一侧必须自己让开这 16px**，否则第一行会被"洗"掉一半。
2. **`.app` 带 `zoom`**（≥1780px 起 1.06 / 1.08 / 1.10 / 1.15）
   —— 布局宽度 ≠ 视口宽度。算断点要按**布局 px**算，测量必须用真浏览器 + 真实产物 CSS。
3. **视图渲染是一条三元链 + 链外保活面板** —— 视图键在**三处**枚举
   （`view` 联合类型 / `HASH_VIEWS` / 三元链），任何一处漂移都会冒出
   「未知视图：xxx」红框。

## 一、CSS 改动的五个高频翻车点（都真实发生过）

| 动作 | 会发生什么 | 正确做法 |
|---|---|---|
| **锁死某一列宽度**（`flex: 0 0 auto`） | 缺口**整体转移**到另一侧：页签被压到折行，左边还空出一截"看着没用上的空白"（用户原话：「还有空白间距 …… 使所有一级页签在同一行」） | 要锁就得**同时锁另一侧**（`.tabs { flex-shrink: 0 }`），并算清最小宽度：品牌两行下限 318 + 页签整排 935 + 铃铛/头像/间隙 ≈170 = **1423 布局 px** → 视口 ≥1560 才敢锁（`styles.css` 里注释写着这条算式） |
| **`white-space` / `max-width` 二选一写错** | `nowrap` 会**顶出容器**（把页签挤出视口）；`max-width: 40ch` 会**必然折行**（免责声明当年就是被它钉成两行的） | 改这两个属性前先明确回答：**最多几行？放不下时谁让位？** 并把答案写进注释 |
| **只给一侧补留白**（sticky 顶栏、`::before` 伪元素、负 margin） | 另一侧立刻错位：`.header + * { margin-top: 16px }` 漏了 → 策略回测「多因子（35个因子）」行、量化交易二级页签被顶栏渐隐带压住半截（用户两次报障） | 成对改动，并在注释里写清**配对的那条规则在哪** |
| **行内元素折行后用 `getBoundingClientRect()` 判断位置** | 它返回的是**并集**（left 会变成第二行的行首），于是"排不排在副标题后面"这类判据误报（我因此在 1280/1366 档位误判过） | 数行数/判断首行位置一律用 `getClientRects()` 的**首个片段**（按 top 去重数行） |
| **忘了覆盖全局 `button { min-width: 100px }`** | 窄档位下按钮被压到 100px，里面 260px 的文字**溢出到邻居上面**（721~1000px 档位实测） | 需要时显式 `min-width: min-content`（本仓库的 `.brand` 就靠它兜底） |

> 补充：**`.brand-sub` / `.brand-disclaimer` 的连排**是用户口径（"这两个内容要写在一起"），
> 别退回 `flex-direction: column`；宽屏 1 行、≤1366px 折 2 行是刻意设计，
> 由 `@media (min-width: 1560px)` 与 `.brand` 的三档一起决定，改一处必须重跑断言 A3/A4/A7。

## 二、删「重复提示」的正确姿势（ETF 门控去重那次）

1. **先找出同一句话的所有展示位**：`grep` 关键词（如 `不放行`），列表化；
2. **确认哪一处是"独家 + 跨视图可见"的**（ETF 那次是页签上方的状态条，它在四个子视图都可见）；
3. **删之前逐项核对被删块里的每一条信息在别处还有落点** ——
   包括后端 `source_notes` / `gaps` 到底显示在哪（那次核对出：`regime.gaps` 已由后端
   `out.gaps.extend(out.regime.gaps)` 合并，脚注也再列一次；`source_notes` 在脚注"来源："里）；
4. **死链一起改**：被删块被别处用文字指着（"详见「总览」的环境横幅"），指针必须同步改，
   否则用户按提示找不到东西；
5. 留下"**别再把它加回来**"的注释 + 一条守卫/验收（`scripts/_verify_etf_flow_gate.py`）。

## 三、验收纪律（本次事故最贵的一课）

### 3.1 单请求耗时表**会骗人**

前几轮优化都只量单接口（curl 一条条打），那张表**一直好看**（全都 ≤2 秒）；
而用户遇到的是"首屏 14 个请求同时打"的形态 —— 实测**每个都要 40~52 秒**，
连 0.1 KB 的响应也等了 40 秒（共享阻塞点的典型特征）。

**规矩**：任何"首屏慢 / 面板出不来 / 后端不可达"的问题，
先跑 `scripts/_verify_first_login_load.py`（真浏览器 + 真链路 + 逐页签耗时 +
页面上有没有「后端服务当前不可达」字样），**不要**只看单接口。

### 3.2 布局改动的最小验收矩阵

| 维度 | 覆盖 |
|---|---|
| 宽度 | 断点**两侧**各一档 + 1024/1200/1366/1440/1560/1600/1780/1920/2048 |
| 页签数 | **两种形态**：租户视图 8 个 / 管理员业务视图 10 个（结论会相反：宽屏下 10 个页签要求品牌退成两行） |
| 状态 | 冷启动（清缓存后第一次）+ 热缓存 |
| 产物 | **真实构建产物 CSS**（`web/dist/assets/index-*.css`），不是源码 |

上面这套由 `scripts/_verify_header_css.py` 直接覆盖（它带 10 档宽度 × 2 种页签数）。

### 3.3 改动前后都要留数字

`data/run/header-css/`、`data/run/header-freeze/`、`data/run/first-login/` 里有截图与实测值。
没有"改前"的对照，就不能说"修好了"，只能说"看着像好了"。

### 3.4 上线动作分开记

```powershell
# 前端：web/dist → web/dist-pilot（先备份 dist-pilot），客户刷新即见
# 后端：改的是 Python，dist 不用动，但**必须重启** 8110（uvicorn 不热加载）
.venv\Scripts\python.exe manage.py stop   --env pilot --port 8110
.venv\Scripts\python.exe manage.py ensure --env pilot --port 8110
```

### 3.5 现成的护栏（改完逐个跑）

```powershell
C:\veighna_studio\python.exe scripts\_verify_header_css.py        # 顶栏几何：10 档 × 2 形态
C:\veighna_studio\python.exe scripts\_verify_header_freeze.py http://127.0.0.1:8110
C:\veighna_studio\python.exe scripts\_verify_view_routing.py http://127.0.0.1:8110
C:\veighna_studio\python.exe scripts\_verify_etf_flow_gate.py http://127.0.0.1:8110
C:\veighna_studio\python.exe scripts\_verify_first_login_load.py https://hk.wujiaitool.cn
.venv\Scripts\python.exe -m pytest tests/unit/test_frontend_prefetch_structure.py `
    tests/unit/test_api_no_loop_blocking.py -q
```

## 四、视图键三处一致（「未知视图：research」红框）

**症状**：功能正常的页面上多出一行红框「未知视图：research（请刷新页面；若持续出现请反馈）」
—— 用户会以为系统坏了。

**根因**：`view` 联合类型里有 `research`，但**三元链里没有它的分支**（内容单独挂在链外），
于是每次都落到兜底分支。同一族的第二个 bug：`HASH_VIEWS` 里留着已删除的 `mypools`
→ 旧书签 `#/mypools` 通过白名单、切到不存在的视图 → 同样红框。

**规矩**：
- 新增/删除页签时，**三处一起改**：`view` 联合类型 / `HASH_VIEWS` / 三元链；
- 面板确实由链外承载时（KeepAlive 那些），在三元链里**显式 `return null`** 并写清理由，
  不要"靠漏掉"来实现；
- 兜底分支只对**真的未知**视图生效（它还在，是"脏 hash 别白屏"的最后一道）；
- 守卫：`tests/unit/test_frontend_prefetch_structure.py`（每个视图键必须有分支 +
  白名单与联合类型一一对应）。

## 五、接口侧的同族红线（前端症状常常是后端的病）

> 2026-09-27 事故：用户报"三个面板都出不来 + 间歇性后端不可达"，
> 真凶是「策略回测」首屏拉的体检接口 `GET /api/v1/quant/data-status`。

- **`async def` 里不许有任何同步阻塞活**：扫目录、`SQL MIN/MAX` 全表扫描、
  `requests` 同步请求都算。单进程 uvicorn **只有一条事件循环**，一个请求能把**全站**按住
  （实测：它把 0 I/O 的 `/api/v1/health/live` 拖到 **12.75 秒**，而前端探针 3 秒判死
  → 假报"后端不可达"，其它页签一起不出来）。
- 慢活要么 `await asyncio.to_thread(...)`（本仓库还有关键路径专用池
  `src/core/executors.py` 的 `run_infra`），要么丢后台 job。
- **顺手体检类接口必须带 TTL 缓存 + 启动预热**：它看起来只是"展示状态"，
  实际可能在扫 1 亿行（那次的仓库是 SQLite、10 张表、日期列无索引，单次 7.18s）。
- **预热的缓存键必须与路由默认值同源**：写死字面量会**静默失效**
  （第一版写 `"data/quant"`，路由默认是 `DEFAULT_ROOT="data/quant/tushare"` → 预热等于没做，
  第一个用户照样等 11 秒，日志里毫无异常）。默认值一律引用同一个常量。
- 加接口时三问：**会不会扫全表/扫目录？并发 14 个会怎样？它挂了 `/health/live` 还答得动吗？**
- 守卫：`tests/unit/test_api_no_loop_blocking.py`（AST 断言：路由自己的语句里
  不许出现已知阻塞调用；体检实体必须是同步 `def`；必须有 TTL；预热键不许写死）。

## 六、写守卫脚本时容易自己翻车（本次三个都踩了）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| 断言对象没去注释/docstring | 三元链的注释里写着"KeepAlive 不能放进来"，被当成代码 → 误报 | 先 `_strip_comments()`；AST 断言天然不看注释，但**要看 docstring 就错了** |
| AST 遍历钻进嵌套函数 | `def work()` 里的 `store.keys()` 是**正确**写法（要丢进线程），被误判为阻塞 | 只遍历函数**自己的语句**，遇到 `FunctionDef/AsyncFunctionDef/Lambda/ClassDef` 就跳过 |
| 用 `getComputedStyle().display` 判断可见性 | 祖先 `display:none` 时子元素自己**仍然是 `block`** → 误判"可见" | 用**渲染盒**：`getClientRects().length > 0 && rect.width > 0 && offsetParent !== null` |

## 七、提交前自检清单（改前端/相关接口时逐条打勾）

- [ ] 断点两侧 + 9 档宽度都量过；**两种页签数形态**都量过（有数字，不是"看着行"）
- [ ] 顶栏相关：第一个内容行让开渐隐带 **≥15px**；每条二级页签行也在带外
- [ ] 文案/块删除：所有展示位都找过；被删信息在别处有落点；**死链已改**
- [ ] 枚举改动：视图键三处一致（`test_frontend_prefetch_structure.py`）
- [ ] 接口改动：无同步阻塞活；有 TTL 缓存；预热键与路由默认同源（`test_api_no_loop_blocking.py`）
- [ ] 首屏实测：`_verify_first_login_load.py` 全绿（含"页面上没有不可达文案"）
- [ ] 上线：`dist → dist-pilot`（有备份）；后端改动**重启 8110**
- [ ] 注释里写清**取舍与配对规则**（下一个人要能看懂"为什么不能退回 flex-column"）

---

## 附：本会话真实翻车清单（犯了哪条 → 现在谁来挡）

| # | 犯的错 | 后果 | 现在的守卫 |
|---|---|---|---|
| 1 | 用 `flex: 0 0 auto` 锁死品牌列 | 一段时期内 10 个页签里「事件告警」被挤折行，左边空出一截 | `_verify_header_css.py` 的 **A7**（≥1560px 页签必须一行） |
| 2 | 给 `sticky` 顶栏补了留白，忘了补内容侧 | 「多因子」行、量化交易二级页签被渐隐带压住 | A6（第一个内容行 ≥15px 留白）+ `_verify_view_routing.py` 的 A2/A3 |
| 3 | 启动预热写死 `root` 字面量 | 预热静默失效，第一个用户照等 11 秒 | `test_api_no_loop_blocking.py` 的 `warm` 签名断言 |
| 4 | 守卫脚本没去 docstring / 钻进嵌套函数 / 用 display 判可见 | 自己写的守卫连续误报 | 第六节的写法 + 先跑"用旧代码验证守卫会红" |
| 5 | （既有 bug，本次修掉）三元链漏 `research` 分支、`HASH_VIEWS` 留 `mypools` | 正常页面挂红框「未知视图」 | `test_frontend_prefetch_structure.py` 三条视图键守卫 |
| 6 | （既有 bug，本次修掉）`data-status` 在事件循环上扫仓库 | 首屏全线排队、假报后端不可达 | `test_api_no_loop_blocking.py` + 首屏实测脚本 |
