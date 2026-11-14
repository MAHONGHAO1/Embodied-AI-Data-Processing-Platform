# known-bad 故障样本

每个目录是一个故意做坏的小数据集，附 `EXPECTED.md` 写明应当命中的判据，以及生成时实测命中的判据。

它们用来验证判据本身有没有在工作，也能拿去测你自己的检查器。

生成方式：`python scripts/make_known_bad.py`（会重建整个目录，并在实测不符合预期时失败）。

判据定义见 `docs/RULES.md`。
