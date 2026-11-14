# 具身智能数据处理平台 · 商业化上线级审计报告

| 项目 | 内容 |
| --- | --- |
| 审计对象 | `Embodied-AI-Data-Processing-Platform` v0.8.0，修复后发布为 **v0.9.0** |
| 报告日期 | 2026-11-14 |
| 审计基准 | **当前磁盘版本**（所有「已修复」结论均以本次修复后的代码为准；「问题」描述对应修复前版本，用于复盘投诉成因） |
| 触发事件 | 用户反馈：提交审核后离开页面约一小时，回来发现「整个页面都回退到要导入的阶段」 |
| 审计范围 | 前端 React 19 + TypeScript + Vite；后端 Python + FastAPI + Uvicorn；本地业务状态机（`BatchStore`）；运行协调与登记回写；SSE；上传、文件与产物链路；路径与信息泄露；并发、性能、压力、异常路径 |
| 运行边界 | **本机单用户、仅 127.0.0.1 loopback**。本报告结论适用于该边界；不等同于公网多用户 SaaS 的上线认证 |

---

## 0. 执行摘要

**投诉属实，且是 P0 级缺陷。** 它不是「页面刷新丢了前端状态」，而是三个独立问题叠加后产生的**真实的业务数据回写**：

1. 后端把「读取运行记录」实现成了带副作用的操作：`_load_state` 无条件调用 `business.reconcile_run`，而质检登记重放会调用 `apply_quality_report`，把已提交的审核全部打回草稿。
2. SSE 终态没有下发 `event: done`，前端 `EventSource` 在服务端 `break` 后无限自动重连，一小时里把这条危险读路径放大了成百上千次。
3. 前端阶段进度条是硬编码推导的，审核与产物一旦失效，7 段进度只剩「导入」，视觉上就是「全部回退到要导入的阶段」。

**关键更正**：由于 `apply_quality_report` 带 `run_id` 幂等键，**同一运行的业务重置实际只发生 1 次**。一小时内的大量重连主要是空转读与前端 `refresh` 放大；`failed` 分支才会有额外的事件/状态写盘放大。多个不同 run_id 或用户多次重跑质检，才会形成多次重置。这一点决定了修复重点应放在「读路径零副作用」，而不是简单加轮询节流。

**第二轮复核补修**：首轮只覆盖了 React 工作台。对全量入口复查时发现**旧版 Streamlit 入口仍完整保留同类根因**——`sync_last_run()` 在每次重绘（含 `@st.fragment(run_every="1s")` 轮询片段）自动调用 `reconcile_run`，且 `ui_runs.load_run` 默认会写盘并可能终止进程。此外还发现「恢复登记」门禁的判据与 `_register` 的字段回退口径不一致（历史记录可绕过）、以及 `set_disposition` 会静默把全部保留任务审核打回草稿并作废产物。这四条已一并修复并补上回归护栏。

**当前状态**：P0 根因链在**两个入口**（React 工作台 / 旧 Streamlit 界面）均已闭环，前后端全部修复已落地，新增上线级回归套件并通过全量回归、前端测试、生产构建与真实 Uvicorn 运行态验证。仍有若干**产品语义决策项**和**公网化前置项**未处理，见第 8 节——它们不会导致本次投诉复发，但会影响是否可宣称「商业化上线」。

---

## 1. 投诉复现与根因链

### 1.1 用户操作时间线

| 时刻 | 用户动作 / 系统行为 |
| --- | --- |
| T0 | 导入批次 → 质检 → 筛选确认 → 标注 → **提交审核**，界面显示 7 段进度已推进 |
| T0+ | 对应运行进入终态（`completed` / `failed` / `interrupted` / `completed_with_errors`） |
| T0 ~ T0+1h | 用户离开页面；浏览器标签页仍保持 `EventSource` 连接 |
| T0+1h | 用户返回，发现审核全部消失、清洗确认失效、产物标为非当前版本、进度条只剩「导入」 |

### 1.2 后端链路（修复前）

