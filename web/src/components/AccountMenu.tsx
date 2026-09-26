/**
 * 顶栏账户区：身份徽标 + 下拉（设备管理 / 改密 / 退出）。
 *
 * 对应后端：`GET /auth/me`、`GET /auth/sessions`、
 * `DELETE /auth/sessions/{id}`、`POST /auth/password/change`、`POST /auth/logout`。
 *
 * ## 为什么"退出登录"要分两个按钮
 *
 * 「退出本机」与「退出全部设备」是**两种不同的需求**：
 *   - 在公共电脑上 → 只想退掉这一台；
 *   - 怀疑账号被盗 → 需要一键踢掉所有设备（用户自救通道，设计文档 §8.6.6）。
 * 合成一个按钮时，第二种场景就没有入口了 —— 而那恰恰是最需要的时候。
 */

import { useEffect, useRef, useState } from "react";
import { AuthUser, SessionInfo, authApi } from "../api";
import { STATUS_LABEL, TIER_LABEL } from "../hooks/useAuth";
import { explain } from "./LoginScreen";

export default function AccountMenu({
  user, onLogout, onReload, compact = false,
  isAdmin = false, inAdminView = false,
  onEnterAdmin, onExitAdmin,
}: {
  user: AuthUser;
  onLogout: (allDevices: boolean) => Promise<void>;
  onReload: () => Promise<unknown>;
  /** 只显示一个圆形头像（用户口径 2026-09-23：顶栏不要身份徽标）。 */
  compact?: boolean;
  isAdmin?: boolean;
  /** 当前是否在系统管理视图（决定菜单里显示"进入"还是"返回"）。 */
  inAdminView?: boolean;
  onEnterAdmin?: () => void;
  onExitAdmin?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState<"" | "sessions" | "password">("");
  const box = useRef<HTMLDivElement | null>(null);

  // 点外面关掉：否则菜单会一直挡着页面（没有遮罩层，很容易被忽略）
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) {
        setOpen(false); setPanel("");
      }
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);

  const label = user.display_name || user.username || user.user_id;
  const tier = TIER_LABEL[user.applied_tier] ?? user.applied_tier;
  const expired = isPast(user.valid_until);

  return (
    <div className="account" ref={box}>
      {/* compact 模式：人头 + 齿轮，**没有任何外形**（无边框、无底色、无胶囊）。
          用户口径 2026-09-23 两次修订合起来是：
            ① 顶栏只留一个很小的用户人头（不要身份徽标）；
            ② 但"只有个人头"太单薄 —— 右侧加一个**齿轮状设置图标**，
               并且**去掉那个椭圆外形**（原话："不要用什么椭圆形状，太丑了，
               直接去掉外形"）。
          齿轮在这里是"这是设置入口"的通用视觉语言（GitHub / Slack 都是
          头像 + 齿轮），而不是第二个独立按钮：整块仍然只有一个点击目标，
          避免在一个 24px 高度里放两个热区导致误点。
          去掉外形后靠 hover/active 的淡底色保留"可点"的反馈 ——
          完全没有反馈的无边框图标会让人以为它只是装饰。 */}
      <button className={compact ? "account-chip bare" : "account-chip"}
        onClick={() => setOpen((v) => !v)}
        title={compact ? `${label}（账户与设置）` : undefined}
        aria-label="账户与设置">
        <span className="account-avatar">
          {compact ? <HeadIcon /> : label.slice(0, 1).toUpperCase()}
        </span>
        {compact ? <GearIcon /> : null}
        {!compact && (
          <>
            <span className="account-name">{label}</span>
            {tier && <span className="account-tier">{tier}</span>}
            <span className="account-caret">▾</span>
          </>
        )}
      </button>

      {open && (
        <div className="account-menu">
          <div className="account-info">
            <div><b>{label}</b></div>
            <div className="account-dim">
              账号：{user.username || "—"}
            </div>
            <div className="account-dim">
              状态：{STATUS_LABEL[user.status] ?? user.status}
              {user.valid_until && (
                <> · 有效期至 {fmt(user.valid_until)}
                  {expired && <span className="account-warn"> 已过期</span>}
                </>
              )}
            </div>
          </div>

          {expired && (
            <div className="error-box account-note">
              账号已过期：可查看，但不能执行写操作。请联系管理员续期。
            </div>
          )}

          {/* ★ 管理员入口：只在头像菜单里（业务视图顶栏不再出现"系统管理"）。
              在系统管理视图时显示"返回业务视图"，反之显示"进入系统管理" ——
              同一个位置承担双向切换，不需要两个按钮。 */}
          {isAdmin && (
            <>
              <div className="account-sep" />
              {inAdminView ? (
                <button className="account-item"
                  onClick={() => { onExitAdmin?.(); setOpen(false); }}>
                  ← 返回业务视图
                </button>
              ) : (
                <button className="account-item"
                  onClick={() => { onEnterAdmin?.(); setOpen(false); }}>
                  系统管理 · 用户管理
                </button>
              )}
              <div className="account-sep" />
            </>
          )}

          <button className="account-item"
            onClick={() => setPanel(panel === "sessions" ? "" : "sessions")}>
            登录设备管理
          </button>
          {panel === "sessions" && (
            <Sessions onReload={onReload} />
          )}

          <button className="account-item"
            onClick={() => setPanel(panel === "password" ? "" : "password")}>
            修改密码
          </button>
          {panel === "password" && <ChangePassword onDone={onReload} />}

          <div className="account-sep" />
          <button className="account-item"
            onClick={() => void onLogout(false)}>
            退出登录（本机）
          </button>
          <button className="account-item danger"
            onClick={() => void onLogout(true)}>
            退出全部设备
          </button>
        </div>
      )}
    </div>
  );
}

