/**
 * `mainlineCache` 的**行为**自证（与 `check:intel` / `check:rateguard` 同款做法）。
 *
 * 为什么不让 pytest 只断言源码结构：结构判据是**形状判据** ——
 * 把 `MAX_AGE_MS` 改大、把形状校验删掉、把 `clear` 改成只删一个键，
 * 源码里那些字串照样存在，测试照样绿。这里喂真输入、看真输出。
 *
 * 覆盖"一旦写错就静默退化"的五种情形：
 *
 *   ① 往返：写进去的快照读出来必须**逐字段还在**（丢了 scores 会画成空面板，
 *      而用户会以为"今天没有主线"）；
 *   ② 形状：载荷缺 `scores` 时**必须当没有缓存**（画空白比走网络更糟）；
 *   ③ 损坏 / 过期：坏 JSON、超 TTL 一律当没有缓存（退回冷路径，不会更差）；
 *   ④ 键隔离：不同参数的缓存**不能互相污染**；
 *   ⑤ 清理：`clear` 只删自己前缀的键，**不动别人的**。
 *
 * 跑法：`npm run check:mainline`
 */
import type { MainlineSnapshot } from "../src/mainlineApi";
import {
  MAINLINE_MAX_AGE_MS,
  clearMainlineCache,
  mainlineCacheKey,
  mainlineSnapshotParams,
  readMainlineSnapshot,
  writeMainlineSnapshot,
} from "../src/mainlineCache";

// ---- 假 localStorage（Node 里没有 window）--------------------------------
const store = new Map<string, string>();
(globalThis as unknown as { window: unknown }).window = {
  localStorage: {
    get length() { return store.size; },
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => { store.set(k, String(v)); },
    removeItem: (k: string) => { store.delete(k); },
  },
};

let failed = 0;
function ok(cond: boolean, label: string) {
  if (cond) {
    console.log(`  ✅ ${label}`);
  } else {
    failed += 1;
    console.error(`  ❌ ${label}`);
  }
}

/** 一份最小可用快照（只带面板首帧真正要用的字段）。 */
function snap(tradeDate: string, boards: number): MainlineSnapshot {
  return {
    generated_at: `${tradeDate.slice(0, 4)}-${tradeDate.slice(4, 6)}`
      + `-${tradeDate.slice(6, 8)}T18:10:33+08:00`,
    trade_date: tradeDate,
    session_state: "closed",
    session_label: "已收盘",
    board_count: boards,
    scored_count: boards,
    candidate_count: 0,
    selected_count: 0,
    alert_count: 0,
    weight_mode: "static",
    weights: { six_dim: 80, accumulation: 20 },
    ic_summary: {},
    scores: Array.from({ length: boards }, (_, i) => ({
      code: `88580${i}`, name: `板块${i}`, total: 50 + i,
    })) as MainlineSnapshot["scores"],
    candidates: [], selected: [], alerts: [], tracked: [],
    source_notes: [], gaps: [], refresh_hint: "", disclaimer: "测试用",
  };
}

console.log("主线挖掘快照缓存（mainlineCache）行为自证");
console.log("─".repeat(70));

// ① 空缓存 → 没有缓存（不是"空对象"）
ok(readMainlineSnapshot() === null, "① 空缓存时读出来是 null");

// ② 写 → 读往返
const key = mainlineCacheKey();
writeMainlineSnapshot(snap("20260930", 116), key);
const back = readMainlineSnapshot(key);
ok(back !== null, "② 写进去能读出来");
ok(back?.snapshot.scores.length === 116, "② 评分条数原样保留（116）");
ok(back?.snapshot.board_count === 116, "② 漏斗计数原样保留");
ok(typeof back?.at === "number" && back.at > 0, "② 记下了写入时刻");

// ③ 形状不对 → 当没有缓存
store.set(key, JSON.stringify({
  version: 1, at: Date.now(), snapshot: { trade_date: "20260930" },
}));
ok(readMainlineSnapshot(key) === null, "③ 缺 scores 的载荷当没有缓存");

// ④ 损坏 JSON → 当没有缓存
store.set(key, "{ 这不是 JSON");
ok(readMainlineSnapshot(key) === null, "④ 坏 JSON 当没有缓存");

// ⑤ 过期 / 未过期（边界两侧各测一次）
writeMainlineSnapshot(snap("20260930", 116), key);
const raw = JSON.parse(store.get(key)!);
store.set(key, JSON.stringify({ ...raw, at: Date.now() - MAINLINE_MAX_AGE_MS - 1 }));
ok(readMainlineSnapshot(key) === null, "⑤ 超过 TTL 一律当没有缓存");
store.set(key, JSON.stringify({ ...raw, at: Date.now() - MAINLINE_MAX_AGE_MS + 5000 }));
ok(readMainlineSnapshot(key) !== null, "⑤ TTL 内仍然命中（边界另一侧）");

// ⑥ 键隔离：另一组参数的缓存不能顶替这一组
const other = mainlineCacheKey({ top: 20, alertLimit: 5 });
writeMainlineSnapshot(snap("20260101", 3), other);
ok(readMainlineSnapshot(key)?.snapshot.trade_date === "20260930",
   "⑥ 不同参数各自一份，互不污染");
ok(readMainlineSnapshot(other)?.snapshot.trade_date === "20260101",
   "⑥ 另一组参数读到的仍是它自己那份");

// ⑦ 清理：只删自己前缀
store.set("moss.alerts.v1:--", "别人的缓存");
clearMainlineCache();
ok(readMainlineSnapshot(key) === null, "⑦ clear 之后读不到（本模块的键已清）");
ok(store.get("moss.alerts.v1:--") === "别人的缓存",
   "⑦ clear 不动别人的键（只删自己前缀）");

// ⑧ 参数只有一处来源：与后端热快照指纹对应的那组值
const params = mainlineSnapshotParams();
ok(params.top === 60 && params.alertLimit === 100,
   "⑧ 请求参数 = top 60 / alert_limit 100（与后端 WARM_* 对应）");

console.log("─".repeat(70));
if (failed > 0) {
  console.error(`❌ ${failed} 条不通过`);
  process.exit(1);
}
console.log("✅ 全部通过");