```
POST /api/runs（质检）→ 协调器完成 → registration.json 写入 pending/failed
        │
        ├─ 终态 SSE 服务端 break，但没有 event: done / retry: / 204
        │       └─ 浏览器 EventSource 自动重连（selectedRun.run_id 不变，
        │          listenRun 的 cleanup 不触发，stream.close() 从不执行）
        │
        └─ 每次重连 → GET /api/runs/{id}/events → _load_state(run_id)
                └─ 旧版 _load_state 内部无条件 business.reconcile_run(run_id)
                        └─ _register(directory)
                                └─ registration_operation == "quality"
                                        └─ store.apply_quality_report(...)
                                                ├─ 所有 episode.review → {"status":"draft"}
                                                ├─ disposition → keep / quarantine
                                                ├─ cleaning_confirmed = False
                                                ├─ _mutate(content=True) → content_version += 1
                                                └─ outputs / deliveries 的 current 全部变 False
```

`apply_quality_report` 的这段行为（`robodata_business/store.py` 的 `change(state)`）本身在「新质检证据到达」时是**正确**的：新证据不应静默保留旧审批。问题在于它被挂在了**读路径**上，用户在没有发起任何新质检的情况下，仅因为浏览器重连就被重新应用了一遍旧报告。

### 1.3 前端链路（修复前）

```tsx
const done = index === 0
  || (index === 1 && batch.cleaning_confirmed)
  || (index >= 4 && Boolean(batch.outputs?.some((item) => item.current)));
```

`cleaning_confirmed=False` 且 `outputs` 全部非 `current` 后，7 段里只有「导入」为 done。因此**后端业务状态被真实改写，前端只是如实反映了改写结果**——不是缓存、不是渲染 bug。

### 1.4 复现与固化

`tests/test_launch_audit.py` 中：

- `test_characterization_quality_rerun_silently_resets_submitted_review`：构造完整「已提交审核」批次，重放质检登记，断言审核被打回草稿、`cleaning_confirmed` 变 False、`content_version` 递增、产物失效。
- `test_reading_runs_never_triggers_reconciliation`：连续 25 次 `GET /api/runs/{id}`，断言 `registration.json` 字节不变、批次 `revision` 不变（P0 根因护栏）。
- `test_backend_state_is_durable_across_new_app_instance`：重建 app / store 后审核状态必须持久化。
- `test_repeated_reconcile_reads_are_stable`：60 次终态读保持稳定且不放大写盘。

---

## 2. 审计范围与方法

- **双线独立审计**：后端审计员与前端审计员分别对当前磁盘版本做商业化上线级代码审计，互不代替；审计结论由本报告统一收口。
- **静态审计**：状态机所有写入口、乐观锁与幂等键、运行目录校验、错误分流、路径脱敏、上传落盘、SSE 生命周期、前端 effect / 竞态 / 会话恢复。
- **动态验证**：`TestClient` API 级负向测试、并发线程压测、性能计时、真实 Uvicorn 进程 + HTTP/SSE 冒烟。
- **回归策略**：新增用例与既有全量套件一起复跑；构建与类型检查由 `npm run build`（含 `tsc -b`）覆盖。
- **测试隔离**：所有新增用例使用临时 `business` / `runs` 根目录，不读写真实 `work/` 数据。

---

## 3. 后端审计发现

