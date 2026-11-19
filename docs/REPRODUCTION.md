# 五分钟复现（外部验证用）

这份页面的目标只有一个：让第一次接触仓库的人，在**不装 Docker、不下载大样本**的情况下，判断它能不能在自己机器上跑起来。反馈请按 [FEEDBACK.md](FEEDBACK.md) 记录；失败同样有效。

## 先决条件

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js 24（只在跑前端时需要）

## Tier 1 · 无网络（约 2 分钟）

```bash
git clone https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform.git
cd Embodied-AI-Data-Processing-Platform
uv sync --locked
uv run --locked robodata verify-fixtures
uv run --locked python -m pytest tests/test_version_consistency.py -q
```

期望结果：

- `verify-fixtures` 输出 `6/6`，六个已知坏样本都命中各自的 `EXPECTED.md` 判据；
- 版本一致性测试输出 `1 passed, 1 skipped`（跳过的是只在打 tag 时运行的 Git tag 校验）。

如果失败，请记录操作系统、Python / uv 版本、完整命令和报错原文。只写“跑不起来”无法定位。

## Tier 2 · 固定真实样本（约 5–10 分钟，下载约 19.2 MB）

```bash
uv run --locked robodata fetch-sample --episodes 0-1 --data-root work/samples/so100-e0-1
uv run --locked python scripts/verify_pipeline.py \
  --sample work/samples/so100-e0-1 --episodes 0-1 --mode source \
  --output work/acceptance/pipeline-source.json
```

期望结果：

- 样本来自固定 revision `54141acb0bd6bcb868e34b4eb328f26481d333b2` 的完整 Episode 0 和 1；
- 9 个文件 / 19,215,391 字节，逐文件大小与 SHA-256 校验通过；
- 报告 `passed: true`，2 Episode / 1,300 帧，3 个阶段（来源、导入、审核夹具）。

`huggingface.co` 直连失败时，可以只改变传输端点：

```bash
uv run --locked robodata fetch-sample --episodes 0-1 \
  --data-root work/samples/so100-e0-1 --endpoint https://hf-mirror.com
```

`--endpoint` 只是传输路径；固定 revision 与清单哈希仍是信任根。镜像返回 429 时等待后重试，不要改成未固定的分支或最新版本。

## 可选 · 完整 9 阶段（需要转换环境）

```bash
uv sync --project tools/conversion --locked
uv run --locked python scripts/verify_pipeline.py \
  --sample work/samples/so100-e0-1 --episodes 0-1 --mode full \
  --output work/acceptance/pipeline-full.json
```

这会实际执行转换、官方 `lerobot 0.4.4` DataLoader 探针、交付打包和独立 ZIP 验证。完整耗时、DataLoader 波动和边界见 [ACCEPTANCE.md](ACCEPTANCE.md) 与 [性能基线](benchmarks/2026-11-18-pipeline-2-episodes.md)。没有转换环境时，Tier 1 / Tier 2 已经足够给出第一条外部反馈。

## 反馈时请只写可核对的事实

1. 操作系统、Python / uv 版本；有前端时附 Node.js 版本；
2. 执行了哪一条命令（Tier 1 / Tier 2 / 完整）；
3. 成功还是失败；失败请贴完整报错，不要只写摘要；
4. 耗时；
5. 是否有网络限制（`huggingface.co`、镜像、代理）。

不需要真实姓名或账号。记录规则见 [FEEDBACK.md](FEEDBACK.md)。
