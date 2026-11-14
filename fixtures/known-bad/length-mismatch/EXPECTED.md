# 表格行数与元数据不符

元数据把 length 写成 12，实际只有 8 行。

应当命中的判据：

- `row_count`

实测（`python scripts/make_known_bad.py` 自动记录，任务状态 `issues`）：

- `row_count`

这个目录是故意做坏的，不是采集缺陷样本。判据来源见 `docs/RULES.md`。
