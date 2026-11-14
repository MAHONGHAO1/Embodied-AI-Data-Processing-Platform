# 关节值出现 NaN

第 2 行 observation.state 的第 2 个关节改为 NaN。

应当命中的判据：

- `numeric_finite`

实测（`python scripts/make_known_bad.py` 自动记录，任务状态 `issues`）：

- `numeric_finite`

这个目录是故意做坏的，不是采集缺陷样本。判据来源见 `docs/RULES.md`。
