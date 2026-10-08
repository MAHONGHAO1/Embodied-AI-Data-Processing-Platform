# 外部部署依赖

本目录描述需要人工部署、不能提交到 Git 或复制进应用镜像的二进制依赖。版本化校验元数据保留在仓库中，运维人员应在启动 worker 前验证手工复制的文件。

## Embodied VL SDK

Embodied VL 客户端支持 Linux x86_64 与 CPython 3.9、3.10、3.11、3.12。启动 `worker-ai` 前，将经过审查的四个 `.so` 文件复制到部署主机的 `deploy/external-deps/embodied-vl/`。禁止复制到 `backend/`、写入 Dockerfile，或提交 `.so`/`.whl` 文件。

```bash
mkdir -p deploy/external-deps/embodied-vl
cp /secure/sdk_bin/embodied_vl_client.cpython-*-x86_64-linux-gnu.so \
  deploy/external-deps/embodied-vl/
shasum -a 256 -c deploy/external-deps/checksums.sha256
```

生产 Compose 只将 `embodied-vl/` 以只读方式挂载到 `worker-ai`。`worker-ai` 位于显式 `ai` profile，正常生产启动不会开启对外 AI 处理；UAT 只有在二进制复制和校验完成后才能设置 `UAT_AI_WORKER_ENABLED=true`。

Linux x86_64 可运行无网络原生导入检查：

```bash
RUN_EMBODIED_VL_NATIVE_TEST=1 PYTHONPATH=backend \
  python -m pytest backend/tests/test_embodied_vl_native_sdk.py -q
```

ARM 主机会跳过原生导入检查，但仍应运行与架构无关的适配器和部署测试。