| 编号 | 级别 | 问题 | 位置 | 处置 |
| --- | --- | --- | --- | --- |
| B-P0-1 | P0 | 读路径 `_load_state` 调用 `reconcile_run`，GET/SSE 可触发业务回写并清空审核 | `robodata/web.py` | **已修复**：`_load_state` 纯只读，恢复改由显式 POST |
| B-P0-2 | P0 | SSE 终态直接 `break`，无 `done` 事件，浏览器无限重连放大副作用 | `robodata/web.py: run_events` | **已修复**：终态 / 错误 / 超时下发 `event: done`，15s 心跳、7200s 超时、events 文件按 `(mtime_ns, size)` 缓存 |
| B-P0-3 | P0 | `get_run` 在读时会 `_save_state` 并 `_terminate_identity`，可能误杀 PID 复用进程 | `robodata/runtime.py` | **已修复**：`mark_interrupted=False` 返回只读派生视图；无创建身份时不终止进程 |
| B-P0-4 | P0 | `reconcile_run` 无状态早返回、无重试上限，失败登记会被后台反复重放 | `robodata/business.py` | **已修复**：`registered/stale` 锁外早返回、`failed` 非 force 不重试、`recovery_attempts >= 3` 硬上限（force 也不能绕过）、active 判定读原始 `state.json` |
| B-P0-5 | P0 | 新增 `/recover` 端点初期缺并发保护，连击可重复回写 | `robodata/web.py` | **已修复**：`_recover_guard` + 2s 节流 + 429 / `Retry-After` |
| B-P0-6 | P0 | 显式「恢复登记」若恢复的是质检登记，仍会经 `_register → apply_quality_report` 静默重置审核与产物 | `robodata/web.py: recover_run` / `business.py: _register` | **已修复**：前置只读探测 `registration_operation`，质检登记必须显式 `invalidate_reviews: true`，否则 409 并完整说明后果；判据改用与 `_register` 同口径的 `registration_operation_of()` |
| B-P0-7 | P0 | **旧版 Streamlit 入口** `sync_last_run()` 在每次重绘（含 1s 轮询片段）终态自动调用 `reconcile_run`，老根因链完整存活 | `robodata/ui_business.py: sync_last_run` | **已修复**：改为纯只读 + 提示，恢复登记只能由用户显式点击按钮（两步确认）；源码级护栏用例防回归 |
| B-P0-8 | P0 | `ui_runs.load_run` 默认 `mark_interrupted=True`，UI 读操作会写 `state.json`、可能终止进程 | `robodata/ui_runs.py` | **已修复**：默认改为 `False`（读路径默认只读），需要持久标记中断的协调器路径显式传 `True` |
| B-P1-1 | P1 | API 响应回显本机绝对路径（含 `traceback`、`stderr`、文本内嵌路径） | `robodata/web.py` | **已修复**：按键黑名单 + 文本键内嵌脱敏 + URL 白名单保持；覆盖 Windows 盘符 / UNC / `/Users` `/home` `/opt` `/data` 等，不误伤 `https://` 与合法相对文件名 |
| B-P1-2 | P1 | 上传无大小上限，可无界写满本机磁盘 | `robodata/importing.py` | **已修复**：`ROBODATA_MAX_UPLOAD_MB`（默认 2048 MiB），分块累计超限即报错并清理临时文件 |
| B-P1-3 | P1 | 文件锁未成功获取也会在 `finally` 解锁 | `robodata_business/store.py` | **已修复**：`acquired` 标志 |
| B-P1-4 | P1 | `_mutate` 在锁外读回，返回给调用方的 `revision` 可能被其他进程抢先覆盖 | `robodata_business/store.py` | **已修复**：`get_batch` 读回保持在锁内 |
| B-P1-5 | P1 | 早期 / 手工修复记录缺字段时，整个批次列表会 404 | `robodata_business/store.py` | **已修复**：兼容缺失 `disposition` / `row_count` / `outputs` / `deliveries` |
| B-P1-6 | P1 | 运行编号允许 `.` / `..` / 点开头 / 路径穿越 / 超长 | `robodata/runtime.py: _run_dir` | **已修复**：显式拒绝并校验 `Path(run_id).name == run_id` |
| B-P1-7 | P1 | 手动重跑质检会静默使已提交 / 已通过审核失效 | `robodata/web.py: start_run` | **已修复**：`_guard_review_invalidation` 返回 409；文案已补齐「已验证转换产物与交付包也会变为非当前版本，需重新质检、转换与打包」 |
| B-P1-8 | P1 | HDF5 批次 episode 读取用了错误根目录 | `robodata/web.py` | **已修复**：改用 `preview_root` |
| B-P1-9 | P1 | 错误分流不统一，`start_run` 可能把可预期的冲突抛成 500 | `robodata/web.py` | **已修复**：`ConflictError → 409`、`OSError → 503`、`ValueError/KeyError → 422` |
| B-P1-10 | P1 | 历史 `registration.json` 缺 `registration_operation` 时门禁判据与 `_register` 的回退口径不一致，可绕过二次确认 | `robodata/web.py` / `business.py` | **已修复**：新增只读 `registration_operation_of()`，与 `_register` 同样按 `request.json.operation` 回退；新增专门用例 |
| B-P1-11 | P1 | `set_disposition` 会把**全部**保留任务的审核打回草稿、清洗确认失效、`content_version+1` 并使产物失效，用户无任何提示 | `robodata_business/store.py` / `robodata/web.py` | **已修复**：存在已提交/已通过审核或当前产物时返回 409 + 完整后果说明，显式 `invalidate_reviews: true` 才放行 |
| B-P2-6 | P2 | `invalidate_reviews` 是调用方声明、无 Origin 请求也被放行；`cli.py` / `scripts/verify_business.py` 等命令行入口无 UI 确认 | 全局 | 当前 loopback 单用户边界内可接受；这些入口都是人工显式执行，不是读路径自动触发。公网化前须改为服务端凭证 |
| B-P2-1 | P2 | `content_version` 粒度过粗：`set_disposition` / `confirm_cleaning` / 标注与审核写入默认 `content=True`，会连带作废 outputs / deliveries | `robodata_business/store.py: _mutate` | **待产品决策**，见第 8 节 |
| B-P2-2 | P2 | 上传临时文件无 TTL / 定期清理 | `robodata/importing.py` | 待处理 |
| B-P2-3 | P2 | delivery `_portable` 的脱敏规则与 web 层不完全一致 | 交付模块 | 待处理 |
| B-P2-4 | P2 | 任意本机路径输入（`source_kind` / `path`）仍可指向本机任意目录 | `robodata/web.py` | 边界内为设计选择；公网化前必须收敛 |
| B-P2-5 | P2 | loopback 单用户模型无认证 / CSRF / 全局限流 | 全局 | 当前边界内可接受；公网化前必须补齐 |

