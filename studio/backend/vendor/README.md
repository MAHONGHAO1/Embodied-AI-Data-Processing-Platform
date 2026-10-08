# 后端 Vendor 依赖

本目录由初始化或部署脚本填充。QRDF SDK 源码默认不提交到 QuicData 仓库。

## QRDF SDK

| 场景 | 命令 | 行为 |
|---|---|---|
| 部署或 CI | `bash scripts/fetch-qrdf.sh` | 缺失时按配置克隆；完整 SDK 已存在时跳过 |
| 强制更新 | `QRDF_FORCE_FETCH=1 bash scripts/fetch-qrdf.sh` | 忽略现有目录并重新获取 |
| 本地联调 | `bash scripts/sync-qrdf.sh ../qrdf` | 从本地相邻仓库同步 |
| 初始化 | `bash scripts/setup.sh` 或 `bash deploy/bootstrap.sh` | 自动调用获取脚本 |

运行 vendor QRDF 测试前执行 `make test-vendor-qrdf`。该目标会把当前
`backend/vendor/qrdf` 以 editable 方式安装，并安装其测试依赖（包括
`jsonschema`）；普通 QuicStudio 后端测试不要求把 vendor 测试混入默认测试路径。

远程地址和版本配置见 `deploy/qrdf.env.example`。完整 SDK 必须同时存在 `qrdf/__init__.py` 与 `pyproject.toml`，临时 stub 不视为可用。

```text
backend/vendor/qrdf/
├── pyproject.toml
└── qrdf/             # import qrdf
```

运行时由 `app/bootstrap.py` 注册路径，业务代码只通过 `app/integrations/qrdf/` 使用 SDK。
