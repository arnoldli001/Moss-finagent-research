# 热点&研报小作文 / 事件告警：真正的预加载与保活

> 用户报障（2026-09-26，第二次）：
> 「热点&研报小作文 事件告警 每次打开这个界面不能预加载到浏览器吗，
>   切界面还是会有 2 秒延迟，数据可以在本地服务器后台处理，
>   界面打开的时候应该立即能加载到或者提前把数据放到前端服务器。」

---

## 一、上一轮为什么没解决（两个根因）

上一轮我给两个面板都加了 `localStorage` 缓存 + stale-while-revalidate，
**代码是对的**，但缓存**始终是空的**。两个根因：

### 根因 ① 预加载只在"用户手动登录"那一个动作里触发

```ts
// useAuth.login() —— 只有手动登录才走到这里
void import("../alertsCache").then(({ preloadAlerts }) => ...)
```

而绝大多数访问根本不是登录，是**带着 remember-me Cookie 刷新页面** ——
那条路径走的是 `probe()` → `authApi.bootstrap()`，**从来不预取**。

**所以每次刷新页面后，缓存都是冷的，第一次点面板必然等一个完整往返。**
这正好解释了你说的"每次打开"。

### 根因 ② 缓存有 TTL（告警 5 分钟 / 情报 10 分钟），放着就过期

即使在登录时预取到了，用户在别的页面待够 TTL，切回来时缓存已经作废。
在 ≈51 KB/s 的隧道上，一次冷取就是 2 秒级。

### 根因 ③（顺带发现）面板被**卸载重挂**

```tsx
view === "intel-hot" ? <IntelPanel/> : view === "alerts" ? <AlertsPanel/> : ...
```

三元链的语义是"**只渲染命中的那一个**"—— 切走即卸载，切回来 `useState` 全归零。
数据要重取之外，**展开的卡片、滚动位置、筛选档位也全丢**。

---

## 二、修复①：常驻**保活预取**（`web/src/panelPrefetch.ts`，新增）

不是"启动时取一次"，而是**持续保温**：

```
已登录 → 立刻预取一次
        ↓
   每 4 分钟后台续期（< 最短 TTL 5 分钟）
        ↓
   页面隐藏（切标签/最小化）时跳过 —— 回到前台立刻补一次
```

关键细节（每一条都写在代码注释里）：

- **挂在"已登录"状态上**，不是挂在 `login()` 动作上 —— 这样
  **刷新页面那条路径也会预取**（这才是本次报障的根因）。
- **续期必须 `force=true`**：`preloadAlerts` 有个 60 秒的新鲜度去重，
  不绕过它的话第二次起会被判定"还新鲜"而直接返回，**缓存永远停在第一次**，
  TTL 一到又变冷。所以给它加了 `{ force }` 参数。
- **续期间隔 4 分钟 < 最短 TTL 5 分钟**：否则两次续期之间会出现
  "缓存刚过期"的窗口，那就白做了。这个不变式有测试守着。
- **页面隐藏时跳过续期**：用户没在看，续期纯属白花隧道带宽 ——
  而带宽正是这两个面板慢的根因。回到前台立刻补。
- **绝不抛**：两个目标用 `Promise.allSettled` 互相隔离，失败静默 ——
  预取失败只等于"回到优化前的行为"，不会更差。
- **登出重置记账**：否则下一个人登录后，保活会以为"刚取过"而跳过第一次预取。

---

## 三、修复②：**keep-alive**（`web/src/components/KeepAlive.tsx`，新增）

把这两个面板**保持挂载**，用 `display: none` 控制显隐 ——
切回来是"显示"而不是"重建"，连交互状态一起保住。

```tsx
{/* ✅ 对：KeepAlive 始终在树上，只切换 active */}
<KeepAlive active={view === "alerts"}><AlertsPanel/></KeepAlive>
```

**三个必须遵守的摆放约束**（都有测试守着，因为写错了不会报错、
只会让延迟悄悄回来）：

1. **不能放进三元链** —— 放进去 `KeepAlive` 自己也被卸载，
   它的"曾可见过"标记一起归零，保活完全失效。
2. **不能放进主 `ErrorBoundary`** —— 它带 `resetKey={view}`，切页签会重置；
   常驻面板放进去，任何一次渲染错误都会把它们清掉，正好抵消保活。