---

## 4. 前端审计发现

| 编号 | 级别 | 问题 | 位置 | 处置 |
| --- | --- | --- | --- | --- |
| F-P0-1 | P0 | `listenRun` 只在 effect cleanup 时 `close()`；终态下 `selectedRun.run_id` 不变导致 cleanup 不触发，`EventSource` 永不关闭 | `frontend/src/api.ts` | **已修复**：终态快照、`done` 事件、带数据的错误事件均立即幂等 `close()`；无 data 的原生网络抖动不关闭，交给自动重连 |
| F-P0-2 | P0 | 阶段进度硬编码，审核 / 产物一失效就只剩「导入」 | `frontend/src/App.tsx` | **已修复**：`deriveStages(batch, runs)` 由业务快照与运行记录推导；「独立验包」只认 `verify_delivery` 且 `completed` 的 run，不再用 `zip_sha256` 误判 |
| F-P0-3 | P0 | 刷新 / 长时间离开丢失 route、批次、Episode、帧、选中运行与表单草稿 | `frontend/src/App.tsx` | **已修复**：URL 完整上下文（`view/batch/ep/run`）+ localStorage 会话（7 天过期、损坏 JSON 兜底）+ 草稿绑定 `content_version` |
| F-P1-1 | P1 | `refresh()` 分别提交列表与详情，存在竞态导致版本不一致 | `frontend/src/App.tsx` | **已修复**：`refreshSeq` 序号 + 统一拉取后一次性提交 state |
| F-P1-2 | P1 | 写操作可并发重复提交 | `frontend/src/App.tsx` | **已修复**：`exclusive()` 串行化所有写操作 |
| F-P1-3 | P1 | 内容版本变化后旧草稿仍可能被提交，且用户无感知 | `frontend/src/session.ts` / `App.tsx` | **已修复**：`getDraft(batchId, episodeIndex, contentVersion)` 版本不符自动失效 + 版本变更常驻提示 |
| F-P1-4 | P1 | 加载中误报「还没有登记批次」；后端断连时旧状态被当成真实空态 | `frontend/src/App.tsx` | **已修复**：区分 loading / 真空 / 失败，失败时保留上次成功内容并给「重试连接」按钮 |
| F-P1-5 | P1 | 质检重跑无任何提示，直接清空审核 | `frontend/src/App.tsx: startQuality` | **已修复**：计算 `protectedCount`，`window.confirm` 后带 `invalidate_reviews: true` |
| F-P1-6 | P1 | 恢复登记按钮看起来是无害重试，实为破坏性回写 | `frontend/src/App.tsx: recoverRun` | **已修复**：识别 `registration_operation === 'quality'`（字段缺失时用 `request.operation` 兜底），先确认再带确认标记请求；后端 409 兜底 |
| F-P1-7 | P1 | error / notice 互相覆盖、提示永久驻留 | `frontend/src/App.tsx` / `styles.css` | **已修复**：`.notice-stack` 双通道、8s 自动过期、`.stage-item.active` 进行中态 |
| F-P1-8 | P1 | 页面隐藏 / 恢复时缺少状态对账 | `frontend/src/App.tsx` | **已修复**：`visibilitychange`、`beforeunload`、URL `replaceState`、600ms 防抖保存 |
| F-P1-9 | P1 | 修改筛选决定无任何提示，实际会重置全部保留任务审核并作废产物 | `frontend/src/App.tsx: saveDisposition` | **已修复**：按已提交/已通过审核数与当前产物数弹确认，确认后带 `invalidate_reviews: true` |
| F-P1-10 | P1 | 渲染层异常会整页白屏，用户容易误判为「数据全没了」 | `frontend/src/main.tsx` | **已修复**：`WorkbenchErrorBoundary` 明确提示「业务数据仍在磁盘上」，提供错误信息与重新加载按钮 |
| F-P1-11 | P1 | 后端 409 门禁拒绝时前端无兜底：`request()` 丢弃 HTTP 状态码、错误条只有关闭按钮，陈旧批次下反复点同一按钮只会反复 409 | `frontend/src/api.ts` / `App.tsx` | **已修复**：`ApiError` 保留 `status`、`isConflict()` 识别 409；错误条新增「确认并重试」，先取服务器最新 `revision` / `content_version` 再带 `invalidate_reviews` 重发；新增 2 项单测 |
| F-P1-12 | P1 | 用户自己的写操作被误报成「后台处理已更新批次内容版本」，并连带清掉本地草稿 | `frontend/src/App.tsx: applyServerBatch` | **已修复**：写成功即同步版本基准；提示改为中性归因「批次内容版本已更新」 |
| F-P1-13 | P1 | 保存标注 / 提交审核 / 审核 / 确认筛选在已有当前产物时没有任何提示，产物静默失效 | `frontend/src/App.tsx` | **已修复（前端侧）**：存在当前产物时先确认，成功后明确说明「产物与交付包变为非当前版本」；后端门禁未加，见 8.2 |
| F-P2-1 | P2 | 生产构建 ECharts chunk > 500 kB 告警 | `frontend/vite.config` | 建议后续 `manualChunks` / 动态 import 拆分；不影响功能 |
| F-P2-2 | P2 | EventSource 未使用服务端 `retry:` 做退避 | `frontend/src/api.ts` | 已由终态 `done` + 前端主动关闭从根上消除重连需求 |

