"""
YOLO 人手 Pose 检测算子 Mock 冒烟测试。
不依赖 GPU / ultralytics；验证契约组装与统一持久化层。
"""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from quic_op.core.data_types import HandPoseOperatorResult
from quic_op.core.persistence import JsonResultPersister, PERSISTENCE_SCHEMA_VERSION
from quic_op.perception import HandDetector


class HandDetectorPersistenceTest(unittest.TestCase):
    def test_detect_frames_returns_bundle_and_json(self):
        # 使用仓库内置 ONNX 路径；未装 ultralytics 时走 Mock
        detector = HandDetector()
        frames = [np.zeros((64, 64, 3), dtype=np.uint8) for _ in range(3)]

        with tempfile.TemporaryDirectory() as tmp:
            result = detector.detect_frames(
                frames,
                conf=0.25,
                output_dir=tmp,
                artifact_name="unit_test",
                fps=30.0,
            )

            self.assertIsInstance(result, HandPoseOperatorResult)
            self.assertEqual(result.num_frames, 3)
            self.assertEqual(result.source_type, "frames")
            self.assertTrue(os.path.isabs(result.json_path))
            self.assertTrue(os.path.exists(result.json_path))

            with open(result.json_path, "r", encoding="utf-8") as file:
                document = json.load(file)

            self.assertEqual(document["schema_version"], PERSISTENCE_SCHEMA_VERSION)
            self.assertEqual(document["operator"], "perception.hand_pose_yolo")
            self.assertEqual(len(document["frames"]), 3)
            self.assertIn("stats", document)
            self.assertEqual(document["meta"]["num_frames"], 3)
            self.assertEqual(document["meta"]["fps"], 30.0)

    def test_persister_build_document_shell(self):
        persister = JsonResultPersister()
        doc = persister.build_document(
            operator="perception.hand_pose_yolo",
            payload={"stats": {"total_frames": 0}, "frames": []},
            meta={"source_type": "frame", "num_frames": 0, "conf_threshold": 0.25},
        )
        self.assertEqual(doc["schema_version"], "1.0")
        self.assertIn("created_at", doc["meta"])


if __name__ == "__main__":
    unittest.main()
