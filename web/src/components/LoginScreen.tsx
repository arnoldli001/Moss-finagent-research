/**
 * 登录 / 注册 / 找回密码 三合一入口页。
 *
 * ## 为什么合成一个组件而不是三个页面
 *
 * 本应用**没有路由库**（只是 `App.tsx` 里的一个 `view` state）。
 * 为一个前置入口引入 react-router 不划算；而这三个流程本身是**互相跳转的一串**
 * （登录 → 忘记密码 → 重置 → 回登录；注册 → 待审批 → 回登录），
 * 放在一个组件里用一个 `mode` 切换，比三个互相 import 的组件更短也更清楚。
 *
 * ## 三个流程与后端端点的对应
 *
 * | 界面动作 | 后端 |
 * |---|---|
 * | 获取图形码 → 发邮箱验证码 | `GET /auth/captcha` → `POST /auth/verify-code` |
 * | 注册 | `POST /auth/register`（落 `pending`，**需管理员审批**） |
 * | 登录 | `POST /auth/login`（Set-Cookie 三层令牌） |
 * | 找回：发码 | `POST /auth/password/forgot`（**防枚举**，文案统一） |
 * | 找回：重置 | `POST /auth/password/reset` |
 *
 * ⚠️ 注册**不会**直接能登录：后端把新用户落成 `pending`，等管理员审批。
 * 界面上必须说清楚，否则用户会一直试密码并以为系统坏了。
 */

import { useEffect, useRef, useState } from "react";
import { authApi } from "../api";
import { LoginMode } from "../api";
import { LoginResult } from "../api";
// `ApiError` 是判断"服务端要求图形码"的**唯一可靠依据**（见 `run` 的说明）
import { ApiError } from "../errors";
import HumanCheck, { CaptchaValue } from "./HumanCheck";

type Mode = "login" | "register" | "forgot";

const EMPTY_CAPTCHA: CaptchaValue = { token: "", answer: "", ready: false };

/** 客服微信：注册审批用。集中一处，改文案不必翻 JSX。 */
const WECHAT_ID = "lucy2026Sun";

/** 品牌图标（内联 SVG，`currentColor` 跟随主题，不依赖图片/字体）。 */
function BrandMark() {
  return (
    <svg viewBox="0 0 24 24" width="26" height="26" fill="none"
      stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
      strokeLinejoin="round">
      <rect x="2.5" y="3.5" width="19" height="17" rx="2.5" />
      <path d="M5.5 15.5 9 11l3 2.5L18.5 7" />
      <circle cx="18.5" cy="7" r="1.3" fill="currentColor" stroke="none" />
    </svg>
  );
}

/** 对话气泡图标（微信提示用）。 */
function ChatIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none"
      stroke="currentColor" strokeWidth="1.9" strokeLinecap="round"
      strokeLinejoin="round">
      <path d="M20 12.5c0 3.6-3.6 6.5-8 6.5-1 0-2-.2-2.9-.5L5 20l1.2-3.1C4.8 15.6 4 14.1 4 12.5 4 8.9 7.6 6 12 6s8 2.9 8 6.5Z" />
    </svg>
  );
}

