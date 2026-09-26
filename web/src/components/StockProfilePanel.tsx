/**
 * 个股口径编辑（权重 / 阈值 / 档位）—— 每个用户各自一份。
 *
 * 对应后端：`src/api/routes/my_profiles.py`。
 *
 * ## 为什么这个界面是"多用户"最直观的证据
 *
 * 旧表 `dim_intraday_profile` 的主键**只有 `code` 一列**
 * （"一只票全系统一份权重"）。所以以前 A 把 600519 的箱体权重调到 17，
 * B 看到的也是 17 —— 两个人对同一只票不可能有各自的参数。
 *
 * 现在打开同一只票，各人看到的是**自己那份**；没调过就显示
 * "跟随系统默认"。界面上必须有这个区分，否则"跟默认走"与"我调过"
 * 长得一样，用户会以为系统在乱改他的参数。
 *
 * ## 三处刻意的设计
 *
 * 1. **来源常驻显示**：`from_user ? "已自定义" : "跟随系统默认"`，
 *    并显示后端给的 `explain` 文案（前端不复述自己的猜测）。
 * 2. **权重合计提示**：项目口径是"权重合计必须=100，否则总分刻度失去可比性"。
 *    前端实时显示合计并给出颜色警示 —— 但**不阻止保存**
 *    （后端才是权威；前端拦下来会让"我先存一半"变得不可能）。
 * 3. **还原按钮 = 删除我的那份**，不是"把默认值填进去"。
 *    后者会让系统默认值将来升级时再也到不了这个用户。
 */

import { useCallback, useEffect, useState } from "react";
import { StockProfile, profileApi } from "../api";
import { explain } from "./LoginScreen";

const MODES = [
  { value: "intraday", label: "做T（盘中）" },
  { value: "daily", label: "日线" },
];

