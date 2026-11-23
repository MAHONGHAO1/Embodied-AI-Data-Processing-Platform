# 发布到 PyPI

第一次发布前必须把凭据配好；之后的版本只要打 tag。

## 一次性配置（由仓库所有者操作，不需要把密码或 token 交给任何人）

1. 注册并登录 <https://pypi.org/>，开启 2FA（强制）。
2. 打开 <https://pypi.org/manage/account/publishing/>，添加一个 Trusted Publisher：
   - PyPI Project Name：`robodata-workbench`
   - Owner：`MAHONGHAO1`
   - Repository name：`Embodied-AI-Data-Processing-Platform`
   - Workflow name：`release.yml`
   - Environment name：`pypi`
3. 确认 GitHub 仓库的 `release` workflow 已存在（本仓库 `.github/workflows/release.yml`）。

Trusted Publishing 用 OIDC 换临时凭据，不需要在 GitHub 里存 token，也不需要把密码发给别人。

## 每次发版

1. 改 `pyproject.toml` 的 `version`，同步 README 版本行、release 徽章文字与徽章链接、`docs/EVIDENCE.md` 顶部版本、`robodata/config.py` 的 `APP_VERSION`、前端 `package.json`。
2. 更新 `CHANGELOG.md` 与 `docs/releases/RELEASE_NOTES_vX.Y.Z.md`。
3. 本地跑一遍：

   ```bash
   uv run --locked python -m pytest tests/test_version_consistency.py -q
   uv run --locked ruff check .
   uv run --locked python -m pytest -q
   uv run --locked robodata verify-fixtures
   ```

4. 提交并打 annotated tag，然后推送：

   ```bash
   git tag -a v0.17.0 -m "v0.17.0"
   git push origin v0.17.0
   ```

5. workflow 会：校验 tag 与 `pyproject` 版本一致 → 构建 sdist/wheel → `twine check` → 用 Trusted Publishing 发布。
6. 发布后验证：

   ```bash
   uv tool install robodata-workbench
   robodata doctor
   ```

## 注意

- PyPI 版本号发出去不能删，只能 yank。第一次发之前把 `name`、`version`、README 渲染、`[tool.hatch.build.targets.wheel] packages` 核对一遍。
- `twine check` 只检查 README 渲染与元数据，不检查包能不能装；发布后一定跑一次 `uv tool install`。
- 如果 Trusted Publishing 报错，先核对 workflow 文件名、environment 名、仓库名三者与 PyPI 页面完全一致。
