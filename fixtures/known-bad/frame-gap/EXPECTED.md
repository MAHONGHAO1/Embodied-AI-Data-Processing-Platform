# 帧索引跳号

第 3 行的 frame_index 从 3 改成 5，中间空出一帧。

应当命中的判据：

- `frame_index`

实测（`python scripts/make_known_bad.py` 自动记录，任务状态 `issues`）：

- `frame_index`

这个目录是故意做坏的，不是采集缺陷样本。判据来源见 `docs/RULES.md`。