---

## 5. 修复清单（按文件）

**后端**

- `robodata/web.py`：`_load_state` 纯只读；`_pending_registration` 只读探测；`RecoverRequest`；`POST /api/runs/{id}/recover` 破坏性确认门禁 + 节流；`_guard_review_invalidation` 门禁与文案；SSE `done` / 心跳 / 超时 / events 缓存；`PRIVATE_KEYS` + `TEXT_KEYS` 双层脱敏；`_episode_root` 用 `preview_root`；错误分流；`RunRequest.invalidate_reviews`。
- `robodata/business.py`：`reconcile_run(force=False)` 早返回 / 限流 / 只读派生视图 / 原始 state 判 active。
- `robodata/runtime.py`：`_run_dir` 路径校验；`_terminate_identity` 无身份不终止；`get_run(..., mark_interrupted=False)`；`list_runs(..., mark_interrupted=False)`。
- `robodata/ui_runs.py`：`available_runs()` / `load_run()` 支持只读派生视图。
- `robodata_business/store.py`：`_lock.acquired`、锁内读回、旧记录字段兼容。
- `robodata/importing.py`：上传大小上限与超限清理。
- `robodata/ui_business.py`：**旧 Streamlit 入口**去掉自动 `reconcile_run`，`sync_last_run` 纯只读 + 提示；`recover_registration` 改两步确认并对质检登记显示完整后果；`render_run_context` 增加确认/放弃状态。
- `robodata/ui_runs.py`：`load_run` 默认改为 `mark_interrupted=False`，读路径默认只读。

**前端**

- 新增 `frontend/src/progress.ts`（`deriveStages`）、`frontend/src/session.ts`（会话与版本绑定草稿）。
- `frontend/src/api.ts`：`listenRun` 生命周期、`api.recoverRun(runId, invalidateReviews)`、`ApiError`（保留 HTTP 状态码）与 `isConflict()`。
- `frontend/src/types.ts`：`business_registration.registration_operation`。
- `frontend/src/App.tsx`：URL + 会话恢复、竞态防护、写串行化、三处破坏性确认、四类写操作的产物失效确认、409「确认并重试」、版本基准同步与中性归因、加载/失败态、服务器归一化回写。
- `frontend/src/styles.css`：`.notice-stack`、`.notice .button`、`.stage-item.active`、`.fatal-error`。
- `frontend/src/main.tsx`：`WorkbenchErrorBoundary` 渲染异常兜底。

