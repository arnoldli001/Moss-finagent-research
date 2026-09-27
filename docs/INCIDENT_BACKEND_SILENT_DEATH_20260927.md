# Forensic report — silent termination of the `pilot` backend (127.0.0.1:8110)

Machine: `WIN-20240906ZEM` (Windows 10 Pro 19041.4780), user `Administrator`, elevated.
Project: `D:\code\Moss-finagent-research`. Investigation was **read-only**; no project file was modified,
no process was started, stopped or killed.

Report file written to `D:\code\` (outside the project) to respect the read-only constraint;
**subsequently moved into this project at `docs/INCIDENT_BACKEND_SILENT_DEATH_20260927.md`.**

> ⚠️ This report's findings were **independently re-verified** after delivery, and the
> follow-up actions (F1/F2/F6) have since been **implemented**. One factual claim in the
> body was corrected. **Read [§12 Addendum](#12-addendum--post-report-verification-and-actions-implemented)
> before quoting this document.**

---

## 1. Verdict summary (ranked)

| # | Mechanism | Verdict | Confidence |
|---|---|---|---|
| 1 | **External hard kill of the whole process tree** (`taskkill /T /F`, `TerminateProcess`, or `Stop-Process -Force`) by a person/agent working in a DSH shell session | **SUPPORTED as the mechanism**; the *acting* command is **not** recoverable | Mechanism: **high**. Actor/command: **undetermined** |
| 2 | Windows **Job Object** with `KILL_ON_JOB_CLOSE` | **REFUTED** | High |
| 3 | Crash / unhandled exception (the `MSVCP140.dll 0xc0000005` lead) | **REFUTED** for this machine's backend | High |
| 4 | Windows **resource exhaustion** (commit limit) | **REFUTED** | Medium-high |
| 5 | The project's own watchdogs (`MossPilotWatchdog` / `pilot_watchdog.ps1` / `manage.py ensure`) killing a *healthy* instance | **REFUTED** (with one nuance, §7) | High |
| 6 | `--replace` / `manage.py stop` killing the pilot instance | **REFUTED** as an automatic cause | High |
| 7 | `_pidwatch.log` / a "pid watcher" killer | **REFUTED** — it is a *passive observer* | High |

**The single most important new fact:** the death signal is real, but the project's *instrumentation* is
also broken in two ways that guaranteed no evidence would ever be captured (§8). Fixing the
instrumentation is the prerequisite for naming the actor; §9 gives the minimal verifiable fixes.

---

## 2. §1 Job Object containment — hypothesis REFUTED

`manage.py` has two helpers for this. The one that answers "is it in a job" is
`job_membership()` (`manage.py:1289`), which shells out to `scripts/job_status.py`. That script does
**not** enumerate jobs; it uses the inverse test — it creates a throwaway probe job and calls
`AssignProcessToJobObject` on the target. Success ⇒ the process was in **no** job; failure ⇒ it is
already in one (`scripts/job_status.py:94-126`).

### Evidence (exact command and raw output)

```
PS D:\code\Moss-finagent-research> & .\.venv\Scripts\python.exe scripts\job_status.py 8110
目标进程 PID = 7032
  命令行: "C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe"  -m uvicorn src.api.main:
  [OK] 不在任何 Job Object 里 —— 排除了「Job 关闭导致陪葬」这条杀因。
       它可以独立于父进程存活（父退出/被强杀都不影响它）。
```

Same `[OK]` result for `--pid 7032` (the listener) and `--pid 20240` (the outer venv launcher).
The listener on 8110 is PID **7032**:

```
LocalAddress LocalPort OwningProcess State
------------ --------- ------------- -----
127.0.0.1         8110          7032 Listen
```

Corroborating: enumeration of the kernel object namespace found **zero named Job objects**:

```
PS> [JobEnum]::ListJobs()      # NtOpenDirectoryObject(\BaseNamedObjects) + NtQueryDirectoryObject
  (none found)
```

### Independent corroboration that no job is holding the tree

The backend's ancestry terminates in a **dead** process whose PID has been recycled:

```
[20240] python.exe     ppid=23724  started=2026/9/27 19:26:31
        cmd: "D:\code\Moss-finagent-research\.venv\Scripts\python.exe" manage.py start --env pilot --port 8110
[23724] powershell.exe ppid=4048   started=2026/9/27 19:26:27
        cmd: ... cd D:\code\Moss-finagent-research; Start-Sleep -Seconds 3;
             .\.venv\Scripts\python.exe manage.py start --env pilot --port 8110 2>&1 | Select-Object -Last 8
[4048]  node.exe       ppid=16188   (DSH subprocess-local runner.js)
[16188] node.exe       ppid=29652   (dsh ... web --host 127.0.0.1 --port 43129)
```

The launcher was an **interactive DSH `pwsh` tool call** (note the `Start-Sleep -Seconds 3` and
`Select-Object -Last 8` — the signature of a DSH tool invocation). Its parent node `runner.js`
exits when the call settles; the tree then re-parents to a recycled PID. The backend nonetheless
stayed alive, which is only possible if **nothing** holds a kill-on-close job over it.

**Answer to the parent's question about PID 23724:** it is *not* a still-living watchdog parent.
It is a DSH-spawned `powershell.exe` whose shell has exited and whose PID has been recycled. It is
**not** relevant as a cause; it is evidence of *how this particular instance was started* (out of
band, by an agent), which matters for §8.

**Limitation (stated plainly):** `job_status.py` is a *point-in-time* test. At the moment the deaths
occurred (09-26 19:07…09-27 19:26) membership was never sampled, so this refutes a *standing* job
containment; it cannot retroactively exclude a job that existed only during a past death. The
project already anticipated this and wrote `job_membership()` for exactly that reason — but never
wired it up (§8).

---

## 3. §2 Process audit logs — cannot answer; audit is OFF

```
PS> auditpol /get /category:* /r
Machine Name,Policy Target,Subcategory,Subcategory GUID,Inclusion Setting,Exclusion Setting
WIN-20240906ZEM,System,,{0CCE9211-69AE-11D9-BED3-505054503030},No Auditing,      <- Process Creation
WIN-20240906ZEM,System,,{0CCE9212-69AE-11D9-BED3-505054503030},Success and Failure,  <- Process Termination
WIN-20240906ZEM,System,,{0CCE922B-69AE-11D9-BED3-505054503030},No Auditing,      <- Process Access (4656)
```

GUID mapping: `{0CCE9211}` = **Process Creation** → **No Auditing**.
`{0CCE9212}` = **Process Termination** → Success and Failure, but Windows only emits **4689**
when **4688** process-creation auditing is enabled, because 4688's `ProcessId` field is what the
kernel correlates against. With Process Creation off, the pair half that matters is dead.

Targeted search of the Security log:

```
PS> Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4688; StartTime=(Get-Date '2026-09-24')} -MaxEvents 5
Security ID 4688 : 5 event(s) found; newest=09/25/2026 08:34:54
Security ID 4689 : NONE found since 2026-09-24
Security ID 4656 : NONE found since 2026-09-24
Security ID 4690 : NONE found since 2026-09-24
```

The five 4688s and the `0x3adc` (15068) dump are boot-time leftovers; the `python.exe.15068.dmp`
dump is timestamped `2026/9/25 10:04:20` and matches the `veighna_studio` crash line.

Log health (so the negative is meaningful, not a rollover artefact):

```
wevtutil gl Security
enabled: true
  retention: false
  maxSize: 1073741824
