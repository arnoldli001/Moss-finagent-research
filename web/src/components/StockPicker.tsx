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
  const timerRef = useRef<number | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);

  // 输入变化 → 防抖查询（180ms：打字过程中不打接口，停一下才查）
  const query = useCallback((text: string) => {
    if (timerRef.current) window.clearTimeout(timerRef.current);
    if (!text.trim()) {
      setOptions([]);
      setOpen(false);
      return;
    }
    timerRef.current = window.setTimeout(async () => {
      setLoading(true);
      try {
        const data = await api.stockSearch(text.trim(), 12);
        setOptions(data.stocks);
        setOpen(data.stocks.length > 0);
        setActive(0);
      } catch {
        setOptions([]);
      } finally {
        setLoading(false);
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
    if (value && !/^\d{6}$/.test(value)) return "";
    if (value) return "未找到该代码";
    return "";
  }, [loading, resolved, value]);

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
