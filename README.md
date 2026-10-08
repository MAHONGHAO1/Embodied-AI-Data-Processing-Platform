# Embodied AI Data Processing Platform v2.0

> 升级自 v0.4。替代旧的 robodata-workbench（Streamlit 版），本版基于工业级具身智能数据平台 **data-engine + studio** 封装。

## 这是什么

- `data-engine/`：底层数据处理算子引擎（Python SDK）。含 `sync` / `clean` / `compliance` / `perception` / `label` / `export` 六大算子库，统一 `list_capabilities()` / `invoke()` 调度。
- `studio/`：数据 / 训练工作台。前端为 Vue + Element Plus，后端为 FastAPI（alpha，未完整实现）。

## 一键使用（重点）

1. 克隆本仓库：
   ```bash
   git clone https://github.com/MAHONGHAO1/Embodied-AI-Data-Processing-Platform.git
   ```
2. 进入文件夹，**双击 `启动studio.cmd`**
3. 自动起前端服务并打开浏览器 → `http://127.0.0.1:8090/?demo=1`
4. 界面为纯前端演示模式（`?demo=1`，全内存 mock 数据），无需后端 / 数据库，所有控制台（数据 / 采集 / 标注 / 审核 / 训练 / 权限）均可点击浏览。

停止：关闭那个最小化的 `QuicStudio-Frontend` 窗口即可。

## 目录结构

| 路径 | 说明 |
|---|---|
| `data-engine/` | 数据处理算子库（算法引擎） |
| `studio/` | 数据 / 训练工作台（前端演示 + 后端 alpha） |
| `启动studio.cmd` | Windows 一键启动器（双击即用，路径自适配） |

## 备注

- `data-engine/perception/weights/YOLO/detector.onnx`（约 102MB）因 GitHub 单文件 100MB 限制未纳入仓库。它仅用于 `data-engine` 的感知算子，前端演示不需要；如需启用感知能力请另行获取该权重。
- 后端（PostgreSQL / Redis / MinIO + 训练域）仍为 alpha，裸 Windows 无法一键起；当前启动器走纯前端演示。
- v0.4 旧内容已在本仓库移除，本版即 2.0。

## License

见各子目录自带 LICENSE。
