/**
 * 「免费档限流」一行的展示口径自证（本仓库 `check:intel` 的同款做法）。
 *
 * 为什么用它兜底：这一行是"siliconflow 被静默限流"**唯一**的可见面，
 * 而组件在浏览器里、要登录才能看到 —— 于是它的文案口径**没人能断言**。
 * 抽成纯函数后就能在这里穷举几种状态，包括最容易做错的那两种：
 *
 *   ① 「没量到」（读不到状态文件）**不能**显示成「0 次限流」
 *   ② 「零样本」**不能**显示成「正常」
 *   ③ 锁定必须给**人话 + 剩余时间**，不是裸秒数
 *
 * 跑法：npm run check:rateguard
 */
import { humanRemaining, rateGuardView } from "../src/rateGuardView";

let failed = 0;
function ok(cond: boolean, label: string) {
  if (cond) {
    console.log(`  ✅ ${label}`);
  } else {
    failed += 1;
    console.error(`  ❌ ${label}`);
  }
}

function entry(o: Partial<{
  locked: boolean; remaining_s: number | null; consecutive_429: number;
  total_429: number; total_success: number; last_reason: string;
}>) {
  return {
    locked: false, remaining_s: null, consecutive_429: 0,
    total_429: 0, total_success: 0, last_reason: "", ...o,
  };
}

console.log("免费档限流一行的展示口径");
console.log("─".repeat(70));

// ① 读不到状态 → 「未量到」，绝不能出现「0」
const unmeasured = rateGuardView({ available: false, error: "IO error" });
ok(unmeasured.text === "未量到", `读不到时为「未量到」（实际 ${unmeasured.text}）`);
ok(!unmeasured.detail.includes("0 次"), "读不到时不得提「0 次」（否则是假绿）");
ok(unmeasured.tone === "run-skipped", "读不到时不得用成功色");

// ①b 字段整个缺失（旧后端）
const missing = rateGuardView(undefined);
ok(missing.text === "未上报", `字段缺失时为「未上报」（实际 ${missing.text}）`);

// ② 零样本 → 「无记录」，不是「正常」
const noSamples = rateGuardView({ available: true, threshold: 3, lock_minutes: 10, models: {} });
ok(noSamples.text === "无记录", `零样本时为「无记录」（实际 ${noSamples.text}）`);
ok(noSamples.text !== "正常", "零样本不得显示为「正常」");

// ③ 锁定 → 人话 + 剩余时间（552s = 9 分 12 秒）
const locked = rateGuardView({
  available: true, threshold: 3, lock_minutes: 10,
  models: { "qwen-siliconflow-7b": entry({ locked: true, remaining_s: 552, total_429: 3 }) },
});
ok(locked.text === "已锁定", `锁定文案（实际 ${locked.text}）`);
ok(locked.tone === "run-failed", "锁定必须用失败色");
ok(locked.detail.includes("qwen-siliconflow-7b"), "必须说清是哪个模型");
ok(locked.detail.includes("9 分 12 秒"), `剩余时间要人话（实际：${locked.detail}）`);

// ③b 剩余时间在 1 分钟内 → 只给秒
ok(humanRemaining(42) === "42 秒", `秒级剩余（实际 ${humanRemaining(42)}）`);
ok(humanRemaining(0) === "0 秒", "0 秒也要能显示，不能变成 undefined");

// ④ 正常态：说明里要有阈值口径（否则读者不知道"多少算多"）
const healthy = rateGuardView({
  available: true, threshold: 3, lock_minutes: 10,
  models: { "qwen-siliconflow-7b": entry({ total_429: 7, total_success: 1200 }) },
});
ok(healthy.text === "正常", `健康态文案（实际 ${healthy.text}）`);
ok(healthy.tone === "run-success", "健康态用成功色");
ok(healthy.detail.includes("7"), "累计 429 次数要显示（量到 0 与没量到要能区分）");
ok(healthy.detail.includes("3") && healthy.detail.includes("10"), "要带上阈值与锁定时长");

// ⑤ 多模型同时锁定 → 不能只报一个而让人以为只有一个
const twoLocked = rateGuardView({
  available: true, threshold: 3, lock_minutes: 10,
  models: {
    "qwen-siliconflow-7b": entry({ locked: true, remaining_s: 300 }),
    "qwen-dashscope-flash": entry({ locked: true, remaining_s: 120 }),
  },
});
ok(twoLocked.detail.includes("另有 1 个"), `多个锁定时要说清（实际：${twoLocked.detail}）`);

console.log("─".repeat(70));
if (failed) {
  console.error(`❌ ${failed} 条不合格`);
  process.exit(1);
}
console.log("✅ 展示口径全部合格（未量到 / 零样本 / 锁定 / 正常 四态可分）");
