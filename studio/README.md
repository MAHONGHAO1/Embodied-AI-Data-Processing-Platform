# quic_studio

QuicData 与 QuicTrain 的工作台。

## 本地前端演示

当前项目包含可独立运行的静态前端演示，不需要后端、数据库或 Node.js：

```bash
make dev-frontend
```

浏览器打开：<http://127.0.0.1:8090/?demo=1>

`?demo=1` 启用内存演示数据和本地演示交互。停止服务时在终端按 `Ctrl+C`。

可通过 `FRONTEND_PORT=8091` 改端口；等价命令为
`python3 frontend/serve_preview.py --host 127.0.0.1 --port 8090`。

需要后台运行时使用受管理入口：

```bash
make dev-frontend-up
make dev-frontend-logs
make dev-frontend-down
```

`make dev-up` 也会启动该普通前端预览，`make dev-down` 会一并停止它。使用
`make dev-mock-up` 可在同一个受管理会话中切换到 Mock 模式。

### 本地演示（Mock）覆盖范围

`make dev-mock`（或 `make dev-mock-up`）后访问
`http://127.0.0.1:8090/?demo=1` 会进入纯前端演示模式：不发任何后端请求，
数据全部来自 `frontend/js/demo-data.js` 的内存投影。已覆盖的控制台流程：

- 数据概览、数据接入、构建数据、采集数据包、标注/审核队列、采集资源、数据资产、数据集
- 工作台：切分、标注、审核（标注/审核页签右上角可切换「工作项 / 批次治理」，
  工作项视图里点「进入工作台」即可演示标注工作台与审核工作台）
- 数采任务 →「查看数据包」抽屉里每个数据包都有「数采审核」入口，会按该项目打开
  数据包审核队列并定位到该数据包
- 数采审核页就是数据包审核队列（按采集项目 / 审核状态筛选，支持通过、退回、
  批量通过和详情抽屉），旧批次详情只在显式选中批次时展示
- 数采任务列表与采集概览共用同一个「采集项目」筛选范围，在任一处选择或清空，
  另一处会同步（真实模式取采集项目实体，mock 取任务上的项目标签）
- 顶部第二级作用域已从「任务集」切换为「采集项目」：数据资产 / 构建数据 / 概览的
  Episode 按 `collection_project_id` 过滤（后端 `/episodes`、`/episodes/assets`
  新增该参数，经数据包 → 采集任务 → 采集项目关联），旧批次导入路径暂以内部默认
  采集项目继续可用，待 Plan ④ 后一并退役
- 训练：总览、任务、新建训练向导（校验/提交/取消/日志）、数据集、模型与 Recipe、资源、系统健康
- 数采：采集概览、数采任务、云端 Pipeline、切分审核、数采配置
- 权限中心与设置中心

列表、详情与写操作都会在内存中生效（例如新建采集项目/任务/批次、审核通过或退回、
数据集版本导出、训练任务提交与取消），因此演示时每一步操作都有可见反馈。
刷新页面会重置为初始演示数据。

演示前可运行一次自检，它会跑接口级流程断言，并用无头 Chrome 逐页确认所有视图都有数据：

```bash
make demo-check
```

## 后端与实现边界

后端数据域位于 `backend/data/`，`backend/train/` 为训练开发预留包。
采集管理、上传、入库审核、建批、人员分配、单审、逻辑资产与全局数据集版本已有实现及测试。
前端可以独立演示，但真实模式与三桶存储仍在整合；Catalog Export 仍为占位实现，
不能用其成功状态作为训练数据已交付的依据。

Data/Train 并行开发请先阅读 [协作基线与交接边界](docs/DEVELOPMENT_HANDOFF.md)。

- 本地依赖与数据库：`make infra-up`、`make dev-migrate`、`make dev-seed-local`。
- 后端启动：`make dev-api`；完整开发服务：`make dev-up`。
- [采集上传接口约定](docs/COLLECTION_UPLOAD_API.md)
- [前三个计划检查记录](docs/PLAN_1_3_REVIEW.md)
- [部署说明](docs/PRODUCTION_SETUP.md)

测试必须使用独立 PostgreSQL/Redis。`backend/tests/conftest.py` 会重置指定测试库，
并清空 `TEST_REDIS_URL` 所在 Redis 实例的 13/14/指定测试库，不能指向业务环境。

```bash
TEST_DATABASE_URL=postgresql+psycopg://quicdata:quicdata@127.0.0.1:15432/quicdata_test_handoff \
TEST_REDIS_URL=redis://127.0.0.1:16379/15 \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -m pytest -p anyio.pytest_plugin backend/tests -q
node --test frontend/tests/*.test.mjs
```