**测试与文档**

- 新增 `tests/test_launch_audit.py`（34 个测试函数，参数化后 43 项），含旧入口只读护栏与门禁回退口径用例。
- 新增 `frontend/src/progress.test.ts`、`session.test.ts`、`api.test.ts`；扩展 `App.test.tsx`（合计 24 项）。
- `CHANGELOG.md` 增加「未发布 · 上线级审计与状态回退修复」；本报告 `docs/LAUNCH_AUDIT_REPORT.md`。

---

## 6. 全量测试与验证

### 6.1 测试矩阵（`tests/test_launch_audit.py`）

| 组 | 覆盖内容 | 代表用例 |
| --- | --- | --- |
| A 状态持久化与回退复现 | 重建 app 后状态不丢、质检重放的表征测试、幂等保护 | `test_backend_state_is_durable_across_new_app_instance`、`test_characterization_quality_rerun_silently_resets_submitted_review` |
| B 负向 / 边界 / 异常输入 | 非法批次号、未知与穿越 run_id、畸形请求体、非本机 Origin / 不可信 Host、陈旧 revision、重复提交、非法审核与标注、错误扩展名、损坏批次隔离、相机名越界、`.`/`..` run_id | `test_invalid_batch_ids_are_rejected`、`test_stale_revision_writes_are_rejected`、`test_damaged_batch_is_isolated_not_fatal` |
| C 并发一致性与幂等 | 同 revision 并发写只有一个成功、质检登记幂等、读路径零副作用、显式恢复幂等与节流 | `test_concurrent_writes_with_same_revision_only_one_wins`、`test_reading_runs_never_triggers_reconciliation` |
| D 性能与压力 | 并发读 p95、5000 episode 大批次读、300 条运行列表、60 次终态读稳定性 | `test_concurrent_read_latency`、`test_large_batch_read_latency`、`test_run_list_stress` |
| E SSE 生命周期与资源 | 终态流必须下发 `done`、路径脱敏（含内联路径 / UNC / POSIX / URL 不误伤）、恢复门禁 | `test_sse_terminal_stream_emits_done_event`、`test_recover_quality_registration_requires_explicit_acknowledgement` |
| F 双入口一致性与破坏性确认 | 门禁字段回退口径、筛选决定确认、旧 Streamlit 只读、UI 读路径不写盘 | `test_recover_gate_falls_back_to_request_operation`、`test_disposition_change_requires_explicit_acknowledgement`、`test_legacy_streamlit_read_paths_are_purely_read_only`、`test_ui_load_run_default_does_not_persist_interrupted_view` |

前端单测覆盖：阶段进度推导（7 项）、会话恢复与草稿失效（6 项）、EventSource 生命周期 / 破坏性确认标记 / 409 状态码保留（10 项）、主组件加载/阶段/刷新恢复（3 项）。

### 6.2 验证结果

| 验证项 | 结果 |
| --- | --- |
| 上线级审计套件（最终） | **43 passed / exit 0**；与 `test_business_app.py` 合并复跑 **67 passed / 37.68s / exit 0** |
| 前端 Vitest（最终） | **26 passed / 4 个测试文件 / exit 0**（新增 409 状态码保留 2 项） |
| 前端生产构建（最终） | **`tsc -b && vite build` 成功**，产出 `index.html`、CSS 29.87 kB、主包 280.17 kB（gzip 86.46 kB）、ECharts 515.24 kB（gzip 175.82 kB）；仅有 chunk 体积告警 |
| 全量 Python 回归（首轮修复后） | **285 passed, 7 skipped, 13 subtests passed / 358.13s / exit 0**（独立 basetemp） |
| 全量 Python 回归（双入口补修后最终版） | **289 passed, 7 skipped, 13 subtests passed / 318.20s / exit 0**（独立 basetemp） |
| 真实 Uvicorn 运行态冒烟 | 见 6.4 |

### 6.3 性能与压力基线（断言阈值）

