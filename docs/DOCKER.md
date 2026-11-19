# Docker / Linux 运行边界

这个镜像只做一件事：把**同一个本地单用户工作台**放进 Linux 容器，证明它不依赖 Windows 桌面或作者本机的 `work/` 目录。

## 构建与运行

```bash
docker build -t robodata-workbench:local .
docker run --rm -p 127.0.0.1:8765:8765 robodata-workbench:local
```

打开 `http://127.0.0.1:8765/`。端口只发布到宿主机的 loopback；容器内 `ROBODATA_WEB_HOST=0.0.0.0` 是为了让 Docker 端口映射可达，不是公网部署。

持久化工作目录（可选）：

```bash
docker run --rm -p 127.0.0.1:8765:8765 \
  -v "$PWD/docker-work:/app/work" \
  -v "$PWD/docker-data:/app/data" \
  robodata-workbench:local
```

镜像内已包含 React 构建产物、Python 依赖和已知坏样本；`robodata verify-fixtures` 可直接运行。

## Web + 队列 worker

```bash
docker compose up --build
```

`docker-compose.yml` 有两个服务：

- `web`：FastAPI + React，端口只映射到 `127.0.0.1:8765`；
- `worker`：一次性执行 `robodata.worker --once`，用来验证容器内 SQLite 队列能打开；空队列时正常退出 0。

两者共享 `./docker-work:/app/work`。worker 仍是单机单用户、并发 1，不是分布式调度。

## CI 中验证了什么

`.github/workflows/acceptance.yml` 的 `docker-linux` job 在 `ubuntu-latest` 上：

1. 构建镜像；
2. 启动容器并等待 `/api/health`；
3. 断言版本与 `pyproject.toml` 一致、健康检查 scope 为 `container`；
4. 请求 `/`，确认 React 页面由 FastAPI 同源托管；
5. 在容器内运行 `robodata verify-fixtures`，确认 6 个已知坏样本仍命中预期判据；
6. 在镜像内启动一次 `robodata.worker --once`，确认 SQLite 队列能打开。

`acceptance/linux-conversion` 另外在 Ubuntu 主机上安装隔离转换环境，跑 v2.1 官方转换/加载与 Episode 级预览续跑；这不是容器内转换。

## 不证明什么

- 没有在本机 Windows 上运行 Docker；Docker 结论只来自上述 Linux CI。
- 镜像没有安装 `tools/conversion` 的 PyTorch / LeRobot 隔离环境，因此**容器内不执行真实数据转换**。
- 不是公网服务、不是多用户、不是云部署；没有鉴权、配额或反向代理配置。
- 没有验证 Linux 桌面启动、GPU、CUDA 或训练吞吐。
- 容器仍按本地单用户设计，不要直接暴露到公网。
