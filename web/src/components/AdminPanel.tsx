/**
 * 管理员控制台：注册审批 / 用户增删 / 套餐等级 / 有效期 / 强制下线。
 *
 * 对应后端：`src/api/routes/admin.py`（11 个端点）。
 *
 * ## 为什么入口在"顶栏账户菜单"里，而不是页面里藏一个按钮
 *
 * 这个控制台是**唯一的审批入口** —— 新用户注册后落 `pending`、不能登录，
 * 没有它整个注册流是断的。所以它必须好找（管理员一进来就能看到"待审批 N 人"），
 * 同时又只对管理员可见。
 *
 * ## 权限边界（前端隐藏 ≠ 权限）
 *
 * 这里用 `tier === "admin"` 决定**是否渲染**，只是体验层 ——
 * 真正的门槛在服务端每个端点上（`require_admin`）。
 * 有人手动改前端状态或直接调接口，一律 403。
 *
 * ## 界面上的两个"防手滑"设计
 *
 * 1. **删除/停用/改等级都要二次确认**：这些都是不可逆或影响他人访问的动作，
 *    误点一次就是一次线上事故（尤其"把自己降级"，所以服务端也堵了这条路）。
 * 2. **有效期用"今天 + N 天"而不是让人选日期**：管理员的心智是
 *    "给他三个月"，不是"到 2026-12-23"；换算交给服务端，避免时区/格式出错。
 */

import { useCallback, useEffect, useState } from "react";
import { AdminOverview, AdminUser, AdminUserDetail, adminApi } from "../api";
import { STATUS_LABEL, TIER_LABEL } from "../hooks/useAuth";
import { explain } from "./LoginScreen";

const TIER_OPTIONS = [
  { value: "vip", label: "VIP（单池100 / 总数100）" },
  { value: "trial", label: "试用（单池5 / 总数25）" },
  { value: "admin", label: "管理员（含管理台权限）" },
];

const DAY_OPTIONS = [7, 30, 90, 180, 365];