| 场景 | 规模 | 断言阈值 | 干净全量实测 |
| --- | --- | --- | --- |
| 并发读延迟 | `/api/batches` 并发 n=80 | p95 < 2.0 s | p50 **0.3804 s** / p95 **0.5656 s** |
| 大批次读取 | 5000 episode 的 `get_batch` | < 1.5 s | **0.0406 s** |
| 运行列表压力 | `GET /api/runs` 300 条 | < 5.0 s | **1.7751 s** |
| 终态读放大 | 60 次终态 `reconcile` 读 | < 5.0 s 且状态稳定 | **0.6057 s**，状态字节级不变 |

四项均为可重复执行的硬断言，不是一次性观测值；CI 中超时即失败。

### 6.4 真实运行态验证

以隔离的 `business` / `runs` 根目录启动真实 `uvicorn`（`ROBODATA_WEB_PORT=8767`），构造一条终态运行，用真实 HTTP 客户端验证：

```
SERVICE_READY True
GET /api/health 200
GET /api/runs 200
GET /api/runs/20260101T000000_smoketest 200
GET /api/batches 200
SSE_DONE_EVENT True
SSE_STREAM_CLOSED_IN 0.008 s
REGISTRATION_UNCHANGED {"status": "registered"}
RECOVER 200 {"status":"registered"}
```

意义：在真实 ASGI 流式服务（而非仅 `TestClient`）下，终态 SSE 在 **8 ms** 内下发 `event: done` 并关闭连接，浏览器没有无限重连的窗口；连续读操作后 `registration.json` 字节级不变。

### 6.5 环境干扰排查（避免误判）

