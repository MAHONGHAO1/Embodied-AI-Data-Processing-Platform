# 时间戳跳了一段

第 5 行之后整体平移 10 秒，采样还在继续但中间空了一段。

应当命中的判据：

- `timestamp_interval`
- `video_timestamp_mapping`

实测（`python scripts/make_known_bad.py` 自动记录，任务状态 `issues`）：

- `timestamp_interval`
- `video_timestamp_mapping`

这个目录是故意做坏的，不是采集缺陷样本。判据来源见 `docs/RULES.md`。
