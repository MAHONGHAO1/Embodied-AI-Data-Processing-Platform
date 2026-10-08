# clean 模块发布说明

本文档用于说明 `quic_op` 项目中 `clean` 模块的短期独立发布方案，方便下游同事仅安装并使用视频清洗能力。

## 1. 发布目标

当前阶段只发布以下能力：

- `quic_op.clean.VideoEditor`
- `quic_op.clean.GeometryProcessor`

当前阶段暂不对外承诺以下模块可用：

- `sync`
- `perception`
- `label`
- `compliance`
- `export`

因此历史短期独立发布包命名为：

- `quic-op-clean`（已收敛进统一发行包 `quic-op`）

当前根目录 `pyproject.toml` 发行名为 **`quic-op`**，版本号由 `quic_op.__version__` /
`quic_op.clean.__version__` 等包内变量提供（`dynamic = ["version"]`），不再在 toml 中写死。

人手 Pose 等可选依赖通过 extras 安装，例如：

```bash
pip install -e .
pip install -e ".[hand-pose]"
```

## 2. 模块能力边界

### 2.1 VideoEditor

文件位置：
[video_editor.py](file:///Users/qcy/job/qc/data_etl/prd_fx/quic_op/clean/video_editor.py)

对外能力：

- `split_with_overlap()`：视频切片
- `extract_keyframes()`：均匀抽帧
- `drop_dead_frames()`：死帧过滤

### 2.2 GeometryProcessor

文件位置：
[geometry.py](file:///Users/qcy/job/qc/data_etl/prd_fx/quic_op/clean/geometry.py)

对外能力：

- `build_isotropic_intrinsics()`：构造相机内参对象
- `get_opencv_matrices()`：生成 OpenCV 矩阵
- `undistort_image()`：图像去畸变

## 3. 运行依赖

### 3.1 系统依赖

必须提前安装：

- `ffmpeg`
- `ffprobe`

说明：

- `VideoEditor` 通过系统命令调用 `ffmpeg/ffprobe`
- 如果缺少这两个二进制，切片与抽帧能力无法运行

### 3.2 Python 依赖

最小依赖如下：

```bash
pip install numpy opencv-python
```

本次发布包 `pyproject.toml` 基础依赖已声明：

- `numpy>=1.24.0`
- `opencv-python>=4.8.0`

（人手 Pose 等重依赖见 optional-dependencies：`hand-pose` / `gpu`）

## 4. 示例脚本

面向下游同事的最小使用示例：

[clean_demo.py](file:///Users/qcy/job/qc/data_etl/prd_fx/quic_op/examples/clean_demo.py)

默认输入：

`data/episode_000002/media/preview/generations/47f482ec5a1e7e3b/head_rgb.mp4`

默认输出：

`data/out/clean_demo_case/`

运行命令：

```bash
python3 examples/clean_demo.py
```

自定义输入输出：

```bash
python3 examples/clean_demo.py \
  --input-video data/episode_000002/media/preview/generations/47f482ec5a1e7e3b/head_rgb.mp4 \
  --output-dir data/out/clean_demo_case \
  --duration-sec 5 \
  --overlap-sec 1 \
  --frame-interval 30 \
  --max-clips 1
```

## 5. 测试验证

### 5.1 集成测试脚本

测试文件：
[test_clean_module.py](file:///Users/qcy/job/qc/data_etl/prd_fx/quic_op/tests/test_clean_module.py)

该测试会验证：

1. 视频切片
2. 抽帧
3. 死帧过滤
4. 去畸变预览图输出

运行命令：

```bash
python3 -m unittest quic_op.tests.test_clean_module
```

测试输出目录：

`data/out/clean_module_case/`

### 5.2 发布前验收建议

发布前建议至少完成以下检查：

1. `examples/clean_demo.py` 可运行
2. `tests/test_clean_module.py` 可运行
3. 输出目录中已生成：
   - `clips/`
   - `frames/`
   - `undistorted_preview.jpg`

## 6. 打包配置

本次发布使用：

[pyproject.toml](file:///Users/qcy/job/qc/data_etl/prd_fx/quic_op/pyproject.toml)

当前配置特点：

1. 发布包名为 `quic-op-clean`
2. 仅打包：
   - `quic_op`
   - `quic_op.clean`
   - `quic_op.core`
3. 不打包未完成的其他模块

这样做的目的，是确保下游安装后只获得当前已稳定的最小能力集。

## 7. 构建发布包命令

在项目根目录 `quic_op/` 下执行：

### 7.1 安装构建工具

```bash
python3 -m pip install build
```

### 7.2 构建源码包与 wheel 包

```bash
python3 -m build
```

构建完成后，产物会出现在：

```bash
dist/
```

常见产物包括：

- `quic_op_clean-0.1.0.tar.gz`
- `quic_op_clean-0.1.0-py3-none-any.whl`

## 8. 本地安装验证

构建完成后，可在新环境中执行：

```bash
pip install dist/quic_op_clean-0.1.0-py3-none-any.whl
```

然后验证导入：

```bash
python3 -c "from quic_op.clean import VideoEditor, GeometryProcessor; print('clean 模块导入成功')"
```

再运行示例脚本：

```bash
python3 examples/clean_demo.py
```

## 9. 建议的发布节奏

推荐版本节奏：

- `0.1.0`：首个对外可用版本
- `0.1.1`：修复 bug，不改 API
- `0.2.0`：增加 clean 新能力

后续其他模块成熟后，建议继续按同样方式独立发布，例如：

- `quic-op-sync`
- `quic-op-perception`

## 10. 注意事项

1. `clean` 当前依赖 `core`，因此发布时需要同时包含 `quic_op.core`
2. `clean` 的抽帧与切片能力依赖系统安装的 `ffmpeg/ffprobe`
3. `geometry` 依赖 `opencv-python`
4. 如仅需视频切片能力，也建议保持 `opencv-python` 为安装依赖，避免 `quic_op.clean` 导入时因 `GeometryProcessor` 触发缺包异常
