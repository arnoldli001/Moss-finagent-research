/** 前端报错文案层 —— 与后端 `src/api/error_codes.py` 同一套分层报错码。
 *
 * ## 分层（码前缀 = 层，与后端一致）
 *
 * | 前缀 | 层 |
 * |---|---|
 * | `SYS_` | 系统层 | `AUTH_` | 认证与权限层 | `REQ_` | 请求与参数层 |
 * | `DATA_` | 数据源层 | `ANA_` | 分析链路层 | `TRADE_` | 交易功能层 |
 * | `EVT_` | 事件告警层 | `NET_` | 网络层（仅前端本地生成） |
 *
 * ## 原则
 *
 * 1. 用户可见的报错**永远来自这张表或后端 envelope 的 message**，
 *    绝不直接显示后端响应体原文（可能含路径/URL/堆栈片段）；
 * 2. 运维指令（`manage.py start` 之类）**只在 localhost 出现** ——
 *    公网试点用户看到"请执行 python manage.py ..."既没用又泄露内部结构；
 * 3. 表里没收录的码：回退到后端 envelope 的 message/detail，
 *    再没有才是 HTTP 状态兜底文案。
 */

/** 后端错误响应的统一结构（见后端全局异常处理器）。 */
export interface ErrorEnvelope {
  detail?: string | { code?: string; message?: string; cause?: string };
  code?: string;
  trace_id?: string;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly traceId?: string;

  constructor(message: string, code: string, status: number,
              traceId?: string) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.traceId = traceId;
  }
}

/** 是否本机开发环境 —— 只有它才显示运维提示（启动命令、端口、cause）。 */
export function isLocalDevHost(): boolean {
  const h = window.location.hostname;
  return h === "localhost" || h === "127.0.0.1" || h === "::1";
}

// --------------------------------------------------------------------------
// 分层文案表：码 → 展示文案。后端 message 已经很友好时不必重复登记，
// 只在"要给用户更具体的下一步指引"时才加。
// --------------------------------------------------------------------------

const COPY: Record<string, string> = {
  // SYS_ 系统层
  SYS_5000: "服务器开了点小差，请稍后重试",
  SYS_5030: "服务暂不可用，可能正在重启，请稍后重试",
  SYS_5100: "服务尚未就绪，请稍后重试",
  // AUTH_ 认证与权限层
  AUTH_4010: "登录状态已失效，请重新登录",
  AUTH_4030: "当前账号没有该操作的权限",
  AUTH_4290: "操作太频繁了，请稍等片刻再试",
  // REQ_ 请求与参数层
  REQ_4000: "请求参数有误，请检查输入",
  REQ_4040: "请求的内容不存在或已被清理",
  REQ_4090: "当前状态不允许该操作",
  REQ_4220: "提交的内容未通过校验，请检查后重试",
  // ── 情报流「点击看全文」的两个成因（2026-10-01 第六轮）──
  //
  // ⚠️ 这两条**必须分开**：用户报障时看到的是"全文读取失败，请稍后重试"，
  // 而真实情况是"这条超出 3 天留存窗口"—— **重试一万次也不会好**。
  // 把它们合成一条 404 兜底文案，等于让用户对着一个永远不会成功的按钮
  // 反复点下去，并且一直以为是后端坏了。
  //
  // 后端侧见 `src/api/routes/intel.py` 的 `intel_item`：
  //   item_not_retained  存储里没有这一条（没采到 / 未落库 / 已过期）
  //   item_bad_hash      指纹编号无效（空串 / 超长垃圾串）
  // 两者都是**终态**，文案里不出现"稍后重试"。
  item_not_retained: "这条全文未留存（或已超出 3 天留存窗口），无法再读取",
  item_bad_hash: "这条记录的编号无效，读不到全文",
  // DATA_ 数据源层
  DATA_5020: "行情/数据源暂时不可用，请稍后重试",
  DATA_5040: "数据源响应超时，请稍后重试",
  // ANA_ 分析链路层
  ANA_5001: "分析任务执行失败，请稍后重试",
  ANA_5031: "分析服务暂不可用，请稍后重试",
  ANA_5401: "自动编码任务失败，请稍后重试",
  // TRADE_ 交易功能层
  TRADE_5021: "行情快照获取失败，请稍后重试",
  TRADE_5022: "回测数据获取失败，请稍后重试",
  TRADE_5023: "资金流数据获取失败，请稍后重试",
  // EVT_ 事件告警层
  EVT_5001: "事件扫描失败，请稍后重试",
  EVT_5031: "事件告警功能暂不可用",
};

