/**
 * 认证状态管理（Hook + Props Context 无关，直接在 App 里用）。
 *
 * 对应后端：`src/api/routes/auth.py`。
 *
 * ## 启动时只探测**一次**
 *
 * ```
 * GET /auth/bootstrap
 *   ├─ 会话 Cookie 有效        → authenticated:true（不轮换任何令牌）
 *   ├─ 否则有"记住我"Cookie     → 服务端续期 + 一并返回身份
 *   └─ 都没有                  → authenticated:false → 显示登录页
 * ```
 *
 * 早先这里是三次串行往返（`/me` 401 → `/refresh` → `/me` 200）。本机看着
 * 无所谓，但公网每次往返实测 0.4~2 秒且偶发 502，于是"正在检查登录状态"
 * 要停十秒左右（2026-09-26 用户报障）。
 *
 * **顺序约束没有消失，只是搬到了服务端**：`refresh` 会**轮换** remember
 * token，所以绝不能无条件先 refresh —— 一旦某次响应丢失（网络抖动/用户
 * 中途关页），客户端手里的旧令牌就变成"重放"，服务端会**撤销整个令牌家族**，
 * 用户被强制重新登录（设计文档 §8.6.5④）。现在由 `/auth/bootstrap` 保证
 * "先看会话、确实没有才续期"，客户端无法用错顺序。
 *
 * ## 为什么不把令牌存 localStorage
 *
 * 令牌只在 HttpOnly Cookie 里，JS 读不到。这不是"少写几行代码"，
 * 而是 XSS 防护的全部意义 —— 一旦放进 localStorage，任何一段被注入的脚本
 * 都能把用户的长期登录态带走。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { AuthUser, LoginMode, MeResult, authApi } from "../api";
import { UNAUTHORIZED_EVENT, UNAUTHORIZED_NOTICE } from "../unauthorized";

export type AuthPhase = "checking" | "anonymous" | "authenticated";

export type AuthState = {
  phase: AuthPhase;
  user: AuthUser | null;
  me: MeResult | null;
  /** 启动探测时的说明（出问题时给用户看，而不是静默停在"检查中"）。 */
  notice: string;
  /** 探测顺带拿到的图形码判定，登录页直接用，省一次公网往返。 */
  loginMode: LoginMode | null;
};

/** 探测请求本身失败（断网 / 后端 5xx）时的说明。
 *
 * 这种时候必须给出**可读的结论**，而不是把 phase 留在 `checking`：
 * 那正是用户看到的"一直显示正在检查登录状态"。
 */
const PROBE_FAILED_NOTICE =
  "无法确认登录状态：与服务器通信失败。请检查网络后重新登录。";

const INITIAL: AuthState = {
  phase: "checking", user: null, me: null, notice: "", loginMode: null,
};

/** 把开机探测的真实耗时打到控制台：这个数就是用户盯着"正在检查登录状态"的时间。
 *
 * 为什么要专门记一条：本轮排障最难的地方不是修，而是**量** —— 服务端日志
 * （uvicorn 访问行）没有时间戳，公网又是 0.4~2 秒、偶发 502 的抖动链路，
 * 只能靠零散 curl 去推。有了这一行，下次用户说"还是慢"时，
 * 打开控制台就能看到是探测慢、还是别的阶段慢。
 */
function logProbe(phase: string, ms: number): void {
  try {
    console.info(
      `[auth] 开机探测 ${phase}：${Math.round(ms)} ms（/auth/bootstrap 单次往返）`);
  } catch {
    /* 控制台不可用（极旧浏览器）不该影响登录 */
  }
}

