# Git 历史档案（2026-09-12 重建记录）

## 事件说明

2026-09-12，本仓库 `.git/objects` 对象库意外损坏：14 条历史提交的
tree 与 blob 对象全部缺失，`.git/refs/heads/` 目录消失，`.git/packed-refs`
不存在。工作区文件完好无损。

抢救结果：

- `git fsck --lost-found` 找回 **1 个 commit 对象**（`0b22f2e5`），其 tree
  （`cfc87507`）亦已丢失，无法还原文件内容
- `.git/logs/HEAD`（reflog）**完好**，保留了 14 条提交的完整 SHA、作者、时间戳与提交信息
- 其余 13 条提交对象不可恢复

处置：以工作区当前状态重建单次基线提交 `80b74c8`，并将 reflog 证据
固化于本文件留档。

---

## 一、reflog 原始记录（`.git/logs/HEAD`，按时间序）

提交链路（自 V0.4 定版起）：

| # | commit | 时间 (+0800) | 提交信息 |
| --- | --- | --- | --- |
| 1 | `64d4ac9` | 1789069029<br>2026-09-11 03:37 | feat: 恢复八节点导航并完成 V0.4 交付包装 |
| 2 | `e75d551` | 1789070829<br>2026-09-11 04:07 | feat: 前端专业度、可理解性与耗时可视化优化 |
| 3 | `b9a65ba` | 1789070966<br>2026-09-11 04:09 | docs: 同步更新界面截图至八节点序号版与展开态 |
| 4 | `6c1453b` | 1789092714<br>2026-09-11 10:11 | feat: 八节点进度条改造为带箭头的流程图 |
| 5 | `dddad66` | 1789093227<br>2026-09-11 10:20 | chore: 关闭 headless，桌面双击启动后自动打开浏览器 |
| 6 | `0b22f2e` | 1789094627<br>2026-09-11 10:43 | feat: 区分格式转换的两类阻断原因，避免被误认为故障 |
| 7 | `fdc79fc` | 1789094948<br>2026-09-11 10:49 | fix: 修正「来源未记录时间定义」兜底文案的表述与可读性 |
| 8 | `a80cee4` | 1789095843<br>2026-09-11 11:04 | fix: 导入路径容错，自动处理带引号的粘贴内容 |
| 9 | `e9dad36` | 1789096443<br>2026-09-11 11:14 | fix: 故障批次的清洗／筛选节点不再显示为绿色 |
| 10 | `145ee3b` | 1789097305<br>2026-09-11 11:28 | feat: 批次名参与输入指纹，支持同源数据建立独立批次 |
| 11 | `80b74c8` | 1789207285<br>2026-09-12 18:01 | chore: 重建版本历史基线（V0.4 定版）← **当前基线** |

> 另有 3 条早期 reflog 条目指向 `e4c90db` / `64d4ac9`，为同一提交的
> 重复登记（含 1 条 `unknown <MA@MA.(none)>` 身份缺失记录）。

### reflog 原文（供校验）

```
0000000000000000000000000000000000000000 e4c90db48403b558248b5355edcce84a0cb3695d MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789068993 +0800	commit (initial): feat: 恢复八节点导航并完成 V0.4 交付包装
0000000000000000000000000000000000000000 64d4ac961aea7fe1c0ef21628592a20cbae0d8af MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789069029 +0800	commit (initial): feat: 恢复八节点导航并完成 V0.4 交付包装
0000000000000000000000000000000000000000 64d4ac961aea7fe1c0ef21628592a20cbae0d8af unknown <MA@MA.(none)> 1789069073 +0800
64d4ac961aea7fe1c0ef21628592a20cbae0d8af e75d5516dd9e5350bed74d976fb521f90efc67e7 MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789070829 +0800	commit: feat: 前端专业度、可理解性与耗时可视化优化
e75d5516dd9e5350bed74d976fb521f90efc67e7 b9a65baa02cd06ecc969fc0b1f66d847313dc489 MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789070966 +0800	commit: docs: 同步更新界面截图至八节点序号版与展开态
b9a65baa02cd06ecc969fc0b1f66d847313dc489 6c1453be8b1e149c4e2d97083c6af1b00ed1707b MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789092714 +0800	commit: feat: 八节点进度条改造为带箭头的流程图
6c1453be8b1e149c4e2d97083c6af1b00ed1707b dddad667184b11c8a13b20d968be4589d5521193 MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789093227 +0800	commit: chore: 关闭 headless，桌面双击启动后自动打开浏览器
dddad667184b11c8a13b20d968be4589d5521193 0b22f2e558663f5d4b613671557e9509cd48654d MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789094627 +0800	commit: feat: 区分格式转换的两类阻断原因，避免被误认为故障
0b22f2e558663f5d4b613671557e9509cd48654d fdc79fc69d388f242840efce516ac646249006fa MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789094948 +0800	commit: fix: 修正「来源未记录时间定义」兜底文案的表述与可读性
fdc79fc69d388f242840efce516ac646249006fa a80cee4f127104cad02af0b4f23b10d1d4e5c065 MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789095843 +0800	commit: fix: 导入路径容错，自动处理带引号的粘贴内容
a80cee4f127104cad02af0b4f23b10d1d4e5c065 e9dad36edbfa85058ea3aa819a97afc024e2ff63 MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789096443 +0800	commit: fix: 故障批次的清洗／筛选节点不再显示为绿色
e9dad36edbfa85058ea3aa819a97afc024e2ff63 145ee3b3899f81f4a1f1996a95b14404614b2915 MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789097305 +0800	commit: feat: 批次名参与输入指纹，支持同源数据建立独立批次
0000000000000000000000000000000000000000 80b74c833431214c7c0957d0fe90a026df95b7b0 MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789207285 +0800	commit (initial): chore: 重建版本历史基线（V0.4 定版）
```