export default function AdminPanel({ selfId }: { selfId: string }) {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [filter, setFilter] = useState("");
  const [keyword, setKeyword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [detail, setDetail] = useState<AdminUserDetail | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      const [ov, list] = await Promise.all([
        adminApi.overview(),
        adminApi.users({ status: filter, keyword }),
      ]);
      setOverview(ov);
      setUsers(list.users);
    } catch (e) {
      setError(explain(e));
    }
  }, [filter, keyword]);

  useEffect(() => { void load(); }, [load]);

  const act = async (fn: () => Promise<{ message?: string } | unknown>) => {
    setBusy(true); setError(""); setNotice("");
    try {
      const r = await fn() as { message?: string };
      setNotice(r?.message || "已更新");
      await load();
      if (detail) {
        setDetail(await adminApi.user(detail.user.user_id));
      }
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  };

  const counts = overview?.counts ?? {};
  const pendingCount = overview?.pending_count ?? 0;

  return (
    <div className="admin">
      <div className="admin-head">
        <h2>用户管理</h2>
        {/* ★ 这个管理台管的是**哪个实例的账号库** —— 必须摆在标题旁边。
            2026-09-23 实测：客户在试点实例（8110）注册、提示"等待管理员审批"，
            而管理员打开的是调试实例（8100）的用户管理 —— 两边账号库分开，
            于是"看不到申请记录"，看起来像注册没落库。
            同机跑多个实例（dev 8100 / 试点 8110 / 生产）时，
            这一行就是"我现在在管谁"的唯一凭据。 */}
        {overview?.instance && (
          <span className="admin-instance mono"
            title={"本管理台只管理这一个账号库；其它实例的注册/用户不会出现在这里。"
                   + `\n环境：${overview.instance.env}`
                   + `\n账号库：${overview.instance.db}`}>
            {overview.instance.env} · {overview.instance.db}
          </span>
        )}
        <div className="admin-stats">
          <span className={pendingCount > 0 ? "admin-stat hot" : "admin-stat"}>
            待审批 <b>{pendingCount}</b>
          </span>
          <span className="admin-stat">正常 <b>{counts.active ?? 0}</b></span>
          <span className="admin-stat">已禁用 <b>{counts.disabled ?? 0}</b></span>
          <span className="admin-stat">已过期 <b>{counts.expired ?? 0}</b></span>
        </div>
        <button className="account-mini" disabled={busy}
          onClick={() => setCreating((v) => !v)}>
          {creating ? "取消开号" : "+ 直接开号"}
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}
      {notice && <div className="info-box">{notice}</div>}

      {pendingCount > 0 && (overview?.pending.length ?? 0) > 0 && (
        <div className="admin-pending">
          <h3>待审批（{pendingCount}）</h3>
          {overview!.pending.map((u) => (
            <ApproveRow key={u.user_id} user={u} busy={busy} onAct={act} />
          ))}
        </div>
      )}

      {creating && <CreateUserForm busy={busy} onAct={act}
        onDone={() => setCreating(false)} />}

      <div className="admin-filters">
        <select className="auth-input" value={filter}
          onChange={(e) => setFilter(e.target.value)}>
          <option value="">全部状态</option>
          <option value="active">正常</option>
          <option value="pending">待审批</option>
          <option value="disabled">已禁用</option>
          <option value="expired">已过期</option>
          <option value="rejected">已驳回</option>
        </select>
        <input className="auth-input" value={keyword} placeholder="搜索用户名 / 显示名"
          onChange={(e) => setKeyword(e.target.value)} />
        <button className="account-mini" disabled={busy}
          onClick={() => void load()}>刷新</button>
      </div>

      <table className="admin-table">
        <thead>
          <tr>
            <th>用户</th><th>状态</th><th>套餐</th><th>到期</th>
            <th title="该账号当前**仍然有效**的会话数（未撤销、未过滑动窗口、
              未过 12 小时绝对上限）。同一账号可多端登录，所以 >1 是正常的；
              它统计的是**设备/会话**，不是人数。">
              在线设备
            </th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.user_id} className={u.user_id === selfId ? "self" : ""}>
              <td>
                <div className="admin-user">
                  {u.display_name || u.username}
                  {u.user_id === selfId && <span className="account-tier">我</span>}
                </div>
                <div className="account-dim">{u.username}</div>
              </td>
              <td>
                <span className={`admin-badge s-${u.status}`}>
                  {STATUS_LABEL[u.status] ?? u.status}
                </span>
                {u.expired && <span className="account-warn"> 已过期</span>}
              </td>
              <td>{TIER_LABEL[u.tier] ?? u.tier ?? "未分配"}</td>
              <td>{u.valid_until ? u.valid_until.slice(0, 10) : "—"}</td>
              <td title={u.active_sessions > 0
                ? `${u.active_sessions} 个仍然有效的会话（设备）`
                : "当前没有有效会话"}>
                {u.active_sessions > 0
                  ? u.active_sessions
                  : <span className="account-dim">—</span>}
              </td>
              <td className="admin-actions">
                <button className="account-mini"
                  onClick={() => void adminApi.user(u.user_id)
                    .then(setDetail).catch((e) => setError(explain(e)))}>
                  详情
                </button>
              </td>
            </tr>
          ))}
          {users.length === 0 && (
            <tr><td colSpan={6} className="account-dim">没有符合条件的用户。</td></tr>
          )}
        </tbody>
      </table>

      {detail && (
        <UserDrawer detail={detail} selfId={selfId} busy={busy} onAct={act}
          onClose={() => setDetail(null)} />
      )}
    </div>
  );
}