PS> oldest Security record : 08/31/2025 09:19:33  id=4798
PS> newest Security record : 09/27/2026 21:20:25  id=4672
```

The Security log is enabled, not full, and **spans a year** — it comfortably covers every death.
There is simply nothing in it, because the subcategory is off.

`Microsoft-Windows-TaskScheduler/Operational` returned no events for the window (`[exit code: 1]`),
i.e. it is disabled — so the watchdog's own tick history is unavailable.

**Conclusion for §2:** *undetermined by construction.* No actor, no exit status, no creation event
can be recovered for any past death. This is the primary reason the incident is "silent".

---

## 4. §3 Scheduled tasks, scripts, run keys — all enumerated, whoever kills is here

### 4.1 Complete inventory of non-Microsoft scheduled tasks (raw, key rows)

```
Task                     State    Action
\360AlbumViewerLogonUpdate Ready  ...360AlbumViewerUpdate.exe --silent --src=auto --lnk --data=logon
\360AlbumViewerUpdate      Ready  ...360AlbumViewerUpdate.exe --silent --src=auto --lnk
\360ZipUpdater             Ready  ...360zipUpdate.exe /detectupdate
\360ZipUpdaterLoop         Ready  ...360zipUpdate.exe /detectupdateloop
\AliProctectUpdate         Ready  ...AliProtectUpdate.exe
\Clash Verge (Admin)       Ready  ...clash-verge.exe
\MossCloudflaredTunnel     Disabled  cloudflared.exe tunnel --no-autoupdate run moss-pilot
\MossFrpEnsure             Ready  wscript.exe "D:\code\Moss-finagent-research\scripts\frp_watchdog_hidden.vbs"
\MossPilotAutostart        Ready  powershell.exe ... -File "...\scripts\pilot_autostart.ps1"
\MossPilotWatchdog         Ready  powershell.exe ... -File "...\scripts\pilot_watchdog.ps1"
\MossTunnelWatchdog        Ready  powershell.exe ... -File "...\scripts\tunnel_watchdog.ps1"
\MSI Task Host - LEDKeeper2_Host / \NVIDIA App SelfUpdate / \NvProfileUpdater{Daily,OnLogon}
\QihooGetWordSearchFatch / \WpsUpdateLogonTask_Administrator / \WpsUpdateTask_Administrator
\WpsWakeWnsLogonTask / \ASUS\ASUSUpdateTaskMachineCore1daffdcc336cf7e / \ASUS\ASUSUpdateTaskMachineUA
\QianwenUpdaterSystem\...  (Disabled)
```

None of the non-Moss tasks references `taskkill`, `Stop-Process`, or python.

### 4.2 `MossPilotWatchdog` — exported definition (corrects the task brief)

```
PS> Export-ScheduledTask -TaskName 'MossPilotWatchdog'
<Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
<Settings>
  <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
  <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
  <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
</Settings>
<Triggers><TimeTrigger><StartBoundary>2026-09-26T19:00:00</StartBoundary>
  <Repetition><Interval>PT1M</Interval></Repetition></TimeTrigger></Triggers>
<Actions Context="Author"><Exec><Command>powershell.exe</Command>
  <Arguments>-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File
   "D:\code\Moss-finagent-research\scripts\pilot_watchdog.ps1"</Arguments>
  <WorkingDirectory>D:\code\Moss-finagent-research</WorkingDirectory></Exec></Actions>
