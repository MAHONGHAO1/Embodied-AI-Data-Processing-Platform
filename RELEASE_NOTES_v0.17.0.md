# v0.17.0 · 交付门禁与可安装性

**这一版让「别人也能用」：一行 Action 接进别人的仓库，一条命令装成 CLI。**

## 新增：GitHub Action 交付门禁

仓库根新增 `action.yml`，别的仓库不用 clone、不用写安装步骤：

```yaml
- uses: MAHONGHAO1/Embodied-AI-Data-Processing-Platform@main
  with:
    data-root: data/my-dataset
    format: junit
    fail-on-incomplete: "true"
```

它会检出本仓库、按锁文件装依赖、跑 `robodata check`，退出码非零就让流水线停下；报告写到 `robodata-report/`。只支持 Linux runner。

仓库自带 `.github/workflows/action-self-test.yml`：拿已知坏样本跑一遍并断言 Action 必须失败，确保「该红就红」不是口头承诺。

## 新增：不经过 PyPI 的命令行安装

```bash
uv tool install "robodata-workbench @ git+https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform"
robodata doctor
robodata check --data-root ./data --format junit --fail-on-incomplete
```

## 修复：非源码安装时 doctor 误报

通过 `pip` / `uv tool` 安装后没有源码目录，`doctor` 的版本链检查会报 `fail`。现在识别为「非源码安装」并标 `skip`，其余检查照常。

## 文档

- `README.md` 首屏改为「30 秒自验」：一条 `verify-fixtures`，6 个已知坏样本 6/6。
- `docs/CI_INTEGRATION.md` 补 Action 用法。
- `docs/RELEASING.md` 新增发版清单。
- `docs/SOP.md` 启动入口改为 8765；旧版 Streamlit 入口标注「仅作对照、功能冻结」。
- `README.md` 补「两条路径的边界」：旧版转换入口固定验证 demo_0-2 的 174 帧，扩容走业务链。

## 转换校验

`conversion.py` 把 174 帧 / 9 采样位置 / 3 条任务提取为 `LEGACY_VERIFY_*` 常量，注释写明来自 V0.4 固定样例验收；失败信息改为说明旧入口的适用范围，不再只报「记录不完整」。

## 验证

| 项 | 结果 |
| --- | --- |
| 本机 `pytest -q` | 371 passed / 19 skipped（跳过项为需独立转换环境或固定样本的用例） |
| `robodata verify-fixtures` | 6/6 |
| acceptance（带 `run_full=true`） | 四个 job 全 success：linux-conversion、docker-linux、source-linux、full-windows |
| Action 自测 | run 44884209788 success |
| 命令行安装 | `uv tool install git+...` 后 `robodata doctor` 返回 `ok: true` |

## 边界不变

不宣称相机硬件同步、训练效果或任务成功率；外部复现记录仍为 0 条，见 `docs/FEEDBACK.md`。