/** 圆形头像里的"人头"图标（内联 SVG，`currentColor` 跟随主题）。
 *
 * ⚠️ **不要在 SVG 上写死 width/height**：尺寸统一由 CSS 的 `.acct-icon` 控制。
 * 写死之后每次调节大小都要改 TSX，而且会和相邻的齿轮图标悄悄不一致
 * （用户口径 2026-09-23："这个人头和设置按键要做成 2 倍大"）。
 */
function HeadIcon() {
  return (
    <svg className="acct-icon" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round"
      aria-hidden="true">
      <circle cx="12" cy="8.5" r="3.6" />
      <path d="M4.8 20c0-3.6 3.2-6.2 7.2-6.2s7.2 2.6 7.2 6.2" />
    </svg>
  );
}

/** 齿轮（设置）图标：外圈 + 8 个齿 + 中心孔。
 *
 * 为什么手画而不用现成的齿轮路径：常见的那套（Feather 风格）为了表现
 * 齿形用了很长的复合路径，图标一小就会糊成一团。几何画法
 * （圆环 + 8 条放射短线 + 中心孔）在任何尺寸下都一眼是齿轮，
 * 也和 `HeadIcon` 同一种笔触（圆头描边）。
 */
function GearIcon() {
  // 8 个齿：45° 一个，从外圈 r=6.4 伸到 r=8.4（viewBox 24，中心 12）
  const teeth = Array.from({ length: 8 }, (_, i) => {
    const a = (i * Math.PI) / 4;
    const c = Math.cos(a);
    const s = Math.sin(a);
    return `M${(12 + 6.4 * c).toFixed(2)} ${(12 + 6.4 * s).toFixed(2)}`
      + `L${(12 + 8.4 * c).toFixed(2)} ${(12 + 8.4 * s).toFixed(2)}`;
  }).join(" ");
  return (
    <svg className="acct-icon" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.9" strokeLinecap="round"
      aria-hidden="true">
      <circle cx="12" cy="12" r="6.4" />
      <circle cx="12" cy="12" r="2.8" />
      <path d={teeth} />
    </svg>
  );
}

function Sessions({ onReload }: { onReload: () => Promise<unknown> }) {
  const [items, setItems] = useState<SessionInfo[]>([]);
  const [msg, setMsg] = useState("");

  const load = async () => {
    try {
      const r = await authApi.sessions();
      setItems(r.sessions);
    } catch (e) {
      setMsg(explain(e));
    }
  };
  useEffect(() => { void load(); }, []);

  const kill = async (id: string, current: boolean) => {
    setMsg("");
    try {
      await authApi.killSession(id);
      // 踢掉的是自己当前这台 → 会话已失效，必须让 App 回到登录页，
      // 否则界面还停在"已登录"的样子，用户点任何东西都会失败。
      if (current) { await onReload(); return; }
      await load();
    } catch (e) {
      setMsg(explain(e));
    }
  };

  return (
    <div className="account-sub">
      {msg && <div className="error-box account-note">{msg}</div>}
      {items.length === 0 && <div className="account-dim">没有活跃设备。</div>}
      {items.map((s) => (
        <div key={s.session_id} className="session-row">
          <div>
            <div>
              {s.device_label || "未知设备"}
              {s.current && <span className="account-tier">本机</span>}
              {!s.valid && <span className="account-dim">（已失效）</span>}
            </div>
            <div className="account-dim">
              {s.ip || "—"} · 最近 {fmt(s.last_seen_at)}
            </div>
          </div>
          <button className="account-mini" onClick={() => void kill(s.session_id, s.current)}>
            踢下线
          </button>
        </div>
      ))}
    </div>
  );
}

function ChangePassword({ onDone }: { onDone: () => Promise<unknown> }) {
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true); setErr(""); setMsg("");
    try {
      const r = await authApi.changePassword({
        old_password: oldPwd, new_password: newPwd,
      });
      setMsg(r.message || "密码已修改。其它设备已自动退出登录。");
      setOldPwd(""); setNewPwd("");
      await onDone();
    } catch (e) {
      setErr(explain(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="account-sub">
      {err && <div className="error-box account-note">{err}</div>}
      {msg && <div className="info-box account-note">{msg}</div>}
      <input className="auth-input" type="password" value={oldPwd}
        onChange={(e) => setOldPwd(e.target.value)} placeholder="当前密码" />
      <input className="auth-input" type="password" value={newPwd}
        onChange={(e) => setNewPwd(e.target.value)} placeholder="新密码" />
      <button className="account-mini" disabled={busy || !oldPwd || !newPwd}
        onClick={() => void submit()}>
        {busy ? "提交中…" : "确认修改"}
      </button>
      <p className="auth-hint">
        改密后<b>其它设备全部退出</b>，本机保持登录。
      </p>
    </div>
  );
}

function isPast(iso: string): boolean {
  if (!iso) return false;
  const t = Date.parse(iso);
  return Number.isFinite(t) && t < Date.now();
}

function fmt(iso: string): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const d = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`;
}
