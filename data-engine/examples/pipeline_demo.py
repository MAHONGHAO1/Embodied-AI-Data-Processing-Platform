"""
quic_op SDK 使用示例：全链路具身智能数据处理流水线。
展示如何将各个领域的算子像乐高积木一样串联，实现从原始视频到最终 QRDF 格式的分片导出。
本脚本可以在本地 CPU 环境运行（模型推理会自动降级为 Mock 或懒加载）。
"""
import os
import cv2
import numpy as np

# 从 SDK 统一入口或各独立库导入算子
from quic_op.core import QRDFEpisode
from quic_op.clean import VideoEditor, GeometryProcessor
from quic_op.sync import TimeAligner, SpatialAligner
from quic_op.perception import DepthEstimator, HandReconstructor, HandDetector
from quic_op.compliance import ImageRedactor
from quic_op.label import VLMClient, PromptBuilder
from quic_op.export import SchemaValidator, ShardExporter


def run_demo_pipeline(video_path: str, output_dir: str):
    """
    执行完整的具身数据处理 Demo 链路。
    """
    print("=" * 60)
    print("🚀 启动 quic_op 企业级全链路处理 Demo")
    print("=" * 60)
    
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    # ---------------------------------------------------------
    # 1. 基础清洗阶段 (CPU/IO 密集型)
    # ---------------------------------------------------------
    print("\n>>> [阶段 1] 基础清洗与切片 (clean)")
    editor = VideoEditor()
    
    # 1.1 视频切割 (假设生成 2 个 15s 的切片)
    clip_dir = os.path.join(output_dir, "clips")
    clip_paths = editor.split_with_overlap(
        video_path,
        clip_dir,
        duration_sec=15,
        overlap_sec=3,
        max_clips=2,
    )
    
    for clip_path in clip_paths:
        print(f"\n正在处理切片: {os.path.basename(clip_path)}")
        
        # 1.2 抽帧
        frame_dir = os.path.join(output_dir, "frames", os.path.basename(clip_path))
        frame_paths = editor.extract_keyframes(clip_path, frame_dir, frame_interval=15)
        
        # 1.3 死区与花屏过滤
        valid_frames = editor.drop_dead_frames(frame_paths)
        
        # ---------------------------------------------------------
        # 2. 时空同步与标定 (sync)
        # ---------------------------------------------------------
        print("\n>>> [阶段 2] 时空同步与传感器对齐 (sync)")

        time_aligner = TimeAligner()
        imu_offset = time_aligner.cross_correlation_align(
            [0.0, 0.033, 0.066],
            [0.015, 0.048, 0.081],
        )
        print(f"INFO: 估计的 IMU 时钟偏移为 {imu_offset:.3f}s")
        
        # 假设读取首帧用于估算相机内参 (模拟 GeoCalib)
        if not valid_frames:
            continue
            
        first_frame = cv2.imread(valid_frames[0])
        if first_frame is None:
            # 制造一个假图像用于 Demo 运行
            first_frame = np.zeros((480, 640, 3), dtype=np.uint8)
            
        h, w = first_frame.shape[:2]
        
        # 空间精修 (模拟 DroidCalib)
        spatial_aligner = SpatialAligner(droid_weights_path="/fake/path", geo_weights_path="/fake/path")
        fake_video_frames = np.zeros((len(valid_frames), h, w, 3), dtype=np.uint8)
        align_result = spatial_aligner.refine_camera_trajectory(fake_video_frames)
        
        # 构建几何内参 (调用 clean 的几何纯数学库)
        intrinsics = GeometryProcessor.build_isotropic_intrinsics(align_result["focal_px"], -0.15, w, h)
        print(f"INFO: 获取到精修内参 - 焦距: {intrinsics.focal_length_x}")

        # ---------------------------------------------------------
        # 3. 合规脱敏 (compliance)
        # ---------------------------------------------------------
        print("\n>>> [阶段 3] 合规隐私脱敏 (compliance)")
        redactor = ImageRedactor(confidence_threshold=0.85)
        # 对首帧进行人脸模糊测试
        safe_frame = redactor.face_redact(first_frame)
        
        # ---------------------------------------------------------
        # 4. 物理感知与重建 (perception)
        # ---------------------------------------------------------
        print("\n>>> [阶段 4] 物理感知与 3D 重建 (perception)")
        # 稠密深度估计 (模拟 MoGe-2)
        depth_estimator = DepthEstimator(weights_path="/fake/moge")
        depth_map, _ = depth_estimator.estimate_depth_moge2(first_frame)
        
        # 人手 Pose 检测 (ONNX；未装 ultralytics 时自动 Mock)
        hand_detector = HandDetector()
        hand_pose = hand_detector.detect_frames(
            [first_frame],
            output_dir=os.path.join(output_dir, "yolo"),
            artifact_name=os.path.splitext(os.path.basename(clip_path))[0],
        )
        print(f"INFO: 人手检测完成，JSON -> {hand_pose.json_path}")

        # 3D 手部网格重建 (模拟 HaWoR)
        hand_reconstructor = HandReconstructor(weights_path="/fake/hawor")
        # 提取当前切片所有帧的手部 3D Mesh
        hand_meshes = hand_reconstructor.reconstruct_hands(valid_frames)
        print(f"INFO: 成功提取 {len(hand_meshes)} 帧的 3D 手部网格")

        # ---------------------------------------------------------
        # 5. 语义标注 (label)
        # ---------------------------------------------------------
        print("\n>>> [阶段 5] 语义标注与接触表拼接 (label)")
        # 使用 Prompt Builder 将手部 3D 轨迹重投影并渲染到画面上
        prompt_builder = PromptBuilder(focal_px=intrinsics.focal_length_x)
        
        # 提取左手手腕轨迹作为示例 (取第 0 个关节)
        left_track = np.array([mesh.joints3d[0] for mesh in hand_meshes])
        # 将前 4 帧渲染为轨迹覆盖图
        rendered_frames = prompt_builder.render_trajectory_overlay(
            valid_frames[:4],
            left_track_3d=left_track,
            right_track_3d=np.zeros_like(left_track),  # 右手为空
            output_dir=os.path.join(output_dir, "traj_overlay"),
        )
        
        # 调用 VLM 进行动作打标
        vlm_client = VLMClient()
        semantic_labels = vlm_client.call_ai_func_vlm(rendered_frames)
        print(f"INFO: VLM 标注结果 -> 左手: {semantic_labels['left']}, 右手: {semantic_labels['right']}")

        # ---------------------------------------------------------
        # 6. 数据校验与导出 (export)
        # ---------------------------------------------------------
        print("\n>>> [阶段 6] 格式化校验与分布式导出 (export)")
        
        # 组装为跨库共享契约 QRDFEpisode
        episode = QRDFEpisode(
            episode_id=f"ep_{os.path.basename(clip_path)}",
            video_paths=[clip_path],
            action_states=[{"speed": 1.5}, {"speed": 1.2}],  # 模拟的动作控制状态
            semantic_label=semantic_labels["left"],
        )
        
        # 出域前强 Schema 校验
        validator = SchemaValidator()
        validator.enforce_schema(episode)
        
        # 分布式分片导出 (限制单片 500MB)
        exporter = ShardExporter(output_dir=os.path.join(output_dir, "shards"), shard_size_mb=500)
        exporter.add_episode(episode)
        exporter.finalize()

        _ = safe_frame
        _ = depth_map

    print("\n" + "=" * 60)
    print("✅ Demo 链路执行完毕！数据已落盘。")
    print("=" * 60)

if __name__ == "__main__":
    # 构造一个假的测试视频路径
    dummy_video = "/tmp/dummy_test_video.mp4"
    if not os.path.exists(dummy_video):
        # 使用 OpenCV 生成一段简单的彩色视频
        print(f"INFO: 正在生成测试视频 {dummy_video}...")
        out = cv2.VideoWriter(dummy_video, cv2.VideoWriter_fourcc(*"mp4v"), 30, (640, 480))
        for i in range(90):  # 3秒视频
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            frame[:] = (i * 2 % 255, 100, 200)  # 颜色渐变
            out.write(frame)
        out.release()
        
    out_dir = "/tmp/quic_op_output"
    run_demo_pipeline(dummy_video, out_dir)
