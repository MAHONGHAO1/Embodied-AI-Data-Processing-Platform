# v0.6.0 状态

更新日期：2026-11-10。状态：v0.6.0 已发布，PR #1 已合并到 `main`，GitHub Release 已创建。固定公开样例 Episode 0–4 的 Windows 端到端验收已通过。

## 版本范围

将固定 ALOHA Coffee 配置接入工作台，完成导入、多相机浏览与质检、审核门禁、LeRobot v3.0 转换、官方加载与数值验证、输出质检、交付打包和独立验包。复用官方 LeRobot 写入和加载接口；当前适配只承诺固定来源 revision 与固定字段，不代表通用 v3.0 数据集支持。

## 实际端到端结果

- 来源：`lerobot/aloha_static_coffee@b144896feb1f37398a862927b22cd3abdf005a6b`，页面声明 MIT；10 个固定来源文件全部通过上游清单哈希核对。
- 数据：Episode 0–4，每条 1,100 帧，共 5,500 帧、50 FPS。四路相机均为 640 × 480，原始文件解码、帧数与记录时间映射检查通过；该证据不代表硬件同步。
- 输入完整性：管理副本指纹运行前后相同（`1c062d6d94c688e9ec50a1cfa63638cba7e49b6cfbe570bc09e966f67ae786ba`）；10 个来源文件的 SHA-256 前后逐项相同。
- 转换：LeRobot v3.0 官方离线加载通过；5,500 帧数值一致性检查通过；四路相机各自 60 个（每条任务首、中、末位置）视频样本抽查通过。
- 输出质检：5/5 条通过，5,500 帧，55/55 项检查完成，0 数据问题、0 加载失败。
- 交付：13 个业务阶段均完成；交付 ZIP SHA-256 为 `a34a79cfb8dbe7b5f7cfa518224d69ee0f156386d206176a9698daf2a374e402`；独立解包、文件清单与交付包官方离线加载通过。
- 工作流运行编号：导入 `20261108T180817_49287effc7b5`；最终处理 `20261108T181145_5196ba6cceb7`；批次 `batch_342e60efcb6f4543a18d4c49d07dcc84`。
- 工作台真实 ALOHA Episode 0 四路相机浏览截图：[workbench-v0.6.0-aloha.png](assets/workbench-v0.6.0-aloha.png)。录屏经抽帧复核仍有加载画面和黑色视频区，因此未作为公开演示；SO-100 截图仅展示该数据集的工作台界面。

自动化验收通过脚本明确标注了模拟标注与审核门禁；这只验证审核后业务链路，不代表人工审核、任务成功判断、机器人执行结果或模型训练效果。完整验收 JSON、来源／输出清单、运行报告、逐节点状态和日志保存在本机忽略目录 `artifacts/aloha-r4/` 与 `work/aloha-integration-r4/`，不会随源码进入 Git。

## 本机验证

- 工作台完整 Python 测试：240 passed、7 skipped、13 subtests passed（Windows / Python 3.12.10）。新增的格式边界和审核子集 profile 测试包含在其中。
- 工作台 Node.js 24：单元测试、Chromium 浏览器流程和生产构建通过；依赖锁检查 `uv lock --check` 通过。
- 基金助手 Python：19 passed；Node.js 24 单元测试、Chromium 浏览器流程和生产构建通过。
- 两个本地服务健康检查均正常：工作台 `127.0.0.1:8765`、基金助手 `127.0.0.1:8766`。

## 验收中发现并修复

1. 固定 revision 的 `episode.tasks[0]` 实际为任务文本，不是任务索引；已让 ALOHA 读取器同时支持文本与索引，并增加回归测试。
2. `next.done` 在 Parquet 中是 Python 布尔标量，但 LeRobot 元数据声明数组形状 `[1]`；转换时现按来源 dtype 与 shape 规范数值特征，标量 shape 回归测试通过。
3. 输出质检此前把审核后的五条派生子集当作必须包含 50 条的原始整集；现以固定的审核子集 profile、来源 Episode 清单和逐条长度核对来区分派生数据，同时保留原始 ALOHA 必须有完整 50 条的约束。

此前失败尝试记录在 `artifacts/aloha-r2/` 与 `artifacts/aloha-r3/`；失败结果没有被登记为验证通过的产物或交付。测试覆盖的异常格式、相机缺失、时间映射、转换与登记门禁结果详见 [验收记录](VALIDATION_v0.6.0.md)。

## 复现命令

在仓库根目录使用 Python 3.12、Node.js 24 和锁定依赖；使用新的、为空的验收目录，不覆盖已有 `work/aloha-integration-r4/`：

```powershell
uv sync --locked
uv sync --project tools/conversion --locked
uv run --locked python scripts/verify_aloha.py `
  --work-root work/aloha-integration-next `
  --artifacts artifacts/aloha-next `
  --timeout-seconds 7200
```

## 发布前剩余事项

- 复核本地 diff、干净克隆安装说明、README／截图和工作流权限范围。
- 真实来源的失败转换、损坏视频与 ZIP 篡改拒绝已由本地契约／故障测试覆盖；若需发布流程级故障注入报告，可作为补充，不改变此次成功验收结论。
- 本轮不创建提交、不推送、不合并、不发布 Release；另行确认后再做 GitHub 更新和正式发布。
