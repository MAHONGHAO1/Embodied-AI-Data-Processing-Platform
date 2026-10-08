# Quic Op API 接口参考手册

所有对外暴露的类名遵循 `PascalCase`，函数、方法、变量与属性遵循 `snake_case`。

## 1. 通用基座 (Core)

### `core.data_types`
定义了系统内流转的标准数据契约。

- **`CameraIntrinsics`**
  - **属性**: `focal_length_x`, `focal_length_y`, `principal_point_x`, `principal_point_y`, `k1`
  - **说明**: 标准各项同性相机内参矩阵模型。

- **`HandMesh3D`**
  - **属性**: `joints3d` (List[List[float]]), `confidence_score` (float), `coordinate_system` (str)
  - **说明**: 表示单帧中的 3D 手部骨架节点坐标。

- **`HandDetectionResult`**
  - **属性**: `frame_id`, `left_hand_present`, `right_hand_present`, `left_bbox`, `right_bbox`, `left_confidence`, `right_confidence`
  - **说明**: 单帧左右手 Pose 检测契约（bbox 为 `[x1,y1,x2,y2]`）。

- **`HandPoseOperatorResult`**
  - **属性**: `frames`, `json_path`, `source_path`, `source_type`, `num_frames`, `fps`, `conf_threshold`, `weights_path`, `stats`, `operator`
  - **说明**: 人手 Pose 检测算子的统一返回契约，同时携带结构化结果与 JSON 绝对路径。
- **`LowLightSample` / `BlurSample` / `FrameDropEvent`**
  - **说明**: clean 质量检测原子契约（低光采样 / 模糊采样 / 掉帧事件）。

- **`LowLightOperatorResult` / `BlurOperatorResult` / `FrameDropOperatorResult`**
  - **说明**: 质量检测算子统一返回契约，同时携带结构化结果与 JSON 绝对路径。

- **`QRDFEpisode`**
  - **属性**: `episode_id`, `video_paths`, `action_states`, `semantic_label`
  - **说明**: 导出阶段使用的标准 Episode 数据契约。

### `core.persistence.JsonResultPersister`
- **`save(operator, payload, output_dir, filename, meta=None) -> str`**
  - **功能**: 将算子过程数据按统一 Schema 写入 JSON，返回绝对路径。

### `core.capability` / `core.invoke`（SDK 统一能力层）
- **`CapabilitySpec`**: 能力声明契约（`operator_id/domain/target/method/version/...`）。
- **`register_capability(spec)` / `list_capabilities(domain=None)` / `get_capability(operator_id)`**
- **`invoke(operator_id, **params) -> Any`**: 按能力声明懒加载并调用算子；属于构造器的参数（如 `weights_path`/`device`）会自动剥离。
- **`describe_capabilities(...) -> List[dict]`**: 导出能力清单供编排层消费。

## 2. 图像清洗算子 (Clean)

### `clean.video_editor.VideoEditor`
- **`split_with_overlap(video_path: str, output_dir: str, duration_sec: int, overlap_sec: int) -> List[str]`**
  - **功能**: 按指定时长与首尾重叠时间对长视频进行物理切分。
  - **抛出**: `VideoCorruptedException`
- **`extract_keyframes(clip_path: str, output_dir: str, frame_interval: int) -> List[str]`**
  - **功能**: 对切片视频执行均匀抽帧。
- **`drop_dead_frames(frame_paths: List[str]) -> List[str]`**
  - **功能**: 过滤全黑帧或死区帧。

### `clean.geometry.GeometryProcessor`
- **`build_isotropic_intrinsics(focal_px: float, k1: float, width: int, height: int) -> CameraIntrinsics`**
  - **功能**: 构造各项同性相机内参对象。
- **`undistort_image(image_array: np.ndarray, intrinsics: CameraIntrinsics, crop: bool = False) -> np.ndarray`**
  - **功能**: 执行 OpenCV 畸变矫正。

### `clean.quality_inspector.QualityInspector`
- **初始化**: `__init__(persister: JsonResultPersister | None = None)`
- **`detect_lowlight_metrics(video_path, ..., output_dir=None) -> LowLightOperatorResult`**
  - **功能**: 低光逐采样帧检测，返回结构化结果并写出 JSON。
- **`detect_blur_metrics(video_path, ..., output_dir=None) -> BlurOperatorResult`**
  - **功能**: 模糊逐采样帧检测，返回结构化结果并写出 JSON。
- **`detect_frame_drop_metrics(video_path, ..., output_dir=None) -> FrameDropOperatorResult`**
  - **功能**: 基于 PTS 的掉帧事件检测，返回结构化结果并写出 JSON。
  - **详细契约**: 见 `clean/README_CLEAN.md`。

