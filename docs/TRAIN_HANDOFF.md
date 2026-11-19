# Train 侧交接：只读数据卡，再加载数据

Process 对 Train 的契约只有两样东西：

1. `dataset_card.json`：来源、内容、质量结论、转换策略、限制；
2. 数据目录或交付 ZIP：按卡里的相对路径加载。

这不是训练脚本，也不要求 Train 理解 Process 的内部 `source_manifest.json`。

## 一条命令验证交接

```powershell
# 从交付 ZIP 解包、验卡、加载
tools\conversion\.venv\Scripts\python.exe scripts\verify_train_handoff.py `
  --archive work\runs-v14-final\<run>\delivery.zip

# 或直接验证已解压的数据集
tools\conversion\.venv\Scripts\python.exe scripts\verify_train_handoff.py `
  --dataset work\business-v14-final\outputs\<run>\staging `
  --card work\runs-v14-final\<run>\unpacked\dataset\dataset_card.json
```

脚本会：

1. 安全解包 ZIP（拒绝绝对路径、`..`、符号链接）；
2. 读取 `dataset/dataset_card.json` 并重算 `card_digest`；
3. 检查 `quality.final_quality.outcome == passed`；
4. 检查 `quality.official_verify.passed == true`；
5. 用 `lerobot 0.4.4` 官方加载器加载，核对帧数、Episode 数和数据卡声明的相机；
6. 输出 JSON 结论；任何一步失败返回非 `0`。

## 成功输出（节选）

```json
{
  "status": "passed",
  "card_valid": true,
  "dataset_id": "lerobot-v21-demo",
  "version": "1",
  "source_kind": "lerobot_v21",
  "content": {"episode_count": 2, "frame_count": 10},
  "loaded": {"rows": 10, "episodes": 2, "cameras_checked": ["observation.images.camera_0"]}
}
```

## 只读 API

本机服务提供只读登记接口：

```text
GET /api/registry
GET /api/registry/{dataset_id}
GET /api/registry/{dataset_id}/{version}
```

它们只返回登记表与数据卡，不提供写入、删除或训练触发。绝对本机路径会被现有的公开字段过滤掉。

## 边界

- `card_digest` 证明卡没被改，不证明数据适合你的任务；
- `quality` 通过不等于策略训练会成功；
- 不声明硬件同步、任务成功率或生产容量；
- 外部复现记录仍为 **0**；
- 当前是本地单用户 SQLite 登记表，不是多租户 Registry 服务。