3. **各自包一个独立 `ErrorBoundary`** —— 这两个面板现在**同时挂载**
   （新增的耦合面），一个崩了不该连累另一个。

**惰性挂载**：`everActive` 为假时 `return null` ——
"从没打开过的页面"不付任何构建/取数成本，只有第一次切过去之后才常驻。
用 `useRef` 而非 `useState`（渲染期更新），避免多出一帧"active 为真但
children 还没挂载"的闪白。

**为什么用 `display: none` 而不是 `visibility`**：后者仍然占位，
会把页面撑出空白。同时加了 `aria-hidden`，隐藏时对读屏器也隐藏。

---

## 四、修复③：`preloadAlerts` 支持强制刷新

```ts
export async function preloadAlerts(fetchBootstrap, opts: { force?: boolean } = {})
```

`force` 用于保活续期；不传时保持原来的 60 秒去重（多标签页各自登录时
只拉一份 112 KB，那个优化要保留）。

---

## 五、验证

### 5.1 构建

```
tsc --noEmit    exit=0
vite build      ✓ built in 1.16s
                dist/assets/panelPrefetch-SpzrKeo4.js   0.99 kB │ gzip: 0.55 kB
```

`panelPrefetch` 是**独立懒加载 chunk**（只在组件挂载后才需要），
不影响首屏。顺带修掉一个 Vite 警告：
`intelCache` 原来被"动态导入 + 静态导入"两次，是"看起来做了代码分割、
其实没有"的假象 —— 改成静态导入后警告消失。

### 5.2 新增结构守卫测试（11 个用例）

前端没有单测框架（仓库只有 `tsc` + `vite build`），所以用**源码结构断言**
守住几个"一旦写错就静默退化"的不变式：

```
test_keep_alive_is_not_mounted_inside_the_ternary_chain
test_panels_are_not_rendered_in_the_ternary_chain
test_ternary_returns_null_for_keepalive_views
test_keep_alive_lazily_mounts
test_keep_alive_hides_from_assistive_tech
test_keepalive_interval_is_shorter_than_shortest_cache_ttl
test_keepalive_uses_force_refresh
test_both_entry_paths_prefetch          ← 本次根因的守卫
test_prefetch_never_throws
test_prefetch_static_imports_shared_caches
test_logout_resets_prefetch_state
```

**11 passed**，`ruff` 全绿。

**并且反向验证过**：我把 `<KeepAlive>` 挪进三元链模拟"写错"，
2 个用例立刻变红；恢复后 11 个全绿。

> 实现这些用例时踩到一次误报：三元链里本来就有解释"为什么 `KeepAlive`
> 不能放进来"的**注释**，直接把源码当断言对象会把说明文字当成代码。
> 所以断言前先 `_strip_comments` —— 否则这种误报很容易让人以为测试坏了
> 而把测试删掉，正好丢掉守卫。

---

## 六、预期效果

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 刷新页面后首次点这两个页签 | 冷缓存 → **等一个完整往返（2 秒级）** | 缓存已预取 → **挂载即有内容**（后台静默核对） |
| 切到别的页面待 10 分钟再切回 | 缓存过期 → 又等 2 秒 | 保活已续期 → **立刻有内容** |
| 同一页签间来回切 | 卸载重挂：数据重取**且**展开/滚动/筛选全丢 | **不卸载**：状态与数据都保住 |
| 从没打开过这两个页签 | 无成本 | 无成本（惰性挂载） |

---

## 七、需要你确认的事

**我无法从代码层验证端到端的真实秒数** —— 没有浏览器会话可以量。
请按下面三步确认（DevTools）：

1. **Application → Local Storage** 搜 `moss.alerts.v1:` 与
   `moss.intel.feed.v1:` —— **登录后就有**说明预取生效了。
2. **切页签**，看 Network 里这两个请求是否**不再出现**
   （保活命中时面板不该再发请求）。
3. 若仍有延迟，看那条请求的 **Size 与 Time**：
   - Size 大 → gzip 没生效（查 `Content-Encoding`）
   - Size 小但 Time 长 → 卡在**服务端**而非传输（查 `/intel/feed` 的 `refreshing`）

如果 Local Storage 里**没有**这两个键，说明预取请求本身失败了
（多半是 401/403）—— 那要看 `/alerts/bootstrap` 与 `/intel/feed` 的返回码，
而不是继续在缓存层找原因。
