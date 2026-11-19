# v0.12.0 · Matching Cable 缺失流可见

**这一版不是再加一个适配器就结束。** 它把「数据不完整还能不能用」做成了明确语义：缺哪路相机、缺哪路深度、缺哪些可选数值字段，平台逐条记录并继续，而不是一个 `ValueError` 把整批数据挡在门外。

适配器基于 PR #2 的 Matching Cable 逐帧 HDF5 支持继续完善；本版重点是可选的流、可核对的缺失状态和合成回归。

## 现在怎么判断一条数据能不能进

必填只有三项：

- `master/joint_position` → `action`
- `puppet/joint_position` → `observation.state`
- `timestamps`：源毫秒时间，50 ± 10 ms/帧；越界直接失败，不静默按 20 FPS 继续

至少需要一路 RGB 相机。其余全部是可选的：

| 流 | 缺失时 |
| --- | --- |
| `camera_0..3` RGB | 只写入存在的相机，其余记入 `missing_cameras` |
| `depth_images/camera_1..2` | 存在的校验可读；缺失记入 `missing_depth_source_cameras`，不阻断 |
| `puppet/eef`、`master/eef`、`puppet/joint_effort`、`puppet/6f` | 记入 `optional_fields=not_provided`，不写入输出特征 |
| 触觉 | 固定记录 `tactile.status=not_provided`；适配器不读取触觉字段 |

清单、批次来源、右侧检视和交付说明都显示这些字段。缺失不是自动修复，也不会补造画面或数值。

## 验证

新增 `scripts/verify_matching_optional.py`：不需要 9 GB 真实数据，生成 2 条 Episode × 2 帧、只有 `camera_0`、没有深度/触觉/可选数值的合成夹具，跑真实预览和六个转换/验证阶段。

本机结果：

- `cameras` 只有 `observation.images.camera_0`；`missing_cameras` 3 路；`missing_depth_source_cameras` 2 路；
- `optional_fields` 全部 `not_provided`；`tactile.status=not_provided`；
- `file_validation`、`matching_validation`、`field_mapping`、`format_write`、`official_verify`、`archive_verify` 六个阶段通过；
- 官方加载器读取 2 Episode / 4 帧，`output_loader_consistency_checked=true`；
- `source_numeric_consistency_rechecked=false`：本次没有重新读取源 HDF5 做逐帧数值回核，字段名不再冒充源数值重核。

另用同一夹具跑了完整业务链（导入 → 基础质检 → 筛选 → 标注审核 → 最终质检 → 转换 → 交付），4 帧产物与交付 ZIP 登记成功。该运行只证明链路，不证明真实 9 GB 数据、硬件同步或策略效果。

## 修复的审查项

- 帧数比较改为**必须相等**：源 Episode 多出或缺少帧都失败，不再取前 N 帧静默截断。
- `assert_source_unchanged()` 在清单含 `sha256` 时**重新计算哈希**，文件大小与 mtime 不变也能发现内容变化。
- 50 ms 帧间隔从 warning 升级为硬失败（容差 ±10 ms）。
- 候选集拒绝重复的源/输出 Episode 索引。
- 官方验证字段改名为 `output_loader_consistency_checked`，业务门禁同步；不再声称源数值已重核。
- 新增合成回归覆盖缺失相机、缺失深度、缺失可选字段、额外帧和同大小同 mtime 的内容变化。

## 不宣称

- 没有在本机跑真实的 9 GB Matching Cable 数据；合成夹具只覆盖链路与缺失语义。
- 不声明相机硬件同步、策略训练效果、任务成功率或生产容量。
- 至少一路 RGB 仍是当前预览的前提；完全没有相机的数据不在本版支持范围。
- 没有触觉建模；触觉字段存在也不会进入交付。
- 仍是本机单用户、`127.0.0.1`；没有登录、多租户或云部署。

## 证据入口

- [Matching Cable 适配器](docs/MATCHING_CABLE.md)：目录、必填/可选字段、缺失语义和命令
- [证据总表](docs/EVIDENCE.md)：版本链、测试计数与未验证事项
- [合成回归脚本](scripts/verify_matching_optional.py)：一条命令复现缺失流场景
- [PR #2](https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform/pull/2)：原始适配器贡献与审查记录
