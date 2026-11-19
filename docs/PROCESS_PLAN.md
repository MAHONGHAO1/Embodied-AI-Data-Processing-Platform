# 冻结计划 `process --plan`

`process` 是工业场景的无头入口：把「导入 → 质检 → 清洗确认 → 标注 → 审核 → 转换 → 验证 → 打包 → 登记」写进一份可评审的 JSON/TOML，然后一条命令跑完。

它不是绕过门禁的脚本。`process` 只调用 UI 使用的同一组 `business` 函数；任何一步未通过，都会停在对应阶段并返回非零退出码。

## 1. 命令

```powershell
uv run --locked robodata process --plan work/telecom-500/process-plan.json `
  --business-root work/business-v14 --runs-root work/runs-v14
```

```powershell
# 只校验计划与输入适配器，不启动任何运行
uv run --locked robodata process --plan plan.json --dry-run
```

退出码：

| 码 | 含义 |
| ---: | --- |
| `0` | 全部通过；或 `review.approve=false` 时标注已提交 |
| `2` | 业务拒绝：质检未通过、没有 keep Episode、标注/审核冲突 |
| `3` | 输入没有适配器支持，或计划指定了未知适配器 |
| `4` | 计划文件本身无效 |
| `5` | 运行失败、超时或缺少当前产物 |

`1` 保留给未预期异常；不要把 `1` 当成数据结论。

## 2. 计划结构

```json
{
  "schema_version": 1,
  "input": {
    "path": "C:\\data\\matching_cable",
    "source": "auto",
    "label": "telecom-500-v14",
    "test_label": null
  },
  "annotations": {
    "task_template": "telecom matching cable episode {source_episode_id}",
    "default_outcome": "success",
    "tags": ["telecom", "matching_cable"],
    "overrides": [
      {"episode_index": 2, "outcome": "failure", "notes": "夹取滑落"}
    ]
  },
  "review": {"approve": true},
  "delivery": {
    "package": true,
    "dataset_id": "telecom-matching-cable-500",
    "version": "1"
  },
  "resume": {"batch_id": null},
  "timeout_seconds": 21600
}
```

字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `input.path` | 是 | 本地文件或目录；`source=auto` 时用于适配器探测 |
| `input.source` | 否 | 默认 `auto`；也可显式写 `hdf5` / `so100` / `aloha` / `matching_cable` |
| `input.label` | 是 | 批次名；同名 + 同输入指纹视为同一批次 |
| `input.test_label` | 否 | 仅用于明确标注的故障副本 |
| `annotations` | 是 | 列表或模板对象，必须覆盖所有 keep Episode |
| `review.approve` | 否 | 默认 `true`；`false` 时只提交标注，等待人工审核 |
| `delivery.package` | 否 | 默认 `true`；`false` 时只转换，不打包 |
| `delivery.dataset_id` | 否 | 数据卡与登记表使用的稳定 ID；不填时由 batch + snapshot 派生 |
| `delivery.version` | 否 | 数据卡版本；不填时用 `content_version` |
| `resume.batch_id` | 否 | 从既有批次继续，跳过重新导入 |
| `timeout_seconds` | 否 | 单次运行等待上限，默认 6 小时 |

### 标注的两种写法

**列表**：按 keep Episode 顺序一一对应；也可以每项都写 `episode_index` 显式指定。

```json
"annotations": [
  {"task": "把电缆插入左端口", "outcome": "success", "tags": ["insert"]},
  {"task": "把电缆插入右端口", "outcome": "failure", "notes": "未到位"}
]
```

**模板**：适合整批同任务；`overrides` 按 `episode_index` 覆盖个别集。

可用占位符：`{episode_index}`、`{source_episode_id}`、`{row_count}`。

`outcome` 只接受 `success` / `failure` / `uncertain`。计划不会自动猜结果；模板模式必须显式给 `default_outcome`。

## 3. 幂等与续跑边界

`process` 的恢复分为两层：

- **阶段级幂等**：同一计划重跑时，复用已导入批次；已 approved 的 Episode 不重复标注；当前 revision 已有转换产物和交付 ZIP 时直接返回。
- **预览 Episode 级续跑**：仅 Matching Cable 预览。每集编码完成后写 `preview.units/journal.jsonl`；重启时只复用输入摘要和产物哈希都一致的单位，再合并成最终 v3.0 预览。`source_manifest.json` 记录 `reused_episodes` / `encoded_episodes`。

`format_write`、HDF5/SO-100/ALOHA 转换仍是**阶段级重跑**：中途失败会从该阶段开头重新执行。不要把它写成“全格式断点续跑”。

`process` 可以通过本地队列执行：`robodata jobs submit --plan ...`，再由 `robodata worker --loop` 领取。队列是单机 SQLite，默认并发 1；租约、取消、重试与接管见 [OPERATIONS.md](OPERATIONS.md)。

计划内容变化会改变计划摘要，从而生成新的状态文件。修改标注时请同时换 `input.label` 或数据卡版本，避免把新标注套到旧批次上。

## 4. 结果

`process` 输出 JSON：

```json
{
  "schema_version": 1,
  "status": "passed",
  "exit_code": 0,
  "batch_id": "batch_...",
  "revision": 6,
  "source_kind": "matching_cable",
  "episodes": 1,
  "frames": 500,
  "runs": {"import": "...", "final_quality": "..."},
  "output": {"output_id": "...", "artifact_path": "...", "current": true},
  "delivery": {"delivery_id": "...", "zip_path": "...", "zip_sha256": "..."},
  "message": "导入、标注、转换、验证与交付全部通过",
  "state_path": "work/business-v14/process/<plan-digest>.json"
}
```

`status` 取值：`validated`（dry-run）、`annotated`（等待人工审核）、`passed`、`rejected`、`failed`。

## 5. 与数据卡 / 登记表的关系

- 打包时生成 `dataset/dataset_card.json`，写进交付 ZIP，并由 ZIP 内 `manifest.json` 覆盖其哈希；
- 交付登记阶段校验数据卡的自校验摘要，再写入本地 `registry.sqlite3`；
- 同一 `dataset_id + version` 重复登记且内容不同会**冲突**，不静默覆盖；
- Train 侧应只读数据卡或登记表，不要把 `source_manifest.json` 的内部字段当成稳定 API。

数据卡字段与登记命令见 [REGISTRY.md](REGISTRY.md)。

## 6. 明确不承诺

- 不承诺任意中断后从节点继续；
- 不承诺计划中的标注是“正确”的，只保证它们被完整、可追溯地应用到每个 keep Episode；
- 不自动修复数据、不补造缺失流、不跳过最终质检；
- 不把 `process` 当成调度器；并发、重试和队列是后续能力。