/** HTTP 状态码兜底（后端没给码时的最后一层）。 */
function statusFallback(status: number): string {
  if (status === 401) return COPY.AUTH_4010;
  if (status === 403) return COPY.AUTH_4030;
  if (status === 404) return COPY.REQ_4040;
  if (status === 409) return COPY.REQ_4090;
  if (status === 422) return COPY.REQ_4220;
  if (status === 429) return COPY.AUTH_4290;
  if (status >= 500) return COPY.SYS_5000;
  return "请求失败，请稍后重试";
}

/** 从错误体里解出 {code, message}：支持字符串 detail 与结构化 detail。 */
function unpack(envelope: ErrorEnvelope): { code: string; message: string } {
  const d = envelope.detail;
  if (d && typeof d === "object") {
    return {
      code: typeof d.code === "string" ? d.code : (envelope.code ?? ""),
      message: typeof d.message === "string" ? d.message : "",
    };
  }
  return {
    code: envelope.code ?? "",
    message: typeof d === "string" ? d : "",
  };
}

/** 解析后端错误响应 → ApiError（**唯一**允许读响应体原文的地方）。
 *
 * 文案优先级：本地文案表 > 后端 message/detail（后端已保证不含内部细节）
 * > 状态码兜底。本机开发时附上报错码与 trace_id 便于排查；
 * 公网只显示干净文案。
 */
export function apiErrorFromResponse(status: number,
                                     bodyText: string): ApiError {
  let code = "";
  let backendMsg = "";
  let traceId: string | undefined;
  try {
    const envelope = JSON.parse(bodyText) as ErrorEnvelope;
    const u = unpack(envelope);
    code = u.code;
    backendMsg = u.message;
    traceId = envelope.trace_id;
  } catch {
    // 响应体不是 JSON（代理/网关的 HTML 错误页等）：一律不显示原文
  }
  const base = COPY[code] || backendMsg || statusFallback(status);
  const suffix = isLocalDevHost()
    ? `（${code || `HTTP ${status}`}${traceId ? ` trace:${traceId}` : ""}）`
    : "";
  return new ApiError(base + suffix, code, status, traceId);
}

/** 网络层失败（fetch reject）：区分"后端不在"与"连接被中断"。 */
export function networkError(_url: string, serverAlive: boolean): ApiError {
  if (serverAlive) {
    return new ApiError(
      "请求连接被中断（服务在线，这条连接已失效）—— 直接重试一次即可。",
      "NET_9002", 0);
  }
  const hint = isLocalDevHost()
    ? "请在项目目录执行 `python manage.py start --daemon --replace`，"
      + "确认 `python manage.py status` 显示后端在线后重试。"
    : "服务暂时不可用，请稍后重试；若持续出现请联系管理员。";
  return new ApiError(`无法连接到服务器。${hint}`, "NET_9001", 0);
}

/** 任意异常 → 用户可读文案（组件 catch 块的统一出口）。 */
export function userMessage(exc: unknown): string {
  if (exc instanceof ApiError) return exc.message;
  if (exc instanceof Error) {
    // 旧格式 `请求失败(400): {...}` 的兼容兜底（理论上新代码不再产生）
    return exc.message;
  }
  return String(exc);
}
