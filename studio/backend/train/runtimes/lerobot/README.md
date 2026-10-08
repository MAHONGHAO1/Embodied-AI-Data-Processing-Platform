# LeRobot GPU runtime

This directory owns the QuicTrain adapter and image recipe. It does not vendor LeRobot and is not a
Git submodule. `upstream.lock` is the source of truth for the official LeRobot ref and source archive
checksum.

## Published target

- Registry: Alibaba Cloud ACR Enterprise, Beijing
- Repository: `quic-robot-registry-vpc.cn-beijing.cr.aliyuncs.com/quicrobot/quictrain-lerobot-runtime`
- Published base tag: `v0.5.1-pytorch2.10.0-cu128-r1`
- Published runtime patch tag: `v0.5.1-pytorch2.10.0-cu128-r2`
- Current runtime digest: `sha256:0072fd58526d4505a06d8098478ba1e70f53a7187eac4f56a78c1327001765af`
- Platform: `linux/amd64`
- Models: ACT and pi05
- Base: PyTorch 2.10.0, CUDA 12.8, cuDNN 9, Python 3.12

The base image, LeRobot commit, and LeRobot source archive are all pinned by digest/checksum. The
finished image records the resolved Python environment at `/opt/quictrain/runtime-requirements.txt`.
For a full dependency build, the helper downloads the verified source archive into the ignored
`.build/` cache before the container build. The default patch build skips that download entirely.

`Dockerfile.patch` is the default r2 recipe. It inherits the verified r1 registry digest and only
replaces the QuicTrain Runner/runtime adapter layers. This keeps an operational patch reproducible
on the restricted DSW build host without re-downloading PyTorch or LeRobot. Use `Dockerfile` for a
full dependency rebuild and publish that result under a later immutable `rN` tag.

## Build

Use Docker Buildx on a normal build host:

```bash
runtimes/lerobot/build-image.sh
```

On a daemonless Linux build host that provides Buildah:

```bash
CONTAINER_ENGINE=buildah runtimes/lerobot/build-image.sh
```

Set `PUSH_IMAGE=1` only after logging in to the target registry. Prefer ACR's one-hour temporary
credential from `GetAuthorizationToken`; do not store a fixed registry password in this repository
or in the image.

Because the ACR repository enforces immutable tags, every runtime, adapter, base image, or dependency
change must use a new `rN` tag and create new QuicTrain Model Versions. Do not publish or depend on a
mutable `latest` tag.

## Runtime smoke

```bash
docker run --rm --gpus all --entrypoint python IMAGE_REF -c \
  'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.get_device_name())'
docker run --rm --entrypoint python IMAGE_REF -c \
  'from lerobot.policies.act.configuration_act import ACTConfig; from lerobot.policies.pi05.configuration_pi05 import PI05Config'
```
