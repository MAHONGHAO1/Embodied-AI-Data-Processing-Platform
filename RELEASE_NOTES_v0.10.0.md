# v0.10.0 · Evidence-grade

**这一版不加导入格式、不加页面、不加适配器。** 它只做一件事：把「这个项目到底做到了什么程度」变成一串能复查的数字和命令，而不是 README 里互相矛盾的说法。

## 版本链收口

v0.9.0 发布时，README 和前端写 0.9.0，`pyproject.toml` 还停在 0.6.0，`config.py` 还停在 0.8.0。质量报告会把 `APP_VERSION` 写进产物，这意味着报告可能声称自己来自一个从未发布的版本。

- `pyproject.toml`、`robodata/config.py`、`frontend/package.json`、README 首行与 release 徽章统一到 **0.10.0**。
- 新增 `tests/test_version_consistency.py`：任一位置漂移、或缺少对应 `RELEASE_NOTES_v<version>.md`，测试直接失败。
- `uv.lock` 中的项目版本随 `pyproject.toml` 更新。

## 运行记录读取

`/api/runs` 在 300 条运行记录下曾超过 5 秒阈值。根因不是磁盘慢，而是 `list_runs` 对每个条目重复调用 `get_run`，每次都重新 `resolve()` 两次运行根目录并重新校验路径。

- 抽出 `_state_from_directory()`，`get_run` 保留完整路径校验，`list_runs` 对已枚举的目录直接读取状态。
- 300 条运行记录的列表读取恢复到阈值内；运行状态语义、只读约束和中断派生逻辑未变。

## 规模证据

新增 [10 万 / 100 万行规模实验](docs/benchmarks/2026-11-18-scale-experiment.md) 与原始 JSON：

| 行数 | 重复 | 中位数 | 表格行/秒 |
| ---: | ---: | ---: | ---: |
| 100,000 | 3 | 4.719 秒 | 21,190 |
| 1,000,000 | 1 | 61.245 秒 | 16,328 |

边界写在实验文档第一段：只测 `run_checks(check_video=False)` 的表格路径，不含 Parquet 读取、视频解码、转换、打包、并发或服务开销；100 万行档只跑 1 次，100,000 行档方差很大。**不声称端到端容量，也不声称性能优化。**

## 训练输入冒烟

新增 `scripts/train_smoke.py` 与 [说明](docs/TRAINING_SMOKE.md)：在标准化状态特征上跑 100 步小批量 SGD，预测 6 维动作。

本机合成数据结果：2,048 行，初始 MSE `0.71632246`，100 步后 `0.00012241`，损失比 `0.000171`，参数全部有限，CPU 耗时 `0.003 秒`。

**这不是策略训练。** 不使用 ACT / Diffusion Policy / VLA，不证明任务成功率、泛化或硬件安全。仓库不包含已验收的 ALOHA / SO-100 产物，所以没有在这里重跑真实数据集；拿到数据后可用 `--dataset-root` 复现。

## Linux 核心检查

新增 `.github/workflows/linux-smoke.yml`，在 `ubuntu-latest` 上安装锁定依赖，运行版本一致性、表格质检、数据集读取、坏样本校验和 100 步冒烟，再执行 `robodata verify-fixtures`。Windows 全量测试 workflow 保持不变。

这只证明核心 Python 路径可在 Linux 上安装并运行；Docker 镜像、Linux 桌面启动脚本和真实数据转换未在此版本验证，因此不写进能力声明。

## 证据入口

- [docs/EVIDENCE.md](docs/EVIDENCE.md)：版本链、测试计数、坏样本、真实数据验收、规模实验、训练冒烟、未验证事项
- [docs/benchmarks/2026-11-18-scale-experiment.md](docs/benchmarks/2026-11-18-scale-experiment.md)
- [docs/TRAINING_SMOKE.md](docs/TRAINING_SMOKE.md)

## 不宣称

- 不宣称训练效果、策略成功率或硬件同步。
- 不宣称 100 万行表格检查等于 100 万行端到端转换。
- 不宣称 Docker 或 Linux 桌面可用；Linux CI 只覆盖核心 Python 检查。
- 运行边界仍是本机单用户、`127.0.0.1` loopback；公网多用户能力未在本版处理。
