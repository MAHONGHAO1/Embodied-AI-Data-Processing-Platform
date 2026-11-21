# 把 check 挂进 CI

`robodata check` 的退出码和两种输出格式，都是为流水线准备的。这份文档给一段能直接抄的配置，以及它现在的限制。

---

## 退出码

| 码 | 含义 | CI 该怎么处理 |
|---|---|---|
| `0` | 可交付：没有数据异常、没有加载失败、没有未检查项 | 放行 |
| `2` | 有加载失败（文件读不出、视频解不开） | 拦下 |
| `3` | 有数据异常（判据不通过） | 拦下 |
| `4` | 结论不完整，且加了 `--fail-on-incomplete` | 拦下 |
| `1` | 运行本身失败或中断 | 拦下，先查工具或环境 |

不加 `--fail-on-incomplete` 时，跳过视频之类的项只记在报告里、不影响退出码——这是有意的：明确选择 `--skip-video` 的人知道自己在做什么。想让「没查全」也不能混过去，就加这个参数。

## 一条命令的产物

```bash
robodata check --data-root ./data \
  --format junit \
  --emit-contract ./delivery \
  --fail-on-incomplete
```

- `--format junit` → 在报告目录写 `quality_junit.xml`，测试面板能逐条显示哪条判据挂了。
- `--format sarif` → `quality_sarif.sarif`，扫描视图用。
- `--emit-contract ./delivery` → `QUALITY_REPORT.json` + `QUALITY_REPORT.md`，第一行就是结论，随交付包走。

`--format` 一次只出一种。两个都要就跑两遍，或者从同一份 JSON 报告自己转。

## 可复制的 workflow

放在你自己的仓库 `.github/workflows/quality.yml`：

```yaml
name: dataset quality

on:
  push:
  pull_request:
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true

      - name: Set up Python
        run: uv python install 3.12

      # 当前没有 PyPI 包，只能从仓库装。见下面的「现在的限制」。
      - name: Install RoboData
        run: |
          git clone --depth 1 https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform.git .robodata
          cd .robodata && uv sync

      - name: Check the dataset
        id: quality
        working-directory: .robodata
        run: |
          uv run robodata check \
            --data-root "$GITHUB_WORKSPACE/data" \
            --format junit \
            --emit-contract "$GITHUB_WORKSPACE/delivery" \
            --fail-on-incomplete

      - name: Upload the reports
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: quality-reports
          path: |
            .robodata/work/runs/*/reports/quality_junit.xml
            delivery/

      - name: Publish test results
        if: always()
        uses: mikepenz/action-junit-report@v5
        with:
          report_paths: .robodata/work/runs/*/reports/quality_junit.xml
          check_name: RoboData 判据
```

`check` 返回 `2` / `3` / `4` 时这一步会红，`if: always()` 保证报告和契约仍然上传——**失败时恰恰最需要看报告**。

`data/` 换成你自己的 LeRobot v2.0 或 v3.0 目录。没有数据想先看效果，把 `--data-root` 指到仓库里的 `fixtures/known-bad/frame-gap`，会稳定返回 `3`。

## 一行引用：仓库自带的 Action

仓库根有 `action.yml`，别的仓库不必自己 clone、写安装步骤：

```yaml
- uses: MAHONGHAO1/Embodied-AI-Data-Processing-Platform@main
  with:
    data-root: data/my-dataset
    format: junit
    fail-on-incomplete: "true"
```

它会检出本仓库、按 `uv.lock` 装依赖、跑 `robodata check`，退出码与上面的表一致；报告默认写到 `robodata-report/`，可以直接 `upload-artifact`。只支持 Linux runner。仓库自己的 `.github/workflows/action-self-test.yml` 会用 `fixtures/known-bad/nan-joint` 验证「判据命中时 Action 确实会红」。

## 判据改了怎么办

报告里的 `rule_version` 和 `docs/RULES.md` 是配套的。规则版本变了，旧报告的结论不该被当新结论用——契约里带着 `rule_version`，下游可以据此决定要不要重跑。

想在自己的仓库里确认判据引擎没被改坏，跑：

```bash
robodata verify-fixtures
```

它一条命令验完六个反例，有判据不触发就返回非 `0`。本仓库的 CI 已经在每次 push 跑这一步。

## 现在的限制

**包还没发到 PyPI。** 所以别人的 CI 只能 `git clone` 再 `uv sync`，会连带装上 Streamlit、FastAPI、PyAV 这一整套界面依赖——对一个只想在流水线里跑质检的人来说太重了。

这是目前最大的接入摩擦。要解它，得把 `cli.py` / `quality.py` / `dataset.py` / `video.py` / `ci_output.py` / `contract.py` 这一条纯 Python 链路拆成一个不带 UI 依赖的独立包。在这之前，上面的 workflow 就是能用的写法，只是重。

其余限制：

- `check` 是单机的，没有并发任务队列；一次跑一个目录。
- `--format junit` 和 `--format sarif` 不能同时出。
- 报告落在 `work/runs/<运行号>/reports/`，路径带运行号，artifact 匹配要用通配。
