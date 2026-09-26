/**
 * 全局「会话失效」信号：任何数据源拿到 401 都广播它，`useAuth` 收到就退回登录页。
 *
 * ## 为什么必须有这一层（2026-09-24 用户报障）
 *
 * 现象：工作台里点开「板块拥挤度」，红条写着
 * `读取拥挤度数据失败：请求失败(401): {"detail":"需要登录：请先登录后再访问该接口。"}`，
 * 看起来像"这个功能坏了"，实际原因是**会话过期了**（30 分钟滑动 / 12 小时绝对上限）。
 *
 * 为什么会这样：会话一失效，工作台**不会自己退出** —— 它继续渲染，每个面板各自
 * 发起请求、各自弹自己的错误。用户看到的第一条错误取决于他点开了哪个页签
 * （这次是拥挤度），于是被误导去排查那个功能。
 *
 * 做法：把"401"从**每个面板的私事**提成**全局信号**。`fetch` 封装层广播事件，
 * `useAuth` 把状态切回 `anonymous`，`App` 自然渲染登录页并说明"登录已过期"。
 *
 * ## 为什么单独一个模块而不是放在 `api.ts` 里
 *
 * 五个数据源各有自己的 `request()` 封装（`api.ts` / `sectorCrowdingApi.ts` /
 * `mainlineApi.ts` / `etfFlowApi.ts` / `auctionApi.ts`）。这个模块**不 import 任何
 * 东西**，谁都能引，不会和 `api.ts` 形成环。
 */

export const UNAUTHORIZED_EVENT = "moss:unauthorized";

/** 会话过期时给用户看的说明（登录页顶部展示）。 */
export const UNAUTHORIZED_NOTICE = "登录状态已过期（或已在别处退出），请重新登录。";

/**
 * 广播「会话已失效」。
 *
 * ⚠️ **认证类接口的 401 是预期的**（启动探测 `/auth/me`、静默续期 `/auth/refresh`、
 * 密码错误），必须排除 —— 否则登录页会自己把自己刷掉，用户永远填不完表单。
 */
export function notifyUnauthorized(url: string): void {
  if (url.includes("/api/v1/auth/")) return;
  try {
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
  } catch {
    /* 非浏览器环境（测试/SSR）忽略：这只是 UI 提示，不该影响调用方 */
  }
}
