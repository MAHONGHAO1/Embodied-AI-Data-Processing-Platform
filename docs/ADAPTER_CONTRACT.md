# Adapter Contract v1

本文件定义“支持一种输入格式”在 RoboData 里的确切含义。它不是插件宣传页，而是**可测试的接口承诺**：核心导入流程只依赖契约，新增格式不修改 `robodata/importing.py` 或业务阶段编排。

- 契约版本：`1.0`
- 实现：`robodata/adapters/contract.py`
- 注册表：`robodata/adapters/registry.py`
- 描述符 Schema：`robodata/adapters/schema/adapter.schema.json`
- 命令：`robodata adapters list|detect|inspect`、`robodata doctor`

> 兼容性的正确口径不是“所有版本都支持”，而是：**声明支持的版本有契约测试；未知版本能被识别并明确拒绝。**

## 1. 阶段与职责

一次导入固定经过四个契约点：

| 阶段 | 方法 | 职责 | 不允许做的事 |
| --- | --- | --- | --- |
| 探测 | `detect(path)` | 判断输入是否属于本适配器，给出置信度、检测版本和拒绝原因 | 不复制、不下载、不修改输入 |
| 预检 | `preflight(path)` | 给出结构摘要（Episode 数、帧数、相机、版本） | 不做完整字段校验，不构建预览 |
| 托管复制 | `prepare(request, raw, managed)` | 把输入复制/下载到批次管理目录，返回文件记录与解释块 | 不覆盖已有 `raw/`，不跳过固定哈希校验 |
| 预览与清单 | `preview(...)` / `inventory(...)` | 生成只读预览，产出 `source` 与 `episodes` | 不把预览缓存当成源数据的替代品 |

核心层负责：批次目录、`input_fingerprint`、阶段证据、失败诊断、业务门禁。

## 2. 描述符

每个适配器必须提供 `AdapterDescriptor`：

```json
{
  "contract_version": "1.0",
  "adapter_id": "matching_cable",
  "label": "Matching Cable（逐帧 HDF5 目录）",
  "source_kind": "matching_cable",
  "input_kind": "directory",
  "supported_versions": ["matching_cable_frames_v1"],
  "pinned": false,
  "supports_test_label": false,
  "description": "父目录下每个数字子目录是一条 Episode，每个 .hdf5 是一帧。"
}
```

硬约束：

- `adapter_id == source_kind`，因为批次落盘后只记录 `source_kind`；两者不一致会让重启后的批次无法路由。
- `supported_versions` 必须是非空字符串列表；版本名可以带来源前缀（如 `lerobot-v3.0-so100-pinned`）。
- `pinned=true` 表示默认只接受固定 revision/哈希；故障副本必须显式标注（若 `supports_test_label=true`）。
- `input_kind` 只能是 `file` 或 `directory`。

完整 JSON Schema 见 `robodata/adapters/schema/adapter.schema.json`。

## 3. 探测结果的三态

`detect` 不抛异常；它返回一个 `Detection`：

| 状态 | `supported` | 含义 | 调用方行为 |
| --- | --- | --- | --- |
| 支持 | `true` | 结构、版本、固定来源均匹配 | 可以进入 `prepare` |
| 格式认识、版本不支持 | `false` + `detected_version` | 例如 LeRobot v2.1 | 明确拒绝，提示计划支持的版本 |
| 完全不认识 | `false` + `unsupported_reason` | 没有结构证据 | 列出所有候选与原因，不猜 |

`robodata adapters detect` 返回所有候选，不只返回最佳匹配；`--source auto` 只在存在 `supported=true` 时继续，否则以退出码 `3` 结束。

## 4. 内置适配器（v0.14）

| 适配器 | 输入 | 声明版本 | 固定来源 | 故障副本 |
| --- | --- | --- | --- | --- |
| `hdf5` | 单个 `.hdf5/.h5` | `robomimic-hdf5-pinned`、`hdf5-test-copy` | 是 | 允许，必须 `--test-label` |
| `so100` | LeRobot v3.0 目录 | `lerobot-v3.0-so100-pinned`、`lerobot-v3.0-so100-test-copy` | 是 | 允许，必须 `--test-label` |
| `aloha` | LeRobot v3.0 目录 | `lerobot-v3.0-aloha-coffee-pinned` | 是 | 不允许 |
| `matching_cable` | 逐帧 HDF5 目录 | `matching_cable_frames_v1` | 否（本机用户数据） | 不允许 |

明确**不支持**的输入（v0.14）：

- 任意未固定的 LeRobot v3.0 目录（可质检，不可导入）；
- LeRobot v2.0/v2.1 导入（计划 v0.15）；
- ROS2 MCAP（计划 v0.15）；
- 远程 URL、ZIP、对象存储路径。

`robodata check` 与 `robodata import-batch` 的支持范围不同：前者是质检读取器，能读任意 LeRobot v2/v3；后者是**可追溯导入**，只接受有固定来源或明确测试标记的输入。不要用 `check` 能读来推断 `import` 也支持。

## 5. 新增适配器的最小步骤

1. 在 `robodata/adapters/builtin/` 下新增模块，或安装一个提供 `robodata.adapters` entry point 的包。
2. 实现 `SourceAdapter` 的五个方法；`descriptor.adapter_id` 与 `source_kind` 保持一致。
3. 在 `registry.py` 的 `_BUILTIN_MODULES` 注册（内置）或通过 entry point 注册（第三方）。
4. 提交三类测试资产：
   - **正样本**：最小可导入目录/文件，断言 `detect`、`preflight`、`prepare`、`preview`、`inventory` 全链路；
   - **坏样本**：缺字段、错版本、哈希不符，断言明确拒绝而不是半成品；
   - **未知版本**：断言 `detected_version` 与拒绝原因，不静默降级。
5. 更新 `docs/COMPATIBILITY.md` 的支持矩阵。
6. 跑 `uv run --locked pytest -q` 与 `robodata adapters detect`。

一个新适配器如果必须改 `robodata/importing.py` 才能工作，说明契约还不完整；应先补契约，而不是在核心加 `if kind == ...`。

## 6. 版本与弃用策略

- 适配器新增版本：**minor** 发布，补 golden 测试。
- 适配器移除版本：先标记 `deprecated` 至少一个 minor 周期，再移除；移除属于 **major** 变更。
- 已发布数据集的 `source_kind` 与 `format_version` 不因升级而改写。
- `lerobot` 固定版本变化必须同时更新转换环境锁、契约测试和 `docs/COMPATIBILITY.md`。

## 7. 不覆盖的范围

- **Writer/输出格式**：输入适配器不负责 LeRobot 写出；输出契约单独定义。
- **UI 动态发现**：前端当前仍使用固定来源选项；`adapters` 是 CLI/SDK 能力。
- **转换能力**：导入成功不等于转换成功；转换仍按 `source_kind` 选择 writer。
- **自动修复**：适配器只记录来源事实，不补造缺失相机、深度、触觉或数值字段。

## 8. 验收命令

```powershell
uv run --locked robodata adapters list
uv run --locked robodata adapters detect --input <本地输入>
uv run --locked robodata adapters inspect --input <本地输入>
uv run --locked robodata doctor
uv run --locked pytest -q tests/test_adapters.py
```

相关文档：[兼容矩阵](COMPATIBILITY.md)、[无头计划](PROCESS_PLAN.md)、[数据卡与登记](REGISTRY.md)。