export default function LoginScreen({
  onLogin,
  notice,
  loginMode,
}: {
  onLogin: (account: string, password: string, remember: boolean,
            captcha?: { captcha_token: string; captcha_answer: string })
    => Promise<LoginResult>;
  /** 会话过期等外部原因导致的"被退回登录页"，在这里如实说明原因。 */
  notice?: string;
  /** 开机探测（`/auth/bootstrap`）顺带带回的图形码判定。
   *
   * 传了就直接用 —— 否则本组件会再问一次 `/auth/login-mode`，那是一次
   * 白花的公网往返（实测 0.4~2 秒）。为空（探测失败/被退回登录页）时才自己取。
   */
  loginMode?: LoginMode | null;
}) {
  const [mode, setMode] = useState<Mode>("login");
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [copied, setCopied] = useState(false);

  // 注册用
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [code, setCode] = useState("");
  // 找回用
  const [resetToken, setResetToken] = useState("");
  const [newPassword, setNewPassword] = useState("");

  /**
   * 图形码状态。
   *
   * 注册/找回**共用**一份（它们不会同时显示）；
   * 登录单独一份 —— 因为登录的图形码是**按需出现**的
   * （该 IP 失败过才要求），不能与注册那份互相覆盖。
   */
  const [formCaptcha, setFormCaptcha] = useState<CaptchaValue>(EMPTY_CAPTCHA);
  const [loginCaptcha, setLoginCaptcha] = useState<CaptchaValue>(EMPTY_CAPTCHA);
  /** 登录是否需要图形码（优先用开机探测带回来的判定）。 */
  const [loginNeedsCaptcha, setLoginNeedsCaptcha] = useState(
    () => Boolean(loginMode?.require_captcha));
  /** 用 ref 读最新值：`sendCode` 等回调里直接引用 state 会拿到过期闭包。 */
  const formRef = useRef(formCaptcha);
  formRef.current = formCaptcha;
  const loginRef = useRef(loginCaptcha);
  loginRef.current = loginCaptcha;
  /** 用于在"图形码被消费/失效"后强制 HumanCheck 重新领一张（换 key 即重挂载）。 */
  const [formCaptchaKey, setFormCaptchaKey] = useState(0);
  const [loginCaptchaKey, setLoginCaptchaKey] = useState(0);

  // 只在探测**没有**带回判定时才自己问一次"要不要图形码"：
  // 需要才渲染，避免正常用户白填一次表单。
  useEffect(() => {
    if (loginMode) {
      setLoginNeedsCaptcha(Boolean(loginMode.require_captcha));
      return;
    }
    authApi.loginMode()
      .then((m) => setLoginNeedsCaptcha(Boolean(m.require_captcha)))
      .catch(() => { /* 拿不到就不显示（服务端仍会在需要时拒绝） */ });
  }, [loginMode]);

  /** 换一张图形码（令牌失效或被消费后调用）。 */
  const renewCaptcha = (which: "form" | "login") => {
    if (which === "form") {
      setFormCaptcha(EMPTY_CAPTCHA);
      setFormCaptchaKey((k) => k + 1);
    } else {
      setLoginCaptcha(EMPTY_CAPTCHA);
      setLoginCaptchaKey((k) => k + 1);
    }
  };

  /**
   * 复制微信号。
   *
   * 为什么不用 `navigator.clipboard` 单一实现：它在**非 HTTPS**（本机
   * `http://127.0.0.1` 之外）或旧浏览器里会直接抛，而"复制微信号"
   * 恰恰是用户最可能用到的一步。所以先试 Clipboard API，失败则退回
   * `document.execCommand("copy")` 的老办法；两者都失败时**把微信号
   * 选中**让用户自己 Ctrl+C —— 总之不能静默失败。
   */
  const copyWechat = async () => {
    setError("");
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(WECHAT_ID);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1800);
        return;
      }
      throw new Error("clipboard unavailable");
    } catch {
      const ta = document.createElement("textarea");
      ta.value = WECHAT_ID;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1800);
      } else {
        setInfo(`请手动复制微信号：${WECHAT_ID}`);
      }
    }
  };

  /**
   * 统一执行 + 失败处理。
   *
   * ★ 关键细节：**图形码相关的失败必须自动换一张图**。
   *
   * 令牌是一次性的（校验即作废，无论对错）。所以用户答错一次之后，
   * 手里那个令牌已经失效 —— 如果他再点一次，服务端只会继续报
   * "图形验证码不正确"，而他盯着的是同一张图、答案也没变，
   * 会觉得"明明填对了却一直说错"。自动换图把这条死循环切断。
   */
  const run = async (fn: () => Promise<void>, which?: "form" | "login") => {
    setBusy(true); setError(""); setInfo("");
    try {
      await fn();
    } catch (e) {
      const text = explain(e);
      setError(text);

      // ## ⚠️ 判据必须是**错误码**，不能是文案或 `String(e)`
      //
      // 这里踩过一个把用户**彻底锁在门外**的坑：
      //
      //   `ApiError` 的 `message` 已经是**给用户看的中文**
      //   （"为确认是你本人操作，请先完成图形验证码"），
      //   而 `String(e)` 得到的是 `"ApiError: 为确认…"` ——
      //   **错误码 `captcha_required` 一个字都不在里面**。
      //
      //   于是 `/captcha/i` 匹配不到（"图形验证码"里没有 captcha 这个词），
      //   `/captcha_required/` 也匹配不到。结果是：服务端要求图形码时，
      //   前端**永远不渲染图形码输入框**，用户每次点登录都只看到同一句
      //   "请先完成图形验证码"，而页面上根本没有那个东西可填 ——
      //   刷新、换浏览器都没用（服务端按 **IP** 记状态）。
      //
      // 现在直接读 `ApiError.code`；文案匹配只作**兜底**（覆盖非 ApiError
      // 的旧格式异常），且把 code 也拼进被搜索的字符串。
      const code = e instanceof ApiError ? e.code : "";
      const hay = `${code} ${text} ${String(e)}`;
      // 服务端实际会发的两个码（见 `routes/auth.py`）：
      //   `captcha_required` 登录时被要求图形码（本次没带或答案错）
      //   `captcha_failed`   发邮箱验证码前的图形码校验没过
      const captchaProblem = code === "captcha_required"
        || code === "captcha_failed"
        || code === "captcha_invalid"
        || /captcha|图形验证码/.test(hay);

      if (which && captchaProblem) {
        renewCaptcha(which);
      }
      // 登录被要求图形码（本次没带）→ **立刻把输入框渲染出来**，
      // 不用等用户刷新页面（刷新也未必有用：状态在服务端按 IP 记着）。
      if (which === "login" && captchaProblem) {
        setLoginNeedsCaptcha(true);
      }
    } finally {
      setBusy(false);
    }
  };

  const doLogin = () => run(async () => {
    // 图形码只在服务端要求时才带上（正常用户第一次登录不需要）。
    // 带上空串也不会出错：服务端只在"该 IP 已被要求"时才校验它。
    const r = await onLogin(account.trim(), password, remember,
                            loginNeedsCaptcha ? captchaFields("login")
                                              : undefined);
    if (r.must_change_password) {
      setInfo("登录成功，但需要先修改初始密码。");
    }
  }, "login");

  /**
   * 取当前表单图形码的两个字段。
   *
   * ⚠️ 从 ref 读而不是闭包变量：这些处理函数在事件回调里执行，
   * 直接引用 state 可能拿到"上一次渲染"的值（答案刚敲进去的那次就丢了）。
   */
  const captchaFields = (which: "form" | "login") => {
    const v = which === "form" ? formRef.current : loginRef.current;
    return { captcha_token: v.token, captcha_answer: v.answer };
  };

  const sendCode = (scene: "register" | "reset_password") => run(async () => {
    const r = await authApi.verifyCode({
      scene, email: email.trim(), ...captchaFields("form"),
    });
    setInfo(r.message || "验证码已发送，请查收邮箱（5 分钟内有效）。");
    // ★ 图形码令牌是**一次性**的：这一次已经消费掉了，必须换新的，
    //   否则下一步「注册」会带着一个作废令牌去发码 → 报"验证码不正确"，
    //   而用户完全看不出是图形码的问题（他会以为邮箱验证码错了）。
    renewCaptcha("form");
  }, "form");

  const doRegister = () => run(async () => {
    // 注册前需要再发一次邮箱验证码（用户已在上一步填过），
    // 用**新领的**图形码令牌（旧的已被 `sendCode` 消费）。
    // 简化：让用户在「注册」前先点过「发送验证码」，这里直接提交。
    const r = await authApi.register({
      email: email.trim(), username: username.trim(),
      password, code: code.trim(), ...captchaFields("form"),
    });
    setInfo(r.message
      || "注册已提交，状态为「待审批」。管理员通过后即可登录。");
    setMode("login");
    setAccount(username.trim() || email.trim());
  }, "form");

  const doForgot = () => run(async () => {
    const r = await authApi.forgot({
      email: email.trim(), ...captchaFields("form") });
    // 后端**故意**不区分"邮箱是否存在"，文案一致；前端也不要去猜。
    setInfo(r.message || "若该邮箱存在，我们已发送重置验证码。");
  });

  const doReset = () => run(async () => {
    const r = await authApi.reset({
      email: email.trim(), code: code.trim(),
      token: resetToken.trim(), new_password: newPassword,
    });
    setInfo(r.message || "密码已重置，请用新密码登录。");
    setMode("login");
  });

  return (
    <div className="auth-shell">
      <div className="auth-card">
        {/* 品牌区：让它看起来像一个**正规站点**，而不是一张裸表单。
            刻意不可点击：登录页点站名没有去处（"回首页"还是这一页），
            那会让人以为按钮坏了；登录成功后顶栏的站名才可点击回首页。 */}
        <div className="auth-brand">
          <span className="auth-brand-mark" aria-hidden="true">
            <BrandMark />
          </span>
          <div>
            <h1 className="auth-title">Moss-FinAgent-Research</h1>
            <p className="auth-sub">多Agent投研工作台 · 全链路可溯源</p>
          </div>
        </div>

        {/* 注册是**审批制**：新账号落 pending，管理员通过后才能登录。
            这句话必须在登录页最显眼处 —— 否则用户注册完一直试着登录，
            只看到"等待管理员审批"，却不知道该找谁。
            （用户口径 2026-09-23：在登录界面提示加微信获取账号审批。） */}
        <div className="auth-approval">
          <span className="auth-approval-icon" aria-hidden="true">
            <ChatIcon />
          </span>
          <div>
            <b>注册后需管理员审批才能登录</b>
            <div className="auth-approval-line">
              加微信 <code className="auth-wx">{WECHAT_ID}</code>
              <button className="auth-copy" type="button"
                onClick={() => void copyWechat()}>
                {copied ? "✓ 已复制" : "复制"}
              </button>
            </div>
          </div>
        </div>

        <div className="auth-tabs">
          {(["login", "register", "forgot"] as Mode[]).map((m) => (
            <button key={m}
              className={mode === m ? "auth-tab active" : "auth-tab"}
              onClick={() => { setMode(m); setError(""); setInfo(""); }}>
              {m === "login" ? "登录" : m === "register" ? "注册" : "找回密码"}
            </button>
          ))}
        </div>

        {error && <div className="error-box auth-msg">{error}</div>}
        {(notice || info) && <div className="info-box auth-msg">{notice || info}</div>}

        {mode === "login" && (
          <>
            <label className="auth-label">邮箱或用户名</label>
            <input className="auth-input" value={account} autoFocus
              onChange={(e) => setAccount(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void doLogin(); }}
              placeholder="you@example.com" />
            <label className="auth-label">密码</label>
            <input className="auth-input" type="password" value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") void doLogin(); }}
              placeholder="输入密码" />
            <label className="auth-check">
              <input type="checkbox" checked={remember}
                onChange={(e) => setRemember(e.target.checked)} />
              7 天内免登录（关掉浏览器再打开无需重新输入密码）
            </label>
            {/* 图形码**按需出现**：只有该 IP 失败过才要求（服务端判定）。
                这样正常用户第一次登录不会被图形码打扰，而脚本连试会被拦。 */}
            {loginNeedsCaptcha && (
              <HumanCheck key={loginCaptchaKey} value={loginCaptcha}
                onChange={setLoginCaptcha} autoFocus />
            )}
            <button className="auth-submit" disabled={busy
              || !account.trim() || !password
              || (loginNeedsCaptcha && !loginCaptcha.ready)}
              onClick={() => void doLogin()}>
              {busy ? "登录中…" : "登录"}
            </button>
          </>
        )}

        {mode === "register" && (
          <>
            <label className="auth-label">邮箱</label>
            <input className="auth-input" value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com" />
            <label className="auth-label">用户名</label>
            <input className="auth-input" value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="用于登录的账号名" />
            <label className="auth-label">邮箱验证码</label>
            <div className="auth-row">
              <input className="auth-input" value={code}
                onChange={(e) => setCode(e.target.value)} placeholder="6 位验证码" />
              <button className="auth-inline-btn"
                disabled={busy || !email.trim() || !formCaptcha.ready}
                onClick={() => void sendCode("register")}>
                发送验证码
              </button>
            </div>
            {/* 图形码放在「发送验证码」**上方语义上更顺**，但这里放在下方是
                刻意的：用户先看邮箱→点发送→才需要看图，放上方会先看到一张
                与当前动作无关的图。 */}
            <HumanCheck key={formCaptchaKey} value={formCaptcha}
              onChange={setFormCaptcha} />
            <label className="auth-label">密码</label>
            <input className="auth-input" type="password" value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="至少 12 位，含大小写字母/数字/符号中的三类" />
            <button className="auth-submit" disabled={busy
              || !email.trim() || !username.trim() || !code.trim() || !password}
              onClick={() => void doRegister()}>
              {busy ? "提交中…" : "注册"}
            </button>
            <p className="auth-hint">
              注册后状态为<b>待审批</b>，管理员通过后才可登录。
            </p>
          </>
        )}

        {mode === "forgot" && (
          <>
            <label className="auth-label">注册邮箱</label>
            <input className="auth-input" value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com" />
            <div className="auth-row">
              <button className="auth-inline-btn"
                disabled={busy || !email.trim() || !formCaptcha.ready}
                onClick={() => void doForgot()}>
                发送重置验证码
              </button>
            </div>
            <HumanCheck key={formCaptchaKey} value={formCaptcha}
              onChange={setFormCaptcha} />
            <label className="auth-label">邮箱验证码</label>
            <input className="auth-input" value={code}
              onChange={(e) => setCode(e.target.value)} placeholder="6 位验证码" />
            <label className="auth-label">
              重置令牌（邮件中提供）
            </label>
            <input className="auth-input" value={resetToken}
              onChange={(e) => setResetToken(e.target.value)}
              placeholder="验证码通过后下发的一次性令牌" />
            <label className="auth-label">新密码</label>
            <input className="auth-input" type="password" value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)} />
            <button className="auth-submit" disabled={busy
              || !email.trim() || !code.trim() || !resetToken.trim()
              || !newPassword} onClick={() => void doReset()}>
              {busy ? "重置中…" : "重置密码"}
            </button>
            <p className="auth-hint">
              出于安全考虑，无论邮箱是否已注册，这里的提示都一样 ——
              避免被用来探测哪些邮箱是有效账号。
            </p>
          </>
        )}
      </div>
    </div>
  );
}

/** 把 `请求失败(400): {"detail":{...}}` 解释成人话。 */
export function explain(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  const m = raw.match(/\{.*\}/s);
  if (m) {
    try {
      const body = JSON.parse(m[0]) as {
        detail?: string | { message?: string; code?: string };
      };
      const d = body.detail;
      if (typeof d === "string") return d;
      if (d && typeof d === "object" && d.message) return d.message;
    } catch { /* 不是 JSON 就退回原文 */ }
  }
  return raw;
}
