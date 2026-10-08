# Quic Op (quic_op) 项目部署与概览文档

## 1. 项目简介
`quic_op` 是一套专为具身智能（Embodied AI）、自动驾驶以及遥操作（Teleop）打造的企业级多模态数据处理算子库。
项目摒弃了传统的“流水线节点（Node-based）”脚本开发模式，采用**领域驱动设计（DDD）**，将数据处理生命周期重组为六大核心算子库。

### 核心特性
- **云边端同构与全平台兼容**：底层实现彻底的 I/O 解耦。算子仅接受内存对象或本地绝对路径，无论是在本地 MacBook、Linux 服务器、边缘工控机，还是云端 MaxFrame / K8s 集群，均可无缝拉起。
- **洋葱架构与防腐层隔离**：模型架构内置，模型权重外置。所有涉及重型深度学习库（如 PyTorch, CUDA）的操作，均通过 Wrapper 类实现了严格的**懒加载（Lazy Import）**。
- **模块化乐高组合**：六大算子库相互独立，上层业务脚本可自由编排调度链路。
- **统一 invoke / 能力声明**：领域算子通过 `CapabilitySpec` 注册；编排层可用 `quic_op.list_capabilities()` / `quic_op.invoke(operator_id, ...)` 统一发现与调用。

## 2. 环境依赖与安装

### 2.1 基础运行环境
- 操作系统：Linux (Ubuntu/CentOS), macOS, Windows 10/11
- Python 版本：>= 3.9

### 2.2 安装方式
在项目根目录下执行：
```bash
# 1. 创建虚拟环境 (推荐)
python3 -m venv venv
source venv/bin/activate

# 2. 基础安装 (仅支持 CPU 轻量算子，如 clean / core，包积极小)
pip install -e .

# 3. 人手 Pose 检测（默认 CPU；GPU 见 hand-pose-gpu）
pip install -e ".[hand-pose]"
# GPU 节点：
# pip install -e ".[hand-pose-gpu]"
# （代码默认 CPU；仅 CUDA 可用且 GPU extras 完整时自动用 GPU）

# 4. 完整感知 GPU 合集
pip install -e ".[gpu]"
```

发行版本由各包 `__init__.py` 中的 `__version__` 管理；根包 `quic_op.__version__` 通过
`pyproject.toml` 的 `dynamic = ["version"]` 注入发行元数据，**不在 pyproject.toml 中硬编码版本号**。

## 3. 六大核心算子库架构概览

| 库名称 | 包路径 | 业务定位 | 核心能力 |
| :--- | :--- | :--- | :--- |
| **时空同步库** | `quic_op.sync` | 多源异构传感器的时空锚定中心 | 互相关时间补偿、SLAM 轨迹精修、点云去运动畸变 |
| **基础清洗库** | `quic_op.clean` | 纯 CPU/IO 密集型图像处理大本营 | 视频按时序带重叠切片、均匀抽帧、死区/花屏剔除 |
| **合规脱敏库** | `quic_op.compliance` | 企业级数据安全门禁 | PII 像素化模糊 (人脸/车牌)、敏感音频消音 |
| **物理感知库** | `quic_op.perception` | 从 2D 逆解 3D 物理世界的重 GPU 区 | 人手 Pose 检测、稠密深度预测、3D 手部网格重建、H2R 动作映射 |
| **语义标注库** | `quic_op.label` | 与 VLM / SAM 大模型交互中枢 | 隐式接触表生成、物理时空雕刻、动名词短语打标 |
| **导出质量库** | `quic_op.export` | 输出下游算法标准资产的最后一道质量网关 | QRDF 封装、Schema 强校验、RLDS 分片导出 |

## 4. 目录结构与隔离规范

项目采用了极度严格的物理隔离规范：
- `core/`：跨库共享基座，定义了全系统的 `data_types.py`、`persistence.py`（JSON 持久化）以及 `capability.py` / `invoke.py`（统一能力声明与调度）；严禁引入任何第三方重度包。
- `models/` 子目录：存在于 `perception`、`label` 等需要加载深度学习模型的库中，作为防腐层，隔离了底层的 PyTorch 逻辑与上层的业务逻辑。
- 算子过程数据统一经 `JsonResultPersister` 落盘为 JSON；对外返回结构化契约的同时给出 JSON 绝对路径（参见 `perception/README_YOLO.md`）。
- 领域包通过 `capabilities.py` 向 SDK 注册可 invoke 的算子（当前 perception 已注册人手 Pose 相关能力，clean 已注册低光/模糊/掉帧）。
