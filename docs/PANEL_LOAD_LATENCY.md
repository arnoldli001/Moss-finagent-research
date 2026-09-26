# 热点&研报小作文 / 事件告警 切页延迟：根因与优化

> 用户报障（2026-09-26）：
> 「热点&研报小作文、事件告警，这两个页面来回切，都会出现刚加载的信息界面，
>   还需要等 2-3 秒的延迟才展示数据，热加载这么慢吗？」
>
> 全部数字本机实测。

---

## 先回答"热加载这么慢吗"

**不是热加载慢，是根本没有热加载。**

这两个页面**没有共享的缓存层**，所谓"来回切"在 React 眼里是
**卸载 + 重新挂载**——组件内所有 state 归零，于是**每次都要重新走一遍网络**。

`App.tsx` 是三元链渲染，不是路由/keep-alive：

```tsx
) : view === "intel-hot" || view === "intel-calendar" ? (
  <IntelPanel ... section={view === "intel-calendar" ? "calendar" : "hot"} />
) : view === "alerts" ? (
  <AlertsPanel ... />
) : ...
```

切到告警 → `IntelPanel` **整个卸载**；切回来 → **全新挂载**。

---

## 一、两个页面的性质完全不同

| | 事件告警 | 热点&研报小作文 |
|---|---|---|
| 客户端缓存 | ✅ 有 `alertsCache.ts`（stale-while-revalidate + 登录预加载） | ❌ **完全没有** |
| 挂载后是否立刻有内容 | ✅ 有（缓存命中直接画，`AlertsPanel.tsx:274`） | ❌ 无（必发请求、先看骨架） |
| 延迟主因 | **纯响应体积** | **体积 + 无客户端缓存** |

**所以"两个都慢"但病因不同** —— 告警的缓存是好的，只是载荷太大；
情报流是缓存这一层**压根没建**。

### 实测载荷（决定隧道上的加载时间）

```
                        条目   明文         gzip     压比    @51KB/s 明文→gzip
事件告警 /alerts         100  109,526 B    1,870 B   1.7%   2,139ms → 37ms
情报流 /intel/feed        60   62,657 B   16,994 B  27.1%   1,224ms → 332ms
```

对照隧道实测带宽 **≈51 KB/s**（见 `web/src/alertsCache.ts` 记录的实测账）。

⚠️ **注意情报流的压比只有 27%（告警是 1.7%）** —— 因为情报的 `summary`
是**每条都不同的中文长文本**，而告警里我实测那份的 `description` 高度重复。
所以**gzip 对情报流的收益远小于告警**：告警 2,139ms → 37ms（58×），
情报 1,224ms → 332ms（3.7×）。

**结论：情报流光靠 gzip 不够，332ms 仍然可感知 —— 客户端缓存才是必需的。**

---

## 二、已实施的修复

### 2.1 新增 `web/src/intelCache.ts`（情报流客户端缓存）

对标 `alertsCache.ts` 的同一套语义：

- **localStorage 而不是内存** —— 内存缓存活不过一次刷新（F5），
  而"打开网站 → 点热点"正是要救的那条路
- **按 `filter|sort|direction` 分开存** —— 混在一起会让"切到高可信档"
  拿到"全部"的列表，看起来像筛选失效
- **先画缓存、再后台核对**（stale-while-revalidate）
- **登出清空** —— 换个人在同一台机器登录不该看到上一个人的情报

**一处与告警不同的设计**：情报流有一个"服务端正在后台重建"的状态
（`feed.refreshing`）。把这个状态一起缓存下来的话，切回来会**立刻**
又进入"等 WS 补拉"的轮询——而那份数据其实已经够看了。
所以写缓存时**强制 `refreshing: false`**，让切回来是"静默核对"语义。

### 2.2 `IntelPanel` 接入缓存 + 修掉两个 bug

```tsx
const ck = feedCacheKeyOf(f, s, d);
if (!opts.refresh) {
  const cached = readIntelFeed(ck);
  if (cached) { setFeed(cached.feed); setRefreshing(false); }
}
// ...然后照常发请求核对，拿到新的再覆盖 + writeIntelFeed(ck, data)
```

顺带修掉两个**既有 bug**（在接入缓存时发现的）：

1. **`loading` 永不解除**。原 `loadAll` 里 `setLoading(false)` 在
   `finally` 里，但改成调用 `loadFeed` 之后如果忘了补，界面会**永远卡在
   "加载中"**。已用 `try/finally` 保证。
2. **`direction` 被写死成 `"all"`**。原代码 `loadFeed(filter, sort, "all")`
   —— 用户在"仅多"档位下切一次筛选，倾向会被**悄悄重置回全部**，
   而界面上那个选择器还停在"仅多"。已改为传当前 `direction`，
   并把 `direction` 加进 `useCallback` 依赖。

