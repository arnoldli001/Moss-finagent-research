/**
 * URL 路由工具：地址栏的 hash 携带「当前页签 + 页内子状态」。
 *
 * ## 为什么是 hash 而不是 path
 *
 * 本项目由 FastAPI 的 `StaticFiles` 托管，**没有服务端路由**。用 `#/intraday?code=300308`
 * 这样的 hash：
 *   · 刷新/收藏/发链接都能回到同一页同一只票；
 *   · hash **不会发给服务器**，所以静态托管不会 404，后端零改动；
 *   · 用 path 则要在服务器上给每个路径都回同一份 index.html（SPA fallback），
 *     那是另一个改动面。
 *
 * ## 为什么子状态要"逐键写"而不是整体覆盖
 *
 * 页签、标的、周期分别由不同组件维护。如果每个组件都"读全量 → 改自己那项 → 写全量"，
 * 就有竞态：A 组件基于旧值写回时会把 B 刚写的键抹掉。
 * 所以只提供 `setParam(key, value)` —— 它**实时读当前 hash** 再改一个键，
 * 任何人写都不会踩掉别人。
 */

export type Route = {
  view: string;
  params: URLSearchParams;
};

/** 解析当前地址栏：`#/intraday?code=300308&mode=daily` */
export function readRoute(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const qIndex = raw.indexOf("?");
  const view = qIndex >= 0 ? raw.slice(0, qIndex) : raw;
  const params = new URLSearchParams(qIndex >= 0 ? raw.slice(qIndex + 1) : "");
  return { view, params };
}

/** 整体替换（只改页签时用；**会保留**已有的子状态参数）。 */
export function writeRoute(view: string, params?: URLSearchParams): void {
  const current = readRoute().params;
  const next = params ?? current;
  const q = next.toString();
  const want = `#/${view}${q ? `?${q}` : ""}`;
  if (window.location.hash !== want) {
    window.history.replaceState(null, "", want);
  }
}

/** 只改一个子状态键（value 传 null 表示删掉）。**不会动页签与其它键。** */
export function setParam(key: string, value: string | null): void {
  const { view, params } = readRoute();
  if (value === null || value === "") params.delete(key);
  else params.set(key, value);
  writeRoute(view, params);
}

/** 读一个子状态键。 */
export function getParam(key: string): string {
  return readRoute().params.get(key) || "";
}
