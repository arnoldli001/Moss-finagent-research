"""面试演示导览：按固定脚本依次演示系统全部核心能力（面向运行中的API服务）。

运行：
  1. 启动服务：.venv\\Scripts\\python.exe manage.py start --daemon
     （= 127.0.0.1:8100 的 **dev 隔离实例**，数据落 data/dev/、匿名可读；
       注意 8110 是**对外试点**，全站强制登录 ⇒ 本脚本不适用）
  2. .venv\\Scripts\\python.exe scripts/demo_tour.py                # 不含回测
     .venv\\Scripts\\python.exe scripts/demo_tour.py --with-backtest
说明：纯HTTP客户端（stdlib urllib），无新增依赖；真实LLM经Ollama降级链，
     宏观任务可能耗时1-3分钟、回测需再等数十秒，均属正常现象。

## 两步与"后来加的门"对齐过（2026-10-05）

* 第 5 步：`/api/v1/scheduler/*` 自 2026-09-26 起是**管理员专属**
  （`routes/scheduler.py` 整条路由挂 `require_admin`，理由是普通用户能借
  `POST .../jobs/{name}/run` 一键触发 4 次完整研究图）。本脚本**不携带任何凭据**，
  所以这里的正确期望是 **401**（fail-closed），而**不是** 200 ——
  断言写成"必须被拒"：哪天门松了这一步会红，而不是悄悄变成假绿。
  要给管理员会话留口子时用 `--admin-cookie`；那会把第 5 步换成真实读取作业清单。
* 第 7 步：`POST /api/v1/backtest/run` 早已改成**异步 job**（返回 `job_id`，
  结果在 `GET /api/v1/backtest/jobs/{job_id}`），本脚本原先按同步响应解引用
  ⇒ 必然崩。现已按"提交 → 轮询"实现，与第 3 步同一套写法。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DISCLAIMER = (
    "⚠️ 演示内容来自公开数据与本地模型推理，仅供参考，不构成投资建议。"
    "投资有风险，入市需谨慎，盈亏自负。"
)


def _request(base: str, path: str, body: dict | None = None,
             timeout: int = 30, cookie: str = "") -> tuple[dict | None, int]:
    """返回 `(载荷, HTTP状态码)`。**HTTP错误不当异常抛** —— 状态码本身是判据。

    为什么必须这么写：`urlopen` 对 4xx/5xx 抛 `HTTPError`，而旧版全脚本只捕获
    `URLError/TimeoutError` ⇒ 一个 401（第 5 步）就让整场演示崩在半路，
    后面几步连机会都没有。鉴权门是**预期结果**，不是崩溃。
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(
        f"{base}{path}", data=data, headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return json.loads(raw), resp.status
            except json.JSONDecodeError:
                return {"_raw": raw[:400]}, resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return json.loads(raw), exc.code
        except json.JSONDecodeError:
            return {"_raw": raw[:400]}, exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # 连接层失败（服务没起 / 端口打错 / 打到 8110 但被拒 / 超时）：
        # 返回 code=0 让调用方**判 FAIL 并给人话**，而不是抛栈崩在半路 ——
        # 这正是本脚本最初的报障形态（ConnectionRefused 直接把演示打断）。
        return {"_error": f"{type(exc).__name__}: {exc}"}, 0


def _service_down_hint(base: str) -> None:
    """服务连不上时，把"下一步做什么"直接印出来（省掉一轮排查）。"""
    print(f"  ❌ 连不上 {base}")
    print("     ① 起服务（dev 实例，数据落 data/dev/）："
          "  .venv\\Scripts\\python.exe manage.py start --daemon")
    print("     ② 确认端口：dev=8100；**8110 是对外试点，全站强制登录**，"
          "本脚本（匿名）不适用")
    print("     ③ 看日志：data/run/backend-dev.log")


def _poll(base: str, path: str, timeout: int, interval: float = 2.0,
          cookie: str = "", label: str = "") -> tuple[dict, int]:
    """轮询到 `status ∈ {completed, failed, done, error}` 或超时；返回最终载荷。

    调用方必须自己看 `status` 才能判 PASS/FAIL —— 超时返回的载荷里
    `status` 仍是 `running`，不允许把它当成"没报错就是好的"。
    """
    deadline = time.time() + timeout
    payload: dict = {}
    code = 0
    while time.time() < deadline:
        payload, code = _request(base, path, timeout=30, cookie=cookie)
        payload = payload or {}
        if code == 0:
            # 连接层都失败了，再轮询只是耗时间：立刻把失败交回给调用方。
            return payload, code
        if payload.get("status") in ("completed", "failed", "done", "error"):
            return payload, code
        stage = payload.get("stage_label") or payload.get("status") or "?"
        print(f"  ...{label}{stage}，等待{int(interval)}s")
        time.sleep(interval)
    return payload, code


def _banner(step: str, title: str) -> None:
    print("\n" + "=" * 64)
    print(f"[{step}] {title}")
    print("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="Moss-FinAgent-Research 演示导览")
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--poll-timeout", type=int, default=300)
    parser.add_argument("--backtest-timeout", type=int, default=300,
                        help="回测（异步 job）最长等待秒数")
    parser.add_argument("--with-backtest", action="store_true")
    parser.add_argument("--admin-cookie", default="",
                        help="管理员会话 Cookie（形如 moss_session=...）。"
                             "只影响第 5 步：不带则断言「匿名必须被拒」，"
                             "带了则真实读取调度作业清单")
    args = parser.parse_args()
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool | None, detail: str = "") -> None:
        """`ok=None` = **未判定**（例如缺前置步骤），既不算 PASS 也不算 FAIL。

        「没量到」与「量到 False」必须分开 —— 否则一次网络故障会被记成
        「这个能力坏了」，而真正的红灯反而被稀释。
        """
        if ok is None:
            print(f"  SKIP  {name}" + (f" — {detail}" if detail else ""))
            return
        checks.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    # 1. 健康检查
    _banner("1/7", "服务健康与审计哈希链")
    health, health_code = _request(args.base, "/api/v1/health")
    health = health or {}
    if health_code == 0:
        _service_down_hint(args.base)
        check("健康检查", False, "服务不可达")
        print(f"\n演示导览结果：0/{len(checks)} PASS"
              "（健康检查未过，其余步骤未执行）")
        return 1
    if health_code != 200:
        # 走到了服务、但被拒：最常见是打到了对外试点 8110（全站强制登录）。
        print(f"  ❌ /api/v1/health 返回 {health_code}：{health}")
        print("     不是 200 ⇒ 后面的步骤全部无意义，直接退出。")
        print("     若这是 8110：那是对外试点，全站强制登录；匿名导览请用 dev 的 8100。")
        check("健康检查", False, f"HTTP {health_code}")
        print(f"\n演示导览结果：0/{len(checks)} PASS"
              "（健康检查未过，其余步骤未执行）")
        return 1
    print(json.dumps(health, ensure_ascii=False, indent=2)[:800])
    check("健康检查", True)
    chain = health.get("audit_chain", {})
    check("LLM审计哈希链完整", chain.get("valid") is True,
          f"records={chain.get('records')}")

    # 2. Supervisor 规划（不执行，秒回）
    _banner("2/7", "Supervisor DAG 规划（宏观任务）")
    plan, _ = _request(args.base, "/api/v1/debug/plan?analysis_type=macro")
    plan = plan or {}
    print(f"analysis_type={plan.get('analysis_type')} agents={plan.get('agents')}")
    check("规划器返回Agent序列", len(plan.get("agents", [])) >= 2)

    # 3. 提交真实分析任务并轮询
    _banner("3/7", "异步投研任务（采集→清洗→校验→入库→分析→推荐）")
    submitted, submit_code = _request(args.base, "/api/v1/research/analyze", {
        "query": "当前宏观经济处于什么阶段？对A股有什么含义？",
        "analysis_type": "macro",
    }, timeout=30)
    submitted = submitted or {}
    if not submitted.get("task_id"):
        print(f"  ❌ 提交失败（HTTP {submit_code}）：{submitted}")
        if submit_code == 0:
            _service_down_hint(args.base)
        check("任务完成且产出报告", False, f"提交未返回 task_id（HTTP {submit_code}）")
        task: dict = {}
    else:
        task_id = submitted["task_id"]
        print(f"task_id={task_id} plan={submitted.get('plan')}")
        task, _ = _poll(args.base, f"/api/v1/research/{task_id}",
                        timeout=args.poll_timeout, interval=10.0, label="")
    ok_task = task.get("status") == "completed" and bool(task.get("report"))
    check("任务完成且产出报告", ok_task,
          task.get("status", "超时/无响应") if task else "提交失败")
    if task.get("errors"):
        print(f"  非致命错误：{task['errors']}")
    print(f"  A17结论={task.get('conclusion')} 置信度={task.get('confidence')}")

    # 4. 推理过程透明化
    _banner("4/7", "Agent输出全览（推理路径可溯源）")
    task_id = task.get("task_id") or locals().get("task_id", "") or ""
    if ok_task and str(task_id).startswith("task_"):
        agents, _ = _request(args.base, f"/api/v1/research/{task_id}/agents")
        agents = agents or {}
        outputs = agents.get("agent_outputs") or []
        for o in outputs:
            print(f"  - {o.get('agent_id')} [{o.get('confidence')}] "
                  f"{str(o.get('conclusion'))[:70]}")
        check("Agent输出可枚举", len(outputs) >= 2)
        trace, _ = _request(args.base, f"/api/v1/trace/{task_id}")
        trace = trace or {}
        check("Trace可查询", bool(trace))
        check("Trace含LLM调用审计", len(trace.get("llm_calls") or []) >= 1,
              f"{len(trace.get('llm_calls') or [])} 条LLM记录")
    else:
        skip = "任务未完成" if not ok_task else "没有可用的 task_id"
        check("Agent输出可枚举", None, skip)
        check("Trace可查询", None, skip)
        check("Trace含LLM调用审计", None, skip)

    # 5. 定时调度：**先问"这扇门该不该开"，再决定要什么证据**
    #
    # `/api/v1/scheduler/*` 自 2026-09-26 起整条路由挂 `require_admin`
    # （普通用户能借 POST .../jobs/{name}/run 一键触发 4 次完整研究图）。
    # 所以匿名请求的**正确期望是 401**，不是 200：
    # 断言写成"必须被拒"，哪天门松了这一步会红 —— 而不是悄悄变成假绿。
    _banner("5/7", "定时调度（管理员门 + 运行事实）")
    runs_path = f"{args.base}/api/v1/scheduler/runs?limit=10"
    if args.admin_cookie:
        jobs, jobs_code = _request(args.base, "/api/v1/scheduler/jobs",
                                   cookie=args.admin_cookie)
        runs, runs_code = _request(args.base, "/api/v1/scheduler/runs?limit=10",
                                   cookie=args.admin_cookie)
        jobs = jobs or {}
        job_items = (jobs.get("jobs") if isinstance(jobs, dict)
                     else jobs if isinstance(jobs, list) else []) or []
        runs = runs or {}
        run_items = (runs.get("runs") if isinstance(runs, dict)
                     else runs if isinstance(runs, list) else []) or []
        print(f"  （带管理员会话）作业清单 HTTP {jobs_code}；"
              f"注册任务 {len(job_items)} 个，最近运行记录 {len(run_items)} 条")
        check("调度作业清单（管理员会话）",
              jobs_code == 200 and len(job_items) >= 1,
              f"HTTP {jobs_code}")
    else:
        codes: dict[str, int] = {}
        for path in ("/api/v1/scheduler/jobs", "/api/v1/scheduler/runs?limit=10"):
            _payload, code = _request(args.base, path)
            codes[path] = code
        got = ", ".join(f"HTTP {c}" for c in codes.values())
        if all(c == 200 for c in codes.values()):
            print(f"  ⚠️ 调度端点对匿名请求返回 200（{got}）—— "
                  "它与 2026-09-26 起的管理员专属口径不一致")
            check("调度端点匿名必须被拒（401）", False, got)
        elif any(c in (401, 403) for c in codes.values()):
            print(f"  ✅ 调度端点对匿名请求已拒绝（{got}）—— 与「调度管理=管理员专属」"
                  "口径一致（要读作业清单请带 --admin-cookie）")
            check("调度端点匿名必须被拒（401）",
                  all(c in (401, 403) for c in codes.values()), got)
        else:
            check("调度端点匿名必须被拒（401）", False, f"非预期状态码：{got}")

        runs_file = Path("data/dev/scheduler/runs.jsonl")
        if not runs_file.exists():
            runs_file = Path("data/scheduler/runs.jsonl")
        records = 0
        last: dict = {}
        if runs_file.exists():
            for line in runs_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                    records += 1
                except json.JSONDecodeError:
                    continue
            print(f"  运行事实（本地读 {runs_file}）：{records} 条记录，"
                  f"最近 {last.get('job_name')} → {last.get('status')} "
                  f"{last.get('start_time', '')}")
        else:
            print(f"  ⚠️ 未找到运行记录文件 {runs_file}（dev 刚起、作业还没跑过属正常）")
        check("调度运行记录可读", None if not runs_file.exists() else records >= 1,
              f"{records} 条" if runs_file.exists() else "文件不存在")

    # 6. LLM运行指标
    _banner("6/7", "LLM运行指标（P50/P95/P99、缓存、降级、Provider分解）")
    metrics, metrics_code = _request(args.base, "/api/v1/metrics?limit=1000")
    m = (metrics or {}).get("metrics") or {}
    if metrics_code == 200 and m:
        lat = m.get("latency_ms") or {}
        print(f"  窗口调用={m.get('window_calls')} P95={lat.get('p95')}ms "
              f"缓存命中率={m.get('cache_hit_rate')} 降级率={m.get('fallback_rate')}")
        check("指标端点", int(m.get("window_calls") or 0) >= 0)
    else:
        check("指标端点", False, f"HTTP {metrics_code}")

    # 7. 回测（可选）：`POST /run` 只返回 job_id，结果在 `GET /jobs/{job_id}`
    if args.with_backtest:
        _banner("7/7", "纯本地规则回测（真实行情实时取数，无LLM/无未来函数）")
        started, start_code = _request(args.base, "/api/v1/backtest/run", {
            "indicator": "PPI", "code": "601088", "eps_pct": 1.0,
        }, timeout=60)
        started = started or {}
        job_id = str(started.get("job_id") or "")
        if start_code != 200 or not job_id:
            print(f"  ❌ 提交回测失败：HTTP {start_code} {started}")
            if start_code == 0:
                _service_down_hint(args.base)
            check("回测端点", False, f"提交失败 HTTP {start_code}")
        else:
            print(f"  job_id={job_id} 预估等待 {started.get('estimated_wait_seconds')}s")
            done, _ = _poll(args.base, f"/api/v1/backtest/jobs/{job_id}",
                            timeout=args.backtest_timeout, interval=2.0,
                            label="回测：")
            bt = done.get("result") if isinstance(done, dict) else None
            if done.get("status") == "done" and isinstance(bt, dict):
                s = bt.get("strategy") or {}
                bah = s.get("buy_and_hold") or {}
                rng = bt.get("range") or {}

                def _pct(v: object) -> str:
                    """契约里是**小数**（0.959 = +95.9%）。旧脚本用 `.2%` 格式化，
                    改异步后照抄会打成 95.90% —— 同一个数字两种读法，所以这里显式乘。"""
                    return f"{float(v) * 100:+.2f}%" if isinstance(v, (int, float)) else "n/a"

                def _pp(v: object) -> str:
                    """**超额是差值不是水平**（`策略 − 买入持有`，两个小数相减），
                    单位是**百分点(pp)**。套用 `_pct` 会输出 -390.00%，
                    读起来像"亏了 390%" —— 口径错比数字错更难被发现。"""
                    return f"{float(v) * 100:+.2f}pp" if isinstance(v, (int, float)) else "n/a"

                print(f"  {rng.get('start')}~{rng.get('end')} "
                      f"共{bt.get('periods')}月 信号={bt.get('signals')}")
                print(f"  策略累计={_pct(s.get('cumulative_return'))} "
                      f"买入持有={_pct(bah.get('cumulative_return'))} "
                      f"超额={_pp(s.get('excess_cumulative_return'))}"
                      "（= 策略 − 买入持有）")
                print(f"  策略最大回撤={_pct(s.get('max_drawdown'))} "
                      f"夏普(rf=0)={s.get('sharpe_rf0')} "
                      f"持仓月数={s.get('invested_months')}/{s.get('total_months')}")
                exc = s.get("excess_cumulative_return")
                if isinstance(exc, (int, float)) and exc < 0:
                    # 跑输就直说：演示里最不该做的就是只留好看的数。
                    print(f"  ⚠️ 该规则区间内**跑输**买入持有 {_pp(exc)} "
                          "（趋势+PE闸门是规则验证，不是可交易策略）")
                check("回测端点", int(bt.get("periods") or 0) >= 8,
                      f"{bt.get('periods')} 个月")
            else:
                print(f"  ❌ 回测未完成：{done.get('status')} "
                      f"{done.get('error') or done.get('message') or ''}")
                check("回测端点", False,
                      f"{done.get('status')}（{done.get('error') or '超时'}）")
    else:
        print("\n[7/7] 回测演示已跳过（加 --with-backtest 启用）")

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok in checks if ok)
    print(f"演示导览结果：{passed}/{len(checks)} PASS")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print(DISCLAIMER)
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
