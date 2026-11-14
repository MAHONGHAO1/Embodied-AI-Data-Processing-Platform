# 参与贡献

本项目是个人工程作品，用于展示可追溯、可验证的数据处理链路。欢迎提 issue 与改进建议；提交 PR 前请先开 issue 讨论。

## 请先读边界

- 两个项目都**只在本机 `127.0.0.1` 运行**，不提供公网交互实例。
- 基金项目使用**虚构合成组合**，不连接真实账户、不执行交易、不提供投资建议；指标不可用时应显式标注原因，不以 0 代替。
- 具身项目的逐路文件时间映射检查**不代表相机硬件同步**；四相机同屏为 16:9 布局验证，批次元数据可能是演示构造。
- 不接受把构造数据、模拟结果或布局验证图写成真实业务证据的改动。

## 本地复现

```bash
# Python：使用项目锁定的环境
uv --directory . run --locked python -m pytest

# 前端
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix frontend run test
```

后端健康检查：具身仓 `http://127.0.0.1:8765/api/health`，基金仓 `http://127.0.0.1:8766/api/health`。

## 提 issue 时请附

1. 版本号与使用的入口（React 工作台 / Streamlit 旧入口）
2. 可重复的复现步骤
3. 期望行为与实际行为
4. OS、Python / Node 版本、相关日志或截图

## 变更约定

- 保持 API 契约、数据口径与能力边界不变；若必须调整，同步更新 README、CHANGELOG 与展示页说明。
- 新增截图必须注明数据来源（真实本机服务 / 公开样例 / 构造数据）与拍摄日期。
- 样式改动请沿用 `frontend/src/styles.css` 中已固化的 `--space-*` 与 `--text-*` token。

许可证：MIT，见 [LICENSE](LICENSE)。
