# 视频帧数与行数不符

视频只编码 5 帧，表格有 8 行。

应当命中的判据：

- `video_frame_count`

实测（`python scripts/make_known_bad.py` 自动记录，任务状态 `incomplete`）：

- `video_frame_count`

这个目录是故意做坏的，不是采集缺陷样本。判据来源见 `docs/RULES.md`。
