import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type StockEntry } from "../api";

/**
 * 股票选择器：**代码 / 中文名 / 拼音首字母 / 全拼** 四路联动联想。
 *
 * ## 为什么所有输入股票的地方都要换掉
 *
 * 项目里所有股票输入框原来都是裸 `<input maxLength={6}>`，只收 6 位数字。
 * 后果有两类：
 *  1. **认不出是哪只票**：做T自选股里出现过一排 `603083 / 600150` ——
 *     名称就是代码，界面上完全看不出是什么公司；
 *  2. **只能靠记忆**：想加"剑桥科技"必须先自己查出代码是 603083。
 *
 * 联想数据来自本地字典（5562 只 A 股 + 拼音，离线可查），
 * 支持 `jqkj`、`PAYH`、`平安`、`603083` 任意一种输入方式。
 *
 * `onPick` 在用户**确认选择**时回调完整条目；`onChange` 在每次输入时回调，
 * 用于兼容"用户就想手打 6 位代码"的场景。
 */
export function StockPicker({
  value, onChange, onPick, disabled = false, placeholder,
  width = 220, autoFocus = false,
}: {
  value: string;
  onChange: (next: string) => void;
  onPick?: (entry: StockEntry) => void;
  disabled?: boolean;
  placeholder?: string;
  width?: number;
  autoFocus?: boolean;
}) {
  const [options, setOptions] = useState<StockEntry[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [resolved, setResolved] = useState<StockEntry | null>(null);
  const [loading, setLoading] = useState(false);
  // 中文输入法组字中（拼音串还没上屏）：此时**不能**发查询
  const [composing, setComposing] = useState(false);
  // 用户是否已从联想列表里选过（决定输入框右侧显示名称还是"未找到该代码"）
  const [picked, setPicked] = useState(false);
  /** 结果对应的查询词：用于识别"列表内容与当前输入不匹配"（竞态兜底展示） */
  const [resultFor, setResultFor] = useState("");
  const timerRef = useRef<number | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);
  /**
   * 请求序号：**丢弃过期响应**。
   *
   * 实测用户报障："输入『日联科技』，下拉却出现『金融街/捷荣技术』"——
   * 那是典型的竞态：连续输入会发出多个请求（180ms 防抖 + 打字间隔），
   * 若**先发的请求后返回**，它会把已经正确的结果覆盖掉。
   * 这里只认"最后一次发出的请求"，其余响应直接丢弃。
   */
  const querySeq = useRef(0);

  // 输入变化 → 防抖查询（180ms：打字过程中不打接口，停一下才查）
  const query = useCallback((text: string) => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
    if (!text.trim()) {
      querySeq.current += 1;          // 清空也要作废在途请求，否则旧结果会把空列表填回来
      setOptions([]);
      setOpen(false);
      return;
    }
    const seq = ++querySeq.current;
    timerRef.current = window.setTimeout(async () => {
      setLoading(true);
      try {
        const data = await api.stockSearch(text.trim(), 12);
        if (seq !== querySeq.current) return;      // 过期响应：丢弃，不覆盖新结果
        setOptions(data.stocks);
        setResultFor(text.trim());
        setOpen(data.stocks.length > 0);
        setActive(0);
      } catch {
        if (seq === querySeq.current) setOptions([]);
      } finally {
        if (seq === querySeq.current) setLoading(false);
      }
    }, 180);
  }, []);

  useEffect(() => () => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
  }, []);

  // 输入 6 位数字后自动解析名称（用户直接手打代码时也能看到是哪只票）
  useEffect(() => {
    let alive = true;
    if (!/^\d{6}$/.test(value)) {
      setResolved(null);
      return () => { alive = false; };
    }
    api.stockDetail(value)
      .then((data) => { if (alive) setResolved(data.stock); })
      .catch(() => { if (alive) setResolved(null); });
    return () => { alive = false; };
  }, [value]);

  useEffect(() => {
    const onClickOutside = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  const pick = (entry: StockEntry) => {
    onChange(entry.code);
    setResolved(entry);
    setPicked(true);
    onPick?.(entry);
    setOpen(false);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (!open || !options.length) {
      if (event.key === "Enter" && /^\d{6}$/.test(value)) {
        event.preventDefault();
      }
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => (index + 1) % options.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (index - 1 + options.length) % options.length);
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      pick(options[active]);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  };

  const suffix = useMemo(() => {
    if (loading) return "查询中…";
    if (resolved) return resolved.name;
    if (composing) return "输入中…";
    // 只在"用户刚选过/输入的是代码"时提示未找到；拼音/中文输到一半时不该报错
    if (value && /^\d{6}$/.test(value)) return "未找到该代码";
    if (value && picked) return "";
    return "";
  }, [loading, resolved, composing, picked, value]);

  return (
    <div className="stock-picker" ref={boxRef} style={{ width }}>
      <input
        className="target-input"
        value={value}
        autoFocus={autoFocus}
        disabled={disabled}
        maxLength={12}
        spellCheck={false}
        placeholder={placeholder ?? "代码 / 拼音首字母 / 中文名"}
        onChange={(event) => {
          const next = event.target.value.trim();
          if (picked) setPicked(false);
          onChange(next);
          // 输入法组字过程中查出来的结果是"半成品拼音"的，会闪且浪费请求；
          // 等 compositionend（上屏）再查一次即可。
          if (composing) return;
          query(next);
        }}
        onCompositionStart={() => setComposing(true)}
        onCompositionEnd={(event) => {
          setComposing(false);
          const next = (event.target as HTMLInputElement).value.trim();
          onChange(next);
          query(next);
        }}
        onFocus={() => { if (options.length) setOpen(true); }}
        onKeyDown={handleKeyDown}
      />
      <span className={`stock-picker-name ${resolved ? "" : "muted-text"}`}>
        {suffix}
      </span>
      {open && options.length > 0 && (
        <ul className="suggest-list stock-suggest">
          {/* 兜底提示：仅当结果对应的是**上一次**查询词时才可能出现（竞态已被
              序号守卫挡掉，这里保留一层可见的说明，避免用户误以为搜错了） */}
          {resultFor && value.trim() && resultFor !== value.trim() && (
            <li className="muted-text suggest-stale">
              以下是「{resultFor}」的结果，正在查询「{value.trim()}」…
            </li>
          )}
          {options.map((item, index) => (
            <li key={item.code}
                className={index === active ? "active" : ""}
                onMouseDown={(event) => {
                  event.preventDefault();
                  pick(item);
                }}>
              <span className="mono suggest-code">{item.code}</span>
              <span className="suggest-name">{item.name}</span>
              <span className="mono muted-text suggest-py">
                {item.pinyin_initials}
              </span>
              <span className="badge badge-muted suggest-kind">
                {item.instrument_type}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