### 2.3 登录后预加载情报流

与 `preloadAlerts` 同一位置（`useAuth.login`），**不 await**：

```ts
void import("../intelCache").then(async ({ feedCacheKeyOf, writeIntelFeed }) => {
  try {
    const { fetchIntelFeed } = await import("../intelApi");
    const feed = await fetchIntelFeed({ limit: 60 });
    writeIntelFeed(feedCacheKeyOf("all", "credibility", "all"), feed);
  } catch { /* 预加载失败对用户不可见且无害 */ }
});
```

**这让"第一次打开热点页"也是缓存命中**，而不只是"切回来快"。

缓存键的对齐已核对：面板初始 `filter="all" sort="credibility" direction="all"`
→ `feedCacheKeyOf("all","credibility","all")`，与预加载写入的键**逐字一致**。

### 2.4 补上体积守卫测试

`tests/integration/test_intel_payload_size.py`（6 个用例）——
因为**加载时间不由后端计算决定，而由响应体积决定**，
而体积膨胀是**静默**的（不会有任何报错，只是越来越慢）。

守四件事：
- 明文体积 ≤ 100 KB（实测 62.7 KB）
- gzip 后 ≤ 30 KB（实测 17.0 KB）
- gzip 后隧道时间 ≤ 700 ms
- **反向断言**：明文传输必须"慢到值得压" —— 若这条红了，
  说明隧道带宽假设（51 KB/s）已过时，所有阈值需要重新评估

---

## 三、修正后的时间线

```
事件告警（切回）
  修复前：卸载 → 重挂 → 发请求 → 明文 109 KB 过隧道 2,139ms → 才能画
          （其实缓存里就有，但 [见下] ）
  修复后：重挂 → 缓存命中立刻画（0ms）→ 后台核对 gzip 1.9 KB / 37ms

情报流（切回）
  修复前：卸载 → 重挂 → 发请求 → 骨架 → 明文 62.7 KB / 1,224ms → 才能画
          若服务端 refreshing，还要等 WS 通知再补拉一次
  修复后：重挂 → 缓存命中立刻画（0ms）→ 后台核对 gzip 17 KB / 332ms

情报流（首次打开，登录时已预加载）
  修复前：骨架 → 1,224ms+ 
  修复后：缓存命中立刻画 → 后台核对
```

---

## 四、有一件事我需要如实说明

**告警页的缓存其实早就是对的**（`alertsCache.ts` + 登录预加载 +
`AlertsPanel.tsx:274` 的 `loading ? "加载中…" : ..."` 都写对了），
所以它的 2-3 秒**只可能来自载荷** —— 而 gzip 已经把它从 2,139ms 压到 37ms。

**但我无法从代码层确认你实测的那 2-3 秒现在是否已经消失**，因为：
- 我改的是后端（gzip）与前端缓存，**没有真实浏览器会话可以量端到端**
- 对外试点的实际带宽我只能引用 `alertsCache.ts` 里记录的历史实测（51 KB/s），
  无法在此刻复测隧道

**建议你这样验证**（打开浏览器 DevTools）：
1. Network 面板看 `/api/v1/alerts` 与 `/api/v1/intel/feed` 的
   **Size 列**是否显示 gzip 后的小体积、`Content-Encoding: gzip` 是否存在
2. 切页签时看这两个请求的 **Time** —— 若仍有 2 秒，
   说明卡在**服务端**而非传输（那就该看 `/intel/feed` 的 `refreshing` 时序）
3. Application → Local Storage 搜 `moss.intel.feed.v1:` 与 `moss.alerts.v1:`
   —— **存在即表示预加载已生效**，切页应该瞬间出内容

---

## 五、如果还慢，下一个方向

| 现象 | 说明 | 对策 |
|---|---|---|
| Local Storage 里没有缓存键 | 预加载没成功（登录时机/权限） | 检查 `/intel/feed` 是否 401/403 |
| 有缓存但切页仍空 | 缓存键不匹配（改过 filter/sort 默认值） | 对比 `feedCacheKeyOf` 与预加载键 |
| 请求 Time 很长、Size 很小 | 卡在服务端而非传输 | 查 `/intel/feed` 的 `refreshing` 与 `_kick_feed_build` |
| Size 仍是大体积 | gzip 没生效 | 确认 `Content-Encoding`；反代可能剥掉了 |
| 首次打开慢、切回快 | 正常（首次无缓存） | 靠登录预加载覆盖 |

另外，情报流载荷里 **`credibility` 子对象占 29%**（60 条合计 14,937 B）。
如果还需要再砍体积，把列表里的 `credibility` 精简成
`{score, level}` 两个字段（详情页再给全量）能省约 25%。
我没有直接改，因为不确定前端列表是否用它渲染了别的信息。