## 3. 物理感知算子 (Perception)

### `perception.mesh.HandReconstructor`
- **初始化**: `__init__(weights_path: str)`
  - **说明**: 初始化时必须传入外部模型权重路径，内部会实例化防腐 Wrapper，此时不会触发 GPU 环境加载。
- **`reconstruct_hands(frames_list: List[object]) -> List[HandMesh3D]`**
  - **功能**: 对传入的帧序列进行推理，返回符合 Core 标准的 3D 手部网格实体列表。

### `perception.detecthands.HandDetector`
- **初始化**: `__init__(weights_path: str | None = None, persister: JsonResultPersister | None = None, device: str | None = None)`
  - **说明**: 默认加载 `perception/weights/YOLO/detector.onnx`（仅支持 `.onnx`）；构造时不触发推理依赖导入。默认 CPU 推理；仅当 GPU 可用且 `hand-pose-gpu` 运行时完整时自动使用 GPU。
- **`detect_video(video_path: str, conf: float = 0.25, output_dir: str | None = None) -> HandPoseOperatorResult`**
  - **功能**: 对本地视频路径逐帧检测，返回结构化结果并写出 JSON。
- **`detect_hdf5(hdf5_path: str, conf: float = 0.25, output_dir: str | None = None) -> HandPoseOperatorResult`**
  - **功能**: 对 HDF5 episode（`frame/head` + `meta_info/fps`）逐帧检测。
- **`detect_frames(frames, conf: float = 0.25, output_dir: str | None = None, artifact_name: str = "memory_frames", fps: float | None = None) -> HandPoseOperatorResult`**
  - **功能**: 对内存 BGR 帧序列检测（云边端同构入口）。
- **`detect_frame(frame, conf: float = 0.25, output_dir: str | None = None, artifact_name: str = "single_frame", persist: bool = True) -> HandPoseOperatorResult`**
  - **功能**: 对单帧 BGR 图像检测；`persist=False` 时不落盘。
- **`get_stats(results) -> dict`**
  - **功能**: 统计左右手出现频次。详细 Schema 见 `perception/README_YOLO.md`。

### `perception.depth.DepthEstimator`
- **`estimate_depth_moge2(image_array: np.ndarray) -> Tuple[np.ndarray, float]`**
  - **功能**: 对单张 RGB 图像估计稠密深度和焦距。

### `perception.physics.KinematicsAnalyzer`
- **`find_velocity_peak(hand_trajectories: List[HandMesh3D], fps: float) -> List[int]`**
  - **功能**: 识别速度峰值帧。
- **`retarget_to_dexhand(human_mesh: HandMesh3D, robot_urdf: str) -> Dict[str, float]`**
  - **功能**: 将人手姿态重定向为机器人关节角。

## 4. 时空同步算子 (Sync)

### `sync.time_align.TimeAligner`
- **`cross_correlation_align(video_ts: List[float], other_ts: List[float], max_shift: int = 100) -> float`**
  - **功能**: 估计两路时间序列的对齐偏移量。

### `sync.slam_fusion.SpatialAligner`
- **`refine_camera_trajectory(video_frames: np.ndarray, init_intrinsics: CameraIntrinsics | None = None) -> Dict[str, Any]`**
  - **功能**: 进行相机轨迹与焦距精修。

## 5. 语义标注算子 (Label)

### `label.vlm_client.VLMClient`
- **`call_ai_func_vlm(image_paths: List[str], prompt_template: str = "") -> Dict[str, str]`**
  - **功能**: 调用视觉大模型返回左右手动作短语。

### `label.prompt_builder.PromptBuilder`
- **`render_trajectory_overlay(frame_paths: List[str], left_track_3d: np.ndarray, right_track_3d: np.ndarray, output_dir: str) -> List[str]`**
  - **功能**: 将 3D 轨迹重投影到 2D 图像并输出覆盖图。

## 6. 合规与导出 (Compliance / Export)

### `compliance.image_redact.ImageRedactor`
- **`face_redact(image_array: Any) -> Any`**
  - **功能**: 执行人脸脱敏。

### `export.validator.SchemaValidator`
- **`enforce_schema(episode_data: QRDFEpisode, schema_version: str = "v1.0") -> bool`**
  - **功能**: 执行导出前的强 Schema 校验。

### `export.shard_exporter.ShardExporter`
- **`add_episode(episode_data: QRDFEpisode) -> None`**
  - **功能**: 将单个 Episode 加入分片队列。
- **`finalize() -> None`**
  - **功能**: 刷写剩余缓冲并结束导出。
