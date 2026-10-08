# UAT Studio 训练流程打通（FakeProvider + 内嵌调度）

Date: 2026-09-24  
Site: [studio-uat.quicrobot.xyz](https://studio-uat.quicrobot.xyz/)  
Host: ECS `i-2ze11xs8mqln1c3ehrj9` / `quicstudio-uat-source@api` :18081

## 变更（可回滚）

文件：`/etc/systemd/system/quicstudio-uat-source@api.service.d/python311.conf`

| 项 | 原值 | 现值 |
| --- | --- | --- |
| `QUICTRAIN_PROVIDER` | `fake` | `fake` |
| `QUICTRAIN_PROVIDER_DISABLED` | `true` | **`false`** |
| `QUICTRAIN_EMBEDDED_SCHEDULER` | `0` | **`1`** |

已留备份：`python311.conf.bak.*`。回滚：恢复备份 → `daemon-reload` → `restart quicstudio-uat-source@api`。

## 验证结果

- `GET /api/train/health`：`provider=fake`，`scheduler=embedded`
- `ops/status`：`capacity.provider_disabled=false`
- 数据集：`dsv_3a4f2567dd74fc428ab6`（PushT READY）
- 资源档：`act-local-sim`（本地模拟 / FakeProvider）
- 作业：`job_051aa3901fca1bbf2b0d`  
  `QUEUED → PROVISIONING → RUNNING → SUCCEEDED`（约 3s，FakeProvider 轮询）

## 页面怎么走

1. 打开 UAT → 训练新建向导  
2. 数据集选 **「PushT OSS 镜像烟测集（ACT READY）」**（不要选 Catalog tar）  
3. 模型 ACT；资源优先 **本地模拟**（或 4090 档也会回退到 Fake，因 UAT 未装 DLC SDK）  
4. 预检通过后提交，任务应在数秒内到 `SUCCEEDED`

## 说明

当前打通的是 **控制面 + 调度状态机**（validate → create → schedule → succeed），不是真实 4090/DLC GPU。真算力仍走共置 QuicTrain `:8001`（`aliyun_dlc`）。若要在 Studio UAT 直连 DLC：装 PAI SDK、注入 QuicTrain 同款 DLC/VPC/镜像 env，并把 `QUICTRAIN_PROVIDER=aliyun_dlc`。