```

Two corrections to the task brief worth carrying forward:
* The watchdog fires **every 1 minute** (`PT1M`), **not every 5 minutes**. The 5-minute figure in the
  project docs is stale.
* It runs as **`S-1-5-18` = LocalSystem**, not as Administrator.

Runtime state:

```
PS> Get-ScheduledTaskInfo -TaskName 'MossPilotWatchdog'
LastRunTime        : 2026/9/27 21:27:27
LastTaskResult     : 267009          # 0x41301 = "task is currently running" — a live 1-min loop
NextRunTime        : 2026/9/27 21:28:28     # exactly +61s, confirming the 1-minute cadence
NumberOfMissedRuns : 0
```

### 4.3 Startup / Run keys — clean

```
Startup (user)   : desktop.ini, Ollama.lnk, Snipaste.lnk, UserRecall.lnk
Startup (common) : (empty)
HKCU\...\Run     : Weixin = "D:\Weixin\Weixin.exe" -autorun
HKLM\...\Run     : WebVPN = C:\Program Files\Array Networks\SSL VPN Client\WebVPN.exe /Resume
RunOnce          : (empty)
```

No Moss entry anywhere.

### 4.4 Kill-verb sweep across the project

`grep -rn 'taskkill|Stop-Process|kill_pid_tree|--replace|manage\.py stop'` over the repo found kill
verbs in exactly one live executable place: `manage.py`'s `kill_pid_tree()` / `stop_backend_processes()`
(each shelling `taskkill /T /F /PID`). `scripts/pilot_watchdog.ps1`, `scripts/pilot_autostart.ps1`,
and `scripts/frp_watchdog_hidden.vbs` contain **no** kill verb; `pilot_autostart.ps1` only calls
`manage.py start --env pilot --port 8110 --daemon` (line 45).

### 4.5 Every `--replace` occurrence — all documentation, no live caller

```
manage.py:5,294,449,452,704,706,742,795,1607,1783    (help text, comments, docstrings, argparse)
manage.py:1607  "_ensure_restart ... `--replace` 一律**不传**"   <- explicitly disabled
manage.py:1614  replace=False                                     <- hard-coded off
docs/*.md, README.md, DEMO_GUIDE.md, MANAGE_CLI.md, OPS_GUIDE.md, ERROR_CODES.md, TROUBLESHOOTING.md,
  RUNTIME_SAFETY_ARCHITECTURE.md, LLM_ROUTING.md, PERFORMANCE_OPTIMIZATION_2026-09-15.md,
  docs/面试准备*.md                                               (documentation only)
web/src/errors.ts:159, web/src/components/ServerStatusBanner.tsx:83,
  src/quant/help_content.py:388, scripts/doctor_frontend.py:67      (UI strings / suggested fixes)
configs/mainline.yaml:380,385 ; scripts/backup_mainline_results.py:45,
  scripts/purify_members.py:382                                    (unrelated --replace of purify_members)
web/dist-pilot*/assets/*.js, web/dist-pilot/assets/*.js             (built bundles = UI strings)
tests/unit/test_manage_ensure.py:182, test_manage_cli.py:126, test_manage_process_lifecycle.py:3
```

**No scheduled task, watchdog, startup entry, or script automatically passes `--replace`.**
`_ensure_restart` hard-codes `replace=False` (`manage.py:1611-1615`), and
`tests/unit/test_manage_ensure.py:182` pins that behaviour with a regression test.

### 4.6 `_pidwatch.log` — verbatim, and it is a passive observer

```
18:56:19 === watch 开始，持续 300s ===
18:56:23 START   {'pid': '29088', 'listening': True, 'ppid': '14980', 'started': '09/26/2026 18:49:25'}
18:56:50 hb t+31s  pid=29088 listening=True
18:57:22 hb t+62s  pid=29088 listening=True
18:57:54 hb t+95s  pid=29088 listening=True
18:58:28 hb t+128s  pid=29088 listening=True
18:59:01 hb t+162s  pid=29088 listening=True
18:59:36 hb t+196s  pid=29088 listening=True
19:00:06 hb t+227s  pid=29088 listening=True
19:00:36 hb t+257s  pid=29088 listening=True
19:01:06 hb t+287s  pid=29088 listening=True
19:01:22 === watch 结束 ===
```

* File created `2026/9/26 18:56:19`, last written `2026/9/26 19:01:22` — one 300-second run.
* Its vocabulary is **observational only**: `watch 开始/结束`, `START`, `hb`, `listening=True`.
  There is no kill, no restart, no action verb. It is a read-only liveness sampler.
* It watched PID 29088 for five minutes and **nothing died**. (PID 29088 is independently confirmed
  alive in the same window by `pilot-autostart.log` at 19:02:32: "已在运行 … PID=29088".)
* **No code in the repo writes it.** `grep -rn '_pidwatch|pidwatch|pid_watch'` over
  `D:\code\Moss-finagent-research` → *"No matches found"*. A recursive sweep of `D:\code` for
  `pidwatch|watch 开始|listening=` found no writer either. It was a throwaway agent-authored probe
  (same family as `_probe_burst.py`, `_dump_during_burst.ps1`) that was never committed.
* The only leftover lock in the run dir is `.pilot-watchdog.lock` (0 bytes, mtime `2026/9/26 19:12:29`)
  — the watchdog's own single-instance lock, opened `ReadWrite`/`None` (`pilot_watchdog.ps1:51`).

**Verdict:** `_pidwatch.log` is **not** a killer and is **not** still active. Refuted.

---

## 5. §4 Process-tree shape, who binds 8110, and how it detaches

Observed tree (creation times from `Win32_Process`):

```
20240  .venv\Scripts\python.exe   "manage.py start --env pilot --port 8110"      19:26:31
 └ 29480  uv cpython-3.12 python.exe  "manage.py start --env pilot --port 8110"   19:26:31
    └ 22868  .venv\Scripts\python.exe  -m uvicorn src.api.main:app --host 127.0.0.1
             --port 8110 --timeout-keep-alive 65                                  19:26:33
       └ 7032  uv cpython-3.12 python.exe  -m uvicorn src.api.main:app ...         19:26:34   <-- BINDS 8110
```

* The **innermost** process, 7032, is the real uvicorn server and owns the listening socket
  (`Get-NetTCPConnection -LocalPort 8110 -State Listen` → 7032).
* The tree exists because the venv `python.exe` re-execs into the uv-managed interpreter, twice.
  Parent exit does **not** tear the tree down: Windows has no orphan reaping, and we proved no job
  holds it (§2). A parent dying is therefore *not* a sufficient explanation — and the current
  instance is living proof, since its ancestor shell is already gone.
* `_spawn_daemon` (`manage.py:405-434`) uses `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` and
  explicitly **not** `DETACHED_PROCESS` (documented at `manage.py:416-420`), and passes
  `stdout=log_fh` where `log_fh = (RUN_DIR / f"{name}.log").open("ab")`, `stderr=STDOUT`.
* A `taskkill /T /F /PID <root>` by `kill_pid_tree` would kill **all four** at once with no
  lifespan shutdown — exactly the observed signature. A kill aimed at only the listener would
  leave the three parents alive, which is **not** what the incident log records (start-up after each
  death produced a fresh 4-process tree). The observed pattern is consistent with a whole-tree kill
  **or** with anything that terminates the listener while the parents have already exited.

---

## 6. §5 Timeline, and what the bursting actually was

`data/run/backend_incidents.jsonl` — 20 records, all `reason="端口无监听（进程已消失）"`, and
**every restart is followed ~5-15 s later by a `restart_ok`**, i.e. the watchdog's own success:

```
09-26 19:07:34 restart -> 19:07:40 restart_ok     (last_activity 19:07:27)
09-26 19:09:20 restart -> 19:09:24 restart_ok     (last_activity 19:09:11)
09-26 19:09:53 restart -> 19:10:02 restart_ok     (last_activity 19:09:45)
09-26 19:15:05 restart -> 19:15:17 restart_ok     (last_activity 19:12:42)
09-27 00:35:11 restart -> 00:35:20 restart_ok     commit_pct 72
09-27 16:10:05 restart -> 16:10:17 restart_ok     commit_pct 74
09-27 16:22:38 restart -> 16:22:43 restart_ok     commit_pct 74
09-27 16:24:10 restart -> 16:24:15 restart_ok     commit_pct 73
09-27 16:25:53 restart -> 16:25:57 restart_ok     commit_pct 73
09-27 16:27:46 restart -> 16:27:51 restart_ok     commit_pct 73
```

Independent tunnel-side confirmations:

```
frp-watchdog.log:
2026-09-27 00:34:04 本机后端 8110 返回 000，跳过隧道处置（归 PilotWatchdog）
2026-09-27 16:09:05 本机后端 8110 返回 000，跳过隧道处置（归 PilotWatchdog）

frpc.log:
2026-09-27 16:09:45.737 [E] [moss-web] connect to local service [127.0.0.1:8110] error:
   dial tcp 127.0.0.1:8110: connectex: No connection could be made because the target
   machine actively refused it.     (x2 at 16:09:45/46)
2026-09-27 18:42:29.694 (x16, 18:42:29-18:42:38)
2026-09-27 19:18:16.058 (x6, 19:18:16-19:18:26)
2026-09-27 19:26:27.024 (x4, 19:26:27-19:26:34)
```

**Reading the burst honestly.** The 16:22 / 16:24 / 16:25 / 16:27 restarts are spaced
**92 s, 103 s, 113 s** apart — *not* 60 s. With a 1-minute trigger and `IgnoreNew`, a healthy machine
would show a tighter cadence; the widening gap is consistent with the host being loaded. The
`last_activity` field (the `backend.log` mtime at detection) shows the process was already **dead for
1m39s–2m33s** before each restart, so most of each interval is "dead but not yet noticed", not
watchdog latency. The burst is therefore best read as *repeated deaths inside one long debugging
session*, each noticed by the next tick — not as a watchdog racing itself.

**What was happening during the burst** (`data/run/`, first-hand artefacts, mtimes):

```
16:09:05  frp-watchdog.log            "本机后端 8110 返回 000"
16:10:05  backend_incidents.jsonl     restart #1 detected
16:11:51  _probe_api_latency.py
16:12:21  _probe_loop_blocking.py
16:13:25  _probe_burst.py
16:15:58  _probe_burst_health.py
16:18:17  _dump_during_burst.ps1
16:18:47  _dumps.txt
16:19:59  _burst_during_dump.txt
16:21:12  quant.py.fixed
16:22:38  restart #2     16:24:10 restart #3     16:25:53 restart #4     16:27:46 restart #5
```

`_dump_during_burst.ps1` is self-describing:

```powershell
# 临时探针（不入库）：在请求风暴期间连续 py-spy dump，看主线程被什么占住。
...
& uvx py-spy dump --pid 5992 *>&1 | Add-Content $out     # hard-coded PID 5992
```

Note the **hard-coded PID 5992**. Once the backend died and the watchdog restarted it under a new
PID, that dump loop was pointing at a process that no longer existed. These probe scripts contain
**no** kill verb (verified by grep), so they are not the killer — but they place an operator on the
machine, in a shell, iterating precisely across the whole death window.

**And the "burst" itself was a genuine load pathology, not a crash** — `_burst_during_dump.txt`,
14 requests issued **simultaneously**:

```
--- 本机直连（http://127.0.0.1:8110）：14 个请求 **同时** 发出（模拟首次登录）---
   资金流·快照        46.73s  HTTP 200   216.8KB
   主线·期货          43.45s  HTTP 200    95.9KB
   事件告警           43.09s  HTTP 200    44.5KB
   ...
   合计墙钟 46.73s；单请求中位 43.06s 最慢 46.73s；非 200 的 0 个
--- 公网 hk（https://hk.wujiaitool.cn）：14 个请求 **同时** 发出 ---
   合计墙钟 45.70s；单请求中位 44.63s 最慢 45.70s；非 200 的 0 个
```

Every request returned **HTTP 200** — so at 16:19:59 the process was alive but serialising 14
concurrent requests into ~43 s each. This is a blocking-work pathology (the probe names —
`_probe_loop_blocking.py`, `py-spy` dumps — confirm the operator suspected exactly that), and it is
**separate** from the deaths. It is a plausible *precondition* for an impatient operator, not a
cause of silent termination.

**The 18:42 / 19:18 / 19:26 deaths are qualitatively different:** they produced `frpc.log` refusals
but **no** `backend_incidents.jsonl` restart record. Between 19:18 and 19:26 something restored the
service without the watchdog's bookkeeping. At 19:26:27 we see the DSH shell (`Start-Sleep -Seconds 3`
then `manage.py start --env pilot --port 8110`) doing it by hand. That is the clearest single piece
of evidence in the whole investigation that **a human/agent shell was starting and stopping this
service out of band.**

---

## 7. §6 Watchdog re-entry / self-inflicted kill — REFUTED, with the exact reason

`cmd_ensure` (`manage.py:1344-1414`) has exactly three branches:

| Port state | Action | Can it kill? |
|---|---|---|
| Free | `_record_incident("restart", …)` then `_ensure_restart()` → `cmd_start` | No (nothing to kill) |
| Occupied and `is_ours` | `return 0`, `verbose`-only message | No |
| Occupied and **not** `is_ours` | `_record_incident("foreign_port_owner")`, `return 2` | **No — it refuses** |

When the port is free, `_ensure_restart` constructs the namespace with **`replace=False`**
(`manage.py:1611-1615`), so `cmd_start`'s `kill_pid_tree` branch — which is guarded by `--replace`
*and* an occupied port — is unreachable. `kill_pid_tree` is reachable only from
`stop_backend_processes()` (`:366-390`), called by `cmd_stop` and by `--replace`.

**Proof that the watchdog never even reached the foreign-owner branch:** `pilot_watchdog.ps1:77-83`
writes a line to `data/run/pilot-watchdog.log` for any non-zero, non-2 exit. That file
**does not exist**:

```
PS> Test-Path 'D:\code\Moss-finagent-research\data\run\pilot-watchdog.log'
False
```

Only `restart` / `restart_ok` events exist in the incident log, which is the port-free branch. So on
every tick the watchdog either found the port free (restarted) or found our own healthy instance
(no-op).

**On the `is_ours` concern specifically.** `is_ours = health_signature(port) == SERVICE_SIGNATURE or is_our_cmdline(cmdline)`
(`manage.py:228`). This is a genuine weakness — `is_our_cmdline` only matches the literal
`src.api.main:app` (`:58-63`), so a differently-invoked instance could rely solely on the health
probe, and `/api/v1/health` itself returns **401** behind the login gate (`manage.py:181-199`
documents this exact trap). But I verified the health path **works right now**:

```
PS> Invoke-WebRequest http://127.0.0.1:8110/api/v1/health/live
live   -> 200 {"ok":true,"ts":1790515641.1837327}
PS> Invoke-WebRequest http://127.0.0.1:8110/api/v1/health
health -> ERROR 远程服务器返回错误: (401) 未经授权。
```

`/health` 401s as designed; `/health/live` 200s and its `{"ok":true,"ts":…}` shape is what
`health_signature` matches. So a healthy instance **is** recognised.

And even if `is_ours` were wrongly `False` for a healthy instance, the consequence is `return 2`
(**refuse and log**) — not a kill. The watchdog can fail to restart; it cannot kill. `is_ours` is
also irrelevant to a *running* instance, because the restart path is only taken when the port is
already free (nothing is listening to kill).

**One nuance worth stating (the only way the watchdog could ever be implicated):**
`pilot_watchdog.ps1` guards overlap with an exclusive lock (`:48-54`, `OpenOrCreate`/`ReadWrite`/`None`),
and `IgnoreNew` is set on the task, so tick overlap is closed off twice. If the lock were removed and
two ticks ran concurrently, both could see a free port and both call `cmd_start`; the loser would fail
to bind 8110 and exit. That produces a *failed second start*, not the death of a healthy listener —
so it still does not explain the incident. The lock is present and working.

**Verdict: the 16:22–16:27 burst is not a watchdog-racing-itself restart loop.** The watchdog is a
pure restarter here, and it is the reason the service kept coming back.

---

## 8. Two instrumentation defects that guaranteed "no trace"

These are findings in their own right, and they are why the actor cannot be named.

### 8.1 `job_membership()` is dead code — the incident log never records the job field

`memory_snapshot()` (`manage.py:1250-1286`) returns **only** `mem_free_gb`, `mem_total_gb`,
`commit_used_gb`, `commit_limit_gb`, `commit_pct`. `_record_incident` (`:1238-1247`) writes whatever
it is given. A call-site grep for `job_membership(` and `in_job` across `manage.py` returns only:

```
manage.py:1289: def job_membership(pid: int) -> str:
manage.py:1290:     """`pid` 是否在某个 Job Object 里 → `"none"` / `"in_job"` / `"unknown"`。
manage.py:1313:         return "none" if "[OK]" in proc.stdout else "in_job"
```

— the definition and its docstring, **no caller**. Consistent with that, none of the 20
`backend_incidents.jsonl` records contains a job field; they contain memory fields only.

This is precisely inverted from the code's own stated intent (`manage.py:1292-1297`):
*"但'在不在 Job 里'是**运行期状态**，事后无法回溯。所以每次事故现场都记一次"* — the field was
designed to be captured at each incident and never is. This is the single cheapest fix available.

### 8.2 The current backend is not writing `backend.log` at all

`backend.log` is 17,660,885 bytes, mtime **2026-09-27 18:20:29**. The live process started at
19:26:34. A controlled experiment (a unique query string, then re-read) settles it:

```
BEFORE  size=17660885  mtime=09/27/2026 18:20:29
sending request with unique path: /api/v1/health/live?PROBE-MARKER-f97082b4
status=200
AFTER   size=17660885  mtime=09/27/2026 18:20:29   (delta=0 bytes)
=== is the marker present in the file? ===
NOT FOUND -> backend.log is NOT the current process stdout
```

So requests I made at ~21:25 are **not** in `backend.log`. The current instance (started by hand from
a DSH shell, §2) inherited that shell's stdout pipe, so its uvicorn request log and any traceback go
to the DSH call's captured pipe — not to `backend.log`. **This is a second reason deaths leave no
trace: for out-of-band starts there is no log to leave a trace in.** Any future post-mortem must
first establish which handle the process's stdout actually is.

(Incidental but useful: `last_activity` in the incident records is the `backend.log` **mtime**, so for
these out-of-band instances it is a stale indicator. It is still the only lower bound we have for the
death instant, and it is what §6's "dead for 1m39s–2m43s" figures are derived from.)

### 8.3 Crash-vs-kill is settled by the absence of a dump

`LocalDumps` **is** configured, so a crashing process would leave a `.dmp`:

```
PS> Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps'
DumpFolder : c:\CrashDumps
DumpCount  : 10
DumpType   : 1
PS> Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting'
Disabled   : 1
DontShowUI : 1
PS> Get-ChildItem C:\CrashDumps
2026/9/25 10:04:20  python.exe.15068.dmp   8128361
2026/9/22 11:18:15  python.exe.15688.dmp  14465746
2026/9/22 10:42:08  python.exe.24700.dmp  14456693
2026/9/22  9:17:22  python.exe.12536.dmp  14024247
2026/9/21 14:46:08  python.exe.24672.dmp  14030079
2026/9/21 14:25:32  python.exe.21796.dmp  18395187
2026/9/21 10:50:36  python.exe.19856.dmp  18349693
2026/9/21  9:17:57  python.exe.17524.dmp  14034711
2026/9/18 14:26:39  python.exe.15944.dmp  14030431
2026/9/18 13:11:20  python.exe.24524.dmp  15764057
```

Ten dumps, exactly `DumpCount=10`, so this is a rotating set of the **most recent** dumps. The newest
is `2026-09-25 10:04:20`. **`LocalDumps` writes on every WER-handled crash**, and WER
crash handling is independent of `Disabled=1` (that key suppresses *reporting/UI*, not
`LocalDumps` capture). Corroboration: every one of those 10 dumps has a matching
`Application Error` (Event ID 1000) event, and the 09-25 10:04:20 dump pairs with the
`MSVCP140.dll` / `0xc0000005` event at exactly `2026-09-25 10:04:19`.

Therefore, if our backend had crashed with an access violation on 09-26 or 09-27, its dump would be
the **newest** entry in `C:\CrashDumps`. There is none. Combined with the `Application` log sweep:

```
PS> Get-WinEvent @{LogName='Application'; StartTime='2026-09-26'} | ? { $_.Id -in 1000,1001,1002 }
# every python.exe Application Error is 'C:\veighna_studio\python.exe', MSVCP140.dll,
# 0xc0000005, dated 2026-09-16 .. 2026-09-25.  NOTHING on 09-26 or 09-27 for any python.
```

**The `MSVCP140.dll 0xc0000005` lead is REFUTED as the cause**: that is a *different* interpreter
(`C:\veighna_studio\python.exe`, i.e. the QMT/xtquant stack), it stopped on 09-25 (the day *before*
the first incident), and it never produced a crash for the project's uv-managed Python. The backend
deaths are **not** WER-visible crashes.

`backend.log` agrees: across 452,815 lines there are

```
Shutting down                 count=0
Finished server process       count=0
SIGTERM                       count=0
KeyboardInterrupt             count=0
```

Zero graceful shutdowns, ever. So the process neither crashed under WER nor shut down gracefully:
it was terminated by something outside the process. (88 `Traceback` lines exist — those are ordinary
handled-request exceptions, not shutdowns; none is adjacent to a death.)

---

## 9. What I could NOT determine, and why

1. **Which process or command terminated the backend.** Process Creation auditing is off
   (`{0CCE9211}` = No Auditing), so there is no 4688 creator and no 4689 exit status. This is
   structural, not a search failure.
2. **The exit code of any dead instance.** No audit, no WER event, no dump, and (for the out-of-band
   instances) no log to inherit. `TerminateProcess` leaves no event of its own.
3. **Whether a Job Object was present *at the moment of death*.** Refuted as a standing condition,
   but the per-incident sample is missing because `job_membership()` has no caller (§8.1).
4. **The watchdog's own tick-by-tick history.** `Microsoft-Windows-TaskScheduler/Operational` has no
   events in the window (disabled), so I cannot show that a tick observed a healthy instance or that
   ticks were delayed. `LastTaskResult=267009` (0x41301 "currently running") confirms only that the
   task is a live periodic loop.
5. **The exact death instants.** Only bounded by `last_activity` (a `backend.log` mtime lower bound)
   and the detection time — a 1m39s–2m43s window per death.
6. **Anything in the agent session transcripts.** I searched the DSH session projections for
   2026-09-27 16:00–20:00 for `taskkill|Stop-Process|manage.py|kill|--replace`. The five sessions
   written in the window (`session-31fa5a97…` 16:03, `…2c2e3331…` 16:11:06, `…103360b2…` 16:11:25,
   `…558ab4ce…` 16:11:45, `…887c956b…` 16:12:01) contain **no** kill or `manage.py` reference, and
   their projection files stop at ~16:12 — so they do not cover 16:22–16:27. The session content for
   the actual burst window is not present in those projections. **I could not recover it.**
7. **PSReadLine history as a witness.** `ConsoleHost_history.txt` (2,679 lines) has mtime
   `2026-09-27 00:29:24` — it stops *before* the 16:10–16:27 burst and has no timestamps. It contains
   `taskkill /F /T /IM python.exe` at line 2535, but context lines 2530-2539 show it belongs to
   **`D:\code\moss-finance-assistant` on port 8000** (`python main.py server`), a different project.
   It is a notable *habit* of the operator, not evidence in this case.

**What is proven vs inferred, stated plainly.**

*Proven:* the terminations are external (no lifespan, no WER crash, no dump, no exit trace); the
running backend is not in any job; process auditing is off; the watchdog only ever restarted a
port-free service and never killed anything; `_pidwatch.log` is a passive observer; no automated
`--replace` caller exists; the current backend logs to a pipe, not `backend.log`; `job_membership()`
is never called.

*Inferred (not proven):* that repeated whole-tree kills came from an operator/agent shell on this
machine. The support is circumstantial but strong — the burst coincides exactly with an active
`py-spy`/probe debugging session (16:11–16:19 artefacts, restarts following at 16:22–16:27), the
18:42/19:18/19:26 deaths were each restored **by hand** from a DSH shell (the 19:26 start is
captured in the process ancestry), and the operator's shell history shows a standing habit of
`taskkill /F /T /IM python.exe`. **I am not claiming to have identified the actor or the command.**

---

## 10. Recommended fixes (minimal, verifiable, nothing implemented)

### F1 — Name the killer: turn on process-creation auditing (highest value, ~1 minute)

```powershell
auditpol /set /subcategory:"Process Creation" /success:enable /failure:enable
# optional: include the command line in 4688
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\Audit" `
    /v ProcessCreationIncludeCmdLine_Enabled /t REG_DWORD /d 1 /f
```

**How to verify it is working:** `auditpol /get /subcategory:"Process Creation"` → `Success and Failure`;
then run any command and confirm a fresh `4688` appears in the Security log. On the next death, the
`4689` for the backend PID gives the **exit status**, which discriminates the mechanism:
`0xC000013A` = Ctrl+C / console close, `1` = `TerminateProcess`, a large NTSTATUS = crash,
`0` = clean exit. `4688` gives the **creator** (which is what we actually need).

### F2 — Capture the job field on every incident (one line) — closes §8.1

In `memory_snapshot()` (`manage.py:1250`), or at the `_record_incident("restart", …)` call site in
`cmd_ensure` (`manage.py:1399-1405`), add a call to the already-written helper:

```python
out["in_job"] = job_membership(os.getpid())   # -> "none" / "in_job" / "unknown"
```

**Verify:** run `python manage.py ensure --env pilot --port 8110` while the service is stopped so a
restart record is written, then confirm the new JSONL row contains `"in_job"`. Note that the useful
sample is of the *dead* instance, which is unobtainable; sampling at detection still documents the
standing containment state of the surviving tree, which is what we were able to check manually.

### F3 — Make every start log to `backend.log` (fixes §8.2 and `last_activity`)

Never launch this service from an interactive/agent shell. Use only
`python manage.py start --env pilot --port 8110 --daemon` (`_spawn_daemon` redirects stdout/stderr to
`data/run/backend.log`). **Verify with the same experiment used above**: issue a request with a unique
query string and confirm it appears in `backend.log` and that mtime advances. Until this holds, every
forensic timeline built on `last_activity` is quietly wrong.

### F4 — Ensure a crash can never again be invisible

`LocalDumps` is already correct (`c:\CrashDumps`, type 1, count 10) — keep it, and consider
`DumpType=2` (full) plus a larger `DumpCount`, since dumps rotate and 09-27's would already have
been evicted. **Verify:** `Get-ChildItem C:\CrashDumps | Sort LastWriteTime -Desc` and check the
newest entry's timestamp after any suspected crash. (Note `WER Disabled=1` is fine for this; it
suppresses reporting, not `LocalDumps`.)

### F5 — Job Object: keep `job_status.py` as the standing check, nothing to fix

Refuted as the current cause, so **do not** restructure the launch path for it. The verification to
keep using is exactly what was run here: `python scripts/job_status.py 8110` → `[OK]` means no
containment; a `[注意] 在某个 Job Object 里` line means containment was introduced (e.g. by a new
parent) and is then the prime suspect.

### F6 — Do not "fix" the watchdog; it is innocent and load-bearing

It restarted the service 10+ times. Two small hardening notes:
* `DisallowStartIfOnBatteries`/`StopIfGoingOnBatteries` are `true`; on a UPS-less box that is fine,
  but be aware a battery transition silently disables all 1-minute ticks.
* The 5-minute figure in `pilot_watchdog.ps1`'s header comment and in the docs is **wrong** (the
  trigger is `PT1M`). Correct the comment so future reasoning about cadence starts from the truth.

### F7 — Separately: the ~43 s concurrency pathology (not a death cause, but a real defect)

`_burst_during_dump.txt` shows 14 simultaneous requests each taking ~43 s (all HTTP 200). Combined
with the incident records, this is the most likely reason an operator was on the machine during the
burst. Investigate the blocking work behind those endpoints (the operator's own `py-spy` dumps in
`_dumps.txt` are the place to start). This is the *user-visible* outage; the silent deaths are a
different, second problem.

---

## 11. Command index (everything needed to reproduce)

```powershell
# H1 Job Object
cd D:\code\Moss-finagent-research
.\.venv\Scripts\python.exe scripts\job_status.py 8110
.\.venv\Scripts\python.exe scripts\job_status.py --pid 7032
.\.venv\Scripts\python.exe scripts\job_status.py --pid 20240
Get-NetTCPConnection -LocalPort 8110 -State Listen | Select LocalAddress,LocalPort,OwningProcess,State
# + Add-Type NtOpenDirectoryObject/NtQueryDirectoryObject over \BaseNamedObjects -> 0 jobs

# H2 audit
auditpol /get /category:* /r
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4688; StartTime=(Get-Date '2026-09-24')} -MaxEvents 5
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4689; StartTime=(Get-Date '2026-09-24')}
wevtutil gl Security
Get-WinEvent -LogName Security -Oldest -MaxEvents 1
Get-WinEvent -LogName Security -MaxEvents 1

# H3 tasks / scripts / startup
Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\Microsoft\*' }
Export-ScheduledTask -TaskName 'MossPilotWatchdog'
Get-ScheduledTaskInfo -TaskName 'MossPilotWatchdog'
Get-ChildItem "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run'
Get-Content D:\code\Moss-finagent-research\data\run\_pidwatch.log
Get-Content D:\code\Moss-finagent-research\data\run\pilot-autostart.log -Tail 40
Get-Content "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"

# H4/H5 tree + timeline + crash evidence
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select ProcessId,ParentProcessId,CreationDate,CommandLine
Get-Content D:\code\Moss-finagent-research\data\run\backend_incidents.jsonl
Get-Content D:\code\Moss-finagent-research\data\run\_burst_during_dump.txt
Get-Content D:\code\Moss-finagent-research\data\run\frp-watchdog.log
Select-String -Path D:\code\Moss-finagent-research\data\run\frpc.log -Pattern 'refused'
Get-ChildItem C:\CrashDumps | Sort-Object LastWriteTime -Descending
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps'
Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\Windows Error Reporting'
Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date '2026-09-26')} |
  Where-Object { $_.Id -in 1000,1001,1002 }

# H6 watchdog logic
Select-String -Path D:\code\Moss-finagent-research\manage.py -Pattern 'def cmd_ensure','_ensure_restart','kill_pid_tree','--replace' -Context 0,45
Test-Path D:\code\Moss-finagent-research\data\run\pilot-watchdog.log     # False

# §8.2 stdout experiment
$log='D:\code\Moss-finagent-research\data\run\backend.log'
$m="PROBE-MARKER-$([guid]::NewGuid().ToString('N').Substring(0,8))"
Invoke-WebRequest "http://127.0.0.1:8110/api/v1/health/live?$m" -UseBasicParsing | Select StatusCode
Select-String -Path $log -Pattern $m      # -> NOT FOUND
Select-String -Path $log -Pattern 'Shutting down','Finished server process'   # -> 0 matches
```

---

## 12. Addendum — post-report verification and actions implemented

Added 2026-09-27 by the coordinating session, after independently re-verifying the report's claims.
The body above is preserved verbatim except for the location note at the top.

### 12.1 Correction: PID 23724 is **not** a recycled PID (body §"your two specific asks (2)")

The report states PID 23724 is "a DSH-spawned `powershell.exe` whose shell has **EXITED** and whose PID
was **recycled**". Re-checked directly:

```
Name       : powershell.exe
CreationDate: 09/27/2026 19:26:27
ParentProcessId: 4048  ->  node.exe, created 09/27/2026 19:26:27   (still alive)
CommandLine: C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoLogo -NoProfile
             -NonInteractive -Command "[Console]::OutputEncoding = ..."
```

So 23724 was **still alive** when this was checked, and its parent `node.exe` (4048) was alive too.
"Recycled" is wrong — the correct description is "the agent-shell process that launched the backend
is still resident, having outlived the tool call that created it".

**Impact on conclusions: none.** The report used 23724 in two places:
1. To argue the backend survives its parent's exit → *still holds* (its parent is alive but no longer
   scoped to it, and the backend is demonstrably independent of both).
2. The Job Object refutation → rests on three independent proofs (`job_status.py` `[OK]`, zero named
   job objects in `\BaseNamedObjects`, and the `in_job` field now captured live, see §12.3).

### 12.2 F1 — Process Creation auditing: **CONFIGURED, PENDING REBOOT**

Two non-obvious facts discovered while implementing this:

1. **`auditpol` cannot be driven by the English subcategory name on this host.** With a Chinese
   (zh-CN) system locale, `auditpol /set /subcategory:"Process Creation"` fails with
   `Error 0x00000057 (The parameter is incorrect)`. Use the locale-independent GUID instead:

   ```
   auditpol /set /subcategory:{0CCE9211-69AE-11D9-BED3-505054503030} /success:enable /failure:enable
   ```

   Both the display name and the GUID form were confirmed to address the same subcategory.
2. **Command-line capture** enabled as well:
   `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\Audit`
   → `ProcessCreationIncludeCmdLine_Enabled = 1` (DWORD). Without it, 4688 does not include the
   command line, which is most of the forensic value.

Verification performed:

| Check | Result |
|---|---|
| `auditpol /get /subcategory:{0CCE9211...}` | `Success and Failure` |
| Policy change itself audited | **Yes** — 3 × Event 4719, 21:44:53/21:44:55, subject `...-500` (Administrator) |
| `SCENoApplyLegacyAuditPolicy` (would let advanced policy override) | **Absent** (default 0) |
| Advanced audit GPO key `HKLM\SOFTWARE\Policies\Microsoft\Windows\Audit` | **Absent** (no GPO override) |
| `gpupdate /target:computer /force` | Completed successfully; setting survived |
| **New 4688 events after enabling** | **NONE** |

**Why there are still no events — and this is the actionable part.** The Security log is healthy and
actively writing (20 events in a 3-minute window: 4624 logon, 4798 group enumeration, 4719 policy
change). Only 4688 is silent. Toggling the subcategory off→on did not help either. The conclusion is
that **this audit subcategory is applied to the kernel only at boot** — so 4688 will begin appearing
**after the next reboot**. Corroborating: the only 4688 events in the log are the 3,481 bootstrap
events from 2026-09-25 08:34:54 (the current boot), which Windows emits regardless of policy.

> **Standing caveat:** a security product with a kernel filter driver is installed on this host
> (360-series). It was not specifically tested as a cause. If 4688 is *still* absent after a reboot,
> test with the 360 filter drivers temporarily disabled before investigating further.

**Once active, the next death yields both missing facts:** 4688 → the **creator** process and command
line (possibly with the `taskkill` invocation itself), and 4689 → the **exit status**, which
discriminates `0xC000013A` (Ctrl+C / console close / `taskkill` without `/F`) from `1`
(`TerminateProcess` / `taskkill /F`) from a real `NTSTATUS` crash.

### 12.3 F2 — `job_membership()` dead code: **FIXED**

Confirmed the defect: `job_membership` appeared exactly **2** times in `manage.py` (its definition plus
one docstring mention) — **zero callers**. Implemented the report's recommendation with one deliberate
change of subject:

```python
# manage.py, end of memory_snapshot()
out["in_job"] = job_membership(os.getpid())
```

The report suggested recording the *dead* instance; that sample is unobtainable (a terminated process
has no job state to query), so the **live watchdog's own** membership is recorded instead. This is the
more useful signal anyway: if the watchdog itself ever runs inside a `KILL_ON_JOB_CLOSE` job, every
instance it spawns is one handle-close away from dying with it.

Verified live: `memory_snapshot()` now returns `in_job: "none"`, i.e. **the Job Object hypothesis is
re-confirmed by a second, independent method**.

### 12.4 F6 — stale interval: **FIXED (and it was worse than "stale")**

`MossPilotWatchdog` really is `PT1M` (**1 minute**), and it runs as **LocalSystem**
(`S-1-5-18`, `HighestAvailable`) — the "every 5 minutes" in the project docs was wrong. Corrected in
`scripts/pilot_watchdog.ps1`. The correction matters beyond wording: the file's own justification for
its single-instance lock assumed a 5-minute interval made overlap impossible. At **1 minute**, the
interval is *shorter* than `cmd_start`'s worst case (up to ~10 s readiness wait plus environment
self-checks), so the lock is **the only** thing preventing overlapping runs — not a second layer of
safety. Recorded in the file so the reasoning is not re-derived wrongly.

(For the record, the measured intervals are: `MossPilotWatchdog` = `PT1M`;
`MossTunnelWatchdog` = `PT5M`; `MossFrpEnsure` = `PT5M`.)

### 12.5 An editing hazard worth knowing before touching these files

While correcting the comment in `pilot_watchdog.ps1`, an apparently trivial two-line **comment** edit
silently **broke the production watchdog**: the text editor dropped the file's UTF-8 BOM, so
PowerShell 5.1 read the script as GBK, a Chinese string literal swallowed a quote, and the parser
failed at line 56 — cascading into `Missing closing '}'` at lines 37/55/64. The script stopped
executing entirely, and the symptom (`LastTaskResult=1`, **not one line of log**) is
**indistinguishable from the healthy silent path** that this watchdog is designed around.

Caught only by `tests/unit/test_ps1_encoding.py`, which exists precisely for this. After restoring the
BOM: 24/24 guardrail tests pass and **both** watchdogs were re-run end-to-end (exit code 0 each).
A warning was added to the head of `pilot_watchdog.ps1`. **Any edit — including a comment-only edit — to
a non-ASCII `.ps1` must be followed by `python scripts/fix_ps1_bom.py <file>`.**

### 12.6 Status of the report's recommendations

| Fix | Status |
|---|---|
| F1 audit | ✅ Configured + verified as far as possible; **4688 pending reboot** |
| F2 `in_job` field | ✅ Implemented and verified live |
| F3 start via `manage.py start --daemon` | ⏸ **Not done — needs approval.** The running instance writes no `backend.log` (started out-of-band), so every `last_activity`-based timeline is silently wrong. Fixing it requires one restart (a few seconds of 502 for paying users on `hk.wujiaitool.cn`). |
| F4 keep `LocalDumps` | ✅ Left as-is (report advises considering `DumpType=2` / larger `DumpCount`) |
| F5 Job Object | ✅ Nothing to fix; `in_job` now captured automatically |
| F6 watchdog untouched, comments fixed | ✅ Watchdog logic deliberately unchanged; comments corrected |
| F7 ~43 s × 14 concurrent requests | ⏸ **Open, unticketed.** Separate defect and probably the actual user-visible outage. |