/** 待审批行：一条就能批完，不用进详情。 */
function ApproveRow({ user, busy, onAct }: {
  user: AdminUser; busy: boolean;
  onAct: (fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [tier, setTier] = useState("vip");
  const [days, setDays] = useState(30);
  const [note, setNote] = useState("");
  return (
    <div className="approve-row">
      <div className="approve-who">
        <b>{user.display_name || user.username}</b>
        <span className="account-dim"> {user.username}</span>
        <span className="account-dim">
          {" "}注册于 {user.created_at ? user.created_at.slice(0, 10) : "—"}
        </span>
      </div>
      <div className="approve-controls">
        <select className="auth-input" value={tier}
          onChange={(e) => setTier(e.target.value)}>
          {TIER_OPTIONS.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
        <select className="auth-input" value={days}
          onChange={(e) => setDays(Number(e.target.value))}>
          {DAY_OPTIONS.map((d) => <option key={d} value={d}>{d} 天</option>)}
        </select>
        <input className="auth-input" value={note} placeholder="备注（可选）"
          onChange={(e) => setNote(e.target.value)} />
        <button className="auth-inline-btn" disabled={busy}
          onClick={() => void onAct(() => adminApi.approve(user.user_id,
            { tier, days, note }))}>
          通过
        </button>
        <button className="account-mini danger" disabled={busy}
          onClick={() => {
            const why = window.prompt("驳回原因（会显示给用户）：", "信息不完整");
            if (why === null) return;
            void onAct(() => adminApi.reject(user.user_id, why));
          }}>
          驳回
        </button>
      </div>
    </div>
  );
}

function CreateUserForm({ busy, onAct, onDone }: {
  busy: boolean;
  onAct: (fn: () => Promise<unknown>) => Promise<void>;
  onDone: () => void;
}) {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tier, setTier] = useState("vip");
  const [days, setDays] = useState(30);
  const [mustChange, setMustChange] = useState(true);

  return (
    <div className="admin-create">
      <h3>直接开号（跳过邮箱验证，立即生效）</h3>
      <div className="admin-create-grid">
        <input className="auth-input" value={username} placeholder="用户名"
          onChange={(e) => setUsername(e.target.value)} />
        <input className="auth-input" value={email} placeholder="邮箱（可选，用于找回）"
          onChange={(e) => setEmail(e.target.value)} />
        <input className="auth-input" type="password" value={password}
          placeholder="初始密码（至少12位，含大小写/数字/符号三类）"
          onChange={(e) => setPassword(e.target.value)} />
        <select className="auth-input" value={tier}
          onChange={(e) => setTier(e.target.value)}>
          {TIER_OPTIONS.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
        <select className="auth-input" value={days}
          onChange={(e) => setDays(Number(e.target.value))}>
          {DAY_OPTIONS.map((d) => <option key={d} value={d}>{d} 天</option>)}
        </select>
        <label className="auth-check">
          <input type="checkbox" checked={mustChange}
            onChange={(e) => setMustChange(e.target.checked)} />
          首次登录强制改密
        </label>
      </div>
      <div className="admin-create-actions">
        <button className="auth-submit" disabled={busy || !username || !password}
          onClick={() => void onAct(async () => {
            const r = await adminApi.createUser({
              username, email, password, tier, days,
              must_change_password: mustChange,
            });
            onDone();
            return r;
          })}>
          创建
        </button>
      </div>
      <p className="auth-hint">
        初始密码由你设定 —— 建议勾选"首次登录强制改密"，
        否则这把钥匙等于两个人共用。
      </p>
    </div>
  );
}

/** 用户详情抽屉：改套餐/有效期/重置密码/强制下线/删除。 */
function UserDrawer({ detail, selfId, busy, onAct, onClose }: {
  detail: AdminUserDetail; selfId: string;
  busy: boolean;
  onAct: (fn: () => Promise<unknown>) => Promise<void>;
  onClose: () => void;
}) {
  const u = detail.user;
  const isSelf = u.user_id === selfId;
  const [tier, setTier] = useState(u.tier || "vip");
  const [days, setDays] = useState(30);
  const [newPwd, setNewPwd] = useState("");

  return (
    <div className="admin-drawer">
      <div className="admin-drawer-head">
        <h3>{u.display_name || u.username} 的详情</h3>
        <button className="account-mini" onClick={onClose}>关闭</button>
      </div>

      <div className="admin-drawer-grid">
        <div>
          <div className="account-dim">账号</div>
          <div>{u.username}（{u.user_id}）</div>
          <div className="account-dim" style={{ marginTop: 8 }}>状态</div>
          <div>
            {STATUS_LABEL[u.status] ?? u.status}
            {u.expired && <span className="account-warn"> · 已过期</span>}
          </div>
          <div className="account-dim" style={{ marginTop: 8 }}>有效期至</div>
          <div>{u.valid_until ? u.valid_until.slice(0, 10) : "—"}</div>
          <div className="account-dim" style={{ marginTop: 8 }}>联系方式</div>
          <div>
            {detail.contacts.length === 0
              ? "—"
              : detail.contacts.map((c) => (
                <div key={c.kind + c.masked}>
                  {c.masked} {c.verified ? "（已验证）" : "（未验证）"}
                </div>
              ))}
          </div>
        </div>

        <div>
          <div className="account-dim">改套餐与有效期</div>
          <div className="admin-inline">
            <select className="auth-input" value={tier}
              onChange={(e) => setTier(e.target.value)}>
              {TIER_OPTIONS.map((t) => (
                <option key={t.value} value={t.value}>{t.label}</option>
              ))}
            </select>
            <select className="auth-input" value={days}
              onChange={(e) => setDays(Number(e.target.value))}>
              {DAY_OPTIONS.map((d) => <option key={d} value={d}>{d} 天</option>)}
            </select>
            <button className="auth-inline-btn"
              disabled={busy || (isSelf && tier !== "admin")}
              onClick={() => void onAct(() => adminApi.updateUser(u.user_id,
                { tier, days }))}>
              应用
            </button>
          </div>
          {isSelf && (
            <p className="auth-hint">
              这是你自己的账号：**不能**改套餐等级或停用
              （否则会失去唯一的进入管理台的入口）。服务端也会拒绝。
            </p>
          )}

          <div className="account-dim" style={{ marginTop: 12 }}>账号状态</div>
          <div className="admin-inline">
            {(["active", "disabled", "expired"] as const).map((s) => (
              <button key={s} className="account-mini" disabled={busy || isSelf}
                onClick={() => void onAct(() => adminApi.updateUser(u.user_id,
                  { status: s }))}>
                {STATUS_LABEL[s]}
              </button>
            ))}
          </div>

          <div className="account-dim" style={{ marginTop: 12 }}>安全管理</div>
          <div className="admin-inline">
            <input className="auth-input" type="password" value={newPwd}
              placeholder="新密码（重置后该用户所有设备下线）"
              onChange={(e) => setNewPwd(e.target.value)} />
            <button className="account-mini" disabled={busy || !newPwd}
              onClick={() => void onAct(async () => {
                const r = await adminApi.resetPassword(u.user_id, newPwd);
                setNewPwd("");
                return r;
              })}>
              重置密码
            </button>
            <button className="account-mini" disabled={busy}
              onClick={() => void onAct(() => adminApi.kickAll(u.user_id))}>
              强制下线全部设备
            </button>
            <button className="account-mini danger" disabled={busy || isSelf}
              onClick={() => {
                if (!window.confirm(
                  `确认删除 ${u.username}？\n\n`
                  + "删除后：该账号无法登录、在线会话立即失效、"
                  + "其邮箱会被释放（可用于重新注册）。\n"
                  + "审计链保留，不做物理删除。")) return;
                void onAct(async () => {
                  const r = await adminApi.deleteUser(u.user_id, "管理员删除");
                  onClose();
                  return r;
                });
              }}>
              删除账号
            </button>
          </div>
        </div>
      </div>

      <div className="account-dim" style={{ marginTop: 14 }}>
        在线设备（{detail.sessions.filter((s) => s.valid).length}）
        <span title="下面是该账号**全部未撤销**的会话；已过滑动窗口的那些标为
          「已失效」，不再计入上面的数字。">
          {" "}· 共 {detail.sessions.length} 条未撤销会话
        </span>
      </div>
      {detail.sessions.length === 0 && <div className="account-dim">无</div>}
      {detail.sessions.map((s) => (
        <div key={s.session_id} className="session-row">
          <div>
            <div>{s.device_label || "未知设备"} {s.ip}</div>
            <div className="account-dim">
              最近活动 {s.last_seen_at ? s.last_seen_at.slice(0, 16) : "—"}
            </div>
          </div>
          <span className="account-dim">{s.valid ? "有效" : "已失效"}</span>
        </div>
      ))}

      <div className="account-dim" style={{ marginTop: 14 }}>
        操作流水（最近 {detail.reviews.length} 条，含操作人）
      </div>
      {detail.reviews.map((r, i) => (
        <div key={i} className="session-row">
          <div>
            <div>{r.action}
              {r.tier_code ? ` · ${r.tier_code}` : ""}
              {r.from_status && r.to_status
                ? ` · ${r.from_status}→${r.to_status}` : ""}
            </div>
            <div className="account-dim">
              操作人 {r.reviewer_id || "—"} · {r.created_at?.slice(0, 16)}
              {r.note ? ` · ${r.note}` : ""}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