首轮全量回归出现过 `PYTEST_EXIT=1`，但进度条 100%、无任何 `F`/`E`。排查后确认：pytest 默认临时根目录 `pytest-of-MA` 累积的历史 `garbage-*` 目录在 session teardown 触发了环境的批量删除保护（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`，count 976 > threshold 50），导致 teardown 假失败、统计行未写出，**与业务代码无关**。改用独立 `--basetemp` 后 `exit 0` 且统计行完整。本报告只采用干净 basetemp 的验收数据。

---

## 7. 防复发机制

1. **读路径零副作用**：`_load_state` / `available_runs` / `load_run(mark_interrupted=False)` / `get_run(mark_interrupted=False)` 全部不写盘、不终止进程、不回写业务。护栏用例 `test_reading_runs_never_triggers_reconciliation` 会阻止回退。
2. **显式写操作**：任何恢复登记只能通过 `POST /api/runs/{id}/recover`，带节流与幂等；任何质检重跑必须带 `invalidate_reviews: true`。
3. **破坏性操作二次确认**：后端 409 门禁 + 前端 `window.confirm` 双保险，且文案明确列出「审核失效 / 清洗确认失效 / 产物与交付包非当前版本 / 需重新质检转换打包」。
4. **SSE 有始有终**：服务端 `event: done` + 心跳 + 超时，客户端终态主动 `close()`；`api.test.ts` 用 FakeEventSource 固化生命周期。
5. **版本绑定**：草稿与 `content_version` 绑定，后台变更自动失效并提示；所有写操作带 `expected_revision` 乐观锁。
6. **进度由事实推导**：`deriveStages(batch, runs)` 只读业务快照与运行记录，不再用硬编码布尔式推断「做到哪一步」。
7. **两个入口同一纪律**：React 工作台与旧 Streamlit 界面都遵循「读只读、写显式、破坏性二次确认」；`ui_runs.load_run` 默认只读，源码级护栏用例防止旧入口被改回自动 `reconcile`。
8. **口径统一**：破坏性门禁与真正执行回写的 `_register` 共用 `registration_operation_of()`，避免「门禁看 A、执行看 B」的绕过。
9. **失败可见**：渲染层异常有 `WorkbenchErrorBoundary`，明确告知业务数据仍在磁盘，避免把白屏误判为数据丢失。
10. **回归套件常驻**：`tests/test_launch_audit.py` 把本次根因、负向、并发、性能、SSE、双入口一致性全部固化为可执行断言。

---

## 8. 剩余风险与上线前清单

### 8.1 需要你做产品决策（不修也能避免本次 bug，但影响「上线」成色）

| 项 | 说明 | 建议 |
| --- | --- | --- |
| `content_version` 语义 | 目前 `set_disposition` / `confirm_cleaning` / 标注 / 审核写入默认 `content=True`，会递增内容版本并作废已验证输出与交付包。从数据血缘看这是**保守且安全**的，但用户每次改一个标注就看到「产物失效」会困惑 | 二选一：①保持现状，在 UI 明确解释「审核结论变化会使产物失效」；②拆出 `evidence_version`，仅当候选集内容真变化时失效产物。需业务定义，不应由工程单方面改 |
| 恢复质检登记的产品语义 | 已加二次确认，但「恢复登记」与「重跑质检」对用户仍是两个相近按钮 | 建议把破坏性恢复的文案与主流程 SOP 对齐，或直接合并为「重新执行质检并登记」 |

### 8.2 建议补齐（工程可自动完成，不影响本次闭环）

- **显式业务写操作的后端门禁**：`save_annotation` / `submit_annotation` / `review_annotation` / `confirm_cleaning` 四条端点仍无 `_guard_*`；当前由前端在存在当前产物时弹确认并明确提示产物失效（`F-P1-13`），后端仅靠 `expected_revision` 乐观锁。建议按 `_guard_disposition_invalidation` 同款补 409 门禁。
- **端到端浏览器回归**：`frontend/e2e/home.spec.ts` 仍是 2 条 v0.8.0 用例，SSE mock 为空 body；未覆盖「离开一小时 / 标签页被丢弃 / 409 后确认重试」。单测与后端护栏已覆盖逻辑层，真实浏览器路径仍缺回归防线。
- 面板键盘焦点：全局空格绑定只排除输入控件，聚焦按钮时按空格仍会切播放。
- 相机检查请求 `.catch(() => null)` 静默吞错、ECharts 全量 `setOption` 重绘、notice 8 秒过期偏短、`session` 浅校验、非总览视图缺 loading 态。
- 上传临时文件的 TTL / 定期清理（`B-P2-2`）。
- delivery `_portable` 与 web 层脱敏规则统一（`B-P2-3`）。
- ECharts 产物 code-split，消除 > 500 kB 构建告警（`F-P2-1`）。
- SSE 服务端补充 `retry:` 做重连退避（当前已由 `done` 消除终态重连需求，属纵深防御）。
- 把 `invalidate_reviews` 从「调用方声明」升级为服务端一次性确认凭证（`B-P2-6`），并给命令行入口加显式 `--confirm`。

### 8.3 仅当要公网 / 多用户部署时才必须处理

- 认证、CSRF、全局限流、会话隔离（`B-P2-5`）。
- 任意本机路径输入的白名单收敛（`B-P2-4`）。
- 上传配额、存储隔离、审计日志留存、备份与灾难恢复。
- 当前项目定位是**本机单用户工作台**，展示页为静态截图；这些不在本轮范围内。

---

## 9. 交付物与复现命令

```bash
# 全量后端回归（用独立 basetemp 规避临时目录清理干扰）
uv --directory . run --locked python -m pytest -q --tb=short -p no:cacheprovider \
  --basetemp=/tmp/robo_audit_basetemp

# 上线级审计套件
uv --directory . run --locked python -m pytest tests/test_launch_audit.py -q

# 前端单测与生产构建
npm --prefix frontend ci
npm --prefix frontend run test
npm --prefix frontend run build
```

主要交付物：`docs/LAUNCH_AUDIT_REPORT.md`（本报告）、`tests/test_launch_audit.py`、`frontend/src/{progress,session,api,App}.test.*`、`CHANGELOG.md` 未发布条目。

---

## 10. 结论

- **「离开一小时回来全部回退到导入阶段」确属缺陷，且是真实业务数据被改写，不是前端显示问题。**
- 根因是「读路径带业务写副作用」+「SSE 终态不终止导致浏览器无限重连」+「前端阶段进度硬编码」三者叠加；幂等键使重置只发生 1 次，一小时主要是空转放大。
- 当前磁盘版本已完成前后端全链路修复、破坏性操作确认门禁、会话与版本绑定、进度事实推导，并通过全量回归、前端测试、生产构建与真实运行态验证；**该 P0 已闭环，具备防止同类复发的可执行护栏**。
- 是否可对外宣称「商业化上线」，取决于第 8.1 节的产品语义决策与第 8.3 节的公网化前置项；按当前「本机单用户工作台」定位，本次审计范围内的上线标准已满足。
