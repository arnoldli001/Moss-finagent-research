/**
 * 真人检测（图形验证码）组件。
 *
 * 对应后端：`GET /api/v1/auth/captcha`（签发）+ 各接口的 `captcha_token` /
 * `captcha_answer` 字段（校验）。
 *
 * ## 为什么把它做成独立组件

    登录 / 注册 / 找回密码**三处**都要用它。如果各自内联一份，
    会立刻出现三种不一致：有的能点图换一张、有的不刷新令牌、
    有的忘了把答案一起提交（那会一直报"验证码不正确"，而用户明明填对了）。
    集中一处，只有一种行为。
 *
 * ## 三个刻意的设计

 * 1. **点图片换一张**：用户看不清时必须能换 —— 否则他会卡在这里放弃。
 *    换图必须**同时换令牌**（旧令牌是一次性的、已经绑了旧答案）。
 * 2. **`debug_answer` 只在 dev 出现**：本地调试时直接把答案填进输入框，
 *    省掉"辨认扭曲文字"这件与调试无关的事。生产端返回空串。
 * 3. **`onChange` 回调带令牌**：让父组件拿到"当前有效的那一对
 *    （令牌 + 答案）"，而不是让父组件自己再发一次请求取令牌
 *    （那会与这里显示的图不是同一张）。
 */

import { useCallback, useEffect, useState } from "react";
import { CaptchaChallenge, authApi } from "../api";
import { explain } from "./LoginScreen";

export type CaptchaValue = {
  token: string;
  answer: string;
  /** 是否已填写（父组件据此决定按钮可用性） */
  ready: boolean;
};

export default function HumanCheck({
  value, onChange, autoFocus = false,
}: {
  value: CaptchaValue;
  onChange: (next: CaptchaValue) => void;
  autoFocus?: boolean;
}) {
  const [challenge, setChallenge] = useState<CaptchaChallenge | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const c = await authApi.captcha();
      setChallenge(c);
      // 换图**必须**同时换令牌并清空答案：旧令牌是一次性的
      // （已绑定旧答案），复用只会得到"验证码不正确"。
      onChange({ token: c.captcha_token, answer: "", ready: false });
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  }, [onChange]);

  useEffect(() => { void refresh(); }, []); // 仅挂载时领一张

  const setAnswer = (answer: string) => {
    onChange({ token: challenge?.captcha_token ?? "", answer,
               ready: Boolean(answer.trim()) });
  };

  return (
    <div className="human-check">
      <label className="auth-label">
        真人检测
        {challenge?.meta?.alphabet_note && (
          <span className="human-hint">（{challenge.meta.alphabet_note}）</span>
        )}
      </label>
      <div className="human-row">
        <button type="button" className="human-image" disabled={busy}
          onClick={() => void refresh()}
          title={challenge?.meta?.hint || "点击换一张"}>
          {challenge ? (
            <img src={challenge.image_png} alt="图形验证码" width={168}
              height={52} />
          ) : (
            <span className="human-placeholder">
              {busy ? "加载中…" : "点击获取"}
            </span>
          )}
        </button>
        <input className="auth-input human-input" value={value.answer}
          autoFocus={autoFocus} maxLength={8} inputMode="text"
          autoComplete="off" spellCheck={false}
          placeholder={`${challenge?.meta?.length ?? 4} 位字符`}
          onChange={(e) => setAnswer(e.target.value)} />
        <button type="button" className="account-mini" disabled={busy}
          onClick={() => void refresh()}>
          换一张
        </button>
      </div>
      {challenge?.debug_answer && (
        <p className="auth-hint">
          （开发模式：答案是 <code>{challenge.debug_answer}</code>）
          <button type="button" className="auth-copy"
            onClick={() => setAnswer(challenge.debug_answer)}>
            填入
          </button>
        </p>
      )}
      {error && <div className="error-box auth-msg">{error}</div>}
    </div>
  );
}
