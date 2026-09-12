# 开发历程（提交信息存档）

> 本文件保存项目在整理发布前的提交信息全文，作为工程决策的叙事记录。
> 当前仓库是整理发布后的结构，提交编号与历史编号不一一对应。
> Git 对象库损坏与抢救的完整记录见 `docs/GIT_HISTORY_ARCHIVE_20260912.md`。

---

## chore: 重建版本历史基线（V0.4 定版）

背景：.git/objects 对象库意外损坏，14 条历史提交的 tree 与 blob
全部缺失，仅抢救回 1 个 commit 对象（已备份至 refs/backup/salvaged-0b22f2e）。
工作区文件完好，本提交以工作区当前状态重建可追溯基线。

内容范围：
- robodata/ 业务状态机、质检规则、转换编排、UI 模块
- robodata_business/ 批次存储
- tools/conversion/ 独立转换环境（worker/preview/failure_checks）
- tests/ 19 个文件 218 个用例
- docs/ 15 篇（含 5 份 VALIDATION、SOP、数据字典）与 v04 界面截图
- app.py 八节点链路 + 卡片边界强化 + 展开块状态着色 + 内部执行管线

.gitignore 已覆盖 .venv/ data/ work/ reports/ *.log，环境与数据不进版本库。

## docs: 归档 Git 历史重建记录与幸存对象取证

- 固化 .git/logs/HEAD 的 14 条 reflog 记录（含 SHA、时间戳、提交信息）
- 保存抢救回的提交信息全文（0b22f2e5，tree 已丢失）
- 记录 3 个幸存 tree 与 1 个幸存 blob 的取证结论
- 导出唯一救回的历史文件版本 app.py（145ee3b，59108 字节）
  至 docs/history/，作为界面改动前的复核基线

## feat: HDF5 数据范围可配置，支持启用全部 10 条 demo

背景：固定 test.hdf5 实际包含 10 条 demo（531 帧），此前硬编码只处理
demo_0-2（174 帧），导致"数据工程平台"看起来只吃得下 174 帧。

改动
- worker.py：新增 _ALL_DEMO_LENGTHS 冻结清单（10 条 demo 的预期帧数）
  与 _select_demos()，按 ROBODATA_HDF5_EPISODES 选择范围；越界或非法
  范围直接失败，不静默降级。校验始终使用冻结清单，不从被检文件自证。
- preview.py：来源统计不一致的警告文案改为按实际启用清单动态生成。
- config.py：新增 parse_episode_range() 与 HDF5_EPISODES，与转换环境
  使用同一环境变量名，保证输入指纹与实际处理范围一致。
- importing.py：PROFILE 引用 HDF5_EPISODES。
- start.ps1：新增 -Episodes 参数；新增「启动工作台-全量数据.cmd」入口。
- README：数据范围表标注全量为 10 条，新增「调整 HDF5 数据范围」小节。

默认范围仍为 demo_0-2（174 帧），V0.4 既有验收数字与截图不受影响。
范围变化会改变输入指纹，已有批次需重新导入，符合现有产物失效设计。

测试：新增 tests/test_episode_range.py 15 项，覆盖区间/单点/去重/非法
输入、默认三条、全量十条 531 帧、越界拒绝；全套 234 项通过。

## fix: 修正重复导入断言，补齐异名批次分支测试

背景：test_duplicate_import_keeps_approval_and_original_quality 自
commit 145ee3b（批次名参与输入指纹）起持续失败，是 V0.4 以来唯一的
长期红项，与界面改动无关。

根因：该提交把批次名纳入输入指纹，语义变为「同名复用既有批次、异名
建立独立批次」（importing.py 有明确注释）。但测试第二次导入使用了
不同批次名 "duplicate test"，却仍断言复用同一 batch_id，与实现意图
冲突。

改动
- 原用例改用 batch["label"] 作为第二次导入的批次名，忠实还原「同源
  同名重复导入」场景，断言复用批次、审核与质检保留。
- 新增 test_duplicate_import_with_new_label_builds_independent_batch，
  覆盖此前从未被测试的「异名建立独立批次」分支，并断言原批次的
  审核状态与产物不受影响。

结果：该文件 5 项全部通过，全套 234 项零失败。

## feat: 全量范围端到端验收通过，新增流式编码开关与运行锁健壮性

背景：ROBODATA_HDF5_EPISODES 扩容路径此前只验证到预览阶段，未证明
全量数据（10 条 / 531 帧）能走完转换与交付。

验收结果（scripts/verify_full_range.py）
- 导入 10 条 / 531 帧；原始预览 30 个采样位置通过
- 输入质检 100/100 项、输出质检 110/110 项，0 数据问题
- 官方库离线加载：531 帧数值比对、30 个采样位置像素一致
- 交付包 531 帧 / 10 条，整包路径可移植，独立加载通过
- 重复导入复用同一批次；源文件 SHA-256 未变

改动
- worker.py：新增 streaming_encoding()，按 ROBODATA_STREAMING_ENCODING 启用
  LeRobot 流式编码。默认关闭以保持既有编码路径。启用后直接向编码器喂帧，
  不落临时图片目录，因此不产生批量删除 —— 这是「临时文件清理被安全策略拦截」
  环境下转换失败（工作进程退出码 1）的根因。
- runtime.py：运行锁清理改为先判断存在再删除。锁持有者退出后文件可能已被其他
  进程清理，原 unlink(missing_ok=True) 在受限删除实现下会对不存在的文件报错，
  中断启动流程。
- README：补充全量验收结论、受限环境编码开关说明与"范围可扩展且已验证"证据行。
- 新增 scripts/verify_full_range.py；脱敏证据摘要 examples/v04-evidence/full-range-success.json。

测试：全套 234 项通过（含新增 15 项范围测试）。

## ci: 新增 GitHub Actions 测试工作流

- windows-latest + uv，与开发环境一致；部分用例验证 Windows Job Object
  的父进程退出保证，仅在 Windows 上执行。
- 只同步主环境。转换相关用例需要 tools/conversion 独立环境与已下载的
  固定样本，缺失时用例自身会 skip；其余用例不依赖下载数据。
- README 补充说明，使「测试通过」成为可在仓库侧复核的状态。

注：本工作流尚未在推送后实际运行，首次执行结果待确认。

## fix: 全量数据入口改名，避免抢占桌面启动器的首个 .cmd

背景：桌面启动器（RoboData 工作台.cmd）通过枚举项目根目录第一个 *.cmd
来启动服务，其设计假设是项目里只有一个 .cmd。

问题：上一提交新增的「启动工作台-全量数据.cmd」因 '-'（U+002D）排在
'.'（U+002E）之前，被 for 优先选中，导致桌面快捷方式启动的不再是原来的
「启动工作台.cmd」，用户表现为"启动不了"。

修复
- 重命名为「启动工作台全量数据.cmd」：'全'（U+5168）排在 '.' 之后，
  根目录枚举顺序恢复为 启动工作台.cmd → 启动工作台全量数据.cmd（已实测）。
- 内容改为纯 ASCII。原「启动工作台.cmd」无中文；新增脚本引入的 UTF-8
  中文在 cmd 默认 GBK 代码页下会乱码，可能导致批处理解析失败。
- README 增补「启动入口命名约束」，避免后续再次踩同一坑。

确认未受影响：start.ps1 的 UTF-8 BOM、.venv、app.py 均正常。

