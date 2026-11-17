# 100 步训练输入冒烟

## 它回答什么

导出的 `observation.state` / `action` 数组，能不能在不报错、不产生 NaN 的前提下，驱动一个真实的优化循环并让损失下降？

## 它不回答什么

- 不是策略训练，不使用 ACT / Diffusion Policy / VLA。
- 不证明机器人任务成功率、泛化能力或硬件安全。
- 不证明超参数、网络结构或数据规模足以训练出可用策略。
- 合成数据上的损失下降只证明**循环机制**可用，不证明真实数据上的学习效果。

## 实现

`scripts/train_smoke.py` 在标准化后的状态特征上加偏置项，用 100 步小批量 SGD 预测 6 维动作。只用 NumPy，不引入 torch；因此它能在主环境里跑，不依赖 `tools/conversion/` 的独立环境。

## 复现

合成数据（CI / 无数据集时）：

```powershell
uv run --locked python scripts\train_smoke.py --synthetic --steps 100
```

真实 LeRobot 数据集（需要本地已转换的数据）：

```powershell
uv run --locked python scripts\train_smoke.py --dataset-root <数据集根目录> --steps 100
```

输出 JSON 包含 `initial_mse`、`final_mse`、`loss_ratio`、`finite`、参数更新量和耗时。

## 2026-11-18 本机结果

| 项 | 值 |
| --- | --- |
| 数据 | 合成线性关系，2,048 行 × 6 维 |
| 步数 | 100 |
| 初始 MSE | 0.71632246 |
| 最终 MSE | 0.00012241 |
| 损失比 | 0.000171 |
| 参数有限 | 是 |
| 耗时 | 0.003 秒（CPU） |

原始输出：[train-smoke-2026-11-18.json](benchmarks/train-smoke-2026-11-18.json)。

**边界**：这是本机合成数据结果。仓库不包含已验收的 ALOHA / SO-100 转换产物，因此没有在这里重跑真实数据集；拿到数据后可用上面的 `--dataset-root` 命令复现。真实数据上的损失是否下降取决于数据与超参数，本文件不作保证。
