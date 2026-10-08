# UAT 数据集有效范围导出证据

环境：`studio-uat.quicrobot.xyz`，`ecs-data` 的 `/opt/quic_studio/uat/quic_studio`，独立 `quicstudio-uat-source` 服务。只使用验收资产；不操作生产，不注册训练或发起训练。签名地址和凭据不写入本记录。

## 固定输入

- UI 创建 dataset=2，版本号 v1 / version_id=2，引用 asset=2 / workspace=3 / batch=2 / Episode=3。
- 固定 admission_attempt=1、annotation_submission_id=2、annotation_revision_id=2 / version=2。
- 版本 snapshot SHA-256：`b0bbd4b34e7c81fff0743b9c50dd75a74a56a6ca357031d294f7f0ac72f56ae8`。
- 资产 snapshot SHA-256：`2963fa8179f7233061ed294542aec1983ea6c94f8230c4e28d3470a0bd9b2e79`。
- 来源 fingerprint：`f933e40d11019f3dd0eda0663ef2f5357dce60a1bfb69b1ddab4c5ed69d26594`。
- 原始时间范围 `[1790058677208992000, 1790059096763553024)`，419.554561024 秒；审核通过的两个有效区间共 **19.399389184 秒**，两段之间的空洞不得导出为有效样本。

| 有效区间 `[start_ns, end_ns)` | 固定描述 |
| --- | --- |
| `[1790058698624850944, 1790058708324592128)` | 第一人称视角拍摄笔记本电脑与桌面，记录设备和周边物品。 |
| `[1790058718024297984, 1790058727723945984)` | 第一人称镜头从桌面向周边工作区域移动，记录环境布局与物品；此段为移动视角。 |

## 首次真实导出与修复

- `8ec5569`：UI 发起 QRDF export=2、LeRobot export=3，均为 attempt=1，持久状态 failed，错误 `provider object identity is incomplete`。
- API 读取固定版本确认：raw `data.mcap`（878522358 bytes）及 process archive（961331200 bytes）都有 ETag、SHA-256 和大小，`version_id=null`。Aliyun provider 支持 ETag 条件下载；Catalog 额外强制 version ID，导致尚未读取或转换内容就失败。
- 修复仅允许符合 provider 契约的 ETag 固定对象，仍校验逻辑桶、键、ETag、大小和 SHA-256；有 version ID 的来源必须精确匹配，不能降级为忽略版本。
- 本地隔离回归 **26 passed**：版本化／无版本来源的真实 QRDF 和 LeRobot 分段导出、空洞排除、描述和固定提交、输出及下载身份验证。pre-commit 通过。
- 修复由主任务提交为 `11f4c46` 并部署；API 和 export worker 均 active。主任务使用 UI Retry，生成以下成功 attempt；export=2、3 的失败记录完整保留。

## 实际产物核对

| 格式 | 成功 Export | Attempt | 归档大小 | SHA-256 |
| --- | --- | --- | --- | --- |
| QRDF 0.2 | 4 | 2 | 43722277 bytes | `6165e522a6e9d350fa60010611ce1b24f698d2d38c329b9f4aeb71f1fc2a779f` |
| LeRobot 3.0 | 5 | 2 | 1022838 bytes | `ae2fcd853242891eb65eb495d2c4c4804db6828ac097b873c3c0c9e138bbebf6` |

- 两个归档都通过公网签名 URL 实际完整下载（HTTP 200），本地字节数及 SHA-256 与持久导出记录一致。默认下载参数为 `url_ttl_seconds=3600`、`oss_network=internal`，确认签名使用内网 endpoint；从 `ecs-data` 向内网签名地址实际发起 Range GET，分别返回 `206 bytes 0-0/43722277` 与 `206 bytes 0-0/1022838`，OSS SHA-256 元数据一致。没有输出或保存签名 URL。
- 两个归档的 `quicstudio_export.json` 都引用 version=2 和上述固定版本 snapshot；`quicdata_manifest.json` 中两条记录的描述、精确纳秒范围、源 Episode=3、admission_attempt=1、submission=2、revision=2、来源 fingerprint 全部与固定版本逐字段一致。
- 归档中不含完整 `process.tar`、原始 preview 目录或 worker 中间目录；原始完整来源继续留在原对象，导出交付内容是选定片段。

### QRDF

- 归档只有 9 个文件、2 个输出 Episode，真实 QRDF SDK canonical + annotation 校验通过。
- 两条输出 Episode 的 metadata 时间范围和语言描述、annotation 单条 high-level subtask 的范围和描述与固定提交一致。
- 每条 RGB 均为 291 帧；第一段第一／末帧分别为 `1790058698624850944` / `1790058708291260928`，第二段为 `1790058718024297984` / `1790058727690768128`。
- 遍历全部动态 topic（RGB、depth、pose、IMU、magnetic field），所有时间戳均满足对应 `[start_ns,end_ns)`；两段之间的空洞与源记录其他部分没有进入输出。校准信息保留为上下文，start/stop 事件按区间生成。

### LeRobot

- 归档只有 8 个文件，真实 LeRobot SDK validator 通过；`export_mode=ego_rgb`，2 episodes，194 帧，10 fps。
- parquet 中每条 Episode 恰好 97 帧，数据行分别为 `[0,97)` 和 `[97,194)`，两个 task 文本与上述描述一致。
- MP4 实际解码为 960×720、194 帧、19.4 秒；Episode 视频区间为 `[0,9.7)`、`[9.7,19.4)`。19.4 秒是 10 fps 输出时间格点，源有效区间仍精确保存为 19399389184 ns，未改写为完整 419 秒来源。
- 将全部 194 个解码视频帧与 QRDF 两条有效 Episode 的 10 fps 采样逐帧对比（每 4 个像素抽样、H.264 有损编码）：两段最小 PSNR 分别为 41.450 / 41.133 dB。来源采样末时间戳分别为 `1790058708224850944` / `1790058727624297984`，全部在选定源区间内；视频没有拼入空洞或其他源范围。

## 本地证据与边界

- 归档及解包内容：`/tmp/quicstudio-uat-export-4/`、`/tmp/quicstudio-uat-export-5/`。
- 各目录 `verification.json` 保存不含凭据的范围、数量、哈希、来源、SDK 校验及下载证据；验证 helper 为 `/tmp/quicstudio-uat-verify-catalog-export.py`。
- 本子任务未注册训练数据集，也未发起训练；训练前登记继续由主任务通过 UI 验收。