export default function StockProfilePanel({ code }: { code: string }) {
  const [mode, setMode] = useState("intraday");
  const [profile, setProfile] = useState<StockProfile | null>(null);
  const [weights, setWeights] = useState<Record<string, string>>({});
  const [thresholds, setThresholds] = useState<Record<string, string>>({});
  const [levels, setLevels] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async (m: string) => {
    setError(""); setNotice("");
    try {
      const p = await profileApi.get(code, m);
      setProfile(p);
      setWeights(toText(p.weights));
      setThresholds(toText(p.thresholds));
      setLevels(toText(p.levels));
    } catch (e) {
      setError(explain(e));
    }
  }, [code]);

  useEffect(() => { void load(mode); }, [load, mode]);

  const total = sumNumbers(weights);
  const totalOk = Math.abs(total - 100) < 0.001;

  const save = async () => {
    setBusy(true); setError(""); setNotice("");
    try {
      const r = await profileApi.save(code, {
        mode,
        weights: toNumbers(weights),
        thresholds: toNumbers(thresholds),
        levels: toNumbers(levels),
        boards: profile?.boards ?? [],
        overseas: profile?.overseas ?? [],
      });
      setProfile(r.profile);
      setNotice(r.message || "已保存");
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (!window.confirm(
      "还原为系统默认？\n\n"
      + "这会删除你自己保存的这份口径，之后跟随系统默认值。\n"
      + "（系统默认值将来升级时，你会自动跟上。）")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const r = await profileApi.reset(code, mode);
      setNotice(r.message);
      await load(mode);
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  };

  const addField = (
    setter: React.Dispatch<React.SetStateAction<Record<string, string>>>,
    current: Record<string, string>, label: string,
  ) => {
    const key = window.prompt(`新增${label}的键名（如 box / chan / action）：`);
    if (!key || !key.trim()) return;
    if (current[key.trim()] !== undefined) {
      setError(`键 ${key.trim()} 已存在`);
      return;
    }
    setter({ ...current, [key.trim()]: "0" });
  };

  const removeField = (
    setter: React.Dispatch<React.SetStateAction<Record<string, string>>>,
    current: Record<string, string>, key: string,
  ) => {
    const next = { ...current };
    delete next[key];
    setter(next);
  };

  return (
    <div className="profile-panel">
      <div className="profile-head">
        <h3>个股口径 · {code}</h3>
        <select className="auth-input" value={mode}
          onChange={(e) => setMode(e.target.value)}>
          {MODES.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </select>
        {profile && (
          <span className={profile.from_user
            ? "admin-badge s-active" : "admin-badge"}>
            {profile.from_user ? "已自定义" : "跟随系统默认"}
          </span>
        )}
      </div>

      {profile?.explain && (
        <p className="auth-hint">{profile.explain}</p>
      )}
      {error && <div className="error-box">{error}</div>}
      {notice && <div className="info-box">{notice}</div>}

      <section className="profile-block">
        <div className="profile-block-head">
          <h4>因子权重</h4>
          <span className={totalOk ? "profile-total ok" : "profile-total warn"}>
            合计 {total.toFixed(1)}
            {totalOk ? "（符合 100）" : "（项目口径要求合计=100）"}
          </span>
          <button className="account-mini" disabled={busy}
            onClick={() => addField(setWeights, weights, "权重")}>
            + 新增因子
          </button>
        </div>
        <div className="profile-grid">
          {Object.entries(weights).map(([key, value]) => (
            <label key={key} className="profile-field">
              <span className="profile-key">{key}</span>
              <input className="auth-input" type="number" step="0.5"
                value={value}
                onChange={(e) => setWeights(
                  { ...weights, [key]: e.target.value })} />
              <button className="profile-del" title="删除该因子"
                onClick={() => removeField(setWeights, weights, key)}>×</button>
            </label>
          ))}
          {Object.keys(weights).length === 0 && (
            <div className="account-dim">
              没有权重项。点「+ 新增因子」添加（没配过时这里是空的，
              表示跟随系统默认）。
            </div>
          )}
        </div>
      </section>

      <section className="profile-block">
        <div className="profile-block-head">
          <h4>阈值</h4>
          <button className="account-mini" disabled={busy}
            onClick={() => addField(setThresholds, thresholds, "阈值")}>
            + 新增阈值
          </button>
        </div>
        <div className="profile-grid">
          {Object.entries(thresholds).map(([key, value]) => (
            <label key={key} className="profile-field">
              <span className="profile-key">{key}</span>
              <input className="auth-input" type="number" step="0.5"
                value={value}
                onChange={(e) => setThresholds(
                  { ...thresholds, [key]: e.target.value })} />
              <button className="profile-del" title="删除该阈值"
                onClick={() => removeField(setThresholds, thresholds, key)}>
                ×
              </button>
            </label>
          ))}
          {Object.keys(thresholds).length === 0 && (
            <div className="account-dim">没有阈值项。</div>
          )}
        </div>
      </section>

      <section className="profile-block">
        <div className="profile-block-head">
          <h4>档位 / 参数</h4>
          <button className="account-mini" disabled={busy}
            onClick={() => addField(setLevels, levels, "档位")}>
            + 新增档位
          </button>
        </div>
        <div className="profile-grid">
          {Object.entries(levels).map(([key, value]) => (
            <label key={key} className="profile-field">
              <span className="profile-key">{key}</span>
              <input className="auth-input" type="number" step="0.1"
                value={value}
                onChange={(e) => setLevels({ ...levels, [key]: e.target.value })} />
              <button className="profile-del" title="删除该档位"
                onClick={() => removeField(setLevels, levels, key)}>×</button>
            </label>
          ))}
          {Object.keys(levels).length === 0 && (
            <div className="account-dim">没有档位参数。</div>
          )}
        </div>
      </section>

      {profile && (
        <p className="auth-hint">
          口径指纹 <code>{profile.caliber_key}</code>：
          只由「模式 + 权重 + 阈值 + 档位」决定。
          两人口径相同就能**共用同一次计算**，不同则各自计算 ——
          这就是"计算可共享、参数不共享"的落地方式。
          {profile.updated_at && ` · 上次修改 ${profile.updated_at.slice(0, 16)}`}
        </p>
      )}

      <div className="profile-actions">
        <button className="auth-submit" disabled={busy} onClick={() => void save()}>
          {busy ? "保存中…" : "保存我的口径"}
        </button>
        <button className="account-mini danger" disabled={busy}
          onClick={() => void reset()}>
          还原系统默认
        </button>
      </div>
    </div>
  );
}

function toText(src: Record<string, number>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(src ?? {})) out[k] = String(v);
  return out;
}

function toNumbers(src: Record<string, string>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(src)) {
    const n = Number(v);
    // 空串/非法输入不提交（后端也会拒 NaN，但前端先挡一层更友好）
    if (v.trim() !== "" && Number.isFinite(n)) out[k] = n;
  }
  return out;
}

function sumNumbers(src: Record<string, string>): number {
  return Object.values(src).reduce((acc, v) => {
    const n = Number(v);
    return acc + (Number.isFinite(n) ? n : 0);
  }, 0);
}