---

## 二、抢救回的提交对象全文（`0b22f2e5`）

`git cat-file -p 0b22f2e558663f5d4b613671557e9509cd48654d` 输出：

```
tree cfc8750761afb1e345c0d7343d6e2183fc5050c6
parent dddad667184b11c8a13b20d968be4589d5521193
author MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789094627 +0800
committer MAHONGHAO <MAHONGHAO1@users.noreply.github.com> 1789094627 +0800

feat: 区分格式转换的两类阻断原因，避免被误认为故障

背景：用户在 SO-100 批次点开格式转换节点，看到灰字"暂不能转换：当前业务转换
仅支持已验证的 HDF5 配置"，误以为是功能故障或没有自动执行。

改动
- 把阻断原因拆成两类并分别用不同样式呈现：
  - 批次类型不在转换范围内（设计边界）→ st.info，明确写出"这是设计边界，不是故障"，
    并说明 SO-100 本身已是 LeRobot v2.0 官方格式、本节点无需再写入，
    同时指引用户切换到 HDF5 批次查看转换链路
  - 前置条件未满足（未审核完、质检未过、批次运行中）→ st.warning，逐条列出
- 原有单行 caption 提示信息量不足，用户无法判断"该做什么"或"是否正常"

测试：转换页断言由 caption 改为 warning，页面测试 34 项通过。
```

该 commit 的 tree 对象（`cfc87507`）已随对象库一并丢失，文件内容无法还原；
上述提交信息是本次唯一可完整还原的历史内容。

---

## 三、未纳入版本库的目录（`.gitignore`）

```
.venv/          # 主环境 416 MB
__pycache__/
.pytest_cache/
*.pyc
data/           # 95 MB 公开数据集
reports/
.env
.env.*
*.local.*
work/           # 运行记录、批次快照、导出产物
dist/
*.log
```

`tools/conversion/.venv`（2.2 GB）被 `.venv/` 规则覆盖，同样不进入版本库。

---

## 四、教训与后续防护

1. **`git stash` 在有沙箱 safe-delete 拦截的环境下高风险** —— 本次损坏高度
   怀疑由该操作触发（stash 未成功，但对象库被打残）。
2. 建议启用 `git config gc.auto 0` 并定期手工 `git gc`，避免自动清理在
   受限环境下产生不可预期行为。
3. 重要节点建议同时推送远端（GitHub），本地对象库损坏时远端即备份。
4. 本次已确认：`.git/logs` 与工作区文件的生命周期相互独立，**工作区才是
   最后防线**；日常应保持工作区干净，勿用 `git checkout --` 之类的操作。

---

## 五、幸存对象取证（补充）

对象库中共 **3 个 tree + 1 个 blob + 1 个 commit** 幸存，逐个确认如下：

### 3 个目录树（均为提交 `0b22f2e` 时的目录快照）

| tree SHA | 对应目录 | 条目数 |
| --- | --- | --- |
| `06e7a523` | `docs/assets/v04/` | 11（界面截图） |
| `075d1eff` | `robodata/` | 15（含 profiles 子树） |
| `0839b8fd` | `docs/` | 15（含 assets 子树） |

> 说明：这些 tree 引用的 blob 在 `git add` 之后大多可被 `git cat-file` 找到，
> 但**那是内容寻址的巧合** —— 重建基线提交时把工作区文件写入了对象库，
> 内容相同的文件共享同一 SHA。这不代表历史版本被还原。

### 1 个幸存 blob（唯一被完整救回的历史文件内容）

| blob SHA | 大小 | 内容 |
| --- | --- | --- |
| `0b34b3ce` | 59108 字节 | `app.py` 在提交 `145ee3b` 时的版本 |

该 blob 在对象库中处于 **dangling（无任何引用）** 状态，是本次唯一被完整
救回的历史文件。已导出为 `docs/history/app.py.salvaged-59108b.py` 留档。

**取证价值：** 它与当前 `app.py`（70453 字节）的 247 行差异，精确对应
2026-09-11 之后的所有界面改动，可作为"改动前基线"复核：

- 新增 `from contextlib import contextmanager`
- 卡片边界强化：`stVerticalBlockBorderWrapper` 由 `background:#fff`
  改为 `background:#ffffff; border:1px solid #e2e8f0; box-shadow:...`
- 八节点进度条由 `grid` 四列布局重构为带 SVG 箭头的流程条
  （新增 `.rd-progress-row` / `.rd-progress-num` / `.rd-progress-name` /
  `.rd-progress-state` / `.rd-progress-arrow` 等规则）
- 展开块状态着色（`.streamlit-expander:has(...)`）与内部执行管线
  （`.rd-pipe*`）为纯新增，不对应删除行

