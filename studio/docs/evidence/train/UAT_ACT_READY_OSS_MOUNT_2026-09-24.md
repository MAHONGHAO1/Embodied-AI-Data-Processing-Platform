# UAT 训练预检失败根因与 OSS READY 数据挂载

Date: 2026-09-24  
Site: [studio-uat.quicrobot.xyz](https://studio-uat.quicrobot.xyz/)

## 现象（向导报错）

1. 数据版本不是 READY 状态 / 状态为 REGISTERED，需 READY 后方可调度  
2. ACT 缺少 `action_dim`  
3. ACT 缺少 `state_dim`

## 系统分析

当前向导默认用的是 Catalog 登记集 `dsv_d812c0a85701b958e1e1`（`catalog-2` / export=6）：

| 属性 | 值 | 影响 |
| --- | --- | --- |
| URI | `…/dataset.tar.gz` | 归档不可物化 → **只能 REGISTERED，不能 READY** |
| features | 仅 `observation.images.head` + index 类字段 | **无 `action` / `observation.state`** |
| `action_dim` / `state_dim` | 0 | ACT 策略校验 `POLICY_FEATURE_MISSING` |

这是 **EGO 视频样本**，不是模仿学习动作轨迹；与 ACT 训练契约不匹配。不是前端误报，而是数据面交付形态与模型需求不一致。

Catalog 桥接故意：`status=REGISTERED` 且 `request_materialization=False`；物化器也拒绝直接把 `.tar.gz` 标为 READY。

## 已挂载的合适 OSS 数据

在 UAT 训练库 `prj_other` 登记 **目录型** QuicTrain PushT 烟测集（与 Quota4090 OSS 镜像前缀一致）：

| 字段 | 值 |
| --- | --- |
| id | `dsv_3a4f2567dd74fc428ab6` |
| dataset_id | `quictrain_pusht_smoke` |
| name | PushT OSS 镜像烟测集（ACT READY） |
| status | **READY** |
| uri | `oss://oss-pai-1183v1b6du4vkucj3h-cn-beijing/quictrain/datasets/lerobot/quictrain-pusht-smoke-v1` |
| action_dim / state_dim | **2 / 2** |
| camera | `observation.image` |

OSS 上已确认存在 `meta/info.json`（ECS RAM Role 可读）。

## 预检对比

| 数据集 | `jobs/validate` ACT×`act-4090-standard` |
| --- | --- |
| PushT READY `dsv_3a4f2567dd74fc428ab6` | **`valid=true`，issues=[]** |
| Catalog tar `dsv_d812c0a85701b958e1e1` | `valid=false`：`DATASET_NOT_READY` + `POLICY_FEATURE_MISSING`×2 |

## 页面操作

打开 [studio-uat.quicrobot.xyz](https://studio-uat.quicrobot.xyz/) → 训练新建向导，数据集请选择 **「PushT OSS 镜像烟测集（ACT READY）」**，不要再选「UAT 实采有效片段」那条 tar 登记。

说明：UAT 已开启 `provider=fake` + `provider_disabled=false` + 内嵌调度，可用 PushT READY + `act-local-sim` 走通提交→成功（见 [UAT_FAKE_E2E_OPENED_2026-09-24.md](UAT_FAKE_E2E_OPENED_2026-09-24.md)）。**真实 GPU** 仍走共置 QuicTrain DLC 控制面。

## 后续（Catalog 真要训 ACT）

需要同时满足：

1. 导出 **含 action + observation.state** 的轨迹数据（非纯视频 EGO）  
2. 交付 **目录型 `oss://` LeRobot 树**（或实现安全 tar 解包物化）后再标 READY  
3. 或把展开后的树同步到 `QUICTRAIN_OSS_CPFS_MIRROR_PREFIX` 下再登记
