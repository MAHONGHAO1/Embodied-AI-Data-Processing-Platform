# 数据卡与本地登记表

数据后处理的终点不是“生成一个目录”，而是**下游能判断这份数据能不能用**。v0.14 起，每次打包都会生成一张 `dataset_card.json`，并在本地登记表中留下可重算的记录。

## 1. 数据卡

位置：交付 ZIP 内 `dataset/dataset_card.json`；同时被 `manifest.json` 的文件清单覆盖。

```json
{
  "schema_version": 1,
  "dataset_id": "telecom-matching-cable-500",
  "version": "1",
  "app_version": "0.14.0",
  "format": {"name": "LeRobot", "version": "v3.0"},
  "source": {"kind": "matching_cable", "repo_id": "...", "revision": "...", "fps": 20},
  "content": {"episode_count": 1, "frame_count": 500, "tasks": ["..."], "cameras": ["..."]},
  "quality": {"final_quality": {"outcome": "passed"}, "official_verify": {"passed": true}},
  "conversion": {"name": "preview_video_reuse", "source_numeric_consistency_rechecked": true},
  "artifacts": {"dataset": "dataset", "source_manifest": "dataset/source_manifest.json"},
  "evidence": {"source_manifest_sha256": "...", "official_verify_sha256": "..."},
  "limitations": ["不声明相机硬件同步"],
  "card_digest": "..."
}
```

`card_digest` 覆盖除 `created_at` 和自身以外的字段。这样：

- 同样的内容在不同时间打包，摘要一致；
- 任何一个内容字段被改，摘要变化；
- 任何人拿到卡都能重算，不需要信任生成方。

校验：

```powershell
uv run --locked robodata registry verify --card path/to/dataset_card.json
```

它证明的是「卡没被改」，不证明「数据一定适合训练」。适用性仍由 `quality`、`limitations` 和下游任务决定。

## 2. 本地登记表

位置：`<business-root>/registry.sqlite3`。表结构只做索引，不是第二份真相：

```text
datasets(dataset_id, label, created_at)
versions(dataset_id, version, card_digest, card_path, zip_path, batch_id, output_id,
         content_version, source_kind, format_version, artifact_path, created_at)
```

规则：

- 同一 `dataset_id + version` 重复登记且 `card_digest` 相同 → `unchanged`；
- 摘要不同 → 抛冲突，要求换新版本，不覆盖；
- 登记前必须通过 `card_digest` 自校验；
- 交付登记阶段在写业务交付记录前先登记数据卡；登记失败不会留下“已验证交付”的假记录。

## 3. 命令

```powershell
uv run --locked robodata registry list --business-root work/business-v14
uv run --locked robodata registry show --dataset-id telecom-matching-cable-500 --version 1 --business-root work/business-v14
uv run --locked robodata registry verify --card work/runs-v14/<run>/unpacked/dataset/dataset_card.json
uv run --locked robodata registry register --card path/to/dataset_card.json --zip path/to/delivery.zip
```

`process --plan` 会自动完成登记；手动 `register` 用于既有交付包的补登记或跨机转移。

## 4. 与 Train 的交接

Train 侧只需要两样东西：

1. `dataset_card.json`：判断来源、格式、质量结论和边界；
2. 数据目录或交付 ZIP：按卡里的相对路径加载。

建议的消费顺序：

```text
读卡 → 校验 card_digest → 检查 quality.official_verify.passed
     → 检查 quality.final_quality.outcome == passed
     → 读取 limitations → 再决定是否加载
```

不要把 `source_manifest.json` 的完整内部结构当稳定 API；它是 Process 的证据文件，不是 Train 的契约。

## 5. 边界

- 本地单用户 SQLite，不是多租户服务；
- 没有权限、审计、保留策略；
- 数据卡不包含绝对本机路径；
- 登记表不复制数据，只记录索引与哈希；
- 外部复现记录仍为 0；本登记表只证明本机与 CI 跑过的内容。