export function useAuth() {
  const [state, setState] = useState<AuthState>(INITIAL);
  // 防止 React 18 StrictMode 下 effect 跑两次、并发发两个 refresh
  // （并发 refresh 会触发令牌轮换冲突 → 被判定重放 → 撤销家族）。
  const probing = useRef(false);

  const probe = useCallback(async () => {
    if (probing.current) return;
    probing.current = true;
    const t0 = performance.now();
    try {
      // 一次往返拿到全部结论：服务端内部做"会话优先 → 否则静默续期"。
      //
      // 旧的写法是三步串行（me 401 → refresh → me 200）。每一步都是一次
      // 公网往返（实测 0.4~2 秒，且偶发 502），三次叠起来就是用户看到的
      // "正在检查登录状态"停十秒左右。其中第三步是纯浪费：refresh 的响应
      // 里本来就带 user，却还是重问了一遍 /me。
      const r = await authApi.bootstrap();
      logProbe(r.authenticated ? (r.renewed ? "续期成功" : "已有会话")
                               : "未登录", performance.now() - t0);
      if (r.authenticated) {
        setState({
          phase: "authenticated",
          user: toUser(r),
          me: toMe(r),
          notice: "",
          loginMode: r.login_mode ?? null,
        });
        return;
      }
      // 未登录是**正常结论**，不是异常：不需要 try/catch 兜。
      setState({
        phase: "anonymous", user: null, me: null, notice: "",
        loginMode: r.login_mode ?? null,
      });
    } catch (e) {
      // 只有真的"请求本身失败"（断网、后端 5xx）才走到这里。
      // 此时**不能**停在 checking —— 那正是"一直显示正在检查登录状态"。
      logProbe(`失败（${e instanceof Error ? e.message : String(e)}）`,
               performance.now() - t0);
      setState({
        phase: "anonymous", user: null, me: null,
        notice: PROBE_FAILED_NOTICE, loginMode: null,
      });
    } finally {
      probing.current = false;
    }
  }, []);

  useEffect(() => { void probe(); }, [probe]);

  /**
   * 任何数据源拿到 401（会话过期、在别处退出、被管理员踢下线）→ 立刻退回登录页。
   *
   * 不接这个信号的话，工作台会**继续渲染**，而每个面板各自弹自己的错误
   * （2026-09-24 用户报障：「读取拥挤度数据失败：请求失败(401)……」——
   * 看起来像拥挤度功能坏了，实际只是掉线了）。这里把状态切回 anonymous，
   * `App` 自然渲染登录页，并带上"登录已过期"的说明。
   */
  useEffect(() => {
    const onUnauthorized = () => {
      setState({ phase: "anonymous", user: null, me: null,
                 notice: UNAUTHORIZED_NOTICE, loginMode: null });
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  const login = useCallback(async (
    account: string, password: string, rememberMe: boolean,
    captcha?: { captcha_token: string; captcha_answer: string },
  ) => {
    const result = await authApi.login({
      account, password, remember_me: rememberMe, ...(captcha ?? {}),
    });
    // `/auth/login` 的响应里已经有完整身份，**不要再打一次 `/me`**：
    // 那是一次纯粹白花的公网往返（实测 0.4~2 秒），而登录本来就是
    // 用户盯着等的一步。`me` 留空即可 —— 全应用没有人读它，
    // 需要联系方式时走 `reload()`（它会真的去拿 `/me`）。
    setState((prev) => ({
      ...prev,
      phase: "authenticated", user: result.user ?? null, me: null,
      notice: "",
    }));
    // ★ 预加载事件告警与情报流。
    //
    // 这里**保留**（登录是"用户马上就会用到"的时刻，越早取越好），
    // 但**不再只靠这里** —— 见 `panelPrefetch.startPanelKeepAlive`：
    // 绝大多数访问其实是"带 remember-me Cookie 刷新页面"，走的是
    // `probe()` 而不是本函数，原来那条路径**从不预取**，
    // 所以每次刷新后缓存都是冷的、第一次点面板必然等一个完整往返
    // （用户报障 2026-09-26："每次打开这个界面不能预加载到浏览器吗"）。
    //
    // 刻意**不 await**：登录这一步不该被任何"增益型"请求拖慢，
    // 失败也只等于"第一次打开时慢一次"。
    void import("../panelPrefetch").then(({ prefetchPanels }) =>
      prefetchPanels(true));
    return result;
  }, []);

  const logout = useCallback(async (allDevices = false) => {
    try {
      await authApi.logout(allDevices);
    } finally {
      // ★ 清掉本地缓存：换个人在同一台机器上登录，不该看到上一个人的告警。
      //   放在 finally 里 —— 即使登出请求失败也要清（留在界面上只会让人
      //   以为还登着，那缓存就更不该留）。
      try {
        const { clearAlertsCache } = await import("../alertsCache");
        clearAlertsCache();
      } catch { /* 清理失败不该阻断登出 */ }
      // 情报流缓存同理：换个人在同一台机器登录，不该看到上一个人的情报。
      try {
        const { clearIntelCache } = await import("../intelCache");
        clearIntelCache();
      } catch { /* 清理失败不该阻断登出 */ }
      // 预取记账也要重置：否则下一个人登录后，保活续期会以为
      // "刚刚取过"而跳过第一次预取，缓存又是冷的。
      try {
        const { resetPrefetchState } = await import("../panelPrefetch");
        resetPrefetchState();
      } catch { /* 同上 */ }
      // 即使请求失败也要回到登录页：留在界面上只会让人以为还登着
      setState({ phase: "anonymous", user: null, me: null, notice: "",
                 loginMode: null });
    }
  }, []);

  /** 重新读取 `/me`（改密、被管理员改状态后调用）。 */
  const reload = useCallback(async () => {
    try {
      const me = await authApi.me();
      setState((prev) => ({ ...prev, user: toUser(me), me }));
      return me;
    } catch {
      setState({ phase: "anonymous", user: null, me: null, notice: "",
                 loginMode: null });
      return null;
    }
  }, []);

  return { ...state, login, logout, reload, probe };
}

function toUser(me: MeResult): AuthUser {
  return {
    user_id: me.user_id, username: me.username,
    display_name: me.display_name, status: me.status,
    applied_tier: me.applied_tier, valid_until: me.valid_until,
  };
}

/** 只取 `/me` 载荷里属于"身份 + 联系方式"的那几个字段。
 *
 * 为什么不直接把整个响应对象当 `me`：bootstrap 的响应还带着
 * `authenticated` / `login_mode` 这些**探测专用**字段，混进 `me`
 * 会让"资料"与"协议状态"两种数据流串在一起。
 */
function toMe(r: MeResult): MeResult {
  return {
    user_id: r.user_id, username: r.username,
    display_name: r.display_name, status: r.status,
    applied_tier: r.applied_tier, valid_until: r.valid_until,
    session_id: r.session_id, contacts: r.contacts,
  };
}

/** 状态中文名（前端只做展示，判定一律用原始值）。 */
export const STATUS_LABEL: Record<string, string> = {
  active: "正常",
  pending: "待审批",
  disabled: "已停用",
  expired: "已过期",
};

/** 套餐中文名。 */
export const TIER_LABEL: Record<string, string> = {
  admin: "管理员",
  vip: "VIP",
  trial: "试用",
  "": "未分配",
};
