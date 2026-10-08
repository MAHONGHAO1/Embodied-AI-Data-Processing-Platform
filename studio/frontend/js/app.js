/* Static Vue console for the Batch / Episode workflow. */
(() => {
  const { createApp, computed, onMounted, reactive, ref } = Vue;
  const nextTick = typeof Vue.nextTick === 'function' ? Vue.nextTick : async () => {};
  const onBeforeUnmount = typeof Vue.onBeforeUnmount === 'function' ? Vue.onBeforeUnmount : () => {};
  const watch = typeof Vue.watch === 'function' ? Vue.watch : () => {};
  const { ElMessage, ElMessageBox } = ElementPlus;
  const WORK_QUEUE_PAGE_SIZES = [50, 100];
  const EPISODE_PAGE_SIZES = [50, 100, 200];
  const VIEWS = new Set(['overview', 'intake', 'batches', 'work-queue', 'workbench', 'intake-review', 'package-workbench', 'resources', 'assets', 'datasets', 'trainDash', 'trainJobs', 'trainNew', 'trainDatasets', 'trainModels', 'trainResources', 'trainSystem', 'miningTasks', 'miningDash', 'admin', 'settings']);

  const COLLECTION_VIEWS = new Set(['miningTasks', 'miningDash', 'batches', 'intake', 'intake-review']);

  const MESSAGES = {
    'zh-CN': {
      metadata: '基础信息', assignDateLabel: '分配时间', loadFailed: '加载失败，请重试',
      brand: 'QuicStudio', product: '数据控制台', signIn: '登录', signOut: '退出', email: '邮箱', password: '密码', localDemo: '进入本地演示', demoEntered: '已进入本地演示',
      restoringSession: '正在进入数据控制台...',
      changePassword: '修改密码', changePasswordRequired: '请先修改初始密码后再进入控制台。', currentPassword: '原密码', newPassword: '新密码', confirmPassword: '确认新密码',
      passwordMismatch: '两次输入的新密码不一致', passwordTooShort: '新密码至少需要 10 位', passwordChanged: '密码已修改，请使用新密码重新登录。', backToConsole: '返回控制台',
      apiTokens: 'API 令牌', apiTokenCreate: '创建令牌', apiTokenOnce: '该令牌仅显示一次，关闭后无法再次查看。', apiTokenCopy: '复制',
      apiTokenName: '名称', apiTokenNameRequired: '请输入令牌名称', apiTokenExpires: '过期时间', apiTokenCreated: '创建时间',
      apiTokenNever: '永不过期', apiTokenLastUsed: '最后使用', apiTokenActive: '有效', apiTokenRevoked: '已吊销',
      apiTokenExpired: '已过期', apiTokenRotate: '轮换', apiTokenRotateConfirm: '轮换将生成新的 secret，旧 secret 在一小时内仍有效。继续？',
      apiTokenRevokeConfirm: '确定吊销令牌「{name}」？吊销后立即失效。', apiTokenRevoked: '令牌已吊销', apiTokenCopied: '已复制', apiTokenCopyFailed: '复制失败，请手动复制',
      emptyApiTokens: '暂无令牌。创建一枚令牌供外部工具（duance / 算法）调用平台。', daysUnit: '天',
      workspace: '数采工作空间', sourceWorkspace: '来源数采工作空间', allWorkspaces: '全部数采工作空间', workspaceName: '数采工作空间名称', taskSet: '采集项目', projectLabel: '项目', managementCenter: '管理中心', createWorkspace: '新建数采工作空间', createTaskSet: '新建采集项目', description: '说明',
      overview: '概览', dataOverview: '所有数据总览', refreshDashboard: '刷新数据', dashboardEmpty: '看板快照尚未生成，可点击刷新触发统计。', dashboardMockHint: '当前为样式预览模拟数据，真实 ADS 快照生成后将自动替换。', dashboardLoadFailed: '看板数据加载失败，请重试。', scopeChanged: '数采工作空间已切换，请重新打开当前操作。',
      kpiTotalEpisodes: '数据总数', kpiTotalDuration: 'Episode 总时长（含派生）', kpiCollectedToday: '今日接入（UTC）', kpiAnnotationCompleted: '标注完成数', kpiAnnotationRate: '标注完成比率',
      vsYesterday: '较昨日', pipelineFunnel: '数据流水漏斗', avgDuration: '平均时长', todayTasks: '今日任务', collectTrend7d: '近7天接入（UTC）', deviceDistribution: '设备分布数量',
      queueName: '名称', queueCount: '数量', queueShare: '占比', queuePending: '待完成', devicesUnit: '设备(个)', durationUnknown: '未知',
      'intake-review': '数采审核', intake: '数据概览', batches: '数采审核', episodes: 'Episodes', workQueue: '工作队列', workbench: '工作台', resources: '采集资源', assets: '数据资产', datasets: '数据集', trainDash: '训练总览', trainJobs: '训练任务', trainNew: '新建训练', trainDatasets: '训练数据集', trainModels: '模型与 Recipe', trainResources: '训练资源', trainSystem: '系统健康', trainNav: '训练', managementCenter: '管理中心', admin: '数采工作空间', settings: '项目',
      currentTaskSet: '当前采集项目', noTaskSet: '创建采集项目后即可开始建立采集批次和导入数据。', externalProject: '外部项目', externalProjectUnbound: '未绑定', externalProjectBound: '已绑定',
      workflowOverview: '流程概览', pendingWork: '待处理工作', recentEpisodes: '近期 Episode', goProcess: '去处理',
      stageImport: '数据接入', stageQuality: '质量检查', stageHuman: '人工处理', stageReview: '审核', stagePublish: '发布',
      createBatch: '新建采集批次', batchName: '名称', batchType: '数据类型', source: '来源', pipeline: '导入方式',
      batchStatus: '数据包状态', importData: '导入数据', refresh: '刷新', allStatus: '全部状态',
      importHistory: '导入记录', validCollectedDuration: '有效采集时长', sourceEpisodes: '源 Episode', batchSummary: '数据包概要', activity: '活动', noActivity: '暂无可展示的数据包活动。', episodesDetail: '数据详情', importUid: '数据名称', importState: '导入状态', importStateImported: '已导入', importStateImporting: '导入中', importStateTodo: '待导入',
      intakeStepBatch: '1. 选择采集批次', intakeStepSource: '2. 选择接入方式', intakeStepTrack: '3. 跟踪处理状态', intakeSelectBatch: '选择需要接入数据的采集批次', intakeBatchHint: '采集批次负责归档来源、采集任务和质量进度；数据文件会接入到所选批次。', intakeUploadHint: '上传 QRDF ZIP 或 MCAP，支持分片与断点续传。', intakeOssHint: '扫描平台已授权的 OSS 前缀并选择候选数据。', intakeChooseBatchFirst: '请先选择一个采集批次。',
      activityBatchLog: '批次状态', activityImportSession: '导入记录', activityJobRun: '系统作业', progress: '进度',
      jobImportParse: '解析导入', jobQualityCheck: '质量检查', jobPreview: '生成预览', jobPublication: '发布', jobProcessing: '系统处理',
      jobQueued: '等待执行', jobRunning: '执行中', jobSucceeded: '完成', jobFailed: '失败', jobCancelled: '已取消',
      importFile: '上传文件', importOss: '选择已有 OSS 数据', chooseFile: '选择文件', startImport: '开始导入',
      intakeGuideTitle: '数据包由 duance 桌面工具上传，控制台负责审核与构建。',
      intakeGuideStepProject: '1. 在「采集项目 → 采集任务」下确认数据包清单与目标时长。',
      intakeGuideStepManifest: '2. 采集前下载离线清单，或让运维携带清单到现场。',
      intakeGuideStepUpload: '3. 现场按清单绑定文件（一个数据包可含多个 Episode），联网后由 duance 断点上传。',
      intakeGuideStepReview: '4. 平台解析完成后回到「数采审核」勾选数据包并通过入库审核。',
      intakeGuideOfflineHint: '离线环境请先下载清单再出发；上传完成后可在「采集任务」详情里下载平台清单核对 revision。',
      collectionTask: '采集任务', allCollectionTasks: '全部采集任务', noTaskLabel: '未指定', selectCollectionTask: '请选择采集任务', emptyTaskLabels: '尚未配置采集任务词表，请由管理员新建后再导入。',
      createTaskLabel: '新建采集任务', taskLabelName: '名称', taskLabelDescription: '说明', taskLabelCreated: '采集任务已创建',
      fileHint: '支持 QRDF ZIP 或单个 MCAP。浏览器仅分片传输；解析和质检由系统异步完成。',
      ossHint: '系统只扫描已获授权的候选数据，不显示或接收 OSS 路径与凭据。', scanCandidates: '扫描候选', scanQueued: '等待扫描', scanningCandidates: '扫描中', scanRetryPending: '扫描重试中', scanSucceeded: '扫描完成', scanFailed: '扫描失败', noCandidates: '未发现符合条件的可导入 Episode。',
      importCandidate: '导入此候选', importSelectedCandidates: '导入已选', candidates: '可导入候选', candidateStatus: '导入状态', candidateSourceGroup: '来源分组', candidateSourceGroupStatus: '来源分组状态', sourceGroupValid: '有效分组', sourceGroupMissing: '未提供分组', sourceGroupInvalid: '分组无效', sourceGroupLegacy: '历史分组', selectSourceGroup: '整组选择', selectedSourceGroups: '来源组', selectedIndividualCandidates: '单条候选', candidateSourceGroupsTruncated: '仅显示前 {count} 个来源分组；可用筛选或翻页定位其余候选。', candidateDateRangeLimit: '扫描日期范围最多 31 天', captureEndedAt: '采集结束时间', legacySourceGroup: '历史来源分组', timeUnknown: '时间未知', sourceDiscovered: '未导入', sourceImporting: '导入中', sourceImported: '已导入', sourceFailed: '导入失败', viewCandidates: '查看候选', cancel: '取消', retry: '重试',
      currentImport: '当前导入', processingImport: '系统正在处理导入内容', transferringFile: '浏览器正在传输文件',
      uploadAccepted: '文件传输完成，服务器正在合并并写入 raw 存储。', selectCollector: '请选择默认采集员', defaultCollector: '默认采集员（未上报时使用）', selectDevice: '请选择默认主设备',
      uploadLimit: '单文件上限', uploadResuming: '检测到同名未完成上传，已从服务器确认的分片继续。', uploadSessionConflict: '同名文件已有使用其他采集归属的未完成上传，请先取消该记录。',
      episode: 'Episode', kind: '类型', modality: '模态', scene: '场景', quality: '质检', workflow: '流程', review: '审核',
      metrics: '质量指标', referenceFrames: '参考帧', duration: '时长', frameRate: 'RGB 频率', parentEpisode: '父 Episode',
      identityAndLineage: '身份与层级', businessContext: '业务归属', lifecycle: '生命周期', currentWorkflow: '当前工作项', currentStage: '当前阶段', technicalInfo: '技术信息', databaseId: '数据库 ID', workspaceId: '数采工作空间 ID', taskSetId: '采集项目 ID', importSessionId: '导入会话 ID', embodimentId: '机器人形态 ID', taskLabelId: '采集任务 ID', derivationVersion: '派生版本', sourceRange: '源时间范围', episodeDetailLoadFailed: 'Episode 详情加载失败', previewLoadFailed: '预览信息加载失败', retryLoad: '重新加载',
      artifacts: '文件产物', preview: '媒体预览', previewUnavailable: '预览尚未生成或当前不可用', previewPreparing: '预览生成中', previewReady: '预览就绪', previewFailed: '预览生成失败', storageRole: '存储层', size: '大小', publication: '发布状态', rawSourceDownloads: '原始文件下载', loadRawSourceDownloads: '加载原始文件', downloadRawFile: '下载文件', rawSourceUnavailable: '当前无可下载的原始文件。',
      packetMetadata: '数据包元数据', packetSourceEpisode: '源 Episode ID', packetDataFile: '数据文件', packetQrdfVersion: 'QRDF 版本',
      packetTaskName: '采集任务', packetTaskLanguage: '任务描述', packetRobotName: '机器人', packetRobotArms: '臂数', packetRobotBaseFrame: '基准坐标系',
      packetCaptureMode: '采集模式', packetEpisodeType: 'Episode 类型', packetCaptureApp: '采集应用', packetReferenceTopic: '参考相机 Topic',
      packetStartTime: '开始时间', packetEndTime: '结束时间', packetCameras: '相机流', packetSensors: '低维传感器', packetTopic: 'Topic',
      packetResolution: '分辨率', packetFps: '帧率', packetFrequency: '频率', emptyPacketMetadata: '该 Episode 暂无可展示的数据包元数据。',
      queueCut: '切分', queueAnnotation: '标注', queueReview: '审核', queueCompleted: '已完成', workType: '工作类型',
      queueModeWorkItems: '工作项', queueModeGovernance: '批次治理',
      reviewType: '审核类型', allReviewTypes: '全部审核', reviewCut: '分割审核', reviewAnnotation: '标注审核',
      actionClaim: '领取', actionContinue: '继续', actionRelease: '释放', actionSubmit: '提交', actions: '操作',
      actionAccept: '通过', actionReject: '退回', actionReassign: '转派', returnReason: '退回原因', noAction: '无可用操作', workItemUpdated: '工作项已更新，请刷新列表后重试。', assignee: '负责人',
      workPending: '待领取', workAssigned: '已分配', workInProgress: '处理中', workSubmitted: '已提交', workNeedsRework: '待返工', workAccepted: '已完成',
      publicationQueued: '发布排队中', publicationRunning: '发布处理中', publicationSucceeded: '发布成功', publicationCancelled: '发布已取消',
      openWorkbench: '进入工作台', backToQueue: '返回工作队列', cutWorkbench: '切分工作台', annotationWorkbench: '标注工作台', reviewWorkbench: '审核工作台',
      timeline: '时间轴', cutSegments: '切分点列表', addSegment: '新增片段', addBoundary: '在播放点新增分界', deleteBoundary: '删除所选分界', restoreQrBoundary: '恢复到二维码时间', useWholeEpisode: '不切分', setStart: '设为起点', setEnd: '设为终点',
      segmentStart: '开始时间', segmentEnd: '结束时间', remove: '删除', saveDraft: '保存草稿', draftSaved: '草稿已保存', submitForReview: '提交审核',
      cutNote: '切分备注', workbenchUnavailable: '当前工作项无法打开工作台。', noCutSegments: '请至少保留一个片段，或选择不切分。', timelineMappingUnavailable: '精确时间映射不可用，暂时不能调整分界点。', dragBoundary: '左右拖动分界点', dragSegment: '左右拖动片段', partitionInvalid: '切分窗口必须连续覆盖完整 Episode。', submitCutTooLong: '草稿可以保存；提交前请缩短红色的有效片段，或将其标为无效。', annotationIncomplete: '提交审核前请完整填写所有标注片段。', annotationNoRoom: '时间轴没有足够的空闲位置容纳新片段。', partitionedMode: '连续预切分', wholeMode: '完整 Episode', currentPlayhead: '当前播放点', windowDuration: '窗口时长', localAdjustment: '局部精调（前后 15 秒）', boundaryQrEvent: '二维码建议', boundaryAdjusted: '已人工调整', boundaryHuman: '人工分界', segmentIncluded: '有效', segmentExcluded: '无效', exclusionReason: '无效原因（可选）', reasonOffTask: '非目标动作', reasonIdleOrSetup: '等待或准备', reasonPrivacySensitive: '隐私内容', reasonOther: '其他',
      annotationSegments: '标注片段', annotationDescription: '动作描述', outcome: '结果', outcomeSuccess: '成功', outcomeFailure: '失败', outcomeUnknown: '未知',
      play: '播放', pause: '暂停', stepBackward: '后退 1 秒', stepForward: '前进 1 秒', stepBackwardLarge: '后退 5 秒', stepForwardLarge: '前进 5 秒', snapOn: '吸附开', snapOff: '吸附关', mute: '静音', unmute: '取消静音', playbackRate: '倍速', fullscreen: '全屏', undo: '撤销', redo: '重做', copy: '复制', paste: '粘贴', zoomOut: '缩小时间轴', zoomIn: '放大时间轴', selectBoundary: '选择切分点', resizeTimeline: '调整时间轴高度', shortcutHelp: '快捷键', shortcutClose: '关闭快捷键', shortcutPlayPause: '播放或暂停', shortcutShuttleBackward: '反向快进', shortcutShuttlePause: '暂停快进', shortcutShuttleForward: '正向快进', shortcutPreviousBoundary: '上一个分界', shortcutNextBoundary: '下一个分界', shortcutSnap: '切换吸附', shortcutSelectPrevious: '选择上一个片段', shortcutSelectNext: '选择下一个片段', shortcuts: 'Space 播放/暂停 · N 新建片段 · I 起点 · O 终点',
      segmentContent: '片段内容', rating: '评分', annotationNote: '备注', boundaryType: '分界类型', boundaryTime: '时间点', previewFrame: '帧预览', accountMenu: '账号菜单',
      collector: '采集员', collectorName: '姓名', collectorNumber: '编号', collectorNumberHint: '正整数编号；留空则自动分配', unknownCollector: 'Unknown', collectionDevice: '主采集设备', unknownDevice: 'Unknown', deviceName: '设备名称', deviceType: '设备类型', deviceModel: '型号', serialNumber: 'SN', active: '启用', inactive: '已停用', activate: '启用', deactivate: '停用', createCollector: '新建采集员', addCollector: '添加采集员', addExistingCollector: '添加已有采集员', collectorAddMode: '添加方式', collectorModeCreate: '新建录入', collectorModeExisting: '从已有库选择', selectExistingCollector: '选择已有采集员', emptyAvailableCollectors: '暂无可添加的已有采集员', emptyAvailableCollectorsHint: '仅列出平台已存在但尚未加入当前数采工作空间的采集员。', revokeCollectorMember: '移出空间', collectorMemberRevoked: '已移出当前数采工作空间', confirmRevokeCollector: '确定要将该采集员移出当前数采工作空间吗？（不会删除其历史采集数据）', createDevice: '新建设备', collectorCreated: '采集员已创建', deviceCreated: '设备已创建', emptyCollectors: '当前数采工作空间还没有采集员。', emptyDevices: '当前数采工作空间还没有采集设备。', collectorDurationSummary: '采集员时长汇总', deviceDurationSummary: '设备时长汇总', attribution: '采集归属', attributionUnknown: '未确认', attributionOfflineDeclared: '导入声明', attributionOfflineCurated: '人工校对', attributionMachineReported: '设备上报', attributionOnlineVerified: '在线可信', reportedCollector: '上报采集员', collectorAutomatic: '自动识别', collectorDefault: '采用默认值', collectorUnreported: '未上报', collectorMappingError: '映射异常', collectorFormatError: '编号格式异常', collectorImportBlocked: '该候选需先修正上报采集员映射后才能导入。', qrCodes: '二维码', qrControlCode: '开始 / 结束', qrSegmentCode: '分割', qrControlDescription: '待机时开始采集，录制时结束采集。', qrSegmentDescription: '录制中标记一个分割点。', downloadQr: '下载二维码', print: '打印', collectorIdMissing: '该采集员没有编号，暂不能生成 EGO 控制二维码。', aiSuggestions: 'AI 建议', generateAiSuggestions: '生成 AI 建议', applyAiSuggestion: '采用建议',
      aiUnavailable: 'AI 当前不可用', aiQueued: 'AI 建议已排队', aiFailed: 'AI 建议失败', suggestionStatus: '建议状态',
      reviewTarget: '审核对象', reviewNote: '审核意见', reviewAccept: '通过审核', reviewReject: '退回标注', useSubmittedCollector: '采用系统识别结果', useSubmittedDevice: '采用系统识别结果', overrideCollector: '指定采集员',
      details: '详情', viewAssets: '查看产物', selectEpisodeAssets: '选择一个 Episode 查看文件产物', retentionPolicy: '保留策略', retentionUntil: '保留至', emptyAssets: '该 Episode 尚未产生可展示的文件产物。',
      emptyQueue: '当前没有可处理的工作项。', emptyEpisodes: '当前范围内没有 Episode。', emptyBatches: '当前采集项目还没有采集批次。',
      qualityPending: '质检中', qualityPassed: '质检通过', qualityRecovered: '已恢复，存在数据丢失', qualityFailed: '质检失败',
      allAssetKinds: '全部层级', assetSource: 'Source', assetDerived: 'Derived', allReviewStatuses: '全部审核状态', reviewPending: '待审核', reviewAccepted: '已通过', reviewRejected: '已驳回', reviewNotApplicable: '不适用', allPublicationStatuses: '全部发布状态', publicationUnpublished: '未发布', publicationPublishing: '发布中', publicationPublished: '已发布', publicationFailed: '发布失败', humanStage: '人工阶段', humanPending: '未开始', humanAssigned: '待处理', humanInProgress: '处理中', humanSubmitted: '已提交', humanAccepted: '已完成', humanRejected: '已退回', noDerivedAssets: '暂无派生资产', derivedAssets: '子资产', lineageIssue: '层级异常', lineageMissingParent: '父 Episode 不在当前范围', lineageCycle: '父子关系成环', lineageDepthExceeded: '父链超过 32 层', lineageInvalid: 'Episode 标识无效', assetRange: '源内区间',
      qualityFailureReason: '失败原因', qualityFailureGeneric: '质检未通过，请检查原始文件结构、时间轴和参考相机数据。', qualityWorkerInterrupted: '质检 worker 执行中断，可由操作员重试。', qualityMetadataInvalid: '元数据结构无效或缺失。', qualityRawSourceUnavailable: '原始数据不可用或无法读取。', qualityReferenceTopicMissing: '参考相机数据缺失。', qualityReferenceTimelineIncomplete: '参考相机时间轴未覆盖完整 Episode。',
      importInit: '等待选择文件', importUploading: '上传中', importUploaded: '解析排队中', importParsing: '解析中',
      importSucceeded: '导入完成', importPartial: '部分完成', importFailed: '导入失败', importCancelled: '已取消', importSuperseded: '已替换',
      sourceDataCopyFailed: '源数据拷贝失败', sourceMetadataCopyFailed: '源元数据拷贝失败', sourceCompleteCopyFailed: '完成标记拷贝失败', sourceValidationFailed: '源数据校验失败', sourcePublishFailed: '标准存储发布失败', sourceQualityJobFailed: '质检任务创建失败', sourceImportFailed: '源数据导入失败',
      batchCreated: '已创建', batchImporting: '导入中', batchProcessing: '处理中', batchReady: '已就绪', batchFailed: '失败', batchCancelled: '已取消',
      sourceUpload: '本地上传', sourceOss: 'OSS 候选', sourceFilesystem: '文件系统候选', sourceMixed: '混合来源', sourceUnknown: '尚未接入',
      typeEgo: 'EGO', typeUmi: 'UMI', typeTeleop: '遥操作', typeLerobot: 'LeRobot', typeQrdf: 'QRDF 导入', typeManual: '手动上传',
      language: '中文 / EN', backToBatchesList: '返回数据包列表', viewAllEpisodes: '查看采集项目全部',
      importComplete: '导入完成，已进入质检中', importQueued: '文件已上传，正在解析', batchCreatedToast: '采集批次已创建',
      workspaceCreated: '数采工作空间已创建', taskSetCreated: '采集项目已创建', actionSucceeded: '工作项已更新', workspaceNameExists: '数采工作空间名称不可重复', taskSetNameExists: '相同数采工作空间下采集项目名称不可重复', collectorNameExists: '该数采工作空间内采集员名称重复，请重新输入', deviceSerialExists: '设备SN号重复请重新输入',
      uploadTooLarge: '文件超过当前分片上传上限', uploadEmpty: '不能上传空文件', chooseImportFile: '请选择 QRDF ZIP 或 MCAP 文件',
      annotatorRoleHint: '仅可选择角色为 annotator 的用户，请先在管理中心的数采工作空间中创建或调整用户角色。',
      releasePrompt: '可填写交接说明；取消则不释放。', forceReleasePrompt: '请输入强制释放原因；取消则不释放。', forceReleaseReasonRequired: '强制释放他人的工作项必须填写原因。', status: '状态', createdAt: '创建时间', updatedAt: '更新时间', operationTime: '操作时间', operation: '操作', viewDuration: '查看时长', batchGroupName: '批次名称', batchKind: '数据批次', importRecordKind: '导入记录', expand: '展开', governanceFlow: '数据治理', annotationFlow: '标注流程', complianceCheck: '合规检查', buildBatchTitle: '构建数据批次', buildBatchName: '批次名称', buildBatchNameRequired: '请输入批次名称', buildBatchGovernance: '治理流程', buildBatchAnnotation: '标注流程', buildBatchSelectedCount: '已选择 {count} 项', buildBatchConfirm: '确定', buildBatchCancel: '取消', buildBatchSuccess: '批次构建成功', selectDataFirst: '请先选择数据', enabled: '已启用', publishedAt: '发布时间', clearFilters: '清空筛选', searchEpisode: '搜索 Episode', searchDataset: '搜索数据集', allTaskSets: '全部采集项目', operationDate: '操作时间',
      importType: '接入方式', itemCount: '项', selectedBatch: '当前数据包', taskSetScope: '当前采集项目', batchSequence: '数据包序号', sourceCount: '源数据', totalDuration: '采集时长', availableDuration: '可用片段时长（去重）', qualityProgress: '质检进度',
      dataType: '数据类型', noWorkspace: '当前账号没有可访问的数采工作空间。',
      accessRedirected: '当前账号无权访问该页面，已返回可访问页面。', noConsoleAccess: '当前账号没有可访问的控制台模块。',
      createDataset: '新建数据集', datasetName: '数据集名称', datasetVersions: '版本', createDatasetRevision: '创建版本', datasetsBrowse: '数据集查看', datasetsBuild: '数据集构建', datasetCatalog: '数据集目录', nativeLerobot: '原生 LeRobot', lerobotCatalogImport: 'LeRobot 直导', catalogVersionAssets: '资产清单', nativeLerobotVerified: '已完成标记校验；平台副本完成后才可交付', lerobotDatasetRoot: 'LeRobot 数据集根目录', scanLerobotCandidates: '扫描 LeRobot 候选', lerobotCandidateEmpty: '未发现已完成且可登记的 LeRobot 数据集。', registerLerobot: '登记 LeRobot 数据集', lerobotRegistered: 'LeRobot 数据集已登记，正在复制到平台。', lerobotImportSessions: '导入会话', lerobotStartSession: '开始选择', lerobotSubmitSelection: '提交已选', lerobotSelectionCount: '已选 {count} 项', lerobotNoSession: '请先扫描完成候选，再开始选择。', content: '内容', association: '关联', datasetId: '数据集 ID', fileCount: '文件数', completedAt: '完成时间', ossRootUri: 'OSS 根路径', copyOssPath: '复制 OSS 路径', copyOssUri: '复制 OSS URI', archive: '归档', archived: '已归档', generate: '生成', datasetDelivery: '生成与交付', deliveryUnavailable: '当前环境没有可复制的 OSS 路径，可使用受权下载。', copied: '已复制', resetColumnWidths: '恢复默认列宽', nativePlatformCopy: '平台复制', nativeCopyQueued: '复制排队中', nativeCopyRunning: '正在复制', nativeCopySucceeded: '平台副本已就绪', nativeCopyFailed: '复制失败', nativeCopyBackfillPending: '等待历史回填', nativeCopyVerified: '已验证 {files} 个文件 · {size}', nativeCopyUnavailable: '平台副本尚未完成，暂不可交付。', nativeCopyErrorCopyAccessDenied: '平台无权读取来源或写入目标。', nativeCopyErrorCopyStateInvalid: '复制记录状态无效。', nativeCopyErrorCopyUnavailable: '平台复制暂时不可用。', nativeCopyErrorSourceChanged: '来源内容在复制过程中发生变化。', nativeCopyErrorSourceIdentityUnavailable: '来源对象身份无法安全校验。', nativeCopyErrorSourceScopeChanged: '来源授权已变化，需要重新授权。', nativeCopyErrorTargetConflict: '平台目标已存在冲突数据。', nativeCopyErrorGeneric: '平台复制未完成。', retryPlatformCopy: '重试复制', reauthorizeSource: '重新授权来源', reauthorizeSourceHint: '仅选择与当前数据集完全一致的已授权候选；系统会再次核验完成标记。', reauthorizeSourceEmpty: '未发现匹配的已授权候选。', selectSourceSnapshot: '选择来源快照', packageZip: '打包 ZIP', packagingZip: '正在打包 ZIP', downloadZip: '下载 ZIP', zipReady: 'ZIP 已就绪', zipFailed: 'ZIP 生成失败，可重新发起。', nativeCopyUpdatedAt: '复制更新时间',
      publishedEpisodes: '已发布 Episode', selectPublishedEpisodes: '选择已发布 Episode', emptyDatasets: '当前数采工作空间还没有数据集。',
      emptyDatasetRevisions: '当前数据集还没有版本。', latestExport: '生成结果', export: '生成', download: '下载', exportLerobot: 'LeRobot', exportQrdf: 'QRDF',
      availableCandidates: '可选 Episode', selectedCandidates: '已选 Episode', sourceGroups: '来源组', searchCandidates: '搜索 Episode、任务或采集员', collectionDate: '采集时间', allCandidates: '全部', onlySelected: '仅看已选', allCollectors: '全部采集员', allDevices: '全部设备',
      sourceEpisode: '源 Episode', fullSourceEpisode: '完整源 Episode', derivedEpisode: '派生 Episode', selectionStatus: '选择状态', candidatePreview: '候选预览', chooseCandidatePreview: '点击候选行查看内容', previewRetry: '重新加载预览', sourceTimeline: '源时间轴', timelineUnavailable: '该来源没有可靠的时间映射', currentRange: '源内位置', selectedSummary: '已选摘要', clearSelection: '清空选择', confirmClearSelection: '确定清空当前所有选择吗？', confirmCloseRevisionBuilder: '当前选择尚未创建版本，确定关闭吗？', invalidSelectionTitle: '部分候选状态已变化', revisionCandidatesChanged: '候选列表已刷新，请检查失效项后重试。', candidateLoadFailed: '候选加载失败', createRevisionCount: '创建版本', openDetails: '在详情中打开', noRevisionCandidates: '当前采集项目没有可用于创建版本的已发布 Episode。', noFilteredCandidates: '没有符合当前筛选条件的候选。',
      exportQueued: '导出排队中', exportRunning: '导出中', exportSucceeded: '导出完成', exportFailed: '导出失败', exportCancelled: '导出已取消',
      datasetCreated: '数据集已创建', datasetNameExists: '相同数采工作空间下数据集名称不可重复', datasetRevisionCreated: '数据集版本已创建', exportQueuedToast: '导出已排队', exportRetried: '导出已重新排队',
      userManagement: '用户管理', workspaceMembers: '数采工作空间成员', createManagedUser: '新建用户', userRole: '角色', resetPassword: '重置密码', resetPasswordConfirm: '确定重置用户「{email}」的密码？重置后该用户需使用新临时密码登录并立即修改。', resetPasswordSuccess: '密码已重置', resetPasswordOnce: '临时密码（仅此一次显示）：\n{password}\n\n请把该密码告知用户，用户首次登录后必须修改密码。',
      grantWorkspaceMember: '添加成员', revokeWorkspaceMember: '移除成员', userCreated: '用户已创建', memberGranted: '成员已添加', memberRevoked: '成员已移除',
      emptyManagedUsers: '当前没有可管理的用户。', emptyWorkspaceMembers: '当前数采工作空间没有成员。', selectUser: '请选择用户',
      mining: '采集管理', miningTasks: '数采任务', miningCloud: '数据生产',
      miningDash: '采集概览', miningConfig: '数采配置', miningPlanHint: '数采任务定义交付目标与排期，数据包由目标自动拆解生成。',
      dataBuild: '数据构建', dataOverview: '数据概览', intakeOverviewTab: '采集数据', intakeBatchTab: '数据批次', stageEnabled: '启用', stageDisabled: '未启用', annotateStatusPending: '待领取', annotateReviewApproved: '已通过审批', annotateReviewRejected: '未通过审批', stagePassed: '通过', stageRejected: '不通过', governanceError: '发生错误', annotateUnsubmittedHint: '含未标注数据，不可提交审核', reportFailedReason: '不通过原因', reportErrorReason: '错误原因', annotatedCount: '已标注', pendingReviewCount: '待审批', dataPackageName: '数据包名称', collectedDuration: '采集时长', validDuration: '有效采集时长', uploadTime: '上传时间', videoCountCol: '视频个数', videoCountNumCol: '视频个数', stageRerunDone: '已重新检查完成', rerunIntegrityLabel: '重新完整性', rerunQualityLabel: '重新自动质检', rerunDesensitizeLabel: '重新脱敏', prevVideo: '上一个', nextVideo: '下一个', unannotatedCount: '未标注', annotateStatusProcessing: '处理中', viewAction: '查看', enterAnnotationWorkbench: '进入标注工作台', viewGovernanceReport: '治理报告', governanceReportTitle: '治理报告', reportNoIssues: '无不通过或发生错误的视频', buildBatchNameCol: '数据批次名称', buildDataNameCol: '数据批次名称', dataBatchNameCol: '数据批次名称', governanceStatusCol: '治理状态', governanceNotEnabled: '未启用', governanceFailed: '已失败', governanceIncomplete: '未完成', governanceCompleted: '已完成', governanceNotStarted: '未启动', governanceStatusRunning: '进行中', governanceStatusFailed: '失败', markGovernanceDone: '设为已完成', governanceManualDoneToast: '已手动标记为已完成', collectProject: '采集项目', dataAnnotation: '数据标注', dataTab: '数据', trainTab: '训练', annotationDesc: '标注描述', annotationProgressCol: '标注进度', taskUnclaimed: '任务未领取', annotatedSlashTotal: '已标注/全部', submittedUnreviewed: '已提交未审核', reviewDone: '审核完成', buildDatasetAction: '构建数据集', datasetRemark: '备注', datasetNameRequired: '请输入数据集名称', buildDatasetSuccess: '数据集已生成并放入训练数据集', annotationSegments: '标注片段', approveReview: '通过审核', rejectReview: '未通过审核', reviewRejectedLabel: '已驳回', annotateStatusSubmitted: '已提交', annotateStatusUnreviewed: '未审核', annotateStatusReviewing: '审核中', annotateReviewPassedLabel: '已通过审核', annotateReviewFailedLabel: '未通过审核', annotateLineLabeled: '已标注/全部', annotateLineSubReviewed: '子视频已审核/全部', importSegDesc1: '把球放到盒子里', importSegDesc2: '叠蓝色盒子', importSegDesc3: '叠黄色盒子', importSegDesc4: '松开并回位', buildLogicalDataset: '构建数据批次', importList: '数据批次', importBatchExisting: '基于已有批次', importBatchCreate: '新建批次', dataProcess: '数据处理', dataConsolidate: '数据沉淀', collectedData: '数采审核', buildData: '构建数据', logicalData: '逻辑数据', annotateWorkbench: '标注工作台', trainDataset: '训练数据集', splitWorkbench: '切分工作台', dataPreview: '数据预览', logicalSplitRunning: '切分中', logicalSplitPending: '待切分', logicalAnnotateRunning: '标注中', logicalAnnotateTodo: '未开始', logicalReviewPassed: '通过', logicalReviewPending: '待审核', logicalReviewRejected: '驳回', splitStatusCol: '切分状态', annotateStatusCol: '标注状态', splitReviewCol: '切分审核', annotateReviewCol: '标注审核',
      buildDataAction: '数据集构建', buildExit: '退出构建', buildSelectedCount: '已选 {count} 个 Episode', buildSubmitted: '已提交数据集构建（{count} 个 Episode）',
      consolidateDataset: '沉淀数据集', consolidateTitle: '沉淀为数据集', consolidateName: '数据集名称', consolidateNamePlaceholder: '请输入数据集名称，例如：桌面抓取-v1', consolidateFilterTask: '任务名称', consolidateFilterDuration: '时长范围（秒）', consolidateFilterRange: '采集时间段', consolidateEpisodesTitle: '可选 Episode', consolidateSelectedCount: '已选 {count} 个 Episode', consolidateAction: '沉淀为数据集', consolidateEmpty: '暂无沉淀数据集。质检与脱敏完成的 Episode 可随时沉淀，切分不再作为前置条件。', consolidateList: '已沉淀数据集', episodeSplitCount: '切分方式', splitManage: '切分管理', splitReuseTitle: '该 Episode 已存在切分方式', splitReuseOption: '沿用 {name} · v{version}（{count} 段）', splitNewOption: '全新切分', splitConfirm: '开始切分', splitDoneToast: '切分已提交', annotateAction: '标注', trainAction: '训练', datasetEpisodeCount: 'Episode 数', datasetModesCount: '{count} 种', datasetEpisodesTitle: '数据集 Episode', reuseModeLabel: '沿用', newModeLabel: '全新',
      dashCapacity: '产能看板', dashCollection: '数采看板', dashEfficiency: '人效看板',
      dashPlanTarget: '计划目标', dashRawTotal: '原始采集', dashValidTotal: '有效数据', dashCompletion: '计划完成率',
      dashDate: '日期', dashDailyRaw: '采集条数', dashDailyValid: '有效条数', dashDailyTrend: '近 7 日产能趋势',
      dashByScene: '场景分布', dashByPurpose: '任务用途分布', dashByProject: '项目分布', dashBatchStages: '批次状态',
      dashCollectors: '采集员人效排行', dashDevices: '设备稳定性排行', dashCollected: '采集条数', dashAvgPerDay: '日均产出', dashAbnormal: '异常次数', dashRank: '排名',
      batchStageCollecting: '采集中', batchStageProcessing: '处理中', batchStageDone: '已完成',
      configDictionaries: '标签字典', dashCollectBoard: '产能看板', dashDataType: '数据类型', dashTypeStats: '项目与类型统计', dashTypeStatsTitle: '项目与类型统计', dashPersonnelStats: '人员采集时长', dashCountsStats: '数采统计指标', dashActiveStats: '活跃统计', dashUnitCount: '个', dashUnitPeople: '人', dashRegion: '地区', dashDateStart: '开始日期', dashDateEnd: '结束日期', dashDataBoard: '数采看板', dashPeopleBoard: '人效看板', dashProjectDist: '项目分布图', dashProjectTrend: '项目完成度趋势图', dashTaskDist: '任务分布图', dashTaskTrend: '任务完成度趋势图', dashSceneDist: '场景分布图', dashSceneTrend: '场景完成度趋势图', dashProjectsCard: '项目数', dashTypeCollected: '采集时长', dashTypeValid: '有效采集时长', dashCollectedTrend: '采集时长趋势', dashValidTrend: '有效采集时长趋势', dashWorkTotal: '采集人员工作总时长', dashCollectedCard: '采集时长', dashValidCard: '有效采集时长', dashPerMachine: '单机单日有效采集时长', dashDelta: '环比', dashWeek: '周', dashDay: '日', dashHour: '小时', dashMonth: '月', dashAllAll: '全部', dashValidOnly: '有效', dashModuleProject: '项目', dashModuleTask: '任务', dashModuleDuration: '时长', dashModuleSize: '大小', dashProjectsTotal: '项目总量', dashProjectsToday: '项目今日新增', dashProjectsTrend: '项目数量趋势', dashTasksTotal: '任务总量', dashTasksToday: '任务今日新增', dashTasksTrend: '任务数量趋势', dashEpisodesTotal: '数据包总量', dashEpisodesToday: '数据包今日新增', dashEpisodesTrend: '数据包数量趋势', dashCollectedTotal: '采集时长', dashCollectedToday: '今日新增采集时长', dashValidDuration: '有效时长', dashTodayValidDuration: '今日新增有效采集时长', dashValidSizeTotal: '有效采集数据总量', dashTodayValidSize: '今日有效采集总量', dashCollectedTrendH: '采集时长趋势', dashSizeTotal: '采集数据总量', dashSizeToday: '采集今日新增', dashSizeTrend: '采集数据大小趋势', dashDailyActive: '日活跃人数', dashActiveTrend: '活跃人数趋势', dashActiveDurationTrend: '活跃总时长趋势', dashAvgActiveTrend: '平均活跃时长趋势', dashActiveDist: '活跃时长分布', dashOutputRanking: '人员产出排行', dashWorkSummary: '工作时长汇总', dashValidCollected: '有效采集时长', dashValidRatio: '有效数据产出比', configAddTag: '添加', configDefaultSettings: '采集默认配置', configMinRate: '有效率下限', configBatchSize: '默认批次大小', configEdgeSync: 'Edge 回传策略', configEdgeAuto: '自动上传', configEdgeManual: '人工批量同步', configDesensEdge: 'Edge 侧脱敏', configSaved: '配置已保存', configTagExists: '标签已存在',
      tagProject: '项目标签', tagScene: '场景标签', tagPurpose: '任务用途', tagTrain: '训练标签', tagSoftware: '软件版本', tagDeviceVersion: '设备版本',
      allProjects: '全部项目', allScenes: '全部场景', allPurposes: '全部用途', allTrains: '全部训练标签', allBatches: '全部批次',
      miningPreviewHint: '当前为设计预览数据；采集任务与云端 Pipeline 接口接入后将自动替换。',
      miningTaskNameRequired: '请填写采集任务名称', miningTaskCreated: '采集任务已创建',
      createMiningTask: '新建采集任务', miningTaskName: '任务名称', miningTaskSop: '采集 SOP', miningTaskTarget: '目标有效数据', miningTaskMinRate: '有效率下限', miningTaskDue: '截止时间',
      miningTaskList: '采集任务', miningTaskModalities: '数据模态', miningTaskStatus: '任务状态', miningTaskActive: '进行中', miningTaskPaused: '已暂停',
      progressTarget: '目标', progressRaw: '原始采集', progressChecked: '完成质检', progressValid: '有效数据', progressRate: '有效率', progressGap: '缺口', progressDoneBatches: '达标数据包',
      kpiTaskTarget: '总任务数（个）', kpiPendingAssign: '待分配任务数（个）', kpiQcDesensDone: '总数据包数（个）', kpiAvgValidRate: '平均有效时长占比（%）', kpiCollectGoal: '目标总采集时长（小时）', kpiQcDoneBatches: '完成质检数据包（个）', kpiPendingSplit: '待切分数据包及Episode（个）',
      miningKpiPanelTitle: '数采任务统计', kpiTaskTargetLabel: '总任务数', kpiPendingAssignLabel: '待分配任务数', kpiQcDesensDoneLabel: '总数据包数', kpiCollectGoalLabel: '目标总采集时长', kpiAvgValidRateLabel: '平均有效时长占比',
      qcStatus: '人工质检', qcDone: '完成', qcUnfinished: '未完成', manualQc: '人工质检',
      enterSplitWorkbench: '进入切分工作台', approveSplitAction: '通过审核', autoSplitStatusLabel: '自动切分状态', cutApprove: '通过', cutReject: '驳回',
      miningFilterPlaceholder: '多个关键字用竖线"|"分隔，多个过滤标签用回车分隔', miningFilterMenuTitle: '选择数据包属性进行过滤',
      splitBatches: '目标拆解为数据包', batchCount: '数据包数', perBatch: '每批目标', splitBatchesHint: '按目标条数自动生成数据包子目标，已存在的数据包会保留。', splitBatchesDone: '已生成 {count} 个数据包',
      taskSplitStatusLabel: '拆解状态', taskSplitDone: '已拆解', taskSplitTodo: '未拆解', taskAssignStatusLabel: '分配状态', taskAssignDone: '已完成', taskAssignTodo: '未完成', taskSplitAction: '数据包拆解',
      miningBatchDetailTitle: '数据包详情',
      miningTaskFilterTargetHours: '目标采集时长（小时）', filterValidRate: '有效率', miningTaskHoursMin: '下限', miningTaskHoursMax: '上限', collectDateLabel: '采集日期', collectPeriodLabel: '采集周期', collectionTimeLabel: '采集时间',
      collectProjectName: '采集项目名称', allCollectionProjects: '全部采集项目', modalityLabel: '模态', validDurationLabel: '有效采集时长', assignTaskAction: '分配任务', batchTargetDurationLabel: '目标采集时长', reviewStatusLabel: '审核状态', batchReviewPending: '待审核', batchReviewApproved: '已通过', batchReviewRejected: '已驳回', batchReviewNotSubmitted: '未提交', uploadEndTime: '上传完成时间', batchEpisodeViewDetail: '查看详情', batchDetailApprovedToast: '数据包已通过审核', batchDetailRejectedToast: '数据包已作废，需要补采时请新建数据包', rateBucketLt30: '30%以下', rateBucket30to40: '30%-40%', rateBucket40to50: '40%-50%', rateBucket50to60: '50%-60%', rateBucket60to70: '60%-70%', rateBucket70to80: '70%-80%', rateBucket80to90: '80%-90%', rateBucket90to100: '90%-100%',
      dataPackageDrawerTitle: '数据包详情', offlineManifestTitle: '离线清单', offlineManifestHint: '离线清单用于现场采集设备与 SDK 确认任务与采集包身份',
      downloadManifestCsv: '下载离线清单 (CSV)', downloadManifestJson: '下载离线清单 (JSON)', downloadTaskManifest: '下载任务清单', manifestDownloadSuccess: '离线清单下载成功', manifestDownloadFailed: '下载离线清单失败', manifestUnavailableHint: '未分配或已作废的数据包不可生成离线清单',
      intakeReviewTitle: '入库审核', intakeReviewNotReady: '该数据包尚未进入可审核状态：数据还未上传或仍在解析中，请等待 duance 上传完成后再审核。', intakeReviewApprove: '审核通过', intakeReviewReject: '审核驳回', bulkApproveIntake: '批量审核通过', bulkApproveConfirm: '确定批量通过选中的 {count} 个数据包吗？', bulkApproveSuccess: '成功审核通过 {count} 个数据包', intakeApproveConfirm: '确认通过该数据包的入库审核？通过后该数据包将可用于批次构建。', intakeApproveSuccess: '数据包入库审核已通过',
      rejectReasonLabel: '驳回原因', rejectReasonPlaceholder: '请填写驳回原因（必填）', rejectReasonRequired: '驳回原因不能为空', intakeRejectSuccess: '数据包已驳回',
      packageStatusPendingAssignment: '待分配', packageStatusAssigned: '已分配待采集', packageStatusPendingUpload: '待回传', packageStatusUploading: '回传中', packageStatusParsing: '解析中', packageStatusPendingReview: '待入库审核', packageStatusIntakeApproved: '审核通过', packageStatusBatched: '已入批次', packageStatusParseFailed: '解析失败', packageStatusVoided: '已作废',
      episodesList: 'Episode 列表', admissionStatusLabel: '准入状态', validityStatusLabel: '审核状态', previewStatusLabel: '预览', viewVideo: '查看视频', noEpisodesInPackage: '当前数据包暂无 Episode 数据（尚未完成回传或解析）',
      packageResponsibleCollector: '数采员', packageOperatorCollector: '数采员', packageCapturedDuration: '实际采集时长', packageIntakeValidDuration: '入库有效时长', packageTargetDuration: '目标时长', viewPackageDetails: '查看详情', extraInfo: '额外信息', closePreview: '关闭', more: '更多',
      openIntakeReview: '数采审核', enterIntakeReviewAction: '进入审核', intakeReviewBatchMissing: '该数据包还没有可审核的批次',
      intakeReviewHistory: '审核历史', createSupplementPackage: '创建补采包', acceptedEpisodes: '已接受', rejectedEpisodes: '已标记不合格', excludedEpisodes: '已排除',
      admissionReady: '就绪', admissionFailed: '失败', admissionRunning: '处理中', runningNotBlocking: '处理中项不阻塞审核，通过时按当前事实处理',
      admissionReasonLabel: '失败原因', privacySensitive: '隐私敏感', reasonMissingFact: '未解析（文件缺失或未生成校验事实）',
      reasonIntegrity: '完整性校验失败', reasonPreview: '预览生成失败', reasonOutput: '输出校验失败',
      reasonFingerprint: '源指纹缺失/变化', reasonPolicy: '校验策略过期', reasonHumanRejected: '人工标记不合格',
      intakeApprovePreview: '通过本包？当前勾选 {rejected} 个 Episode 为不合格。', backToBatches: '返回审核队列',
      reviewPackagesTitle: '数据包审核', dataPackageStatusLabel: '数据包状态', emptyReviewPackages: '当前范围内没有数据包。', reviewPackagesHint: '入库审核以数据包为单位',
      pkgStatusPendingAssignment: '待分配', pkgStatusAssigned: '已分配', pkgStatusPendingUpload: '待上传', pkgStatusUploading: '上传中', pkgStatusParsing: '解析中', pkgStatusIngested: '已入库', pkgStatusPendingIntakeReview: '待入库审核', pkgStatusIntakeApproved: '审核通过', pkgStatusBatched: '已建批', pkgStatusGoverning: '治理中', pkgStatusPublished: '已发布', pkgStatusParseFailed: '解析失败', pkgStatusVoided: '已作废',
      batchSeq: '数据包', batchTarget: '子目标', batchCollected: '已采集', batchValid: '有效', batchAssignees: '采集归属', batchWindow: '采集时间窗口', batchTotalDuration: '已采集时长/有效时长',
      assignBatch: '分配数据包', assigneeCollectors: '采集人员', assigneeDevices: '采集设备', dispatchStatusCol: '下发状态', dispatched: '已下发', pendingDispatch: '待下发', taskDispatch: '任务下发', dispatchDoneToast: '任务已下发', unassigned: '待分配', assignSubmitted: '分配已保存', assignWindowPlaceholder: '例如 08-20 ~ 08-24', viewBatch: '查看',
      assignToTask: '按任务统一分配', assignToTaskHint: '所选采集员将按顺序轮流分配到该任务的待分配数据包，已分配的数据包不受影响。', assignOnlyUnassigned: '仅分配待分配数据包', assignAllSubmitted: '已按任务统一分配', assignPackageAction: '分配',
      assignModeTitle: '任务分配', assignModeEntry: '任务分配', assignModeTask: '按任务分配', assignModeBatch: '按数据包分配', assignModeLabel: '分配模式', assignModeEven: '按顺序轮流分配待分配数据包', packageDurationHours: '单包目标时长（小时）', packageCountPreview: '将生成 {count} 个数据包', targetDuration: '目标时长', hoursUnit: '小时',
      assignModeTaskHint: '统一选择人员与设备，一次应用到该任务的全部数据包；之后仍可在单个数据包中调整。',
      assignModeBatchHint: '不做统一分配，在数据包列表中逐数据包点击「分配数据包」单独指定人员。',
      assignModeSet: '分配方式已切换为按数据包分配', assignModeLabel: '分配方式',
      miningBatchPlanned: '待采集', miningBatchCollecting: '采集中', miningBatchUploaded: '已回传', miningBatchProcessing: '数据包处理', miningBatchDone: '已完成',
      failureReasons: '失败原因分布', emptyMiningTasks: '当前范围内还没有采集任务。', emptyMiningBatches: '该任务还没有数据包，请先拆解目标。',
      cloudTransfer: '回传与对账', cloudTransferSource: '来源', cloudTransferSize: '数据量', cloudTransferChecksum: '校验', cloudTransferVerified: '已校验', cloudTransferUnverified: '未校验', cloudTransferRetry: '重新同步',
      cloudTransferTotal: '回传任务', cloudTransferRunning: '同步中', cloudTransferSucceeded: '已完成', cloudTransferFailed: '失败',
      cloudPipeline: '云端 Pipeline', cloudPipelineName: '处理链', cloudPipelineTrigger: '触发方式', cloudPipelineStage: '处理阶段', cloudPipelineUpdated: '最近更新',
      cloudQuality: '质量指标', cloudQualityValid: '有效数据量', cloudQualityRate: '有效率', cloudQualityFailed: '失败数据',
      cloudSourceEdge: '现场 Edge Node', cloudSourceUpload: '本地上传', cloudSourceOss: 'OSS 扫描',
      pipelineQueued: '等待执行', pipelineRunning: '执行中', pipelineSucceeded: '完成', pipelineFailed: '失败',
      transferQueued: '等待上传', transferUploading: '上传中', transferRunning: '同步中', transferSucceeded: '已完成', transferFailed: '失败',
      miningCuts: '数据查看', cutEpisodeList: '切分数据', cutAllTasks: '全部任务', searchCutEpisode: '搜索 Episode / 采集员 / 数据包',
      cutTaskName: '采集任务', cutCollector: '采集员', cutDevice: '采集设备', cutSegments: '片段数', cutPreviewTitle: '在线可视化查看',
      cutSegmentList: '子切分内容', cutSegmentDesc: '片段内容', cutRange: '时间区间', cutValid: '有效', cutInvalid: '无效',
      cutEmpty: '当前范围内没有切分数据。', cutSelectHint: '选择左侧切分数据，即可在线查看子切分内容', cutPlaying: '播放中', cutBatch: '数据包',
      cutPreview: '切分预览', previewReviewHint: '请先预览切分结果，再决定审核通过、重新自动切分或恢复原始数据。',
      splitStatusDone: '完成', splitStatusTodo: '待切分', splitChoiceTitle: '切分方式', splitChoiceAuto: '自动切分', splitChoiceManual: '人工切分',
      splitChoiceAutoHint: '系统按预设规则自动完成切分，完成后进入自动质检。', splitChoiceManualHint: '转人工切分，前往工作队列的切分工作台手动完成。',
      splitAction: '切分', splitDoneToast: '切分完成', splitManualToast: '已转人工切分',
      splitUnfinished: '未完成', splitRunningLabel: '自动切分中', splitFailedLabel: '切分失败', splitReviewPending: '切分完成·待人工审核', manualReview: '人工审核', resplitAction: '重新切分',
      taskOwner: '新建人员', taskDetail: '任务详情', creatorCol: '创建人', totalDurationCol: '总时长',
      cutReview: '切分审核', backToMiningCloud: '返回数据生产', cutAuditTitle: '切分审核', cutAuditDone: '切分已审核通过，后续质检已解锁。',
      miningBatchProcessing: '批次处理', stageIntegrity: '完整性检查', stageSplit: '自动切分', stageQuality: '自动质检', stageDesensitize: '脱敏', stageSplitStatus: '切分状态',
      miningQuickCreate: '新建', miningTaskConfigHintBefore: '如果下拉菜单中无可选项，请前往', miningTaskConfigHintAfter: '。',
      stageQueued: '待执行', stageBlocked: '等待前置', stageRunning: '处理中', stagePendingReview: '待人工审核', stageSucceeded: '完成', stageFailed: '失败', stageManual: '人工切分中',
      splitModeAuto: '自动切分', splitModeManual: '人工切分', splitReviewHint: '自动切分完成，请人工审核切分结果', miningCloudTask: '采集任务',
      actRunIntegrity: '完整性检查', actRunSplit: '自动切分', actRunQuality: '自动质检', actRunDesensitize: '脱敏', actRetryStage: '重试',
      actApproveSplit: '审核通过', actRerunSplit: '重新自动切分', actRestoreManual: '恢复原始数据', actManualCut: '人工切分',
      miningStageStarted: '已开始执行', miningStageDone: '执行完成', miningSplitApproved: '切分结果已审核通过', miningSplitRework: '已重新发起自动切分', miningSplitRestored: '已恢复原始数据，请人工手动切分', miningStageBlocked: '请先完成前置步骤',
      settingsProjects: '采集项目管理', settingsLabels: '数采标签字典', settingsWorkspaces: '数采工作空间管理', emptyWorkspaces: '暂无数采工作空间', workspaceCreator: '创建者', settingsQuickCreate: '新建', createCollectionProject: '新建采集项目', editCollectionProject: '编辑项目', archiveCollectionProject: '归档', archiveConfirm: '确定要归档该采集项目吗？归档后将不能新建基于该项目的数采任务。', emptyProjects: '暂无采集项目', sceneLabels: '场景标签', purposeLabels: '任务用途', trainingLabels: '训练用途', modalityLabels: '数据模态', addLabel: '添加标签', labelName: '标签名称', labelDeactivated: '标签已停用', labelCreated: '标签已添加', emptyLabels: '暂无标签', settingsLoadFailed: '设置数据加载失败，请重试。失败项：', settingsLoadRetry: '重试加载', settingsUnavailable: '设置数据未加载完成，暂不能修改。', viewDataPackages: '查看数据包', taskDataPackages: '数据包清单', taskDetails: '任务详情',
      reviewSourceFilter: '标注来源', reviewSourceAll: '全部标注', reviewSourceAlgorithm: '仅算法标注', reviewSourceHuman: '仅人工标注', reviewAlgorithmSource: '算法',
      reviewLowConfidence: '仅看低置信度', reviewSource: '来源', reviewHumanSource: '人工',
    },
    'en-US': {
      metadata: 'Details', assignDateLabel: 'Assigned at', hoursUnit: 'hours', loadFailed: 'Loading failed. Please retry.',
      brand: 'QuicStudio', product: 'Data Console', signIn: 'Sign in', signOut: 'Sign out', email: 'Email', password: 'Password', localDemo: 'Open local demo', demoEntered: 'Opened the local demo',
      restoringSession: 'Opening the data console...',
      changePassword: 'Change password', changePasswordRequired: 'Change the initial password before entering the console.', currentPassword: 'Current password', newPassword: 'New password', confirmPassword: 'Confirm new password',
      passwordMismatch: 'The new passwords do not match', passwordTooShort: 'The new password must be at least 10 characters', passwordChanged: 'Password changed. Sign in again with the new password.', backToConsole: 'Back to console',
      apiTokens: 'API tokens', apiTokenCreate: 'Create token', apiTokenOnce: 'This token is shown only once and cannot be viewed again.', apiTokenCopy: 'Copy',
      apiTokenName: 'Name', apiTokenNameRequired: 'Token name is required', apiTokenExpires: 'Expires', apiTokenCreated: 'Created',
      apiTokenNever: 'Never expires', apiTokenLastUsed: 'Last used', apiTokenActive: 'Active', apiTokenRevoked: 'Revoked',
      apiTokenExpired: 'Expired', apiTokenRotate: 'Rotate', apiTokenRotateConfirm: 'Rotation issues a new secret; the old secret stays valid for one hour. Continue?',
      apiTokenRevokeConfirm: 'Revoke token "{name}"? It becomes invalid immediately.', apiTokenRevoked: 'Token revoked', apiTokenCopied: 'Copied', apiTokenCopyFailed: 'Copy failed, please copy manually',
      emptyApiTokens: 'No tokens yet. Create one so external tools (duance / algorithms) can call the platform.', daysUnit: 'days',
      workspace: 'Collection workspace', sourceWorkspace: 'Source collection workspace', allWorkspaces: 'All collection workspaces', workspaceName: 'Collection workspace name', taskSet: 'Collection project', projectLabel: 'Project', createWorkspace: 'New collection workspace', createTaskSet: 'New collection project', description: 'Description',
      overview: 'Overview', dataOverview: 'Data overview', refreshDashboard: 'Refresh data', dashboardEmpty: 'Dashboard snapshot is not ready yet. Click refresh to compute stats.', dashboardMockHint: 'Showing design-preview mock data until a real ADS snapshot is available.', dashboardLoadFailed: 'Dashboard data failed to load. Please retry.', scopeChanged: 'The workspace changed. Reopen this action before submitting.',
      kpiTotalEpisodes: 'Total episodes', kpiTotalDuration: 'Episode duration (including derived)', kpiCollectedToday: "Today's intake (UTC)", kpiAnnotationCompleted: 'Annotations completed', kpiAnnotationRate: 'Annotation completion rate',
      vsYesterday: 'vs yesterday', pipelineFunnel: 'Data pipeline funnel', avgDuration: 'Avg duration', todayTasks: "Today's tasks", collectTrend7d: 'Intake over 7 days (UTC)', deviceDistribution: 'Device distribution',
      queueName: 'Name', queueCount: 'Count', queueShare: 'Share', queuePending: 'Pending', devicesUnit: 'devices', durationUnknown: 'Unknown',
      'intake-review': 'Collection review', intake: 'Data overview', batches: 'Collection review', episodes: 'Episodes', workQueue: 'Work queue', workbench: 'Workbench', resources: 'Collection resources', assets: 'Data assets', datasets: 'Datasets', trainDash: 'Training overview', trainJobs: 'Training jobs', trainNew: 'New training', trainDatasets: 'Training datasets', trainModels: 'Models and recipes', trainResources: 'Training resources', trainSystem: 'System health', trainNav: 'Training', managementCenter: 'Management', admin: 'Collection workspaces', settings: 'Projects',
      currentTaskSet: 'Current collection project', noTaskSet: 'Create a collection project to create batches and import data.', externalProject: 'External project', externalProjectUnbound: 'Unbound', externalProjectBound: 'Bound',
      workflowOverview: 'Workflow overview', pendingWork: 'Pending work', recentEpisodes: 'Recent Episodes', goProcess: 'Open',
      stageImport: 'Intake', stageQuality: 'Quality', stageHuman: 'Human work', stageReview: 'Review', stagePublish: 'Publish',
      createBatch: 'New collection batch', batchName: 'Name', batchType: 'Data type', source: 'Source', pipeline: 'Pipeline',
      batchStatus: 'Batch status', importData: 'Import data', refresh: 'Refresh', allStatus: 'All statuses',
      importHistory: 'Import history', validCollectedDuration: 'Valid collected duration', sourceEpisodes: 'Source Episodes', batchSummary: 'Batch summary', activity: 'Activity', noActivity: 'No batch activity is available yet.', episodesDetail: 'Data details', importUid: 'Data name', importState: 'Import state', importStateImported: 'Imported', importStateImporting: 'Importing', importStateTodo: 'Pending import',
      intakeStepBatch: '1. Select batch', intakeStepSource: '2. Choose source', intakeStepTrack: '3. Track processing', intakeSelectBatch: 'Select the batch receiving this data', intakeBatchHint: 'The batch owns source attribution, collection task, and quality progress. Imported files are attached to the selected batch.', intakeUploadHint: 'Upload QRDF ZIP or MCAP with resumable chunk transfer.', intakeOssHint: 'Scan authorized OSS prefixes and select candidate data.', intakeChooseBatchFirst: 'Select a batch first.',
      activityBatchLog: 'Batch status', activityImportSession: 'Import record', activityJobRun: 'System job', progress: 'Progress',
      jobImportParse: 'Parse import', jobQualityCheck: 'Quality check', jobPreview: 'Generate preview', jobPublication: 'Publication', jobProcessing: 'System processing',
      jobQueued: 'Queued', jobRunning: 'Running', jobSucceeded: 'Completed', jobFailed: 'Failed', jobCancelled: 'Cancelled',
      importFile: 'Upload file', importOss: 'Select existing OSS data', chooseFile: 'Choose file', startImport: 'Start import',
      intakeGuideTitle: 'Data packages are uploaded by the duance desktop tool; the console reviews and builds on them.',
      intakeGuideStepProject: '1. Confirm packages and target duration under Collection project -> Collection task.',
      intakeGuideStepManifest: '2. Download the offline manifest before collection, or carry it to the site.',
      intakeGuideStepUpload: '3. Bind local files to packages on site (one package may hold several Episodes); duance uploads with resume once online.',
      intakeGuideStepReview: '4. After parsing, review the data packages in Collection review.',
      intakeGuideOfflineHint: 'Download the manifest before going offline; the task detail exports the platform manifest for revision checks.',
      collectionTask: 'Collection task', allCollectionTasks: 'All collection tasks', noTaskLabel: 'Not specified', selectCollectionTask: 'Select a collection task', emptyTaskLabels: 'No collection-task vocabulary is configured. An admin must create one before importing.',
      createTaskLabel: 'New collection task', taskLabelName: 'Name', taskLabelDescription: 'Description', taskLabelCreated: 'Collection task created',
      fileHint: 'Supports a QRDF ZIP or one MCAP. The browser only transfers chunks; parsing and quality checks run asynchronously.',
      ossHint: 'The server scans only authorized candidates. No OSS path or credential is displayed or accepted here.', scanCandidates: 'Scan candidates', scanQueued: 'Scan queued', scanningCandidates: 'Scanning', scanRetryPending: 'Retrying scan', scanSucceeded: 'Scan complete', scanFailed: 'Scan failed', noCandidates: 'No importable Episodes matched the scan.',
      importCandidate: 'Import candidate', importSelectedCandidates: 'Import selected', candidates: 'Available candidates', candidateStatus: 'Import state', candidateSourceGroup: 'Source group', candidateSourceGroupStatus: 'Source group status', sourceGroupValid: 'Valid group', sourceGroupMissing: 'No source group', sourceGroupInvalid: 'Invalid group', sourceGroupLegacy: 'Legacy group', selectSourceGroup: 'Select group', selectedSourceGroups: 'source groups', selectedIndividualCandidates: 'individual candidates', candidateSourceGroupsTruncated: 'Only the first {count} source groups are shown. Filter or page through candidates to find the rest.', candidateDateRangeLimit: 'The scan date range cannot exceed 31 days', captureEndedAt: 'Capture ended', legacySourceGroup: 'Historical source group', timeUnknown: 'Time unknown', sourceDiscovered: 'Not imported', sourceImporting: 'Importing', sourceImported: 'Imported', sourceFailed: 'Import failed', viewCandidates: 'View candidates', cancel: 'Cancel', retry: 'Retry',
      currentImport: 'Current import', processingImport: 'The system is processing import content', transferringFile: 'The browser is transferring the file',
      uploadAccepted: 'File transfer is complete. The server is assembling and publishing the raw object.', selectCollector: 'Select the default collector', defaultCollector: 'Default collector (only when not reported)', selectDevice: 'Select the default primary device',
      uploadLimit: 'Per-file limit', uploadResuming: 'An unfinished upload with the same name was found. Resuming from server-confirmed chunks.', uploadSessionConflict: 'An unfinished file with the same name uses different collection attribution. Cancel that record first.',
      episode: 'Episode', kind: 'Kind', modality: 'Modality', scene: 'Scene', quality: 'Quality', workflow: 'Workflow', review: 'Review',
      metrics: 'Quality metrics', referenceFrames: 'Reference frames', duration: 'Duration', frameRate: 'RGB rate', parentEpisode: 'Parent Episode',
      identityAndLineage: 'Identity and lineage', businessContext: 'Business context', lifecycle: 'Lifecycle', currentWorkflow: 'Current work item', currentStage: 'Current stage', technicalInfo: 'Technical information', databaseId: 'Database ID', workspaceId: 'Collection workspace ID', taskSetId: 'Collection project ID', importSessionId: 'Import session ID', embodimentId: 'Embodiment ID', taskLabelId: 'Collection task ID', derivationVersion: 'Derivation version', sourceRange: 'Source range', episodeDetailLoadFailed: 'Episode detail failed to load', previewLoadFailed: 'Preview information failed to load', retryLoad: 'Reload',
      artifacts: 'Artifacts', preview: 'Media preview', previewUnavailable: 'Preview is not generated or is currently unavailable', previewPreparing: 'Preview is being generated', previewReady: 'Preview ready', previewFailed: 'Preview generation failed', storageRole: 'Storage role', size: 'Size', publication: 'Publication', rawSourceDownloads: 'Raw source downloads', loadRawSourceDownloads: 'Load raw files', downloadRawFile: 'Download file', rawSourceUnavailable: 'No raw source file is available to download.',
      packetMetadata: 'Packet metadata', packetSourceEpisode: 'Source episode ID', packetDataFile: 'Data file', packetQrdfVersion: 'QRDF version',
      packetTaskName: 'Collection task', packetTaskLanguage: 'Task language', packetRobotName: 'Robot', packetRobotArms: 'Arms', packetRobotBaseFrame: 'Base frame',
      packetCaptureMode: 'Capture mode', packetEpisodeType: 'Episode type', packetCaptureApp: 'Capture app', packetReferenceTopic: 'Reference camera topic',
      packetStartTime: 'Start time', packetEndTime: 'End time', packetCameras: 'Camera streams', packetSensors: 'Low-dim sensors', packetTopic: 'Topic',
      packetResolution: 'Resolution', packetFps: 'FPS', packetFrequency: 'Frequency', emptyPacketMetadata: 'This Episode has no displayable packet metadata yet.',
      queueCut: 'Cut', queueAnnotation: 'Annotation', queueReview: 'Review', queueCompleted: 'Completed', workType: 'Work type',
      queueModeWorkItems: 'Work items', queueModeGovernance: 'Batch governance',
      reviewType: 'Review type', allReviewTypes: 'All reviews', reviewCut: 'Cut review', reviewAnnotation: 'Annotation review',
      actionClaim: 'Claim', actionContinue: 'Continue', actionRelease: 'Release', actionSubmit: 'Submit', actions: 'Actions',
      actionAccept: 'Accept', actionReject: 'Reject', actionReassign: 'Reassign', returnReason: 'Return reason', noAction: 'No action', workItemUpdated: 'This work item changed. Refresh the queue and try again.', assignee: 'Assignee',
      workPending: 'Unclaimed', workAssigned: 'Assigned', workInProgress: 'In progress', workSubmitted: 'Submitted', workNeedsRework: 'Needs rework', workAccepted: 'Completed',
      publicationQueued: 'Publication queued', publicationRunning: 'Publishing', publicationSucceeded: 'Published', publicationCancelled: 'Publication cancelled',
      openWorkbench: 'Open workbench', backToQueue: 'Back to work queue', cutWorkbench: 'Cut workbench', annotationWorkbench: 'Annotation workbench', reviewWorkbench: 'Review workbench',
      timeline: 'Timeline', cutSegments: 'Cut point list', addSegment: 'Add segment', addBoundary: 'Add boundary at playhead', deleteBoundary: 'Delete selected boundary', restoreQrBoundary: 'Restore QR timestamp', useWholeEpisode: 'Do not cut', setStart: 'Set start', setEnd: 'Set end',
      segmentStart: 'Start time', segmentEnd: 'End time', remove: 'Remove', saveDraft: 'Save draft', draftSaved: 'Draft saved', submitForReview: 'Submit for review',
      cutNote: 'Cut note', workbenchUnavailable: 'This work item cannot open a workbench.', noCutSegments: 'Keep at least one segment or choose not to cut.', timelineMappingUnavailable: 'The exact timeline mapping is unavailable, so boundaries cannot be edited yet.', dragBoundary: 'Drag boundary left or right', dragSegment: 'Drag segment left or right', partitionInvalid: 'Cut windows must continuously cover the complete Episode.', submitCutTooLong: 'The draft can be saved. Shorten red included segments or mark them excluded before submission.', annotationIncomplete: 'Complete every annotation segment before submitting for review.', annotationNoRoom: 'There is not enough free timeline space for a new segment.', partitionedMode: 'Contiguous pre-cut', wholeMode: 'Whole Episode', currentPlayhead: 'Current playhead', windowDuration: 'Window duration', localAdjustment: 'Precision adjustment (plus/minus 15 seconds)', boundaryQrEvent: 'QR suggestion', boundaryAdjusted: 'Manually adjusted', boundaryHuman: 'Manual boundary', segmentIncluded: 'Included', segmentExcluded: 'Excluded', exclusionReason: 'Exclusion reason (optional)', reasonOffTask: 'Off task', reasonIdleOrSetup: 'Idle or setup', reasonPrivacySensitive: 'Privacy sensitive', reasonOther: 'Other',
      annotationSegments: 'Annotation segments', annotationDescription: 'Action description', outcome: 'Outcome', outcomeSuccess: 'Success', outcomeFailure: 'Failure', outcomeUnknown: 'Unknown',
      play: 'Play', pause: 'Pause', stepBackward: 'Back 1 second', stepForward: 'Forward 1 second', stepBackwardLarge: 'Back 5 seconds', stepForwardLarge: 'Forward 5 seconds', snapOn: 'Snap on', snapOff: 'Snap off', mute: 'Mute', unmute: 'Unmute', playbackRate: 'Playback speed', fullscreen: 'Fullscreen', undo: 'Undo', redo: 'Redo', copy: 'Copy', paste: 'Paste', zoomOut: 'Zoom timeline out', zoomIn: 'Zoom timeline in', selectBoundary: 'Select boundary', resizeTimeline: 'Resize timeline', shortcutHelp: 'Keyboard shortcuts', shortcutClose: 'Close shortcuts', shortcutPlayPause: 'Play or pause', shortcutShuttleBackward: 'Shuttle backward', shortcutShuttlePause: 'Pause shuttle', shortcutShuttleForward: 'Shuttle forward', shortcutPreviousBoundary: 'Previous boundary', shortcutNextBoundary: 'Next boundary', shortcutSnap: 'Toggle snap', shortcutSelectPrevious: 'Select previous segment', shortcutSelectNext: 'Select next segment', shortcuts: 'Space play/pause · N new segment · I in · O out',
      segmentContent: 'Clip content', rating: 'Rating', annotationNote: 'Note', boundaryType: 'Boundary type', boundaryTime: 'Timestamp', previewFrame: 'Frame preview', accountMenu: 'Account menu',
      collector: 'Collector', collectorName: 'Name', collectorNumber: 'Number', collectorNumberHint: 'Positive integer; leave blank to assign automatically', unknownCollector: 'Unknown', collectionDevice: 'Primary collection device', unknownDevice: 'Unknown', deviceName: 'Device name', deviceType: 'Device type', deviceModel: 'Model', serialNumber: 'SN', active: 'Active', inactive: 'Inactive', activate: 'Activate', deactivate: 'Deactivate', createCollector: 'New collector', addCollector: 'Add collector', addExistingCollector: 'Add existing collector', collectorAddMode: 'Method', collectorModeCreate: 'Create new', collectorModeExisting: 'Select existing', selectExistingCollector: 'Select existing collector', emptyAvailableCollectors: 'No existing collectors available to add', emptyAvailableCollectorsHint: 'Only collectors existing in the platform but not yet added to this workspace are shown.', revokeCollectorMember: 'Remove from workspace', collectorMemberRevoked: 'Removed from current workspace', confirmRevokeCollector: 'Remove this collector from the current workspace? (Historical collection data will not be affected)', createDevice: 'New device', collectorCreated: 'Collector created', deviceCreated: 'Device created', emptyCollectors: 'No collectors exist in this workspace.', emptyDevices: 'No collection devices exist in this workspace.', collectorDurationSummary: 'Collector duration summary', deviceDurationSummary: 'Device duration summary', attribution: 'Collection attribution', attributionUnknown: 'Unconfirmed', attributionOfflineDeclared: 'Import declaration', attributionOfflineCurated: 'Human curated', attributionMachineReported: 'Device reported', attributionOnlineVerified: 'Online verified', reportedCollector: 'Reported collector', collectorAutomatic: 'Automatically matched', collectorDefault: 'Default applied', collectorUnreported: 'Not reported', collectorMappingError: 'Mapping error', collectorFormatError: 'Identifier format error', collectorImportBlocked: 'Correct the reported collector mapping before importing this candidate.', qrCodes: 'QR codes', qrControlCode: 'Start / stop', qrSegmentCode: 'Segment', qrControlDescription: 'Starts from standby and stops while recording.', qrSegmentDescription: 'Marks a segment boundary while recording.', downloadQr: 'Download QR code', print: 'Print', collectorIdMissing: 'This collector needs a numeric identifier before an EGO control QR code can be generated.', aiSuggestions: 'AI suggestions', generateAiSuggestions: 'Generate AI suggestions', applyAiSuggestion: 'Apply suggestion',
      aiUnavailable: 'AI is currently unavailable', aiQueued: 'AI suggestion queued', aiFailed: 'AI suggestion failed', suggestionStatus: 'Suggestion status',
      reviewTarget: 'Review target', reviewNote: 'Review note', reviewAccept: 'Accept review', reviewReject: 'Return for revision', useSubmittedCollector: 'Use detected collector', useSubmittedDevice: 'Use detected device', overrideCollector: 'Choose collector',
      details: 'Details', viewAssets: 'View artifacts', selectEpisodeAssets: 'Select an Episode to inspect its artifacts', retentionPolicy: 'Retention policy', retentionUntil: 'Retain until', emptyAssets: 'This Episode has no displayable artifacts yet.',
      emptyQueue: 'No work items are available in this stage.', emptyEpisodes: 'No Episodes are available in this scope.', emptyBatches: 'No collection batches exist in this collection project.',
      qualityPending: 'Quality checking', qualityPassed: 'Quality passed', qualityRecovered: 'Recovered with data loss', qualityFailed: 'Quality failed',
      allAssetKinds: 'All layers', assetSource: 'Source', assetDerived: 'Derived', allReviewStatuses: 'All review states', reviewPending: 'Pending review', reviewAccepted: 'Accepted', reviewRejected: 'Rejected', reviewNotApplicable: 'Not applicable', allPublicationStatuses: 'All publication states', publicationUnpublished: 'Unpublished', publicationPublishing: 'Publishing', publicationPublished: 'Published', publicationFailed: 'Publication failed', humanStage: 'Human stage', humanPending: 'Not started', humanAssigned: 'Assigned', humanInProgress: 'In progress', humanSubmitted: 'Submitted', humanAccepted: 'Completed', humanRejected: 'Returned', noDerivedAssets: 'No derived assets', derivedAssets: 'child assets', lineageIssue: 'Lineage issue', lineageMissingParent: 'Parent Episode is outside this scope', lineageCycle: 'Parent relationship contains a cycle', lineageDepthExceeded: 'Parent chain exceeds 32 levels', lineageInvalid: 'Episode identifier is invalid', assetRange: 'Source range',
      qualityFailureReason: 'Failure reason', qualityFailureGeneric: 'Quality validation failed. Check the source structure, timeline, and reference camera data.', qualityWorkerInterrupted: 'The quality worker was interrupted. An operator can retry it.', qualityMetadataInvalid: 'Source metadata is invalid or missing.', qualityRawSourceUnavailable: 'Raw source data is unavailable or unreadable.', qualityReferenceTopicMissing: 'Reference camera data is missing.', qualityReferenceTimelineIncomplete: 'The reference camera timeline does not cover the full Episode.',
      importInit: 'Waiting for a file', importUploading: 'Uploading', importUploaded: 'Parse queued', importParsing: 'Parsing',
      importSucceeded: 'Import complete', importPartial: 'Partially complete', importFailed: 'Import failed', importCancelled: 'Cancelled', importSuperseded: 'Superseded',
      sourceDataCopyFailed: 'Source data copy failed', sourceMetadataCopyFailed: 'Source metadata copy failed', sourceCompleteCopyFailed: 'Completion marker copy failed', sourceValidationFailed: 'Source validation failed', sourcePublishFailed: 'Raw publication failed', sourceQualityJobFailed: 'Quality job creation failed', sourceImportFailed: 'Source import failed',
      batchCreated: 'Created', batchImporting: 'Importing', batchProcessing: 'Processing', batchReady: 'Ready', batchFailed: 'Failed', batchCancelled: 'Cancelled',
      sourceUpload: 'Local upload', sourceOss: 'OSS candidate', sourceFilesystem: 'Filesystem candidate', sourceMixed: 'Mixed sources', sourceUnknown: 'No intake yet',
      typeEgo: 'EGO', typeUmi: 'UMI', typeTeleop: 'Teleoperation', typeLerobot: 'LeRobot', typeQrdf: 'QRDF import', typeManual: 'Manual upload',
      language: '中文 / EN', backToBatchesList: 'Back to batches', viewAllEpisodes: 'View all collection project Episodes',
      importComplete: 'Import completed and quality checking started', importQueued: 'File uploaded and parsing started', batchCreatedToast: 'Collection batch created',
      workspaceCreated: 'Collection workspace created', taskSetCreated: 'Collection project created', actionSucceeded: 'Work item updated', workspaceNameExists: 'Collection workspace names must be unique', taskSetNameExists: 'Collection project names must be unique in a workspace', collectorNameExists: 'Collector names must be unique in a workspace', deviceSerialExists: 'Device SN already exists in this workspace',
      uploadTooLarge: 'The file exceeds the current chunked-upload limit', uploadEmpty: 'An empty file cannot be uploaded', chooseImportFile: 'Choose a QRDF ZIP or MCAP file',
      annotatorRoleHint: 'Only users with the annotator role can be assigned; create or update one in Settings first.',
      releasePrompt: 'Optionally add a handoff note. Cancel to keep the item.', forceReleasePrompt: 'Enter a reason for force release. Cancel to keep the item.', forceReleaseReasonRequired: 'A reason is required to release another assignee\'s work item.', status: 'Status', createdAt: 'Created', updatedAt: 'Updated', operationTime: 'Operation time', operation: 'Operation', viewDuration: 'View duration', batchGroupName: 'Batch name', batchKind: 'Data batch', importRecordKind: 'Import record', expand: 'Expand', governanceFlow: 'Data governance', annotationFlow: 'Annotation flow', complianceCheck: 'Compliance check', buildBatchTitle: 'Build data batch', buildBatchName: 'Batch name', buildBatchNameRequired: 'Please enter a batch name', buildBatchGovernance: 'Governance flow', buildBatchAnnotation: 'Annotation flow', buildBatchSelectedCount: '{count} selected', buildBatchConfirm: 'Confirm', buildBatchCancel: 'Cancel', buildBatchSuccess: 'Batch built successfully', selectDataFirst: 'Please select data first', enabled: 'Enabled', publishedAt: 'Published', clearFilters: 'Clear filters', searchEpisode: 'Search Episode', searchDataset: 'Search datasets', allTaskSets: 'All collection projects', operationDate: 'Operation time',
      importType: 'Intake method', itemCount: 'items', selectedBatch: 'Selected batch', taskSetScope: 'Collection project scope', batchSequence: 'Batch no.', sourceCount: 'Sources', totalDuration: 'Collection duration', availableDuration: 'Available duration (union)', qualityProgress: 'Quality progress',
      dataType: 'Data type', noWorkspace: 'This account has no accessible collection workspace.',
      accessRedirected: 'This account cannot access that page. You were returned to an available page.', noConsoleAccess: 'This account has no accessible console modules.',
      createDataset: 'New dataset', datasetName: 'Dataset name', datasetVersions: 'Versions', createDatasetRevision: 'Create revision', datasetsBrowse: 'Dataset view', datasetsBuild: 'Dataset build', datasetCatalog: 'Dataset catalog', nativeLerobot: 'Native LeRobot', lerobotCatalogImport: 'Import LeRobot', catalogVersionAssets: 'Asset list', nativeLerobotVerified: 'The completion marker is verified; delivery starts after the platform copy succeeds', lerobotDatasetRoot: 'LeRobot dataset root', scanLerobotCandidates: 'Scan LeRobot candidates', lerobotCandidateEmpty: 'No completed and registerable LeRobot dataset was found.', registerLerobot: 'Register LeRobot dataset', lerobotRegistered: 'LeRobot dataset registered and queued for platform copy.', lerobotImportSessions: 'Import sessions', lerobotStartSession: 'Start selection', lerobotSubmitSelection: 'Submit selection', lerobotSelectionCount: '{count} selected', lerobotNoSession: 'Scan candidates successfully before starting a selection.', content: 'Content', association: 'Association', datasetId: 'Dataset ID', fileCount: 'Files', completedAt: 'Completed', ossRootUri: 'OSS root URI', copyOssPath: 'Copy OSS path', copyOssUri: 'Copy OSS URI', archive: 'Archive', archived: 'Archived', generate: 'Generate', datasetDelivery: 'Generation & delivery', deliveryUnavailable: 'This environment has no copyable OSS path; use the authorized download instead.', copied: 'Copied', resetColumnWidths: 'Reset column widths', nativePlatformCopy: 'Platform copy', nativeCopyQueued: 'Copy queued', nativeCopyRunning: 'Copying', nativeCopySucceeded: 'Platform copy ready', nativeCopyFailed: 'Copy failed', nativeCopyBackfillPending: 'Awaiting historical backfill', nativeCopyVerified: '{files} files verified · {size}', nativeCopyUnavailable: 'The platform copy is not ready for delivery.', nativeCopyErrorCopyAccessDenied: 'The platform cannot read the source or write the target.', nativeCopyErrorCopyStateInvalid: 'The copy record is not in a valid state.', nativeCopyErrorCopyUnavailable: 'The platform copy is temporarily unavailable.', nativeCopyErrorSourceChanged: 'The source changed while it was being copied.', nativeCopyErrorSourceIdentityUnavailable: 'The source identity cannot be verified safely.', nativeCopyErrorSourceScopeChanged: 'Source authorization changed and must be renewed.', nativeCopyErrorTargetConflict: 'The platform target contains conflicting data.', nativeCopyErrorGeneric: 'The platform copy is not complete.', retryPlatformCopy: 'Retry copy', reauthorizeSource: 'Reauthorize source', reauthorizeSourceHint: 'Select only the matching authorized candidate. The completion marker will be verified again.', reauthorizeSourceEmpty: 'No matching authorized candidate was found.', selectSourceSnapshot: 'Select source snapshot', packageZip: 'Package ZIP', packagingZip: 'Packaging ZIP', downloadZip: 'Download ZIP', zipReady: 'ZIP ready', zipFailed: 'ZIP creation failed. Start it again.', nativeCopyUpdatedAt: 'Copy updated',
      publishedEpisodes: 'Published Episodes', selectPublishedEpisodes: 'Select published Episodes', emptyDatasets: 'No datasets exist in this workspace.',
      emptyDatasetRevisions: 'This dataset has no revisions.', latestExport: 'Generated deliverables', export: 'Generate', download: 'Download', exportLerobot: 'LeRobot', exportQrdf: 'QRDF',
      availableCandidates: 'Available Episodes', selectedCandidates: 'Selected Episodes', sourceGroups: 'source groups', searchCandidates: 'Search Episode, task, or collector', collectionDate: 'Collection date', allCandidates: 'All', onlySelected: 'Selected only', allCollectors: 'All collectors', allDevices: 'All devices',
      sourceEpisode: 'Source Episode', fullSourceEpisode: 'Full source Episode', derivedEpisode: 'Derived Episode', selectionStatus: 'Selection', candidatePreview: 'Candidate preview', chooseCandidatePreview: 'Select a candidate row to preview it', previewRetry: 'Reload preview', sourceTimeline: 'Source timeline', timelineUnavailable: 'No reliable source time mapping is available', currentRange: 'Source range', selectedSummary: 'Selection summary', clearSelection: 'Clear selection', confirmClearSelection: 'Clear the entire current selection?', confirmCloseRevisionBuilder: 'The current selection has not been used to create a revision. Close it?', invalidSelectionTitle: 'Some candidates changed', revisionCandidatesChanged: 'Candidates were refreshed. Review invalid items and retry.', candidateLoadFailed: 'Failed to load candidates', createRevisionCount: 'Create revision', openDetails: 'Open details', noRevisionCandidates: 'This collection project has no published Episodes available for a revision.', noFilteredCandidates: 'No candidates match the current filters.',
      exportQueued: 'Export queued', exportRunning: 'Exporting', exportSucceeded: 'Export completed', exportFailed: 'Export failed', exportCancelled: 'Export cancelled',
      datasetCreated: 'Dataset created', datasetNameExists: 'A dataset with this name already exists in the current workspace.', datasetRevisionCreated: 'Dataset revision created', exportQueuedToast: 'Export queued', exportRetried: 'Export queued again',
      userManagement: 'User management', workspaceMembers: 'Collection workspace members', createManagedUser: 'New user', userRole: 'Role', resetPassword: 'Reset password', resetPasswordConfirm: 'Reset the password for "{email}"? The user must sign in with the new temporary password and change it immediately.', resetPasswordSuccess: 'Password reset', resetPasswordOnce: 'Temporary password (shown only once):\n{password}\n\nShare it with the user; they must change it on first sign-in.',
      grantWorkspaceMember: 'Add member', revokeWorkspaceMember: 'Remove member', userCreated: 'User created', memberGranted: 'Member added', memberRevoked: 'Member removed',
      emptyManagedUsers: 'No manageable users.', emptyWorkspaceMembers: 'This collection workspace has no members.', selectUser: 'Select a user',
      mining: 'Collection management', miningTasks: 'Collection tasks', miningCloud: 'Data production',
      miningPreviewHint: 'Design-preview data. It is replaced automatically once the collection task and cloud pipeline APIs are available.',
      miningTaskNameRequired: 'Enter a collection task name', miningTaskCreated: 'Collection task created',
      createMiningTask: 'New collection task', miningTaskName: 'Task name', miningTaskSop: 'Collection SOP', miningTaskTarget: 'Valid episodes target', miningTaskMinRate: 'Minimum valid rate', miningTaskDue: 'Due date',
      miningTaskList: 'Collection tasks', miningTaskModalities: 'Modality', miningTaskStatus: 'Status', miningTaskActive: 'Active', miningTaskPaused: 'Paused',
      progressTarget: 'Target', progressRaw: 'Raw collected', progressChecked: 'Quality checked', progressValid: 'Valid', progressRate: 'Valid rate', progressGap: 'Gap', progressDoneBatches: 'Batches on target',
      kpiTaskTarget: 'Task target (count)', kpiPendingAssign: 'Pending assignment (count)', kpiQcDesensDone: 'QC & desensitized batches (count)', kpiAvgValidRate: 'Avg valid duration ratio (%)', kpiCollectGoal: 'Target batches (count)', kpiQcDoneBatches: 'QC-passed batches (count)', kpiPendingSplit: 'Pending split batches/episodes',
      miningKpiPanelTitle: 'Collection task stats', kpiTaskTargetLabel: 'Total tasks', kpiPendingAssignLabel: 'Pending assignment', kpiQcDesensDoneLabel: 'Total batches', kpiCollectGoalLabel: 'Target collection duration', kpiAvgValidRateLabel: 'Avg valid duration ratio',
      qcStatus: 'Manual QC', qcDone: 'Done', qcUnfinished: 'Unfinished', manualQc: 'Manual QC',
      enterSplitWorkbench: 'Enter split workbench', approveSplitAction: 'Approve', autoSplitStatusLabel: 'Auto split status', splitWorkbench: 'Split workbench', cutApprove: 'Approve', cutReject: 'Reject',
      miningFilterPlaceholder: 'Separate keywords with "|", press Enter to add filter tags', miningFilterMenuTitle: 'Filter by batch attribute',
      splitBatches: 'Split target into batches', batchCount: 'Batch count', perBatch: 'Target per batch', splitBatchesHint: 'Batch sub-targets are generated from the task target; existing batches are kept.', splitBatchesDone: '{count} batches created',
      taskSplitStatusLabel: 'Split status', taskSplitDone: 'Split', taskSplitTodo: 'Not split', taskAssignStatusLabel: 'Assign status', taskAssignDone: 'Completed', taskAssignTodo: 'Incomplete', taskSplitAction: 'Split batches',
      miningBatchDetailTitle: 'Batch details',
      miningTaskFilterTargetHours: 'Target duration (hours)', filterValidRate: 'Valid rate', miningTaskHoursMin: 'Min', miningTaskHoursMax: 'Max', collectDateLabel: 'Collection date', collectPeriodLabel: 'Collection period', collectionTimeLabel: 'Collection time',
      collectProjectName: 'Collection project', allCollectionProjects: 'All collection projects', modalityLabel: 'Modality', validDurationLabel: 'Valid collection duration', assignTaskAction: 'Assign task', batchTargetDurationLabel: 'Target duration', reviewStatusLabel: 'Review status', batchReviewPending: 'Pending review', batchReviewApproved: 'Approved', batchReviewRejected: 'Rejected', batchReviewNotSubmitted: 'Not submitted', uploadEndTime: 'Upload completed time', batchEpisodeViewDetail: 'View details', batchDetailApprovedToast: 'Batch approved', batchDetailRejectedToast: 'Package voided; create a new package if recollection is needed', rateBucketLt30: '<30%', rateBucket30to40: '30%-40%', rateBucket40to50: '40%-50%', rateBucket50to60: '50%-60%', rateBucket60to70: '60%-70%', rateBucket70to80: '70%-80%', rateBucket80to90: '80%-90%', rateBucket90to100: '90%-100%',
      dataPackageDrawerTitle: 'Data Package Details', offlineManifestTitle: 'Offline Manifest', offlineManifestHint: 'The offline manifest is used by offline collection devices and SDK to verify task and package identities.',
      downloadManifestCsv: 'Download Manifest (CSV)', downloadManifestJson: 'Download Manifest (JSON)', downloadTaskManifest: 'Download Task Manifest', manifestDownloadSuccess: 'Offline manifest downloaded successfully', manifestDownloadFailed: 'Failed to download offline manifest', manifestUnavailableHint: 'Offline manifest is unavailable for unassigned or voided packages',
      intakeReviewTitle: 'Intake Review', intakeReviewNotReady: 'This data package is not reviewable yet: data has not been uploaded or is still being parsed. Wait for the duance upload to finish before reviewing.', intakeReviewApprove: 'Approve', intakeReviewReject: 'Reject', bulkApproveIntake: 'Bulk Approve Intake', bulkApproveConfirm: 'Are you sure you want to approve the selected {count} data packages?', bulkApproveSuccess: 'Successfully approved {count} data packages', intakeApproveConfirm: 'Approve intake for this data package? Once approved, it can be included in data batches.', intakeApproveSuccess: 'Data package intake approved',
      rejectReasonLabel: 'Rejection Reason', rejectReasonPlaceholder: 'Please enter rejection reason (required)', rejectReasonRequired: 'Rejection reason is required', intakeRejectSuccess: 'Data package rejected',
      packageStatusPendingAssignment: 'Pending Assignment', packageStatusAssigned: 'Assigned', packageStatusPendingUpload: 'Pending Upload', packageStatusUploading: 'Uploading', packageStatusParsing: 'Parsing', packageStatusPendingReview: 'Pending Intake Review', packageStatusIntakeApproved: 'Intake Approved', packageStatusBatched: 'Batched', packageStatusParseFailed: 'Parse Failed', packageStatusVoided: 'Voided',
      episodesList: 'Episode List', admissionStatusLabel: 'Admission Status', validityStatusLabel: 'Validity Status', previewStatusLabel: 'Preview', viewVideo: 'View video', noEpisodesInPackage: 'No episodes in this data package yet (upload or parse pending)',
      packageResponsibleCollector: 'Collector', packageOperatorCollector: 'Collector', packageCapturedDuration: 'Captured Duration', packageIntakeValidDuration: 'Intake Valid Duration', packageTargetDuration: 'Target Duration', viewPackageDetails: 'View Details', extraInfo: 'Additional info', closePreview: 'Close', more: 'More',
      openIntakeReview: 'Collection review', enterIntakeReviewAction: 'Enter review', intakeReviewBatchMissing: 'No review batch is available for this data package yet',
      intakeReviewHistory: 'Review history', createSupplementPackage: 'Create supplemental package', acceptedEpisodes: 'Accepted', rejectedEpisodes: 'Marked unqualified', excludedEpisodes: 'Excluded',
      admissionReady: 'Ready', admissionFailed: 'Failed', admissionRunning: 'Processing', runningNotBlocking: 'Processing items do not block review; they are handled at current state on approval',
      admissionReasonLabel: 'Failure reason', privacySensitive: 'Privacy sensitive', reasonMissingFact: 'Not parsed (files missing or validation fact absent)',
      reasonIntegrity: 'Integrity check failed', reasonPreview: 'Preview generation failed', reasonOutput: 'Output verification failed',
      reasonFingerprint: 'Source fingerprint missing/changed', reasonPolicy: 'Validation policy outdated', reasonHumanRejected: 'Marked unqualified by reviewer',
      intakeApprovePreview: 'Approve this package? {rejected} Episode(s) are marked unqualified.', backToBatches: 'Back to review queue',
      reviewPackagesTitle: 'Data package review', dataPackageStatusLabel: 'Data package status', emptyReviewPackages: 'No data packages in this scope.', reviewPackagesHint: 'Intake review is per data package',
      pkgStatusPendingAssignment: 'Pending assignment', pkgStatusAssigned: 'Assigned', pkgStatusPendingUpload: 'Pending upload', pkgStatusUploading: 'Uploading', pkgStatusParsing: 'Parsing', pkgStatusIngested: 'Ingested', pkgStatusPendingIntakeReview: 'Pending intake review', pkgStatusIntakeApproved: 'Intake approved', pkgStatusBatched: 'Batched', pkgStatusGoverning: 'Governing', pkgStatusPublished: 'Published', pkgStatusParseFailed: 'Parse failed', pkgStatusVoided: 'Voided',
      batchSeq: 'Batch', batchTarget: 'Sub-target', batchCollected: 'Collected', batchValid: 'Valid', batchAssignees: 'Assignment', batchWindow: 'Collection window', batchTotalDuration: 'Collected/Valid duration',
      assignBatch: 'Assign batch', assigneeCollectors: 'Collectors', assigneeDevices: 'Devices', dispatchStatusCol: 'Dispatch status', dispatched: 'Dispatched', pendingDispatch: 'Pending dispatch', taskDispatch: 'Dispatch', dispatchDoneToast: 'Batch dispatched', unassigned: 'Unassigned', assignSubmitted: 'Assignment saved', assignWindowPlaceholder: 'for example 08-20 ~ 08-24', viewBatch: 'View', assignPackageAction: 'Assign', assignToTaskHint: "Selected collectors are assigned in turn to the task's pending packages; assigned packages are unchanged.", assignModeEven: 'Assign pending packages in turn', packageDurationHours: 'Package target duration (h)', packageCountPreview: '{count} packages will be generated',
      miningBatchPlanned: 'Planned', miningBatchCollecting: 'Collecting', miningBatchUploaded: 'Uploaded', miningBatchProcessing: 'Processing', miningBatchDone: 'Done',
      failureReasons: 'Failure reasons', emptyMiningTasks: 'No collection task in the current scope.', emptyMiningBatches: 'This task has no batch yet. Split the target first.',
      cloudTransfer: 'Transfer and reconciliation', cloudTransferSource: 'Source', cloudTransferSize: 'Size', cloudTransferChecksum: 'Checksum', cloudTransferVerified: 'Verified', cloudTransferUnverified: 'Pending', cloudTransferRetry: 'Re-sync',
      cloudTransferTotal: 'Transfers', cloudTransferRunning: 'Running', cloudTransferSucceeded: 'Completed', cloudTransferFailed: 'Failed',
      cloudPipeline: 'Cloud pipeline', cloudPipelineName: 'Pipeline', cloudPipelineTrigger: 'Trigger', cloudPipelineStage: 'Stage', cloudPipelineUpdated: 'Last update',
      cloudQuality: 'Quality metrics', cloudQualityValid: 'Valid episodes', cloudQualityRate: 'Valid rate', cloudQualityFailed: 'Failed episodes',
      cloudSourceEdge: 'Edge node', cloudSourceUpload: 'Local upload', cloudSourceOss: 'OSS scan',
      pipelineQueued: 'Queued', pipelineRunning: 'Running', pipelineSucceeded: 'Succeeded', pipelineFailed: 'Failed',
      transferQueued: 'Queued', transferUploading: 'Uploading', transferRunning: 'Syncing', transferSucceeded: 'Completed', transferFailed: 'Failed',
      miningCuts: 'Data viewer', cutEpisodeList: 'Cut episodes', cutAllTasks: 'All tasks', searchCutEpisode: 'Search episode / collector / batch',
      miningDash: 'Collection overview', miningConfig: 'Collection settings', miningPlanHint: 'Collection tasks define delivery targets and schedules; batches are generated from targets.',
      dataBuild: 'Build data', dataOverview: 'Data overview', intakeOverviewTab: 'Collected data', intakeBatchTab: 'Data batches', stageEnabled: 'Enabled', stageDisabled: 'Disabled', annotateStatusPending: 'Pending claim', annotateReviewApproved: 'Approved', annotateReviewRejected: 'Rejected', stagePassed: 'Passed', stageRejected: 'Failed', governanceError: 'Error', annotateUnsubmittedHint: 'Contains unannotated data; review submission is blocked', reportFailedReason: 'Failure reason', reportErrorReason: 'Error reason', annotatedCount: 'Annotated', dataPackageName: 'Data package name', collectedDuration: 'Collected duration', validDuration: 'Valid collected duration', uploadTime: 'Upload time', videoCountCol: 'Videos', videoCountNumCol: 'Videos', stageRerunDone: 'Re-check completed', rerunIntegrityLabel: 'Re-run integrity', rerunQualityLabel: 'Re-run QC', rerunDesensitizeLabel: 'Re-desensitize', prevVideo: 'Previous', nextVideo: 'Next', unannotatedCount: 'Unannotated', annotateStatusProcessing: 'Processing', viewAction: 'View', enterAnnotationWorkbench: 'Enter annotation workbench', viewGovernanceReport: 'View governance report', governanceReportTitle: 'Governance report', reportNoIssues: 'No failed or error videos', buildBatchNameCol: 'Data batch name', buildDataNameCol: 'Data batch name', dataBatchNameCol: 'Data batch name', governanceStatusCol: 'Governance status', governanceNotEnabled: 'Not enabled', governanceFailed: 'Failed', governanceIncomplete: 'Incomplete', governanceCompleted: 'Completed', governanceNotStarted: 'Not started', governanceStatusRunning: 'In progress', governanceStatusFailed: 'Failed', markGovernanceDone: 'Mark as completed', governanceManualDoneToast: 'Manually marked as completed', collectProject: 'Collection project', dataAnnotation: 'Data annotation', dataTab: 'Data', trainTab: 'Training', annotationDesc: 'Annotation description', annotationProgressCol: 'Annotation progress', taskUnclaimed: 'Unclaimed tasks', annotatedSlashTotal: 'Annotated / total', submittedUnreviewed: 'Submitted, unreviewed', reviewDone: 'Review completed', buildDatasetAction: 'Build dataset', datasetRemark: 'Remark', datasetNameRequired: 'Please enter the dataset name', buildDatasetSuccess: 'Dataset created and added to training datasets', annotationSegments: 'Annotation segments', approveReview: 'Approve review', rejectReview: 'Reject review', reviewRejectedLabel: 'Rejected', annotateStatusSubmitted: 'Submitted', annotateStatusUnreviewed: 'Unreviewed', annotateStatusReviewing: 'Reviewing', annotateReviewPassedLabel: 'Approved', annotateReviewFailedLabel: 'Rejected', annotateLineLabeled: 'Labeled / total', annotateLineSubReviewed: 'Sub-videos reviewed / total', importSegDesc1: 'Put the ball into the box', importSegDesc2: 'Stack the blue box', importSegDesc3: 'Stack the yellow box', importSegDesc4: 'Release and return', buildLogicalDataset: 'Build data batch', importList: 'Data batches', importBatchExisting: 'Existing batch', importBatchCreate: 'New batch', dataProcess: 'Process data', dataConsolidate: 'Consolidate data', collectedData: 'Collection review', buildData: 'Build data', logicalData: 'Logical data', annotateWorkbench: 'Annotate workbench', trainDataset: 'Training datasets', splitWorkbench: 'Split workbench', dataPreview: 'Data preview', logicalSplitRunning: 'Splitting', logicalSplitPending: 'Pending split', logicalAnnotateRunning: 'Annotating', logicalAnnotateTodo: 'Not started', logicalReviewPassed: 'Passed', logicalReviewPending: 'Pending review', logicalReviewRejected: 'Rejected', splitStatusCol: 'Split status', annotateStatusCol: 'Annotation status', splitReviewCol: 'Split review', annotateReviewCol: 'Annotation review',
      buildDataAction: 'Build dataset', buildExit: 'Exit build', buildSelectedCount: '{count} episodes selected', buildSubmitted: 'Dataset build submitted ({count} episodes)',
      consolidateDataset: 'Consolidate dataset', consolidateTitle: 'Consolidate into dataset', consolidateName: 'Dataset name', consolidateNamePlaceholder: 'e.g. tabletop-grasp-v1', consolidateFilterTask: 'Task', consolidateFilterDuration: 'Duration range (s)', consolidateFilterRange: 'Captured period', consolidateEpisodesTitle: 'Available episodes', consolidateSelectedCount: '{count} episodes selected', consolidateAction: 'Consolidate', consolidateEmpty: 'No consolidated datasets yet. QC-passed and desensitized episodes can be consolidated anytime — splitting is optional.', consolidateList: 'Consolidated datasets', episodeSplitCount: 'Split styles', splitManage: 'Split manage', splitReuseTitle: 'This episode already has split styles', splitReuseOption: 'Reuse {name} · v{version} ({count} segments)', splitNewOption: 'New split', splitConfirm: 'Start split', splitDoneToast: 'Split submitted', annotateAction: 'Annotate', trainAction: 'Train', datasetEpisodeCount: 'Episodes', datasetModesCount: '{count} styles', datasetEpisodesTitle: 'Dataset episodes', reuseModeLabel: 'Reused', newModeLabel: 'New',
      dashCapacity: 'Capacity', dashCollection: 'Data collection', dashEfficiency: 'Human efficiency',
      dashPlanTarget: 'Plan target', dashRawTotal: 'Raw collected', dashValidTotal: 'Valid data', dashCompletion: 'Plan completion',
      dashDate: 'Date', dashDailyRaw: 'Collected', dashDailyValid: 'Valid', dashDailyTrend: '7-day capacity trend',
      dashByScene: 'By scene', dashByPurpose: 'By purpose', dashByProject: 'By project', dashBatchStages: 'Batch status',
      dashCollectors: 'Collector leaderboard', dashDevices: 'Device stability', dashCollected: 'Collected', dashAvgPerDay: 'Avg / day', dashAbnormal: 'Abnormal count', dashRank: 'Rank',
      batchStageCollecting: 'Collecting', batchStageProcessing: 'Processing', batchStageDone: 'Done',
      configDictionaries: 'Tag dictionaries', dashCollectBoard: 'Capacity dashboard', dashDataType: 'Data type', dashTypeStats: 'Projects & types', dashTypeStatsTitle: 'Projects & types', dashPersonnelStats: 'Personnel collection duration', dashCountsStats: 'Data collection metrics', dashActiveStats: 'Activity metrics', dashUnitCount: '', dashUnitPeople: '', dashRegion: 'Region', dashDateStart: 'Start date', dashDateEnd: 'End date', dashDataBoard: 'Data dashboard', dashPeopleBoard: 'Efficiency dashboard', dashProjectDist: 'Projects distribution', dashProjectTrend: 'Project completion trend', dashTaskDist: 'Tasks distribution', dashTaskTrend: 'Task completion trend', dashSceneDist: 'Scenes distribution', dashSceneTrend: 'Scene completion trend', dashProjectsCard: 'projects', dashTypeCollected: 'collected duration', dashTypeValid: 'valid duration', dashCollectedTrend: 'Collected duration trend', dashValidTrend: 'Valid duration trend', dashWorkTotal: 'Total work duration', dashCollectedCard: 'Collected duration', dashValidCard: 'Valid duration', dashPerMachine: 'Valid duration per machine/day', dashDelta: 'DoD', dashWeek: 'Week', dashDay: 'Day', dashHour: 'Hour', dashMonth: 'Month', dashAllAll: 'All', dashValidOnly: 'Valid', dashModuleProject: 'Projects', dashModuleTask: 'Tasks', dashModuleDuration: 'Duration', dashModuleSize: 'Size', dashProjectsTotal: 'Total projects', dashProjectsToday: 'New projects today', dashProjectsTrend: 'Project count trend', dashTasksTotal: 'Total tasks', dashTasksToday: 'New tasks today', dashTasksTrend: 'Task count trend', dashEpisodesTotal: 'Total packages', dashEpisodesToday: 'New packages today', dashEpisodesTrend: 'Package count trend', dashCollectedTotal: 'Collected duration', dashCollectedToday: 'New collected duration today', dashValidDuration: 'Valid duration', dashTodayValidDuration: 'New valid duration today', dashValidSizeTotal: 'Total valid data size', dashTodayValidSize: 'New valid data size today', dashCollectedTrendH: 'Collected duration trend (hours)', dashSizeTotal: 'Total data size (GB/TB)', dashSizeToday: 'New data size today (GB/TB)', dashSizeTrend: 'Data size trend (GB/TB)', dashDailyActive: 'Daily active people', dashActiveTrend: 'Active people trend', dashActiveDurationTrend: 'Active duration trend', dashAvgActiveTrend: 'Average active duration trend', dashActiveDist: 'Active duration distribution', dashOutputRanking: 'People output ranking', dashWorkSummary: 'Work duration summary', dashValidCollected: 'Valid collected duration', dashValidRatio: 'Valid output ratio', configAddTag: 'Add', configDefaultSettings: 'Default collection settings', configMinRate: 'Minimum valid rate', configBatchSize: 'Default batch size', configEdgeSync: 'Edge sync policy', configEdgeAuto: 'Auto upload', configEdgeManual: 'Manual batch sync', configDesensEdge: 'Desensitize on edge', configSaved: 'Settings saved', configTagExists: 'Tag already exists',
      tagProject: 'Project tag', tagScene: 'Scene tag', tagPurpose: 'Purpose', tagTrain: 'Training tag', tagSoftware: 'Software version', tagDeviceVersion: 'Device version',
      allProjects: 'All projects', allScenes: 'All scenes', allPurposes: 'All purposes', allTrains: 'All training tags',
      cutTaskName: 'Collection task', cutCollector: 'Collector', cutDevice: 'Device', cutSegments: 'Segments', cutPreviewTitle: 'Online viewer',
      cutSegmentList: 'Sub-splits', cutSegmentDesc: 'Description', cutRange: 'Range', cutValid: 'Valid', cutInvalid: 'Invalid',
      cutEmpty: 'No cut episode in the current scope.', cutSelectHint: 'Select a cut episode to review its sub-splits online', cutPlaying: 'Playing', cutBatch: 'Batch',
      cutPreview: 'Split preview', previewReviewHint: 'Preview the cut result first, then approve, re-run the auto split, or restore to raw.',
      splitStatusDone: 'Done', splitStatusTodo: 'Pending split', splitChoiceTitle: 'Split method', splitChoiceAuto: 'Auto split', splitChoiceManual: 'Manual split',
      splitChoiceAutoHint: 'The system splits automatically by preset rules, then runs auto QC.', splitChoiceManualHint: 'Switch to manual cutting in the cut workbench of the work queue.',
      splitAction: 'Split', splitDoneToast: 'Split completed', splitManualToast: 'Switched to manual split',
      splitUnfinished: 'Unfinished', splitRunningLabel: 'Auto splitting', splitFailedLabel: 'Split failed', splitReviewPending: 'Split done · review pending', manualReview: 'Manual review', resplitAction: 'Re-split',
      taskOwner: 'Created by', taskDetail: 'Task details', creatorCol: 'Creator', totalDurationCol: 'Total duration',
      cutReview: 'Review splits', backToMiningCloud: 'Back to data production', cutAuditTitle: 'Split review', cutAuditDone: 'Split approved. QC stage unlocked.',
      miningBatchProcessing: 'Batch processing', stageIntegrity: 'Integrity check', stageSplit: 'Auto split', stageQuality: 'Auto QC', stageDesensitize: 'Desensitize', stageSplitStatus: 'Split status',
      miningQuickCreate: 'Create', miningTaskConfigHintBefore: 'No options in the dropdown? Go to ', miningTaskConfigHintAfter: '.',
      stageQueued: 'Queued', stageBlocked: 'Blocked', stageRunning: 'Running', stagePendingReview: 'Pending review', stageSucceeded: 'Done', stageFailed: 'Failed', stageManual: 'Manual cutting',
      splitModeAuto: 'Auto split', splitModeManual: 'Manual split', splitReviewHint: 'Auto split finished. Review the cut result first.', miningCloudTask: 'Collection task',
      actRunIntegrity: 'Integrity check', actRunSplit: 'Auto split', actRunQuality: 'Auto QC', actRunDesensitize: 'Desensitize', actRetryStage: 'Retry',
      actApproveSplit: 'Approve', actRerunSplit: 'Re-run split', actRestoreManual: 'Restore to raw', actManualCut: 'Manual cut',
      miningStageStarted: 'Stage started', miningStageDone: 'Stage completed', miningSplitApproved: 'Split result approved', miningSplitRework: 'Auto split re-started', miningSplitRestored: 'Restored to raw. Cut manually.', miningStageBlocked: 'Finish the previous stage first',
      settingsProjects: 'Collection Projects', settingsLabels: 'Label Dictionaries', settingsWorkspaces: 'Collection workspaces', emptyWorkspaces: 'No collection workspaces', workspaceCreator: 'Creator', settingsQuickCreate: 'New', createCollectionProject: 'New collection project', editCollectionProject: 'Edit Project', archiveCollectionProject: 'Archive', archiveConfirm: 'Are you sure you want to archive this collection project?', emptyProjects: 'No collection projects', sceneLabels: 'Scene Labels', purposeLabels: 'Purpose Labels', trainingLabels: 'Training Purpose', modalityLabels: 'Modality Labels', addLabel: 'Add Label', labelName: 'Label Name', labelDeactivated: 'Label deactivated', labelCreated: 'Label created', emptyLabels: 'No labels', settingsLoadFailed: 'Settings data failed to load. Please retry. Failed sections:', settingsLoadRetry: 'Retry loading', settingsUnavailable: 'Settings data has not loaded; changes are temporarily disabled.', viewDataPackages: 'View Packages', taskDataPackages: 'Data Packages', taskDetails: 'Task Details',
      reviewSourceFilter: 'Source', reviewSourceAll: 'All annotations', reviewSourceAlgorithm: 'Algorithm only', reviewSourceHuman: 'Human only', reviewAlgorithmSource: 'Algorithm',
      reviewLowConfidence: 'Low confidence only', reviewSource: 'Source', reviewHumanSource: 'Human',
    },
  };

  function readStoredLocale() {
    try {
      return localStorage.getItem('quicdata_locale') === 'en-US' ? 'en-US' : 'zh-CN';
    } catch {
      return 'zh-CN';
    }
  }

  function routeStateFromLocation() {
    if (typeof window === 'undefined') return { view: 'overview', workItemId: '', episodeId: '', query: {} };
    const raw = String(window.location.hash || '').replace(/^#\/?/, '');
    const [path, search = ''] = raw.split('?', 2);
    const parts = path.split('/').filter(Boolean);
    const requestedView = parts[0] || 'overview';
    const view = requestedView === 'episodes' ? 'assets' : requestedView;
    if (requestedView === 'episodes') window.location.hash = '#/assets';
    if (!VIEWS.has(view)) return { view: 'overview', workItemId: '', episodeId: '', query: {} };
    const params = new URLSearchParams(search);
    return {
      view,
      workItemId: view === 'workbench' ? parts[1] || '' : '',
      episodeId: view === 'workbench' ? params.get('episode_id') || '' : '',
      query: Object.fromEntries(params.entries()),
    };
  }

  function routeFromLocation() {
    return routeStateFromLocation().view;
  }

  function shouldReplaceWorkbenchDraft(previousSnapshot, nextSnapshot, dirty) {
    const previousItemId = Number(previousSnapshot?.work_item?.id || 0);
    const nextItemId = Number(nextSnapshot?.work_item?.id || 0);
    return previousItemId !== nextItemId || !dirty;
  }

  const FilterSearchBox = {
    name: 'FilterSearchBox',
    props: {
      modelValue: { type: Array, default: () => [] },
      attrs: { type: Array, default: () => [] },
      valueMap: { type: Object, default: () => ({}) },
      placeholder: { type: String, default: '' },
      menuTitle: { type: String, default: '' },
      allLabel: { type: String, default: '全选' },
      confirmLabel: { type: String, default: '确定' },
      cancelLabel: { type: String, default: '取消' },
    },
    emits: ['update:modelValue', 'keyword-change', 'change'],
    data() {
      return { keyword: '', currentAttr: '', checked: [] };
    },
    computed: {
      currentValues() {
        return this.valueMap[this.currentAttr] || [];
      },
      allChecked() {
        return this.currentValues.length > 0 && this.checked.length === this.currentValues.length;
      },
    },
    methods: {
      attrName(key) {
        return this.attrs.find((attr) => attr.key === key)?.label || key;
      },
      syncChecked(key) {
        this.checked = this.modelValue.filter((filter) => filter.attr === key).map((filter) => filter.value);
      },
      toggleAll(checked) {
        this.checked = checked ? [...this.currentValues] : [];
      },
      confirm() {
        const attr = this.currentAttr;
        if (!attr) return;
        const rest = this.modelValue.filter((filter) => filter.attr !== attr);
        const next = [...rest, ...this.checked.map((value) => ({ attr, value }))];
        this.$emit('update:modelValue', next);
        this.$emit('change', next);
        this.currentAttr = '';
        this.$refs.popper?.hide?.();
      },
      cancel() {
        this.currentAttr = '';
        this.$refs.popper?.hide?.();
      },
      removeFilter(filter) {
        const next = this.modelValue.filter((item) => item !== filter);
        this.$emit('update:modelValue', next);
        this.$emit('change', next);
      },
      clearAll() {
        this.keyword = '';
        this.$emit('keyword-change', '');
        this.$emit('update:modelValue', []);
        this.$emit('change', []);
      },
      onOpen() {
        this.currentAttr = '';
      },
    },
    template: `
      <div class="filter-search-box">
        <el-popover ref="popper" trigger="click" placement="bottom-start" :width="300" popper-class="filter-search-popper" @show="onOpen">
          <template #reference>
            <div class="filter-search-field">
              <span v-for="filter in modelValue" :key="filter.attr + filter.value" class="filter-token">
                <span class="filter-token-label">{{ attrName(filter.attr) }}:</span>
                <span class="filter-token-value">{{ filter.value }}</span>
                <span class="filter-token-close" @click.stop="removeFilter(filter)">✕</span>
              </span>
              <input v-model="keyword" class="filter-search-input" :placeholder="modelValue.length ? '' : placeholder" @input="$emit('keyword-change', keyword)" @keyup.enter="$emit('keyword-change', keyword)" />
              <span class="filter-search-actions">
                <span v-if="modelValue.length || keyword" class="filter-search-clear" @click.stop="clearAll">✕</span>
                <span class="filter-search-trigger">⌕</span>
              </span>
            </div>
          </template>
          <div class="filter-search-menu">
            <template v-if="!currentAttr">
              <p class="filter-search-title">{{ menuTitle }}</p>
              <label v-for="attr in attrs" :key="attr.key" class="filter-search-option" @click.prevent="currentAttr = attr.key; syncChecked(attr.key)">{{ attr.label }}</label>
            </template>
            <template v-else>
              <div class="filter-search-option filter-search-checkall">
                <el-checkbox :model-value="allChecked" @change="toggleAll">{{ allLabel }}</el-checkbox>
              </div>
              <div class="filter-search-optionlist">
                <el-checkbox-group v-model="checked">
                  <label v-for="value in currentValues" :key="value" class="filter-search-option">
                    <el-checkbox :label="value">{{ value }}</el-checkbox>
                  </label>
                </el-checkbox-group>
              </div>
              <div class="filter-search-footer">
                <el-button type="primary" size="small" :disabled="!checked.length" @click="confirm">{{ confirmLabel }}</el-button>
                <el-button size="small" text @click="cancel">{{ cancelLabel }}</el-button>
              </div>
            </template>
          </div>
        </el-popover>
      </div>
    `,
  };

  const app = createApp({
    components: { FilterSearchBox },
    setup() {
      const ready = ref(false);
      const user = ref(null);
      const sessionRestoring = ref(true);
      const locale = ref(readStoredLocale());
      const initialRouteWasExplicit = typeof window !== 'undefined' && Boolean(String(window.location.hash || '').replace(/^#\/?/, ''));
      const initialRoute = routeStateFromLocation();
      const initialListQuery = initialRoute.query || {};
      const initialQueryValue = (view, key, fallback = '') => (
        initialRoute.view === view ? (initialListQuery[key] ?? fallback) : fallback
      );
      const initialQueryNumber = (view, key, fallback = '') => {
        const value = Number(initialQueryValue(view, key, fallback));
        return Number.isInteger(value) && value > 0 ? value : fallback;
      };
      const activeView = ref(initialRoute.view);
      const workbenchRoute = reactive({ workItemId: initialRoute.workItemId, episodeId: initialRoute.episodeId });
      const sidebarCollapsed = ref(false);
      const workbenchQueueOpen = ref(['work-queue', 'workbench'].includes(initialRoute.view));
      const miningNavOpen = ref(['miningTasks', 'miningCloud'].includes(initialRoute.view));

      const sidebarGroupOpen = reactive({
        manage: ['miningDash', 'miningTasks', 'batches', 'resources', 'miningConfig'].includes(initialRoute.view),
        build: ['intake', 'datasets'].includes(initialRoute.view),
        process: ['miningCuts', 'work-queue', 'workbench'].includes(initialRoute.view),
        annotate: ['work-queue', 'workbench'].includes(initialRoute.view),
        consolidate: ['assets'].includes(initialRoute.view),
        train: ['trainDash', 'trainJobs', 'trainNew', 'trainDatasets', 'trainModels', 'trainResources', 'trainSystem'].includes(initialRoute.view),
        management: ['admin', 'settings'].includes(initialRoute.view),
      });

      function toggleSidebarGroup(key) {
        sidebarGroupOpen[key] = !sidebarGroupOpen[key];
      }

      function navigateDatasetsTab() {
        navigate('datasets');
      }
      const collectionProjects = ref([]);
      const adminActiveTab = ref('users');
      const settingsActiveTab = ref('projects');
      const loadingSettingsCenter = ref(false);
      const settingsCenterLoadFailures = ref([]);
      const settingsCenterLoadedWorkspaceId = ref(null);
      let settingsCenterGeneration = 0;
      const showCollectionProjectDialog = ref(false);
      const editingCollectionProject = ref(null);
      const collectionProjectForm = reactive({ name: '', description: '' });
      const savingCollectionProject = ref(false);
      let collectionProjectDialogWorkspaceId = 0;
      let collectionProjectWriteGeneration = 0;
      const newLabelInputs = reactive({ scene: '', purpose: '', training: '', modality: '' });
      const creatingLabel = reactive({ scene: false, purpose: false, training: false, modality: false });
      const miningTasks = ref([]);
      const miningTaskTableRef = ref(null);
      const miningSelectedTaskId = ref(initialQueryValue('miningTasks', 'task_id', ''));
      const miningTransfers = ref([]);
      const miningPipelines = ref([]);
      const showMiningTaskDialog = ref(false);
      const showMiningSplitDialog = ref(false);
      const showMiningAssignDialog = ref(false);
      let miningTaskDialogWorkspaceId = 0;
      let miningSplitDialogWorkspaceId = 0;
      let miningAssignDialogWorkspaceId = 0;
      let miningAssignTaskId = '';
      let miningWriteGeneration = 0;
      let miningTaskSelectionGeneration = 0;
      const miningAssignScope = ref('batch');
      const miningTaskForm = reactive({ name: '', modality: 'ego', sop: '', target_episodes: 1000, target_duration_hours: 100, package_hours: 2, min_valid_rate: 0.9, due_date: '', project: '', purpose: '', scene: '', train: '' });
      const miningPackageCountPreview = computed(() => QuicDataMiningUtils.packageCountPreview(miningTaskForm.target_duration_hours, miningTaskForm.package_hours));
      const miningSplitForm = reactive({ batch_count: 10, per_batch: 100 });
      const miningSplitTotalHours = computed(() => {
        const total = Number(miningSelectedTask.value?.target_duration_hours);
        return Number.isFinite(total) && total > 0 ? total : 0;
      });
      const miningSplitPerBatchHours = computed(() => {
        const count = Math.max(1, Number(miningSplitForm.batch_count) || 1);
        const total = miningSplitTotalHours.value;
        return total > 0 ? (Math.round((total / count) * 100) / 100).toFixed(2) : '—';
      });
      const miningAssignForm = reactive({ batch_id: '', collector_ids: [], device_id: '', window: '', mode: 'even' });
      const miningCutEpisodes = ref([]);
      const miningCutTask = ref('');
      const miningCutKeyword = ref('');
      const miningCutProject = ref('');
      const miningCutScene = ref('');
      const miningCutPurpose = ref('');
      const miningCutTrain = ref('');
      const miningCutBatch = ref('');
      const miningCutSelectedId = ref('');
      const miningDashTab = ref('collect');
      const miningDashTrendRange = ref('week');
      const miningDashFilters = reactive({ range: null, robot: '', project: [], task: [], scene: [], data_type: [], purpose: [], region: [] });
      const miningDashRegions = ['上海', '深圳', '杭州', '北京'];
      const miningDashTaskOptions = computed(() => {
        const list = demoMode.value ? QuicDataMining.listTasks() : (miningTasks.value || []);
        return list.map((task) => task.name).filter(Boolean);
      });

      function clearMiningDashFilters() {
        miningDashFilters.range = null;
        miningDashFilters.robot = '';
        miningDashFilters.project = [];
        miningDashFilters.task = [];
        miningDashFilters.scene = [];
        miningDashFilters.data_type = [];
        miningDashFilters.purpose = [];
        miningDashFilters.region = [];
      }

      function miningDashDataTypeOf(task) {
        return String(task.modality || 'ego').toUpperCase();
      }

      function miningDashDisableDate(date) {
        return date.getTime() > Date.now() || date.getTime() < Date.now() - 365 * 86400000;
      }
      const miningDashGranularity = ref('day');
      const miningDashMetricMode = ref('all');
      const miningDictionaries = ref([]);

      const overviewStageRerunOverrides = reactive({});
      function governanceBackendStageKey(stage) {
        return stage === 'desensitize' ? 'compliance' : stage;
      }

      function governanceBackendStage(row, stage) {
        const report = row?.governance_report;
        if (!report || !Array.isArray(report.stages)) return null;
        const key = governanceBackendStageKey(stage);
        return report.stages.find((item) => item?.stage === key) || null;
      }

      function governanceBackendIssueItems(row, stage, stageRow) {
        const result = stageRow?.result_json && typeof stageRow.result_json === 'object'
          ? stageRow.result_json
          : {};
        const raw = stage === 'integrity'
          ? result.issues
          : stage === 'quality'
            ? result.dropped
            : [];
        if (!Array.isArray(raw)) return [];
        return raw.map((item) => {
          const id = item?.episode_id ?? item?.data_package_id ?? item?.id ?? row?.id;
          return {
            videoId: id,
            name: item?.name || (id != null ? `Episode ${id}` : row?.name || '—'),
            reason: String(item?.reason || stageRow?.error_message || 'governance check failed'),
          };
        });
      }

      function governanceBackendStageCounts(row, stage, stageRow) {
        const result = stageRow?.result_json && typeof stageRow.result_json === 'object'
          ? stageRow.result_json
          : {};
        const total = Math.max(1, Number(row?.governance_total) || Number(row?.episode_count) || 1);
        const issues = governanceBackendIssueItems(row, stage, stageRow);
        const resultCount = stage === 'integrity'
          ? Number(result.issue_count) || 0
          : stage === 'quality'
            ? Number(result.dropped_count) || 0
            : 0;
        const issueCount = Math.max(issues.length, resultCount);
        const status = String(stageRow?.status || 'queued');
        const failed = status === 'failed' ? Math.max(1, issueCount) : issueCount;
        const error = status === 'error' ? 1 : 0;
        const passed = ['passed', 'failed'].includes(status)
          ? Math.max(0, total - failed - error)
          : 0;
        return { total, passed, failed, error };
      }

      function normalizeGovernanceStageStatus(status) {
        const value = String(status || 'queued');
        if (value === 'passed') return 'succeeded';
        if (value === 'failed') return 'failed';
        if (value === 'running') return 'running';
        if (value === 'skipped') return 'queued';
        if (value === 'queued') return 'queued';
        return 'error';
      }

      function overviewImportStage(row, stage) {
        const overrideKey = `${row?.id}-${stage}`;
        const backendStage = governanceBackendStage(row, stage);
        if (backendStage) {
          const counts = governanceBackendStageCounts(row, stage, backendStage);
          return {
            status: normalizeGovernanceStageStatus(backendStage.status),
            text: `${counts.passed}/${counts.total}`,
            counts,
          };
        }
        if (overviewStageRerunOverrides[overrideKey] && demoMode.value) {
          const total2 = Number(row.total_frames) || 180;
          return { status: overviewStageRerunOverrides[overrideKey], text: `${total2}/${total2}` };
        }
        const total = Number(row.total_frames) || 180;
        if (!demoMode.value) return { status: 'queued', text: `0/${total}` };
        const seed = Number(row?.id) || 0;
        const scenario = seed % 6;
        const order = ['integrity', 'quality', 'desensitize'];
        const stageIdx = order.indexOf(stage);
        const mk = (status, done) => ({ status, text: `${done}/${total}` });
        // Stages have sequential dependencies: integrity check -> automated QC -> desensitization; downstream will not run if upstream fails/incomplete
        if (scenario === 0) {
          return mk('succeeded', total);
        }
        if (scenario === 1) {
          if (stageIdx === 0) return mk('running', 45);
          return mk('queued', 0);
        }
        if (scenario === 2) {
          if (stageIdx === 0) return mk('failed', 90);
          return mk('queued', 0);
        }
        if (scenario === 3) {
          if (stageIdx === 0) return mk('succeeded', total);
          if (stageIdx === 1) return mk('running', 120);
          return mk('queued', 0);
        }
        if (scenario === 4) {
          if (stageIdx === 0) return mk('succeeded', total);
          if (stageIdx === 1) return mk('failed', 150);
          return mk('queued', 0);
        }
        if (stageIdx === 0) return mk('succeeded', total);
        if (stageIdx === 1) return mk('succeeded', total);
        return mk('error', 60);
      }

      const batchCandidates = ref([]);
      let batchCandidatesGeneration = 0;
      const overviewImportRows = computed(() => batchCandidates.value.map((pkg) => ({
        id: pkg.id,
        data_package_id: pkg.id,
        name: pkg.package_uid || `package-${pkg.id}`,
        package_uid: pkg.package_uid,
        project_name: pkg.project_name || pkg.collection_project?.name || '',
        task_name: pkg.task_name || pkg.collection_task?.name || pkg.task_label?.name || '',
        owner: pkg.owner || '',
        scene: pkg.scene || pkg.tags?.scene || '',
        purpose: pkg.purpose || pkg.tags?.purpose || '',
        train: pkg.train || pkg.tags?.train || '',
        channel: 'data_package',
        modality: pkg.modality || '',
        collector_name: pkg.collector_name || pkg.responsible_collector?.name || '',
        device: pkg.device || { name: '—', serial: '—', version: '—' },
        reference_frame_count: Number(pkg.reference_frame_count) || 0,
        duration_s: Math.round(Number(pkg.intake_valid_duration_hours || 0) * 3600),
        total_frames: Number(pkg.total_frames) || 0,
        updated_at: pkg.updated_at,
        governance: pkg.governance || {},
        annotation_status: pkg.annotation_status || '',
        annotation_descs: Array.isArray(pkg.annotation_descs) ? pkg.annotation_descs : [],
        target_duration_s: Math.round(Number(pkg.target_duration_hours || 0) * 3600),
        collected_duration_s: Math.round(Number(pkg.intake_valid_duration_hours || 0) * 3600),
        valid_duration_s: Math.round(Number(pkg.governed_valid_duration_hours || pkg.intake_valid_duration_hours || 0) * 3600),
        collection_project_id: pkg.collection_project_id,
        collection_task_id: pkg.collection_task_id,
        status: pkg.status,
      })));

      const overviewImportGroups = computed(() => {
        const rows = overviewImportRows.value;
        if (!rows.length) return [];
        return [{
          id: 'candidates',
          name: t('intakeOverviewTab'),
          created_at: rows[0]?.updated_at,
          rows: [...rows],
          task_name: '',
          modality: '',
          collector_name: '',
          device: { name: '', serial: '', version: '' },
          channel: '',
        }];
      });

      function viewImportDuration(row) {
        ElMessage.info(`${row.name} ${t('duration')}: ${formatDuration(row.duration_s)}`);
      }

      const overviewImportExpandedGroupIds = ref(new Set());
      const overviewFilterState = reactive({ task: [], project: '', owner: [], purpose: [], scene: [], train: [], modality: [], collector: [], device: [], uploadEnd: null });
      function overviewImportTaskInfo(row) {
        const target = row && row.row_kind ? (row.item || row.group || row) : (row || {});
        if (target.scene != null || target.train != null || target.owner != null || target.purpose != null) {
          return { project: target.project_name || target.project || '', owner: target.owner || '', purpose: target.purpose || '', scene: target.scene || '', train: target.train || '' };
        }
        const map = overviewTaskInfoByName.value;
        return map[target?.task_name || row?.task_name] || {};
      }
      const overviewTaskInfoByName = computed(() => {
        const map = {};
        const source = miningTaskRows.value.length ? miningTaskRows.value : (demoMode.value ? (QuicDataMining.listTasks() || []) : []);
        for (const row of source) {
          if (row.name) {
            map[row.name] = { project: row.tags?.project || '', owner: row.owner || '', purpose: row.tags?.purpose || '', scene: row.tags?.scene || '', train: row.tags?.train || '' };
          }
        }
        return map;
      });
      const overviewFilterActive = computed(() => Object.values(overviewFilterState).some((v) => (Array.isArray(v) ? v.length > 0 : v != null && v !== '')));
      const overviewFilterOptions = computed(() => {
        const rows = overviewImportRows.value;
        const labelNames = (category) => [
          ...new Set(
            collectionLabels.value
              .filter((item) => item.category === category && item.is_active !== false)
              .map((item) => item.name)
              .filter(Boolean),
          ),
        ];
        return {
          tasks: [...new Set(rows.map((r) => r.task_name).filter(Boolean))],
          projects: [...new Set(rows.map((r) => overviewImportTaskInfo(r).project).filter(Boolean))],
          owners: [...new Set(rows.map((r) => overviewImportTaskInfo(r).owner).filter(Boolean))],
          purposes: labelNames('purpose'),
          scenes: labelNames('scene'),
          trains: labelNames('training'),
          modalities: labelNames('modality'),
          collectors: [...new Set(rows.map((r) => r.collector_name).filter(Boolean))],
          devices: [...new Set(rows.map((r) => r.device?.name).filter(Boolean))],
        };
      });
      function matchOverviewFilters(row) {
        // Label/modality filters are applied server-side via candidates label_ids.
        const info = overviewImportTaskInfo(row);
        if (overviewFilterState.task.length && !overviewFilterState.task.includes(row.task_name)) return false;
        if (overviewFilterState.project && info.project !== overviewFilterState.project) return false;
        if (overviewFilterState.owner.length && !overviewFilterState.owner.includes(info.owner)) return false;
        if (overviewFilterState.collector.length && !overviewFilterState.collector.includes(row.collector_name)) return false;
        if (overviewFilterState.device.length && !overviewFilterState.device.includes(row.device?.name)) return false;
        if (Array.isArray(overviewFilterState.uploadEnd) && overviewFilterState.uploadEnd.length === 2 && overviewFilterState.uploadEnd[0] && overviewFilterState.uploadEnd[1]) {
          const day = String(row.updated_at || '').slice(0, 10);
          const startDay = String(overviewFilterState.uploadEnd[0]).slice(0, 10);
          const endDay = String(overviewFilterState.uploadEnd[1]).slice(0, 10);
          if (!day || day < startDay || day > endDay) return false;
        }
        return true;
      }
      function clearOverviewFilters() {
        Object.assign(overviewFilterState, { task: [], project: '', owner: [], purpose: [], scene: [], train: [], modality: [], collector: [], device: [], uploadEnd: null });
        loadDataBatchCandidates();
      }
      const overviewImportTreeRows = computed(() => {
        const rows = [];
        overviewImportGroups.value.forEach((group) => {
          const items = group.rows || [];
          const visibleItems = overviewFilterActive.value ? items.filter((row) => matchOverviewFilters(row)) : items;
          visibleItems.forEach((item) => {
            rows.push({ row_kind: 'item', row_key: `item-${String(group.id)}-${String(item.id)}`, group, item });
          });
        });
        return rows;
      });
      function overviewImportRowField(row, key) {
        return row.row_kind === 'source' ? row.group?.[key] : row.item?.[key];
      }
      function overviewImportDeviceOf(row) {
        return overviewImportRowField(row, 'device') || { name: '', serial: '', version: '' };
      }
      function overviewImportGroupDuration(group) {
        return (group.rows || []).reduce((sum, row) => sum + (Number(row.duration_s) || 0), 0);
      }
      function overviewImportGroupRefFrames(group) {
        return (group.rows || []).reduce((sum, row) => sum + (Number(row.reference_frame_count) || 0), 0);
      }
      function overviewImportStageRow(row) {
        if (!row) return {};
        if (row.row_kind === 'item') return row.item;
        if (row.row_kind === 'source') return row.group?.rows?.[0] || {};
        return row;
      }
      function overviewImportStageEnabled(row, stage) {
        const g = row?.governance || {};
        if (stage === 'integrity') return Boolean(g.integrity);
        if (stage === 'quality') return Boolean(g.quality);
        return Boolean(g.desensitize ?? g.compliance);
      }
      function overviewImportAnnotateStatus(row) {
        const item = overviewImportStageRow(row);
        if (item?.annotation_status) return item.annotation_status;
        if (item?.annotation) return t('annotateStatusPending');
        return '';
      }
      function annotateStatusTagType(status) {
        if (status === t('annotateStatusProcessing') || status === t('annotateStatusReviewing')) return 'warning';
        if (status === t('annotateStatusPending')) return 'primary';
        if (status === t('annotateStatusSubmitted') || status === t('annotateStatusUnreviewed')) return 'info';
        if (status === t('annotateReviewApproved') || status === t('annotateReviewPassedLabel')) return 'success';
        if (status === t('annotateReviewRejected') || status === t('annotateReviewFailedLabel')) return 'danger';
        return 'info';
      }
      function openImportDetail(item) {
        if (!item) return;
        return openDataPackageDrawer({ ...item, id: item.data_package_id || item.id });
      }
      function hasImportGovernance(row) {
        const item = overviewImportStageRow(row);
        return ['integrity', 'quality', 'desensitize'].some((stage) => overviewImportStageEnabled(item, stage));
      }
      function overviewImportStageCounts(group, stage) {
        const directSummary = governanceBackendSummary(group, stage);
        if (directSummary) return directSummary.counts;
        const reportedRow = (group?.rows || []).find((row) => governanceBackendStage(row, stage));
        const reportedSummary = governanceBackendSummary(reportedRow, stage);
        if (reportedSummary) return reportedSummary.counts;
        const counts = { passed: 0, failed: 0, error: 0 };
        (group.rows || []).forEach((row) => {
          const status = overviewImportStage(row, stage).status;
          if (status === 'succeeded') counts.passed += 1;
          else if (status === 'failed') counts.failed += 1;
          else if (status === 'error') counts.error += 1;
        });
        return counts;
      }
      function overviewImportAnnotateCounts(group) {
        const counts = { unclaimed: 0, annotated: 0, un: 0, approved: 0, rejected: 0 };
        (group.rows || []).forEach((row) => {
          const status = row.annotation_status || (row.annotation ? t('annotateStatusPending') : '');
          if (status === t('annotateStatusPending')) counts.unclaimed += 1;
          if (status === t('annotateStatusProcessing')) counts.annotated += 1;
          else if (status === t('annotateReviewApproved')) counts.approved += 1;
          else if (status === t('annotateReviewRejected')) counts.rejected += 1;
          else counts.un += 1;
        });
        return counts;
      }
      async function rerunOverviewStage(item, stage) {
        const workspaceId = selectedWorkspaceId.value;
        const batchId = Number(item?.batch_id);
        if (!workspaceId || !batchId || !hasPermission('workspace:write')) return;
        const backendStage = stage === 'desensitize' ? 'compliance' : stage;
        const key = `${workspaceId}:${batchId}:${backendStage}`;
        if (overviewStageRerunOverrides[key] === 'running') return;
        overviewStageRerunOverrides[key] = 'running';
        try {
          const result = await QuicDataAPI.retryDataBatchGovernance(batchId, { workspace_id: workspaceId, stage: backendStage });
          if (workspaceId !== selectedWorkspaceId.value) return;
          await loadDataBatches();
          if (workspaceId !== selectedWorkspaceId.value) return;
          if (result?.status === 'passed') ElMessage.success(t('stageRerunDone'));
          else ElMessage.warning(result?.error_message || t('governanceFailed'));
        } catch (error) {
          if (workspaceId === selectedWorkspaceId.value) errorMessage(error);
        } finally {
          delete overviewStageRerunOverrides[key];
        }
      }
      function stageItemCheckCounts(row, stage) {
        const st = overviewImportStage(row, stage);
        if (st.counts) return st.counts;
        const parts = String(st.text || '0/0').split('/');
        const done = parseInt(parts[0], 10) || 0;
        const total = parseInt(parts[1], 10) || 0;
        if (st.status === 'succeeded') return { passed: total, failed: 0, error: 0 };
        if (st.status === 'failed') return { passed: Math.max(0, done - 2), failed: 2, error: 0 };
        if (st.status === 'error') return { passed: Math.max(0, done - 1), failed: 0, error: 1 };
        return { passed: 0, failed: 0, error: 0 };
      }
      function annotationItemProgress(row) {
        const progress = row?.annotation_progress;
        const keys = ['annotated', 'un', 'approved'];
        if (!progress || !keys.every((key) => Number.isInteger(progress[key]) && progress[key] >= 0)) return null;
        return progress;
      }
      function importGovernanceStatusLabel(row) {
        const stages = ['integrity', 'quality', 'desensitize'];
        const enabled = stages.filter((stage) => overviewImportStageEnabled(row, stage));
        if (!enabled.length) return { label: t('governanceNotEnabled'), type: 'info' };
        if (enabled.some((stage) => overviewImportStage(row, stage).status === 'failed')) return { label: t('governanceFailed'), type: 'danger' };
        if (enabled.every((stage) => overviewImportStage(row, stage).status === 'succeeded')) return { label: t('governanceCompleted'), type: 'success' };
        return { label: t('governanceIncomplete'), type: 'warning' };
      }
      const governanceReportVisible = ref(false);
      const governanceReportRow = ref(null);
      const governanceExpandedStages = ref([]);
      async function openGovernanceReport(item) {
        governanceReportRow.value = item;
        governanceExpandedStages.value = [];
        governanceReportVisible.value = true;
        if (demoMode.value || !item?.batch_id || !selectedWorkspaceId.value) return;
        try {
          const report = await QuicDataAPI.getDataBatchGovernanceReport(item.batch_id, selectedWorkspaceId.value);
          if (!report) return;
          item.governance_report = report;
          const batch = builtImportBatches.value.find((group) => String(group.id) === String(item.batch_id));
          if (batch) {
            batch.governance_report = report;
            (batch.rows || []).forEach((row) => { row.governance_report = report; });
          }
        } catch {
          // Keep the last loaded report visible; a transient refresh failure must not replace it with mock data.
        }
      }
function governanceReportEntries(row) {
        if (!row) return [];
        if (Array.isArray(row.rows)) return row.rows;
        return [row];
      }
      function governanceStageLabel(stage) {
        return t(stage === 'integrity' ? 'stageIntegrity' : stage === 'quality' ? 'stageQuality' : 'stageDesensitize');
      }
      function governanceStageSummary(row, stage) {
        const backendSummary = governanceBackendSummary(row, stage);
        if (backendSummary) return backendSummary;
        const items = governanceReportEntries(row);
        const counts = { passed: 0, failed: 0, error: 0 };
        const failedItems = [];
        const errorItems = [];
        items.forEach((item) => {
          const st = overviewImportStage(item, stage).status;
          if (st === 'succeeded') counts.passed += 1;
          else if (st === 'failed') { counts.failed += 1; failedItems.push({ videoId: item.video_id || item.id, name: item.name, reason: governanceFailureReason(item, stage) }); }
          else if (st === 'error') { counts.error += 1; errorItems.push({ videoId: item.video_id || item.id, name: item.name, reason: governanceFailureReason(item, stage) }); }
        });
        return { counts, failedItems, errorItems };
      }

      function governanceBackendSummary(row, stage) {
        const stageRow = governanceBackendStage(row, stage);
        if (!stageRow) return null;
        const counts = governanceBackendStageCounts(row, stage, stageRow);
        const issues = governanceBackendIssueItems(row, stage, stageRow);
        const failedItems = [...issues];
        const errorItems = [];
        if (String(stageRow.status) === 'failed' && !failedItems.length && stageRow.error_message) {
          failedItems.push({
            videoId: row?.id,
            name: row?.name || '—',
            reason: String(stageRow.error_message),
          });
        }
        if (String(stageRow.status) === 'error') {
          errorItems.push({
            videoId: row?.id,
            name: row?.name || '—',
            reason: String(stageRow.error_message || 'governance stage error'),
          });
        }
        return { counts, failedItems, errorItems };
      }

      function governanceFailureReason(row, stage) {
        const stageRow = governanceBackendStage(row, stage);
        const backendIssue = governanceBackendIssueItems(row, stage, stageRow)[0];
        if (backendIssue?.reason) return backendIssue.reason;
        if (stageRow?.error_message) return String(stageRow.error_message);
        const reasons = {
          integrity: t('qualityMetadataInvalid'),
          quality: t('qualityReferenceTopicMissing'),
          desensitize: t('qualityRawSourceUnavailable'),
        };
        return reasons[stage] || t('qualityFailureGeneric');
      }
      const annotationPageTab = ref('data');
      const annotationFilterState = reactive({ batch: '', project: '', task: '', modality: '', collector: '', device: '', status: '', desc: [], range: null });
      const annotationTaskRows = computed(() => [...overviewImportGroups.value, ...builtImportBatches.value].flatMap((group) => (group.rows || []).map((row) => ({ ...row, batch_name: group.name }))));
      function importVideoCount(row) {
        if (!row) return 0;
        if (row.video_count != null) return row.video_count;
        return Math.max(1, Math.round((Number(row.duration_s) || 60) / 45));
      }
      const annotationTaskOptions = computed(() => ({
        batches: [...new Set(annotationTaskRows.value.map((r) => r.batch_name))],
        projects: [...new Set(annotationTaskRows.value.map((r) => r.project_name).filter(Boolean))],
        tasks: [...new Set(annotationTaskRows.value.map((r) => r.task_name).filter(Boolean))],
        modalities: [...new Set(annotationTaskRows.value.map((r) => r.modality).filter(Boolean))],
        collectors: [...new Set(annotationTaskRows.value.map((r) => r.collector_name).filter(Boolean))],
        devices: [...new Set(annotationTaskRows.value.map((r) => r.device?.name).filter(Boolean))],
        statuses: [t('annotateStatusPending'), t('annotateStatusProcessing')],
        descs: [...new Set(annotationTaskRows.value.flatMap((r) => r.annotation_descs || []))],
      }));
      const annotationFiltersActive = computed(() => Object.entries(annotationFilterState).some(([key, value]) => (key === 'range' ? Array.isArray(value) && value.length === 2 : Boolean(value))));
      function clearAnnotationFilters() {
        Object.assign(annotationFilterState, { batch: '', project: '', task: '', modality: '', collector: '', device: '', status: '', desc: [], range: null });
      }
      const datasetFilterState = reactive({ batch: '', project: [], task: [], modality: [], collector: [], device: [], governance: [], status: [], desc: [], range: null });
      const datasetFiltersActive = computed(() => Object.entries(datasetFilterState).some(([key, value]) => (key === 'range' ? Array.isArray(value) && value.length === 2 : Array.isArray(value) ? value.length > 0 : Boolean(value))));
      function clearDatasetFilters() {
        Object.assign(datasetFilterState, { batch: '', project: [], task: [], modality: [], collector: [], device: [], governance: [], status: [], desc: [], range: null });
      }
      function matchDatasetFilters(row) {
        if (datasetFilterState.batch && row.batch_name !== datasetFilterState.batch) return false;
        if (datasetFilterState.project.length && !datasetFilterState.project.includes(row.project_name)) return false;
        if (datasetFilterState.task.length && !datasetFilterState.task.includes(row.task_name)) return false;
        if (datasetFilterState.modality.length && !datasetFilterState.modality.includes(row.modality)) return false;
        if (datasetFilterState.collector.length && !datasetFilterState.collector.includes(row.collector_name)) return false;
        if (datasetFilterState.device.length && !datasetFilterState.device.includes(row.device?.name)) return false;
        const status = row.annotation_status || (row.annotation ? t('annotateStatusPending') : '');
        if (datasetFilterState.governance.length && !datasetFilterState.governance.includes(importGovernanceStatusLabel(row).label)) return false;
        if (datasetFilterState.status.length && !datasetFilterState.status.includes(status)) return false;
        if (datasetFilterState.desc.length && !datasetFilterState.desc.some((d) => (row.annotation_descs || []).includes(d))) return false;
        if (Array.isArray(datasetFilterState.range) && datasetFilterState.range.length === 2) {
          const ts = new Date(String(row.updated_at || '').replace(' ', 'T')).getTime();
          const from = new Date(datasetFilterState.range[0]).getTime();
          const to = new Date(datasetFilterState.range[1]).getTime();
          if (Number.isNaN(ts) || ts < from || ts > to) return false;
        }
        return true;
      }
      function matchAnnotationFilters(row) {
                if (annotationFilterState.batch && row.batch_name !== annotationFilterState.batch) return false;
        if (annotationFilterState.project && row.project_name !== annotationFilterState.project) return false;
        if (annotationFilterState.task && row.task_name !== annotationFilterState.task) return false;
        if (annotationFilterState.modality && row.modality !== annotationFilterState.modality) return false;
        if (annotationFilterState.collector && row.collector_name !== annotationFilterState.collector) return false;
        if (annotationFilterState.device && row.device?.name !== annotationFilterState.device) return false;
        const status = row.annotation_status || (row.annotation ? t('annotateStatusPending') : '');
        if (annotationFilterState.status && status !== annotationFilterState.status) return false;
        if (Array.isArray(annotationFilterState.desc) && annotationFilterState.desc.length && !annotationFilterState.desc.some((d) => (row.annotation_descs || []).includes(d))) return false;
        if (Array.isArray(annotationFilterState.range) && annotationFilterState.range.length === 2) {
          const ts = new Date(String(row.updated_at || '').replace(' ', 'T')).getTime();
          const from = new Date(annotationFilterState.range[0]).getTime();
          const to = new Date(annotationFilterState.range[1]).getTime();
          if (Number.isNaN(ts) || ts < from || ts > to) return false;
        }
        return true;
      }
      const annotationTaskRowsFiltered = computed(() => annotationTaskRows.value.filter((row) => matchDatasetFilters(row)));
      function batchAllowedInAssets(group) {
        const rows = group.rows || [];
        if (!rows.length) return true;
        const governanceKeys = rows.map((row) => overviewGovernanceStatus({ item: row }).key);
        const governanceOk = governanceKeys.every((key) => key === 'notstarted') || governanceKeys.every((key) => key === 'done');
        const annotationNotStarted = rows.every((row) => !row.annotation && !row.annotation_status);
        const approvedLabels = [t('annotateReviewApproved'), t('annotateReviewPassedLabel')];
        const annotationApproved = rows.some((row) => row.annotation) && rows.every((row) => !row.annotation || approvedLabels.includes(row.annotation_status || ''));
        return governanceOk || annotationNotStarted || annotationApproved;
      }
      const assetBatchRows = computed(() => [...overviewImportGroups.value, ...builtImportBatches.value]
        .filter((group) => batchAllowedInAssets(group))
        .flatMap((group) => (group.rows || []).map((row) => ({ ...row, batch_name: group.name }))));
      const assetListRowsFiltered = computed(() => assetBatchRows.value.filter((row) => matchDatasetFilters(row)));
      function toggleOverviewImportGroup(group) {
        const key = String(group.id);
        const next = new Set(overviewImportExpandedGroupIds.value);
        if (next.has(key)) next.delete(key); else next.add(key);
        overviewImportExpandedGroupIds.value = next;
      }
      function overviewImportRowClass({ row }) {
        return row.row_kind === 'source' ? 'import-group-source-row' : 'import-group-item-row';
      }

      const builtImportBatches = ref([]);
      const overviewBuildMode = ref(false);
      const overviewBuildSelection = ref(new Set());
      const overviewBuildDialogVisible = ref(false);
      let overviewBuildWorkspaceId = 0;
      let overviewBuildWriteGeneration = 0;
      const collectionLabels = ref([]);
      const annotationWorkItems = ref([]);
      const reviewWorkItems = ref([]);
      // Annotation and review are not workspace-scoped: '' lists every workspace,
      // an id narrows the list. Options come from the workspaces of visible items.
      const workQueueWorkspaceId = ref('');
      const workQueueWorkspaceOptions = ref([]);
      const reassignDialog = reactive({
        visible: false,
        kind: 'annotation',
        itemId: null,
        workspaceId: null,
        to_user_id: null,
        reason: '',
      });
      const pendingBuildPackageIds = ref([]);
      const dataOverviewRefreshKey = ref(0);
      const intakeReviewWorkbenchRef = ref(null);
      const packageAnnotationWorkbenchRef = ref(null);
      const packageWorkbenchRoute = reactive({
        workItemId: initialQueryNumber('package-workbench', 'item_id', null),
        workspaceId: initialQueryNumber('package-workbench', 'workspace_id', null),
        mode: initialQueryValue('package-workbench', 'mode') === 'review' ? 'review' : 'annotation',
      });
      function packageWorkbenchHash() {
        return `#/package-workbench?item_id=${packageWorkbenchRoute.workItemId}&workspace_id=${packageWorkbenchRoute.workspaceId}&mode=${packageWorkbenchRoute.mode}`;
      }
      async function openPackageWorkbench(item) {
        if (!Number.isSafeInteger(Number(item?.id)) || !Number.isSafeInteger(Number(item?.workspace_id))) return;
        packageWorkbenchRoute.workItemId = Number(item.id);
        packageWorkbenchRoute.workspaceId = Number(item.workspace_id);
        packageWorkbenchRoute.mode = item.queue_kind === 'review' ? 'review' : 'annotation';
        window.location.hash = packageWorkbenchHash();
      }
      function backFromPackageWorkbench() {
        return navigate('work-queue', { queueStageOverride: packageWorkbenchRoute.mode });
      }
      function applyPackageWorkbenchRoute(route) {
        const workspaceId = Number(route.query.workspace_id);
        const workItemId = Number(route.query.item_id);
        // The work item's own workspace locates it; membership is not required.
        if (!Number.isSafeInteger(workItemId) || workItemId <= 0 || !Number.isSafeInteger(workspaceId) || workspaceId <= 0) {
          setAuthorizedView('work-queue', { warn: true });
          ElMessage.warning(t('accessRedirected'));
          return false;
        }
        packageWorkbenchRoute.workItemId = workItemId;
        packageWorkbenchRoute.workspaceId = workspaceId;
        packageWorkbenchRoute.mode = route.query.mode === 'review' ? 'review' : 'annotation';
        return true;
      }
      const overviewBuildForm = reactive({
        name: '',
        governance: { integrity: false, quality: false, compliance: false },
        annotation: false,
        annotators: [],
        reviewMode: 'single',
        reviewers: [],
        sceneLabelIds: [],
        purposeLabelIds: [],
        modalityLabelIds: [],
        trainLabelIds: [],
      });
      const sceneLabelOptions = computed(() => collectionLabels.value.filter((item) => item.category === 'scene' && item.is_active !== false));
      const purposeLabelOptions = computed(() => collectionLabels.value.filter((item) => item.category === 'purpose' && item.is_active !== false));
      const modalityLabelOptions = computed(() => collectionLabels.value.filter((item) => item.category === 'modality' && item.is_active !== false));
      const trainLabelOptions = computed(() => collectionLabels.value.filter((item) => item.category === 'training' && item.is_active !== false));
      // The data-batch API validates these assignments by role.  Keep the
      // selectors aligned with that contract instead of treating an admin as
      // an implicit annotator or reviewer.
      // Annotators and reviewers may work in any workspace, so candidates are not membership-filtered.
      const annotatorUserOptions = computed(() => (managedUsers.value || []).filter((user) => user.role === 'annotator' && user.is_active !== false));
      const reviewerUserOptions = computed(() => (managedUsers.value || []).filter((user) => user.role === 'auditor' && user.is_active !== false));
      function viewRequiresWorkspace(view) {
        return !['work-queue', 'package-workbench', 'assets', 'datasets', 'admin', 'trainDash', 'trainJobs', 'trainNew', 'trainDatasets', 'trainModels', 'trainResources', 'trainSystem'].includes(view);
      }
      function scopeWorkspaceRequired(view) {
        return viewRequiresWorkspace(view);
      }
      function stageSnapshotLabel(snapshot) {
        if (!snapshot || typeof snapshot !== 'object') return '—';
        return Object.entries(snapshot).map(([stage, value]) => `${stage}:${value?.status || value || '—'}`).join(' · ') || '—';
      }
      function assigneeLabel(userId) {
        const id = Number(userId);
        if (!Number.isInteger(id) || id <= 0) return '—';
        const user = (managedUsers.value || []).find((item) => Number(item.id) === id)
          || (workspaceMembers.value || []).find((item) => Number(item.user_id) === id);
        return user?.email || user?.display_name || user?.name || `#${id}`;
      }

      function buildIntakeTreeRows(groups, prefix) {
        const rows = [];
        (groups || []).forEach((group) => {
          (group.rows || []).forEach((item) => {
            rows.push({ row_kind: 'item', row_key: `${prefix}-item-${String(group.id)}-${String(item.id)}`, group, item });
          });
        });
        return rows;
      }
      const allBatchesTreeRows = computed(() => buildIntakeTreeRows(builtImportBatches.value, 'all'));
      const batchTagFilterState = reactive({ scene: [], purpose: [], train: [], modality: [], integrity: [], quality: [], desensitize: [], annotateStatus: [], uploadRange: null });
      const batchTagFilterActive = computed(() => Object.entries(batchTagFilterState).some(([key, value]) => (key === 'uploadRange' ? Array.isArray(value) && value.length === 2 && value[0] && value[1] : Array.isArray(value) ? value.length > 0 : Boolean(value))));
      function clearBatchTagFilters() {
        Object.assign(batchTagFilterState, { scene: [], purpose: [], train: [], modality: [], integrity: [], quality: [], desensitize: [], annotateStatus: [], uploadRange: null });
      }
      const batchStageStatusOptions = computed(() => {
        const stages = { integrity: new Set(), quality: new Set(), desensitize: new Set() };
        allBatchesTreeRows.value.forEach((row) => {
          if (row.row_kind !== 'item') return;
          Object.keys(stages).forEach((stage) => stages[stage].add(miningStageStatusLabel(miningStageAggregatedStatus(row.item, stage))));
        });
        return { integrity: [...stages.integrity], quality: [...stages.quality], desensitize: [...stages.desensitize] };
      });
      const batchAnnotateStatusOptions = computed(() => {
        const set = new Set();
        allBatchesTreeRows.value.forEach((row) => {
          if (row.row_kind === 'item') set.add(overviewImportAnnotateStatus(row.item) || t('stageDisabled'));
        });
        return [...set];
      });
      function overviewGovernanceStatus(row) {
        const item = row.item || row;
        const stages = ['integrity', 'quality', 'desensitize'];
        const enabled = stages.filter((stage) => overviewImportStageEnabled(item, stage));
        if (!enabled.length) return { key: 'notstarted', label: t('governanceNotStarted'), type: 'info' };
        const statuses = enabled.map((stage) => miningStageAggregatedStatus(item, stage));
        if (statuses.some((st) => ['failed', 'rejected', 'error'].includes(st))) return { key: 'failed', label: t('governanceStatusFailed'), type: 'danger' };
        if (statuses.every((st) => st === 'succeeded')) return { key: 'done', label: t('governanceCompleted'), type: 'success' };
        return { key: 'running', label: t('governanceStatusRunning'), type: 'warning' };
      }
      function overviewBatchTagValue(row, kind) {
        if (!row) return '';
        const groupTags = row.source_group?.tags || row.group?.tags;
        if (groupTags && groupTags[kind]) return groupTags[kind];
        return overviewImportTaskInfo(row)[kind] || '';
      }
      function matchBatchTagFilters(row) {
        const check = (kind, values) => {
          const raw = String(overviewBatchTagValue(row, kind));
          const parts = raw ? raw.split('、') : [];
          return values.some((v) => parts.includes(v));
        };
        if (batchTagFilterState.scene.length && !check('scene', batchTagFilterState.scene)) return false;
        if (batchTagFilterState.purpose.length && !check('purpose', batchTagFilterState.purpose)) return false;
        if (batchTagFilterState.train.length && !check('train', batchTagFilterState.train)) return false;
        if (batchTagFilterState.modality.length && !batchTagFilterState.modality.includes(row.item?.modality || row.item?.batch_type || row.group?.modality)) return false;
        if (batchTagFilterState.integrity.length && !batchTagFilterState.integrity.includes(miningStageStatusLabel(miningStageAggregatedStatus(row.item, 'integrity')))) return false;
        if (batchTagFilterState.quality.length && !batchTagFilterState.quality.includes(miningStageStatusLabel(miningStageAggregatedStatus(row.item, 'quality')))) return false;
        if (batchTagFilterState.desensitize.length && !batchTagFilterState.desensitize.includes(miningStageStatusLabel(miningStageAggregatedStatus(row.item, 'desensitize')))) return false;
        if (batchTagFilterState.annotateStatus.length && !batchTagFilterState.annotateStatus.includes(overviewImportAnnotateStatus(row.item) || t('stageDisabled'))) return false;
        if (Array.isArray(batchTagFilterState.uploadRange) && batchTagFilterState.uploadRange.length === 2 && batchTagFilterState.uploadRange[0] && batchTagFilterState.uploadRange[1]) {
          const day = String(row.item?.updated_at || '').slice(0, 10);
          const startDay = String(batchTagFilterState.uploadRange[0]).slice(0, 10);
          const endDay = String(batchTagFilterState.uploadRange[1]).slice(0, 10);
          if (!day || day < startDay || day > endDay) return false;
        }
        return true;
      }
      const intakeTreeRows = computed(() => {
        const rows = intakeTab.value === 'imports' ? allBatchesTreeRows.value : overviewImportTreeRows.value;
        if (intakeTab.value === 'imports' && batchTagFilterActive.value) return rows.filter((row) => matchBatchTagFilters(row));
        return rows;
      });
      const intakeBatchGroupRows = computed(() => builtImportBatches.value.map((g) => ({ row_kind: 'source', row_key: `all-${String(g.id)}`, group: g })));
      const intakeImportCount = computed(() => (intakeTab.value === 'imports'
        ? intakeBatchGroupRows.value.reduce((sum, g) => sum + (g.group.rows || []).length, 0)
        : intakeTreeRows.value.filter((r) => r.row_kind === 'item').length));

      function overviewBuildPrefix() {
        return intakeTab.value === 'imports' ? 'all' : '';
      }
      function overviewBuildItemKeys(group, prefix) {
        const keyPrefix = prefix ? `${prefix}-` : '';
        return (group.rows || []).map((item) => `${keyPrefix}item-${String(group.id)}-${String(item.id)}`);
      }
      function isOverviewBuildGroupSelected(group, prefix) {
        const keys = overviewBuildItemKeys(group, prefix);
        return keys.length > 0 && keys.every((k) => overviewBuildSelection.value.has(k));
      }
      function isOverviewBuildGroupIndeterminate(group, prefix) {
        const keys = overviewBuildItemKeys(group, prefix);
        const selected = keys.filter((k) => overviewBuildSelection.value.has(k)).length;
        return selected > 0 && selected < keys.length;
      }
      function toggleOverviewBuildGroup(group, prefix) {
        const keys = overviewBuildItemKeys(group, prefix);
        const allSelected = isOverviewBuildGroupSelected(group, prefix);
        const next = new Set(overviewBuildSelection.value);
        keys.forEach((k) => { if (allSelected) next.delete(k); else next.add(k); });
        overviewBuildSelection.value = next;
      }
      function isOverviewBuildSelected(row) {
        return overviewBuildSelection.value.has(row.row_key);
      }
      function toggleOverviewBuildSelected(row) {
        const next = new Set(overviewBuildSelection.value);
        if (next.has(row.row_key)) next.delete(row.row_key); else next.add(row.row_key);
        overviewBuildSelection.value = next;
      }
      const overviewBuildSelectedInfo = ref({ scenes: [], purposes: [], modalities: [], trains: [] });
      function computeOverviewBuildSelectedInfo() {
        const info = { scenes: new Set(), purposes: new Set(), modalities: new Set(), trains: new Set() };
        const addGroup = (group, prefix) => {
          const keyPrefix = prefix ? `${prefix}-` : '';
          (group.rows || []).forEach((item) => {
            const key = `${keyPrefix}item-${String(group.id)}-${String(item.id)}`;
            if (!overviewBuildSelection.value.has(key)) return;
            const taskInfo = overviewImportTaskInfo(item);
            if (taskInfo.scene) info.scenes.add(taskInfo.scene);
            if (taskInfo.purpose) info.purposes.add(taskInfo.purpose);
            if (taskInfo.train) info.trains.add(taskInfo.train);
            const modality = item.modality || item.batch_type;
            if (modality) info.modalities.add(String(modality));
          });
        };
        overviewImportGroups.value.forEach((group) => addGroup(group, ''));
        builtImportBatches.value.forEach((group) => addGroup(group, 'all'));
        overviewBuildSelectedInfo.value = { scenes: [...info.scenes], purposes: [...info.purposes], modalities: [...info.modalities], trains: [...info.trains] };
      }
      function enterOverviewBuildMode() {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId) {
          ElMessage.warning(t('noWorkspace'));
          return;
        }
        overviewBuildWriteGeneration += 1;
        overviewBuildWorkspaceId = workspaceId;
        overviewBuildMode.value = true;
        loadDataBatchCandidates();
        const preselectKeys = new Set();
        const prefix = overviewBuildPrefix();
        overviewImportGroups.value.forEach((group) => {
          overviewBuildItemKeys(group, prefix).forEach((key) => preselectKeys.add(key));
        });
        overviewBuildSelection.value = preselectKeys;
        overviewBuildForm.name = '';
        overviewBuildForm.governance = { integrity: false, quality: false, compliance: false };
        overviewBuildForm.annotation = false;
        overviewBuildForm.annotators = [];
        overviewBuildForm.reviewMode = 'single';
        overviewBuildForm.reviewers = [];
        overviewImportExpandedGroupIds.value = new Set(overviewImportGroups.value.map((g) => String(g.id)));
      }
      function exitOverviewBuildMode() {
        overviewBuildWriteGeneration += 1;
        overviewBuildWorkspaceId = 0;
        overviewBuildMode.value = false;
        overviewBuildSelection.value = new Set();
        overviewBuildDialogVisible.value = false;
        pendingBuildPackageIds.value = [];
        saving.value = false;
      }

      function onOverviewBuildDialogClosed() {
        if (overviewBuildDialogVisible.value) return;
        exitOverviewBuildMode();
      }
      function openPackageBatchDialog(ids) {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId) {
          ElMessage.warning(t('noWorkspace'));
          return;
        }
        overviewBuildWriteGeneration += 1;
        overviewBuildWorkspaceId = workspaceId;
        overviewBuildMode.value = true;
        pendingBuildPackageIds.value = [...new Set(ids.map(Number).filter(id => Number.isSafeInteger(id) && id > 0))];
        openOverviewBuildDialog();
      }
      function openOverviewBuildDialog() {
        if (!pendingBuildPackageIds.value.length && !overviewBuildSelection.value.size) {
          ElMessage.warning(t('selectDataFirst'));
          return;
        }
        if (!pendingBuildPackageIds.value.length) computeOverviewBuildSelectedInfo();
        overviewBuildForm.name = '';
        overviewBuildForm.governance = { integrity: false, quality: false, compliance: false };
        overviewBuildForm.annotation = false;
        overviewBuildForm.annotators = [];
        overviewBuildForm.reviewMode = 'single';
        overviewBuildForm.reviewers = [];
        overviewBuildForm.sceneLabelIds = [];
        overviewBuildForm.purposeLabelIds = [];
        overviewBuildForm.modalityLabelIds = [];
        overviewBuildForm.trainLabelIds = [];
        loadCollectionLabels();
        if (!managedUsers.value.length) void loadAdmin();
        else void loadWorkspaceMembers();
        overviewBuildDialogVisible.value = true;
      }
      async function submitOverviewBuild() {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const dialogWorkspaceId = Number(overviewBuildWorkspaceId) || 0;
        const name = overviewBuildForm.name.trim();
        if (!name) {
          ElMessage.warning(t('buildBatchNameRequired'));
          return;
        }
        if (!workspaceId) {
          ElMessage.warning(t('noWorkspace'));
          return;
        }
        if (!overviewBuildMode.value || !dialogWorkspaceId || workspaceId !== dialogWorkspaceId) {
          exitOverviewBuildMode();
          ElMessage.warning(t('scopeChanged'));
          return;
        }
        const uniquePackageIds = [...pendingBuildPackageIds.value];
        if (!uniquePackageIds.length) {
          ElMessage.warning(t('selectDataFirst'));
          return;
        }
        const annotation = Boolean(overviewBuildForm.annotation);
        if (annotation && (!overviewBuildForm.annotators.length || !overviewBuildForm.reviewers.length)) {
          ElMessage.warning(locale.value === 'en-US' ? 'Select annotators and a reviewer.' : '启用标注流程时，请选择标注员和审核员');
          return;
        }
        const annotatorIds = overviewBuildForm.annotators
          .map(Number)
          .filter((id) => annotatorUserOptions.value.some((user) => Number(user.id) === id));
        const reviewerIds = overviewBuildForm.reviewers
          .map(Number)
          .filter((id) => reviewerUserOptions.value.some((user) => Number(user.id) === id));
        if (annotation && (annotatorIds.length !== overviewBuildForm.annotators.length || reviewerIds.length !== overviewBuildForm.reviewers.length)) {
          ElMessage.warning(locale.value === 'en-US' ? 'Select an active annotator and auditor.' : '请选择有效的标注员和审核员');
          return;
        }
        if (annotation && overviewBuildForm.reviewMode === 'single' && overviewBuildForm.reviewers.length !== 1) {
          ElMessage.warning(locale.value === 'en-US' ? 'Select exactly one reviewer.' : '单审模式只能选择一名审核员');
          return;
        }
        const label_ids = [
          ...overviewBuildForm.sceneLabelIds,
          ...overviewBuildForm.purposeLabelIds,
          ...overviewBuildForm.modalityLabelIds,
          ...overviewBuildForm.trainLabelIds,
        ].map((id) => Number(id)).filter((id) => Number.isInteger(id) && id > 0);
        const requestGeneration = ++overviewBuildWriteGeneration;
        const isCurrent = () => requestGeneration === overviewBuildWriteGeneration
          && overviewBuildMode.value
          && Number(overviewBuildWorkspaceId) === workspaceId
          && Number(selectedWorkspaceId.value) === workspaceId;
        try {
          saving.value = true;
          await QuicDataAPI.createDataBatch({
            workspace_id: workspaceId,
            name,
            data_package_ids: uniquePackageIds,
            label_ids: [...new Set(label_ids)],
            integrity_check_enabled: Boolean(overviewBuildForm.governance.integrity),
            quality_check_enabled: Boolean(overviewBuildForm.governance.quality),
            compliance_check_enabled: Boolean(overviewBuildForm.governance.compliance),
            annotation_enabled: annotation,
            annotator_user_ids: annotation ? annotatorIds : [],
            reviewer_user_id: annotation ? reviewerIds[0] : null,
            review_mode: 'single',
          });
          if (!isCurrent()) return;
          ElMessage.success(t('buildBatchSuccess'));
          exitOverviewBuildMode();
          dataOverviewRefreshKey.value += 1;
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        } finally {
          if (requestGeneration === overviewBuildWriteGeneration) saving.value = false;
        }
      }
      async function loadDataBatchCandidates() {
        const generation = ++batchCandidatesGeneration;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const isCurrent = () => generation === batchCandidatesGeneration
          && workspaceId === (Number(selectedWorkspaceId.value) || 0);
        if (!workspaceId) {
          batchCandidates.value = [];
          return;
        }
        try {
          const labelIds = [];
          const labelNameFilters = [
            ['scene', overviewFilterState.scene],
            ['purpose', overviewFilterState.purpose],
            ['modality', overviewFilterState.modality],
            ['training', overviewFilterState.train],
          ];
          for (const [category, names] of labelNameFilters) {
            for (const name of names || []) {
              const match = collectionLabels.value.find(
                (label) => label.category === category && label.name === name && label.is_active !== false,
              );
              if (match?.id != null) labelIds.push(Number(match.id));
            }
          }
          const params = { workspace_id: workspaceId };
          if (labelIds.length) params.label_ids = [...new Set(labelIds)];
          const data = await QuicDataAPI.listDataBatchCandidates(params);
          if (isCurrent()) batchCandidates.value = Array.isArray(data?.items) ? data.items : [];
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        }
      }
      async function loadDataBatches() {
        if (!selectedWorkspaceId.value) {
          builtImportBatches.value = [];
          return;
        }
        try {
          const data = await QuicDataAPI.listDataBatches({ workspace_id: selectedWorkspaceId.value });
          const items = Array.isArray(data?.items) ? data.items : [];
          builtImportBatches.value = await Promise.all(items.map(async (batch) => {
            let governanceReport = null;
            if (!demoMode.value && batch.id != null) {
              try {
                governanceReport = await QuicDataAPI.getDataBatchGovernanceReport(batch.id, selectedWorkspaceId.value);
              } catch {
                // A missing or temporarily unavailable report leaves the real stage in queued state.
              }
            }
            const governance = {
              integrity: Boolean(batch.integrity_check_enabled),
              quality: Boolean(batch.quality_check_enabled),
              desensitize: Boolean(batch.compliance_check_enabled),
              compliance: Boolean(batch.compliance_check_enabled),
            };
            const governanceTotal = Math.max(1, Number(batch.episode_count) || (batch.data_package_ids || []).length || 1);
            const rows = (batch.data_package_ids || []).map((packageId) => ({
              id: packageId,
              batch_id: batch.id,
              data_package_id: packageId,
              name: `package-${packageId}`,
              package_uid: `package-${packageId}`,
              project_name: '',
              task_name: '',
              owner: '',
              scene: '',
              purpose: '',
              train: '',
              channel: 'data_package',
              modality: '',
              collector_name: '',
              device: { name: '—', serial: '—', version: '—' },
              reference_frame_count: 0,
              duration_s: 0,
              total_frames: 0,
              updated_at: batch.updated_at,
              governance,
              governance_report: governanceReport,
              governance_source: governanceReport ? 'backend' : 'backend_unavailable',
              governance_total: governanceTotal,
              annotation_status: '',
              annotation_descs: [],
              target_duration_s: 0,
              collected_duration_s: 0,
              valid_duration_s: 0,
              status: batch.status,
            }));
            return {
              id: batch.id,
              name: batch.name || `batch-${batch.id}`,
              created_at: batch.created_at,
              governance_report: governanceReport,
              governance_source: governanceReport ? 'backend' : 'backend_unavailable',
              governance_total: governanceTotal,
              rows,
              task_name: '',
              modality: '',
              collector_name: '',
              device: { name: '', serial: '', version: '' },
              channel: '',
            };
          }));
        } catch (error) {
          errorMessage(error);
        }
      }
      async function loadCollectionLabels() {
        if (!selectedWorkspaceId.value) {
          collectionLabels.value = [];
          return;
        }
        try {
          const data = await QuicDataAPI.listCollectionLabels({ workspace_id: selectedWorkspaceId.value });
          collectionLabels.value = Array.isArray(data?.items) ? data.items : [];
        } catch (error) {
          errorMessage(error);
        }
      }
      let loadAnnotationWorkItemsGeneration = 0;
      async function loadAnnotationWorkItems() {
        const generation = ++loadAnnotationWorkItemsGeneration, workspaceId = workQueueWorkspaceId.value;
        const page = queuePage.value, pageSize = queuePageSize.value, stage = queueStage.value;
        const isCurrent = () => generation === loadAnnotationWorkItemsGeneration && String(workspaceId) === String(workQueueWorkspaceId.value) && page === queuePage.value && pageSize === queuePageSize.value && stage === queueStage.value;
        annotationWorkItems.value = [];
        loading.queue = true;
        try {
          const params = { limit: pageSize, offset: (page - 1) * pageSize };
          if (workspaceId) params.workspace_id = Number(workspaceId);
          const data = await QuicDataAPI.listAnnotationWorkItems(params);
          if (isCurrent()) {
            annotationWorkItems.value = Array.isArray(data?.items) ? data.items : [];
            workQueueWorkspaceOptions.value = Array.isArray(data?.workspaces) ? data.workspaces : [];
            queueTotal.value = Number.isSafeInteger(data?.total) ? data.total : annotationWorkItems.value.length;
            if (page > 1 && !annotationWorkItems.value.length && queueTotal.value < (page - 1) * pageSize + 1) {
              queuePage.value = Math.max(1, Math.ceil(queueTotal.value / pageSize));
              await loadAnnotationWorkItems();
            }
          }
        } catch (error) { if (isCurrent()) errorMessage(error); }
        finally { if (isCurrent()) loading.queue = false; }
      }

      let loadReviewWorkItemsGeneration = 0;
      async function loadReviewWorkItems() {
        const generation = ++loadReviewWorkItemsGeneration, workspaceId = workQueueWorkspaceId.value;
        const page = queuePage.value, pageSize = queuePageSize.value, stage = queueStage.value;
        const isCurrent = () => generation === loadReviewWorkItemsGeneration && String(workspaceId) === String(workQueueWorkspaceId.value) && page === queuePage.value && pageSize === queuePageSize.value && stage === queueStage.value;
        reviewWorkItems.value = [];
        loading.queue = true;
        try {
          const params = { limit: pageSize, offset: (page - 1) * pageSize };
          if (workspaceId) params.workspace_id = Number(workspaceId);
          const data = await QuicDataAPI.listReviewWorkItems(params);
          if (isCurrent()) {
            reviewWorkItems.value = Array.isArray(data?.items) ? data.items : [];
            workQueueWorkspaceOptions.value = Array.isArray(data?.workspaces) ? data.workspaces : [];
            queueTotal.value = Number.isSafeInteger(data?.total) ? data.total : reviewWorkItems.value.length;
            if (page > 1 && !reviewWorkItems.value.length && queueTotal.value < (page - 1) * pageSize + 1) {
              queuePage.value = Math.max(1, Math.ceil(queueTotal.value / pageSize));
              await loadReviewWorkItems();
            }
          }
        } catch (error) { if (isCurrent()) errorMessage(error); }
        finally { if (isCurrent()) loading.queue = false; }
      }

      function openReassignDialog(kind, item) {
        reassignDialog.visible = true;
        reassignDialog.kind = kind;
        reassignDialog.itemId = item?.id || null;
        reassignDialog.workspaceId = item?.workspace_id || null;
        reassignDialog.to_user_id = null;
        reassignDialog.reason = '';
      }
      async function submitReassignDialog() {
        const itemId = Number(reassignDialog.itemId);
        const toUserId = Number(reassignDialog.to_user_id);
        const reason = String(reassignDialog.reason || '').trim();
        const workspaceId = Number(reassignDialog.workspaceId);
        if (!Number.isInteger(workspaceId) || !Number.isInteger(itemId) || !Number.isInteger(toUserId) || !reason) return;
        try {
          saving.value = true;
          const body = { workspace_id: workspaceId, to_user_id: toUserId, reason };
          if (reassignDialog.kind === 'review') await QuicDataAPI.reassignReviewWorkItem(itemId, body);
          else await QuicDataAPI.reassignAnnotationWorkItem(itemId, body);
          reassignDialog.visible = false;
          if (reassignDialog.kind === 'review') await loadReviewWorkItems();
          else await loadAnnotationWorkItems();
          ElMessage.success(t('actionSucceeded'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }
      const annotationQueueRows = computed(() => annotationWorkItems.value.map((item) => ({
        ...item,
        row_key: `ann-${item.id}`,
        queue_kind: 'annotation',
        return_reason: item.return_reason || item.reassign_reason || '',
      })));
      const reviewQueueRows = computed(() => reviewWorkItems.value.map((item) => ({
        ...item,
        row_key: `rev-${item.id}`,
        queue_kind: 'review',
        return_reason: item.reason || item.reassign_reason || '',
      })));
      const reviewFilterState = reactive({ source: '', lowConfidence: false, threshold: 0.7 });
      function reviewSourceKind(row) {
        const source = row?.source || null;
        if (source && source.kind === 'algorithm') return 'algorithm';
        if (source && source.kind === 'human') return 'human';
        return 'human';
      }
      function reviewRowConfidence(row) {
        const source = row?.source || null;
        return typeof source?.confidence === 'number' ? source.confidence : null;
      }
      const reviewFilteredRows = computed(() => {
        let rows = reviewQueueRows.value;
        if (reviewFilterState.source === 'algorithm') {
          rows = rows.filter((row) => reviewSourceKind(row) === 'algorithm');
        } else if (reviewFilterState.source === 'human') {
          rows = rows.filter((row) => reviewSourceKind(row) === 'human');
        }
        if (reviewFilterState.lowConfidence) {
          const threshold = Number(reviewFilterState.threshold) || 0.7;
          rows = rows.filter((row) => {
            const confidence = reviewRowConfidence(row);
            return confidence != null && confidence < threshold;
          });
        }
        return rows;
      });
      const governanceFilteredRows = computed(() => (
        queueStage.value === 'review' && demoMode.value ? reviewFilteredRows.value : governanceAnnotationQueueRows.value
      ));
      const reviewFilterActive = computed(() => (
        Boolean(reviewFilterState.source) || reviewFilterState.lowConfidence
      ));
      function clearReviewFilters() {
        Object.assign(reviewFilterState, { source: '', lowConfidence: false, threshold: 0.7 });
      }
      function reviewSourceTag(row) {
        const source = row?.source || null;
        if (source && source.kind === 'algorithm') {
          const confidence = typeof source.confidence === 'number' ? ` · ${Math.round(source.confidence * 100)}%` : '';
          return { label: `${source.name || t('reviewAlgorithmSource')}@${source.version || '?'}${confidence}`, type: 'warning' };
        }
        return { label: t('reviewHumanSource'), type: 'info' };
      }
      const governanceAnnotationQueueRows = computed(() => (
        queueStage.value === 'review' ? reviewQueueRows.value : annotationQueueRows.value
      ));
      function governanceLabelList(governance) {
        const labels = [];
        if (governance?.integrity) labels.push(t('stageIntegrity'));
        if (governance?.quality) labels.push(t('stageQuality'));
        if (governance?.compliance) labels.push(t('complianceCheck'));
        return labels;
      }

      const collectionConfigForm = reactive({ min_valid_rate: 0.9, batch_size: 100, edge_sync_mode: 'auto', desensitize_on_edge: true });
      const miningConfigTagInputs = reactive({ project: '', purpose: '', scene: '', train: '', software: '' });
      const miningCapacity = ref(null);
      const miningCollectionDash = ref(null);
      const miningEfficiency = ref(null);
      const cutVideoEl = ref(null);
      const cutPlayTime = ref(0);
      const miningRunningStage = ref('');
      const miningCutAudit = ref(null);
      const showMiningAssignModeDialog = ref(false);
      const miningAssignModeChoice = ref('task');
      const showMiningSplitChoiceDialog = ref(false);
      const miningSplitChoice = ref('auto');
      const miningSplitTargetBatchId = ref('');
      const showMiningTaskPackagesPage = ref(initialRoute.view === 'miningTasks' && Boolean(initialRoute.query.task_id));
      const miningTaskPackagesFocusMode = computed(() => (
        activeView.value === 'miningTasks' && showMiningTaskPackagesPage.value
      ));
      const loading = reactive({ workspaces: false, taskSets: false, batches: false, episodes: false, imports: false, activity: false, taskLabels: false, resources: false, queue: false, candidates: false, lerobotCandidates: false, lerobotSessions: false, lerobotScan: false, lerobotBatchDatasets: false, workbench: false, ai: false, users: false, members: false, settings: false, dashboard: false, miningPackages: false });
      const dashboardOverview = ref(null);
      const dashboardOverviewError = ref('');
      let dashboardOverviewGeneration = 0;
      const dashboardTodayQueues = computed(() => QuicDataDashboardState.todayQueuesFromFunnel(
        dashboardOverview.value,
        locale.value,
      ));
      const dashboardCharts = { funnel: null, trend: null, device: null };
      const funnelChartEl = ref(null);
      const trendChartEl = ref(null);
      const deviceChartEl = ref(null);
      const workspaces = ref([]);
      const taskSets = ref([]);
      const batches = ref([]);
      const batchImports = ref([]);
      const batchActivity = ref([]);
      const episodes = ref([]);
      const episodeTotal = ref(0);
      const episodePage = ref(initialQueryNumber('assets', 'page', 1));
      const episodePageSize = ref(initialQueryNumber('assets', 'page_size', 50));
      const queueRows = ref([]);
      const queueTotal = ref(0);
      const queuePage = ref(initialQueryNumber('work-queue', 'page', 1));
      const queuePageSize = ref(initialQueryNumber('work-queue', 'page_size', 50));
      const taskLabels = ref([]);
      const lerobotCandidates = ref([]);
      const nativeLerobotReauthorizationCandidate = ref('');
      const managedUsers = ref([]);
      const roleDefinitions = ref({});
      const workspaceMembers = ref([]);
      const managementWorkspaces = ref([]);
      const managementMembers = ref([]);
      const managementWorkspaceId = ref(null);
      const platformSettings = ref({ ai: null, oss_import_scopes: [] });
      const selectedWorkspaceId = ref(null);
      const settingsCenterError = computed(() => {
        const failures = settingsCenterLoadFailures.value || [];
        if (!failures.length) return '';
        const failedSections = failures.map((section) => t(section === 'projects' ? 'settingsProjects' : 'settingsLabels'));
        return `${t('settingsLoadFailed')} ${failedSections.join(locale.value === 'en-US' ? ', ' : '、')}`;
      });
      const settingsCenterWritable = computed(() => (
        canView('settings')
        && !loadingSettingsCenter.value
        && !settingsCenterError.value
        && Number(settingsCenterLoadedWorkspaceId.value) > 0
        && Number(settingsCenterLoadedWorkspaceId.value) === Number(selectedWorkspaceId.value)
      ));
      // Project creation is available from the shared scope header as well as
      // the settings center.  Keep the permission check based on the active
      // workspace, but do not make the menu item silently disabled while the
      // workspace list is still being hydrated.
      const collectionProjectCreateWritable = computed(() => {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!hasPermission('workspace:write') || workspaceId <= 0) return false;
        const workspace = workspaces.value.find((item) => Number(item.id) === workspaceId);
        return workspace ? workspace.collection_access !== false : true;
      });
      const membershipScopedViews = new Set([...COLLECTION_VIEWS, 'settings']);
      const availableScopeWorkspaces = computed(() => membershipScopedViews.has(activeView.value)
        ? workspaces.value.filter(item => item.collection_access !== false) : workspaces.value);
      const selectedTaskSetId = ref(null);
      // Collection project is the user-facing scope level now; the legacy task
      // set stays internal for the batch import path until it is migrated.
      const selectedCollectionProjectId = ref(null);
      const collectionProjectOptions = computed(() => (
        (collectionProjects.value || []).filter((item) => item.status !== 'archived')
      ));
      const settingsProjectRows = computed(() => {
        // 管理中心的项目管理需要展示当前数采工作空间的完整项目清单，
        // 不应被业务页面上的“当前采集项目”全局筛选影响。
        return collectionProjects.value || [];
      });
      async function loadCollectionProjectOptions() {
        if (!selectedWorkspaceId.value) {
          collectionProjects.value = [];
          selectedCollectionProjectId.value = null;
          return;
        }
        try {
          const res = await QuicDataAPI.listCollectionProjects(selectedWorkspaceId.value);
          collectionProjects.value = Array.isArray(res?.items) ? res.items : (Array.isArray(res) ? res : []);
        } catch {
          collectionProjects.value = [];
        }
        if (!collectionProjectOptions.value.some((item) => Number(item.id) === Number(selectedCollectionProjectId.value))) {
          selectedCollectionProjectId.value = null;
        }
        pruneCollectionProjectScope();
      }
      async function switchCollectionProject() {
        if (activeView.value === 'batches') await resetReviewPackagePage();
      }
      async function switchCollectionProjects() {
        if (activeView.value === 'batches') await resetReviewPackagePage();
      }
      const selectedBatch = ref(null);
      const selectedEpisode = ref(null);
      const selectedNativeLerobotDataset = ref(null);
      const selectedNativeLerobotBundle = ref(null);
      const nativeLerobotSessions = ref([]);
      const nativeLerobotSession = ref(null);
      const nativeLerobotSnapshot = ref(null);
      const nativeLerobotScanCandidates = ref([]);
      const nativeLerobotSessionCandidateIds = ref(new Set());
      const nativeLerobotBatchDatasets = ref([]);
      const selectedCollectorForQr = ref(null);
      let collectorDialogWorkspaceId = 0;
      let deviceDialogWorkspaceId = 0;
      let availableCollectorsGeneration = 0;
      let collectorDialogWriteGeneration = 0;
      let deviceDialogWriteGeneration = 0;
      let resourceWriteGeneration = 0;
      const tableLayoutVersion = ref(0);
      const episodePreview = ref(null);
      const rawSourceDownloads = ref(null);
      const nativeLerobotActionLoading = ref(false);
      const episodeDrawerContext = ref(null);
      const episodeDrawerTab = ref('overview');
      const episodeTechnicalSections = ref([]);
      const episodeDrawerLoading = reactive({ detail: false, preview: false, rawSource: false });
      const episodeDrawerError = reactive({ detail: '', preview: '' });
      const episodeDetailRequestGate = QuicDataEpisodeDetail.createRequestGate();
      const activeWorkbench = ref(null);
      const workbenchTimeline = ref(null);
      const workbenchDraft = ref({ mode: 'partitioned', segments: [], note: '' });
      const workbenchDraftDirty = ref(false);
      const workbenchVideo = ref(null);
      const cutTimelineTrack = ref(null);
      const cutTimelineCanvas = ref(null);
      const cutPlayheadLine = ref(null);
      const cutPlayheadPin = ref(null);
      const timelineTimecode = ref(null);
      const timelineScrollViewport = ref(null);
      const cutListViewport = ref(null);
      const annotationListViewport = ref(null);
      const timelineHoverPreview = ref(null);
      const timelineHoverLabel = ref(null);
      const cutLocalTimelineTrack = ref(null);
      const cutPlayheadTimestamp = ref(null);
      const cutListScrollTop = ref(0);
      const annotationListScrollTop = ref(0);
      const timelineViewRange = ref({ startRatio: 0, endRatio: 1 });
      const selectedCutBoundary = ref(null);
      const cutLocalRangeAnchor = ref(null);
      const activeCutSegmentIndex = ref(0);
      const activeAnnotationSegmentIndex = ref(0);
      const annotationDetailOpen = ref(false);
      const reviewDetailOpen = ref(false);
      const workbenchPlaying = ref(false);
      const workbenchMuted = ref(false);
      const workbenchVolume = ref(1);
      const workbenchPlaybackRate = ref(1);
      const lastWorkbenchPlaybackSeconds = ref(0);
      const timelineSnapEnabled = ref(true);
      const timelineZoom = ref(1);
      const timelinePanelHeight = ref(160);
      const workbenchEditorWidth = ref(420);
      const timelineHover = reactive({ visible: false, left: 0, pageX: 0, pageY: 0, timestamp_ns: '', playback_s: 0 });
      const timelineHoverVideo = ref(null);
      const workbenchHistory = reactive({ past: [], future: [], current: null, applying: false, ready: false });
      const workbenchClipboard = ref(null);
      const collectorProfiles = ref([]);
      const collectionDevices = ref([]);
      const aiSuggestions = ref({ capability: { eligible: false, rgb_topics: [], default_rgb_topic: '' }, items: [] });
      const selectedAiTopic = ref('');
      const CUT_WINDOW_ROW_STRIDE = 116;
      const ANNOTATION_SEGMENT_ROW_STRIDE = 282;
      const CUT_TIMELINE_DOM_LIMIT = 80;
      const WORKBENCH_HISTORY_LIMIT = 80;
      const WORKBENCH_HISTORY_LIMIT_DENSE = 24;
      const WORKBENCH_HISTORY_DEBOUNCE_MS = 250;
      let timelineWheelZoomDebt = 0;
      let workbenchSaveCoordinator = null;
      let workbenchSaveItemId = null;
      let workbenchInvalidationNotified = false;
      let workbenchHistoryTimer = null;
      let cutListScrollRaf = 0;
      let pendingCutListScrollTop = 0;
      let annotationListScrollRaf = 0;
      let pendingAnnotationListScrollTop = 0;
      let timelinePaintRaf = 0;
      let hoverSeekRaf = 0;
      let cutValidityCache = { segments: null, boundsStart: '', boundsEnd: '', save: false, submit: false };
      let ticksCache = { key: '', ticks: [] };
      const batchStatus = ref('');
      const episodeModality = ref(initialQueryValue('assets', 'modality'));
      const episodeTaskLabelId = ref(initialQueryNumber('assets', 'task_label_id'));
      const assetKind = ref(initialQueryValue('assets', 'kind'));
      const assetReviewStatus = ref('');
      const assetPublicationStatus = ref('');
      const assetKeyword = ref(initialQueryValue('assets', 'keyword'));
      const assetCollectorId = ref(initialQueryNumber('assets', 'collector_profile_id'));
      const assetDeviceId = ref(initialQueryNumber('assets', 'collection_device_id'));
      const assetPublishedRange = ref([
        initialQueryValue('assets', 'published_from'),
        initialQueryValue('assets', 'published_to'),
      ].filter(Boolean));
      const assetSortBy = ref(initialQueryValue('assets', 'sort_by', 'published_at'));
      const assetSortOrder = ref(initialQueryValue('assets', 'sort_order', 'desc'));
      const queueTaskSetId = ref(initialQueryNumber('work-queue', 'task_set_id'));
      const queueTaskLabelId = ref(initialQueryNumber('work-queue', 'task_label_id'));
      const queueStatus = ref(initialQueryValue('work-queue', 'status'));
      const queueEpisodeKeyword = ref(initialQueryValue('work-queue', 'episode_keyword'));
      const queueUpdatedRange = ref([
        initialQueryValue('work-queue', 'updated_from'),
        initialQueryValue('work-queue', 'updated_to'),
      ].filter(Boolean));
      const queueSortBy = ref(initialQueryValue('work-queue', 'sort_by'));
      const queueSortOrder = ref(initialQueryValue('work-queue', 'sort_order'));
      const assetExpandedIds = ref(new Set());
      const queueStage = ref(initialQueryValue('work-queue', 'stage', 'annotation'));
      const initialQueueReviewTargetKind = initialQueryValue('work-queue', 'review_target_kind');
      const queueReviewTargetKind = ref(
        queueStage.value === 'review' && ['cut', 'annotation'].includes(initialQueueReviewTargetKind)
          ? initialQueueReviewTargetKind
          : '',
      );
      function setQueueStageValue(stage) {
        if (!demoMode.value && !['annotation', 'review'].includes(stage)) stage = QuicDataAccessPolicy.defaultQueueStage(user.value) || 'annotation';
        queueStage.value = stage;
        if (stage !== 'review') queueReviewTargetKind.value = '';
      }
      // Annotation and review stages carry two kinds of work: the Episode-owned
      // work item queue (workbench entry) and the data-batch governance queue.
      const queueDataMode = ref('governance');
      const queueDataModeOptions = computed(() => [
        { value: 'work_items', label: t('queueModeWorkItems') },
        { value: 'governance', label: t('queueModeGovernance') },
      ]);
      const governanceQueueActive = computed(() => (
        (queueStage.value === 'annotation' || queueStage.value === 'review')
        && (!demoMode.value || queueDataMode.value === 'governance')
      ));
      async function switchQueueDataMode(mode) {
        queueDataMode.value = mode === 'work_items' ? 'work_items' : 'governance';
        queueStatus.value = '';
        resetWorkQueuePage();
        await loadWorkQueue();
        subscribeWorkspaceQueue();
      }
      let queueStageInitialized = initialRoute.view === 'work-queue' && ['cut', 'annotation', 'review', 'completed'].includes(queueStage.value);
      const showWorkspaceDialog = ref(false);
      const showTaskSetDialog = ref(false);
      const showBatchDialog = ref(false);
      const showChangePassword = ref(false);
      const showApiTokens = ref(false);
      const apiTokens = ref([]);
      const apiTokensLoading = ref(false);
      const tokenForm = reactive({ name: '', expires_in_days: 90 });
      const tokenSecret = ref(null);
      const tokenSecretVisible = ref(false);
      const showManagedUserDialog = ref(false);
      const showWorkspaceMemberDialog = ref(false);
      const showIntakeGuide = ref(false);
      const showTaskLabelDialog = ref(false);
      const showCollectorDialog = ref(false);
      const showDeviceDialog = ref(false);
      const showCollectorQrDialog = ref(false);
      const showEpisodeDrawer = ref(false);
      const showNativeLerobotDialog = ref(false);
      const showNativeLerobotReauthorizationDialog = ref(false);
      const showShortcutHelp = ref(false);
      const saving = ref(false);
      const uploading = ref(false);
      const intakeTab = ref('overview');
      const login = reactive({ email: '', password: '' });
      const passwordForm = reactive({ oldPassword: '', newPassword: '', confirmPassword: '' });
      const workspaceForm = reactive({ workspace_name: '', desc: '' });
      const taskSetForm = reactive({ name: '', description: '', scene: '' });
      const batchForm = reactive({ name: '', batch_type: 'ego' });
      const taskLabelForm = reactive({ name: '', description: '' });
      const collectorForm = reactive({ name: '', profile_key: '' });
      const collectorDialogMode = ref('existing');
      const availableCollectors = ref([]);
      const selectedExistingCollectorId = ref(null);
      const loadingAvailableCollectors = ref(false);
      const deviceForm = reactive({ name: '', device_type: 'iphone', model: '', serial_number: '' });
      const managedUserForm = reactive({ email: '', password: '', role: 'viewer' });
      const workspaceMemberForm = reactive({ user_id: null });
      const reviewForm = reactive({ note: '', rating: 0, target_work_item_id: null });
      const subscriptions = new Map();
      let initialRouteResolved = false;
      let accessRedirectWarningShown = false;

      const currentWorkspace = computed(() => workspaces.value.find((item) => item.id === selectedWorkspaceId.value) || null);
      const currentTaskSet = computed(() => taskSets.value.find((item) => item.id === selectedTaskSetId.value) || null);
      const collectorProfileFormValid = computed(() => {
        const key = collectorForm.profile_key.trim();
        return Boolean(collectorForm.name.trim()) && (!key || /^[1-9][0-9]*$/.test(key));
      });
      const collectorQrCodes = computed(() => (
        typeof QuicDataQrControlCodes === 'undefined'
          ? null
          : QuicDataQrControlCodes.forCollector(selectedCollectorForQr.value)
      ));
      const workbenchShortcutCommands = computed(() => (
        typeof QuicDataWorkbenchShortcuts === 'undefined'
          ? []
          : QuicDataWorkbenchShortcuts.commandsFor(activeWorkbench.value?.capabilities || {})
      ));
      const queueStageOptions = computed(() => [
        { value: 'annotation', label: t('queueAnnotation') },
        { value: 'review', label: t('queueReview') },
        ...(demoMode.value ? [{ value: 'completed', label: t('queueCompleted') }] : []),
      ]);
      const overviewCounts = computed(() => ({
        batches: batches.value.length,
        episodes: episodeTotal.value,
        queue: queueTotal.value,
        runningImports: batchImports.value.filter((item) => !['succeeded', 'failed', 'cancelled', 'superseded'].includes(item.status)).length,
      }));
      const overviewStages = computed(() => {
        const stages = [
          { key: 'import', label: t('stageImport'), count: overviewCounts.value.runningImports },
          { key: 'quality', label: t('stageQuality'), count: episodes.value.filter((item) => ['pending', 'running', 'processing'].includes(item.quality?.status)).length },
          { key: 'human', label: t('stageHuman'), count: episodes.value.filter((item) => ['assigned', 'in_progress', 'submitted'].includes(item.human_stage?.status)).length },
          { key: 'review', label: t('stageReview'), count: episodes.value.filter((item) => ['pending', 'in_progress'].includes(item.review?.status || item.review_status)).length },
          { key: 'publish', label: t('stagePublish'), count: publishedEpisodes.value.length },
        ];
        const max = Math.max(...stages.map((item) => item.count), 1);
        return stages.map((item) => ({ ...item, percent: Math.round(item.count / max * 100) }));
      });
      const overviewActions = computed(() => [
        { key: 'cut', label: t('queueCut'), count: episodes.value.filter((item) => ['pending', 'assigned', 'in_progress'].includes(item.human_stage?.status)).length },
        { key: 'annotation', label: t('queueAnnotation'), count: episodes.value.filter((item) => item.human_stage?.status === 'submitted').length },
        { key: 'review', label: t('queueReview'), count: episodes.value.filter((item) => ['pending', 'in_progress'].includes(item.review?.status || item.review_status)).length },
      ]);
      const assetGroups = computed(() => QuicDataAssetLineage.buildGroups(episodes.value, {
        kind: assetKind.value,
        review_status: assetReviewStatus.value,
        publication_status: assetPublicationStatus.value,
      }));
      const assetRows = computed(() => episodes.value);
      const activeAssetFilters = computed(() => [
        assetKeyword.value.trim() && { key: 'keyword', label: `${t('episode')}: ${assetKeyword.value.trim()}` },
        episodeModality.value && { key: 'modality', label: `${t('modality')}: ${String(episodeModality.value).toUpperCase()}` },
        episodeTaskLabelId.value && { key: 'task_label_id', label: `${t('collectionTask')}: ${taskLabelName(episodeTaskLabelId.value)}` },
        assetKind.value && { key: 'kind', label: `${t('kind')}: ${assetKindLabel(assetKind.value)}` },
        assetCollectorId.value && { key: 'collector_profile_id', label: `${t('collector')}: ${collectorChoiceLabel(assetCollectorId.value)}` },
        assetDeviceId.value && { key: 'collection_device_id', label: `${t('collectionDevice')}: ${collectionDeviceChoiceLabel(assetDeviceId.value)}` },
        assetPublishedRange.value?.length === 2 && {
          key: 'published_range',
          label: `${t('publishedAt')}: ${formatDate(assetPublishedRange.value[0])} - ${formatDate(assetPublishedRange.value[1])}`,
        },
      ].filter(Boolean));
      const activeWorkQueueFilters = computed(() => [
        queueEpisodeKeyword.value.trim() && { key: 'episode_keyword', label: `${t('episode')}: ${queueEpisodeKeyword.value.trim()}` },
        queueTaskSetId.value && {
          key: 'task_set_id',
          label: `${t('taskSet')}: ${taskSets.value.find((item) => Number(item.id) === Number(queueTaskSetId.value))?.name || queueTaskSetId.value}`,
        },
        queueTaskLabelId.value && { key: 'task_label_id', label: `${t('collectionTask')}: ${taskLabelName(queueTaskLabelId.value)}` },
        queueStage.value === 'review' && queueReviewTargetKind.value && {
          key: 'review_target_kind',
          label: `${t('reviewType')}: ${queueReviewTargetKind.value === 'cut' ? t('reviewCut') : t('reviewAnnotation')}`,
        },
        queueStatus.value && { key: 'status', label: `${t('status')}: ${workItemStatusLabel(queueStatus.value)}` },
        queueUpdatedRange.value?.length === 2 && {
          key: 'updated_range',
          label: `${t('operationTime')}: ${formatDate(queueUpdatedRange.value[0])} - ${formatDate(queueUpdatedRange.value[1])}`,
        },
      ].filter(Boolean));
      const mustChangePassword = computed(() => Boolean(user.value?.must_change_password));
      const workbenchKind = computed(() => activeWorkbench.value?.work_item?.kind || '');
      const workbenchTitle = computed(() => ({
        cut: t('cutWorkbench'), annotation: t('annotationWorkbench'), review: t('reviewWorkbench'),
      }[workbenchKind.value] || t('workbench')));
      const canUndoWorkbench = computed(() => workbenchHistory.past.length > 0);
      const canRedoWorkbench = computed(() => workbenchHistory.future.length > 0);
      const canPasteWorkbench = computed(() => Boolean(
        activeWorkbench.value?.capabilities?.annotation
        && workbenchClipboard.value?.kind === 'annotation_segment',
      ));
      const userDisplayName = computed(() => {
        const account = user.value || {};
        return account.nickname || account.display_name || account.name || String(account.email || '').split('@')[0] || t('accountMenu');
      });
      const userInitial = computed(() => String(userDisplayName.value || 'Q').trim().charAt(0).toUpperCase());
      watch(userDisplayName, (name) => {
        if (name && !miningTaskForm.owner) miningTaskForm.owner = name;
      }, { immediate: true });
      const demoMode = ref(typeof QuicDataAPI.isDemoMode === 'function' && QuicDataAPI.isDemoMode());
      const localPreviewAvailable = computed(() => typeof QuicDataAPI.isLocalPreview === 'function' && QuicDataAPI.isLocalPreview());

      function t(key, params) {
        let text = MESSAGES[locale.value]?.[key] || MESSAGES['zh-CN'][key] || key;
        if (params && typeof text === 'string') {
          text = text.replace(/\{(\w+)\}/g, (match, paramKey) => (params[paramKey] !== undefined ? String(params[paramKey]) : match));
        }
        return text;
      }

      function setLocale(nextLocale) {
        locale.value = nextLocale === 'en-US' ? 'en-US' : 'zh-CN';
        try { localStorage.setItem('quicdata_locale', locale.value); } catch { /* Optional UI preference. */ }
        if (typeof document !== 'undefined') {
          document.documentElement.lang = locale.value;
          document.title = `${t('brand')} - ${t('product')}`;
        }
      }

      function toggleLocale() {
        setLocale(locale.value === 'zh-CN' ? 'en-US' : 'zh-CN');
      }

      function hasPermission(permission) {
        return QuicDataAccessPolicy.hasPermission(user.value, permission);
      }

      function canView(view) {
        return QuicDataAccessPolicy.canView(user.value, view);
      }

      const visibleViews = computed(() => QuicDataAccessPolicy.visibleViews(user.value));
      const consoleAccessAvailable = computed(() => visibleViews.value.length > 0);

      function roleLabel(role) {
        const configured = roleDefinitions.value?.roles?.[role];
        const fallback = configured && typeof configured === 'object' ? configured.label : '';
        return QuicDataAccessPolicy.roleLabel(role, locale.value, fallback);
      }

      const sidebarIcons = {
        overview: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
        intake: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 17v3h16v-3"/></svg>',
        batches: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 7 8-4 8 4-8 4-8-4Z"/><path d="m4 12 8 4 8-4M4 17l8 4 8-4"/></svg>',
        'work-queue': '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5h11M9 12h11M9 19h11"/><path d="m3 5 1 1 2-2m-3 8 1 1 2-2m-3 8 1 1 2-2"/></svg>',
        resources: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="4"/><path d="M3 21v-2a6 6 0 0 1 12 0v2M16 7h5m-2.5-2.5v5"/></svg>',
        assets: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h7l2 2h9v11H3V6Z"/><path d="M3 10h18"/></svg>',
        datasets: '<svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>',
        admin: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 5 6v5c0 4.6 2.8 8.1 7 10 4.2-1.9 7-5.4 7-10V6l-7-3Z"/><circle cx="12" cy="10" r="2.5"/><path d="M8.5 16a4 4 0 0 1 7 0"/></svg>',
        settings: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></svg>',
        mining: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V9l5-4 5 4v11"/><path d="M14 20V13h6v7"/><path d="M9 20v-5h2v5"/></svg>',
      };

      function toggleSidebar() {
        sidebarCollapsed.value = !sidebarCollapsed.value;
      }

      function toggleWorkbenchQueueNav() {
        if (sidebarCollapsed.value) {
          navigate('work-queue');
          return;
        }
        workbenchQueueOpen.value = !workbenchQueueOpen.value;
        if (workbenchQueueOpen.value && activeView.value !== 'work-queue' && activeView.value !== 'workbench') navigate('work-queue');
      }

      function toggleMiningNav() {
        if (sidebarCollapsed.value) {
          navigate('miningTasks');
          return;
        }
        miningNavOpen.value = !miningNavOpen.value;
        if (miningNavOpen.value && activeView.value !== 'miningTasks' && activeView.value !== 'miningCloud') navigate('miningTasks');
      }

      const canManageWorkspace = computed(() => hasPermission('workspace:write'));
      const canCreateBatch = computed(() => Boolean(currentWorkspace.value && currentTaskSet.value && hasPermission('batch:write')));
      const canImport = computed(() => hasPermission('import:write'));
      const canManageTaskLabels = computed(() => Boolean(canImport.value && user.value?.role === 'admin'));
      const canManageDatasets = computed(() => hasPermission('dataset:write'));
      const canExportDatasets = computed(() => hasPermission('export:write'));
      const canManageUsers = computed(() => user.value?.role === 'admin' || hasPermission('*'));
      const canViewDashboard = computed(() => (
        hasPermission('dashboard:read') || hasPermission('dashboard:*') || hasPermission('*')
      ));
      const assignableRoles = computed(() => Object.keys(roleDefinitions.value?.roles || {}));
      const availableWorkspaceMembers = computed(() => {
        const existing = new Set(managementMembers.value.map((member) => Number(member.user_id)));
        return managedUsers.value.filter((managedUser) => !existing.has(Number(managedUser.id)));
      });

      function showAccessRedirectWarning() {
        if (accessRedirectWarningShown) return;
        accessRedirectWarningShown = true;
        ElMessage.warning(t('accessRedirected'));
      }

      function setAuthorizedView(requestedView, { warn = false, updateHash = true, preserveAuthorizedHash = false, queueStageOverride = null } = {}) {
        let nextView = requestedView;
        const requestedViewAllowed = canView(nextView);
        if (!requestedViewAllowed) {
          nextView = QuicDataAccessPolicy.defaultView(user.value);
          if (warn) showAccessRedirectWarning();
        }
        if (!nextView) return null;
        const previousView = activeView.value;
        activeView.value = nextView;
        if (nextView !== 'workbench') {
          workbenchRoute.workItemId = '';
          workbenchRoute.episodeId = '';
        }
        if (nextView === 'work-queue' && (previousView !== 'work-queue' || !queueStageInitialized)) {
          setQueueStageValue(
            queueStageOverride
              || (queueStageInitialized ? queueStage.value : null)
              || QuicDataAccessPolicy.defaultQueueStage(user.value)
              || queueStage.value,
          );
          queueStageInitialized = true;
        }
        const shouldNormalizeHash = !preserveAuthorizedHash || !requestedViewAllowed;
        if (updateHash && shouldNormalizeHash && typeof window !== 'undefined') {
          const packageId = nextView === 'intake-review' ? Number(intakeReviewPackageId.value || 0) : 0;
          const nextHash = nextView === 'package-workbench' ? packageWorkbenchHash() : packageId > 0 ? `#/intake-review?package_id=${packageId}` : `#/${nextView}`;
          if (window.location.hash !== nextHash) window.location.hash = nextHash;
        }
        return nextView;
      }

      function syncListRouteState({ push = false } = {}) {
        if (typeof window === 'undefined' || !['assets', 'work-queue'].includes(activeView.value)) return false;
        const params = new URLSearchParams();
        const add = (key, value) => {
          if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
        };
        if (activeView.value === 'assets') {
          add('page', normalizedEpisodePage(episodePage.value));
          add('page_size', normalizedEpisodePageSize(episodePageSize.value));
          add('sort_by', assetSortBy.value);
          add('sort_order', assetSortOrder.value);
          add('keyword', assetKeyword.value.trim());
          add('modality', episodeModality.value);
          add('task_label_id', episodeTaskLabelId.value);
          add('kind', assetKind.value);
          add('collector_profile_id', assetCollectorId.value);
          add('collection_device_id', assetDeviceId.value);
          add('published_from', assetPublishedRange.value?.[0]);
          add('published_to', assetPublishedRange.value?.[1]);
        } else {
          add('stage', queueStage.value);
          add('page', normalizedWorkQueuePage(queuePage.value));
          add('page_size', normalizedWorkQueuePageSize(queuePageSize.value));
          add('sort_by', queueSortBy.value);
          add('sort_order', queueSortOrder.value);
          add('task_set_id', queueTaskSetId.value);
          add('task_label_id', queueTaskLabelId.value);
          add('review_target_kind', queueStage.value === 'review' ? queueReviewTargetKind.value : '');
          add('status', queueStatus.value);
          add('episode_keyword', queueEpisodeKeyword.value.trim());
          add('updated_from', queueUpdatedRange.value?.[0]);
          add('updated_to', queueUpdatedRange.value?.[1]);
        }
        const search = params.toString();
        const hash = `#/${activeView.value}${search ? `?${search}` : ''}`;
        if (push && window.location.hash !== hash) {
          window.location.hash = hash;
          return true;
        }
        window.history.replaceState(null, '', hash);
        return false;
      }

      function applyListRouteQuery(route) {
        const query = route?.query || {};
        const positiveInteger = (key, fallback) => {
          const value = Number(query[key]);
          return Number.isInteger(value) && value > 0 ? value : fallback;
        };
        if (route?.view === 'assets') {
          episodePage.value = positiveInteger('page', 1);
          episodePageSize.value = normalizedEpisodePageSize(positiveInteger('page_size', 50));
          assetSortBy.value = ['published_at', 'created_at', 'updated_at', 'episode_uid', 'duration'].includes(query.sort_by)
            ? query.sort_by : 'published_at';
          assetSortOrder.value = ['asc', 'desc'].includes(query.sort_order) ? query.sort_order : 'desc';
          assetKeyword.value = query.keyword || '';
          episodeModality.value = query.modality || '';
          episodeTaskLabelId.value = positiveInteger('task_label_id', '');
          assetKind.value = query.kind || '';
          assetCollectorId.value = positiveInteger('collector_profile_id', '');
          assetDeviceId.value = positiveInteger('collection_device_id', '');
          assetPublishedRange.value = [query.published_from, query.published_to].filter(Boolean);
        } else if (route?.view === 'work-queue') {
          queuePage.value = positiveInteger('page', 1);
          queuePageSize.value = normalizedWorkQueuePageSize(positiveInteger('page_size', 50));
          setQueueStageValue(['cut', 'annotation', 'review', 'completed'].includes(query.stage) ? query.stage : 'cut');
          queueReviewTargetKind.value = queueStage.value === 'review'
            && ['cut', 'annotation'].includes(query.review_target_kind)
            ? query.review_target_kind
            : '';
          queueTaskSetId.value = positiveInteger('task_set_id', '');
          queueTaskLabelId.value = positiveInteger('task_label_id', '');
          queueStatus.value = query.status || '';
          queueEpisodeKeyword.value = query.episode_keyword || '';
          queueUpdatedRange.value = [query.updated_from, query.updated_to].filter(Boolean);
          queueSortBy.value = ['created_at', 'updated_at', 'episode_uid', 'duration'].includes(query.sort_by)
            ? query.sort_by : '';
          queueSortOrder.value = ['asc', 'desc'].includes(query.sort_order) ? query.sort_order : '';
          queueStageInitialized = true;
        }
      }

      function resolveInitialAuthorizedView() {
        if (initialRouteResolved) {
          const preserveAuthorizedRoute = (
            activeView.value === 'workbench' && Boolean(workbenchRoute.workItemId && workbenchRoute.episodeId)
          ) || ['assets', 'work-queue'].includes(activeView.value);
          return setAuthorizedView(activeView.value, { warn: true, preserveAuthorizedHash: preserveAuthorizedRoute });
        }
        initialRouteResolved = true;
        const requestedView = initialRouteWasExplicit
          ? activeView.value
          : QuicDataAccessPolicy.defaultView(user.value);
        return setAuthorizedView(requestedView, { warn: initialRouteWasExplicit, preserveAuthorizedHash: initialRouteWasExplicit });
      }

      function errorMessage(error) {
        const businessErrorKeys = {
          workspace_name_exists: 'workspaceNameExists',
          task_set_name_exists: 'taskSetNameExists',
          dataset_name_exists: 'datasetNameExists',
          collector_name_exists: 'collectorNameExists',
          device_serial_exists: 'deviceSerialExists',
        };
        const translationKey = businessErrorKeys[String(error?.code || '')];
        ElMessage.error(translationKey ? t(translationKey) : (error instanceof Error ? error.message : '请求失败'));
      }

      function clearPasswordForm() {
        passwordForm.oldPassword = '';
        passwordForm.newPassword = '';
        passwordForm.confirmPassword = '';
      }

      function openChangePassword() {
        clearPasswordForm();
        showChangePassword.value = true;
      }

      function cancelPasswordChange() {
        if (!mustChangePassword.value) {
          clearPasswordForm();
          showChangePassword.value = false;
        }
      }

      async function openApiTokens() {
        showApiTokens.value = true;
        tokenSecret.value = null;
        tokenSecretVisible.value = false;
        await loadApiTokens();
      }
      function cancelApiTokens() {
        showApiTokens.value = false;
        tokenSecret.value = null;
        tokenSecretVisible.value = false;
      }
      async function loadApiTokens() {
        apiTokensLoading.value = true;
        try {
          const data = await QuicDataAPI.listApiTokens();
          apiTokens.value = Array.isArray(data?.items) ? data.items : [];
        } catch (error) {
          errorMessage(error);
        } finally {
          apiTokensLoading.value = false;
        }
      }
      async function createApiToken() {
        const name = tokenForm.name.trim();
        if (!name) {
          ElMessage.error(t('apiTokenNameRequired'));
          return;
        }
        try {
          const created = await QuicDataAPI.createApiToken({ name, expires_in_days: Number(tokenForm.expires_in_days) });
          tokenSecret.value = created.secret;
          tokenSecretVisible.value = true;
          tokenForm.name = '';
          tokenForm.expires_in_days = 90;
          await loadApiTokens();
        } catch (error) {
          errorMessage(error);
        }
      }
      async function rotateApiToken(row) {
        try {
          await ElMessageBox.confirm(t('apiTokenRotateConfirm'), t('apiTokens'), {
            confirmButtonText: t('apiTokenRotate'), cancelButtonText: t('cancel'), type: 'warning',
          });
          const rotated = await QuicDataAPI.rotateApiToken(row.id);
          tokenSecret.value = rotated.secret;
          tokenSecretVisible.value = true;
          await loadApiTokens();
        } catch (error) {
          if (error !== 'cancel') {
            errorMessage(error);
            await loadApiTokens();
          }
        }
      }
      async function revokeApiToken(row) {
        try {
          await ElMessageBox.confirm(t('apiTokenRevokeConfirm', { name: row.name }), t('apiTokens'), {
            confirmButtonText: t('apiTokenRevoke'), cancelButtonText: t('cancel'), type: 'danger',
          });
          await QuicDataAPI.revokeApiToken(row.id);
          ElMessage.success(t('apiTokenRevoked'));
          await loadApiTokens();
        } catch (error) {
          if (error !== 'cancel') {
            errorMessage(error);
            await loadApiTokens();
          }
        }
      }
      function copyTokenSecret() {
        if (!tokenSecret.value) return;
        const text = tokenSecret.value;
        const fallbackCopy = () => {
          try {
            const area = document.createElement('textarea');
            area.value = text;
            area.setAttribute('readonly', '');
            area.style.position = 'fixed';
            area.style.opacity = '0';
            document.body.appendChild(area);
            area.select();
            const copied = document.execCommand('copy');
            document.body.removeChild(area);
            return copied;
          } catch (err) {
            return false;
          }
        };
        if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(text)
            .then(() => ElMessage.success(t('apiTokenCopied')))
            .catch(() => {
              if (fallbackCopy()) ElMessage.success(t('apiTokenCopied'));
              else ElMessage.error(t('apiTokenCopyFailed'));
            });
        } else if (fallbackCopy()) {
          ElMessage.success(t('apiTokenCopied'));
        } else {
          ElMessage.error(t('apiTokenCopyFailed'));
        }
      }
      function apiTokenStatus(row) {
        if (row?.revoked_at) return { label: t('apiTokenRevoked'), type: 'info' };
        if (row?.expires_at && new Date(row.expires_at).getTime() < Date.now()) return { label: t('apiTokenExpired'), type: 'danger' };
        return { label: t('apiTokenActive'), type: 'success' };
      }

      function qualityStatusLabel(status) {
        const normalized = status === 'profiled' ? 'passed' : status;
        return {
          pending: t('qualityPending'), running: t('qualityPending'), passed: t('qualityPassed'),
          recovered: t('qualityRecovered'), failed: t('qualityFailed'),
        }[normalized] || t('qualityPending');
      }

      function qualityStatusType(status) {
        const normalized = status === 'profiled' ? 'passed' : status;
        if (normalized === 'passed') return 'success';
        if (normalized === 'recovered') return 'warning';
        if (normalized === 'failed') return 'danger';
        return 'info';
      }

      function importStatusLabel(status) {
        return {
          init: t('importInit'), uploading: t('importUploading'), materialize_queued: t('processingImport'), uploaded: t('importUploaded'), parsing: t('importParsing'),
          succeeded: t('importSucceeded'), partially_succeeded: t('importPartial'), failed: t('importFailed'), cancelled: t('importCancelled'), superseded: t('importSuperseded'),
        }[status] || t('importParsing');
      }

      function importStatusType(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'partially_succeeded') return 'warning';
        if (status === 'failed' || status === 'error') return 'danger';
        if (status === 'cancelled' || status === 'superseded') return 'info';
        return 'warning';
      }

      function importDisplayStatus(session) {
        if (session?.status === 'uploading' && session?.materialize_status === 'queued') return 'materialize_queued';
        if (session?.status === 'uploading' && session?.materialize_status === 'failed') return 'failed';
        const imported = Number(session?.imported_source_count);
        const failed = Number(session?.failed_source_count);
        if (session?.import_type === 'oss_scan' && session?.status === 'failed'
          && Number.isInteger(imported) && Number.isInteger(failed)
          && imported > 0 && failed > 0) {
          return 'partially_succeeded';
        }
        return session?.status || 'init';
      }

      function importProgressText(session) {
        const hasSourceCounts = session?.imported_source_count !== null
          && session?.imported_source_count !== undefined
          && session?.failed_source_count !== null
          && session?.failed_source_count !== undefined;
        const importedSources = Number(session?.imported_source_count);
        const failedSources = Number(session?.failed_source_count);
        if (hasSourceCounts && Number.isInteger(importedSources) && Number.isInteger(failedSources)
          && importedSources >= 0 && failedSources >= 0) {
          return `${t('sourceImported')} ${importedSources} · ${t('sourceFailed')} ${failedSources}`;
        }
        const progress = session?.upload_progress;
        const uploaded = Number(progress?.uploaded_chunks);
        const total = Number(progress?.total_chunks);
        const percent = Number(progress?.percent);
        if (!Number.isInteger(uploaded) || !Number.isInteger(total) || total <= 0 || !Number.isInteger(percent)) return '';
        return `${uploaded} / ${total} · ${Math.max(0, Math.min(100, percent))}%`;
      }

      function qualityProgressPercent(batch) {
        const total = Math.max(0, Number(batch?.quality_progress?.total || 0));
        const completed = Math.max(0, Number(batch?.quality_progress?.completed || 0));
        return total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
      }

      function qualityDiagnosticLabel(diagnostic) {
        return {
          worker_interrupted: t('qualityWorkerInterrupted'),
          metadata_invalid: t('qualityMetadataInvalid'),
          raw_source_unavailable: t('qualityRawSourceUnavailable'),
          reference_topic_missing: t('qualityReferenceTopicMissing'),
          reference_timeline_incomplete: t('qualityReferenceTimelineIncomplete'),
        }[diagnostic?.summary] || t('qualityFailureGeneric');
      }

      function scanStatusLabel(status) {
        return {
          queued: t('scanQueued'), running: t('scanningCandidates'), retry_pending: t('scanRetryPending'),
          succeeded: t('scanSucceeded'), failed: t('scanFailed'), cancelled: t('scanFailed'),
        }[status] || t('scanQueued');
      }

      function scanStatusType(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'failed' || status === 'cancelled') return 'danger';
        if (status === 'running' || status === 'queued' || status === 'retry_pending') return 'warning';
        return 'info';
      }

      function batchStatusLabel(status) {
        return {
          created: t('batchCreated'), importing: t('batchImporting'), processing: t('batchProcessing'), ready: t('batchReady'),
          failed: t('batchFailed'), cancelled: t('batchCancelled'),
        }[status] || status || '—';
      }

      function jobKindLabel(kind) {
        return {
          import_parse: t('jobImportParse'), ego_profile: t('jobQualityCheck'), quality_check: t('jobQualityCheck'),
          episode_preview: t('jobPreview'), process_preview_publish: t('jobPreview'), episode_publish: t('jobPublication'),
        }[kind] || t('jobProcessing');
      }

      function jobStatusLabel(status) {
        return {
          queued: t('jobQueued'), retry_pending: t('jobQueued'), running: t('jobRunning'),
          succeeded: t('jobSucceeded'), failed: t('jobFailed'), cancelled: t('jobCancelled'),
        }[status] || t('jobProcessing');
      }

      function activityKindLabel(item) {
        if (item?.kind === 'batch_log') return t('activityBatchLog');
        if (item?.kind === 'import_session') return t('activityImportSession');
        if (item?.kind === 'job_run') return t('activityJobRun');
        return t('activity');
      }

      function activitySummary(item) {
        if (item?.kind === 'batch_log') return batchStatusLabel(item.to_status);
        if (item?.kind === 'import_session') return item.original_name || t('activityImportSession');
        if (item?.kind === 'job_run') return jobKindLabel(item.job_kind);
        return '—';
      }

      function activityStatusLabel(item) {
        if (item?.kind === 'batch_log') return batchStatusLabel(item.to_status);
        if (item?.kind === 'import_session') return importStatusLabel(item.status);
        if (item?.kind === 'job_run') return jobStatusLabel(item.status);
        return '—';
      }

      function activityStatusType(item) {
        if (item?.kind === 'import_session') return importStatusType(item.status);
        if (item?.kind === 'job_run') return exportStatusType(item.status);
        if (item?.to_status === 'ready') return 'success';
        if (item?.to_status === 'failed') return 'danger';
        return 'info';
      }

      function sourceLabel(sourceType) {
        return { upload: t('sourceUpload'), oss_candidate: t('sourceOss'), filesystem_candidate: t('sourceFilesystem') }[sourceType] || sourceType || '—';
      }

      function batchTypeLabel(batchType) {
        return { ego: t('typeEgo'), umi: t('typeUmi'), teleop: t('typeTeleop'), lerobot: t('typeLerobot'), qrdf_import: t('typeQrdf'), manual_upload: t('typeManual') }[batchType] || batchType || '—';
      }

      function batchSourceLabel(source) {
        return {
          local_upload: t('sourceUpload'), oss: t('sourceOss'), filesystem: t('sourceFilesystem'),
          mixed: t('sourceMixed'), unknown: t('sourceUnknown'),
        }[source] || t('sourceUnknown');
      }

      function batchContentSummary(batch) {
        if (batch?.batch_type === 'lerobot') {
          return `${Number(batch.dataset_count || 0)} ${t('datasets')} · ${Number(batch.file_count || 0)} ${t('fileCount')} · ${formatBytes(batch.total_size)}`;
        }
        return `${Number(batch?.source_episode_count || 0)} ${t('sourceEpisodes')} · ${formatDuration(batch?.source_duration_s)}`;
      }

      function batchAssociationSummary(batch) {
        if (batch?.batch_type === 'lerobot') return `${Number(batch.session_count || 0)} ${t('importHistory')}`;
        return batch?.task_label?.name || t('noTaskLabel');
      }

      function workActionLabel(action) {
        return { claim: t('actionClaim'), continue: t('openWorkbench'), release: t('actionRelease'), submit: t('actionSubmit') }[action] || action;
      }

      function workItemKindLabel(kind) {
        return { cut: t('queueCut'), annotation: t('queueAnnotation'), review: t('queueReview') }[kind] || kind || '—';
      }

      function workItemStatusLabel(status) {
        return {
          pending: t('workPending'), assigned: t('workAssigned'), in_progress: t('workInProgress'),
          submitted: t('workSubmitted'), rejected: t('workNeedsRework'), returned: t('workNeedsRework'), accepted: t('workAccepted'), approved: t('workAccepted'), done: t('workAccepted'),
        }[status] || status || '—';
      }

      function queueStageLabel(stage) {
        return {
          cut: t('queueCut'), annotation: t('queueAnnotation'), review: t('queueReview'), completed: t('queueCompleted'),
        }[stage] || stage || '—';
      }

      function publicationJobStatusLabel(status) {
        return {
          waiting: t('publicationQueued'), queued: t('publicationQueued'), retry_pending: t('publicationQueued'),
          running: t('publicationRunning'), succeeded: t('publicationSucceeded'),
          failed: t('publicationFailed'), cancelled: t('publicationCancelled'),
        }[status] || status || '—';
      }

      function queuePreviewStatus(row) {
        const status = row?.preview?.status;
        return ['ready', 'processing', 'failed'].includes(status) ? status : 'ready';
      }

      function previewStatusLabel(statusOrPreview) {
        const status = typeof statusOrPreview === 'string'
          ? statusOrPreview
          : (statusOrPreview && statusOrPreview.status);
        return {
          ready: t('previewReady'), processing: t('previewPreparing'), failed: t('previewFailed'),
        }[status] || t('previewUnavailable');
      }

      function previewStatusType(status) {
        if (status === 'ready') return 'success';
        if (status === 'failed' || status === 'error') return 'danger';
        return 'warning';
      }

      function queueRowKey(row) {
        const workItemId = Number(row?.work_item?.id);
        if (Number.isInteger(workItemId) && workItemId > 0) return `work-item:${workItemId}`;
        return `episode:${row?.episode?.id || row?.episode?.episode_uid || ''}`;
      }

      function queueActionKey(row, action) {
        return `${queueRowKey(row)}:${action}`;
      }

      function assetKindLabel(kind) {
        return kind === 'source' ? t('assetSource') : kind === 'derived' ? t('assetDerived') : kind || '—';
      }

      function reviewStatusLabel(status) {
        return {
          pending: t('reviewPending'), accepted: t('reviewAccepted'), rejected: t('reviewRejected'),
          not_applicable: t('reviewNotApplicable'),
        }[status] || '—';
      }

      function reviewStatusType(status) {
        if (status === 'accepted') return 'success';
        if (status === 'rejected') return 'danger';
        if (status === 'pending') return 'warning';
        return 'info';
      }

      function publicationCategory(item) {
        return QuicDataAssetLineage.publicationCategory(item);
      }

      function publicationCategoryLabel(item) {
        return {
          unpublished: t('publicationUnpublished'), publishing: t('publicationPublishing'),
          published: t('publicationPublished'), failed: t('publicationFailed'),
        }[publicationCategory(item)] || '—';
      }

      function publicationCategoryType(item) {
        const category = publicationCategory(item);
        if (category === 'published') return 'success';
        if (category === 'failed') return 'danger';
        if (category === 'publishing') return 'warning';
        return 'info';
      }

      function episodePublicationLabel(item) {
        if (item?.publication?.category) return publicationCategoryLabel(item);
        return publicationJobStatusLabel(item?.publication?.status || item?.publication_status);
      }

      function humanStageLabel(item) {
        const stage = item?.kind === 'source' ? t('queueCut') : t('queueAnnotation');
        const status = item?.human_work?.primary?.status || 'pending';
        const state = {
          pending: t('humanPending'), assigned: t('humanAssigned'), in_progress: t('humanInProgress'),
          submitted: t('humanSubmitted'), accepted: t('humanAccepted'), rejected: t('humanRejected'),
        }[status] || status || '—';
        return `${stage} · ${state}`;
      }

      function humanStageType(item) {
        const status = item?.human_work?.primary?.status || 'pending';
        if (status === 'accepted') return 'success';
        if (status === 'rejected') return 'danger';
        if (['assigned', 'in_progress', 'submitted'].includes(status)) return 'warning';
        return 'info';
      }

      function assetRootSummary(summary) {
        const total = Number(summary?.descendants || 0);
        if (!total) return t('noDerivedAssets');
        const review = summary?.review || {};
        const publication = summary?.publication || {};
        const segments = [`${total} ${t('derivedAssets')}`];
        if (review.accepted) segments.push(`${t('reviewAccepted')} ${review.accepted}`);
        if (review.pending) segments.push(`${t('reviewPending')} ${review.pending}`);
        if (review.rejected) segments.push(`${t('reviewRejected')} ${review.rejected}`);
        if (publication.published) segments.push(`${t('publicationPublished')} ${publication.published}`);
        if (summary?.quality_failed) segments.push(`${t('qualityFailed')} ${summary.quality_failed}`);
        return segments.join(' · ');
      }

      function assetLineageIssueLabel(issue) {
        return {
          missing_parent: t('lineageMissingParent'), cycle: t('lineageCycle'), depth_exceeded: t('lineageDepthExceeded'),
          invalid_id: t('lineageInvalid'), unexpected_parent: t('lineageIssue'),
        }[issue] || t('lineageIssue');
      }

      function formatAssetClock(milliseconds) {
        const total = Number(milliseconds);
        if (!Number.isFinite(total) || total < 0) return '—';
        const hours = Math.floor(total / 3600000);
        const minutes = Math.floor((total % 3600000) / 60000);
        const seconds = Math.floor((total % 60000) / 1000);
        const fraction = Math.floor(total % 1000);
        const clock = `${String(minutes).padStart(hours ? 2 : 1, '0')}:${String(seconds).padStart(2, '0')}.${String(fraction).padStart(3, '0')}`;
        return hours ? `${hours}:${clock}` : clock;
      }

      function assetRangeLabel(item) {
        try {
          const base = BigInt(String(item?.asset_group_start_ns ?? ''));
          const start = BigInt(String(item?.source_start_ns ?? ''));
          const end = BigInt(String(item?.source_end_ns ?? ''));
          if (start < base || end <= start) return '—';
          return `${formatAssetClock((start - base) / 1000000n)} - ${formatAssetClock((end - base) / 1000000n)}`;
        } catch {
          return '—';
        }
      }

      function assetGroupExpanded(item) {
        if (item?.asset_has_children && (assetKind.value || assetReviewStatus.value || assetPublicationStatus.value)) {
          return true;
        }
        const group = assetGroups.value.find((candidate) => Number(candidate.root.id) === Number(item?.id));
        return Boolean(group?.auto_expand || assetExpandedIds.value.has(item?.id) || assetExpandedIds.value.has(String(item?.id)));
      }

      function toggleAssetGroup(item) {
        if (!item?.asset_has_children) return;
        const next = new Set(assetExpandedIds.value);
        if (assetGroupExpanded(item)) {
          next.delete(item.id);
          next.delete(String(item.id));
        } else {
          next.add(item.id);
        }
        assetExpandedIds.value = next;
      }

      function assetRowClass({ row }) {
        if (row?.asset_lineage_issue) return 'asset-row asset-row-anomaly';
        return row?.asset_depth === 0 ? 'asset-row asset-row-root' : 'asset-row asset-row-derived';
      }

      function openAssetRow(item) {
        if (item?.asset_has_children) {
          toggleAssetGroup(item);
          return;
        }
        void openEpisodeDetail(item, { source: 'assets' });
      }

      function collectorDisplayLabel(profile) {
        if (!profile) return '';
        return profile.name || '';
      }

      function collectorChoiceLabel(id) {
        if (id === null || id === undefined || id === '') return t('unknownCollector');
        const profile = collectorProfiles.value.find((item) => Number(item.id) === Number(id));
        return collectorDisplayLabel(profile) || t('unknownCollector');
      }

      function collectionDeviceChoiceLabel(id) {
        if (id === null || id === undefined || id === '') return t('unknownDevice');
        const device = collectionDevices.value.find((item) => Number(item.id) === Number(id));
        return device ? `${device.name} · ${device.serial_number}` : t('unknownDevice');
      }

      function collectorAttributionLabel(attribution) {
        return collectorDisplayLabel(attribution?.collector) || t('unknownCollector');
      }

      function collectionDeviceAttributionLabel(attribution) {
        const device = attribution?.device;
        if (!device) return t('unknownDevice');
        return device.serial_number ? `${device.name} · ${device.serial_number}` : (device.name || t('unknownDevice'));
      }

      function attributionSourceLabel(source) {
        return {
          offline_declared: t('attributionOfflineDeclared'),
          offline_curated: t('attributionOfflineCurated'),
          machine_reported: t('attributionMachineReported'),
          online_verified: t('attributionOnlineVerified'),
        }[source] || t('attributionUnknown');
      }

      function exportStatusLabel(status) {
        return {
          queued: t('exportQueued'), retry_pending: t('exportQueued'), running: t('exportRunning'),
          succeeded: t('exportSucceeded'), failed: t('exportFailed'), cancelled: t('exportCancelled'),
        }[status] || status || '—';
      }

      function exportStatusType(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'failed' || status === 'error') return 'danger';
        if (status === 'cancelled') return 'info';
        if (status === 'running' || status === 'queued' || status === 'retry_pending') return 'warning';
        return 'info';
      }

      function formatNumber(value) {
        if (value === null || value === undefined || value === '') return '—';
        return new Intl.NumberFormat(locale.value).format(Number(value));
      }

      function formatDuration(value) {
        const seconds = Number(value);
        if (!Number.isFinite(seconds) || seconds <= 0) return '—';
        const minutes = Math.floor(seconds / 60);
        const remaining = Math.floor(seconds % 60);
        return `${minutes}:${String(remaining).padStart(2, '0')}`;
      }

      function formatDashboardDuration(value) {
        const seconds = Number(value);
        if (!Number.isFinite(seconds) || seconds <= 0) return '—';
        if (seconds >= 3600) {
          const hours = Math.floor(seconds / 3600);
          const minutes = Math.floor((seconds % 3600) / 60);
          return `${hours}h ${String(minutes).padStart(2, '0')}m`;
        }
        return formatDuration(seconds);
      }

      function formatAvailableDuration(value) {
        if (value === null || value === undefined || value === '') return '—';
        if (Number(value) === 0) return '0:00';
        return formatDuration(value);
      }

      function formatBytes(value) {
        if (value === null || value === undefined || value === '') return '—';
        const bytes = Number(value);
        if (!Number.isFinite(bytes) || bytes < 0) return '—';
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
        return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
      }

      function formatDate(value) {
        const text = typeof QuicDataDateTime === 'undefined'
          ? ''
          : QuicDataDateTime.compact(value);
        return text || '—';
      }

      function formatDateFull(value) {
        const text = typeof QuicDataDateTime === 'undefined'
          ? ''
          : QuicDataDateTime.full(value);
        return text || '—';
      }

      function tableLayoutStorage() {
        return typeof localStorage === 'undefined' ? null : localStorage;
      }

      function tableLayoutScope(tableKey) {
        return {
          userId: user.value?.id,
          workspaceId: selectedWorkspaceId.value,
          tableKey,
        };
      }

      function tableColumnWidth(tableKey, columnKey, fallback) {
        void tableLayoutVersion.value;
        if (typeof QuicDataTableLayoutPreference === 'undefined') return fallback;
        return QuicDataTableLayoutPreference.resolveWidth(
          tableLayoutStorage(),
          tableLayoutScope(tableKey),
          columnKey,
          fallback,
        );
      }

      function onTableHeaderDrag(tableKey, newWidth, column) {
        if (typeof QuicDataTableLayoutPreference === 'undefined') return;
        const columnKey = QuicDataTableLayoutPreference.columnKey(column);
        if (!columnKey) return;
        if (QuicDataTableLayoutPreference.writeColumn(
          tableLayoutStorage(),
          tableLayoutScope(tableKey),
          columnKey,
          newWidth,
        )) tableLayoutVersion.value += 1;
      }

      function formatPacketTimestamp(value) {
        if (value === null || value === undefined || value === '') return '';
        const asNumber = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : Number(value);
        if (!Number.isFinite(asNumber) || asNumber <= 0) return String(value);
        const millis = asNumber > 1e15 ? asNumber / 1e6 : asNumber > 1e12 ? asNumber / 1e3 : asNumber;
        const parsed = new Date(millis);
        return Number.isNaN(parsed.valueOf()) ? String(value) : formatDate(parsed.toISOString());
      }

      function packetMetadataRows(meta) {
        if (!meta || typeof meta !== 'object') return [];
        const rows = [];
        const push = (key, labelKey, value) => {
          if (value === null || value === undefined || value === '') return;
          rows.push({ key, label: t(labelKey), value: String(value) });
        };
        push('qrdf_version', 'packetQrdfVersion', meta.qrdf_version);
        push('episode_id', 'packetSourceEpisode', meta.episode_id);
        push('data_file', 'packetDataFile', meta.data_file);
        push('task_name', 'packetTaskName', meta.task?.name);
        push('task_language', 'packetTaskLanguage', meta.task?.language);
        push('robot_name', 'packetRobotName', meta.robot?.name);
        if (Number.isFinite(Number(meta.robot?.num_arms))) push('robot_arms', 'packetRobotArms', meta.robot.num_arms);
        push('robot_base_frame', 'packetRobotBaseFrame', meta.robot?.base_frame);
        push('capture_mode', 'packetCaptureMode', meta.capture?.mode);
        push('episode_type', 'packetEpisodeType', meta.capture?.episode_type);
        if (meta.capture?.app) {
          const app = meta.capture.app_version ? `${meta.capture.app} · ${meta.capture.app_version}` : meta.capture.app;
          push('capture_app', 'packetCaptureApp', app);
        }
        push('reference_topic', 'packetReferenceTopic', meta.reference_topic);
        push('start_time', 'packetStartTime', formatPacketTimestamp(meta.timing?.start_timestamp_ns));
        push('end_time', 'packetEndTime', formatPacketTimestamp(meta.timing?.end_timestamp_ns));
        if (meta.timing?.duration_s != null) push('duration', 'duration', formatDuration(meta.timing.duration_s));
        if (meta.metrics?.reference_frame_count != null) push('frames', 'referenceFrames', formatNumber(meta.metrics.reference_frame_count));
        if (meta.metrics?.average_rgb_rate_hz != null) push('fps', 'frameRate', `${Number(meta.metrics.average_rgb_rate_hz).toFixed(2)} Hz`);
        return rows;
      }

      function hasPacketMetadata(meta) {
        if (!meta || typeof meta !== 'object') return false;
        return packetMetadataRows(meta).length > 0 || Boolean(meta.cameras?.length) || Boolean(meta.sensors?.length);
      }

      function cameraResolutionLabel(row) {
        if (!row) return '—';
        if (Number.isFinite(Number(row.width)) && Number.isFinite(Number(row.height))) return `${row.width}×${row.height}`;
        return '—';
      }

      function formatDashboardTime(value) {
        if (!value) return '—';
        const parsed = new Date(value);
        if (Number.isNaN(parsed.valueOf())) return '—';
        const pad = (n) => String(n).padStart(2, '0');
        return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
      }

      function parseNanoseconds(value) {
        try {
          if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'bigint') return null;
          const normalized = String(value).trim();
          if (!/^\d+$/.test(normalized)) return null;
          return BigInt(normalized);
        } catch {
          return null;
        }
      }

      function workbenchBounds() {
        const timeline = workbenchTimeline.value || activeWorkbench.value?.timeline;
        const start = parseNanoseconds(timeline?.start_ns);
        const end = parseNanoseconds(timeline?.end_ns);
        return start !== null && end !== null && end > start ? { start, end } : null;
      }

      function formatRelativeTimestamp(value) {
        const bounds = workbenchBounds();
        const timestamp = parseNanoseconds(value);
        if (!bounds || timestamp === null || timestamp < bounds.start) return '—';
        const milliseconds = Number((timestamp - bounds.start) / 1000000n);
        const hours = Math.floor(milliseconds / 3600000);
        const minutes = Math.floor((milliseconds % 3600000) / 60000);
        const seconds = Math.floor((milliseconds % 60000) / 1000);
        const fraction = milliseconds % 1000;
        const clock = `${String(minutes).padStart(hours ? 2 : 1, '0')}:${String(seconds).padStart(2, '0')}.${String(fraction).padStart(3, '0')}`;
        return hours ? `${hours}:${clock}` : clock;
      }

      function outcomeLabel(outcome) {
        return {
          success: t('outcomeSuccess'),
          failure: t('outcomeFailure'),
          unknown: t('outcomeUnknown'),
        }[outcome] || t('outcomeUnknown');
      }

      function nextSegmentId() {
        return `segment-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      }

      function cloneWorkbenchDraft(draft = workbenchDraft.value) {
        const source = draft || { mode: 'partitioned', segments: [], note: '' };
        if (typeof structuredClone === 'function') {
          try {
            return structuredClone(source);
          } catch {
            /* fall through to JSON clone */
          }
        }
        return JSON.parse(JSON.stringify(source));
      }

      function workbenchDraftSignature(draft = workbenchDraft.value) {
        const source = draft || {};
        const segments = Array.isArray(source.segments) ? source.segments : [];
        let signature = `${source.mode || ''}\0${source.note || ''}\0${source.outcome || ''}\0${source.rating || 0}\0${source.collector_profile_id || ''}\0${source.collection_device_id || ''}`;
        for (const segment of segments) {
          const boundary = segment?.boundary_after;
          signature += `\n${segment?.id || ''}\t${segment?.start_ns || ''}\t${segment?.end_ns || ''}\t${segment?.eligibility || ''}\t${segment?.exclusion_reason || ''}\t${segment?.description || ''}\t${boundary?.origin || ''}\t${boundary?.adjusted ? 1 : 0}`;
        }
        return signature;
      }

      function cancelScheduledWorkbenchHistory() {
        if (workbenchHistoryTimer !== null && typeof window !== 'undefined' && typeof window.clearTimeout === 'function') {
          window.clearTimeout(workbenchHistoryTimer);
        }
        workbenchHistoryTimer = null;
      }

      function resetWorkbenchHistory() {
        cancelScheduledWorkbenchHistory();
        workbenchHistory.past = [];
        workbenchHistory.future = [];
        workbenchHistory.current = cloneWorkbenchDraft();
        workbenchHistory.currentSig = workbenchDraftSignature(workbenchHistory.current);
        workbenchHistory.applying = false;
        workbenchHistory.ready = true;
        workbenchClipboard.value = null;
      }

      function recordWorkbenchHistory() {
        if (!workbenchHistory.ready || workbenchHistory.applying || !activeWorkbench.value) return;
        const next = cloneWorkbenchDraft();
        const nextSig = workbenchDraftSignature(next);
        if (nextSig === workbenchHistory.currentSig) return;
        if (workbenchHistory.current) workbenchHistory.past.push(workbenchHistory.current);
        const dense = (workbenchDraft.value.segments?.length || 0) > 80;
        const maxEntries = dense ? WORKBENCH_HISTORY_LIMIT_DENSE : WORKBENCH_HISTORY_LIMIT;
        if (workbenchHistory.past.length > maxEntries) workbenchHistory.past.shift();
        workbenchHistory.current = next;
        workbenchHistory.currentSig = nextSig;
        workbenchHistory.future = [];
      }

      function scheduleWorkbenchHistory() {
        if (!workbenchHistory.ready || workbenchHistory.applying || !activeWorkbench.value) return;
        cancelScheduledWorkbenchHistory();
        const capture = () => {
          workbenchHistoryTimer = null;
          recordWorkbenchHistory();
        };
        if (typeof window !== 'undefined' && typeof window.setTimeout === 'function') {
          workbenchHistoryTimer = window.setTimeout(capture, WORKBENCH_HISTORY_DEBOUNCE_MS);
        } else {
          capture();
        }
      }

      function flushWorkbenchHistory() {
        if (workbenchHistoryTimer === null) return;
        cancelScheduledWorkbenchHistory();
        recordWorkbenchHistory();
      }

      function restoreWorkbenchHistory(snapshot) {
        if (!snapshot) return;
        cancelScheduledWorkbenchHistory();
        workbenchHistory.applying = true;
        workbenchDraft.value = cloneWorkbenchDraft(snapshot);
        workbenchHistory.current = cloneWorkbenchDraft(snapshot);
        workbenchHistory.currentSig = workbenchDraftSignature(workbenchHistory.current);
        activeCutSegmentIndex.value = Math.max(0, Math.min(activeCutSegmentIndex.value, workbenchDraft.value.segments.length - 1));
        activeAnnotationSegmentIndex.value = Math.max(0, Math.min(activeAnnotationSegmentIndex.value, workbenchDraft.value.segments.length - 1));
        if (selectedCutBoundary.value !== null && selectedCutBoundary.value >= workbenchDraft.value.segments.length - 1) {
          selectedCutBoundary.value = null;
          cutLocalRangeAnchor.value = null;
        }
        markWorkbenchDraftDirty();
        nextTick(() => { workbenchHistory.applying = false; });
      }

      function undoWorkbench() {
        flushWorkbenchHistory();
        if (!workbenchHistory.past.length) return;
        workbenchHistory.future.push(cloneWorkbenchDraft(workbenchHistory.current));
        restoreWorkbenchHistory(workbenchHistory.past.pop());
      }

      function redoWorkbench() {
        flushWorkbenchHistory();
        if (!workbenchHistory.future.length) return;
        workbenchHistory.past.push(cloneWorkbenchDraft(workbenchHistory.current));
        restoreWorkbenchHistory(workbenchHistory.future.pop());
      }

      function copyWorkbenchSelection() {
        if (!activeWorkbench.value?.capabilities?.annotation) return;
        const segment = workbenchDraft.value.segments[activeAnnotationSegmentIndex.value];
        if (segment) workbenchClipboard.value = { kind: 'annotation_segment', segment: cloneWorkbenchDraft(segment) };
      }

      function pasteWorkbenchSelection() {
        const clipboard = workbenchClipboard.value;
        if (!clipboard) return;
        if (activeWorkbench.value?.capabilities?.annotation && clipboard.kind === 'annotation_segment') {
          const source = cloneWorkbenchDraft(clipboard.segment);
          const originalStart = parseNanoseconds(source.start_ns);
          const originalEnd = parseNanoseconds(source.end_ns);
          const current = parseNanoseconds(currentPlaybackTimestamp());
          const bounds = workbenchBounds();
          if (originalStart === null || originalEnd === null || current === null || !bounds) return;
          const duration = originalEnd - originalStart;
          if (duration <= 0n) return;
          const legalStart = closestAnnotationSegmentStart(
            current,
            annotationSegmentStartRanges(null, duration, bounds),
          );
          if (legalStart === null) {
            ElMessage.warning(t('annotationNoRoom'));
            return;
          }
          source.id = nextSegmentId();
          source.start_ns = legalStart.toString();
          source.end_ns = (legalStart + duration).toString();
          workbenchDraft.value.segments.push(source);
          sortAnnotationSegmentsByStart();
          selectAnnotationSegment(workbenchDraft.value.segments.indexOf(source));
          markWorkbenchDraftDirty();
        }
      }

      function normalizedCutBoundary(boundary) {
        if (!boundary || typeof boundary !== 'object') return undefined;
        if (boundary.origin === 'human') return { origin: 'human' };
        if (boundary.origin !== 'qr_event') return undefined;
        return {
          origin: 'qr_event',
          suggested_timestamp_ns: String(boundary.suggested_timestamp_ns || ''),
          adjusted: boundary.adjusted === true,
          segment_id_hint: String(boundary.segment_id_hint || ''),
          segment_index: Number(boundary.segment_index),
          protocol_version: Number(boundary.protocol_version),
        };
      }

      function normalizedWorkbenchDraft(snapshot) {
        const payload = snapshot?.draft?.payload;
        const segments = Array.isArray(payload?.segments) ? payload.segments : [];
        return {
          mode: payload?.mode === 'whole' ? 'whole' : 'partitioned',
          segments: segments.map((segment) => {
            const result = {
              id: typeof segment?.id === 'string' && segment.id ? segment.id : nextSegmentId(),
              start_ns: String(segment?.start_ns || ''),
              end_ns: String(segment?.end_ns || ''),
              description: typeof segment?.description === 'string' ? segment.description : '',
              behavior_tag_ids: Array.isArray(segment?.behavior_tag_ids) ? [...segment.behavior_tag_ids] : undefined,
              eligibility: segment?.eligibility === 'excluded' ? 'excluded' : 'included',
            };
            if (['off_task', 'idle_or_setup', 'privacy_sensitive', 'other'].includes(segment?.exclusion_reason)) {
              result.exclusion_reason = segment.exclusion_reason;
            }
            const boundary = normalizedCutBoundary(segment?.boundary_after);
            if (boundary) result.boundary_after = boundary;
            return result;
          }),
          note: typeof payload?.note === 'string' ? payload.note : '',
          outcome: ['success', 'failure', 'unknown'].includes(payload?.outcome) ? payload.outcome : 'unknown',
          rating: Number.isInteger(payload?.rating) && payload.rating >= 1 && payload.rating <= 5 ? payload.rating : 0,
        };
      }

      function previewPlaybackTimeline() {
        return activeWorkbench.value?.media?.preview?.playback_timeline || null;
      }

      function exactPlaybackTimelineAvailable() {
        return QuicDataPreviewTimeline.sourceTimestampAt(0, previewPlaybackTimeline()) !== null;
      }

      function currentPlaybackTimestamp() {
        const seconds = Number(workbenchVideo.value?.currentTime || 0);
        return QuicDataPreviewTimeline.sourceTimestampAt(seconds, previewPlaybackTimeline());
      }

      function cutBoundsPayload() {
        const bounds = workbenchBounds();
        return bounds ? { start_ns: bounds.start.toString(), end_ns: bounds.end.toString() } : null;
      }

      function initializeCutDraft(snapshot) {
        const bounds = cutBoundsPayload();
        if (!bounds) return;
        const suggestions = snapshot?.timeline?.boundary_suggestions || [];
        workbenchDraft.value.mode = 'partitioned';
        workbenchDraft.value.segments = QuicDataCutWorkbench.initialize(bounds, suggestions);
        selectedCutBoundary.value = null;
        cutLocalRangeAnchor.value = null;
        activeCutSegmentIndex.value = 0;
        nextTick(() => scheduleCutTimelinePaint());
      }

      function useWholeEpisodeCut() {
        const bounds = cutBoundsPayload();
        if (!bounds) return;
        workbenchDraft.value.mode = 'whole';
        workbenchDraft.value.segments = QuicDataCutWorkbench.initialize(bounds, []);
        selectedCutBoundary.value = null;
        cutLocalRangeAnchor.value = null;
        activeCutSegmentIndex.value = 0;
        markWorkbenchDraftDirty();
      }

      function addCutBoundary() {
        const timestamp = currentPlaybackTimestamp();
        if (!timestamp) {
          ElMessage.warning(t('timelineMappingUnavailable'));
          return;
        }
        const next = QuicDataCutWorkbench.insertBoundary(workbenchDraft.value.segments, timestamp);
        if (!next) return;
        workbenchDraft.value.mode = 'partitioned';
        workbenchDraft.value.segments = next;
        selectCutBoundary(next.findIndex((segment) => segment.end_ns === timestamp), { recenter: true });
        markWorkbenchDraftDirty();
      }

      function deleteSelectedCutBoundary() {
        if (selectedCutBoundary.value === null) return;
        const index = Number(selectedCutBoundary.value);
        if (!Number.isInteger(index)) return;
        const next = QuicDataCutWorkbench.removeBoundary(workbenchDraft.value.segments, index);
        if (!next) return;
        workbenchDraft.value.segments = next;
        selectedCutBoundary.value = null;
        cutLocalRangeAnchor.value = null;
        activeCutSegmentIndex.value = Math.min(activeCutSegmentIndex.value, next.length - 1);
        markWorkbenchDraftDirty();
      }

      function restoreSelectedQrBoundary() {
        if (selectedCutBoundary.value === null) return;
        const index = Number(selectedCutBoundary.value);
        if (!Number.isInteger(index)) return;
        const next = QuicDataCutWorkbench.restoreQrBoundary(workbenchDraft.value.segments, index);
        if (!next) return;
        workbenchDraft.value.segments = next;
        selectCutBoundary(index, { recenter: true });
        markWorkbenchDraftDirty();
      }

      function selectCutBoundary(index, { recenter = true } = {}) {
        const normalized = Number(index);
        const segments = workbenchDraft.value.segments;
        if (!Number.isInteger(normalized) || normalized < 0 || normalized >= segments.length - 1) return;
        const derivedRange = QuicDataCutWorkbench.localRange(segments, normalized, cutBoundsPayload(), 15);
        const timestamp = parseNanoseconds(segments[normalized]?.end_ns);
        const anchorStart = parseNanoseconds(cutLocalRangeAnchor.value?.start_ns);
        const anchorEnd = parseNanoseconds(cutLocalRangeAnchor.value?.end_ns);
        const outsideAnchor = timestamp === null || anchorStart === null || anchorEnd === null
          || timestamp < anchorStart || timestamp > anchorEnd;
        if (derivedRange && (recenter || outsideAnchor)) {
          cutLocalRangeAnchor.value = { start_ns: derivedRange.start_ns, end_ns: derivedRange.end_ns };
        }
        selectedCutBoundary.value = normalized;
        activeCutSegmentIndex.value = normalized;
        nextTick(() => {
          scrollCutListToIndex(normalized);
          scheduleCutTimelinePaint();
        });
      }

      function selectNearestCutBoundary() {
        const timestamp = currentPlaybackTimestamp();
        const index = QuicDataCutWorkbench.nearestBoundaryIndex(workbenchDraft.value.segments, timestamp);
        if (index < 0) return;
        selectCutBoundary(index, { recenter: true });
        seekWorkbenchToTimestamp(workbenchDraft.value.segments[index].end_ns);
      }

      function selectCutSegment(index, { seek = false } = {}) {
        const normalized = Math.max(0, Math.min(Number(index), workbenchDraft.value.segments.length - 1));
        if (!Number.isInteger(normalized) || !workbenchDraft.value.segments[normalized]) return;
        activeCutSegmentIndex.value = normalized;
        scrollCutListToIndex(normalized);
        scheduleCutTimelinePaint();
        if (seek) seekWorkbenchToTimestamp(workbenchDraft.value.segments[normalized].start_ns);
      }

      function setCutEligibility(index, eligibility) {
        const next = QuicDataCutWorkbench.setEligibility(workbenchDraft.value.segments, index, eligibility);
        if (!next) return;
        workbenchDraft.value.segments = next;
        activeCutSegmentIndex.value = Number(index);
        markWorkbenchDraftDirty();
      }

      function setCutExclusionReason(index, reason) {
        const next = QuicDataCutWorkbench.setExclusionReason(workbenchDraft.value.segments, index, reason || '');
        if (!next) return;
        workbenchDraft.value.segments = next;
        markWorkbenchDraftDirty();
      }

      function cutBoundaryOrigin(index) {
        const boundary = workbenchDraft.value.segments[index]?.boundary_after;
        if (boundary?.origin === 'qr_event') return boundary.adjusted ? t('boundaryAdjusted') : t('boundaryQrEvent');
        return t('boundaryHuman');
      }

      function cutSegmentBoundaryLabel(segment, index) {
        if (index >= workbenchDraft.value.segments.length - 1) return t('boundaryHuman');
        const boundary = segment?.boundary_after;
        if (boundary?.origin === 'qr_event') return boundary.adjusted ? t('boundaryAdjusted') : t('boundaryQrEvent');
        return t('boundaryHuman');
      }

      function exclusionReasonLabel(reason) {
        return {
          off_task: t('reasonOffTask'),
          idle_or_setup: t('reasonIdleOrSetup'),
          privacy_sensitive: t('reasonPrivacySensitive'),
          other: t('reasonOther'),
        }[reason] || '';
      }

      function timestampPercent(value) {
        const bounds = workbenchBounds();
        const timestamp = parseNanoseconds(value);
        if (!bounds || timestamp === null || timestamp < bounds.start || timestamp > bounds.end) return 0;
        return Math.max(0, Math.min(100, Number(timestamp - bounds.start) / Number(bounds.end - bounds.start) * 100));
      }

      function cutSegmentStyle(segment) {
        const left = timestampPercent(segment?.start_ns);
        const right = timestampPercent(segment?.end_ns);
        return { left: `${left}%`, width: `${Math.max(0, right - left)}%` };
      }

      function cutBoundaryStyle(index) {
        return { left: `${timestampPercent(workbenchDraft.value.segments[index]?.end_ns)}%` };
      }

      function cutPlayheadStyle() {
        return { left: `${timestampPercent(cutPlayheadTimestamp.value)}%` };
      }

      function selectedCutLocalRange() {
        const derivedRange = QuicDataCutWorkbench.localRange(
          workbenchDraft.value.segments,
          selectedCutBoundary.value,
          cutBoundsPayload(),
          15,
        );
        if (!derivedRange) return null;
        const timestamp = parseNanoseconds(derivedRange.boundary_ns);
        const anchorStart = parseNanoseconds(cutLocalRangeAnchor.value?.start_ns);
        const anchorEnd = parseNanoseconds(cutLocalRangeAnchor.value?.end_ns);
        if (timestamp === null || anchorStart === null || anchorEnd === null || timestamp < anchorStart || timestamp > anchorEnd) {
          return derivedRange;
        }
        return {
          start_ns: anchorStart.toString(),
          end_ns: anchorEnd.toString(),
          boundary_ns: timestamp.toString(),
        };
      }

      function cutLocalBoundaries() {
        return QuicDataCutWorkbench.localBoundaries(
          workbenchDraft.value.segments,
          selectedCutLocalRange(),
          selectedCutBoundary.value,
        );
      }

      function localTimestampPercent(value) {
        const range = selectedCutLocalRange();
        const timestamp = parseNanoseconds(value);
        const start = parseNanoseconds(range?.start_ns);
        const end = parseNanoseconds(range?.end_ns);
        if (timestamp === null || start === null || end === null || end <= start) return 0;
        return Math.max(0, Math.min(100, Number(timestamp - start) / Number(end - start) * 100));
      }

      function cutLocalBoundaryStyle(boundary) {
        return { left: `${localTimestampPercent(boundary?.timestamp_ns)}%` };
      }

      function cutLocalPlayheadStyle() {
        return { left: `${localTimestampPercent(cutPlayheadTimestamp.value)}%` };
      }

      function cutLocalPlayheadVisible() {
        const range = selectedCutLocalRange();
        const timestamp = parseNanoseconds(cutPlayheadTimestamp.value);
        const start = parseNanoseconds(range?.start_ns);
        const end = parseNanoseconds(range?.end_ns);
        return timestamp !== null && start !== null && end !== null && timestamp >= start && timestamp <= end;
      }

      function cutWindowDuration(segment) {
        const start = parseNanoseconds(segment?.start_ns);
        const end = parseNanoseconds(segment?.end_ns);
        return start === null || end === null || end <= start ? '—' : formatDuration(Number(end - start) / 1e9);
      }

      function cutSegmentTooLong(segment) {
        const start = parseNanoseconds(segment?.start_ns);
        const end = parseNanoseconds(segment?.end_ns);
        return segment?.eligibility !== 'excluded' && start !== null && end !== null && end - start > 300000000000n;
      }

      function cutDraftSaveValid() {
        return cutDraftValidity().save;
      }

      function cutDraftSubmitValid() {
        return cutDraftValidity().submit;
      }

      function cutDraftValidity() {
        const segments = workbenchDraft.value.segments;
        const bounds = cutBoundsPayload();
        if (draggingCutBoundary && cutValidityCache.segments) return cutValidityCache;
        if (
          cutValidityCache.segments === segments
          && cutValidityCache.boundsStart === (bounds?.start_ns || '')
          && cutValidityCache.boundsEnd === (bounds?.end_ns || '')
        ) {
          return cutValidityCache;
        }
        const save = QuicDataCutWorkbench.validateForSave(segments, bounds);
        const submit = save && QuicDataCutWorkbench.validateForSubmit(segments, bounds);
        cutValidityCache = {
          segments,
          boundsStart: bounds?.start_ns || '',
          boundsEnd: bounds?.end_ns || '',
          save,
          submit,
        };
        return cutValidityCache;
      }

      function sourceTimestampFromPointer(event, rangeStart, rangeEnd, minimum, maximum, track) {
        if (!track || !exactPlaybackTimelineAvailable()) return null;
        const start = parseNanoseconds(rangeStart);
        const end = parseNanoseconds(rangeEnd);
        if (start === null || end === null || end <= start) return null;
        const rect = track.getBoundingClientRect();
        if (!rect.width) return null;
        const ratio = Math.max(0, Math.min(1, (Number(event.clientX) - rect.left) / rect.width));
        const scale = 1000000n;
        const target = start + (end - start) * BigInt(Math.round(ratio * Number(scale))) / scale;
        return QuicDataPreviewTimeline.nearestSourceTimestamp(
          target.toString(),
          previewPlaybackTimeline(),
          String(minimum),
          String(maximum),
        );
      }

      function snapTimestampToWorkbenchPoint(timestamp, toleranceSeconds = 0.35) {
        const bounds = workbenchBounds();
        const value = parseNanoseconds(timestamp);
        if (!timelineSnapEnabled.value || !bounds || value === null) return timestamp;
        const index = QuicDataCutWorkbench.nearestBoundaryIndex(workbenchDraft.value.segments, timestamp);
        if (index < 0) return timestamp;
        const boundary = parseNanoseconds(workbenchDraft.value.segments[index]?.end_ns);
        if (boundary === null) return timestamp;
        const tolerance = BigInt(Math.round(Math.max(0, toleranceSeconds) * 1e9));
        const distance = boundary > value ? boundary - value : value - boundary;
        return distance <= tolerance ? boundary.toString() : timestamp;
      }

      function applyPlayheadDom(timestamp) {
        const left = `${timestampPercent(timestamp)}%`;
        if (cutPlayheadLine.value) cutPlayheadLine.value.style.left = left;
        if (cutPlayheadPin.value) {
          cutPlayheadPin.value.style.left = left;
          cutPlayheadPin.value.setAttribute('aria-label', formatRelativeTimestamp(timestamp));
        }
        if (timelineTimecode.value) timelineTimecode.value.textContent = formatRelativeTimestamp(timestamp);
      }

      function syncWorkbenchPlayhead() {
        const timestamp = currentPlaybackTimestamp();
        applyPlayheadDom(timestamp);
        if (cutPlayheadTimestamp.value === null || timestamp === null) cutPlayheadTimestamp.value = timestamp;
        const seconds = Number(workbenchVideo.value?.currentTime);
        if (Number.isFinite(seconds)) lastWorkbenchPlaybackSeconds.value = seconds;
        const playing = Boolean(workbenchVideo.value && !workbenchVideo.value.paused && !workbenchVideo.value.ended);
        if (workbenchPlaying.value !== playing) workbenchPlaying.value = playing;
      }

      async function toggleWorkbenchPlayback() {
        const video = workbenchVideo.value;
        if (!video) return;
        if (video.paused || video.ended) {
          const resumeTime = Number.isFinite(lastWorkbenchPlaybackSeconds.value) ? lastWorkbenchPlaybackSeconds.value : 0;
          const duration = Number(video.duration || 0);
          const safeResumeTime = duration > 0 && resumeTime > 0 && resumeTime < duration ? resumeTime : 0;
          if (video.ended) video.currentTime = safeResumeTime;
          else if (video.currentTime === 0 && safeResumeTime > 0) video.currentTime = safeResumeTime;
          try {
            await video.play();
          } catch (error) {
            errorMessage(error);
          }
        } else {
          const current = Number(video.currentTime);
          if (Number.isFinite(current)) lastWorkbenchPlaybackSeconds.value = current;
          video.pause();
        }
        syncWorkbenchPlayhead();
      }

      function toggleWorkbenchMute() {
        const video = workbenchVideo.value;
        if (!video) return;
        video.muted = !video.muted;
        workbenchMuted.value = video.muted;
      }

      function setWorkbenchVolume(value) {
        const video = workbenchVideo.value;
        const volume = Math.max(0, Math.min(1, Number(value)));
        if (!video || !Number.isFinite(volume)) return;
        video.volume = volume;
        video.muted = volume === 0;
        workbenchVolume.value = volume;
        workbenchMuted.value = video.muted;
      }

      function setWorkbenchPlaybackRate(value) {
        const video = workbenchVideo.value;
        const rate = Number(value);
        if (!video || ![0.5, 1, 1.5, 2, 3, 4, 6, 8].includes(rate)) return;
        video.playbackRate = rate;
        workbenchPlaybackRate.value = rate;
      }

      async function openWorkbenchFullscreen() {
        const target = typeof document !== 'undefined'
          ? document.querySelector('.workbench-studio')
          : workbenchVideo.value?.parentElement || workbenchVideo.value;
        if (target?.requestFullscreen) await target.requestFullscreen();
      }

      function setTimelineZoom(value) {
        const next = Math.max(1, Math.min(12, Number(value)));
        if (Number.isFinite(next)) timelineZoom.value = next;
        nextTick(() => {
          syncTimelineViewRange();
          scheduleCutTimelinePaint();
        });
      }

      function zoomWorkbenchTimeline(direction) {
        setTimelineZoom(timelineZoom.value + Number(direction));
      }

      function timelineCanvasStyle() {
        return { width: `${timelineZoom.value * 100}%`, minWidth: '100%' };
      }

      function timelineTickStep(durationSeconds, zoom) {
        const targetMajorTicks = Math.max(6, Math.min(24, Math.round(6 + zoom * 1.6)));
        const raw = durationSeconds / targetMajorTicks;
        const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
        const major = steps.find((step) => step >= raw) || 600;
        const minor = zoom >= 10 ? major / 10 : zoom >= 7 ? major / 5 : zoom >= 4 ? major / 2 : major;
        return { major, minor: Math.max(1 / 30, minor) };
      }

      function timelineTicks() {
        const bounds = workbenchBounds();
        const durationSeconds = Number(activeWorkbench.value?.timeline?.duration_s || 0);
        const zoom = timelineZoom.value;
        const key = `${bounds?.start || ''}:${bounds?.end || ''}:${durationSeconds}:${zoom}`;
        if (ticksCache.key === key) return ticksCache.ticks;
        if (!bounds || !Number.isFinite(durationSeconds) || durationSeconds <= 0) {
          ticksCache = { key, ticks: [] };
          return ticksCache.ticks;
        }
        const { major, minor } = timelineTickStep(durationSeconds, zoom);
        const maxTicks = 1600;
        const effectiveMinor = durationSeconds / minor > maxTicks ? durationSeconds / maxTicks : minor;
        const ticks = [];
        for (let seconds = 0; seconds <= durationSeconds + effectiveMinor / 2; seconds += effectiveMinor) {
          const normalizedSeconds = Math.min(durationSeconds, seconds);
          const isMajor = Math.abs(normalizedSeconds / major - Math.round(normalizedSeconds / major)) < 0.0001 || normalizedSeconds === 0 || normalizedSeconds === durationSeconds;
          ticks.push({
            key: `${Math.round(normalizedSeconds * 1000)}`,
            left: `${durationSeconds ? (normalizedSeconds / durationSeconds) * 100 : 0}%`,
            label: isMajor ? formatDuration(normalizedSeconds) : '',
            major: isMajor,
          });
        }
        ticksCache = { key, ticks };
        return ticks;
      }

      function timelineIcon(name) {
        return {
          play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
          pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>',
          back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 7 5 12l5 5V7zm2 0h2v10h-2V7zm4 0h2v10h-2V7z"/></svg>',
          forward: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 7v10l5-5-5-5zm-4 0h2v10h-2V7zM6 7h2v10H6V7z"/></svg>',
          backLarge: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 6 4 12l7 6V6zm1 6 7-6v12l-7-6z"/></svg>',
          forwardLarge: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m13 6 7 6-7 6V6zm-1 6-7 6V6l7 6z"/></svg>',
          mute: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4zm12.5 1.5L19 13l2.5-2.5 1.5 1.5-2.5 2.5L23 17l-1.5 1.5L19 16l-2.5 2.5L15 17l2.5-2.5L15 12l1.5-1.5z"/></svg>',
          volume: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4zm12.5-.5a5 5 0 0 1 0 7l-1.4-1.4a3 3 0 0 0 0-4.2l1.4-1.4z"/></svg>',
          add: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6z"/></svg>',
          select: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4a8 8 0 0 0-8 8H2l3 4 3-4H6a6 6 0 1 1 6 6v2a8 8 0 0 0 0-16zm-1 7H8v2h3v3h2v-3h3v-2h-3V8h-2v3z"/></svg>',
          restore: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7V4H5v7h7V9H8.4A6 6 0 1 1 12 18v2A8 8 0 1 0 7 7z"/></svg>',
          trash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4h8l1 2h4v2H3V6h4l1-2zm1 6h2v8H9v-8zm4 0h2v8h-2v-8zM6 10h12l-1 10H7L6 10z"/></svg>',
          undo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7V4H5v8h8v-2H8.7A5.5 5.5 0 1 1 13 19v2A7.5 7.5 0 1 0 7 7z"/></svg>',
          redo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 7V4h2v8h-8v-2h4.3A5.5 5.5 0 1 0 11 19v2a7.5 7.5 0 1 1 6-14z"/></svg>',
          copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 7h10v13H8V7zm-3-3h10v2H7v11H5V4z"/></svg>',
          paste: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4h6l1 2h3v15H5V6h3l1-2zm1 4H7v11h10V8h-3v2h-4V8z"/></svg>',
          zoomOut: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 4a6 6 0 1 0 3.7 10.7L19 20l1-1-5.3-5.3A6 6 0 0 0 10 4zm-3 5h6v2H7V9z"/></svg>',
          zoomIn: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 4a6 6 0 1 0 3.7 10.7L19 20l1-1-5.3-5.3A6 6 0 0 0 10 4zm-1 5V7h2v2h2v2h-2v2H9v-2H7V9h2z"/></svg>',
          fullscreen: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h6v2H7v4H5V5zm8 0h6v6h-2V7h-4V5zM7 13v4h4v2H5v-6h2zm10 0h2v6h-6v-2h4v-4z"/></svg>',
          keyboard: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zm1 3v2h2V8H5zm4 0v2h2V8H9zm4 0v2h2V8h-2zm4 0v2h2V8h-2zM5 12v2h10v-2H5zm12 0v2h2v-2h-2zm-8 4v1h6v-1H9z"/></svg>',
        }[name] || '';
      }

      function handleTimelineWheel(event) {
        if (event.shiftKey) {
          event.preventDefault();
          event.currentTarget.scrollLeft += Math.abs(event.deltaY) > Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
          return;
        }
        if (!event.ctrlKey && !event.metaKey && Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
        event.preventDefault();
        const viewport = event.currentTarget;
        const rect = viewport?.getBoundingClientRect?.();
        const rawDelta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
        if (!Number.isFinite(rawDelta) || rawDelta === 0) return;
        timelineWheelZoomDebt += rawDelta;
        const threshold = event.ctrlKey || event.metaKey ? 240 : 360;
        if (Math.abs(timelineWheelZoomDebt) < threshold) return;
        const zoomStep = timelineWheelZoomDebt < 0 ? 1 : -1;
        timelineWheelZoomDebt = 0;
        if (!rect?.width) {
          zoomWorkbenchTimeline(zoomStep);
          return;
        }
        const oldZoom = timelineZoom.value;
        const nextZoom = Math.max(1, Math.min(12, oldZoom + zoomStep));
        if (nextZoom === oldZoom) return;
        const anchorX = Number(event.clientX) - rect.left;
        const anchorRatio = (viewport.scrollLeft + anchorX) / Math.max(1, viewport.scrollWidth);
        timelineZoom.value = nextZoom;
        nextTick(() => {
          viewport.scrollLeft = Math.max(0, anchorRatio * viewport.scrollWidth - anchorX);
          syncTimelineViewRange();
          scheduleCutTimelinePaint();
        });
      }

      function applyHoverDom(pageX, pageY, timestamp, playbackSeconds) {
        const preview = timelineHoverPreview.value;
        if (preview) {
          preview.style.left = `${pageX}px`;
          preview.style.top = `${pageY}px`;
        }
        if (timelineHoverLabel.value) timelineHoverLabel.value.textContent = formatRelativeTimestamp(timestamp);
        if (hoverSeekRaf && typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
          window.cancelAnimationFrame(hoverSeekRaf);
        }
        const seconds = playbackSeconds;
        hoverSeekRaf = typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function'
          ? window.requestAnimationFrame(() => {
            hoverSeekRaf = 0;
            if (timelineHoverVideo.value && Number.isFinite(seconds)) timelineHoverVideo.value.currentTime = seconds;
          })
          : 0;
        if (!hoverSeekRaf && timelineHoverVideo.value && Number.isFinite(seconds)) {
          timelineHoverVideo.value.currentTime = seconds;
        }
      }

      function updateTimelineHover(event) {
        const track = cutTimelineTrack.value;
        const bounds = workbenchBounds();
        if (!track || !bounds) return;
        const rect = track.getBoundingClientRect();
        if (!rect.width) return;
        timelineHover.left = Math.max(0, Math.min(rect.width, Number(event.clientX) - rect.left));
        const ratio = Math.max(0, Math.min(1, (Number(event.clientX) - rect.left) / rect.width));
        const timestamp = bounds.start + (bounds.end - bounds.start) * BigInt(Math.round(ratio * 1000000)) / 1000000n;
        const playbackSeconds = QuicDataPreviewTimeline.playbackSecondsAt(timestamp.toString(), previewPlaybackTimeline());
        const pageX = Math.max(92, Math.min((typeof window !== 'undefined' ? window.innerWidth : rect.right) - 92, Number(event.clientX)));
        const pageY = Math.max(92, rect.top - 12);
        applyHoverDom(pageX, pageY, timestamp.toString(), playbackSeconds === null ? 0 : playbackSeconds);
        if (!timelineHover.visible) {
          timelineHover.visible = true;
          nextTick(() => applyHoverDom(pageX, pageY, timestamp.toString(), playbackSeconds === null ? 0 : playbackSeconds));
        }
      }

      function clearTimelineHover() {
        timelineHover.visible = false;
      }

      function cutTrackSegments() {
        if (activeWorkbench.value?.capabilities?.review) {
          return activeWorkbench.value.review_target?.payload?.segments || [];
        }
        return workbenchDraft.value.segments || [];
      }

      function timelineVisibleNsRange() {
        const bounds = workbenchBounds();
        if (!bounds) return null;
        const span = bounds.end - bounds.start;
        const scale = 1000000n;
        const startRatio = Math.max(0, Math.min(1, Number(timelineViewRange.value.startRatio) || 0));
        const endRatio = Math.max(startRatio, Math.min(1, Number(timelineViewRange.value.endRatio) || 1));
        return {
          startNs: bounds.start + span * BigInt(Math.round(startRatio * Number(scale))) / scale,
          endNs: bounds.start + span * BigInt(Math.round(endRatio * Number(scale))) / scale,
        };
      }

      function cutTimelineVisibleCount() {
        const segments = cutTrackSegments();
        const range = timelineVisibleNsRange();
        if (!segments.length) return 0;
        const overlap = range
          ? QuicDataCutWorkbench.overlappingRange(segments, range.startNs.toString(), range.endNs.toString(), 0)
          : { startIndex: 0, endIndex: segments.length };
        return overlap.endIndex - overlap.startIndex;
      }

      function shouldPaintCutTimelineCanvas() {
        return cutTimelineVisibleCount() > CUT_TIMELINE_DOM_LIMIT;
      }

      function syncTimelineViewRange() {
        const viewport = timelineScrollViewport.value;
        if (!viewport || !viewport.scrollWidth) {
          timelineViewRange.value = { startRatio: 0, endRatio: 1 };
          return;
        }
        const startRatio = viewport.scrollLeft / viewport.scrollWidth;
        const endRatio = (viewport.scrollLeft + viewport.clientWidth) / viewport.scrollWidth;
        timelineViewRange.value = { startRatio, endRatio };
      }

      function onTimelineScroll() {
        syncTimelineViewRange();
        scheduleCutTimelinePaint();
      }

      function onCutListScroll(event) {
        pendingCutListScrollTop = event.currentTarget.scrollTop;
        if (cutListScrollRaf) return;
        const apply = () => {
          cutListScrollRaf = 0;
          cutListScrollTop.value = pendingCutListScrollTop;
        };
        if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
          cutListScrollRaf = window.requestAnimationFrame(apply);
        } else {
          apply();
        }
      }

      function onAnnotationListScroll(event) {
        pendingAnnotationListScrollTop = event.currentTarget.scrollTop;
        if (annotationListScrollRaf) return;
        const apply = () => {
          annotationListScrollRaf = 0;
          annotationListScrollTop.value = pendingAnnotationListScrollTop;
        };
        if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
          annotationListScrollRaf = window.requestAnimationFrame(apply);
        } else {
          apply();
        }
      }

      function scrollCutListToIndex(index) {
        const viewport = cutListViewport.value;
        if (!viewport || !Number.isInteger(index) || index < 0) return;
        const top = index * CUT_WINDOW_ROW_STRIDE;
        if (top < viewport.scrollTop || top + CUT_WINDOW_ROW_STRIDE > viewport.scrollTop + viewport.clientHeight) {
          viewport.scrollTop = Math.max(0, top - Math.max(0, viewport.clientHeight - CUT_WINDOW_ROW_STRIDE) / 3);
        }
        cutListScrollTop.value = viewport.scrollTop;
      }

      function scrollAnnotationListToIndex(index) {
        const viewport = annotationListViewport.value;
        if (!viewport || !Number.isInteger(index) || index < 0) return;
        const top = index * ANNOTATION_SEGMENT_ROW_STRIDE;
        if (top < viewport.scrollTop || top + ANNOTATION_SEGMENT_ROW_STRIDE > viewport.scrollTop + viewport.clientHeight) {
          viewport.scrollTop = Math.max(0, top - Math.max(0, viewport.clientHeight - ANNOTATION_SEGMENT_ROW_STRIDE) / 3);
        }
        annotationListScrollTop.value = viewport.scrollTop;
      }

      function paintCutTimelineCanvas() {
        const canvas = cutTimelineCanvas.value;
        const track = cutTimelineTrack.value;
        if (!canvas || !track) return;
        const dense = shouldPaintCutTimelineCanvas();
        canvas.style.display = dense ? 'block' : 'none';
        if (!dense) return;
        const bounds = workbenchBounds();
        const segments = cutTrackSegments();
        const width = Math.max(1, track.clientWidth);
        const height = Math.max(1, track.clientHeight);
        const dpr = typeof window !== 'undefined' ? Number(window.devicePixelRatio) || 1 : 1;
        const pixelWidth = Math.round(width * dpr);
        const pixelHeight = Math.round(height * dpr);
        if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
          canvas.width = pixelWidth;
          canvas.height = pixelHeight;
          canvas.style.width = `${width}px`;
          canvas.style.height = `${height}px`;
        }
        const ctx = canvas.getContext('2d');
        if (!ctx || !bounds || !segments.length) {
          ctx?.clearRect(0, 0, width, height);
          return;
        }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, height);
        const span = Number(bounds.end - bounds.start);
        if (!Number.isFinite(span) || span <= 0) return;
        const top = 6;
        const barHeight = Math.max(4, height - 12);
        const selected = activeWorkbench.value?.capabilities?.cut
          ? activeCutSegmentIndex.value
          : activeAnnotationSegmentIndex.value;
        const whole = activeWorkbench.value?.capabilities?.review
          ? activeWorkbench.value.review_target?.payload?.mode === 'whole'
          : activeWorkbench.value?.capabilities?.annotation
            ? false
            : workbenchDraft.value.mode === 'whole';
        const visibleRange = timelineVisibleNsRange();
        const visible = visibleRange
          ? QuicDataCutWorkbench.overlappingRange(
            segments,
            visibleRange.startNs.toString(),
            visibleRange.endNs.toString(),
            1,
          )
          : { startIndex: 0, endIndex: segments.length };
        const startIndex = Math.max(0, Math.min(segments.length, visible.startIndex));
        const endIndex = Math.max(startIndex, Math.min(segments.length, visible.endIndex));
        for (let index = startIndex; index < endIndex; index += 1) {
          const start = parseNanoseconds(segments[index]?.start_ns);
          const end = parseNanoseconds(segments[index]?.end_ns);
          if (start === null || end === null || end <= start) continue;
          const x = Number(start - bounds.start) / span * width;
          const w = Math.max(1, Number(end - start) / span * width);
          const excluded = segments[index]?.eligibility === 'excluded';
          const tooLong = !excluded && end - start > 300000000000n;
          ctx.fillStyle = excluded ? '#edf0f4' : tooLong ? '#fee2e2' : whole ? '#dff3e7' : '#dce8ff';
          ctx.fillRect(x, top, w, barHeight);
          if (excluded) {
            ctx.save();
            ctx.beginPath();
            ctx.rect(x, top, w, barHeight);
            ctx.clip();
            ctx.fillStyle = '#dfe4ea';
            for (let stripe = x - barHeight; stripe < x + w + barHeight; stripe += 12) {
              ctx.beginPath();
              ctx.moveTo(stripe, top);
              ctx.lineTo(stripe + 6, top);
              ctx.lineTo(stripe + 6 - barHeight, top + barHeight);
              ctx.lineTo(stripe - barHeight, top + barHeight);
              ctx.closePath();
              ctx.fill();
            }
            ctx.restore();
          }
          ctx.strokeStyle = excluded ? '#aab3bf' : tooLong ? '#db7474' : whole ? '#8fc3a1' : '#8eabe0';
          ctx.lineWidth = 1;
          ctx.strokeRect(x + 0.5, top + 0.5, Math.max(0, w - 1), Math.max(0, barHeight - 1));
          if (index === selected) {
            ctx.strokeStyle = '#315caa';
            ctx.lineWidth = 2;
            ctx.strokeRect(x + 1, top + 1, Math.max(0, w - 2), Math.max(0, barHeight - 2));
          }
          if (w >= 22) {
            ctx.fillStyle = tooLong ? '#942f2f' : excluded ? '#667181' : whole ? '#27643f' : '#244d91';
            ctx.font = '700 11px sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(String(index + 1), x + w / 2, top + barHeight / 2);
          }
        }
      }

      function scheduleCutTimelinePaint() {
        const paint = () => {
          timelinePaintRaf = 0;
          paintCutTimelineCanvas();
        };
        if (timelinePaintRaf) return;
        if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
          timelinePaintRaf = window.requestAnimationFrame(paint);
        } else {
          paint();
        }
      }

      function cutBoundaryIndexNearPointer(event, pxTolerance = 8) {
        const track = cutTimelineTrack.value;
        const bounds = workbenchBounds();
        const segments = workbenchDraft.value.segments;
        if (!track || !bounds || !Array.isArray(segments) || segments.length < 2) return null;
        const rect = track.getBoundingClientRect();
        if (!rect.width) return null;
        const ratio = Math.max(0, Math.min(1, (Number(event.clientX) - rect.left) / rect.width));
        const timestamp = bounds.start + (bounds.end - bounds.start) * BigInt(Math.round(ratio * 1000000)) / 1000000n;
        const index = QuicDataCutWorkbench.nearestBoundaryIndex(segments, timestamp.toString());
        if (index < 0) return null;
        const boundary = parseNanoseconds(segments[index]?.end_ns);
        if (boundary === null) return null;
        const boundaryX = Number(boundary - bounds.start) / Number(bounds.end - bounds.start) * rect.width;
        return Math.abs(boundaryX - (Number(event.clientX) - rect.left)) <= pxTolerance ? index : null;
      }

      function startCutTrackPointer(event) {
        if (!activeWorkbench.value?.capabilities?.cut || saving.value) return;
        if (event?.target?.closest?.('.cut-boundary, .cut-window, .cut-playhead-pin')) return;
        const index = cutBoundaryIndexNearPointer(event);
        if (index === null) return;
        event.preventDefault();
        startCutBoundaryDrag(index, event);
      }

      const visibleCutList = computed(() => {
        const segments = workbenchDraft.value.segments || [];
        const viewportHeight = cutListViewport.value?.clientHeight || 520;
        const start = Math.max(0, Math.floor(cutListScrollTop.value / CUT_WINDOW_ROW_STRIDE) - 4);
        const end = Math.min(segments.length, start + Math.ceil(viewportHeight / CUT_WINDOW_ROW_STRIDE) + 8);
        return {
          height: Math.max(0, segments.length * CUT_WINDOW_ROW_STRIDE),
          items: segments.slice(start, end).map((segment, offset) => ({
            segment,
            index: start + offset,
            top: (start + offset) * CUT_WINDOW_ROW_STRIDE,
          })),
        };
      });

      const visibleAnnotationList = computed(() => {
        const segments = workbenchDraft.value.segments || [];
        const viewportHeight = annotationListViewport.value?.clientHeight || 520;
        const start = Math.max(0, Math.floor(annotationListScrollTop.value / ANNOTATION_SEGMENT_ROW_STRIDE) - 3);
        const end = Math.min(segments.length, start + Math.ceil(viewportHeight / ANNOTATION_SEGMENT_ROW_STRIDE) + 6);
        return {
          height: Math.max(0, segments.length * ANNOTATION_SEGMENT_ROW_STRIDE),
          items: segments.slice(start, end).map((segment, offset) => ({
            segment,
            index: start + offset,
            top: (start + offset) * ANNOTATION_SEGMENT_ROW_STRIDE,
          })),
        };
      });

      const visibleCutTrackWindows = computed(() => {
        const segments = cutTrackSegments();
        if (!segments.length) return [];
        const selected = activeWorkbench.value?.capabilities?.cut
          ? activeCutSegmentIndex.value
          : activeAnnotationSegmentIndex.value;
        const range = timelineVisibleNsRange();
        const overlap = range
          ? QuicDataCutWorkbench.overlappingRange(segments, range.startNs.toString(), range.endNs.toString(), 1)
          : { startIndex: 0, endIndex: segments.length };
        if (overlap.endIndex - overlap.startIndex > CUT_TIMELINE_DOM_LIMIT) {
          return [...new Set([selected - 1, selected, selected + 1].filter((index) => index >= 0 && index < segments.length))]
            .sort((left, right) => left - right)
            .map((index) => ({ segment: segments[index], index }));
        }
        return segments.slice(overlap.startIndex, overlap.endIndex).map((segment, offset) => ({
          segment,
          index: overlap.startIndex + offset,
        }));
      });

      const visibleCutTrackBoundaries = computed(() => {
        if (!activeWorkbench.value?.capabilities?.cut) return [];
        const segments = workbenchDraft.value.segments || [];
        if (segments.length < 2) return [];
        const windows = visibleCutTrackWindows.value;
        if (cutTimelineVisibleCount() > CUT_TIMELINE_DOM_LIMIT) {
          const selected = selectedCutBoundary.value;
          const indexes = [];
          if (Number.isInteger(selected)) {
            for (let index = selected - 1; index <= selected + 1; index += 1) {
              if (index >= 0 && index < segments.length - 1) indexes.push(index);
            }
          }
          return indexes.map((index) => ({ index, segment: segments[index] }));
        }
        const start = windows[0]?.index ?? 0;
        const end = windows[windows.length - 1]?.index ?? -1;
        const items = [];
        for (let index = Math.max(0, start - 1); index <= end && index < segments.length - 1; index += 1) {
          items.push({ index, segment: segments[index] });
        }
        return items;
      });

      watch(timelineZoom, () => {
        nextTick(() => {
          syncTimelineViewRange();
          scheduleCutTimelinePaint();
        });
      });
      watch(timelinePanelHeight, () => scheduleCutTimelinePaint());

      let resizingTimeline = null;

      function resizeTimelineFromPointer(event) {
        if (!resizingTimeline) return;
        const delta = resizingTimeline.startY - Number(event.clientY);
        timelinePanelHeight.value = Math.max(180, Math.min(520, resizingTimeline.startHeight + delta));
        scheduleCutTimelinePaint();
      }

      function stopTimelineResize() {
        resizingTimeline = null;
        if (typeof window !== 'undefined') {
          window.removeEventListener('pointermove', resizeTimelineFromPointer);
          window.removeEventListener('pointerup', stopTimelineResize);
        }
      }

      function startTimelineResize(event) {
        resizingTimeline = { startY: Number(event.clientY), startHeight: timelinePanelHeight.value };
        if (typeof window !== 'undefined') {
          window.addEventListener('pointermove', resizeTimelineFromPointer);
          window.addEventListener('pointerup', stopTimelineResize, { once: true });
        }
      }

      let resizingWorkbenchSplit = null;

      function resizeWorkbenchSplitFromPointer(event) {
        if (!resizingWorkbenchSplit) return;
        const delta = resizingWorkbenchSplit.startX - Number(event.clientX);
        workbenchEditorWidth.value = Math.max(320, Math.min(720, resizingWorkbenchSplit.startWidth + delta));
      }

      function stopWorkbenchSplitResize() {
        resizingWorkbenchSplit = null;
        if (typeof window !== 'undefined') {
          window.removeEventListener('pointermove', resizeWorkbenchSplitFromPointer);
          window.removeEventListener('pointerup', stopWorkbenchSplitResize);
        }
      }

      function startWorkbenchSplitResize(event) {
        resizingWorkbenchSplit = { startX: Number(event.clientX), startWidth: workbenchEditorWidth.value };
        if (typeof window !== 'undefined') {
          window.addEventListener('pointermove', resizeWorkbenchSplitFromPointer);
          window.addEventListener('pointerup', stopWorkbenchSplitResize, { once: true });
        }
      }

      function handleAccountCommand(command) {
        if (command === 'change-password') openChangePassword();
        if (command === 'api-tokens') void openApiTokens();
        if (command === 'sign-out') void signOut();
      }

      function seekWorkbenchToTimestamp(timestamp) {
        if (!workbenchVideo.value) return false;
        const seconds = QuicDataPreviewTimeline.playbackSecondsAt(timestamp, previewPlaybackTimeline());
        if (seconds === null) return false;
        workbenchVideo.value.currentTime = seconds;
        lastWorkbenchPlaybackSeconds.value = seconds;
        syncWorkbenchPlayhead();
        return true;
      }

      function seekWorkbenchFromTimelineEvent(event, track = cutTimelineTrack.value) {
        const bounds = workbenchBounds();
        if (!bounds || !workbenchVideo.value) return;
        const timestamp = snapTimestampToWorkbenchPoint(sourceTimestampFromPointer(
          event, bounds.start, bounds.end, bounds.start, bounds.end, track,
        ));
        if (!seekWorkbenchToTimestamp(timestamp)) {
          ElMessage.warning(t('timelineMappingUnavailable'));
        }
      }

      function annotationSegmentIndexAtPointer(event) {
        const bounds = workbenchBounds();
        const track = cutTimelineTrack.value;
        if (!bounds || !track || !workbenchDraft.value.segments.length) return -1;
        const rect = track.getBoundingClientRect();
        if (!rect.width) return -1;
        const ratio = Math.max(0, Math.min(1, (Number(event.clientX) - rect.left) / rect.width));
        const scale = 1000000n;
        const timestamp = bounds.start + (bounds.end - bounds.start) * BigInt(Math.round(ratio * Number(scale))) / scale;
        return QuicDataCutWorkbench.indexContaining(workbenchDraft.value.segments, timestamp.toString());
      }

      function seekCutTimeline(event) {
        if (draggingCutBoundary || draggingPlayhead?.moved) return;
        seekWorkbenchFromTimelineEvent(event, cutTimelineTrack.value);
        if (activeWorkbench.value?.capabilities?.annotation) {
          const index = annotationSegmentIndexAtPointer(event);
          if (index >= 0) selectAnnotationSegment(index);
        }
      }

      let draggingPlayhead = null;

      function dragPlayheadFromPointer(event) {
        if (!draggingPlayhead) return;
        const distance = Math.abs(Number(event.clientX) - draggingPlayhead.startX);
        if (distance < 2 && !draggingPlayhead.moved) return;
        draggingPlayhead.moved = true;
        seekWorkbenchFromTimelineEvent(event, cutTimelineTrack.value);
      }

      function stopPlayheadDrag() {
        draggingPlayhead = null;
        if (typeof window !== 'undefined') {
          window.removeEventListener('pointermove', dragPlayheadFromPointer);
          window.removeEventListener('pointerup', stopPlayheadDrag);
        }
      }

      function startPlayheadDrag(event) {
        draggingPlayhead = { startX: Number(event.clientX), moved: false };
        if (event?.currentTarget?.setPointerCapture && event.pointerId !== undefined) event.currentTarget.setPointerCapture(event.pointerId);
        if (typeof window !== 'undefined') {
          window.addEventListener('pointermove', dragPlayheadFromPointer);
          window.addEventListener('pointerup', stopPlayheadDrag, { once: true });
        }
      }

      function seekCutLocalTimeline(event) {
        const range = selectedCutLocalRange();
        if (!range) return;
        const timestamp = sourceTimestampFromPointer(
          event,
          range.start_ns,
          range.end_ns,
          range.start_ns,
          range.end_ns,
          cutLocalTimelineTrack.value,
        );
        if (!seekWorkbenchToTimestamp(timestamp)) ElMessage.warning(t('timelineMappingUnavailable'));
      }

      let draggingCutBoundary = null;

      function moveCutBoundaryFromPointer(event) {
        const state = draggingCutBoundary;
        const index = state?.index;
        const segments = workbenchDraft.value.segments;
        if (!Number.isInteger(index) || index < 0 || index >= segments.length - 1) return;
        const leftStart = parseNanoseconds(segments[index].start_ns);
        const rightEnd = parseNanoseconds(segments[index + 1].end_ns);
        if (leftStart === null || rightEnd === null) return;
        const one = 1n;
        const minimum = leftStart + one;
        const maximum = rightEnd - one;
        const bounds = workbenchBounds();
        const local = state.local ? state.range : null;
        const rangeStart = parseNanoseconds(local?.start_ns) || bounds?.start;
        const rangeEnd = parseNanoseconds(local?.end_ns) || bounds?.end;
        const track = state.local ? cutLocalTimelineTrack.value : cutTimelineTrack.value;
        const timestamp = sourceTimestampFromPointer(event, rangeStart, rangeEnd, minimum, maximum, track);
        const next = QuicDataCutWorkbench.moveBoundary(segments, index, timestamp);
        if (!next) return;
        workbenchDraft.value.segments = next;
        workbenchDraftDirty.value = true;
        if (state) state.moved = true;
        scheduleCutTimelinePaint();
      }

      function stopCutBoundaryDrag() {
        if (draggingCutBoundary) markWorkbenchDraftDirty();
        draggingCutBoundary = null;
        if (typeof window !== 'undefined') {
          window.removeEventListener('pointermove', moveCutBoundaryFromPointer);
          window.removeEventListener('pointerup', stopCutBoundaryDrag);
        }
      }

      function startCutBoundaryDrag(index, event, local = false) {
        if (workbenchDraft.value.mode !== 'partitioned' || !exactPlaybackTimelineAvailable()) {
          ElMessage.warning(t('timelineMappingUnavailable'));
          return;
        }
        draggingCutBoundary = {
          index: Number(index),
          local: local === true,
          range: local === true ? selectedCutLocalRange() : null,
          moved: false,
        };
        selectCutBoundary(index, { recenter: local !== true });
        if (event?.currentTarget?.setPointerCapture && event.pointerId !== undefined) {
          event.currentTarget.setPointerCapture(event.pointerId);
        }
        if (typeof window !== 'undefined') {
          window.addEventListener('pointermove', moveCutBoundaryFromPointer);
          window.addEventListener('pointerup', stopCutBoundaryDrag, { once: true });
        }
      }

      function startCutLocalBoundaryPointer(boundary, event) {
        if (!boundary?.selected) return;
        event?.preventDefault?.();
        startCutBoundaryDrag(boundary.index, event, true);
      }

      function removeWorkbenchSegment(index) {
        workbenchDraft.value.segments.splice(index, 1);
        activeAnnotationSegmentIndex.value = Math.max(0, Math.min(activeAnnotationSegmentIndex.value, workbenchDraft.value.segments.length - 1));
        markWorkbenchDraftDirty();
      }

      function setCutBoundary(segment, boundary) {
        const timestamp = currentPlaybackTimestamp();
        if (!timestamp || !segment || !['start_ns', 'end_ns'].includes(boundary)) return;
        segment[boundary] = timestamp;
        markWorkbenchDraftDirty();
      }

      function sortAnnotationSegmentsByStart() {
        workbenchDraft.value.segments.sort((left, right) => {
          const leftStart = parseNanoseconds(left?.start_ns);
          const rightStart = parseNanoseconds(right?.start_ns);
          if (leftStart === rightStart) return 0;
          if (leftStart === null) return 1;
          if (rightStart === null) return -1;
          return leftStart < rightStart ? -1 : 1;
        });
      }

      function annotationSegmentStartRanges(excludedSegment, duration, bounds = workbenchBounds()) {
        if (!bounds || duration <= 0n || duration > bounds.end - bounds.start) return [];
        const occupied = workbenchDraft.value.segments
          .filter((segment) => segment !== excludedSegment)
          .map((segment) => ({ start: parseNanoseconds(segment?.start_ns), end: parseNanoseconds(segment?.end_ns) }))
          .filter((range) => range.start !== null && range.end !== null && range.end > range.start)
          .sort((left, right) => left.start < right.start ? -1 : left.start > right.start ? 1 : 0);
        const ranges = [];
        const latestStart = bounds.end - duration;
        let cursor = bounds.start;
        occupied.forEach((range) => {
          const occupiedStart = range.start < bounds.start ? bounds.start : range.start;
          const occupiedEnd = range.end > bounds.end ? bounds.end : range.end;
          const gapEnd = occupiedStart - duration;
          if (cursor <= gapEnd) ranges.push({ start: cursor, end: gapEnd });
          if (occupiedEnd > cursor) cursor = occupiedEnd;
        });
        if (cursor <= latestStart) ranges.push({ start: cursor, end: latestStart });
        return ranges;
      }

      function closestAnnotationSegmentStart(target, ranges) {
        if (!ranges.length) return null;
        let closest = null;
        ranges.forEach((range) => {
          const candidate = target < range.start ? range.start : target > range.end ? range.end : target;
          const distance = candidate > target ? candidate - target : target - candidate;
          if (!closest || distance < closest.distance) closest = { value: candidate, distance };
        });
        if (!closest || !timelineSnapEnabled.value) return closest?.value ?? null;
        const tolerance = 350000000n;
        let snapped = closest.value;
        let snapDistance = tolerance + 1n;
        ranges.forEach((range) => {
          [range.start, range.end].forEach((point) => {
            const distance = point > target ? point - target : target - point;
            if (distance <= tolerance && distance < snapDistance) {
              snapped = point;
              snapDistance = distance;
            }
          });
        });
        return snapped;
      }

      function addAnnotationSegment() {
        const timestamp = currentPlaybackTimestamp();
        if (!timestamp) {
          ElMessage.warning(t('timelineMappingUnavailable'));
          return;
        }
        const bounds = workbenchBounds();
        const start = parseNanoseconds(timestamp);
        const defaultDuration = 5000000000n;
        let startNs = start;
        let endNs = start !== null ? start + defaultDuration : null;
        if (bounds && startNs !== null && endNs !== null && endNs > bounds.end) {
          endNs = bounds.end;
          startNs = endNs - defaultDuration > bounds.start ? endNs - defaultDuration : bounds.start;
        }
        if (!bounds || startNs === null || endNs === null || endNs <= startNs) return;
        const duration = endNs - startNs;
        const legalStart = closestAnnotationSegmentStart(startNs, annotationSegmentStartRanges(null, duration, bounds));
        if (legalStart === null) {
          ElMessage.warning(t('annotationNoRoom'));
          return;
        }
        const segment = {
          id: nextSegmentId(),
          start_ns: legalStart.toString(),
          end_ns: (legalStart + duration).toString(),
          description: '',
        };
        workbenchDraft.value.segments.push(segment);
        sortAnnotationSegmentsByStart();
        selectAnnotationSegment(workbenchDraft.value.segments.indexOf(segment));
        markWorkbenchDraftDirty();
      }

      function selectAnnotationSegment(index, { openDetail = true } = {}) {
        const normalized = Math.max(0, Math.min(Number(index), workbenchDraft.value.segments.length - 1));
        if (!Number.isInteger(normalized) || !workbenchDraft.value.segments[normalized]) return;
        activeAnnotationSegmentIndex.value = normalized;
        if (openDetail) annotationDetailOpen.value = true;
        nextTick(() => {
          scrollAnnotationListToIndex(normalized);
          scheduleCutTimelinePaint();
        });
      }

      function toggleAnnotationDetail() {
        annotationDetailOpen.value = !annotationDetailOpen.value;
      }

      function selectReviewSegment(index, { openDetail = true } = {}) {
        const segments = activeWorkbench.value?.review_target?.payload?.segments || [];
        const normalized = Math.max(0, Math.min(Number(index), segments.length - 1));
        if (!Number.isInteger(normalized) || !segments[normalized]) return;
        activeAnnotationSegmentIndex.value = normalized;
        if (segments[normalized].start_ns !== undefined && segments[normalized].start_ns !== null) {
          seekWorkbenchToTimestamp(segments[normalized].start_ns);
        }
        if (openDetail) reviewDetailOpen.value = true;
      }

      function toggleReviewDetail() {
        reviewDetailOpen.value = !reviewDetailOpen.value;
      }

      function annotationDraftSaveValid() {
        const bounds = workbenchBounds();
        const segments = workbenchDraft.value.segments;
        if (!bounds || !Array.isArray(segments)) return false;
        let priorEnd = null;
        return segments.every((segment) => {
          const start = parseNanoseconds(segment.start_ns);
          const end = parseNanoseconds(segment.end_ns);
          const valid = start !== null && end !== null && start >= bounds.start && end <= bounds.end
            && start < end && (priorEnd === null || start >= priorEnd);
          priorEnd = end;
          return Boolean(valid);
        });
      }

      function annotationDraftSubmitValid() {
        return annotationDraftSaveValid()
          && workbenchDraft.value.segments.length > 0
          && workbenchDraft.value.segments.every((segment) => Boolean(String(segment.description || '').trim()));
      }

      let draggingAnnotationBoundary = null;

      function moveAnnotationBoundaryFromPointer(event) {
        const state = draggingAnnotationBoundary;
        const bounds = workbenchBounds();
        const segment = workbenchDraft.value.segments[state?.index];
        if (!state || !bounds || !segment) return;
        const other = parseNanoseconds(state.edge === 'start_ns' ? segment.end_ns : segment.start_ns);
        if (other === null) return;
        const minimum = state.edge === 'start_ns' ? bounds.start : other + 1n;
        const maximum = state.edge === 'start_ns' ? other - 1n : bounds.end;
        const timestamp = sourceTimestampFromPointer(
          event, bounds.start, bounds.end, minimum, maximum, cutTimelineTrack.value,
        );
        if (!timestamp) return;
        segment[state.edge] = timestamp;
        workbenchDraftDirty.value = true;
      }

      function stopAnnotationBoundaryDrag() {
        if (draggingAnnotationBoundary) markWorkbenchDraftDirty();
        draggingAnnotationBoundary = null;
        if (typeof window !== 'undefined') {
          window.removeEventListener('pointermove', moveAnnotationBoundaryFromPointer);
          window.removeEventListener('pointerup', stopAnnotationBoundaryDrag);
        }
      }

      function startAnnotationBoundaryDrag(index, edge, event) {
        if (!['start_ns', 'end_ns'].includes(edge) || !exactPlaybackTimelineAvailable()) return;
        selectAnnotationSegment(index, { openDetail: true });
        draggingAnnotationBoundary = { index: Number(index), edge };
        if (event?.currentTarget?.setPointerCapture && event.pointerId !== undefined) event.currentTarget.setPointerCapture(event.pointerId);
        if (typeof window !== 'undefined') {
          window.addEventListener('pointermove', moveAnnotationBoundaryFromPointer);
          window.addEventListener('pointerup', stopAnnotationBoundaryDrag, { once: true });
        }
      }

      let draggingAnnotationSegment = null;

      function moveAnnotationSegmentFromPointer(event) {
        const state = draggingAnnotationSegment;
        const bounds = workbenchBounds();
        const segment = state?.segment;
        if (!state || !bounds || !segment) return;
        const distance = Math.abs(Number(event.clientX) - state.startX);
        if (distance < 4 && !state.moved) return;
        state.moved = true;
        const pointerTimestamp = parseNanoseconds(sourceTimestampFromPointer(
          event, bounds.start, bounds.end, bounds.start, bounds.end, cutTimelineTrack.value,
        ));
        if (pointerTimestamp === null) return;
        let nextStart = pointerTimestamp - state.pointerOffset;
        nextStart = nextStart < bounds.start ? bounds.start : nextStart;
        nextStart = nextStart > bounds.end - state.duration ? bounds.end - state.duration : nextStart;
        nextStart = closestAnnotationSegmentStart(nextStart, state.startRanges);
        if (nextStart === null) return;
        segment.start_ns = nextStart.toString();
        segment.end_ns = (nextStart + state.duration).toString();
        workbenchDraftDirty.value = true;
        scheduleCutTimelinePaint();
      }

      function stopAnnotationSegmentDrag() {
        if (draggingAnnotationSegment?.moved) {
          const movedSegment = draggingAnnotationSegment.segment;
          sortAnnotationSegmentsByStart();
          selectAnnotationSegment(workbenchDraft.value.segments.indexOf(movedSegment));
          markWorkbenchDraftDirty();
        }
        draggingAnnotationSegment = null;
        if (typeof window !== 'undefined') {
          window.removeEventListener('pointermove', moveAnnotationSegmentFromPointer);
          window.removeEventListener('pointerup', stopAnnotationSegmentDrag);
          window.removeEventListener('pointercancel', stopAnnotationSegmentDrag);
        }
      }

      function startAnnotationSegmentDrag(index, event) {
        const normalized = Number(index);
        const bounds = workbenchBounds();
        const segments = workbenchDraft.value.segments;
        const segment = segments[normalized];
        const start = parseNanoseconds(segment?.start_ns);
        const end = parseNanoseconds(segment?.end_ns);
        if (!Number.isInteger(normalized) || !bounds || start === null || end === null || end <= start || !exactPlaybackTimelineAvailable()) {
          ElMessage.warning(t('timelineMappingUnavailable'));
          return;
        }
        const pointerTimestamp = parseNanoseconds(sourceTimestampFromPointer(
          event, bounds.start, bounds.end, bounds.start, bounds.end, cutTimelineTrack.value,
        ));
        if (pointerTimestamp === null) return;
        const duration = end - start;
        const startRanges = annotationSegmentStartRanges(segment, duration, bounds);
        if (!startRanges.length) return;
        const rawPointerOffset = pointerTimestamp - start;
        const pointerOffset = rawPointerOffset < 0n ? 0n : rawPointerOffset > duration ? duration : rawPointerOffset;
        selectAnnotationSegment(normalized, { openDetail: true });
        draggingAnnotationSegment = {
          segment, duration, pointerOffset, startRanges, startX: Number(event.clientX), moved: false,
        };
        if (event?.currentTarget?.setPointerCapture && event.pointerId !== undefined) event.currentTarget.setPointerCapture(event.pointerId);
        if (typeof window !== 'undefined') {
          window.addEventListener('pointermove', moveAnnotationSegmentFromPointer);
          window.addEventListener('pointerup', stopAnnotationSegmentDrag, { once: true });
          window.addEventListener('pointercancel', stopAnnotationSegmentDrag, { once: true });
        }
      }

      function stepWorkbenchPlayback(direction, amount = 'seconds1') {
        const bounds = workbenchBounds();
        const current = parseNanoseconds(currentPlaybackTimestamp());
        if (!bounds || current === null) return;
        const deltaNs = BigInt(Math.round((amount === 'seconds5' ? 5 : 1) * 1e9));
        const next = direction < 0
          ? (current > bounds.start + deltaNs ? current - deltaNs : bounds.start)
          : (current + deltaNs < bounds.end ? current + deltaNs : bounds.end);
        seekWorkbenchToTimestamp(next.toString());
      }

      function jumpToAdjacentBoundary(direction) {
        const current = parseNanoseconds(currentPlaybackTimestamp());
        if (current === null) return;
        const boundaries = workbenchDraft.value.segments
          .slice(0, -1)
          .map((segment) => parseNanoseconds(segment.end_ns))
          .filter((value) => value !== null)
          .sort((left, right) => left < right ? -1 : left > right ? 1 : 0);
        const target = direction < 0
          ? [...boundaries].reverse().find((value) => value < current - 1000000n)
          : boundaries.find((value) => value > current + 1000000n);
        if (target !== undefined) seekWorkbenchToTimestamp(target.toString());
      }

      async function setWorkbenchShuttle(direction) {
        const video = workbenchVideo.value;
        if (!video) return;
        if (direction === 0) {
          video.pause();
          syncWorkbenchPlayhead();
          return;
        }
        const rate = Math.min(8, Math.max(1, Math.abs(Number(workbenchPlaybackRate.value || 1)) * 1.5));
        video.playbackRate = rate;
        workbenchPlaybackRate.value = rate;
        if (direction < 0) stepWorkbenchPlayback(-1, 'seconds5');
        try {
          await video.play();
        } catch (error) {
          errorMessage(error);
        }
        syncWorkbenchPlayhead();
      }

      function handleWorkbenchShortcut(event) {
        if (activeView.value !== 'workbench' || !activeWorkbench.value || typeof QuicDataWorkbenchShortcuts === 'undefined') return;
        if (QuicDataWorkbenchShortcuts.shouldIgnoreTarget(event.target)) return;
        const command = QuicDataWorkbenchShortcuts.resolve(event, activeWorkbench.value.capabilities || {});
        if (!command) return;
        if (command.id === 'help-close') {
          if (showShortcutHelp.value) {
            event.preventDefault();
            showShortcutHelp.value = false;
          }
          return;
        }
        event.preventDefault();
        switch (command.id) {
          case 'help-toggle': showShortcutHelp.value = !showShortcutHelp.value; break;
          case 'history-undo': undoWorkbench(); break;
          case 'history-redo': redoWorkbench(); break;
          case 'annotation-copy': copyWorkbenchSelection(); break;
          case 'annotation-paste': pasteWorkbenchSelection(); break;
          case 'play-toggle': void toggleWorkbenchPlayback(); break;
          case 'step-backward': stepWorkbenchPlayback(-1, 'seconds1'); break;
          case 'step-forward': stepWorkbenchPlayback(1, 'seconds1'); break;
          case 'step-backward-large': stepWorkbenchPlayback(-1, 'seconds5'); break;
          case 'step-forward-large': stepWorkbenchPlayback(1, 'seconds5'); break;
          case 'shuttle-backward': void setWorkbenchShuttle(-1); break;
          case 'shuttle-pause': void setWorkbenchShuttle(0); break;
          case 'shuttle-forward': void setWorkbenchShuttle(1); break;
          case 'boundary-previous': jumpToAdjacentBoundary(-1); break;
          case 'boundary-next': jumpToAdjacentBoundary(1); break;
          case 'timeline-zoom-in': zoomWorkbenchTimeline(1); break;
          case 'timeline-zoom-out': zoomWorkbenchTimeline(-1); break;
          case 'timeline-snap': timelineSnapEnabled.value = !timelineSnapEnabled.value; break;
          case 'cut-select-previous': selectCutSegment(activeCutSegmentIndex.value - 1, { seek: true }); break;
          case 'cut-select-next': selectCutSegment(activeCutSegmentIndex.value + 1, { seek: true }); break;
          case 'cut-delete-boundary': deleteSelectedCutBoundary(); break;
          case 'cut-add-boundary': addCutBoundary(); break;
          case 'cut-restore-qr': restoreSelectedQrBoundary(); break;
          case 'cut-include': setCutEligibility(activeCutSegmentIndex.value, 'included'); break;
          case 'cut-exclude': setCutEligibility(activeCutSegmentIndex.value, 'excluded'); break;
          case 'annotation-add-segment': addAnnotationSegment(); break;
          case 'annotation-set-start': {
            const segment = workbenchDraft.value.segments[activeAnnotationSegmentIndex.value];
            if (segment) setCutBoundary(segment, 'start_ns');
            break;
          }
          case 'annotation-set-end': {
            const segment = workbenchDraft.value.segments[activeAnnotationSegmentIndex.value];
            if (segment) setCutBoundary(segment, 'end_ns');
            break;
          }
          default: break;
        }
      }

      function markWorkbenchDraftDirty() {
        workbenchDraftDirty.value = true;
        workbenchSaveCoordinator?.markDirty();
        if ((workbenchDraft.value.segments?.length || 0) > CUT_TIMELINE_DOM_LIMIT) {
          scheduleWorkbenchHistory();
        } else {
          cancelScheduledWorkbenchHistory();
          recordWorkbenchHistory();
        }
        scheduleCutTimelinePaint();
      }

      function disposeWorkbenchSaveCoordinator() {
        const coordinator = workbenchSaveCoordinator;
        workbenchSaveCoordinator = null;
        workbenchSaveItemId = null;
        workbenchInvalidationNotified = false;
        coordinator?.dispose();
      }

      function configureWorkbenchSaveCoordinator(snapshot) {
        disposeWorkbenchSaveCoordinator();
        const item = snapshot?.work_item;
        if (
          !item?.id
          || !['cut', 'annotation'].includes(item.kind)
          || typeof QuicDataWorkbenchSave === 'undefined'
        ) return;
        workbenchSaveItemId = Number(item.id);
        let coordinator = null;
        coordinator = QuicDataWorkbenchSave.create({
          capture(revision) {
            const request = captureWorkbenchDraft();
            return request ? { ...request, revision } : null;
          },
          save: persistWorkbenchDraft,
          onFailed(error) {
            errorMessage(error);
          },
          onState(state) {
            if (workbenchSaveCoordinator !== coordinator) return;
            const pointerEditing = Boolean(
              draggingCutBoundary || draggingAnnotationBoundary || draggingAnnotationSegment,
            );
            workbenchDraftDirty.value = state.dirty || pointerEditing;
          },
          debounceMs: 5000,
          maxWaitMs: 30000,
        });
        workbenchSaveCoordinator = coordinator;
      }

      function canEnterWorkbench(row) {
        const item = row?.work_item;
        const actions = item?.available_actions || [];
        const canResume = item?.status === 'in_progress'
          && (actions.includes('save_draft') || actions.includes('submit') || actions.includes('review'));
        return Boolean(
          item
          && queuePreviewStatus(row) === 'ready'
          && ['cut', 'annotation', 'review'].includes(item.kind)
          && ['assigned', 'in_progress'].includes(item.status)
          && (actions.includes('continue') || canResume),
        );
      }

      function visibleQueueActions(row) {
        const actions = row?.work_item?.available_actions || [];
        if (queuePreviewStatus(row) !== 'ready') return [];
        if (!['cut', 'annotation', 'review'].includes(row?.work_item?.kind)) return actions;
        return actions.filter((action) => !['claim', 'release', 'continue', 'save_draft', 'submit', 'review'].includes(action));
      }

      function clearSubscriptions() {
        cancelScheduledWorkQueueRefresh();
        for (const unsubscribe of subscriptions.values()) unsubscribe();
        subscriptions.clear();
      }

      function removeSubscription(key) {
        const unsubscribe = subscriptions.get(key);
        if (unsubscribe) unsubscribe();
        subscriptions.delete(key);
      }

      function replaceSubscription(key, options) {
        const existing = subscriptions.get(key);
        if (existing) existing();
        if (demoMode.value || typeof QuicDataRealtime === 'undefined') return;
        subscriptions.set(key, QuicDataRealtime.subscribe(options));
      }

      function ensureRealtimeConnection() {
        if (demoMode.value || typeof QuicDataRealtime === 'undefined') return;
        const accessToken = QuicDataAPI.getAccessToken?.();
        if (accessToken) QuicDataRealtime.connect({ accessToken });
      }

      function subscribeEpisode(episode, requestToken) {
        if (!episode?.id) return;
        ensureRealtimeConnection();
        replaceSubscription('selected-episode', {
          resourceType: 'episode', resourceId: String(episode.id), realtimeVersion: episode.realtime_version || 0,
          refresh: () => QuicDataAPI.getEpisode(episode.id),
          onUpdate: (next) => {
            if (!episodeDetailRequestGate.isCurrent(requestToken, next?.id)) return;
            selectedEpisode.value = QuicDataEpisodeDetail.mergeEpisodeDetail(
              { episode: selectedEpisode.value },
              next,
            );
          },
        });
      }

      function handleWorkbenchItemUpdate(next) {
        const current = activeWorkbench.value;
        const item = current?.work_item;
        if (!next?.id || Number(next.id) !== Number(item?.id)) return;
        const currentAssignee = Number(item.assignee_user_id || 0);
        const nextAssignee = Number(next.assignee_user_id || 0);
        const leaseStillActive = ['assigned', 'in_progress'].includes(next.status)
          && currentAssignee > 0
          && nextAssignee === currentAssignee;
        activeWorkbench.value = {
          ...current,
          work_item: {
            ...item,
            ...next,
            available_actions: leaseStillActive ? (item.available_actions || []) : [],
          },
        };
        if (leaseStillActive) return;
        workbenchSaveCoordinator?.pause();
        if (!workbenchInvalidationNotified) {
          workbenchInvalidationNotified = true;
          ElMessage.warning(t('workItemUpdated'));
        }
      }

      function handleWorkbenchEpisodeUpdate(next) {
        const current = activeWorkbench.value;
        const episode = current?.episode;
        if (!next?.id || Number(next.id) !== Number(episode?.id)) return;
        const quality = next.quality_status
          ? { ...(episode.quality || {}), status: next.quality_status }
          : episode.quality;
        activeWorkbench.value = {
          ...current,
          episode: { ...episode, ...next, quality },
        };
      }

      function subscribeWorkbench(snapshot) {
        const item = snapshot?.work_item;
        const episode = snapshot?.episode;
        if (!item?.id || !episode?.id) return;
        ensureRealtimeConnection();
        replaceSubscription('workbench-item', {
          resourceType: 'work_item', resourceId: String(item.id), realtimeVersion: item.realtime_version || 0,
          refreshOnSubscribe: false,
          refreshOnReconnect: false,
          onUpdate: handleWorkbenchItemUpdate,
        });
        replaceSubscription('workbench-episode', {
          resourceType: 'episode', resourceId: String(episode.id), realtimeVersion: episode.realtime_version || 0,
          refreshOnSubscribe: false,
          refreshOnReconnect: false,
          onUpdate: handleWorkbenchEpisodeUpdate,
        });
      }

      async function loadWorkbench({ resubscribe = true } = {}) {
        const workItemId = Number(workbenchRoute.workItemId);
        const episodeId = Number(workbenchRoute.episodeId);
        if (!Number.isInteger(workItemId) || workItemId <= 0 || !Number.isInteger(episodeId) || episodeId <= 0) {
          disposeWorkbenchSaveCoordinator();
          activeWorkbench.value = null;
          workbenchTimeline.value = null;
          return;
        }
        if (workbenchSaveItemId && Number(workbenchSaveItemId) !== workItemId) {
          disposeWorkbenchSaveCoordinator();
        }
        loading.workbench = true;
        try {
          const workbenchTimeoutMs = 15000;
          const [snapshotResult, timelineResult] = await Promise.allSettled([
            QuicDataAPI.getEpisodeWorkbench(episodeId, workItemId, { timeoutMs: workbenchTimeoutMs }),
            QuicDataAPI.getEpisodeTimeline(episodeId, { timeoutMs: workbenchTimeoutMs }),
          ]);
          if (snapshotResult.status !== 'fulfilled') {
            throw snapshotResult.reason || new Error(t('workbenchUnavailable'));
          }
          const snapshot = snapshotResult.value;
          const previousWorkbenchEpisodeId = Number(activeWorkbench.value?.episode?.id || 0);
          if (['cut', 'annotation', 'review'].includes(snapshot?.work_item?.kind)) {
            setQueueStageValue(snapshot.work_item.kind);
            queueStageInitialized = true;
          }
          const previousWorkbenchItemId = Number(activeWorkbench.value?.work_item?.id || 0);
          const replaceDraft = shouldReplaceWorkbenchDraft(activeWorkbench.value, snapshot, workbenchDraftDirty.value);
          activeWorkbench.value = snapshot;
          if (timelineResult.status === 'fulfilled') {
            workbenchTimeline.value = timelineResult.value || snapshot.timeline || null;
          } else {
            workbenchTimeline.value = snapshot.timeline || null;
            errorMessage(timelineResult.reason || new Error(t('timelineMappingUnavailable')));
          }
          const timeline = workbenchTimeline.value;
          if (replaceDraft) {
            workbenchDraft.value = normalizedWorkbenchDraft(snapshot);
            activeAnnotationSegmentIndex.value = 0;
            activeCutSegmentIndex.value = 0;
            selectedCutBoundary.value = null;
            cutLocalRangeAnchor.value = null;
            annotationDetailOpen.value = false;
            reviewDetailOpen.value = false;
            if (snapshot.capabilities?.cut && !workbenchDraft.value.segments.length) {
              initializeCutDraft({
                ...snapshot,
                timeline: { ...snapshot.timeline, boundary_suggestions: timeline?.boundary_suggestions || snapshot.timeline?.boundary_suggestions || [] },
              });
            }
            workbenchDraftDirty.value = false;
            resetWorkbenchHistory();
            configureWorkbenchSaveCoordinator(snapshot);
          } else if (Number(workbenchSaveItemId || 0) !== Number(snapshot.work_item?.id || 0)) {
            configureWorkbenchSaveCoordinator(snapshot);
          }
          if (snapshot.capabilities?.annotation) {
            await loadAiSuggestions(snapshot.episode.id);
          } else if (snapshot.capabilities?.review && snapshot.review_target?.kind === 'annotation') {
            if (previousWorkbenchItemId !== Number(snapshot.work_item?.id || 0)) {
              reviewForm.note = '';
              reviewForm.rating = 0;
              reviewForm.target_work_item_id = snapshot.review_target?.work_item_id || null;
            }
            collectorProfiles.value = [];
            collectionDevices.value = [];
            aiSuggestions.value = { capability: { eligible: false, rgb_topics: [], default_rgb_topic: '' }, items: [] };
            selectedAiTopic.value = '';
          } else if (snapshot.capabilities?.review) {
            if (previousWorkbenchItemId !== Number(snapshot.work_item?.id || 0)) {
              reviewForm.note = '';
              reviewForm.target_work_item_id = snapshot.review_target?.work_item_id || null;
            }
            collectorProfiles.value = [];
            collectionDevices.value = [];
            aiSuggestions.value = { capability: { eligible: false, rgb_topics: [], default_rgb_topic: '' }, items: [] };
            selectedAiTopic.value = '';
          } else {
            collectorProfiles.value = [];
            collectionDevices.value = [];
            aiSuggestions.value = { capability: { eligible: false, rgb_topics: [], default_rgb_topic: '' }, items: [] };
            selectedAiTopic.value = '';
          }
          if (resubscribe) subscribeWorkbench(snapshot);
          nextTick(() => {
            cutListScrollTop.value = replaceDraft ? 0 : cutListScrollTop.value;
            annotationListScrollTop.value = replaceDraft ? 0 : annotationListScrollTop.value;
            syncTimelineViewRange();
            scheduleCutTimelinePaint();
            applyPlayheadDom(cutPlayheadTimestamp.value);
          });
        } catch (error) {
          disposeWorkbenchSaveCoordinator();
          activeWorkbench.value = null;
          workbenchTimeline.value = null;
          errorMessage(error);
        } finally {
          loading.workbench = false;
        }
      }

      async function loadCollectorProfiles(workspaceId) {
        if (!workspaceId) {
          collectorProfiles.value = [];
          return;
        }
        try {
          collectorProfiles.value = (await QuicDataAPI.listCollectorProfiles(workspaceId)).items || [];
        } catch (error) {
          collectorProfiles.value = [];
          errorMessage(error);
        }
      }

      async function loadCollectionDevices(workspaceId) {
        if (!workspaceId) {
          collectionDevices.value = [];
          return;
        }
        try {
          collectionDevices.value = (await QuicDataAPI.listCollectionDevices(workspaceId)).items || [];
        } catch (error) {
          collectionDevices.value = [];
          errorMessage(error);
        }
      }

      function subscribeAiSuggestionJob(episodeId, job) {
        if (!episodeId || !job?.id) return;
        ensureRealtimeConnection();
        replaceSubscription(`workbench-ai:${job.id}`, {
          resourceType: 'job_run', resourceId: String(job.id), realtimeVersion: job.realtime_version || 0,
          refreshAlways: true,
          refresh: async () => {
            const snapshot = await QuicDataAPI.getEpisodeAiSuggestions(episodeId);
            return snapshot.items?.find((item) => String(item.id) === String(job.id)) || job;
          },
          onUpdate: () => { void loadAiSuggestions(episodeId, { resubscribe: false }); },
        });
      }

      async function loadAiSuggestions(episodeId, { resubscribe = true } = {}) {
        if (!episodeId) return;
        loading.ai = true;
        try {
          const next = await QuicDataAPI.getEpisodeAiSuggestions(episodeId);
          aiSuggestions.value = {
            capability: next.capability || { eligible: false, rgb_topics: [], default_rgb_topic: '' },
            items: next.items || [],
          };
          const topics = aiSuggestions.value.capability.rgb_topics || [];
          if (!topics.includes(selectedAiTopic.value)) {
            selectedAiTopic.value = aiSuggestions.value.capability.default_rgb_topic || topics[0] || '';
          }
          if (resubscribe) aiSuggestions.value.items.forEach((job) => subscribeAiSuggestionJob(episodeId, job));
        } catch (error) {
          aiSuggestions.value = { capability: { eligible: false, rgb_topics: [], default_rgb_topic: '' }, items: [] };
          errorMessage(error);
        } finally {
          loading.ai = false;
        }
      }

      function aiStatusType(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'failed' || status === 'cancelled') return 'danger';
        return 'warning';
      }

      async function requestAiSuggestion() {
        const snapshot = activeWorkbench.value;
        const episodeId = snapshot?.episode?.id;
        if (!episodeId || !aiSuggestions.value.capability?.eligible || !selectedAiTopic.value) return;
        saving.value = true;
        try {
          await QuicDataAPI.createEpisodeAiSuggestions(episodeId, { rgb_topic: selectedAiTopic.value });
          await loadAiSuggestions(episodeId);
          ElMessage.success(t('aiQueued'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function retryAiSuggestion(job) {
        const episodeId = activeWorkbench.value?.episode?.id;
        if (!episodeId || !job?.id) return;
        saving.value = true;
        try {
          await QuicDataAPI.retryEpisodeAiSuggestions(episodeId, job.id);
          await loadAiSuggestions(episodeId);
          ElMessage.success(t('aiQueued'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      function applyAiSuggestion(job) {
        const suggestions = Array.isArray(job?.segments) ? job.segments : [];
        if (!suggestions.length) return;
        const known = new Set(workbenchDraft.value.segments.map((segment) => (
          `${segment.start_ns}:${segment.end_ns}:${segment.description || ''}`
        )));
        const additions = suggestions
          .filter((segment) => !known.has(`${segment.start_ns}:${segment.end_ns}:${segment.description || ''}`))
          .map((segment, index) => ({
            id: `ai-${job.id}-${index}-${nextSegmentId()}`,
            start_ns: String(segment.start_ns),
            end_ns: String(segment.end_ns),
            description: String(segment.description || ''),
          }));
        if (!additions.length) return;
        workbenchDraft.value.segments.push(...additions);
        workbenchDraft.value.segments.sort((left, right) => {
          const leftStart = parseNanoseconds(left.start_ns) || 0n;
          const rightStart = parseNanoseconds(right.start_ns) || 0n;
          return leftStart < rightStart ? -1 : leftStart > rightStart ? 1 : 0;
        });
        markWorkbenchDraftDirty();
      }

      function syncNativeLerobotDataset(dataset) {
        if (!dataset?.id) return;
        nativeLerobotBatchDatasets.value = nativeLerobotBatchDatasets.value.map((row) => (
          Number(row.id) === Number(dataset.id) ? { ...row, ...dataset } : row
        ));
        if (Number(selectedNativeLerobotDataset.value?.id) === Number(dataset.id)) {
          selectedNativeLerobotDataset.value = { ...selectedNativeLerobotDataset.value, ...dataset };
        }
      }

      async function refreshNativeLerobotDataset(datasetId) {
        const dataset = await QuicDataAPI.getNativeLerobotDataset(datasetId);
        syncNativeLerobotDataset(dataset);
        return dataset;
      }

      function subscribeNativeLerobotCopyJob(dataset, job = null) {
        const jobId = job?.id || dataset?.last_copy_job_id;
        if (!dataset?.id || !jobId) return;
        ensureRealtimeConnection();
        replaceSubscription(`native-lerobot-copy:${dataset.id}`, {
          resourceType: 'job_run',
          resourceId: String(jobId),
          realtimeVersion: job?.realtime_version || 0,
          refreshAlways: true,
          refresh: async () => (await QuicDataAPI.getJob(jobId)).job,
          onUpdate: () => {
            void refreshNativeLerobotDataset(dataset.id).catch(() => {
              console.warn('native LeRobot copy status refresh failed');
            });
          },
        });
      }

      function syncNativeLerobotBundle(datasetId, bundle) {
        if (
          !bundle?.id
          || Number(selectedNativeLerobotDataset.value?.id) !== Number(datasetId)
        ) return;
        selectedNativeLerobotBundle.value = bundle;
      }

      async function refreshNativeLerobotBundle(datasetId, bundleId) {
        const bundle = await QuicDataAPI.getNativeLerobotBundle(datasetId, bundleId);
        syncNativeLerobotBundle(datasetId, bundle);
        return bundle;
      }

      function subscribeNativeLerobotBundleJob(dataset, bundle, job = null) {
        const jobId = job?.id || bundle?.job_id;
        if (!dataset?.id || !bundle?.id || !jobId) return;
        ensureRealtimeConnection();
        replaceSubscription(`native-lerobot-bundle:${dataset.id}`, {
          resourceType: 'job_run',
          resourceId: String(jobId),
          realtimeVersion: job?.realtime_version || 0,
          refreshAlways: true,
          refresh: async () => (await QuicDataAPI.getJob(jobId)).job,
          onUpdate: () => {
            void refreshNativeLerobotBundle(dataset.id, bundle.id).catch(() => {
              console.warn('native LeRobot bundle status refresh failed');
            });
          },
        });
      }

      function subscribeWorkspaceQueue() {
        if (!canView('work-queue') || !selectedWorkspaceId.value || governanceQueueActive.value) return;
        const request = workQueueRequest();
        ensureRealtimeConnection();
        replaceSubscription('workspace-queue', {
          resourceType: 'work_queue', resourceId: String(selectedWorkspaceId.value), refreshAlways: true,
          refreshOnSubscribe: false,
          refresh: () => QuicDataAPI.listWorkQueue(workQueueQuery(request)),
          onUpdate: (snapshot) => {
            if (!isCurrentWorkQueueRequest(request)) return;
            if (Array.isArray(snapshot?.items)) {
              const snapshotPageSize = Number(snapshot.limit);
              const snapshotOffset = Number(snapshot.offset);
              const matchesPage = (!Number.isFinite(snapshotPageSize) || snapshotPageSize === request.pageSize)
                && (!Number.isFinite(snapshotOffset) || snapshotOffset === (request.page - 1) * request.pageSize);
              if (matchesPage && !applyWorkQueueResponse(snapshot, request)) {
                void scheduleWorkQueueRefresh();
              }
              else if (!matchesPage) void scheduleWorkQueueRefresh();
            } else {
              void scheduleWorkQueueRefresh();
            }
          },
        });
      }

      function pickLatestScopeId(items) {
        const list = Array.isArray(items) ? items : [];
        if (!list.length) return null;
        return sortScopeItemsNewestFirst(list)[0]?.id || null;
      }

      function sortScopeItemsNewestFirst(items) {
        return [...(Array.isArray(items) ? items : [])].sort((left, right) => {
          const leftTime = Date.parse(left?.created_at || '') || 0;
          const rightTime = Date.parse(right?.created_at || '') || 0;
          if (rightTime !== leftTime) return rightTime - leftTime;
          return Number(right?.id || 0) - Number(left?.id || 0);
        });
      }

      function scopePreferenceStorage() {
        return typeof localStorage === 'undefined' ? null : localStorage;
      }

      function savedScopePreference() {
        if (typeof QuicDataScopePreference === 'undefined' || !user.value?.id) return null;
        return QuicDataScopePreference.read(scopePreferenceStorage(), user.value.id);
      }

      function persistScopePreference() {
        if (typeof QuicDataScopePreference === 'undefined' || !user.value?.id) return;
        QuicDataScopePreference.write(scopePreferenceStorage(), user.value.id, {
          workspace_id: selectedWorkspaceId.value,
          task_set_id: selectedTaskSetId.value,
        });
      }

      async function loadWorkspaces({ preferredWorkspaceId = null } = {}) {
        loading.workspaces = true;
        try {
          const list = (await QuicDataAPI.listWorkspaces()).list || [];
          workspaces.value = sortScopeItemsNewestFirst(list);
          const options = availableScopeWorkspaces.value;
          if (options.some((item) => Number(item.id) === Number(preferredWorkspaceId))) {
            selectedWorkspaceId.value = Number(preferredWorkspaceId);
          } else if (!options.some((item) => Number(item.id) === Number(selectedWorkspaceId.value))) {
            selectedWorkspaceId.value = pickLatestScopeId(options);
          }
        } catch (error) {
          errorMessage(error);
        } finally {
          loading.workspaces = false;
        }
      }

      async function loadTaskSets({ preferredTaskSetId = null } = {}) {
        taskSets.value = [];
        selectedTaskSetId.value = null;
        batches.value = [];
        episodes.value = [];
        episodeTotal.value = 0;
        resetEpisodePage();
        assetExpandedIds.value = new Set();
        batchImports.value = [];
        batchActivity.value = [];
        taskLabels.value = [];
        selectedBatch.value = null;
        if (!selectedWorkspaceId.value || (!demoMode.value && (user.value?.role !== 'admin' || currentWorkspace.value?.collection_access === false))) return;
        loading.taskSets = true;
        try {
          const list = (await QuicDataAPI.listTaskSets(selectedWorkspaceId.value)).list || [];
          taskSets.value = sortScopeItemsNewestFirst(list);
          selectedTaskSetId.value = taskSets.value.some((item) => Number(item.id) === Number(preferredTaskSetId))
            ? Number(preferredTaskSetId)
            : pickLatestScopeId(taskSets.value);
        } catch (error) {
          errorMessage(error);
        } finally {
          loading.taskSets = false;
        }
      }

      function normalizedEpisodePage(value) {
        return QuicDataWorkQueue.normalizePage(value);
      }

      function normalizedEpisodePageSize(value) {
        return QuicDataWorkQueue.normalizePageSize(value, EPISODE_PAGE_SIZES);
      }

      // The collection batch and legacy import APIs are retired. The console
      // reads collection projects, tasks and data packages instead.
      async function loadBatches() {
        batches.value = [];
        selectedBatch.value = null;
        batchImports.value = [];
        batchActivity.value = [];
      }

      async function loadBatchImports() {
        batchImports.value = [];
      }

      async function loadBatchActivity() {
        batchActivity.value = [];
      }

      function episodeRequest() {
        const assetView = activeView.value === 'assets';
        return {
          workspaceId: selectedWorkspaceId.value,
          assetView,
          collectionProjectId: selectedCollectionProjectId.value || null,
          legacyTaskSetId: null,
          batchId: assetView ? null : (selectedBatch.value?.id || null),
          modality: episodeModality.value || '',
          taskLabelId: episodeTaskLabelId.value || '',
          kind: assetKind.value || '',
          keyword: assetView ? assetKeyword.value.trim() : '',
          collectorProfileId: assetView ? assetCollectorId.value : '',
          collectionDeviceId: assetView ? assetDeviceId.value : '',
          publishedFrom: assetView ? assetPublishedRange.value?.[0] || '' : '',
          publishedTo: assetView ? assetPublishedRange.value?.[1] || '' : '',
          sortBy: assetView ? assetSortBy.value : '',
          sortOrder: assetView ? assetSortOrder.value : '',
          page: normalizedEpisodePage(episodePage.value),
          pageSize: normalizedEpisodePageSize(episodePageSize.value),
        };
      }

      function isCurrentEpisodeRequest(request) {
        const current = episodeRequest();
        return Object.keys(current).every((key) => String(current[key] ?? '') === String(request[key] ?? ''));
      }

      function resetEpisodePage() {
        episodePage.value = 1;
      }

      async function loadEpisodes() {
        const assetView = activeView.value === 'assets';
        if (!selectedWorkspaceId.value || (!assetView && !selectedTaskSetId.value)) {
          episodes.value = [];
          episodeTotal.value = 0;
          return;
        }
        const request = episodeRequest();
        const pageQuery = QuicDataWorkQueue.pageQuery({ page: request.page, pageSize: request.pageSize });
        loading.episodes = true;
        try {
          const params = {
            workspace_id: request.workspaceId,
            collection_project_id: request.collectionProjectId || undefined,
            task_set_id: request.legacyTaskSetId || undefined,
            batch_id: request.batchId || undefined,
            modality: request.modality || undefined,
            task_label_id: request.taskLabelId || undefined,
            kind: request.kind || undefined,
            keyword: request.keyword || undefined,
            collector_profile_id: request.collectorProfileId || undefined,
            collection_device_id: request.collectionDeviceId || undefined,
            published_from: request.publishedFrom || undefined,
            published_to: request.publishedTo || undefined,
            sort_by: request.sortBy || undefined,
            sort_order: request.sortOrder || undefined,
            ...pageQuery,
          };
          const data = assetView
            ? await QuicDataAPI.listEpisodeAssets(params)
            : await QuicDataAPI.listEpisodes(params);
          if (!isCurrentEpisodeRequest(request)) return;
          const page = QuicDataWorkQueue.pageResult(data, request.page, request.pageSize);
          if (page.needsReload) {
            episodePage.value = page.lastPage;
            await loadEpisodes();
            return;
          }
          episodes.value = page.items;
          episodeTotal.value = page.total;
          if (assetView) syncListRouteState();
        } catch (error) {
          if (isCurrentEpisodeRequest(request)) errorMessage(error);
        } finally {
          if (isCurrentEpisodeRequest(request)) loading.episodes = false;
        }
      }

      async function reloadEpisodesFromFirstPage() {
        resetEpisodePage();
        assetExpandedIds.value = new Set();
        await loadEpisodes();
      }

      async function clearAssetFilters() {
        assetKeyword.value = '';
        episodeModality.value = '';
        episodeTaskLabelId.value = '';
        assetKind.value = '';
        assetCollectorId.value = '';
        assetDeviceId.value = '';
        assetPublishedRange.value = [];
        await reloadEpisodesFromFirstPage();
      }

      async function clearAssetFilter(key) {
        if (key === 'keyword') assetKeyword.value = '';
        if (key === 'modality') episodeModality.value = '';
        if (key === 'task_label_id') episodeTaskLabelId.value = '';
        if (key === 'kind') assetKind.value = '';
        if (key === 'collector_profile_id') assetCollectorId.value = '';
        if (key === 'collection_device_id') assetDeviceId.value = '';
        if (key === 'published_range') assetPublishedRange.value = [];
        await reloadEpisodesFromFirstPage();
      }

      async function changeAssetSort({ prop, order }) {
        if (!prop || !order) {
          assetSortBy.value = 'published_at';
          assetSortOrder.value = 'desc';
        } else {
          assetSortBy.value = prop;
          assetSortOrder.value = order === 'ascending' ? 'asc' : 'desc';
        }
        await reloadEpisodesFromFirstPage();
      }

      async function changeEpisodePage(page) {
        const nextPage = normalizedEpisodePage(page);
        if (nextPage === episodePage.value) return;
        episodePage.value = nextPage;
        assetExpandedIds.value = new Set();
        await loadEpisodes();
      }

      async function changeEpisodePageSize(pageSize) {
        const nextPageSize = normalizedEpisodePageSize(pageSize);
        if (nextPageSize === episodePageSize.value && episodePage.value === 1) return;
        episodePageSize.value = nextPageSize;
        await reloadEpisodesFromFirstPage();
      }

      let workspaceMembersGeneration = 0;
      async function loadWorkspaceMembers() {
        const generation = ++workspaceMembersGeneration, workspaceId = selectedWorkspaceId.value;
        const isCurrent = () => generation === workspaceMembersGeneration && workspaceId === selectedWorkspaceId.value;
        workspaceMembers.value = [];
        if (!canManageUsers.value || !selectedWorkspaceId.value) return;
        loading.members = true;
        try {
          const data = await QuicDataAPI.listWorkspaceMembers(workspaceId);
          if (isCurrent()) workspaceMembers.value = data.list || [];
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        } finally {
          if (isCurrent()) loading.members = false;
        }
      }

      let managementMembersGeneration = 0;
      async function loadManagementWorkspaceMembers() {
        const generation = ++managementMembersGeneration, workspaceId = managementWorkspaceId.value;
        const isCurrent = () => generation === managementMembersGeneration && workspaceId === managementWorkspaceId.value;
        managementMembers.value = [];
        if (!canManageUsers.value || !managementWorkspaceId.value) return;
        loading.members = true;
        try {
          const data = await QuicDataAPI.listWorkspaceMembers(workspaceId);
          if (isCurrent()) managementMembers.value = data.list || [];
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        } finally {
          if (isCurrent()) loading.members = false;
        }
      }

      async function loadSettingsCenterData() {
        const generation = ++settingsCenterGeneration;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const isSameRequest = () => (
          generation === settingsCenterGeneration
          && workspaceId === (Number(selectedWorkspaceId.value) || 0)
        );
        const isCurrent = () => (
          isSameRequest()
          && canView('settings')
        );
        if (!canView('settings') || !workspaceId) {
          if (generation === settingsCenterGeneration) {
            collectionProjects.value = [];
            collectionLabels.value = [];
            settingsCenterLoadFailures.value = [];
            settingsCenterLoadedWorkspaceId.value = null;
            loadingSettingsCenter.value = false;
          }
          return;
        }
        loadingSettingsCenter.value = true;
        settingsCenterLoadFailures.value = [];
        settingsCenterLoadedWorkspaceId.value = null;
        try {
          const [projectsResult, labelsResult] = await Promise.allSettled([
            Promise.resolve().then(() => QuicDataAPI.listCollectionProjects(workspaceId)),
            Promise.resolve().then(() => QuicDataAPI.listCollectionLabels(workspaceId, null, true)),
          ]);
          if (!isCurrent()) return;
          const listItems = (result) => {
            if (result?.status !== 'fulfilled') return null;
            const value = result.value;
            if (Array.isArray(value)) return value;
            return Array.isArray(value?.items) ? value.items : null;
          };
          const failures = [];
          const projects = listItems(projectsResult);
          const labels = listItems(labelsResult);
          if (projects) {
            collectionProjects.value = projects;
            if (!collectionProjectOptions.value.some((item) => Number(item.id) === Number(selectedCollectionProjectId.value))) {
              selectedCollectionProjectId.value = null;
            }
          } else failures.push('projects');
          if (labels) collectionLabels.value = labels;
          else failures.push('labels');
          settingsCenterLoadFailures.value = failures;
          settingsCenterLoadedWorkspaceId.value = workspaceId;
        } catch (error) {
          if (!isCurrent()) return;
          settingsCenterLoadFailures.value = ['projects', 'labels'];
          settingsCenterLoadedWorkspaceId.value = workspaceId;
          errorMessage(error);
        } finally {
          if (isSameRequest()) loadingSettingsCenter.value = false;
        }
      }

      async function loadPlatformSettings() {
        await loadSettingsCenterData();
      }

      function ensureSettingsCenterWritable() {
        if (settingsCenterWritable.value) return true;
        ElMessage.warning(t('settingsUnavailable'));
        return false;
      }

      function ensureCollectionProjectCreateWritable() {
        if (collectionProjectCreateWritable.value) return true;
        ElMessage.warning(selectedWorkspaceId.value ? t('settingsUnavailable') : t('noWorkspace'));
        return false;
      }

      function resetCollectionProjectDialogState({ invalidate = true } = {}) {
        if (invalidate) collectionProjectWriteGeneration += 1;
        collectionProjectDialogWorkspaceId = 0;
        showCollectionProjectDialog.value = false;
        editingCollectionProject.value = null;
        collectionProjectForm.name = '';
        collectionProjectForm.description = '';
        savingCollectionProject.value = false;
      }

      // Element Plus emits `closed` after the leave transition.  A user can
      // reopen the same dialog before that callback runs; in that case the
      // old callback must not clear the newly opened form.
      function onCollectionProjectDialogClosed() {
        if (showCollectionProjectDialog.value) return;
        resetCollectionProjectDialogState();
      }

      function collectionProjectDialogScopeMatches(workspaceId) {
        return showCollectionProjectDialog.value
          && Number(collectionProjectDialogWorkspaceId) === Number(workspaceId)
          && Number(selectedWorkspaceId.value) === Number(workspaceId);
      }

      function openCreateCollectionProjectDialog() {
        if (!ensureCollectionProjectCreateWritable()) return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId) return;
        collectionProjectWriteGeneration += 1;
        collectionProjectDialogWorkspaceId = workspaceId;
        editingCollectionProject.value = null;
        collectionProjectForm.name = '';
        collectionProjectForm.description = '';
        showCollectionProjectDialog.value = true;
      }

      function handleSettingsCreateCommand(command) {
        if (command === 'workspace') {
          showWorkspaceDialog.value = true;
          return;
        }
        if (command === 'collection-project') openCreateCollectionProjectDialog();
      }

      function openEditCollectionProjectDialog(project) {
        if (!project || !ensureSettingsCenterWritable()) return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId) return;
        collectionProjectWriteGeneration += 1;
        collectionProjectDialogWorkspaceId = workspaceId;
        editingCollectionProject.value = project;
        collectionProjectForm.name = project.name || '';
        collectionProjectForm.description = project.description || '';
        showCollectionProjectDialog.value = true;
      }

      async function saveCollectionProject() {
        if (editingCollectionProject.value) {
          if (!ensureSettingsCenterWritable()) return;
        } else if (!ensureCollectionProjectCreateWritable()) return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const dialogWorkspaceId = Number(collectionProjectDialogWorkspaceId) || 0;
        if (!workspaceId || !dialogWorkspaceId || workspaceId !== dialogWorkspaceId || !showCollectionProjectDialog.value) {
          resetCollectionProjectDialogState();
          ElMessage.warning(t('scopeChanged'));
          return;
        }
        const name = (collectionProjectForm.name || '').trim();
        if (!name) {
          ElMessage.warning('请输入项目名称');
          return;
        }
        if (!selectedWorkspaceId.value) {
          ElMessage.warning(t('noWorkspace'));
          return;
        }
        const requestGeneration = ++collectionProjectWriteGeneration;
        let createdProjectId = null;
        const isCurrent = () => requestGeneration === collectionProjectWriteGeneration
          && collectionProjectDialogScopeMatches(workspaceId);
        savingCollectionProject.value = true;
        try {
          if (editingCollectionProject.value) {
            await QuicDataAPI.updateCollectionProject(editingCollectionProject.value.id, {
              workspace_id: workspaceId,
              name,
              description: (collectionProjectForm.description || '').trim(),
            });
            if (!isCurrent()) return;
            ElMessage.success('项目已更新');
          } else {
            const createdProject = await QuicDataAPI.createCollectionProject({
              workspace_id: workspaceId,
              name,
              description: (collectionProjectForm.description || '').trim(),
            });
            createdProjectId = Number(createdProject?.id) || null;
            if (!isCurrent()) return;
            ElMessage.success('项目已创建');
          }
          if (!isCurrent()) return;
          resetCollectionProjectDialogState();
          if (Number(selectedWorkspaceId.value) !== workspaceId) return;
          if (activeView.value === 'settings') {
            await loadSettingsCenterData();
            if (Number(selectedWorkspaceId.value) !== workspaceId) return;
          }
          await loadCollectionProjectOptions();
          if (Number(selectedWorkspaceId.value) !== workspaceId) return;
          if (createdProjectId && collectionProjectOptions.value.some((item) => Number(item.id) === createdProjectId)) {
            selectedCollectionProjectId.value = createdProjectId;
          }
          if (['miningTasks', 'miningDash', 'batches'].includes(activeView.value)) {
            await loadMiningData();
          }
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        } finally {
          if (requestGeneration === collectionProjectWriteGeneration) savingCollectionProject.value = false;
        }
      }

      async function handleArchiveCollectionProject(project) {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!project?.id || !workspaceId || !ensureSettingsCenterWritable()) return;
        try {
          await ElMessageBox.confirm(
            t('archiveConfirm'),
            t('archiveCollectionProject'),
            { confirmButtonText: t('archiveCollectionProject'), cancelButtonText: t('cancel'), type: 'warning' }
          );
          if (Number(selectedWorkspaceId.value) !== workspaceId || !ensureSettingsCenterWritable()) return;
          await QuicDataAPI.archiveCollectionProject(project.id, workspaceId);
          ElMessage.success('项目已归档');
          await loadSettingsCenterData();
        } catch (e) {
          if (e !== 'cancel') errorMessage(e);
        }
      }

      async function handleCreateCollectionLabel(category) {
        if (!ensureSettingsCenterWritable()) return;
        const catKey = category === 'train' ? 'training' : category;
        const name = (newLabelInputs[category] || newLabelInputs[catKey] || '').trim();
        if (!name) {
          ElMessage.warning('请输入标签名称');
          return;
        }
        if (!selectedWorkspaceId.value) {
          ElMessage.warning(t('noWorkspace'));
          return;
        }
        creatingLabel[category] = true;
        if (creatingLabel[catKey] !== undefined) creatingLabel[catKey] = true;
        try {
          await QuicDataAPI.createCollectionLabel({
            workspace_id: Number(selectedWorkspaceId.value),
            category: catKey,
            name,
            description: '',
          });
          if (newLabelInputs[category] !== undefined) newLabelInputs[category] = '';
          if (newLabelInputs[catKey] !== undefined) newLabelInputs[catKey] = '';
          ElMessage.success(t('labelCreated'));
          await loadSettingsCenterData();
        } catch (error) {
          errorMessage(error);
        } finally {
          creatingLabel[category] = false;
          if (creatingLabel[catKey] !== undefined) creatingLabel[catKey] = false;
        }
      }

      async function handleDeactivateCollectionLabel(label) {
        if (!label?.id || !selectedWorkspaceId.value || !ensureSettingsCenterWritable()) return;
        try {
          await QuicDataAPI.deactivateCollectionLabel(label.id, Number(selectedWorkspaceId.value));
          ElMessage.success(t('labelDeactivated'));
          await loadSettingsCenterData();
        } catch (error) {
          errorMessage(error);
        }
      }

      function scopeWorkspaceName(scope) {
        return workspaces.value.find((item) => Number(item.id) === Number(scope?.workspace_id))?.name || `#${scope?.workspace_id}`;
      }

      function scopeTaskSetName(scope) {
        return taskSets.value.find((item) => Number(item.id) === Number(scope?.task_set_id))?.name || `#${scope?.task_set_id}`;
      }

      function externalProjectStatus(taskSet) {
        const externalProject = taskSet?.external_project;
        if (!externalProject) return t('externalProjectUnbound');
        return externalProject.display_name || externalProject.external_project_id || t('externalProjectBound');
      }

      async function loadAdmin() {
        managedUsers.value = [];
        roleDefinitions.value = {};
        if (!canManageUsers.value) return;
        loading.users = true;
        try {
          const [users, roles] = await Promise.all([
            QuicDataAPI.listUsers(),
            QuicDataAPI.listRoles(),
          ]);
          managedUsers.value = Array.isArray(users) ? users : users.items || [];
          roleDefinitions.value = roles || {};
          if (!assignableRoles.value.includes(managedUserForm.role)) {
            managedUserForm.role = assignableRoles.value.includes('viewer') ? 'viewer' : assignableRoles.value[0] || '';
          }
        } catch (error) {
          errorMessage(error);
        } finally {
          loading.users = false;
        }
        try {
          const result = await QuicDataAPI.listWorkspaces({ scope: 'management' });
          managementWorkspaces.value = sortScopeItemsNewestFirst(result.list || []);
          if (!managementWorkspaces.value.some(item => Number(item.id) === Number(managementWorkspaceId.value))) {
            managementWorkspaceId.value = managementWorkspaces.value[0]?.id || null;
          }
        } catch (error) {
          managementWorkspaces.value = [];
          managementMembers.value = [];
          managementWorkspaceId.value = null;
          errorMessage(error);
        }
        await loadWorkspaceMembers();
        await loadManagementWorkspaceMembers();
      }

      function openManagedUserDialog() {
        managedUserForm.email = '';
        managedUserForm.password = '';
        managedUserForm.role = assignableRoles.value.includes('viewer') ? 'viewer' : assignableRoles.value[0] || '';
        showManagedUserDialog.value = true;
      }

      function openWorkspaceMemberDialog() {
        workspaceMemberForm.user_id = availableWorkspaceMembers.value[0]?.id || null;
        showWorkspaceMemberDialog.value = true;
      }

      async function createManagedUser() {
        if (!canManageUsers.value || saving.value) return;
        const email = managedUserForm.email.trim();
        if (!email || managedUserForm.password.length < 10 || !managedUserForm.role) return;
        saving.value = true;
        try {
          await QuicDataAPI.createManagedUser({
            email,
            password: managedUserForm.password,
            role: managedUserForm.role,
          });
          showManagedUserDialog.value = false;
          await loadAdmin();
          ElMessage.success(t('userCreated'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function changeManagedUserRole(managedUser, role) {
        if (!canManageUsers.value || saving.value || !managedUser?.id || !role || role === managedUser.role) return;
        saving.value = true;
        try {
          await QuicDataAPI.updateUserRole(managedUser.id, role);
          await loadAdmin();
          if (String(managedUser.id) === String(user.value?.id)) {
            await signOut();
            return;
          }
          ElMessage.success(t('actionSucceeded'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function setManagedUserActive(managedUser, isActive) {
        if (!canManageUsers.value || saving.value || !managedUser?.id || String(managedUser.id) === String(user.value?.id)) return;
        saving.value = true;
        try {
          await QuicDataAPI.updateUserStatus(managedUser.id, Boolean(isActive));
          await loadAdmin();
          ElMessage.success(t('actionSucceeded'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function resetManagedUserPassword(managedUser) {
        if (!canManageUsers.value || saving.value || !managedUser?.id) return;
        try {
          await ElMessageBox.confirm(
            t('resetPasswordConfirm', { email: managedUser.email }),
            t('resetPassword'),
            { confirmButtonText: t('resetPassword'), cancelButtonText: t('cancel'), type: 'warning' },
          );
        } catch (error) {
          if (error !== 'cancel') errorMessage(error);
          return;
        }
        saving.value = true;
        try {
          const res = await QuicDataAPI.resetUserPassword(managedUser.id);
          ElMessageBox.alert(
            t('resetPasswordOnce', { password: res.temporary_password }),
            t('resetPasswordSuccess'),
            { confirmButtonText: t('ok'), type: 'warning' },
          );
          await loadAdmin();
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function grantWorkspaceMember() {
        const userId = Number(workspaceMemberForm.user_id);
        if (!canManageUsers.value || saving.value || !managementWorkspaceId.value || !Number.isInteger(userId) || userId <= 0) return;
        const workspaceId = Number(managementWorkspaceId.value);
        const addingSelf = String(userId) === String(user.value?.id);
        saving.value = true;
        try {
          await QuicDataAPI.grantWorkspaceMember(workspaceId, {
            user_id: userId,
          });
          showWorkspaceMemberDialog.value = false;
          await loadManagementWorkspaceMembers();
          await loadWorkspaces({ preferredWorkspaceId: addingSelf ? workspaceId : null });
          if (addingSelf && Number(selectedWorkspaceId.value) === workspaceId) {
            await switchWorkspace();
          }
          ElMessage.success(t('memberGranted'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function revokeWorkspaceMember(member) {
        if (!canManageUsers.value || saving.value || !managementWorkspaceId.value || !member?.user_id) return;
        if (typeof window !== 'undefined' && typeof window.confirm === 'function' && !window.confirm(`${t('revokeWorkspaceMember')} ${member.email}?`)) return;
        saving.value = true;
        try {
          await QuicDataAPI.revokeWorkspaceMember(managementWorkspaceId.value, member.user_id);
          await loadManagementWorkspaceMembers();
          await loadWorkspaces();
          ElMessage.success(t('memberRevoked'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      function normalizedWorkQueuePage(value) {
        return QuicDataWorkQueue.normalizePage(value);
      }

      function normalizedWorkQueuePageSize(value) {
        return QuicDataWorkQueue.normalizePageSize(value, WORK_QUEUE_PAGE_SIZES);
      }

      function workQueueRequest() {
        return QuicDataWorkQueue.requestSnapshot({
          workspaceId: selectedWorkspaceId.value,
          taskSetId: queueTaskSetId.value || null,
          taskLabelId: queueTaskLabelId.value || null,
          reviewTargetKind: queueStage.value === 'review' ? queueReviewTargetKind.value : '',
          status: queueStatus.value || '',
          episodeKeyword: queueEpisodeKeyword.value.trim(),
          updatedFrom: queueUpdatedRange.value?.[0] || '',
          updatedTo: queueUpdatedRange.value?.[1] || '',
          sortBy: queueSortBy.value || '',
          sortOrder: queueSortOrder.value || '',
          stage: queueStage.value,
          page: queuePage.value,
          pageSize: queuePageSize.value,
        }, WORK_QUEUE_PAGE_SIZES);
      }

      function isCurrentWorkQueueRequest(request) {
        return QuicDataWorkQueue.sameRequest(request, workQueueRequest());
      }

      function workQueueQuery(request = workQueueRequest()) {
        return QuicDataWorkQueue.requestQuery(request, WORK_QUEUE_PAGE_SIZES);
      }

      function resetWorkQueuePage() {
        queuePage.value = 1;
      }

      function applyWorkQueueResponse(data, request) {
        const page = QuicDataWorkQueue.pageResult(data, request.page, request.pageSize);
        if (page.needsReload) {
          queuePage.value = page.lastPage;
          return false;
        }
        queueRows.value = page.items;
        queueTotal.value = page.total;
        syncListRouteState();
        return true;
      }

      function applyWorkQueueMutation(row) {
        const result = QuicDataWorkQueue.applyActionRow(
          queueRows.value,
          queueTotal.value,
          row,
          queueStage.value,
        );
        if (!result.changed) return false;
        queueRows.value = result.items;
        queueTotal.value = result.total;
        return true;
      }

      async function loadWorkQueue() {
        if (governanceQueueActive.value) {
          if (queueStage.value === 'annotation') await loadAnnotationWorkItems();
          else await loadReviewWorkItems();
          return;
        }
        if (!selectedWorkspaceId.value) return;
        const requestedStage = queueStage.value;
        const request = { ...workQueueRequest(), stage: requestedStage };
        loading.queue = true;
        try {
          const data = await QuicDataAPI.listWorkQueue(workQueueQuery(request));
          if (requestedStage !== queueStage.value) return;
          if (!isCurrentWorkQueueRequest(request)) return;
          if (!applyWorkQueueResponse(data, request)) {
            await loadWorkQueue();
            subscribeWorkspaceQueue();
          }
        } catch (error) {
          if (requestedStage === queueStage.value && isCurrentWorkQueueRequest(request)) errorMessage(error);
        } finally {
          if (requestedStage === queueStage.value && isCurrentWorkQueueRequest(request)) loading.queue = false;
        }
      }

      function ensureActiveViewData(view) {
        if (view === 'batches') void loadReviewPackages();
        if (['resources', 'intake', 'work-queue'].includes(view) && !collectionProjects.value.length) void loadCollectionProjectOptions();
        if (view === 'miningTasks' && !collectorProfiles.value.length && !collectionDevices.value.length && !loading.resources) loadCollectionResources();
        if ((view === 'batches' || view === 'miningDash' || view === 'miningTasks') && !miningTasks.value.length) loadMiningData();
        if (view === 'buildData') loadEpisodes();
      }

      watch(activeView, (view) => {
        if (COLLECTION_VIEWS.has(view) && !availableScopeWorkspaces.value.some(item => Number(item.id) === Number(selectedWorkspaceId.value))) {
          selectedWorkspaceId.value = pickLatestScopeId(availableScopeWorkspaces.value);
          void switchWorkspace();
        } else ensureActiveViewData(view);
      });

      const workQueueRefreshCoordinator = QuicDataWorkQueue.createRefreshCoordinator({
        refresh: loadWorkQueue,
        setTimer: (callback, delayMs) => window.setTimeout(callback, delayMs),
        clearTimer: (timerId) => window.clearTimeout(timerId),
        delayMs: 250,
      });

      function cancelScheduledWorkQueueRefresh() {
        workQueueRefreshCoordinator.cancel();
      }

      function beginWorkQueueRefresh() {
        return workQueueRefreshCoordinator.begin();
      }

      function scheduleWorkQueueRefresh({ immediate = false } = {}) {
        if (!selectedWorkspaceId.value) return Promise.resolve();
        return workQueueRefreshCoordinator.schedule({ immediate });
      }

      async function changeWorkQueuePage(page) {
        const nextPage = normalizedWorkQueuePage(page);
        if (nextPage === queuePage.value) return;
        queuePage.value = nextPage;
        await loadWorkQueue();
        subscribeWorkspaceQueue();
      }

      async function changeWorkQueuePageSize(pageSize) {
        const nextPageSize = normalizedWorkQueuePageSize(pageSize);
        if (nextPageSize === queuePageSize.value && queuePage.value === 1) return;
        queuePageSize.value = nextPageSize;
        resetWorkQueuePage();
        await loadWorkQueue();
        subscribeWorkspaceQueue();
      }

      async function reloadWorkQueueFromFirstPage() {
        resetWorkQueuePage();
        await loadWorkQueue();
        subscribeWorkspaceQueue();
      }

      function scheduleWorkQueueEpisodeSearch() {
        resetWorkQueuePage();
        // Keep the shareable URL current without dispatching a hashchange for every keystroke.
        syncListRouteState();
        void scheduleWorkQueueRefresh();
        subscribeWorkspaceQueue();
      }

      async function submitWorkQueueEpisodeSearch() {
        cancelScheduledWorkQueueRefresh();
        resetWorkQueuePage();
        syncListRouteState();
        await loadWorkQueue();
        subscribeWorkspaceQueue();
      }

      async function clearWorkQueueFilters() {
        queueTaskSetId.value = '';
        queueTaskLabelId.value = '';
        queueReviewTargetKind.value = '';
        queueStatus.value = '';
        queueEpisodeKeyword.value = '';
        queueUpdatedRange.value = [];
        await reloadWorkQueueFromFirstPage();
      }

      async function clearWorkQueueFilter(key) {
        if (key === 'task_set_id') queueTaskSetId.value = '';
        if (key === 'task_label_id') queueTaskLabelId.value = '';
        if (key === 'review_target_kind') queueReviewTargetKind.value = '';
        if (key === 'status') queueStatus.value = '';
        if (key === 'episode_keyword') queueEpisodeKeyword.value = '';
        if (key === 'updated_range') queueUpdatedRange.value = [];
        await reloadWorkQueueFromFirstPage();
      }

      async function changeWorkQueueSort({ prop, order }) {
        if (!prop || !order) {
          queueSortBy.value = '';
          queueSortOrder.value = '';
        } else {
          queueSortBy.value = prop;
          queueSortOrder.value = order === 'ascending' ? 'asc' : 'desc';
        }
        await reloadWorkQueueFromFirstPage();
      }

      function formatDelta(value) {
        const number = Number(value || 0);
        if (!Number.isFinite(number) || number === 0) return { text: `0.0% ${t('vsYesterday')}`, tone: 'flat' };
        const percent = `${number > 0 ? '+' : ''}${(number * 100).toFixed(1)}%`;
        return { text: `${percent} ${t('vsYesterday')}`, tone: number > 0 ? 'up' : 'down' };
      }

      function formatPercent(value) {
        const number = Number(value || 0);
        if (!Number.isFinite(number)) return '0%';
        return `${(number * 100).toFixed(1)}%`;
      }

      function dashboardMetricValue(value) {
        return value === null || value === undefined || value === '' ? '—' : value;
      }

      function dashboardPercent(value) {
        return value === null || value === undefined || value === '' ? '—' : formatPercent(value);
      }

      function dashboardDelta(value) {
        if (value === null || value === undefined || value === '') return { text: '—', tone: 'flat' };
        return formatDelta(value);
      }

      function disposeDashboardCharts() {
        Object.keys(dashboardCharts).forEach((key) => {
          if (dashboardCharts[key]) {
            dashboardCharts[key].dispose();
            dashboardCharts[key] = null;
          }
        });
      }

      function dashboardChartInstance(key, el) {
        if (!el || typeof echarts === 'undefined') return null;
        const existing = dashboardCharts[key];
        if (existing) {
          if (typeof existing.getDom === 'function' && existing.getDom() === el) return existing;
          existing.dispose();
          dashboardCharts[key] = null;
        }
        dashboardCharts[key] = echarts.init(el);
        return dashboardCharts[key];
      }

      function resizeDashboardCharts() {
        Object.keys(dashboardCharts).forEach((key) => {
          const chart = dashboardCharts[key];
          if (chart && typeof chart.resize === 'function') chart.resize();
        });
      }

      function renderDashboardCharts() {
        if (!canViewDashboard.value || activeView.value !== 'overview' || typeof echarts === 'undefined') return;
        const payload = dashboardOverview.value;
        if (!payload) return;
        const stages = payload.pipeline_funnel?.stages || [];
        const bucketKeys = ['lt_30s', 'bt_30_60s', 'gt_60s', 'unknown'];
        const bucketLabels = ['<30s', '30-60s', '>60s', t('durationUnknown')];
        const colors = ['#3568d4', '#2f9e74', '#7b6fd6'];
        const funnelChart = dashboardChartInstance('funnel', funnelChartEl.value);
        if (funnelChart) {
          funnelChart.setOption({
            color: colors,
            tooltip: { trigger: 'axis' },
            legend: { data: bucketLabels, top: 0 },
            grid: { left: 40, right: 16, top: 36, bottom: 28 },
            xAxis: { type: 'category', data: stages.map((stage) => stage.label || stage.key) },
            yAxis: { type: 'value', minInterval: 1 },
            series: bucketKeys.map((key, index) => ({
              name: bucketLabels[index],
              type: 'bar',
              barGap: '12%',
              data: stages.map((stage) => Number(stage.buckets?.[key] || 0)),
            })),
          }, true);
        }
        const trend = payload.collect_trend_7d || [];
        const trendChart = dashboardChartInstance('trend', trendChartEl.value);
        if (trendChart) {
          trendChart.setOption({
            color: ['#3568d4'],
            tooltip: { trigger: 'axis' },
            grid: { left: 40, right: 16, top: 24, bottom: 28 },
            xAxis: { type: 'category', boundaryGap: false, data: trend.map((item) => String(item.date || '').slice(5)) },
            yAxis: { type: 'value', minInterval: 1 },
            series: [{
              type: 'line',
              smooth: true,
              areaStyle: { color: 'rgba(53,104,212,0.16)' },
              data: trend.map((item) => Number(item.count || 0)),
            }],
          }, true);
        }
        const devices = payload.device_distribution?.items || [];
        const deviceChart = dashboardChartInstance('device', deviceChartEl.value);
        if (deviceChart) {
          deviceChart.setOption({
            color: ['#3568d4', '#2f9e74', '#d4a017', '#7b6fd6', '#6b7c93', '#4aa3df'],
            tooltip: { trigger: 'item' },
            legend: { orient: 'vertical', right: 0, top: 'middle', type: 'scroll' },
            series: [{
              type: 'pie',
              radius: ['48%', '72%'],
              center: ['36%', '50%'],
              label: { show: false },
              data: devices.map((item) => ({ name: item.name || '—', value: Number(item.count || 0) })),
            }],
          }, true);
        }
        resizeDashboardCharts();
      }

      async function scheduleDashboardCharts() {
        if (!canViewDashboard.value || activeView.value !== 'overview' || !dashboardOverview.value) return;
        if (typeof echarts === 'undefined' && typeof QuicStudioPageAssets !== 'undefined') {
          await QuicStudioPageAssets.load('charts');
        }
        if (activeView.value !== 'overview') return;
        await nextTick();
        renderDashboardCharts();
        if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
          window.requestAnimationFrame(() => {
            if (activeView.value === 'overview') {
              renderDashboardCharts();
              resizeDashboardCharts();
            }
          });
        }
      }

      function dashboardScope() {
        // The top selector now contains collection-project IDs.  The dashboard
        // warehouse still stores the legacy task_set_id scope, so forwarding
        // the collection-project ID as task_set_id can look like a cross-
        // workspace request (for example project 1 in workspace 2 vs. legacy
        // task set 1 in workspace 1).  Until the warehouse gains a
        // collection-project dimension, keep dashboard reads at workspace
        // granularity instead of sending a mismatched legacy ID.
        return QuicDataDashboardState.resolveScope(
          user.value,
          selectedWorkspaceId.value,
          null,
        );
      }

      let activeDashboardRefreshJobId = null;

      async function loadDashboardOverview({ manageLoading = true, generation: requestedGeneration = null, workspaceId: requestedWorkspaceId = null, scope: requestedScope = null } = {}) {
        const suppliedGeneration = Number(requestedGeneration);
        const generation = requestedGeneration == null || !Number.isFinite(suppliedGeneration)
          ? ++dashboardOverviewGeneration
          : suppliedGeneration;
        const workspaceId = requestedWorkspaceId == null
          ? Number(selectedWorkspaceId.value) || 0
          : Number(requestedWorkspaceId) || 0;
        const scope = requestedScope || dashboardScope();
        const isCurrent = () => generation === dashboardOverviewGeneration
          && workspaceId === (Number(selectedWorkspaceId.value) || 0);
        if (!canViewDashboard.value) {
          if (isCurrent()) {
            dashboardOverview.value = null;
            dashboardOverviewError.value = '';
          }
          disposeDashboardCharts();
          return;
        }
        if (manageLoading) loading.dashboard = true;
        if (isCurrent()) dashboardOverviewError.value = '';
        try {
          const payload = requestedScope
            ? await QuicDataAPI.getDashboardOverview(scope)
            : await QuicDataAPI.getDashboardOverview(dashboardScope());
          if (!isCurrent()) return;
          dashboardOverview.value = payload;
          await scheduleDashboardCharts();
        } catch (error) {
          if (!isCurrent()) return;
          dashboardOverview.value = null;
          dashboardOverviewError.value = error?.message || t('dashboardLoadFailed');
          disposeDashboardCharts();
          errorMessage(error);
        } finally {
          if (isCurrent() && manageLoading && !activeDashboardRefreshJobId) loading.dashboard = false;
        }
      }

      async function completeDashboardRefresh(receipt, job) {
        if (String(activeDashboardRefreshJobId || '') !== String(receipt.job_id || '')) return;
        const requestGeneration = Number(receipt.dashboard_generation);
        const requestWorkspaceId = Number(receipt.dashboard_workspace_id);
        const hasRequestContext = Number.isFinite(requestGeneration) && Number.isFinite(requestWorkspaceId);
        const isCurrent = () => (!hasRequestContext
          || (requestGeneration === dashboardOverviewGeneration
            && requestWorkspaceId === (Number(selectedWorkspaceId.value) || 0)))
          && QuicDataDashboardState.scopeKey(dashboardScope()) === receipt.scope_key;
        if (QuicDataDashboardState.jobFailed(job)) {
          if (!isCurrent()) return;
          removeSubscription(`dashboard-refresh:${receipt.job_id}`);
          activeDashboardRefreshJobId = null;
          loading.dashboard = false;
          ElMessage.error(job.error_message || t('jobFailed'));
          return;
        }
        if (!QuicDataDashboardState.jobSucceeded(job)) return;
        try {
          if (!isCurrent()) return;
          const payload = receipt.payload && QuicDataDashboardState.isFreshSnapshot(receipt.payload, receipt)
            ? receipt.payload
            : await QuicDataAPI.getDashboardOverview(receipt.scope);
          if (!isCurrent()) return;
          if (!QuicDataDashboardState.isFreshSnapshot(payload, receipt)) {
            throw new Error('看板作业已完成，但新快照尚不可用');
          }
          if (!isCurrent()) return;
          dashboardOverview.value = payload;
          dashboardOverviewError.value = '';
          await scheduleDashboardCharts();
        } catch (error) {
          if (isCurrent()) {
            dashboardOverviewError.value = error?.message || t('dashboardLoadFailed');
            errorMessage(error);
          }
        } finally {
          removeSubscription(`dashboard-refresh:${receipt.job_id}`);
          if (isCurrent() && String(activeDashboardRefreshJobId || '') === String(receipt.job_id || '')) {
            activeDashboardRefreshJobId = null;
            loading.dashboard = false;
          }
        }
      }

      async function refreshDashboardData() {
        if (!canViewDashboard.value || loading.dashboard) return;
        const requestGeneration = ++dashboardOverviewGeneration;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const scope = dashboardScope();
        const isCurrent = () => requestGeneration === dashboardOverviewGeneration
          && workspaceId === (Number(selectedWorkspaceId.value) || 0);
        loading.dashboard = true;
        try {
          await loadDashboardOverview({ manageLoading: false, generation: requestGeneration, workspaceId, scope });
          if (!isCurrent()) return;
          if (demoMode.value || typeof QuicDataRealtime === 'undefined') {
            return;
          }
          const refreshResult = await QuicDataAPI.refreshDashboard(scope);
          if (!isCurrent()) return;
          const receipt = {
            ...refreshResult,
            scope,
            dashboard_generation: requestGeneration,
            dashboard_workspace_id: workspaceId,
          };
          activeDashboardRefreshJobId = receipt.job_id || null;
          if (!receipt.job_id || QuicDataDashboardState.jobFailed(receipt)) {
            if (QuicDataDashboardState.jobFailed(receipt)) ElMessage.error(receipt.error_message || t('jobFailed'));
            activeDashboardRefreshJobId = null;
            loading.dashboard = false;
            return;
          }
          if (receipt.payload && QuicDataDashboardState.isFreshSnapshot(receipt.payload, receipt)) {
            if (!isCurrent() || QuicDataDashboardState.scopeKey(dashboardScope()) !== receipt.scope_key) return;
            dashboardOverview.value = receipt.payload;
            dashboardOverviewError.value = '';
            await scheduleDashboardCharts();
            if (!isCurrent()) return;
          }
          if (QuicDataDashboardState.jobSucceeded(receipt)) {
            await completeDashboardRefresh(receipt, receipt);
            return;
          }
          loading.dashboard = false;
          ensureRealtimeConnection();
          replaceSubscription(`dashboard-refresh:${receipt.job_id}`, {
            resourceType: 'job_run',
            resourceId: String(receipt.job_id),
            refreshAlways: true,
            refresh: async () => (await QuicDataAPI.getJob(receipt.job_id)).job,
            onUpdate: (job) => { void completeDashboardRefresh(receipt, job); },
          });
        } catch (error) {
          if (isCurrent()) {
            errorMessage(error);
            dashboardOverviewError.value = error?.message || t('dashboardLoadFailed');
            activeDashboardRefreshJobId = null;
            loading.dashboard = false;
          }
        } finally {
          if (isCurrent() && !activeDashboardRefreshJobId) loading.dashboard = false;
        }
      }

      watch(activeView, (next, previous) => {
        if (previous === 'overview' && next !== 'overview') {
          disposeDashboardCharts();
          return;
        }
        if (next === 'overview' && canViewDashboard.value) void scheduleDashboardCharts();
      }, { flush: 'post' });

      watch(ready, (isReady) => {
        if (isReady && activeView.value === 'overview' && canViewDashboard.value) void scheduleDashboardCharts();
      }, { flush: 'post' });

      async function loadAuthorizedConsoleData() {
        const view = activeView.value;
        const loaders = [];
        if (['resources', 'intake', 'work-queue'].includes(view)) loaders.push(loadCollectionProjectOptions());
        if (view === 'work-queue' && canView('work-queue')) loaders.push(loadWorkQueue());
        if (view === 'resources' && canView('resources')) loaders.push(loadCollectionResources());
        if (canViewDashboard.value && view === 'overview') loaders.push(loadDashboardOverview());
        if (['miningTasks', 'miningDash', 'batches'].includes(view) && canView(view)) {
          loaders.push(loadMiningData());
          if (view === 'miningTasks') loaders.push(loadCollectionResources());
          if (view === 'batches') loaders.push(loadReviewPackages());
        }
        await Promise.allSettled(loaders);
      }

      async function switchWorkspace() {
        resetCollectionProjectDialogState();
        closeCollectorDialog();
        closeDeviceDialog();
        resetMiningTaskDialogState();
        resetMiningSplitDialogState();
        resetMiningAssignDialogState();
        exitOverviewBuildMode();
        showCollectorQrDialog.value = false;
        selectedCollectorForQr.value = null;
        showMiningTaskPackagesPage.value = false;
        miningSelectedTaskId.value = '';
        settingsCenterGeneration += 1;
        settingsCenterLoadFailures.value = [];
        settingsCenterLoadedWorkspaceId.value = null;
        loadingSettingsCenter.value = false;
        miningDataGeneration += 1;
        miningTaskSelectionGeneration += 1;
        collectionResourcesGeneration += 1;
        resourceWriteGeneration += 1;
        batchCandidatesGeneration += 1;
        dashboardOverviewGeneration += 1;
        dashboardOverview.value = null;
        dashboardOverviewError.value = '';
        reviewPackagesGeneration += 1;
        reviewPackagePage.value = 1;
        reviewPackageTotal.value = 0;
        loadAnnotationWorkItemsGeneration += 1;
        loadReviewWorkItemsGeneration += 1;
        reviewPackageRows.value = [];
        reviewPackageSelectedIds.value = [];
        annotationWorkItems.value = [];
        reviewWorkItems.value = [];
        collectionProjects.value = [];
        miningDashFilters.project = [];
        collectionLabels.value = [];
        miningTasks.value = [];
        pendingBuildPackageIds.value = [];
        dataPackageDrawerVisible.value = false;
        closeEpisodeDetail();
        clearSubscriptions();
        resetNativeLerobotBatchState();
        activeDashboardRefreshJobId = null;
        loading.dashboard = false;
        loading.miningPackages = false;
        resetWorkQueuePage();
        resetEpisodePage();
        queueTaskSetId.value = '';
        queueTaskLabelId.value = '';
        queueReviewTargetKind.value = '';
        queueStatus.value = '';
        queueEpisodeKeyword.value = '';
        queueUpdatedRange.value = [];
        assetKeyword.value = '';
        episodeModality.value = '';
        episodeTaskLabelId.value = '';
        assetKind.value = '';
        assetCollectorId.value = '';
        assetDeviceId.value = '';
        assetPublishedRange.value = [];
        if (hasPermission('workspace:read')) await loadTaskSets();
        persistScopePreference();
        await loadAuthorizedConsoleData();
        if (activeView.value === 'settings' && canView('settings')) await loadSettingsCenterData();
        if (activeView.value === 'admin' && canView('admin')) await loadWorkspaceMembers();
        subscribeWorkspaceQueue();
      }

      async function switchTaskSet() {
        closeEpisodeDetail();
        removeSubscription(`dashboard-refresh:${activeDashboardRefreshJobId}`);
        removeSubscription('native-lerobot-scan');
        resetNativeLerobotBatchState();
        activeDashboardRefreshJobId = null;
        loading.dashboard = false;
        selectedBatch.value = null;
        batchImports.value = [];
        batchActivity.value = [];
        const loaders = [];
        if (canView('batches') || canView('intake')) loaders.push(loadBatches());
        if (canView('assets') || canView('work-queue')) loaders.push(loadTaskLabels());
        if (canViewDashboard.value && activeView.value === 'overview') loaders.push(loadDashboardOverview());
        await Promise.all(loaders);
        persistScopePreference();
      }

      async function switchQueueStage(stage) {
        setQueueStageValue(stage);
        queueStageInitialized = true;
        queueStatus.value = '';
        resetWorkQueuePage();
        await loadWorkQueue();
        subscribeWorkspaceQueue();
      }

      async function switchWorkQueueWorkspace() {
        resetWorkQueuePage();
        await loadWorkQueue();
      }

      function openQueueStage(stage) {
        setQueueStageValue(stage);
        queueStageInitialized = true;
        queueStatus.value = '';
        resetWorkQueuePage();
        workbenchQueueOpen.value = true;
        navigate('work-queue', { queueStageOverride: stage });
      }

      async function syncRoute() {
        if (!user.value || showChangePassword.value || mustChangePassword.value) return;
        const wasWorkbench = activeView.value === 'workbench';
        const route = routeStateFromLocation();
        if (activeView.value === 'package-workbench' && window.location.hash !== packageWorkbenchHash()) {
          const requestedHash = window.location.hash;
          if (packageAnnotationWorkbenchRef.value && !await packageAnnotationWorkbenchRef.value.canLeave()) {
            window.location.hash = packageWorkbenchHash();
            return;
          }
          if (window.location.hash !== requestedHash) return;
        }
        if (route.view === 'package-workbench' && !applyPackageWorkbenchRoute(route)) return;
        if (activeView.value === 'intake-review' && (route.view !== 'intake-review' || Number(route.query.package_id) !== Number(intakeReviewPackageId.value))) {
          const requestedHash = window.location.hash;
          if (intakeReviewWorkbenchRef.value && !await intakeReviewWorkbenchRef.value.canLeave()) {
            window.location.hash = `#/intake-review?package_id=${intakeReviewPackageId.value}`;
            return;
          }
          if (window.location.hash !== requestedHash) return;
        }
        const nextView = setAuthorizedView(route.view, { warn: true, preserveAuthorizedHash: true });
        if (!nextView) return;
        if (nextView === route.view) applyListRouteQuery(route);
        if (nextView === 'workbench') {
          workbenchRoute.workItemId = route.workItemId;
          workbenchRoute.episodeId = route.episodeId;
        }
        if (nextView === 'work-queue') {
          void scheduleWorkQueueRefresh({ immediate: true });
          subscribeWorkspaceQueue();
        }
        if (nextView === 'batches') {
          // 数采审核 shows the data package review queue; batches open only when
          // a caller explicitly selects one (legacy import / LeRobot batch).
          void loadBatches();
        }
        if (nextView === 'miningTasks') {
          const taskId = route.query.task_id || '';
          if (taskId) {
            showMiningTaskPackagesPage.value = true;
            if (String(miningSelectedTaskId.value) !== String(taskId)) {
              void selectMiningTask(taskId, { preserveScroll: false });
            }
          } else {
            showMiningTaskPackagesPage.value = false;
            closeMiningInlinePackageDetail();
          }
        }
        if (nextView === 'intake-review') {
          const id = Number(route.query.package_id || intakeReviewPackageId.value || 0);
          if (id > 0 && id !== Number(intakeReviewPackageId.value)) {
            intakeReviewPackageId.value = id;
          }
        }
        if (nextView === 'buildData') void loadEpisodes();
        if (nextView === 'resources') void loadCollectionResources();
        if (nextView === 'admin') void loadAdmin();
        if (nextView === 'settings') void loadPlatformSettings();
        if (['miningTasks', 'miningCloud', 'miningCuts'].includes(nextView)) {
          void loadMiningData();
          if (nextView === 'miningCuts') void loadMiningCuts();
        }
        if (nextView === 'miningConfig') void loadMiningConfigData();
        if (nextView === 'overview' && canViewDashboard.value) void loadDashboardOverview();
        if (nextView === 'workbench') void loadWorkbench();
        else {
          if (wasWorkbench) {
            const coordinator = workbenchSaveCoordinator;
            const item = activeWorkbench.value?.work_item;
            const canPersist = demoMode.value || item?.available_actions?.includes('save_draft');
            if (workbenchDraftDirty.value && coordinator && canPersist) {
              void coordinator.flush({ drain: true, reason: 'navigation' }).finally(() => {
                if (workbenchSaveCoordinator === coordinator && activeView.value !== 'workbench') {
                  disposeWorkbenchSaveCoordinator();
                }
              });
            } else {
              disposeWorkbenchSaveCoordinator();
            }
          }
          removeSubscription('workbench-item');
          removeSubscription('workbench-episode');
        }
      }

      async function navigate(view, { queueStageOverride = null, refreshQueue = true } = {}) {
        if (!VIEWS.has(view) || view === 'workbench') return;
        if (activeView.value === 'package-workbench' && view !== 'package-workbench' && packageAnnotationWorkbenchRef.value && !await packageAnnotationWorkbenchRef.value.canLeave()) return;
        if (activeView.value === 'intake-review' && view !== 'intake-review' && intakeReviewWorkbenchRef.value && !await intakeReviewWorkbenchRef.value.canLeave()) return;
        if (activeView.value === 'workbench' && workbenchDraftDirty.value) {
          const item = activeWorkbench.value?.work_item;
          const canPersist = demoMode.value || item?.available_actions?.includes('save_draft');
          if (canPersist && !await saveWorkbenchDraft({ quiet: true })) return;
        }
        if (activeView.value === 'workbench') disposeWorkbenchSaveCoordinator();
        const nextQueueStageOverride = queueStageOverride
          || (view === 'work-queue' && queueStageInitialized ? queueStage.value : null);
        const nextView = setAuthorizedView(view, {
          warn: true,
          queueStageOverride: nextQueueStageOverride,
          preserveAuthorizedHash: view === 'work-queue',
        });
        if (!nextView) return;
        workbenchRoute.workItemId = '';
        workbenchRoute.episodeId = '';
        removeSubscription('workbench-item');
        removeSubscription('workbench-episode');
        const queueRouteChanged = nextView === 'work-queue' && syncListRouteState({ push: true });
        if (nextView === 'work-queue' && refreshQueue && !queueRouteChanged) {
          void scheduleWorkQueueRefresh({ immediate: true });
          subscribeWorkspaceQueue();
        } else if (nextView === 'work-queue') {
          subscribeWorkspaceQueue();
        }
        if (nextView === 'batches') {
          // 数采审核 shows the data package review queue; batches open only when
          // a caller explicitly selects one (legacy import / LeRobot batch).
          void loadBatches();
        }
        if (nextView === 'buildData') void loadEpisodes();
        if (nextView === 'resources') void loadCollectionResources();
        if (nextView === 'miningTasks' || nextView === 'miningCloud' || nextView === 'miningCuts') {
          loadMiningData();
          loadMiningCuts();
        }
        if (nextView === 'miningConfig') loadMiningConfigData();
        if (nextView === 'admin') void loadAdmin();
        if (nextView === 'settings') void loadPlatformSettings();
        if (nextView === 'overview' && canViewDashboard.value) void loadDashboardOverview();
      }

      async function createWorkspace() {
        saving.value = true;
        try {
          const workspace = await QuicDataAPI.createWorkspace(workspaceForm);
          showWorkspaceDialog.value = false;
          workspaceForm.workspace_name = '';
          workspaceForm.desc = '';
          await loadWorkspaces();
          selectedWorkspaceId.value = workspace.id;
          await switchWorkspace();
          ElMessage.success(t('workspaceCreated'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function createTaskSet() {
        if (!selectedWorkspaceId.value) return;
        saving.value = true;
        try {
          await QuicDataAPI.createCollectionProject({ workspace_id: selectedWorkspaceId.value, name: taskSetForm.name, description: taskSetForm.description });
          showTaskSetDialog.value = false;
          taskSetForm.name = '';
          taskSetForm.description = '';
          taskSetForm.scene = '';
          await loadMiningData();
          ElMessage.success(t('taskSetCreated'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function loadNativeLerobotBatchDatasets() {
        const batch = selectedBatch.value;
        if (batch?.batch_type !== 'lerobot' || !batch?.id) {
          nativeLerobotBatchDatasets.value = [];
          return;
        }
        loading.lerobotBatchDatasets = true;
        try {
          const data = await QuicDataAPI.listNativeLerobotDatasets({
            workspace_id: batch.workspace_id,
            task_set_id: batch.task_set_id,
          });
          if (Number(selectedBatch.value?.id) !== Number(batch.id)) return;
          nativeLerobotBatchDatasets.value = (Array.isArray(data?.items) ? data.items : []).filter((row) => (
            row?.type === 'native_lerobot' && Number(row.batch_id) === Number(batch.id)
          ));
        } catch (error) {
          errorMessage(error);
        } finally {
          loading.lerobotBatchDatasets = false;
        }
      }

      function nativeLerobotSessionCandidateSelected(candidateId) {
        return nativeLerobotSessionCandidateIds.value.has(String(candidateId));
      }

      function toggleNativeLerobotSessionCandidate(candidateId, selected) {
        const next = new Set(nativeLerobotSessionCandidateIds.value);
        if (selected) next.add(String(candidateId));
        else next.delete(String(candidateId));
        nativeLerobotSessionCandidateIds.value = next;
      }

      function createUuidRequestId(cryptoProvider = globalThis.crypto) {
        if (typeof cryptoProvider?.randomUUID === 'function') return cryptoProvider.randomUUID();
        if (typeof cryptoProvider?.getRandomValues !== 'function') throw new Error('secure random UUID support is unavailable');
        const bytes = new Uint8Array(16);
        cryptoProvider.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
        return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
      }

      function nativeLerobotSessionRequestId() {
        return createUuidRequestId(globalThis.crypto);
      }

      async function loadTaskLabels() {
        if (!user.value || typeof QuicDataAPI.listTaskLabels !== 'function') {
          taskLabels.value = [];
          return;
        }
        loading.taskLabels = true;
        try {
          const data = await QuicDataAPI.listTaskLabels({ limit: 200, offset: 0 });
          taskLabels.value = data.items || [];
        } catch (error) {
          errorMessage(error);
        } finally {
          loading.taskLabels = false;
        }
      }

      function generateTaskLabelKey(name) {
        const rawName = String(name || '').trim();
        const normalized = typeof rawName.normalize === 'function'
          ? rawName.normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
          : rawName;
        const base = normalized.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 96) || 'task';
        const suffix = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
          ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
          : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
        return `${base}-${suffix}`.slice(0, 128);
      }

      async function createTaskLabel() {
        if (!canManageTaskLabels.value || saving.value || !taskLabelForm.name.trim()) return;
        saving.value = true;
        try {
          const key = generateTaskLabelKey(taskLabelForm.name);
          const label = await QuicDataAPI.createTaskLabel({ key, name: taskLabelForm.name, description: taskLabelForm.description });
          taskLabels.value = [...taskLabels.value.filter((item) => item.id !== label.id), label]
            .sort((left, right) => String(left.name).localeCompare(String(right.name), locale.value));
          taskLabelForm.name = '';
          taskLabelForm.description = '';
          showTaskLabelDialog.value = false;
          ElMessage.success(t('taskLabelCreated'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      let collectionResourcesGeneration = 0;
      async function loadCollectionResources() {
        const generation = ++collectionResourcesGeneration;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const isCurrent = () => (
          generation === collectionResourcesGeneration
          && workspaceId === (Number(selectedWorkspaceId.value) || 0)
        );
        collectorProfiles.value = [];
        collectionDevices.value = [];
        if (!workspaceId) {
          loading.resources = false;
          return;
        }
        loading.resources = true;
        try {
          const [collectors, devices] = await Promise.all([
            QuicDataAPI.listCollectorProfiles(workspaceId, true),
            QuicDataAPI.listCollectionDevices(workspaceId, true),
          ]);
          if (!isCurrent()) return;
          collectorProfiles.value = collectors.items || [];
          collectionDevices.value = devices.items || [];
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        } finally {
          if (isCurrent()) loading.resources = false;
        }
      }

      function collectorDialogScopeMatches(workspaceId) {
        return showCollectorDialog.value
          && Number(collectorDialogWorkspaceId) === Number(workspaceId)
          && Number(selectedWorkspaceId.value) === Number(workspaceId);
      }

      function resetCollectorDialogState({ close = true, invalidate = true } = {}) {
        if (invalidate) {
          availableCollectorsGeneration += 1;
          collectorDialogWriteGeneration += 1;
        }
        if (close) showCollectorDialog.value = false;
        collectorDialogWorkspaceId = 0;
        availableCollectors.value = [];
        selectedExistingCollectorId.value = null;
        loadingAvailableCollectors.value = false;
        collectorDialogMode.value = 'existing';
        collectorForm.name = '';
        collectorForm.profile_key = '';
        saving.value = false;
      }

      function onCollectorDialogClosed() {
        if (showCollectorDialog.value) return;
        resetCollectorDialogState();
      }

      function closeCollectorDialog() {
        resetCollectorDialogState();
      }

      function prepareCollectorDialog(workspaceId) {
        resetCollectorDialogState({ close: false });
        collectorDialogWorkspaceId = Number(workspaceId) || 0;
        showCollectorDialog.value = true;
      }

      function deviceDialogScopeMatches(workspaceId) {
        return showDeviceDialog.value
          && Number(deviceDialogWorkspaceId) === Number(workspaceId)
          && Number(selectedWorkspaceId.value) === Number(workspaceId);
      }

      function resetDeviceDialogState({ close = true, invalidate = true } = {}) {
        if (invalidate) deviceDialogWriteGeneration += 1;
        if (close) showDeviceDialog.value = false;
        deviceDialogWorkspaceId = 0;
        deviceForm.name = '';
        deviceForm.device_type = 'iphone';
        deviceForm.model = '';
        deviceForm.serial_number = '';
        saving.value = false;
      }

      function onDeviceDialogClosed() {
        if (showDeviceDialog.value) return;
        resetDeviceDialogState();
      }

      function closeDeviceDialog() {
        resetDeviceDialogState();
      }

      function openDeviceDialog() {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId || !canManageWorkspace.value) return;
        resetDeviceDialogState({ close: false });
        deviceDialogWorkspaceId = workspaceId;
        showDeviceDialog.value = true;
      }

      async function openCollectorDialog() {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId || !canManageWorkspace.value) return;
        prepareCollectorDialog(workspaceId);
        await loadAvailableCollectors(workspaceId);
        if (!collectorDialogScopeMatches(workspaceId)) return;
        if (!availableCollectors.value.length) {
          collectorDialogMode.value = 'create';
        } else {
          collectorDialogMode.value = 'existing';
        }
      }

      async function loadAvailableCollectors() {
        const requestedWorkspaceId = Number(arguments[0]) || Number(collectorDialogWorkspaceId) || Number(selectedWorkspaceId.value) || 0;
        const generation = ++availableCollectorsGeneration;
        const isCurrent = () => generation === availableCollectorsGeneration
          && collectorDialogScopeMatches(requestedWorkspaceId);
        if (!requestedWorkspaceId) {
          availableCollectors.value = [];
          loadingAvailableCollectors.value = false;
          return;
        }
        availableCollectors.value = [];
        loadingAvailableCollectors.value = true;
        try {
          const res = await QuicDataAPI.listAvailableCollectorProfiles(requestedWorkspaceId);
          if (isCurrent()) availableCollectors.value = res?.items || [];
        } catch (err) {
          if (isCurrent()) errorMessage(err);
        } finally {
          if (generation === availableCollectorsGeneration) loadingAvailableCollectors.value = false;
        }
      }

      async function handleCollectorDialogSubmit() {
        if (!canManageWorkspace.value) return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId || !collectorDialogScopeMatches(workspaceId)) {
          closeCollectorDialog();
          ElMessage.warning(t('scopeChanged'));
          return;
        }
        if (collectorDialogMode.value === 'existing') {
          if (!selectedExistingCollectorId.value) return;
          const collectorId = Number(selectedExistingCollectorId.value);
          const requestGeneration = ++collectorDialogWriteGeneration;
          const isCurrent = () => requestGeneration === collectorDialogWriteGeneration
            && collectorDialogScopeMatches(workspaceId);
          saving.value = true;
          try {
            const profile = await QuicDataAPI.grantCollectorMembership(workspaceId, collectorId);
            if (!isCurrent()) return;
            collectorProfiles.value = [...collectorProfiles.value.filter((item) => Number(item.id) !== Number(profile.id)), profile]
              .sort((left, right) => collectorDisplayLabel(left).localeCompare(collectorDisplayLabel(right), locale.value));
            closeCollectorDialog();
            ElMessage.success(t('collectorCreated'));
          } catch (error) {
            if (isCurrent()) errorMessage(error);
          } finally {
            if (requestGeneration === collectorDialogWriteGeneration) saving.value = false;
          }
        } else {
          await createCollectorProfile();
        }
      }

      async function handleRevokeCollector(profile) {
        if (!canManageWorkspace.value || !profile?.id) return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const requestGeneration = ++resourceWriteGeneration;
        try {
          await ElMessageBox.confirm(
            t('confirmRevokeCollector') || '确定要将该采集员移出当前数采工作空间吗？（不会删除其历史采集数据）',
            t('revokeCollectorMember') || '移出空间',
            { confirmButtonText: t('confirm') || '确定', cancelButtonText: t('cancel') || '取消', type: 'warning' }
          );
        } catch {
          return;
        }
        if (requestGeneration !== resourceWriteGeneration || Number(selectedWorkspaceId.value) !== workspaceId) return;
        saving.value = true;
        try {
          await QuicDataAPI.revokeCollectorMembership(workspaceId, profile.id);
          if (requestGeneration !== resourceWriteGeneration || Number(selectedWorkspaceId.value) !== workspaceId) return;
          collectorProfiles.value = collectorProfiles.value.filter((item) => Number(item.id) !== Number(profile.id));
          ElMessage.success(t('collectorMemberRevoked'));
        } catch (error) {
          if (requestGeneration === resourceWriteGeneration && Number(selectedWorkspaceId.value) === workspaceId) errorMessage(error);
        } finally {
          if (requestGeneration === resourceWriteGeneration) saving.value = false;
        }
      }

      async function createCollectorProfile() {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!canManageWorkspace.value || !collectorProfileFormValid.value || !workspaceId || !collectorDialogScopeMatches(workspaceId)) return;
        const requestGeneration = ++collectorDialogWriteGeneration;
        const isCurrent = () => requestGeneration === collectorDialogWriteGeneration
          && collectorDialogScopeMatches(workspaceId);
        saving.value = true;
        try {
          const profile = await QuicDataAPI.createCollectorProfile({
            workspace_id: workspaceId,
            name: collectorForm.name.trim(),
            profile_key: collectorForm.profile_key.trim() || undefined,
          });
          if (!isCurrent()) return;
          collectorProfiles.value = [...collectorProfiles.value, profile]
            .sort((left, right) => collectorDisplayLabel(left).localeCompare(collectorDisplayLabel(right), locale.value));
          closeCollectorDialog();
          ElMessage.success(t('collectorCreated'));
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        } finally {
          if (requestGeneration === collectorDialogWriteGeneration) saving.value = false;
        }
      }

      async function createCollectionDevice() {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!canManageWorkspace.value || !deviceForm.name.trim() || !deviceForm.device_type.trim() || !deviceForm.serial_number.trim() || !workspaceId || !deviceDialogScopeMatches(workspaceId)) return;
        const requestGeneration = ++deviceDialogWriteGeneration;
        const isCurrent = () => requestGeneration === deviceDialogWriteGeneration
          && deviceDialogScopeMatches(workspaceId);
        saving.value = true;
        try {
          const device = await QuicDataAPI.createCollectionDevice({
            workspace_id: workspaceId,
            name: deviceForm.name.trim(),
            device_type: deviceForm.device_type.trim(),
            model: deviceForm.model.trim(),
            serial_number: deviceForm.serial_number.trim(),
          });
          if (!isCurrent()) return;
          collectionDevices.value = [...collectionDevices.value, device]
            .sort((left, right) => String(left.name).localeCompare(String(right.name), locale.value));
          closeDeviceDialog();
          ElMessage.success(t('deviceCreated'));
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        } finally {
          if (requestGeneration === deviceDialogWriteGeneration) saving.value = false;
        }
      }

      async function setCollectorProfileActive(profile, active) {
        if (!canManageWorkspace.value || !profile?.id || typeof active !== 'boolean' || saving.value) return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const requestGeneration = ++resourceWriteGeneration;
        saving.value = true;
        try {
          await QuicDataAPI.updateCollectorProfile(profile.id, { is_active: active });
          if (requestGeneration !== resourceWriteGeneration || Number(selectedWorkspaceId.value) !== workspaceId) return;
          await loadCollectionResources();
        } catch (error) {
          if (requestGeneration === resourceWriteGeneration && Number(selectedWorkspaceId.value) === workspaceId) errorMessage(error);
        } finally {
          if (requestGeneration === resourceWriteGeneration) saving.value = false;
        }
      }

      async function setCollectionDeviceActive(device, active) {
        if (!canManageWorkspace.value || !device?.id || typeof active !== 'boolean' || saving.value) return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const requestGeneration = ++resourceWriteGeneration;
        saving.value = true;
        try {
          await QuicDataAPI.updateCollectionDevice(device.id, { is_active: active });
          if (requestGeneration !== resourceWriteGeneration || Number(selectedWorkspaceId.value) !== workspaceId) return;
          await loadCollectionResources();
        } catch (error) {
          if (requestGeneration === resourceWriteGeneration && Number(selectedWorkspaceId.value) === workspaceId) errorMessage(error);
        } finally {
          if (requestGeneration === resourceWriteGeneration) saving.value = false;
        }
      }

      function collectorQrCard(kind) {
        const codes = collectorQrCodes.value;
        const definition = codes?.[kind];
        if (!definition || typeof QuicDataQrControlCodes === 'undefined') return null;
        const label = kind === 'control' ? t('qrControlCode') : t('qrSegmentCode');
        const description = kind === 'control' ? t('qrControlDescription') : t('qrSegmentDescription');
        return {
          kind,
          label,
          description,
          collector_id: codes.collector_id,
          payload: definition.payload,
          svg: QuicDataQrControlCodes.svg(definition.payload, { alt: label, title: description }),
        };
      }

      const collectorQrCards = computed(() => ['control', 'segment']
        .map((kind) => collectorQrCard(kind))
        .filter(Boolean));

      async function openCollectorQrDialog(profile) {
        if (!profile) return;
        if (typeof QuicDataQrControlCodes === 'undefined' || !QuicDataQrControlCodes.forCollector(profile)) {
          ElMessage.warning(t('collectorIdMissing'));
          return;
        }
        const workspaceId = selectedWorkspaceId.value;
        const userId = user.value?.id;
        try {
          await QuicStudioPageAssets.load('qr');
          if (activeView.value !== 'resources' || workspaceId !== selectedWorkspaceId.value || userId !== user.value?.id) return;
          selectedCollectorForQr.value = profile;
          showCollectorQrDialog.value = true;
        } catch (error) { errorMessage(error); }
      }

      function downloadCollectorQr(card) {
        if (!card || typeof QuicDataQrControlCodes === 'undefined') return;
        const downloaded = QuicDataQrControlCodes.downloadSvg(
          card.svg,
          `quic-ego-${card.collector_id}-${card.kind}.svg`,
        );
        if (!downloaded) ElMessage.error(t('downloadRawFile'));
      }

      function printCollectorQrCodes() {
        if (typeof QuicDataQrControlCodes === 'undefined') return;
        const printed = QuicDataQrControlCodes.print(collectorQrCards.value, {
          title: t('qrCodes'),
          collector: collectorDisplayLabel(selectedCollectorForQr.value),
        });
        if (!printed) ElMessage.error(t('qrCodes'));
      }

      function resetNativeLerobotBatchState() {
        nativeLerobotSessions.value = [];
        nativeLerobotSession.value = null;
        nativeLerobotSnapshot.value = null;
        nativeLerobotScanCandidates.value = [];
        nativeLerobotSessionCandidateIds.value = new Set();
        nativeLerobotBatchDatasets.value = [];
      }

      async function clearBatchSelection() {
        selectedBatch.value = null;
        miningDetailBatch.value = null;
        resetEpisodePage();
        assetExpandedIds.value = new Set();
        batchImports.value = [];
        batchActivity.value = [];
        removeSubscription('native-lerobot-scan');
        resetNativeLerobotBatchState();
        const unsubscribe = subscriptions.get('selected-batch');
        if (unsubscribe) unsubscribe();
        subscriptions.delete('selected-batch');
        await loadEpisodes();
      }

      function taskLabelName(id) {
        const label = taskLabels.value.find((item) => Number(item.id) === Number(id));
        return label?.name || t('noTaskLabel');
      }

      function candidateImportState(candidate) {
        return candidate?.source_status || candidate?.status || 'discovered';
      }

      function candidateSourceStatusLabel(candidate) {
        const attribution = candidateCollectorAttribution(candidate);
        if (!attribution.importable) {
          return attribution.state === 'format_error' ? t('collectorFormatError') : t('collectorMappingError');
        }
        return {
          discovered: t('sourceDiscovered'), importing: t('sourceImporting'),
          imported: t('sourceImported'), failed: t('sourceFailed'),
          consumed: t('sourceImporting'), rejected: t('sourceFailed'),
        }[candidateImportState(candidate)] || t('sourceDiscovered');
      }

      function candidateSourceStatusType(candidate) {
        if (!candidateCollectorAttribution(candidate).importable) return 'danger';
        const status = candidateImportState(candidate);
        if (status === 'imported') return 'success';
        if (status === 'failed' || status === 'rejected') return 'danger';
        if (status === 'importing' || status === 'consumed') return 'warning';
        return 'info';
      }

      function candidateFailureLabel(candidate) {
        return {
          raw_copy_data_failed: t('sourceDataCopyFailed'),
          raw_copy_metadata_failed: t('sourceMetadataCopyFailed'),
          raw_copy_complete_failed: t('sourceCompleteCopyFailed'),
          source_validation_failed: t('sourceValidationFailed'),
          raw_publish_failed: t('sourcePublishFailed'),
          quality_job_failed: t('sourceQualityJobFailed'),
          import_parse_failed: t('sourceImportFailed'),
        }[candidate?.failure_code] || '';
      }

      function candidateCapturedEndedAt(candidate) {
        return candidate?.captured_ended_at ? formatDate(candidate.captured_ended_at) : t('timeUnknown');
      }

      function candidateCollectorAttribution(candidate) {
        const attribution = candidate?.collector_attribution;
        if (!attribution || typeof attribution !== 'object') {
          return { state: 'unreported', source: 'unknown', profile: null, reported_identifier: '', importable: true, error_code: '' };
        }
        return {
          state: String(attribution.state || 'unreported'),
          source: String(attribution.source || 'unknown'),
          profile: attribution.profile && typeof attribution.profile === 'object' ? attribution.profile : null,
          reported_identifier: typeof attribution.reported_identifier === 'string' ? attribution.reported_identifier : '',
          importable: attribution.importable !== false,
          error_code: typeof attribution.error_code === 'string' ? attribution.error_code : '',
        };
      }

      function candidateCollectorAttributionLabel(candidate) {
        const attribution = candidateCollectorAttribution(candidate);
        if (attribution.state === 'automatic') {
          const profileLabel = collectorDisplayLabel(attribution.profile) || t('unknownCollector');
          return `${profileLabel} · ${t('collectorAutomatic')}`;
        }
        if (attribution.state === 'default') return t('collectorDefault');
        if (attribution.state === 'mapping_error') return t('collectorMappingError');
        if (attribution.state === 'format_error') return t('collectorFormatError');
        return t('collectorUnreported');
      }

      function candidateCollectorAttributionType(candidate) {
        const attribution = candidateCollectorAttribution(candidate);
        if (attribution.state === 'automatic') return 'success';
        if (attribution.state === 'mapping_error' || attribution.state === 'format_error') return 'danger';
        if (attribution.state === 'default') return 'warning';
        return 'info';
      }

      function candidateCollectorAttributionTitle(candidate) {
        const attribution = candidateCollectorAttribution(candidate);
        if (!attribution.reported_identifier) return '';
        return attribution.profile?.is_active === false
          ? `${attribution.reported_identifier} · ${t('inactive')}`
          : attribution.reported_identifier;
      }

      function candidateCanQueue(candidate) {
        if (!candidateCollectorAttribution(candidate).importable) return false;
        if (candidate?.candidate_type === 'ego_episode_oss') {
          return ['discovered', 'failed'].includes(candidate?.source_status);
        }
        return candidate?.status === 'discovered';
      }

      function candidateImportLabel(candidate) {
        return candidateImportState(candidate) === 'failed' ? t('retry') : t('importCandidate');
      }

      function nativeCopyStatusLabel(status) {
        return {
          queued: t('nativeCopyQueued'),
          running: t('nativeCopyRunning'),
          succeeded: t('nativeCopySucceeded'),
          failed: t('nativeCopyFailed'),
          backfill_pending: t('nativeCopyBackfillPending'),
        }[String(status || '')] || t('nativeCopyUnavailable');
      }

      function nativeCopyStatusType(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'failed' || status === 'error') return 'danger';
        if (status === 'queued' || status === 'running') return 'warning';
        return 'info';
      }

      function nativeCopyErrorLabel(dataset) {
        const key = {
          copy_access_denied: 'nativeCopyErrorCopyAccessDenied',
          copy_state_invalid: 'nativeCopyErrorCopyStateInvalid',
          copy_unavailable: 'nativeCopyErrorCopyUnavailable',
          source_changed: 'nativeCopyErrorSourceChanged',
          source_identity_unavailable: 'nativeCopyErrorSourceIdentityUnavailable',
          source_scope_changed: 'nativeCopyErrorSourceScopeChanged',
          target_conflict: 'nativeCopyErrorTargetConflict',
        }[String(dataset?.copy_error_code || '')];
        return key ? t(key) : t('nativeCopyErrorGeneric');
      }

      function nativeCopySummary(dataset) {
        const fileCount = Math.max(0, Number(dataset?.file_count) || 0);
        const size = formatBytes(dataset?.total_size);
        if (dataset?.copy_status === 'succeeded') {
          return t('nativeCopyVerified').replace('{files}', String(fileCount)).replace('{size}', size);
        }
        return `${fileCount} · ${size}`;
      }

      function nativeLerobotCanDeliver(dataset = selectedNativeLerobotDataset.value) {
        return Boolean(
          dataset?.status === 'active'
          && dataset?.copy_status === 'succeeded'
          && dataset?.oss_uri_available,
        );
      }

      function nativeLerobotNeedsReauthorization(dataset = selectedNativeLerobotDataset.value) {
        return Boolean(
          dataset?.status === 'active'
          && (dataset?.copy_status === 'backfill_pending'
            || (dataset?.copy_status === 'failed' && dataset?.copy_error_code === 'source_scope_changed')),
        );
      }

      function nativeLerobotReauthorizationCandidates(dataset = selectedNativeLerobotDataset.value) {
        if (!dataset) return [];
        return lerobotCandidates.value.filter((candidate) => (
          candidate?.robot_type === dataset.robot_type
          && candidate?.dataset_id === dataset.dataset_id
          && Number(candidate?.file_count) === Number(dataset.file_count)
          && Number(candidate?.total_size) === Number(dataset.total_size)
        ));
      }

      function nativeLerobotCandidateLabel(candidate) {
        return `${candidate?.dataset_id || '—'} · ${formatBytes(candidate?.total_size)} · ${formatDateFull(candidate?.completed_at)}`;
      }

      function nativeLerobotBundleActionLabel(bundle = selectedNativeLerobotBundle.value) {
        if (bundle?.download_available) return t('downloadZip');
        if (bundle?.status === 'queued' || bundle?.status === 'running') return t('packagingZip');
        return t('packageZip');
      }

      async function copyText(value) {
        const text = String(value || '');
        if (!text) return false;
        try {
          if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(text);
            return true;
          }
          if (typeof document === 'undefined' || typeof document.execCommand !== 'function') return false;
          const input = document.createElement('textarea');
          input.value = text;
          input.setAttribute('readonly', '');
          input.style.position = 'fixed';
          input.style.opacity = '0';
          document.body.appendChild(input);
          input.select();
          const copied = document.execCommand('copy');
          input.remove();
          return copied;
        } catch {
          return false;
        }
      }



      async function openNativeLerobotDataset(dataset) {
        if (!dataset?.id) return;
        selectedNativeLerobotDataset.value = dataset;
        selectedNativeLerobotBundle.value = null;
        nativeLerobotReauthorizationCandidate.value = '';
        showNativeLerobotDialog.value = true;
        try {
          const detail = await refreshNativeLerobotDataset(dataset.id);
          subscribeNativeLerobotCopyJob(detail);
        } catch (error) {
          errorMessage(error);
        }
      }

      async function copyNativeLerobotOssUri(dataset = selectedNativeLerobotDataset.value) {
        if (!dataset?.id || !nativeLerobotCanDeliver(dataset)) {
          ElMessage.info(t('nativeCopyUnavailable'));
          return;
        }
        try {
          const descriptor = await QuicDataAPI.getNativeLerobotDatasetOssUri(dataset.id);
          if (!descriptor?.oss_uri || !await copyText(descriptor.oss_uri)) throw new Error('copy_failed');
          ElMessage.success(t('copied'));
        } catch (error) {
          errorMessage(error);
        }
      }

      async function retryNativeLerobotCopy(dataset = selectedNativeLerobotDataset.value) {
        if (!canCreateBatch.value || !dataset?.id || nativeLerobotActionLoading.value) return;
        nativeLerobotActionLoading.value = true;
        try {
          const result = await QuicDataAPI.retryNativeLerobotCopy(dataset.id);
          const next = result?.native_dataset;
          if (next) syncNativeLerobotDataset(next);
          subscribeNativeLerobotCopyJob(next || dataset, result?.job);
          await loadNativeLerobotBatchDatasets();
          ElMessage.success(t('nativeCopyQueued'));
        } catch (error) {
          errorMessage(error);
        } finally {
          nativeLerobotActionLoading.value = false;
        }
      }

      async function openNativeLerobotSourceReauthorization(dataset = selectedNativeLerobotDataset.value) {
        if (!canCreateBatch.value || !nativeLerobotNeedsReauthorization(dataset)) return;
        nativeLerobotActionLoading.value = true;
        nativeLerobotReauthorizationCandidate.value = '';
        try {
          showNativeLerobotReauthorizationDialog.value = true;
        } catch (error) {
          errorMessage(error);
        } finally {
          nativeLerobotActionLoading.value = false;
        }
      }

      async function reauthorizeNativeLerobotSource() {
        const dataset = selectedNativeLerobotDataset.value;
        const candidateToken = nativeLerobotReauthorizationCandidate.value;
        if (!canCreateBatch.value || !dataset?.id || !candidateToken || nativeLerobotActionLoading.value) return;
        nativeLerobotActionLoading.value = true;
        try {
          const result = await QuicDataAPI.reauthorizeNativeLerobotSource(dataset.id, {
            candidate_token: candidateToken,
          });
          const next = result?.native_dataset;
          if (next) syncNativeLerobotDataset(next);
          subscribeNativeLerobotCopyJob(next || dataset, result?.job);
          await loadNativeLerobotBatchDatasets();
          showNativeLerobotReauthorizationDialog.value = false;
          ElMessage.success(t('nativeCopyQueued'));
        } catch (error) {
          errorMessage(error);
        } finally {
          nativeLerobotActionLoading.value = false;
        }
      }

      async function requestNativeLerobotBundle(dataset = selectedNativeLerobotDataset.value) {
        if (!canExportDatasets.value || !nativeLerobotCanDeliver(dataset) || nativeLerobotActionLoading.value) return;
        nativeLerobotActionLoading.value = true;
        try {
          const result = await QuicDataAPI.requestNativeLerobotBundle(dataset.id);
          const bundle = result?.bundle;
          if (bundle) syncNativeLerobotBundle(dataset.id, bundle);
          subscribeNativeLerobotBundleJob(dataset, bundle, result?.job);
          ElMessage.success(bundle?.download_available ? t('zipReady') : t('packagingZip'));
        } catch (error) {
          errorMessage(error);
        } finally {
          nativeLerobotActionLoading.value = false;
        }
      }

      async function downloadNativeLerobotBundle(
        dataset = selectedNativeLerobotDataset.value,
        bundle = selectedNativeLerobotBundle.value,
      ) {
        if (!dataset?.id || !bundle?.id || !bundle.download_available) return;
        try {
          const descriptor = await QuicDataAPI.getNativeLerobotBundleDownload(dataset.id, bundle.id);
          if (!descriptor?.available || !descriptor?.url || typeof window === 'undefined') {
            throw new Error(t('previewUnavailable'));
          }
          window.location.assign(descriptor.url);
        } catch (error) {
          errorMessage(error);
        }
      }

      async function archiveNativeLerobotDataset(dataset = selectedNativeLerobotDataset.value) {
        if (!canManageDatasets.value || !nativeLerobotCanDeliver(dataset) || !dataset?.id || nativeLerobotActionLoading.value) return;
        try {
          await ElMessageBox.confirm(t('archive'), t('nativeLerobot'), { type: 'warning' });
        } catch {
          return;
        }
        nativeLerobotActionLoading.value = true;
        try {
          const updated = await QuicDataAPI.updateNativeLerobotDataset(dataset.id, { status: 'archived' });
          syncNativeLerobotDataset(updated);
          showNativeLerobotDialog.value = false;
          selectedNativeLerobotBundle.value = null;
          await loadNativeLerobotBatchDatasets();
          ElMessage.success(t('archived'));
        } catch (error) {
          errorMessage(error);
        } finally {
          nativeLerobotActionLoading.value = false;
        }
      }

      function openIntakeGuide() {
        if (uploading.value) return;
        showIntakeGuide.value = true;
      }

      function openWorkbench(row) {
        const item = row?.work_item;
        const episode = row?.episode;
        if (!item?.id || !episode?.id) {
          ElMessage.warning(t('workbenchUnavailable'));
          return;
        }
        const target = `#/workbench/${encodeURIComponent(String(item.id))}?episode_id=${encodeURIComponent(String(episode.id))}`;
        activeView.value = 'workbench';
        workbenchRoute.workItemId = String(item.id);
        workbenchRoute.episodeId = String(episode.id);
        if (typeof window !== 'undefined' && window.location.hash !== target) window.location.hash = target;
        else void loadWorkbench();
      }

      async function enterWorkbench(row) {
        if (!canEnterWorkbench(row)) {
          ElMessage.warning(t('workbenchUnavailable'));
          return;
        }
        const item = row.work_item;
        saving.value = true;
        try {
          if (item.available_actions?.includes('continue')) {
            await QuicDataAPI.workQueueAction(item.id, 'continue');
          }
          await loadWorkQueue();
          openWorkbench(row);
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      function workbenchDraftPayload() {
        const item = activeWorkbench.value?.work_item;
        if (!['cut', 'annotation'].includes(item?.kind)) return null;
        const segments = workbenchDraft.value.segments.map((segment) => {
          const result = {
            id: String(segment.id || nextSegmentId()),
            start_ns: String(segment.start_ns || ''),
            end_ns: String(segment.end_ns || ''),
            description: String(segment.description || ''),
          };
          if (Array.isArray(segment.behavior_tag_ids)) result.behavior_tag_ids = [...segment.behavior_tag_ids];
          if (item.kind === 'cut') {
            result.eligibility = segment.eligibility === 'excluded' ? 'excluded' : 'included';
            if (result.eligibility === 'excluded' && segment.exclusion_reason) {
              result.exclusion_reason = segment.exclusion_reason;
            }
            const boundary = normalizedCutBoundary(segment.boundary_after);
            if (boundary) result.boundary_after = boundary;
          }
          return result;
        });
        const payload = { segments };
        if (item.kind === 'cut') {
          payload.mode = workbenchDraft.value.mode === 'whole' ? 'whole' : 'partitioned';
        } else if (item.kind === 'annotation') {
          payload.outcome = ['success', 'failure', 'unknown'].includes(workbenchDraft.value.outcome)
            ? workbenchDraft.value.outcome
            : 'unknown';
          if (Number.isInteger(workbenchDraft.value.rating) && workbenchDraft.value.rating >= 1 && workbenchDraft.value.rating <= 5) {
            payload.rating = workbenchDraft.value.rating;
          }
        }
        payload.note = String(workbenchDraft.value.note || '').trim();
        return payload;
      }

      function captureWorkbenchDraft() {
        if (draggingCutBoundary || draggingAnnotationBoundary || draggingAnnotationSegment) return null;
        const snapshot = activeWorkbench.value;
        const item = snapshot?.work_item;
        const payload = workbenchDraftPayload();
        const allowed = demoMode.value || item?.available_actions?.includes('save_draft');
        if (!item?.id || !payload || !allowed) return null;
        if (item.kind === 'cut' && !cutDraftSaveValid()) return null;
        if (item.kind === 'annotation' && !annotationDraftSaveValid()) return null;
        return {
          workItemId: Number(item.id),
          baseVersion: Number(snapshot.draft?.version || 0),
          payload,
        };
      }

      function workbenchItemRealtimeVersion(result) {
        const realtimeVersion = Number(result?.work_item_realtime_version);
        return Number.isInteger(realtimeVersion) && realtimeVersion >= 0 ? realtimeVersion : null;
      }

      function acknowledgeWorkbenchItemEvent(workItemId, realtimeVersion) {
        if (
          realtimeVersion === null
          || typeof QuicDataRealtime === 'undefined'
          || typeof QuicDataRealtime.acknowledge !== 'function'
        ) return;
        QuicDataRealtime.acknowledge({
          resourceType: 'work_item',
          resourceId: String(workItemId),
          realtimeVersion,
        });
      }

      function deferWorkbenchItemEvents(workItemId) {
        if (
          typeof QuicDataRealtime === 'undefined'
          || typeof QuicDataRealtime.deferEvents !== 'function'
        ) return () => {};
        return QuicDataRealtime.deferEvents({
          resourceType: 'work_item',
          resourceId: String(workItemId),
        });
      }

      function applyWorkbenchSaveResult(request, result) {
        const current = activeWorkbench.value;
        if (!current || Number(current.work_item?.id) !== Number(request?.workItemId)) return;
        const savedItem = result?.work_item?.work_item;
        const realtimeVersion = workbenchItemRealtimeVersion(result);
        const versionPatch = realtimeVersion !== null
          ? { realtime_version: realtimeVersion }
          : {};
        const savedItemPatch = workbenchInvalidationNotified ? {} : (savedItem || {});
        activeWorkbench.value = {
          ...current,
          draft: { ...current.draft, ...(result?.draft || {}) },
          work_item: { ...current.work_item, ...savedItemPatch, ...versionPatch },
        };
        acknowledgeWorkbenchItemEvent(request.workItemId, realtimeVersion);
      }

      async function persistWorkbenchDraft(request) {
        const rawResult = await QuicDataAPI.saveWorkbenchDraft(request.workItemId, {
          base_version: request.baseVersion,
          payload: request.payload,
        });
        const result = demoMode.value && rawResult?.draft === undefined && rawResult?.version !== undefined
          ? { draft: rawResult }
          : (rawResult || {});
        applyWorkbenchSaveResult(request, result);
        return result;
      }

      async function saveWorkbenchDraft({ quiet = false } = {}) {
        const snapshot = activeWorkbench.value;
        const item = snapshot?.work_item;
        const payload = workbenchDraftPayload();
        if (!item?.id || !payload || (!demoMode.value && !item.available_actions?.includes('save_draft'))) {
          if (!quiet) ElMessage.warning(t('workbenchUnavailable'));
          return false;
        }
        if (item.kind === 'cut' && !cutDraftSaveValid()) {
          if (!quiet) ElMessage.warning(t('partitionInvalid'));
          return false;
        }
        if (item.kind === 'annotation' && !annotationDraftSaveValid()) {
          if (!quiet) ElMessage.warning(t('annotationIncomplete'));
          return false;
        }
        if (!workbenchSaveCoordinator || Number(workbenchSaveItemId) !== Number(item.id)) {
          configureWorkbenchSaveCoordinator(snapshot);
        }
        if (!workbenchSaveCoordinator) return false;
        if (!workbenchSaveCoordinator.state().dirty) workbenchSaveCoordinator.markDirty();
        const saved = await workbenchSaveCoordinator.flush({ drain: true, reason: 'manual' });
        if (saved && !quiet) ElMessage.success(t('draftSaved'));
        return saved;
      }

      async function submitWorkbenchDraft() {
        const snapshot = activeWorkbench.value;
        const item = snapshot?.work_item;
        if (!item?.id) return;
        if (item.kind === 'cut' && !cutDraftSubmitValid()) {
          ElMessage.warning(cutDraftSaveValid() ? t('submitCutTooLong') : t('partitionInvalid'));
          return;
        }
        if (item.kind === 'annotation' && !annotationDraftSubmitValid()) {
          ElMessage.warning(t('annotationIncomplete'));
          return;
        }
        const saved = await saveWorkbenchDraft({ quiet: true });
        if (!saved) return;
        saving.value = true;
        const resumeRealtimeEvents = deferWorkbenchItemEvents(item.id);
        try {
          const result = await QuicDataAPI.workQueueAction(item.id, 'submit', {
            base_version: Number(activeWorkbench.value?.draft?.version || 0),
          });
          acknowledgeWorkbenchItemEvent(item.id, workbenchItemRealtimeVersion(result));
          applyWorkQueueMutation(result.work_item);
          resumeRealtimeEvents();
          ElMessage.success(t('actionSucceeded'));
          await navigate('work-queue', { refreshQueue: false });
          void scheduleWorkQueueRefresh();
        } catch (error) {
          resumeRealtimeEvents();
          errorMessage(error);
        } finally {
          resumeRealtimeEvents();
          saving.value = false;
        }
      }

      async function submitReviewDecision(decision) {
        const snapshot = activeWorkbench.value;
        const item = snapshot?.work_item;
        if (!item?.id || !snapshot.capabilities?.review || !item.available_actions?.includes('review')) return;
        const body = { decision, note: String(reviewForm.note || '').trim() };
        if (Number.isInteger(reviewForm.rating) && reviewForm.rating >= 1 && reviewForm.rating <= 5) body.rating = reviewForm.rating;
        saving.value = true;
        const resumeRealtimeEvents = deferWorkbenchItemEvents(item.id);
        try {
          const result = await QuicDataAPI.workQueueAction(item.id, 'review', body);
          acknowledgeWorkbenchItemEvent(item.id, workbenchItemRealtimeVersion(result));
          applyWorkQueueMutation(result.work_item);
          resumeRealtimeEvents();
          ElMessage.success(t('actionSucceeded'));
          await navigate('work-queue', { refreshQueue: false });
          void scheduleWorkQueueRefresh();
        } catch (error) {
          resumeRealtimeEvents();
          errorMessage(error);
        } finally {
          resumeRealtimeEvents();
          saving.value = false;
        }
      }

      async function releaseWorkbench() {
        const item = activeWorkbench.value?.work_item;
        if (!item?.id || !item.available_actions?.includes('release')) return;
        let note = '';
        if (typeof window !== 'undefined' && typeof window.prompt === 'function') {
          const answer = window.prompt(t('releasePrompt'), '');
          if (answer === null) return;
          note = answer;
        }
        if (workbenchDraftDirty.value && item.available_actions?.includes('save_draft')) {
          if (!await saveWorkbenchDraft({ quiet: true })) return;
        }
        saving.value = true;
        const resumeRealtimeEvents = deferWorkbenchItemEvents(item.id);
        try {
          const result = await QuicDataAPI.workQueueAction(item.id, 'release', { note });
          acknowledgeWorkbenchItemEvent(item.id, workbenchItemRealtimeVersion(result));
          applyWorkQueueMutation(result.work_item);
          resumeRealtimeEvents();
          await navigate('work-queue', { refreshQueue: false });
          void scheduleWorkQueueRefresh();
          ElMessage.success(t('actionSucceeded'));
        } catch (error) {
          resumeRealtimeEvents();
          errorMessage(error);
        } finally {
          resumeRealtimeEvents();
          saving.value = false;
        }
      }

      async function openEpisodeDetail(row, options = {}) {
        const context = QuicDataEpisodeDetail.normalizeContext(row, options);
        if (!context.episodeId) return;
        const requestToken = episodeDetailRequestGate.begin(context.episodeId);
        episodeDrawerContext.value = context;
        episodeDrawerTab.value = 'overview';
        episodeTechnicalSections.value = [];
        selectedEpisode.value = context.episode;
        episodePreview.value = null;
        rawSourceDownloads.value = null;
        episodeDrawerError.detail = '';
        episodeDrawerError.preview = '';
        episodeDrawerLoading.detail = true;
        episodeDrawerLoading.preview = true;
        episodeDrawerLoading.rawSource = false;
        showEpisodeDrawer.value = true;

        const detailRequest = QuicDataAPI.getEpisode(context.episodeId)
          .then((episode) => {
            if (!episodeDetailRequestGate.isCurrent(requestToken, episode?.id)) return;
            selectedEpisode.value = QuicDataEpisodeDetail.mergeEpisodeDetail(context, episode);
            subscribeEpisode(selectedEpisode.value, requestToken);
          })
          .catch((error) => {
            if (!episodeDetailRequestGate.isCurrent(requestToken, context.episodeId)) return;
            episodeDrawerError.detail = error instanceof Error ? error.message : String(error || 'episode_detail_load_failed');
            errorMessage(error);
          })
          .finally(() => {
            if (episodeDetailRequestGate.isCurrent(requestToken, context.episodeId)) {
              episodeDrawerLoading.detail = false;
            }
          });

        const previewRequest = QuicDataAPI.getEpisodePreviewUrl(context.episodeId)
          .then((preview) => {
            if (!episodeDetailRequestGate.isCurrent(requestToken, context.episodeId)) return;
            episodePreview.value = preview;
          })
          .catch((error) => {
            if (!episodeDetailRequestGate.isCurrent(requestToken, context.episodeId)) return;
            episodeDrawerError.preview = error instanceof Error ? error.message : String(error || 'episode_preview_load_failed');
            errorMessage(error);
          })
          .finally(() => {
            if (episodeDetailRequestGate.isCurrent(requestToken, context.episodeId)) {
              episodeDrawerLoading.preview = false;
            }
          });

        await Promise.allSettled([detailRequest, previewRequest]);
      }

      async function loadRawSourceDownloads() {
        const episodeId = selectedEpisode.value?.id;
        if (!episodeId || episodeDrawerLoading.rawSource) return;
        episodeDrawerLoading.rawSource = true;
        try {
          rawSourceDownloads.value = await QuicDataAPI.getEpisodeRawSourceDownloads(episodeId);
        } catch (error) {
          rawSourceDownloads.value = { available: false, files: [] };
          errorMessage(error);
        } finally {
          episodeDrawerLoading.rawSource = false;
        }
      }

      function downloadRawSourceFile(file) {
        if (!file?.available || !file.url || typeof window === 'undefined') return;
        const opened = window.open(file.url, '_blank', 'noopener,noreferrer');
        if (!opened) window.location.assign(file.url);
      }

      function closeEpisodeDetail() {
        episodeDetailRequestGate.invalidate();
        removeSubscription('selected-episode');
        showEpisodeDrawer.value = false;
        selectedEpisode.value = null;
        episodePreview.value = null;
        rawSourceDownloads.value = null;
        episodeDrawerContext.value = null;
        episodeTechnicalSections.value = [];
        episodeDrawerLoading.detail = false;
        episodeDrawerLoading.preview = false;
        episodeDrawerLoading.rawSource = false;
        episodeDrawerError.detail = '';
        episodeDrawerError.preview = '';
      }

      function retryEpisodeDetail() {
        const context = episodeDrawerContext.value;
        if (!context?.episodeId) return;
        void openEpisodeDetail({ episode: context.episode, work_item: context.workItem }, {
          source: context.source,
          queueStage: context.queueStage,
          previewStatus: context.previewStatus,
        });
      }

      function openWorkQueueEpisodeDetail(row) {
        void openEpisodeDetail(row, {
          source: 'work-queue',
          queueStage: queueStage.value,
          previewStatus: queuePreviewStatus(row),
        });
      }

      function inspectBatchSource(row) {
        if (row?.episode_id) void openEpisodeDetail({ id: row.episode_id, episode_uid: row.episode_uid }, { source: 'batch' });
      }

      async function runWorkAction(row, action, decision = '') {
        if (action === 'continue' && canEnterWorkbench(row)) {
          await enterWorkbench(row);
          return;
        }
        const item = row?.work_item;
        if (!item?.id) return;
        let body;
        if (action === 'review') body = { decision };
        if (action === 'release') {
          let note = '';
          const forceRelease = Number(item.assignee_user_id) > 0
            && Number(item.assignee_user_id) !== Number(user.value?.id);
          if (typeof window !== 'undefined' && typeof window.prompt === 'function') {
            const answer = window.prompt(t(forceRelease ? 'forceReleasePrompt' : 'releasePrompt'), '');
            if (answer === null) return;
            note = answer.trim();
          }
          if (forceRelease && !note) {
            ElMessage.error(t('forceReleaseReasonRequired'));
            return;
          }
          body = { note };
        }
        saving.value = true;
        try {
          const result = await QuicDataAPI.workQueueAction(item.id, action, body);
          applyWorkQueueMutation(result.work_item);
          void scheduleWorkQueueRefresh();
          ElMessage.success(t('actionSucceeded'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function runWorkActionById(workItemId, action, decision = '') {
        const itemId = Number(workItemId);
        const row = queueRows.value.find((candidate) => Number(candidate?.work_item?.id) === itemId);
        if (!Number.isInteger(itemId) || itemId <= 0 || !row) {
          ElMessage.error(t('workItemUpdated'));
          return;
        }
        await runWorkAction(row, action, decision);
      }

      async function enterWorkbenchById(workItemId) {
        const itemId = Number(workItemId);
        const row = queueRows.value.find((candidate) => Number(candidate?.work_item?.id) === itemId);
        if (!Number.isInteger(itemId) || itemId <= 0 || !row) {
          ElMessage.error(t('workItemUpdated'));
          return;
        }
        await enterWorkbench(row);
      }

      async function signIn() {
        saving.value = true;
        try {
          const result = await QuicDataAPI.login(login.email, login.password);
          setQueueStageValue('cut');
          queueStageInitialized = false;
          user.value = result.userInfo || QuicDataAPI.currentUser();
          sessionRestoring.value = false;
          login.password = '';
          if (mustChangePassword.value) {
            openChangePassword();
            setLocale(locale.value);
            ready.value = true;
          } else {
            await initialize();
          }
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function enterLocalDemo() {
        if (!QuicDataAPI.enableDemoMode?.()) return;
        demoMode.value = true;
        user.value = QuicDataAPI.currentUser();
        sessionRestoring.value = false;
        activeView.value = 'workbench';
        workbenchRoute.workItemId = '9001';
        workbenchRoute.episodeId = '4200';
        initialRouteResolved = true;
        if (typeof window !== 'undefined') {
          const target = new URL(window.location.href);
          target.searchParams.set('demo', '1');
          target.hash = '#/workbench/9001?episode_id=4200';
          window.history.replaceState(null, '', target);
        }
        await initialize();
        ElMessage.success(t('demoEntered'));
      }

      async function submitPasswordChange() {
        if (!user.value) return;
        if (passwordForm.newPassword.length < 10) {
          ElMessage.error(t('passwordTooShort'));
          return;
        }
        if (passwordForm.newPassword !== passwordForm.confirmPassword) {
          ElMessage.error(t('passwordMismatch'));
          return;
        }
        saving.value = true;
        try {
          await QuicDataAPI.changePassword({
            old_password: passwordForm.oldPassword,
            new_password: passwordForm.newPassword,
          });
          const email = user.value.email;
          closeEpisodeDetail();
          clearSubscriptions();
          if (typeof QuicDataRealtime !== 'undefined') QuicDataRealtime.disconnect();
          QuicDataAPI.clearAuth();
          user.value = null;
          setQueueStageValue('cut');
          queueStageInitialized = false;
          showChangePassword.value = false;
          clearPasswordForm();
          login.email = email;
          workspaces.value = [];
          taskSets.value = [];
          batches.value = [];
          episodes.value = [];
          episodeTotal.value = 0;
          resetEpisodePage();
          managedUsers.value = [];
          roleDefinitions.value = {};
          workspaceMembers.value = [];
          managementWorkspaces.value = [];
          managementMembers.value = [];
          managementWorkspaceId.value = null;
          episodePreview.value = null;
          queueRows.value = [];
          batchImports.value = [];
          batchActivity.value = [];
          selectedWorkspaceId.value = null;
          selectedTaskSetId.value = null;
          selectedBatch.value = null;
          ElMessage.success(t('passwordChanged'));
        } catch (error) {
          errorMessage(error);
        } finally {
          saving.value = false;
        }
      }

      async function signOut() {
        if (activeView.value === 'package-workbench' && packageAnnotationWorkbenchRef.value && !await packageAnnotationWorkbenchRef.value.canLeave()) return;
        if (activeView.value === 'intake-review' && intakeReviewWorkbenchRef.value && !await intakeReviewWorkbenchRef.value.canLeave()) return;
        const leavingDemo = demoMode.value;
        resetCollectionProjectDialogState();
        closeCollectorDialog();
        closeDeviceDialog();
        resetMiningTaskDialogState();
        resetMiningSplitDialogState();
        resetMiningAssignDialogState();
        exitOverviewBuildMode();
        dashboardOverviewGeneration += 1;
        dashboardOverview.value = null;
        dashboardOverviewError.value = '';
        miningTaskSelectionGeneration += 1;
        resourceWriteGeneration += 1;
        collectionResourcesGeneration += 1;
        try {
          await QuicDataAPI.logout();
        } catch (error) {
          errorMessage(error);
          return;
        }
        demoMode.value = false;
        closeEpisodeDetail();
        clearSubscriptions();
        if (typeof QuicDataRealtime !== 'undefined') QuicDataRealtime.disconnect();
        user.value = null;
        setQueueStageValue('cut');
        queueStageInitialized = false;
        workspaces.value = [];
        taskSets.value = [];
        batches.value = [];
        episodes.value = [];
        episodeTotal.value = 0;
        resetEpisodePage();
        managedUsers.value = [];
        roleDefinitions.value = {};
        workspaceMembers.value = [];
        managementWorkspaces.value = [];
        managementMembers.value = [];
        managementWorkspaceId.value = null;
        platformSettings.value = { ai: null, oss_import_scopes: [] };
        episodePreview.value = null;
        disposeWorkbenchSaveCoordinator();
        activeWorkbench.value = null;
        workbenchTimeline.value = null;
        workbenchDraft.value = { mode: 'partitioned', segments: [], note: '' };
        workbenchDraftDirty.value = false;
        workbenchHistory.past = [];
        workbenchHistory.future = [];
        workbenchHistory.current = null;
        workbenchHistory.ready = false;
        cutPlayheadTimestamp.value = null;
        selectedCutBoundary.value = null;
        if (leavingDemo && typeof window !== 'undefined') {
          const target = new URL(window.location.href);
          target.searchParams.delete('demo');
          target.hash = '';
          window.history.replaceState(null, '', target);
        }
        cutLocalRangeAnchor.value = null;
        collectorProfiles.value = [];
        collectionDevices.value = [];
        aiSuggestions.value = { capability: { eligible: false, rgb_topics: [], default_rgb_topic: '' }, items: [] };
        selectedAiTopic.value = '';
        queueRows.value = [];
        batchImports.value = [];
        batchActivity.value = [];
        selectedWorkspaceId.value = null;
        selectedTaskSetId.value = null;
        selectedBatch.value = null;
        showChangePassword.value = false;
        clearPasswordForm();
      }

      async function initialize() {
        ready.value = false;
        const initialView = resolveInitialAuthorizedView();
        if (!initialView) {
          setLocale(locale.value);
          ready.value = true;
          return;
        }
        if (hasPermission('workspace:read')) {
          const savedScope = savedScopePreference();
          await loadWorkspaces({ preferredWorkspaceId: savedScope?.workspace_id });
          await loadTaskSets({
            preferredTaskSetId: Number(selectedWorkspaceId.value) === Number(savedScope?.workspace_id)
              ? savedScope?.task_set_id : null,
          });
          persistScopePreference();
        }
        if (initialView === 'package-workbench') applyPackageWorkbenchRoute(initialRoute);
        await loadAuthorizedConsoleData();
        // Deep links (and page refreshes) land directly on a view, so the
        // per-view loaders the activeView watcher normally runs never fire.
        if (initialView === 'intake-review') {
          const id = Number(initialRoute.query.package_id || intakeReviewPackageId.value || 0);
          if (id > 0) {
            intakeReviewPackageId.value = id;
          }
        }
        if (initialView === 'admin') await loadAdmin();
        if (initialView === 'settings') await loadPlatformSettings();
        ensureRealtimeConnection();
        subscribeWorkspaceQueue();
        if (initialView === 'workbench') await loadWorkbench();
        setLocale(locale.value);
        ready.value = true;
      }

      const productVersion = ref('');
      async function loadProductVersion() {
        try {
          productVersion.value = (await QuicDataAPI.getProductVersion()) || '';
        } catch (error) {
          productVersion.value = '';
        }
      }

      onMounted(async () => {
        void loadProductVersion();
        if (typeof window !== 'undefined') window.addEventListener('hashchange', syncRoute);
        if (typeof window !== 'undefined') window.addEventListener('resize', resizeDashboardCharts);
        if (typeof document !== 'undefined') document.addEventListener('keydown', handleWorkbenchShortcut);
        const restored = await QuicDataAPI.restoreSession();
        if (user.value) {
          sessionRestoring.value = false;
          return;
        }
        user.value = restored ? QuicDataAPI.currentUser() : null;
        sessionRestoring.value = false;
        if (user.value && mustChangePassword.value) {
          openChangePassword();
          setLocale(locale.value);
          ready.value = true;
        } else if (user.value) await initialize();
        else {
          setLocale(locale.value);
          ready.value = true;
        }
      });

      onBeforeUnmount(() => {
        clearReviewPackageReturnHighlightTimer();
        stopPlayheadDrag();
        stopWorkbenchSplitResize();
        stopCutBoundaryDrag();
        stopAnnotationBoundaryDrag();
        stopAnnotationSegmentDrag();
        cancelScheduledWorkbenchHistory();
        disposeWorkbenchSaveCoordinator();
        releaseDatasetRevisionPreview();
        disposeDashboardCharts();
        if (typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
          if (cutListScrollRaf) window.cancelAnimationFrame(cutListScrollRaf);
          if (annotationListScrollRaf) window.cancelAnimationFrame(annotationListScrollRaf);
          if (timelinePaintRaf) window.cancelAnimationFrame(timelinePaintRaf);
          if (hoverSeekRaf) window.cancelAnimationFrame(hoverSeekRaf);
        }
        if (typeof window !== 'undefined') window.removeEventListener('hashchange', syncRoute);
        if (typeof window !== 'undefined') window.removeEventListener('resize', resizeDashboardCharts);
        if (typeof document !== 'undefined') document.removeEventListener('keydown', handleWorkbenchShortcut);
      });

      function miningScope() {
        return { workspace_id: selectedWorkspaceId.value || null, task_set_id: selectedTaskSetId.value || null };
      }

      let miningDataGeneration = 0;
      async function loadMiningData() {
        const generation = ++miningDataGeneration;
        const workspaceId = selectedWorkspaceId.value;
        const isCurrent = () => generation === miningDataGeneration && String(selectedWorkspaceId.value) === String(workspaceId);
        if (demoMode.value) {
          const scoped = QuicDataMining.listTasks(miningScope());
          miningTasks.value = scoped.length ? scoped : QuicDataMining.listTasks();
          if (!miningTasks.value.some((task) => String(task.id) === String(miningSelectedTaskId.value))) {
            miningSelectedTaskId.value = miningTasks.value.length ? String(miningTasks.value[0].id) : '';
          }
          miningPipelines.value = QuicDataMining.listPipelines({ task_id: miningSelectedTaskId.value || null });
          miningTransfers.value = QuicDataMining.listTransfers();
          miningDictionaries.value = QuicDataMining.listDictionaries();
          Object.assign(collectionConfigForm, QuicDataMining.getCollectionConfig());
          return;
        }

        miningTasks.value = [];
        miningDictionaries.value = [];
        collectionLabels.value = [];
        collectionProjects.value = [];
        miningPipelines.value = [];
        miningTransfers.value = [];
        if (!workspaceId) return;
        try {
          const [projectsRes, labelsRes] = await Promise.all([
            QuicDataAPI.listCollectionProjects(workspaceId),
            QuicDataAPI.listCollectionLabels(workspaceId),
          ]);
          if (!isCurrent()) return;
          collectionProjects.value = Array.isArray(projectsRes?.items) ? projectsRes.items : (Array.isArray(projectsRes) ? projectsRes : []);
          if (!collectionProjectOptions.value.some((item) => Number(item.id) === Number(selectedCollectionProjectId.value))) {
            selectedCollectionProjectId.value = null;
          }
          const labelList = Array.isArray(labelsRes?.items) ? labelsRes.items : (Array.isArray(labelsRes) ? labelsRes : []);
          collectionLabels.value = labelList;

          const dictMap = {
            project: { label: '项目标签', items: collectionProjects.value.map((p) => p.name).filter(Boolean) },
            purpose: { label: '任务用途', items: [] },
            scene: { label: '场景标签', items: [] },
            train: { label: '训练用途', items: [] },
            software: { label: '软件版本', items: [] },
          };
          for (const l of labelList) {
            const key = l.category === 'training' ? 'train' : l.category;
            if (dictMap[key]) {
              if (!dictMap[key].items.includes(l.name)) dictMap[key].items.push(l.name);
            }
          }

          miningDictionaries.value = Object.entries(dictMap).map(([kind, cfg]) => ({
            kind,
            label: cfg.label,
            items: cfg.items,
          }));

          const tasksRes = await QuicDataAPI.listCollectionTasks(workspaceId);
          if (!isCurrent()) return;
          const rawTasks = Array.isArray(tasksRes?.items) ? tasksRes.items : (Array.isArray(tasksRes) ? tasksRes : []);

          miningTasks.value = rawTasks.map((task) => {
            const project = collectionProjects.value.find((p) => p.id === task.collection_project_id);
            const taskLabels = collectionLabels.value.filter((l) => (task.label_ids || []).includes(l.id));
            const tags = {
              project: project?.name || '—',
              purpose: taskLabels.find((l) => l.category === 'purpose')?.name || '—',
              scene: taskLabels.find((l) => l.category === 'scene')?.name || '—',
              train: taskLabels.find((l) => l.category === 'training')?.name || '—',
            };
            const targetHours = Number(task.target_duration_hours) || 0;
            const validHours = Number(task.intake_valid_duration_hours) || 0;
            const packages = Array.isArray(task.packages) ? task.packages : [];
            const batches = packages.map((pkg, idx) => {
              const collectorId = pkg.responsible_collector_id || pkg.collector_id || pkg.operator_collector_id;
              const isPlanned = pkg.status === 'pending_assignment' || pkg.status === 'staged';
              const isAssigned = pkg.status === 'assigned';
              const isDone = pkg.status === 'completed' || pkg.status === 'reviewed' || pkg.status === 'intake_approved' || pkg.status === 'batched';
              const packageTargetHours = Number(pkg.target_duration_hours);
              return {
                ...pkg,
                id: pkg.id,
                seq: idx + 1,
                name: pkg.package_uid,
                batch_type: task.modality || 'ego',
                target: Number.isFinite(packageTargetHours) && packageTargetHours > 0 ? packageTargetHours : null,
                raw: Number(pkg.captured_duration_hours ?? pkg.intake_valid_duration_hours) || 0,
                checked: Number(pkg.intake_valid_duration_hours) || 0,
                valid: Number(pkg.intake_valid_duration_hours) || 0,
                target_duration_hours: pkg.target_duration_hours,
                captured_duration_hours: pkg.captured_duration_hours,
                valid_duration_hours: pkg.intake_valid_duration_hours || 0,
                intake_valid_duration_hours: pkg.intake_valid_duration_hours || 0,
                raw_status: pkg.status,
                status: isPlanned ? 'planned' : (isAssigned ? 'collecting' : (isDone ? 'done' : 'processing')),
                assignees: collectorId ? [{ collector_id: collectorId, device_id: pkg.collection_device_id }] : [],
                package_uid: pkg.package_uid,
              };
            });

            return {
              ...task,
              id: task.id,
              name: task.name,
              modality: task.modality || 'ego',
              sop: task.sop_text || task.description || '',
              target_duration_hours: targetHours,
              valid_duration_hours: validHours,
              intake_valid_duration_hours: validHours,
              tags,
              owner: task.created_by_user_email
                || (user.value && Number(task.created_by_user_id) === Number(user.value.id) ? (user.value.email || user.value.name || '当前账号') : '')
                || (task.created_by_user_id ? `用户 #${task.created_by_user_id}` : '管理员'),
              batches,
              package_count: task.package_count || batches.length,
              assigned_count: task.assigned_count || 0,
              pending_assignment_count: task.pending_assignment_count,
            };
          });

          if (!miningTasks.value.some((task) => String(task.id) === String(miningSelectedTaskId.value))) {
            miningSelectedTaskId.value = miningTasks.value.length ? String(miningTasks.value[0].id) : '';
          }

          if (miningSelectedTaskId.value) {
            void selectMiningTask(miningSelectedTaskId.value, { preserveScroll: true });
          }
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        }
      }

      async function loadMiningConfigData() {
        if (demoMode.value) {
          miningDictionaries.value = QuicDataMining.listDictionaries();
          Object.assign(collectionConfigForm, QuicDataMining.getCollectionConfig());
          return;
        }
        await loadMiningData();
      }

      function dictItems(kind) {
        if (!miningDictionaries.value.length) {
          try { miningDictionaries.value = demoMode.value ? QuicDataMining.listDictionaries() : []; } catch (error) { miningDictionaries.value = []; }
        }
        return miningDictionaries.value.find((entry) => entry.kind === kind)?.items || [];
      }

      async function addMiningTag(kind) {
        const text = String(miningConfigTagInputs[kind] || '').trim();
        if (!text) return;
        if (demoMode.value) {
          const ok = QuicDataMining.addDictItem(kind, text);
          if (!ok) {
            ElMessage.warning(t('configTagExists'));
            return;
          }
          miningConfigTagInputs[kind] = '';
          miningDictionaries.value = QuicDataMining.listDictionaries();
          return;
        }

        if (!selectedWorkspaceId.value) {
          ElMessage.warning(t('noWorkspace'));
          return;
        }

        try {
          if (kind === 'project') {
            await QuicDataAPI.createCollectionProject({
              workspace_id: Number(selectedWorkspaceId.value),
              name: text,
              description: '',
            });
          } else {
            const category = kind === 'train' ? 'training' : kind;
            await QuicDataAPI.createCollectionLabel({
              workspace_id: Number(selectedWorkspaceId.value),
              category,
              name: text,
              description: '',
            });
          }
          miningConfigTagInputs[kind] = '';
          await loadMiningData();
          ElMessage.success(t('configSaved'));
        } catch (err) {
          errorMessage(err);
        }
      }

      async function removeMiningTag(kind, value) {
        if (demoMode.value) {
          QuicDataMining.removeDictItem(kind, value);
          miningDictionaries.value = QuicDataMining.listDictionaries();
          return;
        }

        if (!selectedWorkspaceId.value) return;
        try {
          if (kind === 'project') {
            const proj = collectionProjects.value.find((p) => p.name === value);
            if (proj) {
              await QuicDataAPI.archiveCollectionProject(proj.id, selectedWorkspaceId.value);
            }
          } else {
            const lbl = collectionLabels.value.find((l) => l.name === value);
            if (lbl) {
              await QuicDataAPI.deactivateCollectionLabel(lbl.id, selectedWorkspaceId.value);
            }
          }
          await loadMiningData();
        } catch (err) {
          errorMessage(err);
        }
      }

      function saveMiningConfig() {
        QuicDataMining.updateCollectionConfig({ ...collectionConfigForm });
        ElMessage.success(t('configSaved'));
      }

      function dashPercent(count, rows) {
        const max = rows.reduce((maxValue, row) => Math.max(maxValue, Number(row.count) || 0), 0);
        return max > 0 ? Math.round((Number(count) || 0) / max * 100) : 0;
      }

      function tagOf(row, key) {
        return row?.tags?.[key] || '—';
      }

      const miningTaskRows = computed(() => miningTasks.value.map((task) => ({ ...task, progress: QuicDataMining.taskProgress(task) })));
      const miningSelectedTask = computed(() => miningTasks.value.find((task) => String(task.id) === String(miningSelectedTaskId.value)) || null);
      const miningProgress = computed(() => QuicDataMining.taskProgress(miningSelectedTask.value));
      const miningKpiStats = computed(() => {
        const tasks = miningTasks.value || [];
        const batches = tasks.flatMap((task) => task.batches || []);
        const qcDoneBatches = batches.filter((batch) => miningQcStatus(batch).done).length;
        const pendingSplit = batches.filter((batch) => !miningSplitDone(batch));
        const pendingEpisodes = pendingSplit.reduce((sum, batch) => sum + (Number(batch.target) || Number(batch.raw) || 0), 0);
        const pendingAssignBatches = batches.filter((batch) => !(batch.assignees || []).length).length;
        // API durations have two decimal places; sum hundredths to avoid float tails.
        const totalTargetHours = tasks.reduce((sum, task) => {
          const hours = Number(task.target_duration_hours);
          return sum + (Number.isFinite(hours) ? Math.round(hours * 100) : 0);
        }, 0) / 100;
        const pendingAssignTasks = tasks.filter((task) => !miningTaskAssignDone(task)).length;
        const rateBatches = batches.filter((batch) => Number(batch.raw) > 0);
        const avgValidRate = rateBatches.length
          ? Math.round(rateBatches.reduce((sum, batch) => sum + (Number(batch.valid) || 0) / Number(batch.raw), 0) / rateBatches.length * 100)
          : 0;
        return { batchCount: batches.length, qcDoneBatches, pendingSplitBatches: pendingSplit.length, pendingEpisodes, pendingAssignBatches, avgValidRate, totalTasks: tasks.length, totalTargetHours, pendingAssignTasks };
      });

      const miningBatchFilterText = ref('');
      const miningBatchFilters = ref([]);

      const miningTaskFilter = reactive({ name: [], owner: [], purpose: [], scene: [], train: [], modality: [], assignStatus: [], targetHoursMin: null, targetHoursMax: null, validRate: [], dateRange: null });
      // The collection project scope is shared by the task list and the
      // collection dashboards so both views always filter on the same project.
      const miningProjectScope = computed({
        get: () => miningDashFilters.project,
        set: (value) => { miningDashFilters.project = Array.isArray(value) ? value : []; },
      });
      const selectedCollectionProjectIds = computed(() => {
        const selectedNames = new Set(miningProjectScope.value.map((name) => String(name)));
        return collectionProjectOptions.value
          .filter((project) => selectedNames.has(String(project.name)))
          .map((project) => project.id);
      });
      function pruneCollectionProjectScope() {
        const availableNames = new Set(collectionProjectOptions.value.map((project) => String(project.name)));
        miningProjectScope.value = miningProjectScope.value.filter((name) => availableNames.has(String(name)));
      }
      const miningProjectOptions = computed(() => {
        const fromProjects = (collectionProjects.value || [])
          .filter((project) => project.status !== 'archived')
          .map((project) => project.name)
          .filter(Boolean);
        const fromTasks = miningTaskRows.value.map((row) => row.tags?.project).filter(Boolean);
        return [...new Set([...fromProjects, ...fromTasks])];
      });
      function resetMiningTaskFilter() {
        Object.assign(miningTaskFilter, { name: [], owner: [], purpose: [], scene: [], train: [], modality: [], assignStatus: [], targetHoursMin: null, targetHoursMax: null, validRate: [], dateRange: null });
      }
      const miningTaskNameOptions = computed(() => [...new Set(miningTaskRows.value.map((row) => row.name).filter(Boolean))]);
      const miningTaskOwnerOptions = computed(() => [...new Set(miningTaskRows.value.map((row) => row.owner).filter(Boolean))]);
      const miningOwnerCandidates = computed(() => {
        const names = new Set(miningTaskOwnerOptions.value);
        for (const user of managedUsers.value || []) {
          if (user?.nickname) names.add(String(user.nickname));
        }
        return [...names];
      });
      const miningTaskPurposeOptions = computed(() => [...new Set(miningTaskRows.value.map((row) => row.tags?.purpose).filter(Boolean))]);
      const miningTaskSceneOptions = computed(() => [...new Set(miningTaskRows.value.map((row) => row.tags?.scene).filter(Boolean))]);
      const miningTaskTrainOptions = computed(() => [...new Set(miningTaskRows.value.map((row) => row.tags?.train).filter(Boolean))]);
      const miningTaskModalityOptions = computed(() => [...new Set(miningTaskRows.value.map((row) => row.modality).filter(Boolean))]);
      const validRateBucketOptions = [
        { value: 'lt30', labelKey: 'rateBucketLt30', min: 0, max: 30 },
        { value: '30to40', labelKey: 'rateBucket30to40', min: 30, max: 40 },
        { value: '40to50', labelKey: 'rateBucket40to50', min: 40, max: 50 },
        { value: '50to60', labelKey: 'rateBucket50to60', min: 50, max: 60 },
        { value: '60to70', labelKey: 'rateBucket60to70', min: 60, max: 70 },
        { value: '70to80', labelKey: 'rateBucket70to80', min: 70, max: 80 },
        { value: '80to90', labelKey: 'rateBucket80to90', min: 80, max: 90 },
        { value: '90to100', labelKey: 'rateBucket90to100', min: 90, max: 101 },
      ];
      const filteredMiningTaskRows = computed(() => {
        const filter = miningTaskFilter;
        return miningTaskRows.value.filter((row) => {
          if (miningProjectScope.value.length && !miningProjectScope.value.includes(String(row.tags?.project || ''))) return false;
          if (Array.isArray(filter.name) && filter.name.length && !filter.name.some((selected) => String(row.name || '').includes(selected))) return false;
          if (filter.owner.length && !filter.owner.includes(String(row.owner || ''))) return false;
          if (filter.purpose.length && !filter.purpose.includes(String(row.tags?.purpose || ''))) return false;
          if (filter.scene.length && !filter.scene.includes(String(row.tags?.scene || ''))) return false;
          if (filter.train.length && !filter.train.includes(String(row.tags?.train || ''))) return false;
          if (filter.modality.length && !filter.modality.includes(String(row.modality || ''))) return false;
          if (filter.assignStatus.length) {
            const done = miningTaskAssignDone(row);
            if (!filter.assignStatus.includes(done ? 'done' : 'todo')) return false;
          }
          if (Array.isArray(filter.dateRange) && filter.dateRange.length === 2 && filter.dateRange[0] && filter.dateRange[1]) {
            const day = String(row.created_at || '').slice(0, 10);
            const startDay = String(filter.dateRange[0]).slice(0, 10);
            const endDay = String(filter.dateRange[1]).slice(0, 10);
            if (!day || day < startDay || day > endDay) return false;
          }
          const hours = Number(row.target_duration_hours) || 0;
          if (filter.targetHoursMin != null && Number(filter.targetHoursMin) > 0 && hours < Number(filter.targetHoursMin)) return false;
          if (filter.targetHoursMax != null && Number(filter.targetHoursMax) > 0 && hours > Number(filter.targetHoursMax)) return false;
          if (filter.validRate.length) {
            const rate = (Number(row.progress?.valid_rate) || 0) * 100;
            const matched = filter.validRate.some((value) => {
              const bucket = validRateBucketOptions.find((item) => item.value === value);
              return bucket && rate >= bucket.min && rate < bucket.max;
            });
            if (!matched) return false;
          }
          return true;
        });
      });

      function miningBatchFilterValueOf(batch, key) {
        if (key === 'seq') return `#${batch.seq}`;
        if (key === 'batch_type') return batchTypeLabel(batch.batch_type);
        if (key === 'assignees') return miningAssigneeLabel(batch);
        if (key === 'qc') return miningQcStatus(batch).done ? t('qcDone') : t('qcUnfinished');
        if (key === 'split') return miningSplitStatusText(batch);
        return miningStageStatusLabel(stageOf(batch, key).status);
      }

      const miningBatchFilterAttrs = computed(() => ([
        { key: 'seq', label: t('batchSeq') },
        { key: 'batch_type', label: t('dataType') },
        { key: 'assignees', label: t('batchAssignees') },
        { key: 'integrity', label: t('stageIntegrity') },
        { key: 'quality', label: t('stageQuality') },
        { key: 'desensitize', label: t('stageDesensitize') },
      ]));

      const miningBatchFilterValueMap = computed(() => {
        const map = {};
        for (const attr of miningBatchFilterAttrs.value) {
          const values = new Set();
          for (const batch of miningSelectedTask.value?.batches || []) {
            const value = miningBatchFilterValueOf(batch, attr.key);
            if (value && value !== '—') values.add(value);
          }
          map[attr.key] = Array.from(values);
        }
        return map;
      });

      function miningBatchMatchesFilters(batch, filters) {
        const groups = {};
        for (const filter of filters) {
          (groups[filter.attr] = groups[filter.attr] || []).push(filter.value);
        }
        return Object.entries(groups).every(([attr, values]) => values.includes(miningBatchFilterValueOf(batch, attr)));
      }

      const miningBatchDetailFilter = reactive({ name: [], modality: [], collectedMin: null, collectedMax: null, validMin: null, validMax: null, collector: [], device: [], uploadRange: null });
      function resetMiningBatchDetailFilter() {
        Object.assign(miningBatchDetailFilter, { name: [], modality: [], collectedMin: null, collectedMax: null, validMin: null, validMax: null, collector: [], device: [], uploadRange: null });
      }
      function miningBatchCollectedHours(batch) {
        if (batch?.captured_duration_hours != null && batch?.captured_duration_hours !== '') {
          return Number(batch.captured_duration_hours) || 0;
        }
        return (Number(batch?.source_duration_s) || 0) / 3600;
      }
      function miningBatchValidHours(batch) {
        if (batch?.intake_valid_duration_hours != null && batch?.intake_valid_duration_hours !== '') {
          return Number(batch.intake_valid_duration_hours) || 0;
        }
        if (batch?.valid_duration_hours != null && batch?.valid_duration_hours !== '') {
          return Number(batch.valid_duration_hours) || 0;
        }
        const collected = Number(batch?.source_duration_s) || 0;
        const raw = Number(batch?.raw) || 0;
        const valid = Number(batch?.valid) || 0;
        return raw > 0 ? (collected * (valid / raw)) / 3600 : 0;
      }
      function miningBatchTargetHoursText(batch) {
        if (batch?.target_duration_hours != null && batch?.target_duration_hours !== '') {
          const h = Number(batch.target_duration_hours);
          if (Number.isFinite(h) && h > 0) return h.toFixed(1) + ' ' + t('dashHour');
        }
        if (demoMode.value) {
          const hours = (Number(batch?.target) || 0) * 42 / 3600;
          return hours > 0 ? hours.toFixed(1) + ' ' + t('dashHour') : '—';
        }
        const hours = Number(batch?.target || 0);
        return hours > 0 ? hours.toFixed(1) + ' ' + t('dashHour') : '—';
      }
      const miningBatchNameOptions = computed(() => [...new Set((miningSelectedTask.value?.batches || []).map((batch) => batch.name).filter(Boolean))]);
      const miningBatchModalityOptions = computed(() => [...new Set((miningSelectedTask.value?.batches || []).map((batch) => batch.batch_type).filter(Boolean))]);
      const miningBatchCollectorOptions = computed(() => {
        const set = new Set();
        for (const batch of miningSelectedTask.value?.batches || []) {
          for (const line of miningAssigneeCollectors(batch)) set.add(line);
        }
        return [...set];
      });
      const miningBatchDeviceOptions = computed(() => {
        const set = new Set();
        for (const batch of miningSelectedTask.value?.batches || []) {
          for (const device of miningAssigneeDevices(batch)) {
            if (device?.name) set.add(String(device.name));
          }
        }
        return [...set];
      });
      const miningBatchRowsFiltered = computed(() => (miningSelectedTask.value?.batches || []).filter((batch) => {
        const keyword = miningBatchFilterText.value.trim().toLowerCase();
        if (keyword) {
          const haystack = [batch.name, `#${batch.seq}`, batchTypeLabel(batch.batch_type), miningAssigneeLabel(batch)].join(' ').toLowerCase();
          const matched = keyword.split('|').some((word) => word.trim() && haystack.includes(word.trim()));
          if (!matched) return false;
        }
        if (!miningBatchMatchesFilters(batch, miningBatchFilters.value)) return false;
        const filter = miningBatchDetailFilter;
        if (filter.name.length && !filter.name.includes(String(batch.name || ''))) return false;
        if (filter.modality.length && !filter.modality.includes(String(batch.batch_type || ''))) return false;
        const collectedHours = miningBatchCollectedHours(batch);
        const validHours = miningBatchValidHours(batch);
        if (filter.collectedMin != null && Number(filter.collectedMin) > 0 && collectedHours < Number(filter.collectedMin)) return false;
        if (filter.collectedMax != null && Number(filter.collectedMax) > 0 && collectedHours > Number(filter.collectedMax)) return false;
        if (filter.validMin != null && Number(filter.validMin) > 0 && validHours < Number(filter.validMin)) return false;
        if (filter.validMax != null && Number(filter.validMax) > 0 && validHours > Number(filter.validMax)) return false;
        if (filter.collector.length) {
          const lines = miningAssigneeCollectors(batch);
          if (!lines.some((line) => filter.collector.some((selected) => line.includes(selected)))) return false;
        }
        if (filter.device.length) {
          const devices = miningAssigneeDevices(batch).map((device) => String(device?.name || ''));
          if (!devices.some((name) => filter.device.includes(name))) return false;
        }
        if (Array.isArray(filter.uploadRange) && filter.uploadRange.length === 2 && filter.uploadRange[0] && filter.uploadRange[1]) {
          const day = String(batch.created_at || '').slice(0, 10);
          const startDay = String(filter.uploadRange[0]).slice(0, 10);
          const endDay = String(filter.uploadRange[1]).slice(0, 10);
          if (!day || day < startDay || day > endDay) return false;
        }
        return true;
      }));
      const miningFailureRows = computed(() => QuicDataMining.aggregateFailures(miningSelectedTask.value?.batches || []));
      const miningPipelineRows = computed(() => QuicDataMining.pipelineStageRows(miningPipelines.value));
      const miningTransferSummary = computed(() => QuicDataMining.transferSummary(miningTransfers.value));

      const miningStageDefs = [
        { key: 'integrity', labelKey: 'stageIntegrity' },
        { key: 'split', labelKey: 'stageSplit' },
        { key: 'quality', labelKey: 'stageQuality' },
        { key: 'desensitize', labelKey: 'stageDesensitize' },
      ];

      function stageOf(batch, key) {
        return QuicDataMining.batchStage(batch, key) || { key, status: 'queued', progress: 0 };
      }

      function miningStageStatusLabel(status) {
        const labels = {
          queued: 'stageQueued',
          running: 'stageRunning',
          pending_review: 'stagePendingReview',
          succeeded: 'stageSucceeded',
          failed: 'stageFailed',
          rejected: 'stageRejected',
          manual: 'stageManual',
          error: 'governanceError',
        };
        return t(labels[status] || 'stageQueued');
      }

      function miningStageAggregatedStatus(batch, stage) {
        const info = overviewImportStage(batch, stage) || {};
        if (info.status === 'running' || info.status === 'pending_review' || info.status === 'manual') return info.status;
        const counts = stageItemCheckCounts(batch, stage) || {};
        const failed = Number(counts.failed) || 0;
        const error = Number(counts.error) || 0;
        const passed = Number(counts.passed) || 0;
        if (failed > 0 && error > 0) return 'failed';
        if (failed > 0) return 'rejected';
        if (error > 0) return 'error';
        if (passed > 0) return 'succeeded';
        return info.status || 'queued';
      }

      function miningStageStatusType(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'running' || status === 'pending_review') return 'warning';
        if (status === 'failed' || status === 'error' || status === 'rejected') return 'danger';
        if (status === 'manual') return 'primary';
        return 'info';
      }

      const miningBatchDispatchOverrides = reactive({});

      function miningDispatchStatus(batch) {
        if (miningBatchDispatchOverrides[batch.id]) return 'dispatched';
        if (miningBatchUnassigned(batch)) return 'pending_dispatch';
        const seed = String(batch.id || '').split('').reduce((acc, ch) => acc + ch.charCodeAt(0), 0);
        return seed % 2 === 0 ? 'dispatched' : 'pending_dispatch';
      }

      function dispatchMiningBatch(batch) {
        if (!batch?.id) return;
        miningBatchDispatchOverrides[batch.id] = true;
        ElMessage.success(t('dispatchDoneToast'));
      }

      const miningBatchRows = computed(() => (miningSelectedTask.value?.batches || []).map((batch) => ({
        ...batch,
        stages: QuicDataMining.batchStageRows(batch),
        actions: miningBatchActions(batch),
      })));

      function miningBatchActions(batch) {
        const actions = [];
        const rows = QuicDataMining.batchStageRows(batch);
        if (miningRunningStage.value || rows.some((row) => row.status === 'running')) return actions;
        const failed = rows.find((row) => row.status === 'failed');
        if (failed) {
          actions.push({ key: `retry-${failed.key}`, label: t('actRetryStage'), run: () => runMiningStage(batch, failed.key) });
          return actions;
        }
        const split = rows.find((row) => row.key === 'split');
        if (split.status === 'pending_review') {
          actions.push({ key: 'review', label: t('cutReview'), run: () => openMiningSplitReview(batch) });
          return actions;
        }
        if (split.status === 'manual') {
          actions.push({ key: 'manual', label: t('actManualCut'), run: () => manualCutBatch(batch) });
          return actions;
        }
        const next = rows.find((row) => ['queued', 'failed'].includes(row.status));
        if (next) {
          const labels = { integrity: 'actRunIntegrity', split: 'actRunSplit', quality: 'actRunQuality', desensitize: 'actRunDesensitize' };
          actions.push({ key: next.key, label: t(labels[next.key] || 'actRunIntegrity'), run: () => runMiningStage(batch, next.key) });
        }
        return actions;
      }

      function runMiningStage(batch, key) {
        if (!miningSelectedTaskId.value || !batch?.id) return;
        if (!QuicDataMining.canRunStage(batch, key)) {
          ElMessage.warning(t('miningStageBlocked'));
          return;
        }
        miningRunningStage.value = `${batch.id}:${key}`;
        loadMiningData();
        ElMessage.success(t('miningStageStarted'));
        QuicDataMining.simulateBatchStageRun(miningSelectedTaskId.value, batch.id, key, () => {
          miningRunningStage.value = '';
          loadMiningData();
          ElMessage.success(key === 'split' ? t('splitReviewHint') : t('miningStageDone'));
        });
      }

      function approveMiningSplit(batch) {
        QuicDataMining.reviewTaskBatchSplit(miningSelectedTaskId.value, batch.id, true);
        loadMiningData();
        ElMessage.success(t('miningSplitApproved'));
      }

      function rerunMiningSplit(batch) {
        QuicDataMining.reviewTaskBatchSplit(miningSelectedTaskId.value, batch.id, false);
        loadMiningData();
        ElMessage.success(t('miningSplitRework'));
        QuicDataMining.simulateBatchStageRun(miningSelectedTaskId.value, batch.id, 'split', () => {
          loadMiningData();
          ElMessage.success(t('splitReviewHint'));
        });
      }

      function restoreMiningSplit(batch) {
        QuicDataMining.restoreTaskBatchToManual(miningSelectedTaskId.value, batch.id);
        loadMiningData();
        ElMessage.success(t('miningSplitRestored'));
      }

      function manualCutBatch() {
        ElMessage.info(t('actManualCut'));
        navigate('work-queue', { queueStageOverride: 'cut' });
      }

      function openMiningTaskPackagesPage(row) {
        if (!row?.id) return;
        if (typeof window !== 'undefined') {
          const target = new URL(window.location.href);
          target.hash = `#/miningTasks?task_id=${encodeURIComponent(row.id)}`;
          // Keep navigation in the current SPA session so a new login is not required.
          window.location.assign(target.toString());
        }
      }

      function closeMiningTaskPackagesPage() {
        closeMiningInlinePackageDetail();
        showMiningTaskPackagesPage.value = false;
        if (typeof window !== 'undefined' && window.location.hash !== '#/miningTasks') {
          window.location.hash = '#/miningTasks';
        }
      }

      async function selectMiningTask(row, { preserveScroll = true } = {}) {
        const id = row && typeof row === 'object' ? row.id : row;
        if (id == null || id === '') return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const selectionGeneration = ++miningTaskSelectionGeneration;
        const isCurrent = () => selectionGeneration === miningTaskSelectionGeneration
          && workspaceId === (Number(selectedWorkspaceId.value) || 0);
        const savedScrollY = typeof window !== 'undefined' ? window.scrollY : 0;
        miningSelectedTaskId.value = String(id);
        miningPipelines.value = demoMode.value ? QuicDataMining.listPipelines({ task_id: id }) : [];

        if (preserveScroll && typeof window !== 'undefined') {
          await nextTick();
          if (!isCurrent()) return;
          if (Math.abs(window.scrollY - savedScrollY) > 2) {
            window.scrollTo({ top: savedScrollY, behavior: 'instant' });
          }
        }

        if (!demoMode.value && workspaceId && isCurrent()) {
          loading.miningPackages = true;
          try {
            const pkgsRes = await QuicDataAPI.listTaskPackages(id, workspaceId);
            if (!isCurrent()) return;
            const pkgs = Array.isArray(pkgsRes?.items) ? pkgsRes.items : (Array.isArray(pkgsRes) ? pkgsRes : []);
            const targetTask = miningTasks.value.find((t) => String(t.id) === String(id));
            if (targetTask) {
              targetTask.batches = pkgs.map((pkg, idx) => {
                const collectorId = pkg.responsible_collector_id || pkg.collector_id || pkg.operator_collector_id;
                const isPlanned = pkg.status === 'pending_assignment' || pkg.status === 'staged';
                const isAssigned = pkg.status === 'assigned' || pkg.status === 'pending_upload';
                const isDone = pkg.status === 'completed' || pkg.status === 'reviewed' || pkg.status === 'intake_approved' || pkg.status === 'batched';
                const packageTargetHours = Number(pkg.target_duration_hours);
                return {
                  ...pkg,
                  id: pkg.id,
                  seq: idx + 1,
                  name: pkg.package_uid,
                  batch_type: targetTask.modality || pkg.batch_type || 'ego',
                  target: Number.isFinite(packageTargetHours) && packageTargetHours > 0 ? packageTargetHours : null,
                  raw: Number(pkg.captured_duration_hours ?? pkg.intake_valid_duration_hours) || 0,
                  checked: Number(pkg.intake_valid_duration_hours) || 0,
                  valid: Number(pkg.intake_valid_duration_hours) || 0,
                  target_duration_hours: pkg.target_duration_hours,
                  captured_duration_hours: pkg.captured_duration_hours,
                  valid_duration_hours: pkg.intake_valid_duration_hours || 0,
                  intake_valid_duration_hours: pkg.intake_valid_duration_hours || 0,
                  raw_status: pkg.status,
                  status: isPlanned ? 'planned' : (isAssigned ? 'collecting' : (isDone ? 'done' : 'processing')),
                  assignees: collectorId ? [{ collector_id: collectorId, device_id: pkg.collection_device_id }] : [],
                  package_uid: pkg.package_uid,
                };
              });
              targetTask.package_count = pkgs.length;
              targetTask.pending_assignment_count = pkgs.filter((pkg) => pkg.status === 'pending_assignment').length;
            }
          } catch (e) {
            console.error('Failed to load task packages', e);
          } finally {
            if (isCurrent()) loading.miningPackages = false;
            if (preserveScroll && typeof window !== 'undefined') {
              await nextTick();
              if (isCurrent() && Math.abs(window.scrollY - savedScrollY) > 2) {
                window.scrollTo({ top: savedScrollY, behavior: 'instant' });
              }
            }
          }
        }
      }

      function miningDialogScopeMatches(workspaceId, dialogWorkspaceId, visible) {
        return Boolean(visible?.value)
          && Number(dialogWorkspaceId) === Number(workspaceId)
          && Number(selectedWorkspaceId.value) === Number(workspaceId);
      }

      function resetMiningTaskDialogState({ close = true, invalidate = true } = {}) {
        if (invalidate) miningWriteGeneration += 1;
        if (close) showMiningTaskDialog.value = false;
        miningTaskDialogWorkspaceId = 0;
        miningTaskForm.name = '';
        miningTaskForm.sop = '';
        miningTaskForm.package_hours = 2;
        saving.value = false;
      }

      function onMiningTaskDialogClosed() {
        if (showMiningTaskDialog.value) return;
        resetMiningTaskDialogState();
      }

      function resetMiningSplitDialogState({ close = true, invalidate = true } = {}) {
        if (invalidate) miningWriteGeneration += 1;
        if (close) showMiningSplitDialog.value = false;
        miningSplitDialogWorkspaceId = 0;
        saving.value = false;
      }

      function onMiningSplitDialogClosed() {
        if (showMiningSplitDialog.value) return;
        resetMiningSplitDialogState();
      }

      function resetMiningAssignDialogState({ close = true, invalidate = true } = {}) {
        if (invalidate) miningWriteGeneration += 1;
        if (close) showMiningAssignDialog.value = false;
        miningAssignDialogWorkspaceId = 0;
        miningAssignTaskId = '';
        miningAssignForm.batch_id = '';
        miningAssignForm.collector_ids = [];
        miningAssignForm.device_id = '';
        miningAssignForm.window = '';
        saving.value = false;
      }

      function onMiningAssignDialogClosed() {
        if (showMiningAssignDialog.value) return;
        resetMiningAssignDialogState();
      }

      function openMiningTaskDialog() {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId && !demoMode.value) {
          ElMessage.warning(t('noWorkspace'));
          return;
        }
        resetMiningTaskDialogState({ close: false });
        miningTaskDialogWorkspaceId = workspaceId;
        showMiningTaskDialog.value = true;
      }

      function openMiningSplitDialog(task = null) {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const taskId = task?.id || miningSelectedTaskId.value;
        if (!taskId || (!workspaceId && !demoMode.value)) return;
        resetMiningSplitDialogState({ close: false });
        miningSplitDialogWorkspaceId = workspaceId;
        miningSelectedTaskId.value = String(taskId);
        const target = task || miningTasks.value.find((item) => String(item.id) === String(taskId));
        const existingCount = target?.batches?.length || 0;
        miningSplitForm.batch_count = Math.min(200, Math.max(1, existingCount || miningSplitForm.batch_count || 10));
        showMiningSplitDialog.value = true;
      }

      function openMiningAssignScopeDialog(workspaceId) {
        resetMiningAssignDialogState({ close: false });
        miningAssignDialogWorkspaceId = Number(workspaceId) || 0;
        showMiningAssignDialog.value = true;
      }

      async function createMiningTask() {
        if (!String(miningTaskForm.name || '').trim()) {
          ElMessage.warning(t('miningTaskNameRequired'));
          return;
        }
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if ((!workspaceId && !demoMode.value)
          || !miningDialogScopeMatches(workspaceId, miningTaskDialogWorkspaceId, showMiningTaskDialog)) {
          resetMiningTaskDialogState();
          ElMessage.warning(t('scopeChanged'));
          return;
        }
        const requestGeneration = ++miningWriteGeneration;
        const isCurrent = () => requestGeneration === miningWriteGeneration
          && miningDialogScopeMatches(workspaceId, miningTaskDialogWorkspaceId, showMiningTaskDialog);
        const scopeMatches = () => Number(selectedWorkspaceId.value) === workspaceId;
        if (demoMode.value) {
          const task = QuicDataMining.createTask({
            name: miningTaskForm.name,
            modality: miningTaskForm.modality,
            sop: miningTaskForm.sop,
            target_episodes: Number(miningTaskForm.target_episodes) || 0,
            target_duration_hours: Number(miningTaskForm.target_duration_hours) || 0,
            min_valid_rate: Number(miningTaskForm.min_valid_rate) || 0,
            due_date: miningTaskForm.due_date,
            tags: { project: miningTaskForm.project, purpose: miningTaskForm.purpose, scene: miningTaskForm.scene, train: miningTaskForm.train },
            owner: miningTaskForm.owner,
            workspace_id: workspaceId || null,
            task_set_id: selectedTaskSetId.value || null,
          });
          if (!isCurrent()) return;
          resetMiningTaskDialogState();
          loadMiningData();
          miningSelectedTaskId.value = String(task.id);
          ElMessage.success(t('miningTaskCreated'));
          return;
        }

        let project = (collectionProjects.value || []).find((p) => p.name === miningTaskForm.project);
        if (!project && collectionProjects.value && collectionProjects.value.length) {
          project = collectionProjects.value[0];
        }
        if (!project) {
          try {
            const projectsRes = await QuicDataAPI.listCollectionProjects(workspaceId);
            if (!isCurrent()) return;
            const list = Array.isArray(projectsRes?.items) ? projectsRes.items : (Array.isArray(projectsRes) ? projectsRes : []);
            if (list.length) {
              collectionProjects.value = list;
              project = list[0];
            } else {
              if (!isCurrent()) return;
              project = await QuicDataAPI.createCollectionProject({
                workspace_id: workspaceId,
                name: miningTaskForm.project || '默认采集项目',
                description: 'QuicStudio 默认具身智能数采项目',
              });
              if (!isCurrent()) return;
              collectionProjects.value = [project];
            }
          } catch (e) {
            if (isCurrent()) errorMessage(e);
            return;
          }
        }

        const labelIds = [];
        const activeLabels = collectionLabels.value || [];
        for (const tagKey of ['project', 'purpose', 'scene', 'train']) {
          const val = miningTaskForm[tagKey];
          if (val) {
            const found = activeLabels.find((l) => l.name === val || l.name.includes(val));
            if (found && !labelIds.includes(found.id)) labelIds.push(found.id);
          }
        }

        const targetHours = Number(miningTaskForm.target_duration_hours);
        if (!Number.isFinite(targetHours) || targetHours < 0.01) {
          ElMessage.warning(locale.value === 'en-US' ? 'Target duration must be at least 0.01 hours.' : '目标时长必须至少为 0.01 小时');
          return;
        }
        const packageHours = Number(miningTaskForm.package_hours);
        if (!Number.isFinite(packageHours) || packageHours < 0.01) {
          ElMessage.warning(locale.value === 'en-US' ? 'Package duration must be at least 0.01 hours.' : '单包目标时长必须至少为 0.01 小时');
          return;
        }

        try {
          if (!isCurrent()) return;
          const created = await QuicDataAPI.createCollectionTask({
            workspace_id: workspaceId,
            collection_project_id: Number(project.id),
            name: miningTaskForm.name.trim(),
            description: miningTaskForm.sop || '',
            target_duration_hours: targetHours.toFixed(2),
            default_package_duration_hours: packageHours.toFixed(2),
            sop_text: miningTaskForm.sop || '',
            device_model_id: null,
            label_ids: labelIds,
          });
          if (!isCurrent()) return;
          resetMiningTaskDialogState();
          await loadMiningData();
          if (scopeMatches() && created && created.id) {
            miningSelectedTaskId.value = String(created.id);
          }
          if (scopeMatches()) ElMessage.success(t('miningTaskCreated'));
        } catch (err) {
          if (isCurrent()) errorMessage(err);
        }
      }

      async function splitMiningTask() {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const taskId = String(miningSelectedTaskId.value || '');
        if (!taskId || (!workspaceId && !demoMode.value)
          || !miningDialogScopeMatches(workspaceId, miningSplitDialogWorkspaceId, showMiningSplitDialog)) {
          resetMiningSplitDialogState();
          ElMessage.warning(t('scopeChanged'));
          return;
        }
        const requestGeneration = ++miningWriteGeneration;
        const isCurrent = () => requestGeneration === miningWriteGeneration
          && miningDialogScopeMatches(workspaceId, miningSplitDialogWorkspaceId, showMiningSplitDialog)
          && String(miningSelectedTaskId.value) === taskId;
        const scopeMatches = () => Number(selectedWorkspaceId.value) === workspaceId
          && String(miningSelectedTaskId.value) === taskId;
        if (demoMode.value) {
          const created = QuicDataMining.splitTaskBatches(taskId, {
            batch_count: Number(miningSplitForm.batch_count) || 1,
            per_batch: Number(miningSplitForm.per_batch) || 0,
          });
          if (!isCurrent()) return;
          resetMiningSplitDialogState();
          loadMiningData();
          ElMessage.success(String(t('splitBatchesDone')).replace('{count}', String(created?.length || 0)));
          return;
        }

        const batchCount = Math.max(1, Number(miningSplitForm.batch_count) || 1);
        const pending = (miningSelectedTask.value?.batches || []).filter((batch) => QuicDataMiningUtils.canAssignPackage(batch)).sort((a, b) => Number(a.id) - Number(b.id));
        if (!pending.length) { ElMessage.warning('没有可拆分的未分配数据包'); return; }
        const total = pending.reduce((sum,b) => sum + Math.round(Number(b.target_duration_hours ?? b.target ?? 0) * 100), 0);
        if (total < batchCount) { ElMessage.warning('每包目标时长至少 0.01 小时'); return; }
        const operations = pending.length > 1 ? [{ op: 'merge', data_package_ids: pending.map(b => Number(b.id)) }] : [];
        if (batchCount > 1) operations.push({ op: 'split', data_package_id: Number(pending[0].id), durations: Array.from({length: batchCount}, (_,i) => ((Math.floor(total / batchCount) + (i < total % batchCount ? 1 : 0)) / 100).toFixed(2)) });
        if (!operations.length) { resetMiningSplitDialogState(); return; }

        try {
          if (!isCurrent()) return;
          const res = await QuicDataAPI.adjustDataPackages({
            workspace_id: workspaceId,
            collection_task_id: Number(taskId),
            operations,
          });
          if (!isCurrent()) return;
          resetMiningSplitDialogState();
          await loadMiningData();
          const count = res?.packages?.length || batchCount;
          if (scopeMatches()) ElMessage.success(String(t('splitBatchesDone')).replace('{count}', String(count)));
        } catch (err) {
          if (isCurrent()) errorMessage(err);
        }
      }

      function miningTaskSplitDone(task) {
        return (task?.batches || []).length > 0;
      }

      function miningTaskAssignDone(task) {
        return QuicDataMiningUtils.taskAssignDone(task);
      }

      function canAssignPackage(row) {
        return QuicDataMiningUtils.canAssignPackage(row);
      }

      function openMiningTaskSplit(task) {
        if (!task?.id) return;
        openMiningSplitDialog(task);
      }

      const batchPreviewIndex = ref(0);
      const batchDetailPageFilter = reactive({ task: '', batch: '', collectRange: null, uploadRange: null, modality: '', collector: '', device: '' });
      function resetBatchDetailPageFilter() {
        Object.assign(batchDetailPageFilter, { task: '', batch: '', collectRange: null, uploadRange: null, modality: '', collector: '', device: '' });
      }
      const detailTaskOptions = computed(() => miningTaskRows.value.map((row) => ({ id: String(row.id), name: row.name })));
      const detailBatchOptions = computed(() => (batches.value || []).map((batch) => ({ id: String(batch.id), label: '#' + batch.sequence_number + ' ' + (batch.name || '') })));
      const detailModalityOptions = computed(() => [...new Set((batches.value || []).map((batch) => batch.batch_type).filter(Boolean))]);
      function onDetailTaskChange(taskId) {
        if (!taskId) return;
        const task = miningTaskRows.value.find((row) => String(row.id) === String(taskId));
        if (!task) return;
        const target = (batches.value || []).find((batch) => String(batch.task_set_id ?? '') === String(task.task_set_id ?? ''));
        if (target) openBatchWithMiningMock(target);
      }
      function onDetailBatchChange(batchId) {
        if (!batchId) return;
        const target = (batches.value || []).find((batch) => String(batch.id) === String(batchId));
        if (target) openBatchWithMiningMock(target);
      }
      const filteredBatchDetailImports = computed(() => {
        const filter = batchDetailPageFilter;
        const batch = selectedBatch.value;
        const mining = miningBatchOfSelectedBatch.value;
        return (batchImports.value || []).filter((item) => {
          if (Array.isArray(filter.uploadRange) && filter.uploadRange.length === 2 && filter.uploadRange[0] && filter.uploadRange[1]) {
            const day = String(item.updated_at || '').slice(0, 10);
            if (!day || day < String(filter.uploadRange[0]).slice(0, 10) || day > String(filter.uploadRange[1]).slice(0, 10)) return false;
          }
          if (filter.modality && batch && String(batch.batch_type || '') !== filter.modality) return false;
          if (filter.collector) {
            const lines = miningAssigneeCollectors(mining || {});
            if (!lines.some((line) => line.includes(filter.collector))) return false;
          }
          if (filter.device) {
            const devices = miningAssigneeDevices(mining || {}).map((device) => String(device?.name || ''));
            if (!devices.includes(filter.device)) return false;
          }
          if (Array.isArray(filter.collectRange) && filter.collectRange.length === 2 && filter.collectRange[0] && filter.collectRange[1]) {
            const day = String(batch?.created_at || '').slice(0, 10);
            if (!day || day < String(filter.collectRange[0]).slice(0, 10) || day > String(filter.collectRange[1]).slice(0, 10)) return false;
          }
          return true;
        });
      });
      const batchPreviewCurrent = computed(() => filteredBatchDetailImports.value[batchPreviewIndex.value] || null);
      function prevBatchPreview() {
        if (batchPreviewIndex.value > 0) batchPreviewIndex.value -= 1;
      }
      function nextBatchPreview() {
        if (batchPreviewIndex.value < filteredBatchDetailImports.value.length - 1) batchPreviewIndex.value += 1;
      }
      function openBatchImportDetail(item) {
        if (!item) return;
        const episodeId = Number(item.episode_id) || (demoMode.value ? 4200 : Number(item.id));
        if (!episodeId) return;
        void openEpisodeDetail({ id: episodeId }, { source: 'batch-detail' });
      }
      function selectedBatchMiningContext() {
        const mining = miningBatchOfSelectedBatch.value;
        if (!mining) return null;
        const taskList = demoMode.value ? (QuicDataMining.listTasks() || []) : (miningTasks.value || []);
        const task = taskList.find((row) => (row.batches || []).some((batch) => String(batch.id) === String(mining.id)));
        if (!task) return null;
        return { task, mining };
      }
      function reviewSelectedBatchDetail(approve) {
        const context = selectedBatchMiningContext();
        if (!context) {
          ElMessage.warning(t('noTaskLabel'));
          return;
        }
        if (demoMode.value) {
          QuicDataMining.reviewTaskBatchSplit(context.task.id, context.mining.id, approve);
        }
        loadMiningData();
        ElMessage.success(approve ? t('batchDetailApprovedToast') : t('batchDetailRejectedToast'));
      }
      function openMiningAssignDialog(row) {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId && !demoMode.value) return;
        resetMiningAssignDialogState({ close: false });
        miningAssignDialogWorkspaceId = workspaceId;
        miningAssignTaskId = String(miningSelectedTaskId.value || '');
        miningAssignScope.value = 'batch';
        miningAssignForm.batch_id = String(row?.id || '');
        miningAssignForm.collector_ids = (row?.assignees || []).map((item) => item.collector_id);
        miningAssignForm.device_id = row?.assignees?.[0]?.device_id ?? '';
        miningAssignForm.window = row?.window || '';
        miningAssignForm.mode = 'even';
        showMiningAssignDialog.value = true;
      }

      async function openMiningAssignModeDialog(row) {
        if (!row?.id) return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId && !demoMode.value) return;
        await selectMiningTask(row);
        if (Number(selectedWorkspaceId.value) !== workspaceId) return;
        await openMiningTaskAssignDialog(row);
      }

      async function openMiningTaskAssignDialog(task = null) {
        const target = task || miningSelectedTask.value;
        if (!target?.id) return;
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        if (!workspaceId && !demoMode.value) return;
        resetMiningAssignDialogState({ close: false });
        miningAssignDialogWorkspaceId = workspaceId;
        miningAssignTaskId = String(target.id);
        if (String(miningSelectedTaskId.value) !== String(target.id)) {
          await selectMiningTask(target);
        }
        if (Number(selectedWorkspaceId.value) !== workspaceId || String(miningSelectedTaskId.value) !== String(target.id)) {
          resetMiningAssignDialogState();
          return;
        }
        const currentTask = miningTasks.value.find((t) => String(t.id) === String(target.id)) || target;
        if (!(currentTask.batches || []).some((batch) => QuicDataMiningUtils.canAssignPackage(batch))) {
          resetMiningAssignDialogState();
          ElMessage.warning('没有待分配的数据包');
          return;
        }
        miningAssignScope.value = 'task';
        miningAssignForm.batch_id = '*';
        const first = (currentTask.batches || []).find((batch) => (batch.assignees || []).length);
        miningAssignForm.collector_ids = first ? first.assignees.map((item) => item.collector_id) : [];
        miningAssignForm.device_id = first && first.assignees[0] ? first.assignees[0].device_id : '';
        miningAssignForm.window = '';
        miningAssignForm.mode = 'even';
        showMiningAssignDialog.value = true;
      }

      async function submitMiningAssign() {
        const workspaceId = Number(selectedWorkspaceId.value) || 0;
        const taskId = String(miningAssignTaskId || '');
        if ((!workspaceId && !demoMode.value)
          || !miningDialogScopeMatches(workspaceId, miningAssignDialogWorkspaceId, showMiningAssignDialog)
          || (taskId && String(miningSelectedTaskId.value) !== taskId)) {
          resetMiningAssignDialogState();
          ElMessage.warning(t('scopeChanged'));
          return;
        }
        const requestGeneration = ++miningWriteGeneration;
        const isCurrent = () => requestGeneration === miningWriteGeneration
          && miningDialogScopeMatches(workspaceId, miningAssignDialogWorkspaceId, showMiningAssignDialog)
          && (!taskId || String(miningSelectedTaskId.value) === taskId);
        const scopeMatches = () => Number(selectedWorkspaceId.value) === workspaceId
          && (!taskId || String(miningSelectedTaskId.value) === taskId);
        const deviceId = miningAssignForm.device_id === '' || miningAssignForm.device_id === null ? null : Number(miningAssignForm.device_id);
        const assignScope = miningAssignScope.value;
        const collectorIds = (miningAssignForm.collector_ids || []).map(Number).filter(Boolean);
        const batchId = Number(miningAssignForm.batch_id);
        if (demoMode.value) {
          QuicDataMining.assignTaskBatch(taskId || miningSelectedTaskId.value, miningAssignForm.batch_id, {
            assignees: collectorIds.map((collectorId) => ({ collector_id: Number(collectorId), device_id: deviceId })),
            window: miningAssignForm.window,
            only_unassigned: assignScope === 'task',
          });
          if (!isCurrent()) return;
          resetMiningAssignDialogState();
          loadMiningData();
          ElMessage.success(t(assignScope === 'task' ? 'assignAllSubmitted' : 'assignSubmitted'));
          return;
        }

        if (!collectorIds.length) {
          ElMessage.warning('请选择至少一位采集员');
          return;
        }

        try {
          if (!isCurrent()) return;
          const targetTask = miningTasks.value.find((t) => String(t.id) === String(taskId || miningSelectedTaskId.value));
          const batches = targetTask?.batches || [];

          if (assignScope === 'batch') {
            if (collectorIds.length !== 1) { ElMessage.warning('一个数据包请选择一位数采员'); return; }
            await QuicDataAPI.assignDataPackage(batchId, {
              workspace_id: workspaceId,
              collector_id: collectorIds[0],
            });
          } else {
            const targetBatches = batches.filter((b) => QuicDataMiningUtils.canAssignPackage(b));
            if (!targetBatches.length) { ElMessage.warning('没有待分配的数据包'); return; }
            if (collectorIds.length > targetBatches.length) { ElMessage.warning('数采员数量不能超过待分配包数量'); return; }
            await QuicDataAPI.batchAssignDataPackages({
              workspace_id: workspaceId,
              assignments: targetBatches.map((b, index) => ({ data_package_id: Number(b.id), collector_id: collectorIds[index % collectorIds.length] })),
            });
          }
          if (!isCurrent()) return;
          resetMiningAssignDialogState();
          ElMessage.success(t(assignScope === 'task' ? 'assignAllSubmitted' : 'assignSubmitted'));
          await loadMiningData();
          if (scopeMatches() && miningSelectedTaskId.value) {
            await selectMiningTask(miningSelectedTaskId.value);
          }
        } catch (error) {
          if (isCurrent()) errorMessage(error);
        }
      }

      function retryMiningTransfer(row) {
        miningTransfers.value = QuicDataMining.retryTransfer(row?.id).slice();
      }

      function miningBatchStatusLabel(status) {
        const labels = {
          planned: 'miningBatchPlanned',
          collecting: 'miningBatchCollecting',
          uploaded: 'miningBatchUploaded',
          processing: 'miningBatchProcessing',
          done: 'miningBatchDone',
        };
        return t(labels[status] || 'miningBatchPlanned');
      }

      function miningBatchStatusType(status) {
        if (status === 'done') return 'success';
        if (status === 'processing' || status === 'uploaded') return 'warning';
        if (status === 'collecting') return 'primary';
        return 'info';
      }

      function miningPipelineStatusLabel(status) {
        const labels = { queued: 'pipelineQueued', running: 'pipelineRunning', succeeded: 'pipelineSucceeded', failed: 'pipelineFailed' };
        return t(labels[status] || 'pipelineQueued');
      }

      function miningPipelineStatusType(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'running') return 'warning';
        if (status === 'failed' || status === 'error') return 'danger';
        return 'info';
      }

      function miningTransferStatusLabel(status) {
        const labels = {
          queued: 'transferQueued',
          uploading: 'transferUploading',
          running: 'transferRunning',
          succeeded: 'transferSucceeded',
          failed: 'transferFailed',
        };
        return t(labels[status] || 'transferQueued');
      }

      function miningTransferStatusType(status) {
        if (status === 'succeeded') return 'success';
        if (status === 'failed' || status === 'error') return 'danger';
        if (status === 'running' || status === 'uploading') return 'warning';
        return 'info';
      }

      function miningSourceLabel(source) {
        const labels = { edge: 'cloudSourceEdge', upload: 'cloudSourceUpload', oss: 'cloudSourceOss' };
        return t(labels[source] || 'cloudSourceUpload');
      }

      function miningTaskStatusLabel(status) {
        return status === 'paused' ? t('miningTaskPaused') : t('miningTaskActive');
      }

      function collectorWithJobId(collector, collectorId) {
        if (!collector) return `#${collectorId}`;
        const jobNumber = collector.workspace_sequence ?? collector.id;
        return jobNumber ? `${collector.name}（工号 ${jobNumber}）` : collector.name;
      }

      function miningAssigneeLabel(batch) {
        const assignees = Array.isArray(batch?.assignees) ? batch.assignees : [];
        if (!assignees.length) return t('unassigned');
        return assignees
          .map((item) => {
            const collector = collectorProfiles.value?.find((profile) => Number(profile.id) === Number(item.collector_id));
            const device = collectionDevices.value?.find((entry) => Number(entry.id) === Number(item.device_id));
            const parts = [collectorWithJobId(collector, item.collector_id)];
            if (device?.name) parts.push(device.name);
            return parts.join(' · ');
          })
          .join('、');
      }

      function miningAssigneeCollectors(batch) {
        const assignees = Array.isArray(batch?.assignees) ? batch.assignees : [];
        if (!assignees.length) return [];
        return assignees.map((item) => {
          const collector = collectorProfiles.value?.find((profile) => Number(profile.id) === Number(item.collector_id));
          return collectorWithJobId(collector, item.collector_id);
        });
      }

      function miningAssigneeDevices(batch) {
        const assignees = Array.isArray(batch?.assignees) ? batch.assignees : [];
        const validAssignees = assignees.filter((item) => item?.device_id != null && item?.device_id !== '');
        if (!validAssignees.length) return [];
        return validAssignees.map((item) => {
          const device = collectionDevices.value?.find((entry) => Number(entry.id) === Number(item.device_id));
          if (!device?.name) return { name: '#' + item.device_id, meta: '' };
          return { name: device.name, meta: (device.serial_number || '—') + ' / ' + (device.software_number || '—') };
        });
      }

      function miningAssigneeLines(batch) {
        const assignees = Array.isArray(batch?.assignees) ? batch.assignees : [];
        if (!assignees.length) return [t('unassigned')];
        const persons = [];
        const devices = [];
        for (const item of assignees) {
          const collector = collectorProfiles.value?.find((profile) => Number(profile.id) === Number(item.collector_id));
          const device = collectionDevices.value?.find((entry) => Number(entry.id) === Number(item.device_id));
          persons.push(collectorWithJobId(collector, item.collector_id));
          if (device?.name) devices.push(device.name + '（' + (device.serial_number || '—') + ' / ' + (device.software_number || '—') + '）');
        }
        return devices.length ? [persons.join('、'), devices.join('、')] : [persons.join('、')];
      }

      function loadMiningCuts() {
        const audit = miningCutAudit.value;
        miningCutEpisodes.value = audit
          ? QuicDataMining.listBatchCutEpisodes(audit.task_id, audit.batch_id, audit.task_name)
          : QuicDataMining.listCutEpisodes();
        if (!miningCutEpisodes.value.some((row) => String(row.id) === String(miningCutSelectedId.value))) {
          miningCutSelectedId.value = miningCutEpisodes.value.length ? String(miningCutEpisodes.value[0].id) : '';
        }
      }

      function openMiningSplitReview(batch) {
        if (!miningSelectedTaskId.value || !batch?.id) return;
        miningCutAudit.value = {
          task_id: miningSelectedTaskId.value,
          task_name: miningSelectedTask.value?.name || '',
          batch_id: String(batch.id),
          seq: batch.seq,
          status: 'pending_review',
        };
        miningCutTask.value = '';
        miningCutKeyword.value = '';
        navigate('miningCuts');
        loadMiningCuts();
      }

      function clearMiningCutAudit() {
        miningCutAudit.value = null;
        loadMiningCuts();
      }

      function approveCutAudit() {
        const audit = miningCutAudit.value;
        if (!audit) return;
        approveMiningSplit({ id: audit.batch_id });
        miningCutAudit.value = { ...audit, status: 'succeeded' };
        loadMiningCuts();
      }

      function rerunCutAudit() {
        const audit = miningCutAudit.value;
        if (!audit) return;
        rerunMiningSplit({ id: audit.batch_id });
        miningCutAudit.value = { ...audit, status: 'pending_review' };
        loadMiningCuts();
      }

      function restoreCutAudit() {
        const audit = miningCutAudit.value;
        if (!audit) return;
        restoreMiningSplit({ id: audit.batch_id });
        miningCutAudit.value = { ...audit, status: 'manual' };
        loadMiningCuts();
      }

      function manualCutFromAudit() {
        navigate('work-queue', { queueStageOverride: 'cut' });
      }

      function buildCsvContent(headers, rows) {
        const escapeCsv = (val) => {
          if (val === null || val === undefined) return '';
          const str = String(val);
          if (str.includes(',') || str.includes('"') || str.includes('\n') || str.includes('\r')) {
            return `"${str.replace(/"/g, '""')}"`;
          }
          return str;
        };
        const headerLine = headers.map(escapeCsv).join(',');
        const rowLines = rows.map((r) => headers.map((h) => escapeCsv(r[h])).join(','));
        return '\uFEFF' + [headerLine, ...rowLines].join('\r\n');
      }

      function downloadBlob(filename, blob) {
        if (typeof document === 'undefined' || typeof window === 'undefined') return;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        if (a.parentNode) a.parentNode.removeChild(a);
        URL.revokeObjectURL(url);
      }

      function downloadJsonFile(filename, obj) {
        if (typeof Blob === 'undefined') return;
        const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' });
        downloadBlob(filename, blob);
      }

      function downloadCsvFile(filename, headers, rows) {
        if (typeof Blob === 'undefined') return;
        const csvContent = buildCsvContent(headers, rows);
        const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
        downloadBlob(filename, blob);
      }

      const dataPackageDrawerVisible = ref(false);
      const selectedPackageRow = ref(null);
      const packageDetail = ref(null);
      const packageDetailLoading = ref(false);
      const packageDetailError = ref('');
      let packageDetailGeneration = 0;
      const miningInlinePackageDetailId = ref(null);
      const miningInlinePackageDetail = ref(null);
      const miningInlinePackageDetailLoading = ref(false);
      const miningInlinePackageDetailError = ref('');
      let miningInlinePackageDetailGeneration = 0;
      const miningInlinePreviewIndex = ref(0);
      const miningInlinePreviewStreams = ref([]);
      const miningInlinePreviewStreamIndex = ref(0);
      const miningInlinePreviewLoading = ref(false);
      const miningInlinePreviewError = ref('');
      const miningInlineExtraInfoVisible = ref(false);
      let miningInlinePreviewGeneration = 0;
      const miningInlinePackageExpandedKeys = computed(() => (
        miningInlinePackageDetailId.value == null ? [] : [miningInlinePackageDetailId.value]
      ));
      const miningInlinePreviewEpisodes = computed(() => (
        Array.isArray(miningInlinePackageDetail.value?.episodes)
          ? miningInlinePackageDetail.value.episodes.filter((episode) => episode?.preview_available !== false)
          : []
      ));
      const miningInlinePreviewEpisode = computed(() => (
        miningInlinePreviewEpisodes.value[miningInlinePreviewIndex.value] || null
      ));
      const miningInlinePreviewStream = computed(() => (
        miningInlinePreviewStreams.value[miningInlinePreviewStreamIndex.value] || null
      ));
      const rejectDialogVisible = ref(false);
      const rejectReason = ref('');
      const rejectSubmitting = ref(false);
      const selectedMiningPackages = ref([]);
      const intakeReviewPackageId = ref(null);
      const intakeReviewPackage = ref(null);
      const intakeReviewLoading = ref(false);
      const rejectedEpisodeIds = ref([]);
      const intakePreviewDialogVisible = ref(false);
      const intakePreviewEpisode = ref(null);
      const intakePreviewStreams = ref([]);
      const intakePreviewLoading = ref(false);
      const intakePreviewError = ref('');

      // 数采审核 (collection review) queue: data packages replaced the legacy batches.
      const reviewPackageRows = ref([]);
      const reviewPackageStatus = ref('');
      const reviewPackagePage = ref(1);
      const reviewPackageTotal = ref(0);
      const reviewPackageSelectedIds = ref([]);
      const reviewPackageLoading = ref(false);
      const reviewPackageReturnHighlightId = ref(null);
      let reviewPackageReturnHighlightTimer = null;
      const reviewPackageStatusOptions = [
        'pending_assignment',
        'assigned',
        'pending_upload',
        'uploading',
        'parsing',
        'ingested',
        'pending_intake_review',
        'intake_approved',
        'batched',
        'governing',
        'published',
        'parse_failed',
        'voided',
      ];
      function dataPackageStatusLabel(status) {
        return {
          pending_assignment: t('pkgStatusPendingAssignment'),
          assigned: t('pkgStatusAssigned'),
          pending_upload: t('pkgStatusPendingUpload'),
          uploading: t('pkgStatusUploading'),
          parsing: t('pkgStatusParsing'),
          ingested: t('pkgStatusIngested'),
          pending_intake_review: t('pkgStatusPendingIntakeReview'),
          intake_approved: t('pkgStatusIntakeApproved'),
          batched: t('pkgStatusBatched'),
          governing: t('pkgStatusGoverning'),
          published: t('pkgStatusPublished'),
          parse_failed: t('pkgStatusParseFailed'),
          voided: t('pkgStatusVoided'),
        }[status] || status || '—';
      }
      function dataPackageStatusType(status) {
        return {
          pending_intake_review: 'warning',
          intake_approved: 'success',
          published: 'success',
          batched: 'success',
          parse_failed: 'danger',
          voided: 'info',
        }[status] || 'info';
      }
      function reviewPackageProjectName(row) {
        if (row?.project_name) return row.project_name;
        if (row?.collection_project_name) return row.collection_project_name;
        const project = (collectionProjects.value || []).find((item) => Number(item.id) === Number(row?.collection_project_id));
        return project?.name || '—';
      }
      function reviewPackageTaskName(row) {
        if (row?.task_name) return row.task_name;
        if (row?.collection_task_name) return row.collection_task_name;
        const task = (miningTasks.value || []).find((item) => Number(item.id) === Number(row?.collection_task_id));
        return task?.name || '—';
      }
      function reviewPackageProjectId() {
        return selectedCollectionProjectIds.value.length === 1 ? selectedCollectionProjectIds.value[0] : null;
      }
      function reviewPackageCollectorName(row) {
        if (row?.collector_name) return row.collector_name;
        if (row?.responsible_collector_name) return row.responsible_collector_name;
        const profile = (collectorProfiles.value || []).find((item) => Number(item.id) === Number(row?.responsible_collector_id));
        return profile?.name || '—';
      }
      function reviewPackageDeviceName(row) {
        if (row?.device_name) return row.device_name;
        if (row?.collection_device_name) return row.collection_device_name;
        const device = (collectionDevices.value || []).find((item) => Number(item.id) === Number(row?.collection_device_id));
        return device?.name || '—';
      }
      const reviewPackageRowsFiltered = computed(() => {
        const selectedIds = new Set(selectedCollectionProjectIds.value.map((id) => Number(id)));
        if (!selectedIds.size) return reviewPackageRows.value;
        return reviewPackageRows.value.filter((row) => selectedIds.has(Number(row.collection_project_id)));
      });
      function clearReviewPackageReturnHighlightTimer() {
        if (reviewPackageReturnHighlightTimer == null) return;
        window.clearTimeout(reviewPackageReturnHighlightTimer);
        reviewPackageReturnHighlightTimer = null;
      }
      function scheduleReviewPackageReturnHighlightClear() {
        clearReviewPackageReturnHighlightTimer();
        if (reviewPackageReturnHighlightId.value == null) return;
        reviewPackageReturnHighlightTimer = window.setTimeout(() => {
          reviewPackageReturnHighlightId.value = null;
          reviewPackageReturnHighlightTimer = null;
        }, 5000);
      }
      function highlightReturnedReviewPackage(packageId) {
        const id = Number(packageId);
        if (!Number.isSafeInteger(id) || id <= 0) return;
        reviewPackageReturnHighlightId.value = id;
        void nextTick(scheduleReviewPackageReturnHighlightClear);
      }
      function reviewPackageRowClass({ row }) {
        return Number(row?.id) === Number(reviewPackageReturnHighlightId.value)
          ? 'review-package-return-highlight'
          : '';
      }
      async function changeReviewPackagePage(page) {
        reviewPackagePage.value = page;
        await loadReviewPackages();
      }
      function resetReviewPackagePage() {
        reviewPackagePage.value = 1;
        return loadReviewPackages();
      }
      let reviewPackagesGeneration = 0;
      async function loadReviewPackages() {
        const generation = ++reviewPackagesGeneration, workspaceId = selectedWorkspaceId.value;
        const page = reviewPackagePage.value;
        const projectScopeKey = selectedCollectionProjectIds.value.map(String).sort().join(',');
        const isCurrent = () => generation === reviewPackagesGeneration
          && String(workspaceId) === String(selectedWorkspaceId.value)
          && projectScopeKey === selectedCollectionProjectIds.value.map(String).sort().join(',');
        reviewPackageRows.value = [];
        reviewPackageSelectedIds.value = [];
        if (!workspaceId) return;
        reviewPackageLoading.value = true;
        try {
          const params = { workspace_id: workspaceId, page, size: 50 };
          if (reviewPackageStatus.value) params.status = reviewPackageStatus.value;
          if (selectedCollectionProjectIds.value.length) params.collection_project_id = selectedCollectionProjectIds.value;
          const res = await QuicDataAPI.listDataPackages(params);
          if (isCurrent()) {
            reviewPackageRows.value = (res?.items || []).map((pkg, index) => ({ ...pkg, seq: pkg.seq || (page - 1) * 50 + index + 1 }));
            reviewPackageTotal.value = res.total ?? reviewPackageRows.value.length;
            if (reviewPackageRows.value.some((row) => Number(row.id) === Number(reviewPackageReturnHighlightId.value))) {
              void nextTick(scheduleReviewPackageReturnHighlightClear);
            }
          }
        } catch (error) { if (isCurrent()) errorMessage(error); }
        finally { if (isCurrent()) reviewPackageLoading.value = false; }
      }
      function onReviewPackageSelectionChange(rows) {
        reviewPackageSelectedIds.value = (rows || []).map((row) => row.id);
      }
      function canReviewPackageRow(row) {
        // Mirrors review_data_package_intake: only a pending review (or an
        // already reviewed package) can receive an intake verdict.
        const status = row?.status;
        return status === 'pending_intake_review' && !row?.intake_review;
      }
      async function bulkApproveReviewPackages() {
        const ids = reviewPackageSelectedIds.value.slice();
        if (!ids.length) return;
        try {
          await ElMessageBox.confirm(
            t('bulkApproveConfirm', { count: ids.length }),
            t('intakeReviewTitle'),
            { confirmButtonText: t('intakeReviewApprove'), cancelButtonText: t('cancel'), type: 'success' },
          );
          const res = await QuicDataAPI.bulkApproveIntakePackages({
            workspace_id: selectedWorkspaceId.value,
            data_package_ids: ids,
          });
          ElMessage.success(t('bulkApproveSuccess', { count: res?.approved_count || ids.length }));
          reviewPackageSelectedIds.value = [];
          await loadReviewPackages();
        } catch (error) {
          if (error !== 'cancel') errorMessage(error);
        }
      }

      function isManifestUnavailable(pkg) {
        if (!pkg) return true;
        const s = pkg.raw_status || pkg.status;
        return s === 'pending_assignment' || s === 'voided' || s === 'planned';
      }

      function openIntakeReview(row) {
        const id = row?.id || packageDetail.value?.id;
        if (!id) return;
        intakeReviewPackageId.value = id;
        rejectedEpisodeIds.value = [];
        dataPackageDrawerVisible.value = false;
        navigate('intake-review');
      }
      async function loadIntakeReviewPackage(id) {
        intakeReviewLoading.value = true;
        try {
          const res = await QuicDataAPI.getDataPackage(id, selectedWorkspaceId.value);
          intakeReviewPackage.value = res;
        } catch (error) {
          errorMessage(error);
        } finally {
          intakeReviewLoading.value = false;
        }
      }
      async function openIntakeEpisodePreview(episode) {
        const pkg = intakeReviewPackage.value;
        if (!pkg?.id || !episode?.id || !episode.preview_available) return;
        intakePreviewEpisode.value = episode;
        intakePreviewStreams.value = [];
        intakePreviewError.value = '';
        intakePreviewDialogVisible.value = true;
        intakePreviewLoading.value = true;
        try {
          const res = await QuicDataAPI.getDataPackageEpisodePreviewUrls(
            pkg.id,
            episode.id,
            selectedWorkspaceId.value,
          );
          const streams = Array.isArray(res?.streams) ? res.streams : [];
          if (!streams.length) throw new Error(t('previewUnavailable'));
          intakePreviewStreams.value = streams;
        } catch (error) {
          intakePreviewError.value = error instanceof Error
            ? error.message
            : String(error || t('previewUnavailable'));
        } finally {
          intakePreviewLoading.value = false;
        }
      }
      function closeIntakeEpisodePreview() {
        intakePreviewDialogVisible.value = false;
        intakePreviewEpisode.value = null;
        intakePreviewStreams.value = [];
        intakePreviewError.value = '';
      }
      function toggleEpisodeRejected(episode) {
        const ids = rejectedEpisodeIds.value.slice();
        const index = ids.indexOf(episode.id);
        if (index >= 0) ids.splice(index, 1);
        else ids.push(episode.id);
        rejectedEpisodeIds.value = ids;
      }
      function admissionReasonLabel(reason) {
        const map = {
          admission_fact_missing: t('reasonMissingFact'),
          integrity_failed: t('reasonIntegrity'),
          preview_failed: t('reasonPreview'),
          preview_unavailable: t('reasonPreview'),
          output_failed: t('reasonOutput'),
          source_fingerprint_missing: t('reasonFingerprint'),
          source_fingerprint_changed: t('reasonFingerprint'),
          validation_policy_outdated: t('reasonPolicy'),
          episode_intake_rejected: t('reasonHumanRejected'),
        };
        for (const [prefix, label] of Object.entries(map)) {
          if (String(reason || '').startsWith(prefix)) return label;
        }
        return reason || '—';
      }
      async function submitIntakeApprove() {
        const pkg = intakeReviewPackage.value;
        if (!pkg?.id) return;
        const rejectedCount = rejectedEpisodeIds.value.length;
        try {
          await ElMessageBox.confirm(
            t('intakeApprovePreview', { rejected: rejectedCount }),
            t('intakeReviewTitle'),
            { confirmButtonText: t('intakeReviewApprove'), cancelButtonText: t('cancel'), type: 'success' },
          );
          await QuicDataAPI.reviewIntakePackage(pkg.id, {
            workspace_id: selectedWorkspaceId.value,
            verdict: 'approved',
            rejected_episode_ids: rejectedEpisodeIds.value,
          });
          ElMessage.success(t('intakeApproveSuccess'));
          await loadIntakeReviewPackage(pkg.id);
          await loadReviewPackages();
        } catch (err) {
          if (err !== 'cancel') errorMessage(err);
        }
      }
      async function backFromIntakeReview() {
        const packageId = intakeReviewPackageId.value || intakeReviewPackage.value?.id;
        closeIntakeEpisodePreview();
        await navigate('batches');
        if (activeView.value !== 'batches') return;
        intakeReviewPackage.value = null;
        rejectedEpisodeIds.value = [];
        highlightReturnedReviewPackage(packageId);
      }
      const intakeReviewCounts = computed(() => (
        intakeReviewPackage.value?.admission_counts || { ready: 0, running: 0, failed: 0, reviewed: 0 }
      ));
      function intakeReviewRowClass({ row }) {
        return row?.privacy_sensitive ? 'privacy-sensitive-row' : '';
      }

      async function openDataPackageDrawer(row) {
        selectedPackageRow.value = row;
        packageDetail.value = row;
        packageDetailError.value = '';
        dataPackageDrawerVisible.value = true;
        await refreshPackageDetail();
      }

      async function refreshPackageDetail() {
        const packageId = selectedPackageRow.value?.id || packageDetail.value?.id;
        const workspaceId = selectedWorkspaceId.value, generation = ++packageDetailGeneration;
        const isCurrent = () => generation === packageDetailGeneration && workspaceId === selectedWorkspaceId.value && Number(selectedPackageRow.value?.id) === Number(packageId) && dataPackageDrawerVisible.value;
        if (!packageId || !workspaceId || demoMode.value) return;
        packageDetailLoading.value = true;
        packageDetailError.value = '';
        try {
          const res = await QuicDataAPI.getDataPackage(packageId, workspaceId);
          if (isCurrent()) packageDetail.value = { ...selectedPackageRow.value, ...res };
        } catch (error) {
          if (isCurrent()) packageDetailError.value = error?.message || t('loadFailed');
        } finally {
          if (isCurrent()) packageDetailLoading.value = false;
        }
      }

      function closeMiningInlinePackageDetail() {
        miningInlinePackageDetailGeneration += 1;
        miningInlinePreviewGeneration += 1;
        miningInlinePackageDetailId.value = null;
        miningInlinePackageDetail.value = null;
        miningInlinePackageDetailError.value = '';
        miningInlinePackageDetailLoading.value = false;
        miningInlinePreviewIndex.value = 0;
        miningInlinePreviewStreams.value = [];
        miningInlinePreviewStreamIndex.value = 0;
        miningInlinePreviewLoading.value = false;
        miningInlinePreviewError.value = '';
        miningInlineExtraInfoVisible.value = false;
      }

      async function openMiningInlinePackageDetail(row) {
        if (!row?.id) return;
        if (String(miningInlinePackageDetailId.value) === String(row.id)) {
          closeMiningInlinePackageDetail();
          return;
        }
        miningInlinePackageDetailId.value = row.id;
        miningInlinePackageDetail.value = row;
        miningInlinePackageDetailError.value = '';
        miningInlineExtraInfoVisible.value = false;
        await refreshMiningInlinePackageDetail();
      }

      async function refreshMiningInlinePackageDetail() {
        const packageId = miningInlinePackageDetailId.value;
        const workspaceId = selectedWorkspaceId.value;
        const generation = ++miningInlinePackageDetailGeneration;
        const isCurrent = () => generation === miningInlinePackageDetailGeneration
          && String(workspaceId) === String(selectedWorkspaceId.value)
          && String(packageId) === String(miningInlinePackageDetailId.value);
        if (!packageId || !workspaceId) return;
        miningInlinePackageDetailLoading.value = true;
        miningInlinePackageDetailError.value = '';
        try {
          const res = await QuicDataAPI.getDataPackage(packageId, workspaceId);
          if (isCurrent()) {
            miningInlinePackageDetail.value = { ...miningInlinePackageDetail.value, ...res };
            await loadMiningInlineEpisodePreview(0);
          }
        } catch (error) {
          if (isCurrent()) miningInlinePackageDetailError.value = error?.message || t('loadFailed');
        } finally {
          if (isCurrent()) miningInlinePackageDetailLoading.value = false;
        }
      }

      function normalizeMiningInlinePreviewStreams(payload) {
        const streams = Array.isArray(payload?.streams) ? payload.streams : (Array.isArray(payload) ? payload : []);
        return streams.map((stream, index) => {
          const value = typeof stream === 'string' ? { video_url: stream } : (stream || {});
          return {
            ...value,
            url: value.video_url || value.preview_url || value.stream_url || value.url || '',
            label: value.topic || value.name || value.camera || `Stream ${index + 1}`,
          };
        }).filter((stream) => typeof stream.url === 'string' && stream.url.trim());
      }

      async function loadMiningInlineEpisodePreview(index) {
        const episodes = miningInlinePreviewEpisodes.value;
        if (!episodes.length) {
          miningInlinePreviewGeneration += 1;
          miningInlinePreviewIndex.value = 0;
          miningInlinePreviewStreams.value = [];
          miningInlinePreviewLoading.value = false;
          miningInlinePreviewError.value = t('noEpisodesInPackage');
          return;
        }
        const nextIndex = Math.max(0, Math.min(Number(index) || 0, episodes.length - 1));
        const episode = episodes[nextIndex];
        const packageId = miningInlinePackageDetailId.value;
        const workspaceId = selectedWorkspaceId.value;
        const generation = ++miningInlinePreviewGeneration;
        const isCurrent = () => generation === miningInlinePreviewGeneration
          && String(packageId) === String(miningInlinePackageDetailId.value)
          && String(episode?.id) === String(miningInlinePreviewEpisode.value?.id);
        miningInlinePreviewIndex.value = nextIndex;
        miningInlinePreviewStreams.value = [];
        miningInlinePreviewStreamIndex.value = 0;
        miningInlinePreviewLoading.value = false;
        miningInlinePreviewError.value = '';
        if (!episode?.id || episode.preview_available === false) {
          miningInlinePreviewError.value = t('previewUnavailable');
          return;
        }
        miningInlinePreviewLoading.value = true;
        try {
          const res = await QuicDataAPI.getDataPackageEpisodePreviewUrls(packageId, episode.id, workspaceId);
          const streams = normalizeMiningInlinePreviewStreams(res);
          if (!streams.length) throw new Error(t('previewUnavailable'));
          if (isCurrent()) miningInlinePreviewStreams.value = streams;
        } catch (error) {
          if (isCurrent()) miningInlinePreviewError.value = error instanceof Error ? error.message : String(error || t('previewUnavailable'));
        } finally {
          if (isCurrent()) miningInlinePreviewLoading.value = false;
        }
      }

      function selectMiningInlineEpisodeVideo(episode) {
        const index = miningInlinePreviewEpisodes.value.findIndex((item) => String(item.id) === String(episode?.id));
        if (index >= 0) void loadMiningInlineEpisodePreview(index);
      }

      function prevMiningInlineEpisodeVideo() {
        if (miningInlinePreviewIndex.value > 0) void loadMiningInlineEpisodePreview(miningInlinePreviewIndex.value - 1);
      }

      function nextMiningInlineEpisodeVideo() {
        if (miningInlinePreviewIndex.value < miningInlinePreviewEpisodes.value.length - 1) {
          void loadMiningInlineEpisodePreview(miningInlinePreviewIndex.value + 1);
        }
      }

      async function downloadPackageOfflineManifest(pkg, format = 'csv') {
        const packageId = pkg?.id || selectedPackageRow.value?.id;
        if (!packageId) return;
        if (isManifestUnavailable(pkg || selectedPackageRow.value)) {
          ElMessage.warning(t('manifestUnavailableHint'));
          return;
        }
        if (demoMode.value) {
          const manifest = {
            schema_version: 1,
            package_uid: pkg?.package_uid || `pkg-${packageId}`,
            workspace_id: 1,
            collection_project_id: 1,
            collection_task_id: Number(miningSelectedTaskId.value) || 1,
            target_duration_hours: '2.00',
            responsible_collector_id: 10,
            operator_collector_id: 10,
            device_model_id: 1,
            assigned_at: new Date().toISOString(),
          };
          const filename = `offline_manifest_${manifest.package_uid}.${format}`;
          if (format === 'csv') {
            const headers = ['schema_version', 'package_uid', 'workspace_id', 'collection_project_id', 'collection_task_id', 'target_duration_hours', 'responsible_collector_id', 'operator_collector_id', 'device_model_id', 'assigned_at'];
            downloadCsvFile(filename, headers, [manifest]);
          } else {
            downloadJsonFile(filename, manifest);
          }
          ElMessage.success(t('manifestDownloadSuccess'));
          return;
        }
        try {
          const manifest = await QuicDataAPI.getOfflineManifest(packageId, selectedWorkspaceId.value);
          const filename = `offline_manifest_${manifest.package_uid || packageId}.${format}`;
          if (format === 'csv') {
            const headers = ['schema_version', 'package_uid', 'workspace_id', 'collection_project_id', 'collection_task_id', 'target_duration_hours', 'responsible_collector_id', 'operator_collector_id', 'device_model_id', 'assigned_at'];
            downloadCsvFile(filename, headers, [manifest]);
          } else {
            downloadJsonFile(filename, manifest);
          }
          ElMessage.success(t('manifestDownloadSuccess'));
        } catch (err) {
          ElMessage.error(err?.message || t('manifestDownloadFailed'));
        }
      }

      function handlePackageRowCommand(command, row) {
        if (command === 'csv') {
          downloadPackageOfflineManifest(row, 'csv');
        } else if (command === 'json') {
          downloadPackageOfflineManifest(row, 'json');
        }
      }

      // 数采审核 is the data package review queue now, so scope it to the
      // package project and open that package once the queue is loaded.
      function openPackageIntakeReview(row) {
        if (!row) return;
        const projectName = row?.tags?.project || row?.project_name || '';
        if (projectName) miningProjectScope.value = [projectName];
        showMiningTaskPackagesPage.value = false;
        openIntakeReview(row);
      }

      async function downloadTaskOfflineManifest(task, format = 'csv') {
        const targetTask = task || miningSelectedTask.value;
        const workspaceId = selectedWorkspaceId.value;
        if (!targetTask?.id || !workspaceId) return;
        try {
          const manifest = await QuicDataAPI.getTaskOfflineManifest(targetTask.id, workspaceId);
          if (String(selectedWorkspaceId.value) !== String(workspaceId)) return;
          if (!Array.isArray(manifest?.items)) throw new Error(t('manifestDownloadFailed'));
          if (!manifest.items.length) {
            ElMessage.warning(t('manifestUnavailableHint'));
            return;
          }
          const safeName = String(targetTask.name || targetTask.id).replace(/[/\\?%*:|"<>]/g, '_');
          const filename = `task_manifest_${targetTask.id}_${safeName}.${format}`;
          if (format === 'csv') {
            downloadCsvFile(filename, [
              'package_uid', 'collection_project', 'collection_task', 'modality',
              'target_duration_hours', 'expected_files', 'required_metadata',
              'upload_status', 'desensitization_status', 'manifest_revision',
            ], manifest.items);
          } else {
            downloadJsonFile(filename, manifest);
          }
          ElMessage.success(t('manifestDownloadSuccess'));
        } catch (err) {
          if (String(selectedWorkspaceId.value) === String(workspaceId)) {
            ElMessage.error(err?.message || t('manifestDownloadFailed'));
          }
        }
      }

      function handleTaskRowCommand(command, task) {
        if (command === 'csv') {
          downloadTaskOfflineManifest(task, 'csv');
        } else if (command === 'json') {
          downloadTaskOfflineManifest(task, 'json');
        }
      }

      async function createPackageSupplement(pkg) {
        if (!canCreatePackageSupplement(pkg)) return;
        let sourcePackage = pkg;
        try {
          if (!sourcePackage.intake_review) {
            sourcePackage = await QuicDataAPI.getDataPackage(pkg.id, selectedWorkspaceId.value);
          }
          if (!canCreatePackageSupplement(sourcePackage) || !sourcePackage.intake_review) {
            ElMessage.warning(locale.value === 'en-US' ? 'A supplemental package can only be created after the final intake review.' : '数据包完成入库审核后才可创建补采包');
            return;
          }
          const hours = await ElMessageBox.prompt(locale.value === 'en-US' ? 'Supplemental target in hours. The task target will not increase.' : '填写本次补采目标时长（小时），不会增加任务目标', locale.value === 'en-US' ? 'Create supplemental package' : '创建补采包', { inputPattern: /^(?:[1-9]\d*(?:\.\d{1,2})?|0\.(?:0[1-9]|[1-9]\d?))$/, inputErrorMessage: locale.value === 'en-US' ? 'Enter positive hours with up to two decimals.' : '请输入大于零、最多两位小数的时长' });
          const reason = await ElMessageBox.prompt(locale.value === 'en-US' ? 'Enter the collection retry reason.' : '填写补采原因', locale.value === 'en-US' ? 'Create supplemental package' : '创建补采包', { inputValidator: v => !!v?.trim() || (locale.value === 'en-US' ? 'A reason is required.' : '请填写原因') });
          const request = { workspace_id: Number(selectedWorkspaceId.value), target_duration_hours: hours.value, reason: reason.value.trim(), client_request_id: crypto.randomUUID() };
          const created = await QuicDataAPI.createSupplementPackage(sourcePackage.id, request);
          ElMessage.success((locale.value === 'en-US' ? 'Created supplemental package awaiting assignment: ' : '已创建待分配补采包 ') + created.package_uid);
          if (activeView.value === 'batches') await loadReviewPackages();
          await loadMiningData();
          if (miningSelectedTaskId.value) await selectMiningTask(miningSelectedTaskId.value);
        } catch (error) { if (error !== 'cancel' && error !== 'close') errorMessage(error); }
      }

      function canCreatePackageSupplement(pkg) {
        // Any final intake verdict allows a linked supplement package; a rejected package is voided.
        if (!pkg?.id) return false;
        if (pkg.intake_review) return true;
        const status = pkg.raw_status || pkg.status;
        return status === 'intake_approved' || status === 'batched';
      }

      function canReviewPackage(pkg) {
        if (!pkg) return false;
        const s = pkg.raw_status || pkg.status;
        return s === 'pending_intake_review' && !pkg.intake_review;
      }

      async function approvePackageIntake(pkg) {
        const target = pkg || packageDetail.value;
        if (!target?.id) return;
        try {
          await ElMessageBox.confirm(
            t('intakeApproveConfirm'),
            t('intakeReviewTitle'),
            { confirmButtonText: t('intakeReviewApprove'), cancelButtonText: t('cancel'), type: 'success' }
          );
          await QuicDataAPI.reviewIntakePackage(target.id, {
            workspace_id: selectedWorkspaceId.value,
            verdict: 'approved',
          });
          ElMessage.success(t('intakeApproveSuccess'));
          await refreshPackageDetail();
          await loadMiningData();
          await loadDataBatchCandidates();
          await loadReviewPackages();
        } catch (err) {
          if (err !== 'cancel') {
            ElMessage.error(err?.message || '审核失败');
          }
        }
      }

      function openRejectPackageDialog(pkg) {
        if (pkg?.id) {
          selectedPackageRow.value = pkg;
          packageDetail.value = pkg;
        }
        rejectReason.value = '';
        rejectDialogVisible.value = true;
      }

      async function submitRejectPackageIntake() {
        const targetId = packageDetail.value?.id || selectedPackageRow.value?.id;
        if (!targetId) return;
        const reason = rejectReason.value.trim();
        if (!reason) {
          ElMessage.warning(t('rejectReasonRequired'));
          return;
        }
        rejectSubmitting.value = true;
        try {
          await QuicDataAPI.reviewIntakePackage(targetId, {
            workspace_id: selectedWorkspaceId.value,
            verdict: 'rejected',
            reason,
          });
          rejectDialogVisible.value = false;
          ElMessage.success(t('intakeRejectSuccess'));
          await refreshPackageDetail();
          if (Number(intakeReviewPackageId.value) === Number(targetId)) {
            await loadIntakeReviewPackage(targetId);
          }
          await loadMiningData();
          await loadDataBatchCandidates();
          await loadReviewPackages();
        } catch (err) {
          ElMessage.error(err?.message || '驳回失败');
        } finally {
          rejectSubmitting.value = false;
        }
      }

      function handleMiningPackageSelectionChange(selection) {
        selectedMiningPackages.value = selection;
      }

      function canSelectPackageForApproval(row) {
        const s = row.raw_status || row.status;
        return ['pending_intake_review', 'ingested', 'parsing', 'uploading'].includes(s);
      }

      async function bulkApproveSelectedPackages() {
        if (!selectedMiningPackages.value.length) return;
        const count = selectedMiningPackages.value.length;
        try {
          await ElMessageBox.confirm(
            t('bulkApproveConfirm', { count }),
            t('bulkApproveIntake'),
            { confirmButtonText: t('confirm'), cancelButtonText: t('cancel'), type: 'info' }
          );
          if (demoMode.value) {
            for (const p of selectedMiningPackages.value) {
              p.raw_status = 'intake_approved';
              p.status = 'done';
            }
            ElMessage.success(t('bulkApproveSuccess', { count }));
            selectedMiningPackages.value = [];
            return;
          }
          const res = await QuicDataAPI.bulkApproveIntakePackages({
            workspace_id: selectedWorkspaceId.value,
            data_package_ids: selectedMiningPackages.value.map((p) => p.id),
          });
          ElMessage.success(t('bulkApproveSuccess', { count: res?.approved_count || count }));
          selectedMiningPackages.value = [];
          await loadMiningData();
          await loadDataBatchCandidates();
        } catch (err) {
          if (err !== 'cancel') {
            ElMessage.error(err?.message || '批量审核失败');
          }
        }
      }

      const miningDetailBatch = ref(null);

      function openMiningBatchDetail(row) {
        if (row?.seq) miningDetailBatch.value = row;
        openDataPackageDrawer(row);
      }

      const batchDetailFilterText = ref('');
      const batchDetailFilters = ref([]);

      function onBatchDetailFilterChange(filters) {
        const seqFilter = filters.find((filter) => filter.attr === 'seq');
        if (seqFilter) {
          const seq = String(seqFilter.value).replace('#', '');
          const target = (batches.value || []).find((batch) => String(batch.sequence_number) === seq);
          if (target && String(target.sequence_number) !== String(selectedBatch.value?.sequence_number ?? '')) {
            void openBatchWithMiningMock(target);
            return;
          }
        }
        batchDetailFilters.value = filters;
      }

      const batchDetailFilterValueMap = computed(() => {
        const map = {};
        for (const attr of miningBatchFilterAttrs.value) {
          const values = new Set();
          for (const task of miningTasks.value || []) {
            for (const batch of task.batches || []) {
              const value = miningBatchFilterValueOf(batch, attr.key);
              if (value && value !== '—') values.add(value);
            }
          }
          map[attr.key] = Array.from(values);
        }
        return map;
      });

      const batchCutSelectedId = ref('');
      const batchCutPlayTime = ref(0);
      const batchCutVideoEl = ref(null);

      const batchCutEpisodeRows = computed(() => {
        const mining = miningBatchOfSelectedBatch.value;
        if (!mining) {
          const batch = selectedBatch.value;
          if (!batch) return [];
          const taskId = '9' + String(batch.id || 0).padStart(3, '0');
          return QuicDataMining.listBatchCutEpisodes(taskId, String(batch.id), batch.name || '');
        }
        const seeded = (miningCutEpisodes.value || []).filter((row) => String(row.batch) === `批次 #${mining.seq}`);
        if (seeded.length) return seeded;
        const taskId = String(mining.id || '').split('-')[0];
        return QuicDataMining.listBatchCutEpisodes(taskId, mining.id, mining.task_label || '');
      });

      const batchCutSelected = computed(() => {
        const rows = batchCutEpisodeRows.value;
        return rows.find((row) => String(row.id) === String(batchCutSelectedId.value)) || rows[0] || null;
      });

      const batchCutDetailTab = ref('overview');

      const batchCutMetrics = computed(() => {
        const row = batchCutSelected.value;
        if (!row) return { frames: 0, rgbRate: '—' };
        const seed = String(row.episode_uid || row.id || '').split('').reduce((acc, ch) => acc + ch.charCodeAt(0), 0);
        return { frames: 24000 + (seed % 4000), rgbRate: (14 + (seed % 3)) + '.00 Hz' };
      });

      const batchCutLifecycle = computed(() => {
        const row = batchCutSelected.value;
        if (!row) return { created: '—', updated: '—' };
        const seed = String(row.episode_uid || row.id || '').split('').reduce((acc, ch) => acc + ch.charCodeAt(0), 0);
        const base = new Date('2026-08-11T00:50:00Z');
        base.setMinutes(base.getMinutes() + (seed % 600));
        const updated = new Date(base.getTime() + 9 * 3600 * 1000);
        return { created: formatDate(base.toISOString()), updated: formatDate(updated.toISOString()) };
      });

      const batchCutArtifacts = computed(() => {
        const row = batchCutSelected.value;
        if (!row) return [];
        return [
          { type: 'process_preview', role: 'process', size: '437.2 MB', retention: 'permanent', until: '—' },
          { type: 'raw_source', role: 'raw', size: '6.2 GB', retention: 'permanent', until: '—' },
        ];
      });

      const batchCutPacketMetadata = computed(() => {
        const row = batchCutSelected.value;
        if (!row) return null;
        const uid = String(row.episode_uid || '');
        const seed = uid.split('').reduce((acc, ch) => acc + ch.charCodeAt(0), 0);
        const duration = Number(row.duration_s) || 300;
        const start = new Date('2026-08-08T18:17:00Z');
        start.setMinutes(start.getMinutes() + (seed % 500));
        const end = new Date(start.getTime() + duration * 1000);
        return {
          qrdf_version: '0.2.0',
          episode_id: 'episode_20260808T101739Z_' + uid.toLowerCase(),
          data_file: 'data.mcap',
          task: { name: 'iphone_ego', language: 'iphone ego capture' },
          capture: { mode: 'iphone_ego', episode_type: 'human_ego_demo', app: 'quic_ego', app_version: '1.0' },
          reference_topic: '/camera/head/rgb',
          timing: {
            start_timestamp_ns: String(start.getTime() * 1e6),
            end_timestamp_ns: String(end.getTime() * 1e6),
            duration_s: duration,
          },
          metrics: { reference_frame_count: batchCutMetrics.value.frames, average_rgb_rate_hz: 14 + (seed % 3) },
          cameras: [{ topic: '/camera/head/rgb', width: 960, height: 720, fps: 15 }],
          sensors: [{ topic: '/sensor/head/imu', rate: '100 Hz' }],
        };
      });

      const batchCutRawSourceLoaded = ref(false);

      function loadBatchCutRawSource() {
        batchCutRawSourceLoaded.value = true;
      }

      const batchSummaryDurations = computed(() => {
        const batch = selectedBatch.value;
        const mining = miningBatchOfSelectedBatch.value;
        const collected = Number(batch?.source_duration_s ?? mining?.source_duration_s) || 0;
        const raw = Number(mining?.raw) || Number(batch?.source_episode_count) || 0;
        const valid = Number(mining?.valid) || 0;
        const validDuration = raw > 0 ? Math.round(collected * (valid / raw)) : 0;
        return { collected: formatDuration(collected), valid: formatDuration(validDuration) };
      });

      const batchCutSummary = computed(() => QuicDataMining.cutSummary(batchCutSelected.value));
      const batchCutActiveSegment = computed(() => QuicDataMining.activeCutSegment(batchCutSelected.value, batchCutPlayTime.value));

      function selectBatchCutEpisode(row) {
        if (!row?.id) return;
        batchCutSelectedId.value = String(row.id);
        batchCutPlayTime.value = 0;
      }

      const batchCutDrawerVisible = ref(false);

      const logicalSplitStates = ['split_done', 'split_running', 'split_pending'];
      const logicalAnnotateStates = ['annotate_done', 'annotate_running', 'annotate_todo'];
      const logicalReviewStates = ['review_passed', 'review_pending', 'review_rejected'];

      function logicalEpisodeStateOf(uid, kind) {
        const seed = String(uid || '').split('').reduce((acc, ch) => acc + ch.charCodeAt(0), 0);
        return (kind === 'split' ? logicalSplitStates : logicalAnnotateStates)[seed % 3];
      }

      function logicalEpisodeReviewOf(uid, kind) {
        const seed = String(uid || '').split('').reduce((acc, ch) => acc + ch.charCodeAt(0), 0) + (kind === 'split_review' ? 1 : 2);
        const stageDone = kind === 'split_review'
          ? logicalEpisodeStateOf(uid, 'split') === 'split_done'
          : logicalEpisodeStateOf(uid, 'annotate') === 'annotate_done';
        return stageDone ? logicalReviewStates[seed % 3] : 'review_none';
      }

      const logicalPreviewPool = ['https://vjs.zencdn.net/v/oceans.mp4'];

      const logicalEpisodeRows = computed(() => {
        const cutRows = (miningCutRows.value || []).map((row) => ({ ...row }));
        const known = new Set(cutRows.map((row) => row.episode_uid));
        const fromQueue = (queueRows.value || []).filter((row) => row.episode && !known.has(row.episode.episode_uid)).map((row, index) => ({
          id: 'queue-' + row.episode.episode_uid,
          episode_uid: row.episode.episode_uid,
          batch: row.episode.kind === 'derived' ? '派生数据' : '原始数据',
          task_name: row.episode.task_label?.name || '',
          collector: '—',
          device: '—',
          duration_s: Number(row.metrics?.duration_s) || 0,
          segments: [],
          preview_url: logicalPreviewPool[index % logicalPreviewPool.length],
        }));
        return cutRows.concat(fromQueue).map((row) => Object.assign(row, {
          splitState: logicalEpisodeStateOf(row.episode_uid, 'split'),
          annotateState: logicalEpisodeStateOf(row.episode_uid, 'annotate'),
          splitReview: logicalEpisodeReviewOf(row.episode_uid, 'split_review'),
          annotateReview: logicalEpisodeReviewOf(row.episode_uid, 'annotate_review'),
        }));
      });

      function logicalStateLabel(state) {
        if (state === 'split_done' || state === 'annotate_done') return t('qcDone');
        if (state === 'split_running') return t('logicalSplitRunning');
        if (state === 'split_pending') return t('logicalSplitPending');
        if (state === 'annotate_running') return t('logicalAnnotateRunning');
        return t('logicalAnnotateTodo');
      }

      function logicalStateTagType(state) {
        if (state === 'split_done' || state === 'annotate_done') return 'success';
        if (state === 'split_running' || state === 'annotate_running') return 'warning';
        return 'info';
      }

      function logicalReviewLabel(state) {
        if (state === 'review_passed') return t('logicalReviewPassed');
        if (state === 'review_pending') return t('logicalReviewPending');
        if (state === 'review_rejected') return t('logicalReviewRejected');
        return '—';
      }

      function logicalReviewTagType(state) {
        if (state === 'review_passed') return 'success';
        if (state === 'review_pending') return 'warning';
        if (state === 'review_rejected') return 'danger';
        return 'info';
      }

      function onBatchImportRowClick(row) {
        const rows = batchCutEpisodeRows.value;
        if (!rows.length) return;
        const index = Math.max(0, batchImports.value.indexOf(row)) % rows.length;
        selectBatchCutEpisode(rows[index]);
        batchCutDrawerVisible.value = true;
      }

      function playBatchCutSegment(segment) {
        const player = batchCutVideoEl.value;
        if (!player) return;
        player.currentTime = Number(segment?.start_s) || 0;
        const played = player.play?.();
        if (played && typeof played.catch === 'function') played.catch(() => {});
      }

      function onBatchCutTimeUpdate() {
        batchCutPlayTime.value = Number(batchCutVideoEl.value?.currentTime) || 0;
      }

      function batchCutSegmentClass(segment) {
        return {
          'is-active': batchCutActiveSegment.value?.id === segment.id,
          'is-invalid': segment.valid === false,
        };
      }

      function autoSplitSelectedBatch() {
        const mining = miningBatchOfSelectedBatch.value;
        if (!mining || !miningSelectedTaskId.value) return;
        QuicDataMining.simulateBatchStageRun(miningSelectedTaskId.value, mining.id, 'split', () => {
          loadMiningData();
          loadMiningCuts();
          ElMessage.success(t('splitDoneToast'));
        });
      }

      function enterSplitWorkbench() {
        navigate('work-queue', { queueStageOverride: 'cut' });
      }

      function approveSelectedBatchSplit() {
        const mining = miningBatchOfSelectedBatch.value;
        if (!mining || !miningSelectedTaskId.value) return;
        QuicDataMining.reviewTaskBatchSplit(miningSelectedTaskId.value, mining.id, true);
        loadMiningData();
        loadMiningCuts();
        ElMessage.success(t('cutAuditDone'));
      }

      function rejectSelectedBatchSplit() {
        const mining = miningBatchOfSelectedBatch.value;
        if (!mining || !miningSelectedTaskId.value) return;
        QuicDataMining.reviewTaskBatchSplit(miningSelectedTaskId.value, mining.id, false);
        loadMiningData();
        loadMiningCuts();
        ElMessage.success(t('actRerunSplit'));
      }

      function importMiningBatchData(row) {
        const target = (batches.value || []).find((batch) => String(batch.sequence_number) === String(row?.seq)) || (batches.value || [])[0];
        if (!target) {
          ElMessage.warning(t('intakeChooseBatchFirst'));
          return;
        }
        openImportDialog(target);
      }

      function openImportCutViewer(row) {
        const label = `批次 #${selectedBatch.value?.sequence_number ?? ''}`;
        const exists = QuicDataMining.listCutEpisodes().some((episode) => episode.batch === label);
        miningCutAudit.value = null;
        miningCutBatch.value = exists ? label : '';
        navigate('miningCuts');
        loadMiningCuts();
      }

      function runMiningQuickAction(command) {
        if (command === 'task') openMiningTaskDialog();
        if (command === 'split') openMiningSplitDialog();
      }

      function miningSplitDone(batch) {
        const status = stageOf(batch, 'split').status;
        return status === 'succeeded' || status === 'pending_review';
      }

      function miningSplitStatusText(batch) {
        return miningSplitDone(batch) ? t('splitStatusDone') : t('splitStatusTodo');
      }

      function miningQcStatus(batch) {
        const total = Number(batch?.raw) || Number(batch?.target) || 0;
        const done = Number(batch?.checked) || 0;
        if (total > 0 && done >= total) return { done: true, doneCount: done, total };
        return { done: false, doneCount: done, total };
      }

      const consolidateEpisodePool = ref([
        { uid: 'EGO-2026-0811-0042', task: '拿起水杯并放入托盘', duration_s: 125, captured_at: '2026-08-11', tags: ['家居 · 厨房'], quality: 'passed', desensitized: true },
        { uid: 'EGO-2026-0811-0058', task: '拿起水杯并放入托盘', duration_s: 96, captured_at: '2026-08-11', tags: ['家居 · 厨房'], quality: 'passed', desensitized: true },
        { uid: 'EGO-2026-0813-0071', task: '拿起水杯并放入托盘', duration_s: 150, captured_at: '2026-08-13', tags: ['家居 · 客厅'], quality: 'passed', desensitized: true },
        { uid: 'DRV-2026-0811-0042-A', task: '拿起水杯并放入托盘', duration_s: 30, captured_at: '2026-08-11', tags: ['家居 · 厨房'], quality: 'passed', desensitized: true },
        { uid: 'EGO-2026-0812-0063', task: '拿起水杯并放入托盘', duration_s: 88, captured_at: '2026-08-12', tags: ['家居 · 客厅'], quality: 'passed', desensitized: true },
        { uid: 'EGO-2026-0820-0071', task: '水杯与托盘动作补采', duration_s: 110, captured_at: '2026-08-20', tags: ['仓储 · 分拣台'], quality: 'passed', desensitized: true },
        { uid: 'EGO-2026-0820-0085', task: '水杯与托盘动作补采', duration_s: 74, captured_at: '2026-08-20', tags: ['仓储 · 分拣台'], quality: 'passed', desensitized: true },
        { uid: 'TELEOP-2026-0816-0009', task: '遥操作 · 双臂装配场景', duration_s: 210, captured_at: '2026-08-16', tags: ['仓储 · 分拣台'], quality: 'passed', desensitized: true },
        { uid: 'TELEOP-2026-0816-0012', task: '遥操作 · 双臂装配场景', duration_s: 165, captured_at: '2026-08-16', tags: ['仓储 · 货架'], quality: 'passed', desensitized: true },
        { uid: 'EGO-2026-0821-0093', task: '水杯与托盘动作补采', duration_s: 58, captured_at: '2026-08-21', tags: ['仓储 · 货架'], quality: 'passed', desensitized: true },
        { uid: 'EGO-2026-0822-0101', task: '遥操作 · 双臂装配场景', duration_s: 132, captured_at: '2026-08-22', tags: ['家居 · 客厅'], quality: 'passed', desensitized: true },
        { uid: 'EGO-2026-0823-0110', task: '拿起水杯并放入托盘', duration_s: 99, captured_at: '2026-08-23', tags: ['家居 · 厨房'], quality: 'passed', desensitized: true },
      ]);

      const consolidatedDatasets = ref([]);
      const episodeSplitVersions = ref({});
      const showConsolidateDrawer = ref(false);
      const consolidateForm = reactive({ name: '', task: '', minDuration: '', maxDuration: '' });
      const consolidateDateRange = ref([]);
      const consolidateSelected = ref([]);
      const episodeBuildMode = ref(false);
      const episodeBuildSelection = ref([]);
      const buildEpisodeTableRef = ref(null);

      function onEpisodeBuildSelectionChange(rows) {
        episodeBuildSelection.value = rows;
      }

      function submitEpisodeBuild() {
        const count = episodeBuildSelection.value.length;
        ElMessage.success(String(t('buildSubmitted', { count })).replace('{count}', String(count)));
        episodeBuildSelection.value = [];
        buildEpisodeTableRef.value?.clearSelection?.();
        episodeBuildMode.value = false;
      }
      const consolidateTableRef = ref(null);
      const splitManageDataset = ref(null);
      const showSplitManageDrawer = ref(false);
      const showEpisodeSplitDialog = ref(false);
      const episodeSplitTarget = ref(null);
      const episodeSplitChoice = ref('new');

      const consolidateTaskOptions = computed(() => Array.from(new Set(consolidateEpisodePool.value.map((row) => row.task))));
      const consolidateFilteredRows = computed(() => consolidateEpisodePool.value.filter((row) => {
        if (consolidateForm.task && row.task !== consolidateForm.task) return false;
        const min = Number(consolidateForm.minDuration);
        if (consolidateForm.minDuration !== '' && Number.isFinite(min) && row.duration_s < min) return false;
        const max = Number(consolidateForm.maxDuration);
        if (consolidateForm.maxDuration !== '' && Number.isFinite(max) && row.duration_s > max) return false;
        const range = Array.isArray(consolidateDateRange.value) ? consolidateDateRange.value : [];
        if (range[0] && row.captured_at < range[0]) return false;
        if (range[1] && row.captured_at > range[1]) return false;
        return true;
      }));

      function onConsolidateSelectionChange(rows) {
        consolidateSelected.value = rows;
      }

      function submitConsolidate() {
        const name = consolidateForm.name.trim();
        if (!name) {
          ElMessage.warning(t('consolidateNamePlaceholder'));
          return;
        }
        if (!consolidateSelected.value.length) {
          ElMessage.warning(t('consolidateSelectedCount', { count: 0 }));
          return;
        }
        consolidatedDatasets.value = [{
          id: `cd-${Date.now()}`,
          name,
          episodes: consolidateSelected.value.map((row) => row.uid),
          created_at: new Date().toISOString(),
        }, ...consolidatedDatasets.value];
        consolidateForm.name = '';
        consolidateSelected.value = [];
        consolidateTableRef.value?.clearSelection?.();
        showConsolidateDrawer.value = false;
        ElMessage.success(`${t('consolidateAction')}：${name}`);
      }

      function removeConsolidated(dataset) {
        consolidatedDatasets.value = consolidatedDatasets.value.filter((item) => item.id !== dataset.id);
      }

      function mockDatasetAction(dataset, action) {
        ElMessage.success(`${dataset.name} · ${action === 'annotate' ? t('annotateAction') : t('trainAction')}`);
      }

      function datasetSplitStyleCount(dataset) {
        const styles = new Set();
        for (const uid of dataset.episodes || []) {
          for (const version of episodeSplitVersions.value[uid] || []) {
            styles.add(version.mode === 'reused' ? `reuse:${version.reused_from}` : `new:${version.id}`);
          }
        }
        return styles.size;
      }

      function openSplitManage(dataset) {
        splitManageDataset.value = dataset;
        showSplitManageDrawer.value = true;
      }

      function episodeSplitVersionsOf(uid) {
        return episodeSplitVersions.value[uid] || [];
      }

      function datasetEpisodesOf(dataset) {
        return consolidateEpisodePool.value.filter((row) => (dataset?.episodes || []).includes(row.uid));
      }

      const splitManageEpisodeRows = computed(() => datasetEpisodesOf(splitManageDataset.value));

      const showEpisodeSplitDialogVisible = computed(() => showEpisodeSplitDialog.value);
      const episodeSplitReuseOptions = computed(() => {
        const episode = episodeSplitTarget.value;
        return episode ? episodeSplitVersionsOf(episode.uid) : [];
      });

      function openEpisodeSplitDialog(episode) {
        episodeSplitTarget.value = episode;
        episodeSplitChoice.value = 'new';
        showEpisodeSplitDialog.value = true;
      }

      function confirmEpisodeSplit() {
        const episode = episodeSplitTarget.value;
        const dataset = splitManageDataset.value;
        if (!episode) return;
        const list = episodeSplitVersions.value[episode.uid] || (episodeSplitVersions.value[episode.uid] = []);
        if (episodeSplitChoice.value === 'new') {
          const version = list.length + 1;
          list.push({
            id: `${episode.uid}-v${version}-${list.length + 1}`,
            dataset_name: dataset?.name || '',
            version,
            mode: 'new',
            segment_count: 3 + (list.length % 3),
            created_at: new Date().toISOString(),
          });
        } else {
          const reuseId = String(episodeSplitChoice.value).replace('reuse:', '');
          const source = list.find((item) => item.id === reuseId);
          const version = list.length + 1;
          list.push({
            id: `${episode.uid}-v${version}-${list.length + 1}`,
            dataset_name: dataset?.name || '',
            version,
            mode: 'reused',
            reused_from: source ? `${source.dataset_name} v${source.version}` : '',
            segment_count: source ? source.segment_count : 3,
            created_at: new Date().toISOString(),
          });
        }
        showEpisodeSplitDialog.value = false;
        ElMessage.success(t('splitDoneToast'));
      }

      function miningBatchDurationPair(batch) {
        const collected = Number(batch?.source_duration_s) || 0;
        const raw = Number(batch?.raw) || 0;
        const valid = Number(batch?.valid) || 0;
        const validDuration = raw > 0 ? Math.round(collected * (valid / raw)) : 0;
        return formatDuration(collected) + ' / ' + formatDuration(validDuration);
      }

      function miningBatchCollectedDurationText(batch) {
        if (batch?.captured_duration_hours != null && batch?.captured_duration_hours !== '') {
          const h = Number(batch.captured_duration_hours);
          return Number.isFinite(h) && h > 0 ? h.toFixed(1) + ' ' + t('dashHour') : '—';
        }
        if (batch?.source_duration_s != null && Number(batch.source_duration_s) > 0) {
          return formatDuration(Number(batch.source_duration_s));
        }
        return '—';
      }

      function miningBatchValidDurationText(batch) {
        if (batch?.intake_valid_duration_hours != null && batch?.intake_valid_duration_hours !== '') {
          const h = Number(batch.intake_valid_duration_hours);
          return Number.isFinite(h) && h > 0 ? h.toFixed(1) + ' ' + t('dashHour') : '—';
        }
        if (batch?.valid_duration_hours != null && batch?.valid_duration_hours !== '') {
          const h = Number(batch.valid_duration_hours);
          return Number.isFinite(h) && h > 0 ? h.toFixed(1) + ' ' + t('dashHour') : '—';
        }
        const collected = Number(batch?.source_duration_s) || 0;
        const raw = Number(batch?.raw) || 0;
        const valid = Number(batch?.valid) || 0;
        const validDuration = raw > 0 ? Math.round(collected * (valid / raw)) : 0;
        return validDuration > 0 ? formatDuration(validDuration) : '—';
      }

      function miningBatchReviewStatus(batch) {
        if (batch?.raw_status) {
          const s = batch.raw_status;
          if (s === 'intake_approved') return { label: t('packageStatusIntakeApproved'), type: 'success' };
          if (s === 'voided') return { label: t('packageStatusVoided'), type: 'info' };
          if (s === 'pending_intake_review' || s === 'ingested') return { label: t('packageStatusPendingReview'), type: 'warning' };
          if (s === 'uploading' || s === 'parsing') return { label: t('packageStatusParsing'), type: 'warning' };
          if (s === 'assigned' || s === 'pending_upload') return { label: t('packageStatusAssigned'), type: 'primary' };
          if (s === 'pending_assignment') return { label: t('packageStatusPendingAssignment'), type: 'info' };
          if (s === 'batched') return { label: t('packageStatusBatched'), type: 'success' };
          if (s === 'parse_failed') return { label: t('packageStatusParseFailed'), type: 'danger' };
        }
        const status = stageOf(batch, 'split').status;
        if (status === 'pending_review') return { label: t('batchReviewPending'), type: 'warning' };
        if (status === 'succeeded') return { label: t('batchReviewApproved'), type: 'success' };
        if (status === 'rejected') return { label: t('batchReviewRejected'), type: 'danger' };
        return { label: t('batchReviewNotSubmitted'), type: 'info' };
      }

      function miningBatchUploadEndText(batch) {
        return batch?.upload_completed_at ? formatDate(batch.upload_completed_at) : '—';
      }

      // Collection period spans the first and last package capture start; no capture facts, no period.
      function miningTaskCollectionPeriodText(task) {
        if (!task?.captured_started_from) return '—';
        const start = formatDate(task.captured_started_from).slice(0, 10);
        const end = formatDate(task.captured_started_to || task.captured_started_from).slice(0, 10);
        return start === end ? start : `${start} ~ ${end}`;
      }

      function miningBatchCollectionTimeText(batch) {
        return batch?.captured_started_at ? formatDate(batch.captured_started_at) : '—';
      }

      function miningBatchUnassigned(batch) {
        return !(Array.isArray(batch?.assignees) && batch.assignees.length);
      }

      function miningImportStateOf(row) {
        const explicit = String(row?.import_state || '').toLowerCase();
        if (['imported', 'succeeded'].includes(explicit)) return 'imported';
        if (['importing', 'running', 'parsing', 'analyzing'].includes(explicit)) return 'importing';
        if (['todo', 'queued', 'pending', 'created'].includes(explicit)) return 'todo';
        const status = String(row?.status || (typeof importDisplayStatus === 'function' ? importDisplayStatus(row) : '') || '').toLowerCase();
        if (['succeeded', 'imported', 'completed', 'done'].includes(status)) return 'imported';
        if (['running', 'parsing', 'analyzing', 'uploading'].includes(status)) return 'importing';
        if (['queued', 'pending', 'created', 'planned'].includes(status)) return 'todo';
        return '';
      }

      function miningImportStateLabel(row) {
        const state = miningImportStateOf(row);
        if (state === 'imported') return t('importStateImported');
        if (state === 'importing') return t('importStateImporting');
        if (state === 'todo') return t('importStateTodo');
        return '—';
      }

      function miningImportStateType(row) {
        const state = miningImportStateOf(row);
        if (state === 'imported') return 'success';
        if (state === 'importing') return 'warning';
        if (state === 'todo') return 'info';
        return 'info';
      }

      function miningStageProgressText(batch, key) {
        const total = Number(batch?.raw) || Number(batch?.target) || 0;
        if (!total) return '';
        const stage = stageOf(batch, key);
        if (stage.status === 'succeeded' || stage.status === 'pending_review') return `${total}/${total}`;
        const done = Math.min(total, Math.round((total * (Number(stage.progress) || 0)) / 100));
        return `${done}/${total}`;
      }

      function miningStageSummaryText(batch, key) {
        if (!batch) return '—';
        const stage = stageOf(batch, key);
        const total = Number(batch?.raw) || Number(batch?.target) || 0;
        if (!total) return miningStageStatusLabel(stage.status);
        if (stage.status === 'succeeded' || stage.status === 'pending_review') return `${miningStageStatusLabel(stage.status)} ${total}/${total}`;
        const done = Math.round((total * (Number(stage.progress) || 0)) / 100);
        return `${miningStageStatusLabel(stage.status)} ${done}/${total}`;
      }

      function miningQcSummaryText(batch) {
        if (!batch) return '—';
        const status = miningQcStatus(batch);
        return `${status.done ? t('qcDone') : t('qcUnfinished')} ${status.doneCount}/${status.total}`;
      }

      const miningBatchOfSelectedBatch = computed(() => {
        const seq = miningDetailBatch.value ? miningDetailBatch.value.seq : selectedBatch.value?.sequence_number;
        return findMiningBatchBySeq(seq);
      });

      function findMiningBatchBySeq(seq) {
        const target = String(seq ?? '');
        if (!target) return null;
        for (const task of miningTasks.value || []) {
          const found = (task.batches || []).find((batch) => String(batch.seq) === target);
          if (found) return found;
        }
        const idx = (batches.value || []).findIndex((batch) => String(batch.sequence_number) === target);
        if (idx >= 0 && miningSelectedTask.value && (miningSelectedTask.value.batches || [])[idx]) {
          return miningSelectedTask.value.batches[idx];
        }
        return null;
      }

      const miningBatchSplitView = computed(() => {
        const batch = miningBatchOfSelectedBatch.value;
        if (!batch) return null;
        const stage = stageOf(batch, 'split');
        const total = Number(batch.source_episode_count) || 0;
        if (!total) return null;
        const status = stage.status;
        const progress = Math.max(0, Math.min(100, Number(stage.progress) || 0));
        const unfinished = Math.max(0, total - Math.round((total * progress) / 100));
        if (status === 'running') return { label: t('splitRunningLabel'), tagType: 'warning', showProgress: true, progress, unfinished, total, canSplit: false, reviewable: false, resplit: false };
        if (status === 'manual') return { label: t('splitChoiceManual'), tagType: 'warning', showProgress: false, progress: 0, unfinished: total, total, canSplit: true, reviewable: false, resplit: true };
        if (status === 'failed') return { label: t('splitFailedLabel'), tagType: 'danger', showProgress: false, progress: 0, unfinished, total, canSplit: true, reviewable: false, resplit: true };
        if (status === 'pending_review') return { label: t('splitReviewPending'), tagType: 'success', showProgress: false, progress: 0, unfinished, total, canSplit: true, reviewable: true, resplit: true };
        if (status === 'succeeded') return { label: t('splitStatusDone'), tagType: 'success', showProgress: false, progress: 100, unfinished, total, canSplit: true, reviewable: true, resplit: true };
        return { label: t('splitStatusTodo'), tagType: 'info', showProgress: false, progress: 0, unfinished: total, total, canSplit: true, reviewable: false, resplit: false };
      });

      const miningBatchSplitSummaryText = computed(() => {
        const view = miningBatchSplitView.value;
        if (!view) return t('splitStatusTodo');
        const rest = view.unfinished ? `；${t('splitUnfinished')} ${view.unfinished}/${view.total}` : '';
        if (view.showProgress) return `${view.label} ${view.progress}%${rest}`;
        return `${view.label}${rest}`;
      });

      function openMiningSplitChoiceDialog(batch) {
        if (!batch?.id) return;
        miningSplitTargetBatchId.value = String(batch.id);
        miningSplitChoice.value = 'auto';
        showMiningSplitChoiceDialog.value = true;
      }

      function confirmMiningSplitChoice() {
        const batchId = miningSplitTargetBatchId.value;
        if (!miningSelectedTaskId.value || !batchId) return;
        showMiningSplitChoiceDialog.value = false;
        if (miningSplitChoice.value === 'auto') {
          ElMessage.success(t('miningStageStarted'));
          QuicDataMining.simulateBatchStageRun(miningSelectedTaskId.value, batchId, 'split', () => {
            loadMiningData();
            ElMessage.success(t('splitDoneToast'));
          });
          return;
        }
        QuicDataMining.setTaskBatchStage(miningSelectedTaskId.value, batchId, 'split', { status: 'manual', progress: 0 });
        loadMiningData();
        ElMessage.success(t('splitManualToast'));
      }

      function openMiningManualQc() {
        navigate('work-queue', { queueStageOverride: 'review' });
      }

      const miningCutTaskOptions = computed(() => Array.from(new Set(miningCutEpisodes.value.map((row) => row.task_name).filter(Boolean))));
      const miningCutRows = computed(() => {
        const audit = miningCutAudit.value;
        const filters = audit
          ? { keyword: miningCutKeyword.value || '' }
          : {
              task_name: miningCutTask.value || '',
              batch: miningCutBatch.value || '',
              keyword: miningCutKeyword.value || '',
              project: miningCutProject.value || '',
              scene: miningCutScene.value || '',
              purpose: miningCutPurpose.value || '',
              train: miningCutTrain.value || '',
            };
        return QuicDataMining.filterCutEpisodes(miningCutEpisodes.value, filters);
      });
      const miningCutBatchOptions = computed(() => Array.from(new Set(miningCutEpisodes.value.map((row) => row.batch).filter(Boolean))));
      const miningCutSelected = computed(() => QuicDataMining.findCutEpisode(miningCutSelectedId.value));
      const miningCutSummary = computed(() => QuicDataMining.cutSummary(miningCutSelected.value));
      const miningCutActiveSegment = computed(() => QuicDataMining.activeCutSegment(miningCutSelected.value, cutPlayTime.value));

      function cutSummaryOf(row) {
        return QuicDataMining.cutSummary(row);
      }

      function selectCutEpisode(row) {
        if (!row?.id) return;
        miningCutSelectedId.value = String(row.id);
        cutPlayTime.value = 0;
      }

      function playCutSegment(segment) {
        const player = cutVideoEl.value;
        if (!player) return;
        player.currentTime = Number(segment?.start_s) || 0;
        const played = player.play?.();
        if (played && typeof played.catch === 'function') played.catch(() => {});
      }

      function onCutTimeUpdate() {
        cutPlayTime.value = Number(cutVideoEl.value?.currentTime) || 0;
      }

      function cutRangeLabel(segment) {
        return QuicDataMining.formatCutRange(segment);
      }

      function cutSegmentDurationLabel(segment) {
        return formatDuration(Math.max(0, Number(segment?.end_s) - Number(segment?.start_s)));
      }

      function cutSegmentClass(segment) {
        return {
          'is-active': miningCutActiveSegment.value?.id === segment.id,
          'is-invalid': segment.valid === false,
        };
      }

      function cutValidLabel(segment) {
        return segment?.valid === false ? t('cutInvalid') : t('cutValid');
      }

      function cutValidType(segment) {
        return segment?.valid === false ? 'danger' : 'success';
      }

      return {
        ready, user, sessionRestoring, locale, activeView, sidebarCollapsed, workbenchQueueOpen, loading, workspaces, taskSets, batches, episodes, episodeTotal, episodePage, episodePageSize, queueRows, queueTotal, queuePage, queuePageSize, batchImports, batchActivity, demoMode, localPreviewAvailable,
        availableScopeWorkspaces, managementWorkspaces, managementWorkspaceId, managementMembers, taskLabels, managedUsers, roleDefinitions, workspaceMembers, platformSettings, assignableRoles, availableWorkspaceMembers, selectedWorkspaceId, selectedTaskSetId, selectedBatch, selectedEpisode, episodePreview, episodeDrawerContext, episodeDrawerTab, episodeTechnicalSections, episodeDrawerLoading, episodeDrawerError, assetGroups, assetRows, nativeLerobotSessions, nativeLerobotSession, nativeLerobotSnapshot, nativeLerobotScanCandidates, nativeLerobotSessionCandidateIds, nativeLerobotBatchDatasets,
        workbenchRoute, activeWorkbench, workbenchTimeline, workbenchDraft, workbenchDraftDirty, workbenchVideo, cutTimelineTrack, cutTimelineCanvas, cutPlayheadLine, cutPlayheadPin, timelineTimecode, timelineScrollViewport, cutListViewport, annotationListViewport, timelineHoverPreview, timelineHoverLabel, cutLocalTimelineTrack, cutPlayheadTimestamp, selectedCutBoundary, activeCutSegmentIndex, activeAnnotationSegmentIndex, annotationDetailOpen, reviewDetailOpen, workbenchPlaying, workbenchMuted, workbenchVolume, workbenchPlaybackRate, timelineSnapEnabled, workbenchKind, workbenchTitle, collectorProfiles, collectionDevices, aiSuggestions, selectedAiTopic,
        timelineZoom, timelinePanelHeight, workbenchEditorWidth, timelineHover, timelineHoverVideo, visibleCutList, visibleAnnotationList, visibleCutTrackWindows, visibleCutTrackBoundaries, canUndoWorkbench, canRedoWorkbench, canPasteWorkbench, userDisplayName, userInitial,
        batchStatus, episodeModality, episodeTaskLabelId, assetKind, assetReviewStatus, assetPublicationStatus, assetKeyword, assetCollectorId, assetDeviceId, assetPublishedRange, assetSortBy, assetSortOrder, queueTaskSetId, queueTaskLabelId, queueStatus, queueEpisodeKeyword, queueReviewTargetKind, queueUpdatedRange, queueSortBy, queueSortOrder, queueStage, queueStageOptions, activeAssetFilters, activeWorkQueueFilters, showWorkspaceDialog, showTaskSetDialog, showBatchDialog, showIntakeGuide, showTaskLabelDialog,
        queueDataMode, queueDataModeOptions, governanceQueueActive, switchQueueDataMode, selectedCollectionProjectId, selectedCollectionProjectIds, collectionProjectOptions, switchCollectionProject, switchCollectionProjects, loadCollectionProjectOptions,
        showCollectorDialog, showDeviceDialog, showEpisodeDrawer, showChangePassword, showApiTokens, apiTokens, apiTokensLoading, tokenForm, tokenSecret, tokenSecretVisible, showManagedUserDialog, showWorkspaceMemberDialog, showCollectionProjectDialog, editingCollectionProject, collectionProjectForm, savingCollectionProject, newLabelInputs, creatingLabel, collectorDialogMode, availableCollectors, selectedExistingCollectorId, loadingAvailableCollectors, saving, uploading, login, passwordForm, workspaceForm, taskSetForm, batchForm, taskLabelForm, collectorForm, managedUserForm, workspaceMemberForm, reviewForm,
        currentWorkspace, currentTaskSet, overviewCounts, mustChangePassword, visibleViews, consoleAccessAvailable, canManageWorkspace, canCreateBatch, canImport, canManageTaskLabels, canManageDatasets, canExportDatasets, canManageUsers, canViewDashboard, collectorProfileFormValid,
        dashboardOverview, dashboardOverviewError, dashboardTodayQueues, funnelChartEl, trendChartEl, deviceChartEl,
        t, toggleLocale, hasPermission, canView, roleLabel, sidebarIcons, toggleSidebar, toggleWorkbenchQueueNav, navigate, openIntakeGuide, switchWorkspace, switchTaskSet, switchQueueStage, openQueueStage, changeEpisodePage, changeEpisodePageSize, reloadEpisodesFromFirstPage, clearAssetFilters, clearAssetFilter, changeAssetSort, changeWorkQueuePage, changeWorkQueuePageSize, reloadWorkQueueFromFirstPage, scheduleWorkQueueEpisodeSearch, submitWorkQueueEpisodeSearch, clearWorkQueueFilters, clearWorkQueueFilter, signIn, enterLocalDemo, signOut, openChangePassword, cancelPasswordChange, submitPasswordChange, openApiTokens, cancelApiTokens, loadApiTokens, createApiToken, rotateApiToken, revokeApiToken, copyTokenSecret, apiTokenStatus, handleAccountCommand, loadEpisodes, loadWorkQueue, loadCollectionResources, onCollectorDialogClosed, onDeviceDialogClosed,
        loadDashboardOverview, refreshDashboardData, formatDelta, formatPercent, dashboardMetricValue, dashboardPercent, dashboardDelta, formatDashboardDuration, formatDashboardTime,
        loadDataBatchCandidates, loadDataBatches, loadCollectionLabels, loadAnnotationWorkItems, loadReviewWorkItems, openReassignDialog, submitReassignDialog, openPackageWorkbench, backFromPackageWorkbench, packageWorkbenchRoute, packageAnnotationWorkbenchRef, viewRequiresWorkspace, scopeWorkspaceRequired, stageSnapshotLabel, assigneeLabel,
        batchCandidates, collectionLabels, annotationWorkItems, reviewWorkItems, reassignDialog, sceneLabelOptions, purposeLabelOptions, modalityLabelOptions, trainLabelOptions, annotatorUserOptions, reviewerUserOptions, governanceAnnotationQueueRows, annotationQueueRows, reviewQueueRows, reviewFilterState, reviewFilteredRows, governanceFilteredRows, reviewFilterActive, clearReviewFilters, reviewSourceTag,
        createWorkspace, createTaskSet, createTaskLabel, createCollectorProfile, openCollectorDialog, loadAvailableCollectors, handleCollectorDialogSubmit, handleRevokeCollector, openDeviceDialog, createCollectionDevice, setCollectorProfileActive, setCollectionDeviceActive, closeCollectorDialog, resetCollectorDialogState, closeDeviceDialog, resetDeviceDialogState, taskLabelName,         openEpisodeDetail, openWorkQueueEpisodeDetail, inspectBatchSource, closeEpisodeDetail, retryEpisodeDetail, runWorkAction, runWorkActionById, openWorkbench, enterWorkbench, enterWorkbenchById, loadWorkbench, saveWorkbenchDraft, submitWorkbenchDraft, submitReviewDecision, releaseWorkbench,
        requestAiSuggestion, retryAiSuggestion, applyAiSuggestion,
        loadAdmin, loadWorkspaceMembers, loadManagementWorkspaceMembers, openManagedUserDialog, openWorkspaceMemberDialog, createManagedUser, changeManagedUserRole, setManagedUserActive, grantWorkspaceMember, revokeWorkspaceMember,
        loadPlatformSettings, collectionProjects, settingsProjectRows, adminActiveTab, settingsActiveTab, loadingSettingsCenter, settingsCenterError, settingsCenterWritable, collectionProjectCreateWritable, loadSettingsCenterData, openCreateCollectionProjectDialog, handleSettingsCreateCommand, openEditCollectionProjectDialog, saveCollectionProject, resetCollectionProjectDialogState, onCollectionProjectDialogClosed, handleArchiveCollectionProject, handleCreateCollectionLabel, handleDeactivateCollectionLabel, scopeWorkspaceName, scopeTaskSetName, externalProjectStatus,
        qualityStatusLabel, qualityStatusType, qualityDiagnosticLabel, qualityProgressPercent, importStatusLabel, importStatusType, importDisplayStatus, importProgressText, scanStatusLabel, scanStatusType, batchStatusLabel, sourceLabel, batchTypeLabel, batchSourceLabel, collectorDisplayLabel, collectorWithJobId, collectorChoiceLabel, collectionDeviceChoiceLabel, collectorAttributionLabel, collectionDeviceAttributionLabel, attributionSourceLabel, outcomeLabel,
        activityKindLabel, activitySummary, activityStatusLabel, activityStatusType, exclusionReasonLabel,
        workActionLabel, workItemKindLabel, workItemStatusLabel, queueStageLabel, publicationJobStatusLabel, episodePublicationLabel, assetKindLabel, humanStageLabel, humanStageType, reviewStatusLabel, reviewStatusType, publicationCategoryLabel, publicationCategoryType, assetRootSummary, assetLineageIssueLabel, assetRangeLabel, assetGroupExpanded, toggleAssetGroup, assetRowClass, openAssetRow, exportStatusLabel, exportStatusType, formatNumber, formatDuration, formatAvailableDuration, formatBytes, formatDate, formatPacketTimestamp, packetMetadataRows, hasPacketMetadata, cameraResolutionLabel, formatRelativeTimestamp,
        addAnnotationSegment, addCutBoundary, deleteSelectedCutBoundary, restoreSelectedQrBoundary, useWholeEpisodeCut, selectCutSegment, selectAnnotationSegment, toggleAnnotationDetail, selectReviewSegment, toggleReviewDetail, setCutEligibility, setCutExclusionReason, removeWorkbenchSegment, setCutBoundary, markWorkbenchDraftDirty, undoWorkbench, redoWorkbench, copyWorkbenchSelection, pasteWorkbenchSelection,
        exactPlaybackTimelineAvailable, syncWorkbenchPlayhead, toggleWorkbenchPlayback, toggleWorkbenchMute, setWorkbenchVolume, setWorkbenchPlaybackRate, stepWorkbenchPlayback, openWorkbenchFullscreen, seekCutTimeline, seekWorkbenchFromTimelineEvent, startPlayheadDrag, startCutBoundaryDrag, startCutTrackPointer, startCutLocalBoundaryPointer, startAnnotationBoundaryDrag, startAnnotationSegmentDrag, selectCutBoundary, cutSegmentStyle, cutBoundaryStyle, cutPlayheadStyle, cutBoundaryOrigin, cutSegmentBoundaryLabel, cutWindowDuration, cutSegmentTooLong, cutDraftSaveValid, cutDraftSubmitValid, annotationDraftSaveValid, annotationDraftSubmitValid,
        selectNearestCutBoundary, setTimelineZoom, zoomWorkbenchTimeline, timelineCanvasStyle, timelineTicks, timelineIcon, handleTimelineWheel, updateTimelineHover, clearTimelineHover, onTimelineScroll, onCutListScroll, onAnnotationListScroll, startTimelineResize, startWorkbenchSplitResize,
        canEnterWorkbench, visibleQueueActions, queuePreviewStatus, previewStatusLabel, previewStatusType, queueRowKey, queueActionKey, aiStatusType,
        lerobotCandidates, nativeLerobotReauthorizationCandidate, selectedNativeLerobotDataset, selectedNativeLerobotBundle, selectedCollectorForQr, tableLayoutVersion, rawSourceDownloads, nativeLerobotActionLoading, showCollectorQrDialog, showNativeLerobotDialog, showNativeLerobotReauthorizationDialog, showShortcutHelp,
        collectorQrCards, workbenchShortcutCommands, formatDateFull, tableColumnWidth, onTableHeaderDrag, nativeCopyStatusLabel, nativeCopyStatusType, nativeCopyErrorLabel, nativeCopySummary, nativeLerobotCanDeliver, nativeLerobotNeedsReauthorization, nativeLerobotReauthorizationCandidates, nativeLerobotCandidateLabel, nativeLerobotBundleActionLabel,
        nativeLerobotSessionCandidateSelected, toggleNativeLerobotSessionCandidate, batchContentSummary, batchAssociationSummary, openCollectorQrDialog, downloadCollectorQr, printCollectorQrCodes, openNativeLerobotDataset, copyNativeLerobotOssUri, retryNativeLerobotCopy, openNativeLerobotSourceReauthorization, reauthorizeNativeLerobotSource, requestNativeLerobotBundle, downloadNativeLerobotBundle, archiveNativeLerobotDataset, loadRawSourceDownloads, downloadRawSourceFile,
        candidateCollectorAttribution, candidateCollectorAttributionLabel, candidateCollectorAttributionType, candidateCollectorAttributionTitle,
        productVersion, workQueueWorkspaceId, workQueueWorkspaceOptions, switchWorkQueueWorkspace,
        miningNavOpen, miningTasks, miningTaskTableRef, miningSelectedTaskId, miningTaskRows, filteredMiningTaskRows, miningTaskFilter, resetMiningTaskFilter, miningProjectScope, miningProjectOptions, miningTaskNameOptions, miningTaskOwnerOptions, miningOwnerCandidates, miningTaskPurposeOptions, miningTaskSceneOptions, miningTaskTrainOptions, miningTaskModalityOptions, validRateBucketOptions, miningBatchDetailFilter, resetMiningBatchDetailFilter, miningBatchNameOptions, miningBatchModalityOptions, miningBatchCollectorOptions, miningBatchDeviceOptions, miningSelectedTask, miningProgress, miningKpiStats, miningStageProgressText, miningFailureRows, miningPipelines, miningPipelineRows, miningTransfers, miningTransferSummary,
        miningBatchFilterText, miningBatchFilters, miningBatchFilterAttrs, miningBatchFilterValueMap, miningBatchRowsFiltered, miningAssigneeCollectors, miningAssigneeDevices, miningAssigneeLines, openMiningManualQc, miningBatchUnassigned, miningStageSummaryText, miningQcSummaryText, miningImportStateLabel, miningImportStateType, miningBatchDurationPair, miningBatchTargetHoursText, miningBatchCollectedDurationText, miningBatchValidDurationText, miningBatchReviewStatus, miningBatchUploadEndText, miningTaskCollectionPeriodText, miningBatchCollectionTimeText, miningBatchOfSelectedBatch, batchDetailPageFilter, resetBatchDetailPageFilter, detailTaskOptions, detailBatchOptions, detailModalityOptions, onDetailTaskChange, onDetailBatchChange, filteredBatchDetailImports, openBatchImportDetail, reviewSelectedBatchDetail,
        consolidateEpisodePool, consolidatedDatasets, showConsolidateDrawer, consolidateForm, consolidateSelected, consolidateTableRef, consolidateTaskOptions, consolidateFilteredRows, onConsolidateSelectionChange, submitConsolidate, removeConsolidated, mockDatasetAction, datasetSplitStyleCount, openSplitManage, episodeSplitVersionsOf, datasetEpisodesOf, splitManageDataset, showSplitManageDrawer, splitManageEpisodeRows, showEpisodeSplitDialog, episodeSplitTarget, episodeSplitChoice, episodeSplitReuseOptions, openEpisodeSplitDialog, confirmEpisodeSplit, showEpisodeSplitDialogVisible,
        batchDetailFilterText, batchDetailFilters, batchDetailFilterValueMap, onBatchDetailFilterChange,
        batchCutEpisodeRows, batchCutSelected, batchCutSummary, batchCutActiveSegment, batchCutDetailTab, batchCutMetrics, batchCutLifecycle, batchCutArtifacts, batchCutPacketMetadata, batchCutRawSourceLoaded, loadBatchCutRawSource, batchSummaryDurations, batchCutVideoEl, selectBatchCutEpisode, onBatchImportRowClick, playBatchCutSegment, onBatchCutTimeUpdate, batchCutSegmentClass, autoSplitSelectedBatch, enterSplitWorkbench, approveSelectedBatchSplit, rejectSelectedBatchSplit,
        sidebarGroupOpen, toggleSidebarGroup, navigateDatasetsTab,
        dataOverviewRefreshKey, openPackageBatchDialog, intakeReviewWorkbenchRef,
        episodeBuildMode, episodeBuildSelection, buildEpisodeTableRef, onEpisodeBuildSelectionChange, submitEpisodeBuild, batchCutDrawerVisible, logicalEpisodeRows, logicalStateLabel, logicalStateTagType, logicalReviewLabel, logicalReviewTagType, intakeTab, overviewImportRows, overviewImportGroups, overviewImportTreeRows, allBatchesTreeRows, intakeTreeRows, intakeBatchGroupRows, intakeImportCount, overviewImportExpandedGroupIds, overviewImportStage, overviewImportRowField, overviewImportDeviceOf, overviewImportGroupDuration, overviewImportGroupRefFrames, overviewImportStageRow, overviewImportStageEnabled, overviewFilterState, overviewFilterActive, overviewFilterOptions, clearOverviewFilters, overviewImportTaskInfo, overviewImportAnnotateStatus, annotateStatusTagType, openImportDetail, hasImportGovernance, governanceReportVisible, governanceReportRow, governanceExpandedStages, openGovernanceReport, governanceFailureReason, governanceStageSummary, governanceStageLabel, governanceReportEntries, importGovernanceStatusLabel, stageItemCheckCounts, annotationItemProgress, annotationPageTab, annotationFilterState, annotationTaskRowsFiltered, annotationTaskOptions, annotationFiltersActive, clearAnnotationFilters, datasetFilterState, datasetFiltersActive, clearDatasetFilters, batchTagFilterState, batchTagFilterActive, clearBatchTagFilters, batchStageStatusOptions, batchAnnotateStatusOptions, overviewGovernanceStatus, assetListRowsFiltered, overviewBatchTagValue, importVideoCount, toggleOverviewImportGroup, overviewImportRowClass, builtImportBatches, overviewBuildMode, overviewBuildSelection, overviewBuildDialogVisible, overviewBuildForm, overviewBuildPrefix, isOverviewBuildGroupSelected, isOverviewBuildGroupIndeterminate, toggleOverviewBuildGroup, isOverviewBuildSelected, toggleOverviewBuildSelected, enterOverviewBuildMode, exitOverviewBuildMode, onOverviewBuildDialogClosed, openOverviewBuildDialog, submitOverviewBuild, overviewBuildSelectedInfo, governanceLabelList, viewImportDuration,         miningTaskSplitDone, miningTaskAssignDone, openMiningTaskSplit,
        showMiningTaskDialog, showMiningSplitDialog, showMiningAssignDialog, miningTaskForm, miningPackageCountPreview, miningSplitForm, miningSplitTotalHours, miningSplitPerBatchHours, miningAssignForm, openMiningTaskDialog, openMiningSplitDialog, resetMiningTaskDialogState, resetMiningSplitDialogState, resetMiningAssignDialogState, onMiningTaskDialogClosed, onMiningSplitDialogClosed, onMiningAssignDialogClosed,
        miningCutEpisodes, miningCutTask, miningCutKeyword, miningCutSelectedId, cutVideoEl, cutPlayTime, miningCutTaskOptions, miningCutRows,
        miningCutSelected, miningCutSummary, miningCutActiveSegment, miningRunningStage,
        miningStageDefs, miningBatchRows, miningDispatchStatus, dispatchMiningBatch, stageOf, miningStageStatusLabel, miningStageStatusType, miningStageAggregatedStatus, rerunOverviewStage,
        miningCutAudit, openMiningSplitReview, approveCutAudit, rerunCutAudit, restoreCutAudit, manualCutFromAudit, clearMiningCutAudit, runMiningQuickAction,
        openMiningAssignModeDialog, canAssignPackage,
        showMiningSplitChoiceDialog, miningSplitChoice, miningSplitDone, miningSplitStatusText, openMiningSplitChoiceDialog, confirmMiningSplitChoice, miningQcStatus,
        miningBatchOfSelectedBatch, miningBatchSplitView, miningBatchSplitSummaryText,
        openMiningBatchDetail, importMiningBatchData, openImportCutViewer, miningCutBatch, miningCutBatchOptions,
        reviewPackageReturnHighlightId, reviewPackageRowClass,
        reviewPackagePage, reviewPackageTotal, changeReviewPackagePage, resetReviewPackagePage, packageDetailError, reviewPackageRows, reviewPackageRowsFiltered, reviewPackageStatus, reviewPackageStatusOptions, reviewPackageSelectedIds, reviewPackageLoading, loadReviewPackages, onReviewPackageSelectionChange, canReviewPackageRow, bulkApproveReviewPackages, reviewPackageProjectName, reviewPackageTaskName, reviewPackageCollectorName, reviewPackageDeviceName, dataPackageStatusLabel, dataPackageStatusType, dataPackageDrawerVisible, selectedPackageRow, packageDetail, packageDetailLoading, miningInlinePackageDetailId, miningInlinePackageDetail, miningInlinePackageDetailLoading, miningInlinePackageDetailError, miningInlinePackageExpandedKeys, miningInlinePreviewEpisodes, miningInlinePreviewEpisode, miningInlinePreviewStreams, miningInlinePreviewStream, miningInlinePreviewStreamIndex, miningInlinePreviewIndex, miningInlinePreviewLoading, miningInlinePreviewError, miningInlineExtraInfoVisible, openMiningInlinePackageDetail, refreshMiningInlinePackageDetail, closeMiningInlinePackageDetail, loadMiningInlineEpisodePreview, selectMiningInlineEpisodeVideo, prevMiningInlineEpisodeVideo, nextMiningInlineEpisodeVideo, rejectDialogVisible, rejectReason, rejectSubmitting, selectedMiningPackages, isManifestUnavailable, openDataPackageDrawer, refreshPackageDetail, downloadPackageOfflineManifest, handlePackageRowCommand, openPackageIntakeReview, downloadTaskOfflineManifest, handleTaskRowCommand, canReviewPackage, canCreatePackageSupplement, createPackageSupplement, approvePackageIntake, openRejectPackageDialog, submitRejectPackageIntake, handleMiningPackageSelectionChange, canSelectPackageForApproval, bulkApproveSelectedPackages,
        intakeReviewPackageId, intakeReviewPackage, intakeReviewLoading, rejectedEpisodeIds, intakePreviewDialogVisible, intakePreviewEpisode, intakePreviewStreams, intakePreviewLoading, intakePreviewError, openIntakeReview, loadIntakeReviewPackage, openIntakeEpisodePreview, closeIntakeEpisodePreview, toggleEpisodeRejected, admissionReasonLabel, submitIntakeApprove, backFromIntakeReview, intakeReviewCounts, intakeReviewRowClass,
        showMiningTaskPackagesPage, miningTaskPackagesFocusMode, openMiningTaskPackagesPage, closeMiningTaskPackagesPage,
        miningCutProject, miningCutScene, miningCutPurpose, tagOf,
        miningDashTab, miningDashTrendRange, miningDashGranularity, miningDashMetricMode, miningDashFilters, miningDashRegions, miningDashTaskOptions, clearMiningDashFilters, miningDashDisableDate, miningDictionaries, collectionConfigForm, miningConfigTagInputs, miningCapacity, miningCollectionDash, miningEfficiency,
        loadMiningConfigData, dictItems, addMiningTag, removeMiningTag, saveMiningConfig, dashPercent,
        approveMiningSplit, rerunMiningSplit, restoreMiningSplit,
        cutSummaryOf, selectCutEpisode, playCutSegment, onCutTimeUpdate, cutRangeLabel, cutSegmentDurationLabel, cutSegmentClass, cutValidLabel, cutValidType,
        toggleMiningNav, loadMiningData, loadMiningCuts, selectMiningTask, createMiningTask, splitMiningTask, openMiningAssignDialog, submitMiningAssign, batchPreviewIndex, batchPreviewCurrent, prevBatchPreview, nextBatchPreview, retryMiningTransfer,
        miningBatchStatusLabel, miningBatchStatusType, miningPipelineStatusLabel, miningPipelineStatusType, miningTransferStatusLabel, miningTransferStatusType, miningSourceLabel, miningTaskStatusLabel, miningAssigneeLabel,
      };
    },
    template: `
      <main class="app-shell" v-loading.fullscreen.lock="!ready && !sessionRestoring">
        <section v-if="sessionRestoring" class="session-restore-view" :aria-label="t('restoringSession')" aria-live="polite">
          <div class="session-restore-mark">QS</div>
          <span class="session-restore-spinner" aria-hidden="true"></span>
          <p>{{ t('restoringSession') }}</p>
        </section>

        <section v-else-if="!user" class="login-view" aria-label="登录">
          <div class="login-copy"><strong>QuicStudio</strong><span>Batch / Episode</span><small v-if="productVersion" class="product-version">v{{ productVersion }}</small></div>
          <form class="login-form" @submit.prevent="signIn">
            <h1>{{ t('product') }}</h1>
            <div class="login-fields">
              <label class="login-field"><span>{{ t('email') }}</span><el-input v-model="login.email" type="email" :aria-label="t('email')" autocomplete="email" /></label>
              <label class="login-field"><span>{{ t('password') }}</span><el-input v-model="login.password" type="password" :aria-label="t('password')" show-password autocomplete="current-password" /></label>
            </div>
            <el-button native-type="submit" type="primary" :loading="saving" class="login-submit">{{ t('signIn') }}</el-button>
            <el-button v-if="localPreviewAvailable" native-type="button" class="demo-entry-button" @click="enterLocalDemo">{{ t('localDemo') }}</el-button>
          </form>
        </section>

        <section v-else-if="showChangePassword" class="login-view" aria-label="修改密码">
          <div class="login-copy"><strong>QuicStudio</strong><span>{{ mustChangePassword ? t('changePasswordRequired') : t('changePassword') }}</span></div>
          <form class="login-form" @submit.prevent="submitPasswordChange">
            <h1>{{ t('changePassword') }}</h1>
            <div class="login-fields">
              <label class="login-field"><span>{{ t('currentPassword') }}</span><el-input v-model="passwordForm.oldPassword" type="password" :aria-label="t('currentPassword')" show-password autocomplete="current-password" /></label>
              <label class="login-field"><span>{{ t('newPassword') }}</span><el-input v-model="passwordForm.newPassword" type="password" :aria-label="t('newPassword')" show-password autocomplete="new-password" /></label>
              <label class="login-field"><span>{{ t('confirmPassword') }}</span><el-input v-model="passwordForm.confirmPassword" type="password" :aria-label="t('confirmPassword')" show-password autocomplete="new-password" /></label>
            </div>
            <div class="form-actions"><el-button v-if="!mustChangePassword" @click="cancelPasswordChange">{{ t('backToConsole') }}</el-button><el-button native-type="submit" type="primary" :loading="saving">{{ t('changePassword') }}</el-button></div>
          </form>
        </section>

        <section v-else-if="showApiTokens" class="login-view" aria-label="API 令牌">
          <div class="login-copy"><strong>QuicStudio</strong><span>{{ t('apiTokens') }}</span></div>
          <section class="surface-panel table-panel" style="width: min(960px, 92vw); max-height: 78vh; overflow: auto;">
            <div class="panel-heading">
              <div><h2>{{ t('apiTokens') }}</h2></div>
              <div class="panel-actions">
                <el-button size="small" @click="cancelApiTokens">{{ t('backToConsole') }}</el-button>
              </div>
            </div>
            <div class="list-filter-bar">
              <el-input v-model="tokenForm.name" :placeholder="t('apiTokenName')" />
              <el-select v-model="tokenForm.expires_in_days">
                <el-option :label="'30 ' + t('daysUnit')" :value="30" />
                <el-option :label="'90 ' + t('daysUnit')" :value="90" />
                <el-option :label="'180 ' + t('daysUnit')" :value="180" />
                <el-option :label="'365 ' + t('daysUnit')" :value="365" />
                <el-option :label="t('apiTokenNever')" :value="3650" />
              </el-select>
              <el-button size="small" type="primary" @click="createApiToken">{{ t('apiTokenCreate') }}</el-button>
            </div>
            <div v-if="tokenSecretVisible && tokenSecret" class="drawer-section" style="padding: 16px;">
              <el-alert type="warning" :closable="false" show-icon :title="t('apiTokenOnce')" style="margin-bottom: 10px;" />
              <div style="display: flex; gap: 8px; align-items: center;">
                <code style="flex: 1; word-break: break-all; padding: 10px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px;">{{ tokenSecret }}</code>
                <el-button size="small" type="primary" @click="copyTokenSecret">{{ t('apiTokenCopy') }}</el-button>
              </div>
            </div>
            <el-table v-if="apiTokens.length" :data="apiTokens" class="data-table" size="small" v-loading="apiTokensLoading">
              <el-table-column prop="name" :label="t('apiTokenName')" min-width="150" />
              <el-table-column prop="key_id" label="key_id" width="120" />
              <el-table-column :label="t('apiTokenCreated')" width="150"><template #default="scope">{{ formatDate(scope.row.created_at) }}</template></el-table-column>
              <el-table-column :label="t('apiTokenExpires')" width="150"><template #default="scope">{{ scope.row.expires_at ? formatDate(scope.row.expires_at) : t('apiTokenNever') }}</template></el-table-column>
              <el-table-column :label="t('apiTokenLastUsed')" width="150"><template #default="scope">{{ scope.row.last_used_at ? formatDate(scope.row.last_used_at) : '—' }}</template></el-table-column>
              <el-table-column :label="t('status')" width="90"><template #default="scope"><el-tag size="small" :type="apiTokenStatus(scope.row).type">{{ apiTokenStatus(scope.row).label }}</el-tag></template></el-table-column>
              <el-table-column :label="t('operation')" width="160" fixed="right">
                <template #default="scope">
                  <el-button link type="primary" @click="rotateApiToken(scope.row)">{{ t('apiTokenRotate') }}</el-button>
                  <el-button plain size="small" type="danger" @click="revokeApiToken(scope.row)">{{ t('apiTokenRevoke') }}</el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-else :description="t('emptyApiTokens')" />
          </section>
        </section>

        <div v-else class="console-shell" :class="{ 'is-collapsed': sidebarCollapsed, 'is-task-packages-focus': miningTaskPackagesFocusMode }">
          <aside class="sidebar" aria-label="主导航">
            <div class="sidebar-brand" style="cursor:pointer" :title="productVersion ? 'QuicStudio v' + productVersion : 'QuicStudio'" @click="navigate('overview')"><strong>QS</strong><span v-if="!sidebarCollapsed">QuicStudio</span><small v-if="!sidebarCollapsed && productVersion" class="product-version">v{{ productVersion }}</small></div>
            <nav class="sidebar-nav" aria-label="控制台导航">
              <div v-if="canView('miningDash') || canView('miningTasks') || canView('batches')" class="sidebar-nav-group" :class="{ 'is-open': sidebarGroupOpen.manage }">
                <button data-view="miningDash" :title="sidebarCollapsed ? t('mining') : ''" @click="toggleSidebarGroup('manage')"><span class="nav-mark" v-html="sidebarIcons.mining"></span><span class="nav-label">{{ t('mining') }}</span><span class="nav-chevron">⌄</span></button>
                <div v-if="!sidebarCollapsed && sidebarGroupOpen.manage" class="sidebar-subnav">
                  <button v-if="canView('miningDash')" type="button" :class="{ active: activeView === 'miningDash' }" @click="navigate('miningDash')"><span>{{ t('miningDash') }}</span></button>
                  <button v-if="canView('miningTasks')" type="button" :class="{ active: activeView === 'miningTasks' }" @click="navigate('miningTasks')"><span>{{ t('miningTasks') }}</span></button>
                  <button v-if="canView('batches')" data-view="batches" type="button" :class="{ active: ['batches', 'intake-review'].includes(activeView) }" @click="navigate('batches')"><span>{{ t('collectedData') }}</span></button>
                  <button v-if="canView('resources')" data-view="resources" type="button" :class="{ active: activeView === 'resources' }" @click="navigate('resources')"><span>{{ t('resources') }}</span></button>
                </div>
              </div>
              <div v-if="canView('intake')" class="sidebar-nav-group">
                <button data-view="intake" :title="sidebarCollapsed ? t('dataOverview') : ''" :class="{ active: activeView === 'intake' }" @click="navigate('intake')"><span class="nav-mark" v-html="sidebarIcons.intake"></span><span class="nav-label">{{ t('dataOverview') }}</span></button>
              </div>
              <div v-if="canView('work-queue')" class="sidebar-nav-group" :class="{ 'is-open': sidebarGroupOpen.annotate }">
                <button data-view="work-queue" :title="sidebarCollapsed ? t('dataAnnotation') : ''" :class="{ active: activeView === 'work-queue' && (queueStage === 'annotation' || queueStage === 'review') }" @click="toggleSidebarGroup('annotate')"><span class="nav-mark" v-html="sidebarIcons['work-queue']"></span><span class="nav-label">{{ t('dataAnnotation') }}</span><span class="nav-chevron">⌄</span></button>
                <div v-if="!sidebarCollapsed && sidebarGroupOpen.annotate" class="sidebar-subnav">
                  <button type="button" :class="{ active: activeView === 'work-queue' && queueStage === 'annotation' }" @click="openQueueStage('annotation')"><span>{{ t('queueAnnotation') }}</span></button>
                  <button type="button" :class="{ active: activeView === 'work-queue' && queueStage === 'review' }" @click="openQueueStage('review')"><span>{{ t('queueReview') }}</span></button>
                </div>
              </div>
                <div v-if="canView('assets') || canView('datasets')" class="sidebar-nav-group sidebar-nav-group-consolidate" :class="{ 'is-open': sidebarGroupOpen.consolidate }">
                <button data-view="assets" :title="sidebarCollapsed ? t('dataConsolidate') : ''" @click="toggleSidebarGroup('consolidate')"><span class="nav-mark" v-html="sidebarIcons.assets"></span><span class="nav-label">{{ t('dataConsolidate') }}</span><span class="nav-chevron">⌄</span></button>
                <div v-if="!sidebarCollapsed && sidebarGroupOpen.consolidate" class="sidebar-subnav">
                  <button v-if="canView('assets')" data-view="assets" type="button" :class="{ active: activeView === 'assets' }" @click="navigate('assets')"><span>{{ t('assets') }}</span></button>
                  <button v-if="canView('datasets')" data-view="datasets" type="button" :class="{ active: activeView === 'datasets' }" @click="navigateDatasetsTab('view')"><span>{{ t('datasets') }}</span></button>
                </div>
              </div>
              <div v-if="canView('trainDash')" class="sidebar-nav-group" :class="{ 'is-open': sidebarGroupOpen.train }">
                <button type="button" :title="sidebarCollapsed ? t('trainNav') : ''" @click="toggleSidebarGroup('train')"><span class="nav-mark" v-html="sidebarIcons.datasets"></span><span class="nav-label">{{ t('trainNav') }}</span><span class="nav-chevron">⌄</span></button>
                <div v-if="!sidebarCollapsed && sidebarGroupOpen.train" class="sidebar-subnav">
                  <button v-if="canView('trainDash')" type="button" :class="{ active: activeView === 'trainDash' }" @click="navigate('trainDash')"><span>{{ t('trainDash') }}</span></button>
                  <button v-if="canView('trainJobs')" type="button" :class="{ active: activeView === 'trainJobs' }" @click="navigate('trainJobs')"><span>{{ t('trainJobs') }}</span></button>
                  <button v-if="canView('trainNew')" type="button" :class="{ active: activeView === 'trainNew' }" @click="navigate('trainNew')"><span>{{ t('trainNew') }}</span></button>
                  <button v-if="canView('trainDatasets')" type="button" :class="{ active: activeView === 'trainDatasets' }" @click="navigate('trainDatasets')"><span>{{ t('trainDatasets') }}</span></button>
                  <button v-if="canView('trainModels')" type="button" :class="{ active: activeView === 'trainModels' }" @click="navigate('trainModels')"><span>{{ t('trainModels') }}</span></button>
                  <button v-if="canView('trainResources')" type="button" :class="{ active: activeView === 'trainResources' }" @click="navigate('trainResources')"><span>{{ t('trainResources') }}</span></button>
                  <button v-if="canView('trainSystem')" type="button" :class="{ active: activeView === 'trainSystem' }" @click="navigate('trainSystem')"><span>{{ t('trainSystem') }}</span></button>
                </div>
              </div>
              <div v-if="canView('admin') || canView('settings')" class="sidebar-nav-group" :class="{ 'is-open': sidebarGroupOpen.management }">
                <button type="button" :title="sidebarCollapsed ? t('managementCenter') : ''" :class="{ active: activeView === 'admin' || activeView === 'settings' }" @click="toggleSidebarGroup('management')"><span class="nav-mark" v-html="sidebarIcons.settings"></span><span class="nav-label">{{ t('managementCenter') }}</span><span class="nav-chevron">⌄</span></button>
                <div v-if="!sidebarCollapsed && sidebarGroupOpen.management" class="sidebar-subnav">
                  <button v-if="canView('admin')" data-view="admin" type="button" :class="{ active: activeView === 'admin' }" @click="navigate('admin')"><span>{{ t('admin') }}</span></button>
                  <button v-if="canView('settings')" data-view="settings" type="button" :class="{ active: activeView === 'settings' }" @click="navigate('settings')"><span>{{ t('settings') }}</span></button>
                </div>
              </div>
              </nav>
            <button class="sidebar-collapse" :aria-label="sidebarCollapsed ? '展开导航' : '收起导航'" :title="sidebarCollapsed ? '展开导航' : '收起导航'" @click="toggleSidebar"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg></button>
          </aside>

          <section class="console-main">
            <header class="console-header">
              <div class="header-title"><strong>{{ activeView === 'package-workbench' ? (packageWorkbenchRoute.mode === 'review' ? (locale === 'en-US' ? 'Results' : '结果确认') : t('queueAnnotation')) : activeView === 'work-queue' ? queueStageLabel(queueStage) : t(activeView) }}</strong><el-tag v-if="demoMode" size="small" type="warning" effect="plain">本地演示</el-tag></div>
              <div class="header-actions">
                <el-button text @click="toggleLocale">{{ t('language') }}</el-button>
                <el-dropdown trigger="click" placement="bottom-end" @command="handleAccountCommand">
                  <button class="user-menu-trigger" type="button" :aria-label="t('accountMenu')">
                    <span class="user-avatar">{{ userInitial }}</span><span class="user-nickname">{{ userDisplayName }}</span><span class="user-menu-chevron">⌄</span>
                  </button>
                  <template #dropdown>
                    <el-dropdown-menu class="user-dropdown-menu">
                      <el-dropdown-item disabled class="user-dropdown-account"><span class="user-dropdown-email">{{ user.email }}</span><span class="user-dropdown-role">{{ roleLabel(user.role) }}</span></el-dropdown-item>
                      <el-dropdown-item divided command="change-password">{{ t('changePassword') }}</el-dropdown-item>
                      <el-dropdown-item divided command="api-tokens">{{ t('apiTokens') }}</el-dropdown-item>
                      <el-dropdown-item command="sign-out" class="user-dropdown-logout">{{ t('signOut') }}</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </header>

            <section v-if="!consoleAccessAvailable" class="empty-band"><h1>{{ t('noConsoleAccess') }}</h1></section>
            <section v-else-if="!selectedWorkspaceId && viewRequiresWorkspace(activeView)" class="empty-band"><h1>{{ t('noWorkspace') }}</h1><el-button v-if="canManageWorkspace" type="primary" style="margin-top: 12px;" @click="showWorkspaceDialog = true">{{ t('createWorkspace') }}</el-button></section>

            <template v-else>
              <div v-if="consoleAccessAvailable && activeView === 'overview'" class="scope-toolbar page-scope-toolbar" aria-label="工作范围切换">
                <div class="scope-fields">
                  <label><span>{{ t('workspace') }}</span><el-select v-model="selectedWorkspaceId" size="small" @change="switchWorkspace" :placeholder="t('workspace')"><el-option v-for="item in availableScopeWorkspaces" :key="item.id" :label="item.name" :value="item.id" /></el-select></label><el-button v-if="canManageWorkspace" size="small" type="primary" @click="showWorkspaceDialog = true">{{ t('createWorkspace') }}</el-button>
                </div>
              </div>
              <train-console v-if="['trainDash', 'trainJobs', 'trainNew', 'trainDatasets', 'trainModels', 'trainResources', 'trainSystem'].includes(activeView)" :view="activeView" :locale="locale"></train-console>
              <section v-if="activeView === 'overview'" class="view-stack">
                <template v-if="canViewDashboard">
                  <div class="page-heading"><div><p class="eyebrow">{{ t('overview') }}</p><h1>{{ t('dataOverview') }}</h1><span class="dashboard-meta">{{ formatDashboardTime(dashboardOverview?.computed_at) }}</span></div><el-button :loading="loading.dashboard" @click="refreshDashboardData">{{ t('refreshDashboard') }}</el-button></div>
                  <div v-loading="loading.dashboard" class="metric-grid is-dashboard">
                    <article class="metric-card is-kpi"><span>{{ t('kpiTotalEpisodes') }}</span><strong>{{ dashboardMetricValue(dashboardOverview?.kpis?.total_episodes) }}</strong><small class="kpi-delta" :class="'is-' + dashboardDelta(dashboardOverview?.kpis?.deltas?.total_episodes).tone">{{ dashboardDelta(dashboardOverview?.kpis?.deltas?.total_episodes).text }}</small></article>
                    <article class="metric-card is-kpi"><span>{{ t('kpiTotalDuration') }}</span><strong>{{ formatDashboardDuration(dashboardOverview?.kpis?.total_duration_s) }}</strong><small class="kpi-delta" :class="'is-' + dashboardDelta(dashboardOverview?.kpis?.deltas?.total_duration_s).tone">{{ dashboardDelta(dashboardOverview?.kpis?.deltas?.total_duration_s).text }}</small></article>
                    <article class="metric-card is-kpi"><span>{{ t('kpiCollectedToday') }}</span><strong>{{ dashboardMetricValue(dashboardOverview?.kpis?.collected_today) }}</strong><small class="kpi-delta" :class="'is-' + dashboardDelta(dashboardOverview?.kpis?.deltas?.collected_today).tone">{{ dashboardDelta(dashboardOverview?.kpis?.deltas?.collected_today).text }}</small></article>
                    <article class="metric-card is-kpi"><span>{{ t('kpiAnnotationCompleted') }}</span><strong>{{ dashboardMetricValue(dashboardOverview?.kpis?.annotation_completed) }}</strong><small class="kpi-delta" :class="'is-' + dashboardDelta(dashboardOverview?.kpis?.deltas?.annotation_completed).tone">{{ dashboardDelta(dashboardOverview?.kpis?.deltas?.annotation_completed).text }}</small></article>
                    <article class="metric-card is-kpi"><div class="kpi-rate"><span>{{ t('kpiAnnotationRate') }}</span><strong>{{ dashboardPercent(dashboardOverview?.kpis?.annotation_completion_rate) }}</strong><el-progress v-if="dashboardOverview?.kpis?.annotation_completion_rate != null" :percentage="Math.min(100, Math.round(Number(dashboardOverview.kpis.annotation_completion_rate) * 100))" :stroke-width="8" :show-text="false" /></div></article>
                  </div>
                  <div class="dashboard-grid">
                    <section class="surface-panel"><div class="panel-heading"><div><h2>{{ t('pipelineFunnel') }}</h2><span>{{ t('avgDuration') }} {{ formatDashboardDuration(dashboardOverview?.pipeline_funnel?.avg_duration_s) }}</span></div></div><div ref="funnelChartEl" class="dashboard-chart"></div></section>
                    <section class="surface-panel table-panel"><div class="panel-heading"><div><h2>{{ t('todayTasks') }}</h2></div></div>
                      <el-table :data="dashboardTodayQueues" class="data-table dashboard-queue-table">
                        <el-table-column prop="label" :label="t('queueName')" min-width="120" />
                        <el-table-column prop="count" :label="t('queueCount')" width="90" />
                        <el-table-column :label="t('queueShare')" min-width="140"><template #default="scope"><div class="queue-share"><div class="queue-bar"><span :style="{ width: formatPercent(scope.row.share) }"></span></div><span>{{ formatPercent(scope.row.share) }}</span></div></template></el-table-column>
                        <el-table-column prop="pending" :label="t('queuePending')" width="90" />
                      </el-table>
                    </section>
                  </div>
                  <div class="dashboard-grid">
                    <section class="surface-panel"><div class="panel-heading"><div><h2>{{ t('collectTrend7d') }}</h2></div></div><div ref="trendChartEl" class="dashboard-chart is-tall"></div></section>
                    <section class="surface-panel"><div class="panel-heading"><div><h2>{{ t('deviceDistribution') }}</h2><span>{{ dashboardMetricValue(dashboardOverview?.device_distribution?.total) }} {{ t('devicesUnit') }}</span></div></div><div ref="deviceChartEl" class="dashboard-chart is-tall"></div></section>
                  </div>
                  <el-alert v-if="dashboardOverviewError" type="error" :closable="false" :title="dashboardOverviewError" show-icon style="margin-top: 4px;" />
                  <el-alert v-else-if="dashboardOverview?.is_mock" type="warning" :closable="false" :title="t('dashboardMockHint')" show-icon style="margin-top: 4px;" />
                  <el-alert v-else-if="dashboardOverview?.status === 'empty'" type="info" :closable="false" :title="t('dashboardEmpty')" />
                </template>
                <template v-else>
                  <div class="page-heading"><div><p class="eyebrow">{{ t('currentTaskSet') }}</p><h1>{{ currentTaskSet?.name }}</h1><span>{{ currentTaskSet?.scene || '—' }} · {{ t('externalProject') }}: {{ externalProjectStatus(currentTaskSet) }}</span></div><el-button v-if="canImport" type="primary" @click="showIntakeGuide = true">{{ t('importData') }}</el-button></div>
                  <div class="metric-grid"><article v-if="canView('batches')" class="metric-card"><span>{{ t('batches') }}</span><strong>{{ overviewCounts.batches }}</strong></article><article v-if="canView('assets')" class="metric-card"><span>{{ t('episodes') }}</span><strong>{{ overviewCounts.episodes }}</strong></article><article v-if="canView('work-queue')" class="metric-card"><span>{{ t('workQueue') }}</span><strong>{{ overviewCounts.queue }}</strong></article><article v-if="canView('intake')" class="metric-card"><span>{{ t('importHistory') }}</span><strong>{{ overviewCounts.runningImports }}</strong></article></div>
                  <section v-if="canView('work-queue')" class="surface-panel activity-panel"><div class="panel-heading"><div><h2>{{ t('workQueue') }}</h2><span>{{ queueTotal }} {{ t('itemCount') }}</span></div><el-button link type="primary" @click="navigate('work-queue')">{{ t('workQueue') }}</el-button></div><div v-if="queueRows.length" class="compact-list"><button v-for="row in queueRows.slice(0, 5)" :key="row.work_item.id" class="compact-row" @click="openEpisodeDetail(row.episode, { source: 'overview' })"><span>{{ row.episode.episode_uid }}</span><el-tag size="small" effect="plain" :type="qualityStatusType(row.quality?.status)">{{ qualityStatusLabel(row.quality?.status) }}</el-tag><span>{{ row.work_item.status }}</span></button></div><el-empty v-else :description="t('emptyQueue')" :image-size="72" /></section>
                </template>
              </section>

              <section v-else-if="activeView === 'intake'" class="view-stack">
                <data-overview :workspace-id="selectedWorkspaceId" :project-id="selectedCollectionProjectId" :locale="locale" :refresh-key="dataOverviewRefreshKey" @view-package="openDataPackageDrawer" @build="openPackageBatchDialog">
                  <template #scope-filters>
                    <el-select v-model="selectedWorkspaceId" filterable :placeholder="t('workspace')" @change="switchWorkspace"><el-option v-for="item in availableScopeWorkspaces" :key="item.id" :label="item.name" :value="item.id" /></el-select>
                    <el-select v-model="selectedCollectionProjectId" clearable filterable :disabled="!selectedWorkspaceId" :placeholder="t('collectProject')" @change="switchCollectionProject"><el-option v-for="item in collectionProjectOptions" :key="item.id" :label="item.name" :value="item.id" /></el-select>
                  </template>
                </data-overview>
                <el-dialog v-model="overviewBuildDialogVisible" :title="t('buildBatchTitle')" width="520px" destroy-on-close @closed="onOverviewBuildDialogClosed">
                  <el-form label-position="top">
                    <el-form-item :label="t('buildBatchName')" required>
                      <el-input v-model="overviewBuildForm.name" :placeholder="t('buildBatchName')" maxlength="256" />
                    </el-form-item>
                    <el-form-item :label="t('buildBatchGovernance')">
                      <div style="display:flex; gap:16px; flex-wrap:wrap;">
                        <el-checkbox v-model="overviewBuildForm.governance.integrity">{{ t('stageIntegrity') }}</el-checkbox>
                        <el-checkbox v-model="overviewBuildForm.governance.quality">{{ t('stageQuality') }}</el-checkbox>
                        <el-checkbox v-model="overviewBuildForm.governance.compliance">{{ t('complianceCheck') }}</el-checkbox>
                      </div>
                    </el-form-item>
                    <el-form-item :label="t('buildBatchAnnotation')">
                      <el-checkbox v-model="overviewBuildForm.annotation">{{ t('stageEnabled') }}</el-checkbox>
                    </el-form-item>
                    <div v-if="overviewBuildForm.annotation" class="build-assignee-grid">
                      <el-form-item :label="locale === 'en-US' ? 'Annotators' : '标注员（可多选）'" required>
                        <el-select v-model="overviewBuildForm.annotators" multiple collapse-tags :placeholder="locale === 'en-US' ? 'Select annotators' : '选择标注员'" style="width: 100%;">
                          <el-option v-for="user in annotatorUserOptions" :key="user.id" :label="user.email || user.name || ('#' + user.id)" :value="user.id" />
                        </el-select>
                        <small class="form-help">{{ t('annotatorRoleHint') }}</small>
                      </el-form-item>
                      <el-form-item :label="locale === 'en-US' ? 'Review mode' : '审核模式'" required>
                        <el-radio-group v-model="overviewBuildForm.reviewMode">
                          <el-radio value="single">{{ locale === 'en-US' ? 'One reviewer' : '单审' }}</el-radio>
                        </el-radio-group>
                      </el-form-item>
                      <el-form-item :label="locale === 'en-US' ? 'Reviewer' : '审核员'" required>
                        <el-select v-model="overviewBuildForm.reviewers" multiple collapse-tags :multiple-limit="1" :placeholder="locale === 'en-US' ? 'Select reviewer' : '选择审核员'" style="width: 100%;">
                          <el-option v-for="user in reviewerUserOptions" :key="user.id" :label="user.email || user.name || ('#' + user.id)" :value="user.id" />
                        </el-select>
                        <small class="form-help">{{ locale === 'en-US' ? 'One reviewer confirms the submitted results.' : '单审模式由一名审核员完成审核' }}</small>
                      </el-form-item>
                    </div>
                    <el-form-item :label="t('tagScene')">
                      <el-select v-model="overviewBuildForm.sceneLabelIds" multiple filterable :placeholder="t('tagScene')" style="width: 100%;">
                        <el-option v-for="item in sceneLabelOptions" :key="item.id" :label="item.name" :value="item.id" />
                      </el-select>
                    </el-form-item>
                    <el-form-item :label="t('tagPurpose')">
                      <el-select v-model="overviewBuildForm.purposeLabelIds" multiple filterable :placeholder="t('tagPurpose')" style="width: 100%;">
                        <el-option v-for="item in purposeLabelOptions" :key="item.id" :label="item.name" :value="item.id" />
                      </el-select>
                    </el-form-item>
                    <el-form-item :label="t('modalityLabel')">
                      <el-select v-model="overviewBuildForm.modalityLabelIds" multiple filterable :placeholder="t('modalityLabel')" style="width: 100%;">
                        <el-option v-for="item in modalityLabelOptions" :key="item.id" :label="item.name" :value="item.id" />
                      </el-select>
                    </el-form-item>
                    <el-form-item :label="t('tagTrain')">
                      <el-select v-model="overviewBuildForm.trainLabelIds" multiple filterable :placeholder="t('tagTrain')" style="width: 100%;">
                        <el-option v-for="item in trainLabelOptions" :key="item.id" :label="item.name" :value="item.id" />
                      </el-select>
                    </el-form-item>
                  </el-form>
                  <template #footer>
                    <el-button @click="exitOverviewBuildMode">{{ t('buildBatchCancel') }}</el-button>
                    <el-button type="primary" :loading="saving" @click="submitOverviewBuild">{{ t('buildBatchConfirm') }}</el-button>
                  </template>
                </el-dialog>
              </section>

<package-annotation-workbench v-else-if="activeView === 'package-workbench'" ref="packageAnnotationWorkbenchRef" :workspace-id="packageWorkbenchRoute.workspaceId" :work-item-id="packageWorkbenchRoute.workItemId" :mode="packageWorkbenchRoute.mode" :locale="locale" @back="backFromPackageWorkbench" />
<intake-review-workbench v-else-if="activeView === 'intake-review'" ref="intakeReviewWorkbenchRef" :workspace-id="selectedWorkspaceId" :package-id="intakeReviewPackageId" :locale="locale" @back="backFromIntakeReview" @completed="loadReviewPackages" />

<section v-else-if="activeView === 'batches'" class="view-stack">
                <template v-if="selectedBatch">
                  <div class="page-actions page-actions-split">
                    <el-button @click="navigate('miningTasks')">{{ t('backToBatchesList') }}</el-button>
                    <div class="page-actions-group">
                      <filter-search-box v-model="batchDetailFilters" :attrs="miningBatchFilterAttrs" :value-map="batchDetailFilterValueMap" :placeholder="t('miningFilterPlaceholder')" :menu-title="t('miningFilterMenuTitle')" @keyword-change="(value) => batchDetailFilterText = value" @change="onBatchDetailFilterChange" />
                    </div>
                  </div>
                  <template v-if="selectedBatch.batch_type === 'lerobot'">
                    <div class="lerobot-batch-detail-grid">
                      <section class="surface-panel lerobot-batch-summary"><div class="panel-heading"><div><h2>{{ t('batchSummary') }}</h2></div></div><dl class="detail-list"><dt>{{ t('status') }}</dt><dd><el-tag effect="plain">{{ batchStatusLabel(selectedBatch.status) }}</el-tag></dd><dt>{{ t('batchSequence') }}</dt><dd>#{{ selectedBatch.sequence_number }}</dd><dt>{{ t('datasets') }}</dt><dd>{{ selectedBatch.dataset_count || 0 }}</dd><dt>{{ t('fileCount') }}</dt><dd>{{ selectedBatch.file_count || 0 }}</dd><dt>{{ t('size') }}</dt><dd>{{ formatBytes(selectedBatch.total_size) }}</dd><dt>{{ t('nativePlatformCopy') }}</dt><dd>{{ selectedBatch.copy_summary?.succeeded || 0 }} / {{ selectedBatch.copy_summary?.total || 0 }}</dd><dt>{{ t('createdAt') }}</dt><dd><time class="compact-date-time" :datetime="selectedBatch.created_at" :title="formatDateFull(selectedBatch.created_at)" tabindex="0">{{ formatDate(selectedBatch.created_at) }}</time></dd></dl></section>
                    </div>
                    <section class="surface-panel table-panel lerobot-batch-datasets" v-loading="loading.lerobotBatchDatasets"><div class="panel-heading"><div><h2>{{ t('datasets') }}</h2><span>{{ nativeLerobotBatchDatasets.length }} {{ t('itemCount') }}</span></div></div><el-table v-if="nativeLerobotBatchDatasets.length" :data="nativeLerobotBatchDatasets" class="data-table" fit @row-click="openNativeLerobotDataset"><el-table-column :label="t('datasetName')" min-width="240"><template #default="scope"><strong>{{ scope.row.dataset_id }}</strong></template></el-table-column><el-table-column :label="t('robotType')" min-width="140"><template #default="scope">{{ scope.row.robot_type }}</template></el-table-column><el-table-column :label="t('fileCount')" width="110"><template #default="scope">{{ scope.row.file_count }}</template></el-table-column><el-table-column :label="t('size')" width="120"><template #default="scope">{{ formatBytes(scope.row.total_size) }}</template></el-table-column><el-table-column :label="t('nativePlatformCopy')" min-width="170"><template #default="scope"><el-tag size="small" effect="plain" :type="nativeCopyStatusType(scope.row.copy_status)">{{ nativeCopyStatusLabel(scope.row.copy_status) }}</el-tag></template></el-table-column></el-table><el-empty v-else :description="t('emptyDatasets')" :image-size="64" /></section>
                  </template>
                  <template v-else>
                  <div class="batch-detail-grid">
                    <section v-if="selectedBatch.collector_duration_summary?.length" class="surface-panel table-panel"><div class="panel-heading"><div><h2>{{ t('collectorDurationSummary') }}</h2></div></div><el-table :data="selectedBatch.collector_duration_summary" class="data-table" fit><el-table-column :label="t('collector')" min-width="180"><template #default="scope">{{ collectorDisplayLabel(scope.row.collector) || t('unknownCollector') }}</template></el-table-column><el-table-column prop="episode_count" :label="t('sourceCount')" width="110" /><el-table-column :label="t('totalDuration')" width="130"><template #default="scope">{{ formatDuration(scope.row.duration_s) }}</template></el-table-column><el-table-column :label="t('availableDuration')" min-width="170"><template #default="scope">{{ formatAvailableDuration(scope.row.available_duration_s) }}</template></el-table-column></el-table></section>
                    <section v-if="selectedBatch.device_duration_summary?.length" class="surface-panel table-panel"><div class="panel-heading"><div><h2>{{ t('deviceDurationSummary') }}</h2></div></div><el-table :data="selectedBatch.device_duration_summary" class="data-table" fit><el-table-column :label="t('collectionDevice')" min-width="190"><template #default="scope">{{ scope.row.device?.name || t('unknownDevice') }}</template></el-table-column><el-table-column prop="episode_count" :label="t('sourceCount')" width="110" /><el-table-column :label="t('totalDuration')" width="130"><template #default="scope">{{ formatDuration(scope.row.duration_s) }}</template></el-table-column><el-table-column :label="t('availableDuration')" min-width="170"><template #default="scope">{{ formatAvailableDuration(scope.row.available_duration_s) }}</template></el-table-column></el-table></section>
                  </div>
                  <div class="list-filter-bar">
                    <el-select v-model="batchDetailPageFilter.task" :placeholder="t('collectionTask')" @change="onDetailTaskChange" clearable style="min-width: 170px;"><el-option v-for="item in detailTaskOptions" :key="item.id" :label="item.name" :value="item.id" /></el-select>
                    <el-select v-model="batchDetailPageFilter.batch" :placeholder="t('dataPackageName')" @change="onDetailBatchChange" clearable style="min-width: 190px;"><el-option v-for="item in detailBatchOptions" :key="item.id" :label="item.label" :value="item.id" /></el-select>
                    <el-date-picker v-model="batchDetailPageFilter.collectRange" type="datetimerange" value-format="YYYY-MM-DD HH:mm:ss" range-separator="至" :start-placeholder="t('collectDateLabel')" :end-placeholder="t('collectDateLabel')" style="width: 330px;" />
                    <el-date-picker v-model="batchDetailPageFilter.uploadRange" type="datetimerange" value-format="YYYY-MM-DD HH:mm:ss" range-separator="至" :start-placeholder="t('uploadEndTime')" :end-placeholder="t('uploadEndTime')" style="width: 330px;" />
                    <el-select v-model="batchDetailPageFilter.modality" :placeholder="t('modalityLabel')" clearable><el-option v-for="item in detailModalityOptions" :key="item" :label="batchTypeLabel(item)" :value="item" /></el-select>
                    <el-select v-model="batchDetailPageFilter.collector" :placeholder="t('assigneeCollectors')" clearable><el-option v-for="item in miningBatchCollectorOptions" :key="item" :label="item" :value="item" /></el-select>
                    <el-select v-model="batchDetailPageFilter.device" :placeholder="t('assigneeDevices')" clearable><el-option v-for="item in miningBatchDeviceOptions" :key="item" :label="item" :value="item" /></el-select>
                    <el-button @click="resetBatchDetailPageFilter">{{ t('clearFilters') }}</el-button>
                  </div>
                  <section class="surface-panel table-panel"><div class="panel-heading"><div><h2>{{ t('episodesDetail') }}</h2><span>{{ filteredBatchDetailImports.length }} {{ t('itemCount') }}</span><el-button size="small" type="primary" plain :disabled="!batchPreviewCurrent" @click="openBatchImportDetail(batchPreviewCurrent)">{{ t('batchEpisodeViewDetail') }}</el-button></div><div class="panel-actions"><el-button size="small" type="success" plain @click="reviewSelectedBatchDetail(true)">{{ t('approveReview') }}</el-button><el-button size="small" type="danger" plain @click="reviewSelectedBatchDetail(false)">{{ t('rejectReview') }}</el-button></div></div>
                  <div v-if="batchPreviewCurrent" class="batch-preview-body">
                    <div class="batch-preview-meta"><strong>{{ batchPreviewCurrent.original_name || batchPreviewCurrent.import_type }}</strong><div class="panel-actions" style="margin-left: auto;"><el-button :disabled="batchPreviewIndex <= 0" @click="prevBatchPreview">{{ t('prevVideo') }}</el-button><span class="muted" style="align-self: center;">{{ filteredBatchDetailImports.length ? batchPreviewIndex + 1 : 0 }} / {{ filteredBatchDetailImports.length }}</span><el-button :disabled="batchPreviewIndex >= filteredBatchDetailImports.length - 1" @click="nextBatchPreview">{{ t('nextVideo') }}</el-button><el-button @click="loadBatchImports">{{ t('refresh') }}</el-button></div></div>
                    <video :key="batchPreviewIndex" :src="batchPreviewCurrent?.preview_url || batchPreviewCurrent?.video_url || (demoMode ? importDetailMockVideoUrl : '')" controls playsinline preload="metadata" class="cut-preview-video"></video>
                  </div>
                  <el-empty v-else :description="t('emptyQueue')" :image-size="64" />
                </section>
<el-drawer v-model="batchCutDrawerVisible" direction="rtl" size="720px" :with-header="false" class="batch-cut-drawer">
                    <section v-if="batchCutSelected" class="surface-panel mining-cuts-viewer">
                      <div class="panel-heading"><div><h2>{{ batchCutSelected.episode_uid }}</h2><span>{{ batchCutSelected.task_name }} · {{ batchCutSelected.collector }} · {{ batchCutSelected.device }}</span></div></div>
                      <el-tabs v-model="batchCutDetailTab" class="episode-detail-tabs">
                        <el-tab-pane :label="t('overview')" name="overview">
                          <section class="episode-detail-section episode-preview-section">
                            <h3>{{ t('preview') }}</h3>
                            <video v-if="batchCutSelected.preview_url" ref="batchCutVideoEl" :src="batchCutSelected.preview_url" controls playsinline preload="metadata" class="cut-preview-video" @timeupdate="onBatchCutTimeUpdate"></video>
                            <el-empty v-else :description="t('previewUnavailable')" :image-size="64" />
                          </section>
                          <section class="episode-detail-section">
                            <h3>{{ t('identityAndLineage') }}</h3>
                            <dl class="episode-detail-grid">
                              <div><dt>{{ t('episode') }}</dt><dd>{{ batchCutSelected.episode_uid || '—' }}</dd></div>
                              <div><dt>{{ t('kind') }}</dt><dd>{{ String(batchCutSelected.episode_uid || '').indexOf('DRV') === 0 ? 'Derived' : 'Source' }}</dd></div>
                              <div><dt>{{ t('parentEpisode') }}</dt><dd>—</dd></div>
                              <div><dt>{{ t('derivationVersion') }}</dt><dd>—</dd></div>
                            </dl>
                          </section>
                          <section class="episode-detail-section">
                            <h3>{{ t('businessContext') }}</h3>
                            <dl class="episode-detail-grid">
                              <div><dt>{{ t('collectionTask') }}</dt><dd>{{ batchCutSelected.task_name || '—' }}</dd></div>
                              <div><dt>{{ t('modality') }}</dt><dd>ego</dd></div>
                              <div><dt>{{ t('scene') }}</dt><dd>—</dd></div>
                              <div><dt>{{ t('collector') }}</dt><dd>{{ batchCutSelected.collector || '—' }}</dd></div>
                              <div><dt>{{ t('collectionDevice') }}</dt><dd>{{ batchCutSelected.device || '—' }}</dd></div>
                            </dl>
                          </section>
                          <section class="episode-detail-section">
                            <h3>{{ t('quality') }} / {{ t('metrics') }}</h3>
                            <dl class="episode-detail-grid">
                              <div><dt>{{ t('referenceFrames') }}</dt><dd>{{ formatNumber(batchCutMetrics.frames) }}</dd></div>
                              <div><dt>{{ t('duration') }}</dt><dd>{{ formatDuration(batchCutSelected.duration_s) }}</dd></div>
                              <div><dt>{{ t('frameRate') }}</dt><dd>{{ batchCutMetrics.rgbRate }}</dd></div>
                            </dl>
                          </section>
                          <section class="episode-detail-section">
                            <h3>{{ t('lifecycle') }}</h3>
                            <dl class="episode-detail-grid">
                              <div><dt>{{ t('publication') }}</dt><dd>{{ t('publicationQueued') }}</dd></div>
                              <div><dt>{{ t('createdAt') }}</dt><dd>{{ batchCutLifecycle.created }}</dd></div>
                              <div><dt>{{ t('updatedAt') }}</dt><dd>{{ batchCutLifecycle.updated }}</dd></div>
                              <div><dt>{{ t('publishedAt') }}</dt><dd>—</dd></div>
                            </dl>
                          </section>
                          <el-collapse class="episode-technical-collapse">
                            <el-collapse-item :title="t('technicalInfo')" name="technical">
                              <dl class="episode-detail-grid episode-technical-grid">
                                <div><dt>{{ t('databaseId') }}</dt><dd>1</dd></div>
                                <div><dt>{{ t('workspaceId') }}</dt><dd>{{ selectedWorkspaceId || '—' }}</dd></div>
                                <div><dt>{{ t('taskSetId') }}</dt><dd>{{ selectedTaskSetId || '—' }}</dd></div>
                                <div><dt>{{ t('importSessionId') }}</dt><dd>4370902d-a43b-4ad1-b35a-b5ec012f32a7</dd></div>
                                <div><dt>{{ t('embodimentId') }}</dt><dd>—</dd></div>
                                <div><dt>{{ t('taskLabelId') }}</dt><dd>1</dd></div>
                                <div><dt>{{ t('sourceRange') }}</dt><dd>— – —</dd></div>
                              </dl>
                            </el-collapse-item>
                          </el-collapse>
                        </el-tab-pane>
                        <el-tab-pane :label="t('artifacts')" name="artifacts">
                          <el-table :data="batchCutArtifacts" class="data-table" size="small" fit>
                            <el-table-column prop="type" :label="t('kind')" min-width="180" />
                            <el-table-column prop="role" :label="t('storageRole')" width="120" />
                            <el-table-column prop="size" :label="t('size')" width="120" />
                            <el-table-column prop="retention" :label="t('retentionPolicy')" width="130" />
                            <el-table-column prop="until" :label="t('retentionUntil')" width="110" />
                          </el-table>
                          <section class="surface-panel raw-source-panel">
                            <div class="panel-heading"><div><h3>{{ t('rawSourceDownloads') }}</h3></div><el-button size="small" @click="loadBatchCutRawSource">{{ t('loadRawSourceDownloads') }}</el-button></div>
                            <el-table v-if="batchCutRawSourceLoaded" :data="batchCutArtifacts.filter((item) => item.role === 'raw')" class="data-table" size="small" fit>
                              <el-table-column prop="type" :label="t('kind')" min-width="180" />
                              <el-table-column prop="size" :label="t('size')" width="120" />
                            </el-table>
                          </section>
                        </el-tab-pane>
                        <el-tab-pane :label="t('packetMetadata')" name="packet-metadata">
                          <template v-if="hasPacketMetadata(batchCutPacketMetadata)">
                            <dl class="detail-list packet-metadata-list">
                              <template v-for="row in packetMetadataRows(batchCutPacketMetadata)" :key="row.key"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></template>
                            </dl>
                            <el-table v-if="batchCutPacketMetadata.cameras?.length" :data="batchCutPacketMetadata.cameras" class="data-table" size="small" fit>
                              <el-table-column :label="t('packetCameras')" min-width="260"><template #default="scope"><code>{{ scope.row.topic }}</code></template></el-table-column>
                              <el-table-column :label="t('packetResolution')" width="130"><template #default="scope">{{ cameraResolutionLabel(scope.row) }}</template></el-table-column>
                              <el-table-column :label="t('packetFps')" width="90"><template #default="scope">{{ scope.row.fps != null ? scope.row.fps : '—' }}</template></el-table-column>
                            </el-table>
                            <el-table v-if="batchCutPacketMetadata.sensors?.length" :data="batchCutPacketMetadata.sensors" class="data-table" size="small" fit>
                              <el-table-column :label="t('packetSensors')" min-width="260"><template #default="scope"><code>{{ scope.row.topic }}</code></template></el-table-column>
                              <el-table-column :label="t('packetFrequency')" width="110"><template #default="scope">{{ scope.row.rate }}</template></el-table-column>
                            </el-table>
                          </template>
                          <el-empty v-else :description="t('emptyPacketMetadata')" :image-size="64" />
                        </el-tab-pane>
                      </el-tabs>
                    </section>
                    <el-empty v-else :description="t('cutSelectHint')" :image-size="72" />
                  </el-drawer>
                  </template>
                </template>
                <template v-else>
                  <div class="page-actions collection-scope-actions">
                    <label><span>{{ t('workspace') }}</span><el-select v-model="selectedWorkspaceId" size="small" @change="switchWorkspace" :placeholder="t('workspace')"><el-option v-for="item in availableScopeWorkspaces" :key="item.id" :label="item.name" :value="item.id" /></el-select></label>
                    <label><span>{{ t('collectProject') }}</span><el-select v-model="miningProjectScope" size="small" multiple collapse-tags collapse-tags-tooltip clearable :disabled="!selectedWorkspaceId" :placeholder="t('collectProject')" @change="switchCollectionProjects"><el-option v-for="item in collectionProjectOptions" :key="item.id" :label="item.name" :value="item.name" /></el-select></label>
                    <el-button @click="loadReviewPackages">{{ t('refresh') }}</el-button>
                  </div>
                <section class="surface-panel table-panel" v-loading="reviewPackageLoading">
                  <div class="panel-heading">
                    <div><h2>{{ t('reviewPackagesTitle') }}</h2><span>{{ reviewPackageRowsFiltered.length }} / {{ reviewPackageTotal }} {{ t('itemCount') }}</span></div>
                    <div class="list-filter-bar">
                      <el-select v-model="reviewPackageStatus" clearable :placeholder="t('dataPackageStatusLabel')" style="min-width: 150px;" @change="resetReviewPackagePage"><el-option v-for="status in reviewPackageStatusOptions" :key="status" :label="dataPackageStatusLabel(status)" :value="status" /></el-select>
                      <el-button @click="loadReviewPackages">{{ t('refresh') }}</el-button>
                    </div>
                  </div>
                  <el-alert type="info" :closable="false" :title="t('reviewPackagesHint')" show-icon style="margin: 0 0 12px;" />
                  <div class="page-actions" v-if="reviewPackageSelectedIds.length">
                    <el-button type="success" @click="bulkApproveReviewPackages">{{ t('bulkApproveIntake') }} ({{ reviewPackageSelectedIds.length }})</el-button>
                  </div>
                  <el-table v-if="reviewPackageRowsFiltered.length" :data="reviewPackageRowsFiltered" class="data-table" fit row-key="id" :row-class-name="reviewPackageRowClass" @selection-change="onReviewPackageSelectionChange">
                    <el-table-column type="selection" width="46" :selectable="canReviewPackageRow" />
                    <el-table-column :label="t('batchSeq')" width="70"><template #default="scope">#{{ scope.row.seq }}</template></el-table-column>
                    <el-table-column :label="t('collectProject')" min-width="140" show-overflow-tooltip><template #default="scope">{{ reviewPackageProjectName(scope.row) }}</template></el-table-column>
                    <el-table-column :label="t('collectionTask')" min-width="170" show-overflow-tooltip><template #default="scope">{{ reviewPackageTaskName(scope.row) }}</template></el-table-column>
                    <el-table-column :label="t('batchTargetDurationLabel')" width="120"><template #default="scope">{{ scope.row.target_duration_hours ?? '—' }} {{ t('dashHour') }}</template></el-table-column>
                    <el-table-column :label="t('collectedDuration')" width="110"><template #default="scope">{{ formatAvailableDuration(scope.row.captured_duration_s) }}</template></el-table-column>
                    <el-table-column :label="t('validDurationLabel')" width="130"><template #default="scope">{{ formatAvailableDuration(scope.row.intake_valid_duration_s) }}</template></el-table-column>
                    <el-table-column :label="t('collector')" width="110"><template #default="scope">{{ reviewPackageCollectorName(scope.row) }}</template></el-table-column>
                    <el-table-column :label="t('collectionDevice')" min-width="150" show-overflow-tooltip><template #default="scope">{{ reviewPackageDeviceName(scope.row) }}</template></el-table-column>
                    <el-table-column :label="t('uploadEndTime')" width="140"><template #default="scope">{{ scope.row.upload_completed_at ? formatDate(scope.row.upload_completed_at) : '—' }}</template></el-table-column>
                    <el-table-column :label="t('collectionTimeLabel')" width="190"><template #default="scope">{{ miningBatchCollectionTimeText(scope.row) }}</template></el-table-column>
                    <el-table-column :label="t('dataPackageStatusLabel')" width="130"><template #default="scope"><el-tag size="small" effect="plain" :type="dataPackageStatusType(scope.row.status)">{{ dataPackageStatusLabel(scope.row.status) }}</el-tag></template></el-table-column>
                    <el-table-column :label="t('actions')" width="330" fixed="right">
                      <template #default="scope">
                        <div class="review-package-actions">
                          <el-button link size="small" type="primary" class="table-action-view-detail" @click.stop="openDataPackageDrawer(scope.row)">{{ t('viewPackageDetails') }}</el-button>
                          <el-button v-if="canReviewPackage(scope.row)" plain size="small" type="primary" @click="openIntakeReview(scope.row)">{{ t('enterIntakeReviewAction') }}</el-button>
                          <el-button v-if="canReviewPackageRow(scope.row)" plain size="small" type="success" @click="openIntakeReview(scope.row)">{{ t('intakeReviewApprove') }}</el-button>
                        </div>
                      </template>
                    </el-table-column>
                  </el-table>
                  <el-empty v-else :description="t('emptyReviewPackages')" />
                  <el-pagination v-if="reviewPackageTotal > 0" :current-page="reviewPackagePage" :page-size="50" :total="reviewPackageTotal" layout="total, prev, pager, next" @current-change="changeReviewPackagePage" />
                </section>
                </template>
              </section>

              <section v-else-if="activeView === 'workbench'" class="view-stack workbench-view" :style="{ '--timeline-panel-height': timelinePanelHeight + 'px', '--workbench-editor-width': workbenchEditorWidth + 'px' }" v-loading="loading.workbench">
                <div class="page-actions page-actions-split">
                  <el-button @click="navigate('work-queue')">{{ t('backToQueue') }}</el-button>
                  <div v-if="activeWorkbench" class="heading-actions">
                    <el-button v-if="activeWorkbench.work_item.available_actions?.includes('release')" :disabled="saving" @click="releaseWorkbench">{{ t('actionRelease') }}</el-button>
                    <el-button v-if="demoMode || activeWorkbench.work_item.available_actions?.includes('save_draft')" :loading="saving" :disabled="activeWorkbench.capabilities?.cut ? !cutDraftSaveValid() : activeWorkbench.capabilities?.annotation ? !annotationDraftSaveValid() : false" @click="saveWorkbenchDraft">{{ t('saveDraft') }}</el-button>
                    <el-button v-if="activeWorkbench.work_item.available_actions?.includes('submit')" type="primary" :loading="saving" :disabled="activeWorkbench.capabilities?.cut ? !cutDraftSubmitValid() : activeWorkbench.capabilities?.annotation ? !annotationDraftSubmitValid() : false" @click="submitWorkbenchDraft">{{ t('submitForReview') }}</el-button>
                  </div>
                </div>
                <template v-if="activeWorkbench">
                  <div class="workbench-grid" :class="{ 'is-cut-workbench': activeWorkbench.capabilities?.cut }">
                    <div class="workbench-studio">
                    <section class="surface-panel workbench-media">
                        <div class="panel-heading"><div><h2>{{ activeWorkbench.episode.episode_uid }}</h2><span>{{ activeWorkbench.episode.task_label?.name || '—' }} · {{ activeWorkbench.timeline?.reference_topic || '—' }}</span></div><el-tag size="small" effect="plain" :type="qualityStatusType(activeWorkbench.episode.quality?.status)">{{ qualityStatusLabel(activeWorkbench.episode.quality?.status) }}</el-tag></div>
                      <div class="workbench-media-body">
                        <div class="workbench-video-stage">
                          <video ref="workbenchVideo" v-if="activeWorkbench.media?.preview?.available" class="workbench-video" preload="metadata" playsinline :src="activeWorkbench.media.preview.url" :aria-label="t('preview')" @loadedmetadata="syncWorkbenchPlayhead" @timeupdate="syncWorkbenchPlayhead" @play="syncWorkbenchPlayhead" @pause="syncWorkbenchPlayhead" @ended="syncWorkbenchPlayhead"></video>
                          <el-empty v-else :description="previewStatusLabel(activeWorkbench.media?.preview)" :image-size="72" />
                        </div>
                      </div>
                    </section>

                    <div class="workbench-split-handle" role="separator" tabindex="0" :aria-label="t('resizeTimeline')" @pointerdown.prevent="startWorkbenchSplitResize"></div>
                    <section v-if="activeWorkbench.capabilities?.cut" class="surface-panel workbench-editor cut-workbench-editor">
                      <div class="panel-heading"><div><h2>{{ t('cutSegments') }}</h2><span>{{ workbenchDraft.mode === 'whole' ? t('wholeMode') : t('partitionedMode') }} · {{ workbenchDraft.segments.length }} {{ t('itemCount') }}</span></div><el-button v-if="demoMode || activeWorkbench.work_item.available_actions?.includes('save_draft')" plain :loading="saving" :disabled="!cutDraftSaveValid()" @click="saveWorkbenchDraft">{{ t('saveDraft') }}</el-button></div>
                      <div class="segment-list cut-window-list" ref="cutListViewport" @scroll.passive="onCutListScroll">
                        <div class="cut-window-list-spacer" :style="{ height: visibleCutList.height + 'px' }">
                        <div v-for="row in visibleCutList.items" :id="'cut-segment-' + row.index" :key="row.segment.id" class="segment-editor-row cut-window-row" :class="{ 'is-too-long': cutSegmentTooLong(row.segment), 'is-selected': activeCutSegmentIndex === row.index, 'is-excluded': row.segment.eligibility === 'excluded' }" :style="{ top: row.top + 'px' }" @click="selectCutSegment(row.index, { seek: true })">
                          <div class="segment-editor-heading"><strong>#{{ row.index + 1 }}</strong><span>{{ formatRelativeTimestamp(row.segment.start_ns) }} - {{ formatRelativeTimestamp(row.segment.end_ns) }}</span><span class="cut-window-duration">{{ cutWindowDuration(row.segment) }}</span></div>
                          <span class="cut-boundary-origin">{{ cutSegmentBoundaryLabel(row.segment, row.index) }} · {{ t('boundaryTime') }} {{ formatRelativeTimestamp(row.segment.end_ns) }}</span>
                          <div class="cut-segment-eligibility"><el-radio-group :model-value="row.segment.eligibility" size="small" @change="setCutEligibility(row.index, $event)"><el-radio-button label="included">{{ t('segmentIncluded') }}</el-radio-button><el-radio-button label="excluded">{{ t('segmentExcluded') }}</el-radio-button></el-radio-group><el-select v-if="row.segment.eligibility === 'excluded'" :model-value="row.segment.exclusion_reason || ''" clearable :placeholder="t('exclusionReason')" @change="setCutExclusionReason(row.index, $event)"><el-option :label="t('reasonOffTask')" value="off_task" /><el-option :label="t('reasonIdleOrSetup')" value="idle_or_setup" /><el-option :label="t('reasonPrivacySensitive')" value="privacy_sensitive" /><el-option :label="t('reasonOther')" value="other" /></el-select></div>
                        </div>
                        </div>
                        <div v-if="workbenchDraft.note" class="workbench-list-note"><strong>{{ t('cutNote') }}</strong><span>{{ workbenchDraft.note }}</span></div>
                      </div>
                      <div class="workbench-note"><label><span>{{ t('cutNote') }}</span><el-input v-model="workbenchDraft.note" type="textarea" :rows="3" maxlength="2000" show-word-limit @input="markWorkbenchDraftDirty" /></label></div>
                    </section>

                    <section v-else-if="activeWorkbench.capabilities?.annotation" class="surface-panel workbench-editor">
                      <div class="panel-heading"><div><h2>{{ t('annotationSegments') }}</h2><span>{{ workbenchDraft.segments.length }} {{ t('itemCount') }}</span></div><div class="panel-actions"><el-button v-if="demoMode || activeWorkbench.work_item.available_actions?.includes('save_draft')" plain :loading="saving" :disabled="!annotationDraftSaveValid()" @click="saveWorkbenchDraft">{{ t('saveDraft') }}</el-button><el-button type="primary" :disabled="saving" @click="addAnnotationSegment">{{ t('addSegment') }}</el-button></div></div>
                      <div ref="annotationListViewport" class="segment-list annotation-segment-list annotation-virtual-list" :class="{ 'has-detail-open': annotationDetailOpen }" @scroll.passive="onAnnotationListScroll">
                        <div class="annotation-list-spacer" :style="{ height: visibleAnnotationList.height + 'px' }">
                        <div v-for="row in visibleAnnotationList.items" :key="row.segment.id || row.index" class="segment-editor-row" :class="{ 'is-selected': activeAnnotationSegmentIndex === row.index }" :style="{ top: row.top + 'px' }" @click="selectAnnotationSegment(row.index)">
                          <div class="segment-editor-heading"><strong>#{{ row.index + 1 }}</strong><span>{{ formatRelativeTimestamp(row.segment.start_ns) }} - {{ formatRelativeTimestamp(row.segment.end_ns) }}</span><el-button plain type="danger" size="small" :disabled="saving" @click="removeWorkbenchSegment(row.index)">{{ t('remove') }}</el-button></div>
                          <div class="segment-fields">
                            <label><span>{{ t('segmentStart') }}</span><div class="friendly-boundary"><strong>{{ formatRelativeTimestamp(row.segment.start_ns) }}</strong><el-button :disabled="saving" @click="setCutBoundary(row.segment, 'start_ns')">I · {{ t('setStart') }}</el-button></div></label>
                            <label><span>{{ t('segmentEnd') }}</span><div class="friendly-boundary"><strong>{{ formatRelativeTimestamp(row.segment.end_ns) }}</strong><el-button :disabled="saving" @click="setCutBoundary(row.segment, 'end_ns')">O · {{ t('setEnd') }}</el-button></div></label>
                          </div>
                          <label class="annotation-description" @click.stop><span>{{ t('segmentContent') }} <span class="required-mark">*</span></span><el-input v-model="row.segment.description" type="textarea" :rows="2" maxlength="2000" show-word-limit @input="markWorkbenchDraftDirty" /></label>
                        </div>
                        </div>
                        <div v-if="workbenchDraft.note" class="workbench-list-note"><strong>{{ t('annotationNote') }}</strong><span>{{ workbenchDraft.note }}</span></div>
                        <el-empty v-if="!workbenchDraft.segments.length" :description="t('noCutSegments')" :image-size="64" />
                      </div>
                      <div class="annotation-detail-drawer" :class="{ 'is-open': annotationDetailOpen }">
                        <button type="button" class="annotation-detail-toggle" @click="toggleAnnotationDetail"><span>{{ t('rating') }} / {{ t('annotationNote') }} / {{ t('aiSuggestions') }}</span><i>{{ annotationDetailOpen ? '⌄' : '⌃' }}</i></button>
                        <div v-show="annotationDetailOpen" class="annotation-detail-body">
                          <div class="annotation-context">
                            <label><span>{{ t('outcome') }}</span><el-radio-group v-model="workbenchDraft.outcome" @change="markWorkbenchDraftDirty"><el-radio-button label="success">{{ t('outcomeSuccess') }}</el-radio-button><el-radio-button label="failure">{{ t('outcomeFailure') }}</el-radio-button><el-radio-button label="unknown">{{ t('outcomeUnknown') }}</el-radio-button></el-radio-group></label>
                            <label><span>{{ t('rating') }}</span><el-rate v-model="workbenchDraft.rating" :max="5" show-score @change="markWorkbenchDraftDirty" /></label>
                            <label class="annotation-note-field"><span>{{ t('annotationNote') }}</span><el-input v-model="workbenchDraft.note" type="textarea" :rows="2" maxlength="2000" show-word-limit @input="markWorkbenchDraftDirty" /></label>
                          </div>
                          <div class="ai-suggestion-area">
                            <div class="ai-suggestion-header"><div><strong>{{ t('aiSuggestions') }}</strong><span>{{ aiSuggestions.capability?.eligible ? selectedAiTopic : t('aiUnavailable') }}</span></div><div class="panel-actions"><el-select v-if="aiSuggestions.capability?.rgb_topics?.length" v-model="selectedAiTopic" :disabled="!aiSuggestions.capability?.eligible"><el-option v-for="topic in aiSuggestions.capability.rgb_topics" :key="topic" :label="topic" :value="topic" /></el-select><el-button type="primary" :loading="saving || loading.ai" :disabled="!aiSuggestions.capability?.eligible" @click="requestAiSuggestion">{{ t('generateAiSuggestions') }}</el-button></div></div>
                            <div v-if="aiSuggestions.items.length" class="ai-suggestion-list"><div v-for="job in aiSuggestions.items" :key="job.id" class="ai-suggestion-row"><div><el-tag size="small" effect="plain" :type="aiStatusType(job.status)">{{ job.status }}</el-tag><span>{{ job.progress_percent }}%</span></div><div class="ai-suggestion-actions"><el-button v-if="job.status === 'succeeded' && job.segments?.length" link type="primary" :disabled="saving" @click="applyAiSuggestion(job)">{{ t('applyAiSuggestion') }}</el-button><el-button v-else-if="['failed', 'cancelled'].includes(job.status)" link type="primary" :disabled="saving || !aiSuggestions.capability?.eligible" @click="retryAiSuggestion(job)">{{ t('retry') }}</el-button><span v-else class="muted">{{ job.phase || t('suggestionStatus') }}</span></div></div></div>
                          </div>
                        </div>
                      </div>
                    </section>

                    <section v-else-if="activeWorkbench.capabilities?.review" class="surface-panel workbench-editor">
                      <div class="panel-heading"><div><h2>{{ t('reviewTarget') }}</h2><span>{{ activeWorkbench.review_target?.kind || '—' }}</span></div></div>
                      <div v-if="activeWorkbench.review_target" class="segment-list review-segment-list" :class="{ 'has-detail-open': reviewDetailOpen }">
                        <div v-for="(segment, index) in activeWorkbench.review_target.payload?.segments || []" :key="segment.id || index" class="segment-editor-row review-segment-row" :class="{ 'is-selected': activeAnnotationSegmentIndex === index }" @click="selectReviewSegment(index)">
                          <div class="segment-editor-heading"><strong>#{{ index + 1 }}</strong><span>{{ formatRelativeTimestamp(segment.start_ns) }} - {{ formatRelativeTimestamp(segment.end_ns) }}</span><el-tag v-if="activeWorkbench.review_target.kind === 'cut'" size="small" effect="plain" :type="segment.eligibility === 'excluded' ? 'info' : 'success'">{{ segment.eligibility === 'excluded' ? t('segmentExcluded') : t('segmentIncluded') }}</el-tag></div>
                          <p v-if="segment.eligibility === 'excluded' && segment.exclusion_reason" class="review-description">{{ exclusionReasonLabel(segment.exclusion_reason) }}</p>
                          <p v-if="segment.description" class="review-description">{{ segment.description }}</p>
                        </div>
                        <dl v-if="activeWorkbench.review_target.kind === 'annotation'" class="review-summary">
                          <dt>{{ t('outcome') }}</dt><dd>{{ outcomeLabel(activeWorkbench.review_target.payload?.outcome) }}</dd>
                          <dt>{{ t('collector') }}</dt><dd>{{ collectorAttributionLabel(activeWorkbench.episode?.collector_attribution) }}</dd>
                          <dt>{{ t('collectionDevice') }}</dt><dd>{{ collectionDeviceAttributionLabel(activeWorkbench.episode?.device_attribution) }}</dd>
                        </dl>
                        <p v-if="activeWorkbench.review_target.payload?.note" class="review-note-readonly">{{ activeWorkbench.review_target.payload.note }}</p>
                      </div>
                      <el-empty v-else :description="t('workbenchUnavailable')" :image-size="64" />
                      <div class="annotation-detail-drawer review-detail-drawer" :class="{ 'is-open': reviewDetailOpen }">
                        <button type="button" class="annotation-detail-toggle review-detail-toggle" @click="toggleReviewDetail"><span>{{ t('reviewNote') }} / {{ t('rating') }}</span><i>{{ reviewDetailOpen ? '⌄' : '⌃' }}</i></button>
                        <div v-show="reviewDetailOpen" class="annotation-detail-body review-detail-body">
                          <div class="review-controls">
                            <label><span>{{ t('reviewNote') }}</span><el-input v-model="reviewForm.note" type="textarea" :rows="3" maxlength="2000" show-word-limit :disabled="saving || !activeWorkbench.work_item.available_actions?.includes('review')" /></label>
                            <label><span>{{ t('rating') }}</span><el-rate v-model="reviewForm.rating" :max="5" show-score :disabled="saving || !activeWorkbench.work_item.available_actions?.includes('review')" /></label>
                          </div>
                          <div class="review-actions"><el-button type="danger" plain :disabled="saving || !activeWorkbench.work_item.available_actions?.includes('review')" @click="submitReviewDecision('rejected')">{{ t('reviewReject') }}</el-button><el-button type="success" :loading="saving" :disabled="!activeWorkbench.work_item.available_actions?.includes('review')" @click="submitReviewDecision('accepted')">{{ t('reviewAccept') }}</el-button></div>
                        </div>
                      </div>
                    </section>

                    <section v-else class="surface-panel workbench-editor"><el-empty :description="t('workbenchUnavailable')" /></section>

                    <section class="surface-panel studio-timeline" :style="{ height: timelinePanelHeight + 'px' }">
                      <div class="timeline-resize-handle" role="separator" tabindex="0" :aria-label="t('resizeTimeline')" @pointerdown.prevent="startTimelineResize"></div>
                      <div class="panel-heading timeline-toolbar">
                        <div class="timeline-timecode"><span ref="timelineTimecode">{{ formatRelativeTimestamp(cutPlayheadTimestamp) }}</span></div>
                        <div class="timeline-tool-actions">
                          <el-tooltip :content="t('shortcutHelp')"><button type="button" class="timeline-icon-button" :aria-label="t('shortcutHelp')" @click="showShortcutHelp = true" v-html="timelineIcon('keyboard')"></button></el-tooltip>
                          <el-tooltip :content="t('stepBackwardLarge')"><button type="button" class="timeline-icon-button" @click="stepWorkbenchPlayback(-1, 'seconds5')" v-html="timelineIcon('backLarge')"></button></el-tooltip>
                          <el-tooltip :content="t('stepBackward')"><button type="button" class="timeline-icon-button" @click="stepWorkbenchPlayback(-1, 'seconds1')" v-html="timelineIcon('back')"></button></el-tooltip>
                          <el-tooltip :content="workbenchPlaying ? t('pause') : t('play')"><button type="button" class="timeline-icon-button is-primary" @click="toggleWorkbenchPlayback" v-html="timelineIcon(workbenchPlaying ? 'pause' : 'play')"></button></el-tooltip>
                          <el-tooltip :content="t('stepForward')"><button type="button" class="timeline-icon-button" @click="stepWorkbenchPlayback(1, 'seconds1')" v-html="timelineIcon('forward')"></button></el-tooltip>
                          <el-tooltip :content="t('stepForwardLarge')"><button type="button" class="timeline-icon-button" @click="stepWorkbenchPlayback(1, 'seconds5')" v-html="timelineIcon('forwardLarge')"></button></el-tooltip>
                          <div class="timeline-volume-control">
                            <el-tooltip :content="workbenchMuted ? t('unmute') : t('mute')"><button type="button" class="timeline-icon-button" @click="toggleWorkbenchMute" v-html="timelineIcon(workbenchMuted ? 'mute' : 'volume')"></button></el-tooltip>
                            <div class="timeline-volume-popover"><el-slider :model-value="workbenchVolume" :min="0" :max="1" :step="0.05" :show-tooltip="false" class="timeline-volume-slider" :aria-label="t('mute')" @input="setWorkbenchVolume" /></div>
                          </div>
                          <el-select :model-value="workbenchPlaybackRate" size="small" class="timeline-rate-select" :aria-label="t('playbackRate')" @change="setWorkbenchPlaybackRate"><el-option label="0.5×" :value="0.5" /><el-option label="1×" :value="1" /><el-option label="1.5×" :value="1.5" /><el-option label="2×" :value="2" /><el-option label="3×" :value="3" /><el-option label="4×" :value="4" /><el-option label="6×" :value="6" /><el-option label="8×" :value="8" /></el-select>
                          <el-tooltip v-if="activeWorkbench.capabilities?.cut" :content="t('addBoundary')"><button type="button" class="timeline-icon-button" :disabled="saving || !exactPlaybackTimelineAvailable()" @click="addCutBoundary" v-html="timelineIcon('add')"></button></el-tooltip>
                          <el-tooltip v-if="activeWorkbench.capabilities?.cut" :content="t('selectBoundary')"><button type="button" class="timeline-icon-button" :disabled="workbenchDraft.segments.length < 2" @click="selectNearestCutBoundary" v-html="timelineIcon('select')"></button></el-tooltip>
                          <el-tooltip v-if="activeWorkbench.capabilities?.cut" :content="t('restoreQrBoundary')"><button type="button" class="timeline-icon-button" :disabled="saving || selectedCutBoundary === null || workbenchDraft.segments[selectedCutBoundary]?.boundary_after?.origin !== 'qr_event'" @click="restoreSelectedQrBoundary" v-html="timelineIcon('restore')"></button></el-tooltip>
                          <el-tooltip v-if="activeWorkbench.capabilities?.cut" :content="t('deleteBoundary')"><button type="button" class="timeline-icon-button is-danger" :disabled="saving || selectedCutBoundary === null" @click="deleteSelectedCutBoundary" v-html="timelineIcon('trash')"></button></el-tooltip>
                          <el-tooltip v-if="activeWorkbench.capabilities?.annotation" :content="t('addSegment')"><button type="button" class="timeline-icon-button" :disabled="saving" @click="addAnnotationSegment" v-html="timelineIcon('add')"></button></el-tooltip>
                          <el-tooltip :content="timelineSnapEnabled ? t('snapOn') : t('snapOff')"><button type="button" class="timeline-icon-button" :class="{ 'is-primary': timelineSnapEnabled }" @click="timelineSnapEnabled = !timelineSnapEnabled">S</button></el-tooltip>
                          <el-tooltip :content="t('undo') + ' Ctrl/Cmd+Z'"><button type="button" class="timeline-icon-button" :disabled="!canUndoWorkbench" @click="undoWorkbench" v-html="timelineIcon('undo')"></button></el-tooltip>
                          <el-tooltip :content="t('redo') + ' Ctrl/Cmd+Y'"><button type="button" class="timeline-icon-button" :disabled="!canRedoWorkbench" @click="redoWorkbench" v-html="timelineIcon('redo')"></button></el-tooltip>
                          <el-tooltip v-if="activeWorkbench.capabilities?.annotation" :content="t('copy') + ' Ctrl/Cmd+C'"><button type="button" class="timeline-icon-button" :disabled="!workbenchDraft.segments.length" @click="copyWorkbenchSelection" v-html="timelineIcon('copy')"></button></el-tooltip>
                          <el-tooltip v-if="activeWorkbench.capabilities?.annotation" :content="t('paste') + ' Ctrl/Cmd+V'"><button type="button" class="timeline-icon-button" :disabled="!canPasteWorkbench" @click="pasteWorkbenchSelection" v-html="timelineIcon('paste')"></button></el-tooltip>
                          <el-divider direction="vertical" />
                          <el-tooltip :content="t('zoomOut')"><button type="button" class="timeline-icon-button" :disabled="timelineZoom <= 1" @click="zoomWorkbenchTimeline(-1)" v-html="timelineIcon('zoomOut')"></button></el-tooltip>
                          <el-slider v-model="timelineZoom" class="timeline-zoom-slider" :min="1" :max="12" :step="1" :show-tooltip="false" :aria-label="t('zoomIn')" />
                          <el-tooltip :content="t('zoomIn')"><button type="button" class="timeline-icon-button" :disabled="timelineZoom >= 12" @click="zoomWorkbenchTimeline(1)" v-html="timelineIcon('zoomIn')"></button></el-tooltip>
                          <el-tooltip :content="t('fullscreen')"><button type="button" class="timeline-icon-button" @click="openWorkbenchFullscreen" v-html="timelineIcon('fullscreen')"></button></el-tooltip>
                        </div>
                      </div>
                      <div class="cut-timeline-editor">
                        <div ref="timelineScrollViewport" class="timeline-scroll-viewport" @wheel="handleTimelineWheel" @scroll.passive="onTimelineScroll">
                          <div class="timeline-canvas" :style="timelineCanvasStyle()">
                          <div class="timeline-ruler" aria-hidden="true" @click="seekWorkbenchFromTimelineEvent($event, $event.currentTarget.parentElement)">
                            <span v-for="tick in timelineTicks()" :key="tick.key" class="timeline-tick" :class="{ 'is-major': tick.major }" :style="{ left: tick.left }"><small v-if="tick.label">{{ tick.label }}</small></span>
                          </div>
                          <div ref="cutTimelineTrack" class="cut-timeline-track" :class="{ 'is-disabled': !exactPlaybackTimelineAvailable() }" @click="seekCutTimeline" @pointerdown="startCutTrackPointer" @mousemove="updateTimelineHover" @mouseleave="clearTimelineHover">
                            <canvas ref="cutTimelineCanvas" class="cut-timeline-canvas" aria-hidden="true"></canvas>
                            <template v-if="activeWorkbench.capabilities?.review">
                              <button v-for="item in visibleCutTrackWindows" :key="item.segment.id || item.index" type="button" class="cut-window" :class="{ 'is-whole': activeWorkbench.review_target?.payload?.mode === 'whole', 'is-excluded': item.segment.eligibility === 'excluded', 'is-selected': activeAnnotationSegmentIndex === item.index }" :style="cutSegmentStyle(item.segment)" @click.stop="selectReviewSegment(item.index)"><span>{{ item.index + 1 }}</span></button>
                            </template>
                            <template v-else-if="activeWorkbench.capabilities?.annotation">
                              <div v-for="item in visibleCutTrackWindows" :key="'manual-' + (item.segment.id || item.index)" class="annotation-window" :class="{ 'is-selected': activeAnnotationSegmentIndex === item.index, 'is-incomplete': !item.segment.end_ns }" :style="cutSegmentStyle(item.segment)" :title="t('dragSegment')" @click.stop="selectAnnotationSegment(item.index)" @pointerdown.stop.prevent="startAnnotationSegmentDrag(item.index, $event)"><span>{{ item.index + 1 }}</span><button type="button" class="annotation-handle is-start" :aria-label="t('setStart')" @pointerdown.stop.prevent="startAnnotationBoundaryDrag(item.index, 'start_ns', $event)"></button><button v-if="item.segment.end_ns" type="button" class="annotation-handle is-end" :aria-label="t('setEnd')" @pointerdown.stop.prevent="startAnnotationBoundaryDrag(item.index, 'end_ns', $event)"></button></div>
                              <template v-for="job in aiSuggestions.items" :key="'ai-track-' + job.id"><div v-for="(segment, index) in job.status === 'succeeded' ? job.segments || [] : []" :key="'ai-' + job.id + '-' + index" class="ai-annotation-window" :style="cutSegmentStyle(segment)"></div></template>
                            </template>
                            <template v-else>
                              <button v-for="item in visibleCutTrackWindows" :key="item.segment.id" type="button" class="cut-window" :class="{ 'is-whole': workbenchDraft.mode === 'whole', 'is-too-long': cutSegmentTooLong(item.segment), 'is-excluded': item.segment.eligibility === 'excluded', 'is-selected': activeCutSegmentIndex === item.index }" :style="cutSegmentStyle(item.segment)" @click.stop="selectCutSegment(item.index, { seek: true })"><span>{{ item.index + 1 }}</span></button>
                              <button v-for="item in visibleCutTrackBoundaries" :key="'boundary-' + item.index" type="button" class="cut-boundary" :class="{ 'is-selected': selectedCutBoundary === item.index, 'is-qr': item.segment?.boundary_after?.origin === 'qr_event', 'is-adjusted': item.segment?.boundary_after?.adjusted }" :style="cutBoundaryStyle(item.index)" :aria-label="t('dragBoundary')" :title="cutBoundaryOrigin(item.index)" :disabled="saving || !exactPlaybackTimelineAvailable()" @click.stop="selectCutBoundary(item.index, { recenter: true })" @pointerdown.stop.prevent="startCutBoundaryDrag(item.index, $event)"><span><i></i></span></button>
                            </template>
                            <i ref="cutPlayheadLine" v-show="cutPlayheadTimestamp" class="cut-progress-line"></i><button ref="cutPlayheadPin" v-show="cutPlayheadTimestamp" type="button" class="cut-playhead-pin" :aria-label="formatRelativeTimestamp(cutPlayheadTimestamp)" @pointerdown.stop.prevent="startPlayheadDrag"></button>
                            <div v-show="timelineHover.visible" ref="timelineHoverPreview" class="timeline-hover-preview"><video ref="timelineHoverVideo" muted preload="metadata" :src="activeWorkbench.media?.preview?.url" :aria-label="t('previewFrame')"></video><strong ref="timelineHoverLabel">{{ formatRelativeTimestamp(timelineHover.timestamp_ns) }}</strong></div>
                          </div>
                          <i v-show="timelineHover.visible" class="timeline-hover-line" :style="{ left: timelineHover.left + 'px' }" aria-hidden="true"></i>
                          </div>
                        </div>
                        <p v-if="!exactPlaybackTimelineAvailable()" class="cut-timeline-warning">{{ t('timelineMappingUnavailable') }}</p>
                        <p v-else-if="activeWorkbench.capabilities?.cut && !cutDraftSaveValid()" class="cut-timeline-warning is-error">{{ t('partitionInvalid') }}</p>
                        <p v-else-if="activeWorkbench.capabilities?.cut && !cutDraftSubmitValid()" class="cut-timeline-warning is-error">{{ t('submitCutTooLong') }}</p>
                      </div>
                    </section>
                  </div>
                    </div>
                </template>
                <el-empty v-else :description="t('workbenchUnavailable')" />
              </section>

              <section v-else-if="activeView === 'resources'" class="view-stack" v-loading="loading.resources">
                <div class="page-actions collection-scope-actions">
                  <label><span>{{ t('workspace') }}</span><el-select v-model="selectedWorkspaceId" size="small" @change="switchWorkspace" :placeholder="t('workspace')"><el-option v-for="item in availableScopeWorkspaces" :key="item.id" :label="item.name" :value="item.id" /></el-select></label>
                  <label><span>{{ t('collectProject') }}</span><el-select v-model="miningProjectScope" size="small" multiple collapse-tags collapse-tags-tooltip clearable :disabled="!selectedWorkspaceId" :placeholder="t('collectProject')" @change="switchCollectionProjects"><el-option v-for="item in collectionProjectOptions" :key="item.id" :label="item.name" :value="item.name" /></el-select></label>
                  <el-button @click="loadCollectionResources">{{ t('refresh') }}</el-button><el-button v-if="canManageWorkspace" @click="openCollectorDialog">{{ t('addCollector') || t('createCollector') }}</el-button><el-button v-if="canManageWorkspace" type="primary" @click="openDeviceDialog">{{ t('createDevice') }}</el-button>
                </div>
                <div class="resource-directory-grid">
                  <section class="surface-panel table-panel"><div class="panel-heading"><div><h2>{{ t('collector') }}</h2><span>{{ collectorProfiles.length }} {{ t('itemCount') }}</span></div></div><el-table v-if="collectorProfiles.length" :data="collectorProfiles" class="data-table collector-resource-table" fit><el-table-column :label="t('collector')" min-width="180"><template #default="scope"><strong>{{ collectorDisplayLabel(scope.row) }}</strong></template></el-table-column><el-table-column :label="t('collectorNumber')" width="110"><template #default="scope"><code>{{ scope.row.profile_key || '—' }}</code></template></el-table-column><el-table-column :label="t('status')" width="100"><template #default="scope"><el-tag size="small" effect="plain" :type="scope.row.is_active ? 'success' : 'info'">{{ scope.row.is_active ? t('active') : t('inactive') }}</el-tag></template></el-table-column><el-table-column :label="t('qrCodes')" width="112"><template #default="scope"><el-button link type="primary" @click="openCollectorQrDialog(scope.row)">{{ t('qrCodes') }}</el-button></template></el-table-column><el-table-column v-if="canManageWorkspace" :label="t('actions') || t('status')" width="170" fixed="right"><template #default="scope"><el-button plain size="small" :type="scope.row.is_active ? 'danger' : 'primary'" :disabled="saving" @click="setCollectorProfileActive(scope.row, !scope.row.is_active)">{{ scope.row.is_active ? t('deactivate') : t('activate') }}</el-button><el-button plain size="small" type="danger" :disabled="saving" @click="handleRevokeCollector(scope.row)">{{ t('revokeCollectorMember') }}</el-button></template></el-table-column></el-table><el-empty v-else :description="t('emptyCollectors')" :image-size="64" /></section>
                  <section class="surface-panel table-panel"><div class="panel-heading"><div><h2>{{ t('collectionDevice') }}</h2><span>{{ collectionDevices.length }} {{ t('itemCount') }}</span></div></div><el-table v-if="collectionDevices.length" :data="collectionDevices" class="data-table" fit><el-table-column prop="name" :label="t('deviceName')" min-width="180" /><el-table-column prop="device_type" :label="t('deviceType')" min-width="130" /><el-table-column prop="model" :label="t('deviceModel')" min-width="140"><template #default="scope">{{ scope.row.model || '—' }}</template></el-table-column><el-table-column prop="serial_number" :label="t('serialNumber')" min-width="150" /><el-table-column :label="t('status')" width="110"><template #default="scope"><el-tag size="small" effect="plain" :type="scope.row.is_active ? 'success' : 'info'">{{ scope.row.is_active ? t('active') : t('inactive') }}</el-tag></template></el-table-column><el-table-column v-if="canManageWorkspace" :label="t('status')" width="110"><template #default="scope"><el-button plain size="small" :type="scope.row.is_active ? 'danger' : 'primary'" :disabled="saving" @click="setCollectionDeviceActive(scope.row, !scope.row.is_active)">{{ scope.row.is_active ? t('deactivate') : t('activate') }}</el-button></template></el-table-column></el-table><el-empty v-else :description="t('emptyDevices')" :image-size="64" /></section>
                </div>
              </section>

              <section v-else-if="activeView === 'admin' && canManageUsers" class="view-stack" v-loading="loading.users || loading.members">
                <div class="page-actions"><el-button @click="loadAdmin">{{ t('refresh') }}</el-button><el-button type="primary" @click="openManagedUserDialog">{{ t('createManagedUser') }}</el-button><el-button v-if="canManageWorkspace" type="primary" @click="showWorkspaceDialog = true">{{ t('createWorkspace') }}</el-button></div>
                <section class="surface-panel settings-panel">
                  <el-tabs v-model="adminActiveTab" class="settings-tabs">
                    <el-tab-pane :label="t('userManagement')" name="users">
                <section class="surface-panel table-panel">
                  <el-table v-if="managedUsers.length" :data="managedUsers" class="data-table" fit>
                    <el-table-column prop="email" :label="t('email')" min-width="270" />
                    <el-table-column :label="t('userRole')" min-width="180"><template #default="scope"><el-select :model-value="scope.row.role" size="small" :disabled="saving || String(scope.row.id) === String(user?.id)" @change="changeManagedUserRole(scope.row, $event)"><el-option v-for="role in assignableRoles" :key="role" :label="roleLabel(role)" :value="role" /></el-select></template></el-table-column>
                    <el-table-column :label="t('status')" width="120"><template #default="scope"><el-tag size="small" effect="plain" :type="scope.row.is_active ? 'success' : 'info'">{{ scope.row.is_active ? t('active') : t('inactive') }}</el-tag></template></el-table-column>
                    <el-table-column :label="t('createdAt')" min-width="180"><template #default="scope">{{ formatDate(scope.row.created_at) }}</template></el-table-column>
                    <el-table-column :label="t('status')" width="170" fixed="right"><template #default="scope"><div style="display: inline-flex; align-items: center; gap: 6px;"><el-button plain size="small" :type="scope.row.is_active ? 'danger' : 'primary'" :disabled="saving || String(scope.row.id) === String(user?.id)" @click="setManagedUserActive(scope.row, !scope.row.is_active)">{{ scope.row.is_active ? t('deactivate') : t('activate') }}</el-button><el-button plain size="small" type="warning" :disabled="saving || String(scope.row.id) === String(user?.id)" @click="resetManagedUserPassword(scope.row)">{{ t('resetPassword') }}</el-button></div></template></el-table-column>
                  </el-table>
                  <el-empty v-else :description="t('emptyManagedUsers')" />
                </section>
                    </el-tab-pane>
                    <el-tab-pane :label="t('workspaceMembers')" name="members">
                <section class="surface-panel table-panel">
                  <div class="panel-heading admin-members-heading">
                    <div class="admin-members-scope">
                      <el-select class="admin-members-workspace-select" v-model="managementWorkspaceId" :placeholder="t('workspace')" @change="loadManagementWorkspaceMembers"><el-option v-for="item in managementWorkspaces" :key="item.id" :label="item.name" :value="item.id" /></el-select>
                    </div>
                    <div class="panel-actions"><el-button @click="loadManagementWorkspaceMembers">{{ t('refresh') }}</el-button><el-button type="primary" style="color:#fff" :disabled="!managementWorkspaceId || !availableWorkspaceMembers.length" @click="openWorkspaceMemberDialog">{{ t('grantWorkspaceMember') }}</el-button></div>
                  </div>
                  <el-table v-if="managementMembers.length" :data="managementMembers" class="data-table">
                    <el-table-column prop="email" :label="t('email')" min-width="270" />
                    <el-table-column :label="t('userRole')" width="150"><template #default="scope">{{ roleLabel(scope.row.role) }}</template></el-table-column>
                    <el-table-column :label="t('status')" width="130" fixed="right"><template #default="scope"><el-button plain size="small" type="danger" :disabled="saving" @click="revokeWorkspaceMember(scope.row)">{{ t('revokeWorkspaceMember') }}</el-button></template></el-table-column>
                  </el-table>
                  <el-empty v-else :description="t('emptyWorkspaceMembers')" />
                </section>
                    </el-tab-pane>
                  </el-tabs>
                </section>
              </section>

              <section v-else-if="activeView === 'settings' && canView('settings')" class="view-stack" v-loading="loadingSettingsCenter">
                <div class="page-actions settings-center-actions collection-scope-actions">
                  <label><span>{{ t('workspace') }}</span><el-select v-model="selectedWorkspaceId" size="small" @change="switchWorkspace" :placeholder="t('workspace')"><el-option v-for="item in availableScopeWorkspaces" :key="item.id" :label="item.name" :value="item.id" /></el-select></label>
                  <el-button :disabled="loadingSettingsCenter" @click="loadSettingsCenterData">{{ t('refresh') }}</el-button>
                  <el-button v-if="settingsActiveTab === 'projects'" type="primary" :disabled="!collectionProjectCreateWritable" @click="openCreateCollectionProjectDialog">{{ t('createCollectionProject') }}</el-button>
                </div>
                <el-alert v-if="settingsCenterError" :title="settingsCenterError" type="error" :closable="false" show-icon>
                  <el-button size="small" :disabled="loadingSettingsCenter" @click="loadSettingsCenterData">{{ t('settingsLoadRetry') }}</el-button>
                </el-alert>
                <section class="surface-panel settings-panel">
                  <el-tabs v-model="settingsActiveTab" class="settings-tabs">
                    <el-tab-pane :label="t('settingsProjects')" name="projects">
                      <el-table v-if="settingsProjectRows.length" :data="settingsProjectRows" class="data-table" fit>
                        <el-table-column prop="name" :label="t('batchName')" min-width="180">
                          <template #default="scope"><strong>{{ scope.row.name }}</strong></template>
                        </el-table-column>
                        <el-table-column prop="description" :label="t('description')" min-width="220" show-overflow-tooltip>
                          <template #default="scope">{{ scope.row.description || '—' }}</template>
                        </el-table-column>
                        <el-table-column prop="status" :label="t('status')" width="110">
                          <template #default="scope">
                            <el-tag size="small" effect="plain" :type="scope.row.status === 'archived' ? 'info' : 'success'">{{ scope.row.status === 'archived' ? t('archived') : t('active') }}</el-tag>
                          </template>
                        </el-table-column>
                        <el-table-column :label="t('createdAt')" min-width="170">
                          <template #default="scope">{{ formatDate(scope.row.created_at) }}</template>
                        </el-table-column>
                        <el-table-column :label="t('actions')" width="160" fixed="right">
                          <template #default="scope">
                            <el-button link type="primary" :disabled="!settingsCenterWritable" @click="openEditCollectionProjectDialog(scope.row)">{{ t('editCollectionProject') }}</el-button>
                            <el-button v-if="scope.row.status !== 'archived'" plain size="small" type="danger" :disabled="!settingsCenterWritable" @click="handleArchiveCollectionProject(scope.row)">{{ t('archiveCollectionProject') }}</el-button>
                          </template>
                        </el-table-column>
                      </el-table>
                      <el-empty v-else :description="t('emptyProjects')" :image-size="64" />
                    </el-tab-pane>

                    <el-tab-pane :label="t('settingsLabels')" name="labels">
                      <div class="settings-label-categories-grid">
                        <div class="settings-label-card">
                          <div class="settings-label-card-header">
                            <h3>{{ t('sceneLabels') }}</h3>
                            <el-tag size="small" type="info">{{ collectionLabels.filter(l => l.category === 'scene' && l.is_active !== false).length }}</el-tag>
                          </div>
                          <div class="settings-label-card-body">
                            <div class="settings-tag-list">
                              <el-tag
                                v-for="tag in collectionLabels.filter(l => l.category === 'scene')"
                                :key="tag.id"
                                :closable="tag.is_active !== false && settingsCenterWritable"
                                :type="tag.is_active === false ? 'info' : 'primary'"
                                effect="plain"
                                @close="handleDeactivateCollectionLabel(tag)"
                              >
                                {{ tag.name }}<span v-if="tag.is_active === false"> ({{ t('inactive') }})</span>
                              </el-tag>
                              <span v-if="!collectionLabels.some(l => l.category === 'scene')" class="muted">{{ t('emptyLabels') }}</span>
                            </div>
                            <div class="settings-quick-add">
                              <el-input v-model="newLabelInputs.scene" :placeholder="t('labelName')" size="small" @keyup.enter="handleCreateCollectionLabel('scene')">
                                <template #append>
                                  <el-button :loading="creatingLabel.scene" :disabled="!settingsCenterWritable" @click="handleCreateCollectionLabel('scene')">{{ t('addLabel') }}</el-button>
                                </template>
                              </el-input>
                            </div>
                          </div>
                        </div>

                        <div class="settings-label-card">
                          <div class="settings-label-card-header">
                            <h3>{{ t('purposeLabels') }}</h3>
                            <el-tag size="small" type="info">{{ collectionLabels.filter(l => l.category === 'purpose' && l.is_active !== false).length }}</el-tag>
                          </div>
                          <div class="settings-label-card-body">
                            <div class="settings-tag-list">
                              <el-tag
                                v-for="tag in collectionLabels.filter(l => l.category === 'purpose')"
                                :key="tag.id"
                                :closable="tag.is_active !== false && settingsCenterWritable"
                                :type="tag.is_active === false ? 'info' : 'success'"
                                effect="plain"
                                @close="handleDeactivateCollectionLabel(tag)"
                              >
                                {{ tag.name }}<span v-if="tag.is_active === false"> ({{ t('inactive') }})</span>
                              </el-tag>
                              <span v-if="!collectionLabels.some(l => l.category === 'purpose')" class="muted">{{ t('emptyLabels') }}</span>
                            </div>
                            <div class="settings-quick-add">
                              <el-input v-model="newLabelInputs.purpose" :placeholder="t('labelName')" size="small" @keyup.enter="handleCreateCollectionLabel('purpose')">
                                <template #append>
                                  <el-button :loading="creatingLabel.purpose" :disabled="!settingsCenterWritable" @click="handleCreateCollectionLabel('purpose')">{{ t('addLabel') }}</el-button>
                                </template>
                              </el-input>
                            </div>
                          </div>
                        </div>

                        <div class="settings-label-card">
                          <div class="settings-label-card-header">
                            <h3>{{ t('trainingLabels') }}</h3>
                            <el-tag size="small" type="info">{{ collectionLabels.filter(l => (l.category === 'training' || l.category === 'train') && l.is_active !== false).length }}</el-tag>
                          </div>
                          <div class="settings-label-card-body">
                            <div class="settings-tag-list">
                              <el-tag
                                v-for="tag in collectionLabels.filter(l => l.category === 'training' || l.category === 'train')"
                                :key="tag.id"
                                :closable="tag.is_active !== false && settingsCenterWritable"
                                :type="tag.is_active === false ? 'info' : 'warning'"
                                effect="plain"
                                @close="handleDeactivateCollectionLabel(tag)"
                              >
                                {{ tag.name }}<span v-if="tag.is_active === false"> ({{ t('inactive') }})</span>
                              </el-tag>
                              <span v-if="!collectionLabels.some(l => l.category === 'training' || l.category === 'train')" class="muted">{{ t('emptyLabels') }}</span>
                            </div>
                            <div class="settings-quick-add">
                              <el-input v-model="newLabelInputs.training" :placeholder="t('labelName')" size="small" @keyup.enter="handleCreateCollectionLabel('training')">
                                <template #append>
                                  <el-button :loading="creatingLabel.training" :disabled="!settingsCenterWritable" @click="handleCreateCollectionLabel('training')">{{ t('addLabel') }}</el-button>
                                </template>
                              </el-input>
                            </div>
                          </div>
                        </div>

                        <div class="settings-label-card">
                          <div class="settings-label-card-header">
                            <h3>{{ t('modalityLabels') }}</h3>
                            <el-tag size="small" type="info">{{ collectionLabels.filter(l => l.category === 'modality' && l.is_active !== false).length }}</el-tag>
                          </div>
                          <div class="settings-label-card-body">
                            <div class="settings-tag-list">
                              <el-tag
                                v-for="tag in collectionLabels.filter(l => l.category === 'modality')"
                                :key="tag.id"
                                :closable="tag.is_active !== false && settingsCenterWritable"
                                :type="tag.is_active === false ? 'info' : 'danger'"
                                effect="plain"
                                @close="handleDeactivateCollectionLabel(tag)"
                              >
                                {{ tag.name }}<span v-if="tag.is_active === false"> ({{ t('inactive') }})</span>
                              </el-tag>
                              <span v-if="!collectionLabels.some(l => l.category === 'modality')" class="muted">{{ t('emptyLabels') }}</span>
                            </div>
                            <div class="settings-quick-add">
                              <el-input v-model="newLabelInputs.modality" :placeholder="t('labelName')" size="small" @keyup.enter="handleCreateCollectionLabel('modality')">
                                <template #append>
                                  <el-button :loading="creatingLabel.modality" :disabled="!settingsCenterWritable" @click="handleCreateCollectionLabel('modality')">{{ t('addLabel') }}</el-button>
                                </template>
                              </el-input>
                            </div>
                          </div>
                        </div>
                      </div>
                    </el-tab-pane>
                  </el-tabs>
                </section>

              </section>

              <data-catalog v-else-if="activeView === 'assets' || activeView === 'datasets'" :key="activeView" :mode="activeView" :locale="locale" :user="user" :workspaces="workspaces" @registered="navigate('trainDatasets')" />

              <section v-else-if="activeView === 'miningTasks'" class="view-stack">
                <template v-if="!showMiningTaskPackagesPage">
                <div class="page-actions collection-scope-actions">
                  <label><span>{{ t('workspace') }}</span><el-select v-model="selectedWorkspaceId" size="small" @change="switchWorkspace" :placeholder="t('workspace')"><el-option v-for="item in availableScopeWorkspaces" :key="item.id" :label="item.name" :value="item.id" /></el-select></label>
                  <label><span>{{ t('collectProject') }}</span><el-select v-model="miningProjectScope" size="small" multiple collapse-tags collapse-tags-tooltip clearable :disabled="!selectedWorkspaceId" :placeholder="t('collectProject')" @change="switchCollectionProjects"><el-option v-for="item in collectionProjectOptions" :key="item.id" :label="item.name" :value="item.name" /></el-select></label>
                  <el-button @click="loadMiningData">{{ t('refresh') }}</el-button>
                  <el-button type="primary" @click="openMiningTaskDialog">{{ t('miningQuickCreate') }}</el-button>
                </div>
                <section class="mining-dash-stats-panel is-titleless">
                  <div class="mining-dash-stats is-cols-5">
                    <div class="mining-dash-stat"><div class="stat-label">{{ t('kpiTaskTargetLabel') }}</div><div class="stat-value">{{ miningKpiStats.totalTasks }}<span v-if="t('dashUnitCount')" class="stat-unit">{{ t('dashUnitCount') }}</span></div></div>
                    <div class="mining-dash-stat"><div class="stat-label">{{ t('kpiPendingAssignLabel') }}</div><div class="stat-value">{{ miningKpiStats.pendingAssignTasks }}<span v-if="t('dashUnitCount')" class="stat-unit">{{ t('dashUnitCount') }}</span></div></div>
                    <div class="mining-dash-stat"><div class="stat-label">{{ t('kpiQcDesensDoneLabel') }}</div><div class="stat-value">{{ miningKpiStats.batchCount }}<span v-if="t('dashUnitCount')" class="stat-unit">{{ t('dashUnitCount') }}</span></div></div>
                    <div class="mining-dash-stat"><div class="stat-label">{{ t('kpiCollectGoalLabel') }}</div><div class="stat-value">{{ formatNumber(miningKpiStats.totalTargetHours) }}<span class="stat-unit">{{ t('dashHour') }}</span></div></div>
                    <div class="mining-dash-stat"><div class="stat-label">{{ t('kpiAvgValidRateLabel') }}</div><div class="stat-value">{{ miningKpiStats.avgValidRate }}<span class="stat-unit">%</span></div></div>
                  </div>
                </section>
                <section class="surface-panel table-panel">
                  <div class="panel-heading"><div><h2>{{ t('miningTaskList') }}</h2><span>{{ filteredMiningTaskRows.length }} / {{ miningTaskRows.length }} {{ t('itemCount') }}</span></div></div>
                  <div class="list-filter-bar">
                    <el-select v-model="miningTaskFilter.name" multiple collapse-tags :placeholder="t('miningTaskName')" clearable style="min-width: 190px;"><el-option v-for="item in miningTaskNameOptions" :key="item" :label="item" :value="item" /></el-select>
                    <el-select v-model="miningTaskFilter.owner" multiple collapse-tags :placeholder="t('taskOwner')" clearable><el-option v-for="item in miningTaskOwnerOptions" :key="item" :label="item" :value="item" /></el-select>
                    <el-select v-model="miningTaskFilter.purpose" multiple collapse-tags :placeholder="t('tagPurpose')" clearable><el-option v-for="item in miningTaskPurposeOptions" :key="item" :label="item" :value="item" /></el-select>
                    <el-select v-model="miningTaskFilter.scene" multiple collapse-tags :placeholder="t('tagScene')" clearable><el-option v-for="item in miningTaskSceneOptions" :key="item" :label="item" :value="item" /></el-select>
                    <el-select v-model="miningTaskFilter.train" multiple collapse-tags :placeholder="t('tagTrain')" clearable><el-option v-for="item in miningTaskTrainOptions" :key="item" :label="item" :value="item" /></el-select>
                    <el-select v-model="miningTaskFilter.modality" multiple collapse-tags :placeholder="t('miningTaskModalities')" clearable><el-option v-for="item in miningTaskModalityOptions" :key="item" :label="batchTypeLabel(item)" :value="item" /></el-select>
                    <el-select v-model="miningTaskFilter.assignStatus" multiple collapse-tags :placeholder="t('taskAssignStatusLabel')" clearable><el-option :label="t('taskAssignDone')" value="done" /><el-option :label="t('taskAssignTodo')" value="todo" /></el-select>
                    <div class="range-filter-group"><span class="range-filter-label">{{ t('miningTaskFilterTargetHours') }}</span><el-input-number v-model="miningTaskFilter.targetHoursMin" :min="0" :controls="false" :placeholder="t('miningTaskHoursMin')" style="width: 76px;" /><span class="range-filter-sep">—</span><el-input-number v-model="miningTaskFilter.targetHoursMax" :min="0" :controls="false" :placeholder="t('miningTaskHoursMax')" style="width: 76px;" /></div>
                    <el-select v-model="miningTaskFilter.validRate" multiple collapse-tags :placeholder="t('filterValidRate')" clearable><el-option v-for="bucket in validRateBucketOptions" :key="bucket.value" :label="t(bucket.labelKey)" :value="bucket.value" /></el-select>
                    <el-date-picker v-model="miningTaskFilter.dateRange" type="datetimerange" value-format="YYYY-MM-DD HH:mm:ss" range-separator="至" :start-placeholder="t('dashDateStart')" :end-placeholder="t('dashDateEnd')" style="width: 340px;" />
                    <el-button @click="resetMiningTaskFilter">{{ t('clearFilters') }}</el-button>
                  </div>
                  <el-table v-if="miningTaskRows.length" ref="miningTaskTableRef" :data="filteredMiningTaskRows" row-key="id" highlight-current-row class="data-table mining-task-table" fit @row-click="openMiningTaskPackagesPage">
                    <el-table-column prop="name" :label="t('miningTaskName')" min-width="170" fixed="left" />
                    <el-table-column :label="t('collectProjectName')" min-width="110"><template #default="scope">{{ scope.row.tags?.project || '—' }}</template></el-table-column>
                    <el-table-column :label="t('taskOwner')" width="100"><template #default="scope">{{ scope.row.owner || '—' }}</template></el-table-column>
                    <el-table-column :label="t('tagPurpose')" width="100"><template #default="scope">{{ scope.row.tags?.purpose || '—' }}</template></el-table-column>
                    <el-table-column :label="t('tagScene')" width="110"><template #default="scope">{{ scope.row.tags?.scene || '—' }}</template></el-table-column>
                    <el-table-column :label="t('tagTrain')" width="100"><template #default="scope">{{ scope.row.tags?.train || '—' }}</template></el-table-column>
                    <el-table-column :label="t('modalityLabel')" width="90"><template #default="scope"><el-tag size="small" effect="plain">{{ batchTypeLabel(scope.row.modality) }}</el-tag></template></el-table-column>
                    <el-table-column :label="t('miningTaskFilterTargetHours')" width="140"><template #default="scope">{{ scope.row.target_duration_hours != null && scope.row.target_duration_hours !== '' ? scope.row.target_duration_hours + ' ' + t('dashHour') : '—' }}</template></el-table-column>
                    <el-table-column :label="t('validDurationLabel')" width="130"><template #default="scope">{{ scope.row.valid_duration_hours != null && scope.row.valid_duration_hours !== '' ? scope.row.valid_duration_hours + ' ' + t('dashHour') : '—' }}</template></el-table-column>
                    <el-table-column :label="t('taskAssignStatusLabel')" width="100"><template #default="scope"><el-tag size="small" effect="plain" :type="miningTaskAssignDone(scope.row) ? 'success' : 'warning'">{{ miningTaskAssignDone(scope.row) ? t('taskAssignDone') : t('taskAssignTodo') }}</el-tag></template></el-table-column>
                    <el-table-column :label="t('collectPeriodLabel')" width="190"><template #default="scope">{{ miningTaskCollectionPeriodText(scope.row) }}</template></el-table-column>
                    <el-table-column :label="t('actions')" width="270" fixed="right"><template #default="scope"><div style="display: inline-flex; align-items: center;" @click.stop><el-button link type="primary" @click.stop="openMiningTaskPackagesPage(scope.row)">{{ t('viewDataPackages') }}</el-button><el-divider direction="vertical" style="margin: 0 8px; height: 12px;" /><el-button link type="primary" @click.stop="openMiningAssignModeDialog(scope.row)">{{ t('assignTaskAction') }}</el-button><el-divider direction="vertical" style="margin: 0 8px; height: 12px;" /><el-dropdown trigger="click" @command="(cmd) => handleTaskRowCommand(cmd, scope.row)"><el-button link type="primary" class="table-action-more"><span>{{ t('downloadTaskManifest') }}</span><span class="dropdown-caret">⌄</span></el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="csv">{{ t('downloadManifestCsv') }}</el-dropdown-item><el-dropdown-item command="json">{{ t('downloadManifestJson') }}</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></template></el-table-column>
                  </el-table>
                  <el-empty v-else :description="t('emptyMiningTasks')" :image-size="64" />
                  <div v-if="!miningTaskRows.length" style="display: flex; justify-content: center; gap: 8px; margin-top: 6px;">
                    <el-button v-if="!collectionProjects.length" type="primary" @click="openCreateCollectionProjectDialog">{{ t('createCollectionProject') }}</el-button>
                    <el-button v-if="collectionProjects.length" type="primary" @click="openMiningTaskDialog">{{ t('miningQuickCreate') }}</el-button>
                  </div>
                </section>
                </template>
                <el-dialog v-model="showMiningTaskDialog" :title="t('createMiningTask')" width="560px" destroy-on-close @closed="onMiningTaskDialogClosed">
                  <label class="login-field"><span>{{ t('miningTaskName') }}</span><el-input v-model="miningTaskForm.name" maxlength="128" /></label>
                  <label class="login-field"><span>{{ t('miningTaskModalities') }}</span><el-select v-model="miningTaskForm.modality"><el-option label="EGO" value="ego" /><el-option label="UMI" value="umi" /><el-option label="Teleop" value="teleop" /></el-select></label>
                  <label class="login-field"><span>{{ t('collectProjectName') }}</span><el-select v-model="miningTaskForm.project" clearable filterable><el-option v-for="item in collectionProjects.filter(p => p.status !== 'archived')" :key="item.name" :label="item.name" :value="item.name" /></el-select></label>
                  <label class="login-field"><span>{{ t('tagPurpose') }}</span><el-select v-model="miningTaskForm.purpose" clearable filterable><el-option v-for="item in purposeLabelOptions" :key="item.name" :label="item.name" :value="item.name" /></el-select></label>
                  <label class="login-field"><span>{{ t('tagTrain') }}</span><el-select v-model="miningTaskForm.train" clearable filterable><el-option v-for="item in trainLabelOptions" :key="item.name" :label="item.name" :value="item.name" /></el-select></label>
                  <label class="login-field"><span>{{ t('tagScene') }}</span><el-select v-model="miningTaskForm.scene" clearable filterable><el-option v-for="item in sceneLabelOptions" :key="item.name" :label="item.name" :value="item.name" /></el-select></label>
                  <label class="login-field"><span>{{ t('targetDuration') }}</span><el-input-number v-model="miningTaskForm.target_duration_hours" :min="0" :step="10" style="width: 100%;" /><span class="muted" style="margin-left: 8px;">{{ t('hoursUnit') }}</span></label>
                  <label class="login-field"><span>{{ t('packageDurationHours') }}</span><el-input-number v-model="miningTaskForm.package_hours" :min="0.01" :step="0.5" :precision="2" style="width: 100%;" /><span class="muted" style="margin-left: 8px;">{{ t('packageCountPreview', { count: miningPackageCountPreview ?? '—' }) }}</span></label>
                  <label class="login-field"><span>{{ t('miningTaskSop') }}</span><el-input v-model="miningTaskForm.sop" type="textarea" :rows="3" maxlength="2000" /></label>
                  <template #footer><el-button @click="resetMiningTaskDialogState">{{ t('cancel') }}</el-button><el-button type="primary" @click="createMiningTask">{{ t('createMiningTask') }}</el-button></template>
                </el-dialog>
                <el-dialog v-model="showMiningSplitDialog" :title="t('splitBatches')" width="500px" destroy-on-close @closed="onMiningSplitDialogClosed">
                  <div class="mining-split-intro">
                    <p class="muted">设置数据包数量，系统会根据任务总目标时长自动平均分配每包目标。</p>
                  </div>
                  <div class="mining-split-summary">
                    <div><span class="muted">任务总目标</span><strong>{{ miningSplitTotalHours ? miningSplitTotalHours.toFixed(2) + ' 小时' : '—' }}</strong></div>
                    <div><span class="muted">每包目标时长</span><strong class="primary-text">{{ miningSplitPerBatchHours }}{{ miningSplitPerBatchHours !== '—' ? ' 小时' : '' }}</strong></div>
                  </div>
                  <label class="login-field"><span>数据包数</span><el-input-number v-model="miningSplitForm.batch_count" :min="1" :max="200" :step="1" controls-position="right" style="width: 100%;" /></label>
                  <p class="muted mining-split-footnote">拆分只处理尚未分配的数据包；已分配的数据包会保留。</p>
                  <template #footer><el-button @click="resetMiningSplitDialogState">{{ t('cancel') }}</el-button><el-button type="primary" :disabled="!miningSplitTotalHours" @click="splitMiningTask">{{ t('splitBatches') }}</el-button></template>
                </el-dialog>
                <el-dialog v-model="showMiningSplitChoiceDialog" :title="t('splitChoiceTitle')" width="480px">
                  <div class="mining-mode-option" :class="{ 'is-active': miningSplitChoice === 'auto' }" @click="miningSplitChoice = 'auto'">
                    <el-radio :model-value="miningSplitChoice === 'auto' ? 'auto' : ''" label="auto" @click.prevent="miningSplitChoice = 'auto'">{{ t('splitChoiceAuto') }}</el-radio>
                    <p class="muted">{{ t('splitChoiceAutoHint') }}</p>
                  </div>
                  <div class="mining-mode-option" :class="{ 'is-active': miningSplitChoice === 'manual' }" @click="miningSplitChoice = 'manual'">
                    <el-radio :model-value="miningSplitChoice === 'manual' ? 'manual' : ''" label="manual" @click.prevent="miningSplitChoice = 'manual'">{{ t('splitChoiceManual') }}</el-radio>
                    <p class="muted">{{ t('splitChoiceManualHint') }}</p>
                  </div>
                  <template #footer><el-button @click="showMiningSplitChoiceDialog = false">{{ t('cancel') }}</el-button><el-button type="primary" @click="confirmMiningSplitChoice">{{ t('splitAction') }}</el-button></template>
                </el-dialog>
                <el-dialog v-model="showMiningAssignDialog" :title="miningAssignScope === 'task' ? t('assignToTask') : t('assignBatch')" width="520px" destroy-on-close @closed="onMiningAssignDialogClosed">
                  <p v-if="miningAssignScope === 'task'" class="muted">{{ t('assignToTaskHint') }}</p>
                  <label v-if="miningAssignScope === 'task'" class="login-field"><span>{{ t('assignModeLabel') }}</span><el-radio-group v-model="miningAssignForm.mode"><el-radio value="even">{{ t('assignModeEven') }}</el-radio></el-radio-group></label>

                  <label class="login-field"><span>{{ t('collector') }}</span><el-select v-model="miningAssignForm.collector_ids" multiple filterable><el-option v-for="profile in collectorProfiles.filter((item) => item.is_active)" :key="profile.id" :label="collectorDisplayLabel(profile)" :value="profile.id" /></el-select></label>
                  <label v-if="demoMode" class="login-field"><span>{{ t('collectionDevice') }}</span><el-select v-model="miningAssignForm.device_id" clearable filterable><el-option v-for="device in collectionDevices" :key="device.id" :label="device.name" :value="device.id" /></el-select></label>
                  <label v-if="demoMode && miningAssignScope === 'batch'" class="login-field"><span>{{ t('batchWindow') }}</span><el-input v-model="miningAssignForm.window" :placeholder="t('assignWindowPlaceholder')" /></label>

                  <template #footer><el-button @click="resetMiningAssignDialogState">{{ t('cancel') }}</el-button><el-button type="primary" @click="submitMiningAssign">{{ t('assignBatch') }}</el-button></template>
                </el-dialog>
                <div v-if="showMiningTaskPackagesPage" class="mining-task-packages-page">
                  <div class="page-actions mining-task-packages-nav">
                    <el-button @click="closeMiningTaskPackagesPage">← {{ t('miningTaskList') }}</el-button>
                    <el-button @click="selectMiningTask(miningSelectedTaskId, { preserveScroll: false })">{{ t('refresh') }}</el-button>
                  </div>
                  <template v-if="miningSelectedTask">
                    <div class="mining-drawer-summary-card">
                      <div class="summary-card-header">
                        <div class="summary-title-area">
                          <div class="task-title-badges">
                            <h3>{{ miningSelectedTask.name }}</h3>
                            <el-tag size="small" effect="plain">{{ batchTypeLabel(miningSelectedTask.modality) }}</el-tag>
                            <el-tag size="small" effect="plain" :type="miningTaskAssignDone(miningSelectedTask) ? 'success' : 'warning'">{{ miningTaskAssignDone(miningSelectedTask) ? t('taskAssignDone') : t('taskAssignTodo') }}</el-tag>
                          </div>
                          <div class="task-meta-line muted">
                            <span v-if="miningSelectedTask.tags?.project"><strong>{{ t('collectProjectName') }}:</strong> {{ miningSelectedTask.tags.project }}</span>
                            <span v-if="miningSelectedTask.owner"><strong>{{ t('taskOwner') }}:</strong> {{ miningSelectedTask.owner }}</span>
                            <span v-if="miningSelectedTask.created_at"><strong>{{ t('createdAt') }}:</strong> {{ formatDate(miningSelectedTask.created_at) }}</span>
                          </div>
                        </div>
                        <div class="summary-actions">
                          <el-button type="primary" size="small" @click="openMiningAssignModeDialog(miningSelectedTask)">{{ t('assignTaskAction') }}</el-button><el-button size="small" @click="openMiningTaskSplit(miningSelectedTask)">{{ t('splitBatches') }}</el-button>
                          <el-dropdown trigger="click" @command="(cmd) => handleTaskRowCommand(cmd, miningSelectedTask)">
                            <el-button size="small" class="table-action-more">
                              <span>{{ t('downloadTaskManifest') }}</span><span class="dropdown-caret">⌄</span>
                            </el-button>
                            <template #dropdown>
                              <el-dropdown-menu>
                                <el-dropdown-item command="csv">{{ t('downloadManifestCsv') }}</el-dropdown-item>
                                <el-dropdown-item command="json">{{ t('downloadManifestJson') }}</el-dropdown-item>
                              </el-dropdown-menu>
                            </template>
                          </el-dropdown>
                        </div>
                      </div>
                      <div class="mining-drawer-info-grid">
                        <div class="info-item"><span class="info-label">{{ t('tagPurpose') }}</span><span class="info-value">{{ miningSelectedTask.tags?.purpose || '—' }}</span></div>
                        <div class="info-item"><span class="info-label">{{ t('tagScene') }}</span><span class="info-value">{{ miningSelectedTask.tags?.scene || '—' }}</span></div>
                        <div class="info-item"><span class="info-label">{{ t('tagTrain') }}</span><span class="info-value">{{ miningSelectedTask.tags?.train || '—' }}</span></div>
                        <div class="info-item"><span class="info-label">{{ t('miningTaskFilterTargetHours') }}</span><span class="info-value">{{ miningSelectedTask.target_duration_hours != null && miningSelectedTask.target_duration_hours !== '' ? miningSelectedTask.target_duration_hours + ' ' + t('dashHour') : '—' }}</span></div>
                        <div class="info-item"><span class="info-label">{{ t('validDurationLabel') }}</span><span class="info-value">{{ miningSelectedTask.valid_duration_hours != null && miningSelectedTask.valid_duration_hours !== '' ? miningSelectedTask.valid_duration_hours + ' ' + t('dashHour') : '—' }}</span></div>
                        <div class="info-item"><span class="info-label">{{ t('progressDoneBatches') }}</span><span class="info-value">{{ miningProgress.completed_batches }} / {{ miningProgress.batch_count }}</span></div>
                      </div>
                      <div v-if="miningSelectedTask.sop" class="mining-drawer-sop-box">
                        <strong>{{ t('miningTaskSop') }}:</strong>
                        <p>{{ miningSelectedTask.sop }}</p>
                      </div>
                    </div>

                    <section class="surface-panel table-panel mining-drawer-packages-panel" v-loading="loading.miningPackages">
                      <div class="panel-heading">
                        <div>
                          <h2>{{ t('taskDataPackages') }}</h2>
                          <span>{{ miningBatchRowsFiltered.length }} / {{ (miningSelectedTask.batches || []).length }} {{ t('itemCount') }}</span>
                        </div>
                        <div style="display: flex; gap: 8px; align-items: center;">
                          <el-button v-if="selectedMiningPackages.length" type="success" size="small" @click="bulkApproveSelectedPackages">{{ t('bulkApproveIntake') }} ({{ selectedMiningPackages.length }})</el-button>
                          <div class="mining-filter-bar"><filter-search-box v-model="miningBatchFilters" :attrs="miningBatchFilterAttrs" :value-map="miningBatchFilterValueMap" :placeholder="t('miningFilterPlaceholder')" :menu-title="t('miningFilterMenuTitle')" @keyword-change="(value) => miningBatchFilterText = value" /></div>
                        </div>
                      </div>
                      <div class="list-filter-bar">
                        <el-select v-model="miningBatchDetailFilter.name" multiple collapse-tags :placeholder="t('dataPackageName')" clearable style="min-width: 150px;"><el-option v-for="item in miningBatchNameOptions" :key="item" :label="item" :value="item" /></el-select>
                        <el-select v-model="miningBatchDetailFilter.modality" multiple collapse-tags :placeholder="t('modalityLabel')" clearable style="min-width: 110px;"><el-option v-for="item in miningBatchModalityOptions" :key="item" :label="batchTypeLabel(item)" :value="item" /></el-select>
                        <div class="range-filter-group"><span class="range-filter-label">{{ t('collectedDuration') }}（{{ t('dashHour') }}）</span><el-input-number v-model="miningBatchDetailFilter.collectedMin" :min="0" :controls="false" :placeholder="t('miningTaskHoursMin')" style="width: 60px;" /><span class="range-filter-sep">—</span><el-input-number v-model="miningBatchDetailFilter.collectedMax" :min="0" :controls="false" :placeholder="t('miningTaskHoursMax')" style="width: 60px;" /></div>
                        <div class="range-filter-group"><span class="range-filter-label">{{ t('validDuration') }}（{{ t('dashHour') }}）</span><el-input-number v-model="miningBatchDetailFilter.validMin" :min="0" :controls="false" :placeholder="t('miningTaskHoursMin')" style="width: 60px;" /><span class="range-filter-sep">—</span><el-input-number v-model="miningBatchDetailFilter.validMax" :min="0" :controls="false" :placeholder="t('miningTaskHoursMax')" style="width: 60px;" /></div>
                        <el-select v-model="miningBatchDetailFilter.collector" multiple collapse-tags :placeholder="t('assigneeCollectors')" clearable style="min-width: 130px;"><el-option v-for="item in miningBatchCollectorOptions" :key="item" :label="item" :value="item" /></el-select>
                        <el-select v-model="miningBatchDetailFilter.device" multiple collapse-tags :placeholder="t('assigneeDevices')" clearable style="min-width: 130px;"><el-option v-for="item in miningBatchDeviceOptions" :key="item" :label="item" :value="item" /></el-select>
                        <el-date-picker v-model="miningBatchDetailFilter.uploadRange" type="datetimerange" value-format="YYYY-MM-DD HH:mm:ss" range-separator="至" :start-placeholder="t('uploadTime')" :end-placeholder="t('uploadTime')" style="width: 320px;" />
                        <el-button @click="resetMiningBatchDetailFilter">{{ t('clearFilters') }}</el-button>
                      </div>
                      <el-table v-if="miningBatchRowsFiltered.length" :data="miningBatchRowsFiltered" class="data-table mining-batch-table" fit row-key="id" :expand-row-keys="miningInlinePackageExpandedKeys" @selection-change="handleMiningPackageSelectionChange">
                        <el-table-column type="selection" width="48" :selectable="canSelectPackageForApproval" />
                        <el-table-column type="expand" width="1" class-name="mining-inline-detail-trigger">
                          <template #default="scope">
                            <div v-if="String(miningInlinePackageDetailId) === String(scope.row.id)" v-loading="miningInlinePackageDetailLoading" class="mining-inline-package-detail">
                              <el-alert v-if="miningInlinePackageDetailError" :title="miningInlinePackageDetailError" type="error" :closable="false">
                                <el-button @click="refreshMiningInlinePackageDetail">{{ t('refresh') }}</el-button>
                              </el-alert>
                              <template v-if="miningInlinePackageDetail">
                                <section class="mining-inline-package-section mining-inline-video-section">
                                  <div class="mining-inline-section-heading">
                                    <div>
                                      <h4>{{ t('preview') }}</h4>
                                      <span class="muted">{{ miningInlinePreviewEpisode?.episode_uid || '—' }}</span>
                                    </div>
                                    <div class="mining-inline-preview-controls">
                                      <el-button type="primary" plain size="small" @click="miningInlineExtraInfoVisible = true">{{ t('extraInfo') }}</el-button>
                                      <div class="mining-inline-video-navigation">
                                        <el-button size="small" :disabled="miningInlinePreviewLoading || miningInlinePreviewIndex <= 0" @click="prevMiningInlineEpisodeVideo">{{ t('prevVideo') }}</el-button>
                                        <span class="muted">{{ miningInlinePreviewEpisodes.length ? miningInlinePreviewIndex + 1 : 0 }} / {{ miningInlinePreviewEpisodes.length }}</span>
                                        <el-button size="small" :disabled="miningInlinePreviewLoading || miningInlinePreviewIndex >= miningInlinePreviewEpisodes.length - 1" @click="nextMiningInlineEpisodeVideo">{{ t('nextVideo') }}</el-button>
                                      </div>
                                      <el-button plain size="small" @click="closeMiningInlinePackageDetail">{{ t('closePreview') }}</el-button>
                                    </div>
                                  </div>
                                  <div class="mining-inline-video-frame" v-loading="miningInlinePreviewLoading">
                                    <video v-if="miningInlinePreviewStream" :key="(miningInlinePreviewEpisode?.id || '') + ':' + miningInlinePreviewStream.url" :src="miningInlinePreviewStream.url" controls playsinline preload="metadata"></video>
                                    <el-empty v-else :description="miningInlinePreviewError || t('previewUnavailable')" :image-size="56" />
                                  </div>
                                  <div v-if="miningInlinePreviewStreams.length > 1" class="mining-inline-stream-tabs">
                                    <el-button v-for="(stream, streamIndex) in miningInlinePreviewStreams" :key="stream.url + streamIndex" size="small" :type="miningInlinePreviewStreamIndex === streamIndex ? 'primary' : 'default'" :plain="miningInlinePreviewStreamIndex !== streamIndex" @click="miningInlinePreviewStreamIndex = streamIndex">{{ stream.label }}</el-button>
                                  </div>
                                </section>

                                <el-dialog v-model="miningInlineExtraInfoVisible" :title="(miningInlinePackageDetail.package_uid || miningInlinePackageDetail.name || '') + ' · ' + t('extraInfo')" width="min(960px, 92vw)" append-to-body destroy-on-close class="mining-inline-extra-dialog">
                                  <div class="mining-inline-package-header">
                                    <div>
                                      <strong>{{ miningInlinePackageDetail.package_uid || miningInlinePackageDetail.name }}</strong>
                                      <div class="muted">ID: #{{ miningInlinePackageDetail.id }}</div>
                                    </div>
                                    <el-tag size="small" effect="plain" :type="dataPackageStatusType(miningInlinePackageDetail.status)">{{ dataPackageStatusLabel(miningInlinePackageDetail.status) }}</el-tag>
                                  </div>

                                <div class="mining-inline-package-grid">
                                  <section class="mining-inline-package-section">
                                    <h4>{{ t('metadata') }}</h4>
                                    <dl class="detail-list">
                                      <dt>{{ t('dataPackageName') }}</dt><dd>{{ miningInlinePackageDetail.package_uid || miningInlinePackageDetail.name }}</dd>
                                      <dt>{{ t('miningTaskList') }}</dt><dd>{{ miningInlinePackageDetail.task_name || miningSelectedTask?.name || '—' }}</dd>
                                      <dt>{{ t('tagProject') }}</dt><dd>{{ miningInlinePackageDetail.project_name || miningSelectedTask?.tags?.project || '—' }}</dd>
                                      <dt>{{ t('modalityLabel') }}</dt><dd><el-tag size="small" effect="plain">{{ batchTypeLabel(miningInlinePackageDetail.batch_type || miningInlinePackageDetail.capture_mode || miningSelectedTask?.modality) }}</el-tag></dd>
                                      <dt>{{ t('packageTargetDuration') }}</dt><dd>{{ miningInlinePackageDetail.target_duration_hours || miningInlinePackageDetail.target || '—' }} {{ t('hoursUnit') }}</dd>
                                      <dt>{{ t('packageCapturedDuration') }}</dt><dd>{{ miningInlinePackageDetail.captured_duration_hours ?? '—' }} {{ t('hoursUnit') }}</dd>
                                      <dt>{{ t('packageIntakeValidDuration') }}</dt><dd>{{ miningInlinePackageDetail.intake_valid_duration_hours ?? '—' }} {{ t('hoursUnit') }}</dd>
                                      <dt>{{ t('packageOperatorCollector') }}</dt><dd>{{ miningInlinePackageDetail.collector_name || (miningInlinePackageDetail.operator_collector_id ? '#' + miningInlinePackageDetail.operator_collector_id : t('unassigned')) }}</dd>
                                      <dt>{{ t('assignDateLabel') }}</dt><dd>{{ miningInlinePackageDetail.assigned_at ? formatDateFull(miningInlinePackageDetail.assigned_at) : '—' }}</dd>
                                      <dt>{{ t('uploadEndTime') }}</dt><dd>{{ miningInlinePackageDetail.upload_completed_at ? formatDateFull(miningInlinePackageDetail.upload_completed_at) : miningBatchUploadEndText(miningInlinePackageDetail) }}</dd>
                                    </dl>
                                  </section>

                                  <section class="mining-inline-package-section">
                                    <h4>{{ t('offlineManifestTitle') }}</h4>
                                    <p class="muted">{{ t('offlineManifestHint') }}</p>
                                    <div class="mining-inline-package-actions">
                                      <el-button type="primary" plain size="small" :disabled="isManifestUnavailable(miningInlinePackageDetail)" @click="downloadPackageOfflineManifest(miningInlinePackageDetail, 'csv')">{{ t('downloadManifestCsv') }}</el-button>
                                      <el-button plain size="small" :disabled="isManifestUnavailable(miningInlinePackageDetail)" @click="downloadPackageOfflineManifest(miningInlinePackageDetail, 'json')">{{ t('downloadManifestJson') }}</el-button>
                                      <span v-if="isManifestUnavailable(miningInlinePackageDetail)" class="muted">{{ t('manifestUnavailableHint') }}</span>
                                    </div>

                                    <h4 class="mining-inline-review-title">{{ t('intakeReviewTitle') }}</h4>
                                    <div v-if="miningInlinePackageDetail.intake_review" class="mining-inline-review-result">
                                      <div class="mining-inline-package-actions">
                                        <span class="muted">{{ t('reviewStatusLabel') }}:</span>
                                        <el-tag size="small" :type="miningInlinePackageDetail.intake_review.verdict === 'approved' ? 'success' : 'danger'">{{ miningInlinePackageDetail.intake_review.verdict === 'approved' ? t('intakeReviewApprove') : t('intakeReviewReject') }}</el-tag>
                                        <span v-if="miningInlinePackageDetail.intake_review.reviewed_at" class="muted">{{ formatDateFull(miningInlinePackageDetail.intake_review.reviewed_at) }}</span>
                                      </div>
                                      <div v-if="miningInlinePackageDetail.intake_review.reviewer_user_id" class="muted">{{ locale === 'en-US' ? 'Reviewer: user #' : '审核人：用户 #' }}{{ miningInlinePackageDetail.intake_review.reviewer_user_id }}</div>
                                      <div v-if="miningInlinePackageDetail.intake_review.reason" class="mining-inline-reject-reason"><strong>{{ t('rejectReasonLabel') }}:</strong> {{ miningInlinePackageDetail.intake_review.reason }}</div>
                                    </div>
                                    <p v-if="miningInlinePackageDetail.supplement_for_package_id">补采来源包 #{{ miningInlinePackageDetail.supplement_for_package_id }}：{{ miningInlinePackageDetail.supplement_reason }}</p>
                                    <div class="mining-inline-package-actions">
                                      <el-button v-if="canReviewPackage(miningInlinePackageDetail)" type="primary" size="small" @click="openPackageIntakeReview(miningInlinePackageDetail)">{{ t('enterIntakeReviewAction') }}</el-button>
                                    </div>
                                  </section>
                                </div>

                                <section class="mining-inline-package-section mining-inline-episodes">
                                  <div class="mining-inline-section-heading">
                                    <h4>{{ t('episodesList') }}</h4>
                                    <span v-if="Array.isArray(miningInlinePackageDetail.episodes)" class="muted">{{ miningInlinePackageDetail.episodes.length }} {{ t('itemCount') }}</span>
                                  </div>
                                  <el-table v-if="Array.isArray(miningInlinePackageDetail.episodes) && miningInlinePackageDetail.episodes.length" :data="miningInlinePackageDetail.episodes" size="small" class="data-table" max-height="260">
                                    <el-table-column prop="episode_uid" label="Episode UID" min-width="140"><template #default="episodeScope"><el-button v-if="episodeScope.row.preview_available !== false" link type="primary" @click="selectMiningInlineEpisodeVideo(episodeScope.row)">{{ episodeScope.row.episode_uid }}</el-button><span v-else>{{ episodeScope.row.episode_uid }}</span></template></el-table-column>
                                    <el-table-column prop="modality" :label="t('modalityLabel')" width="90"><template #default="episodeScope"><el-tag size="small" effect="plain">{{ episodeScope.row.modality || 'ego' }}</el-tag></template></el-table-column>
                                    <el-table-column :label="t('validDurationLabel')" width="110"><template #default="episodeScope">{{ episodeScope.row.duration_hours ? episodeScope.row.duration_hours + ' h' : '—' }}</template></el-table-column>
                                    <el-table-column :label="t('admissionStatusLabel')" width="110"><template #default="episodeScope"><el-tag size="small" :type="episodeScope.row.admission_status === 'passed' ? 'success' : (episodeScope.row.admission_status === 'running' ? 'warning' : 'danger')">{{ episodeScope.row.admission_status }}</el-tag></template></el-table-column>
                                    <el-table-column :label="t('validityStatusLabel')" width="120"><template #default="episodeScope"><el-tag size="small" effect="plain" :type="episodeScope.row.validity_status === 'valid' ? 'success' : (episodeScope.row.validity_status === 'intake_rejected' ? 'danger' : 'info')">{{ episodeScope.row.validity_status || 'unverified' }}</el-tag></template></el-table-column>
                                    <el-table-column :label="t('previewStatusLabel')" width="90"><template #default="episodeScope"><el-tag size="small" :type="episodeScope.row.preview_available ? 'success' : 'info'">{{ episodeScope.row.preview_available ? '可用' : '—' }}</el-tag></template></el-table-column>
                                  </el-table>
                                  <el-empty v-else :description="t('noEpisodesInPackage')" :image-size="48" />
                                </section>
                                </el-dialog>
                              </template>
                            </div>
                          </template>
                        </el-table-column>
                        <el-table-column :label="t('batchSeq')" width="70"><template #default="scope">#{{ scope.row.seq }}</template></el-table-column>
                        <el-table-column :label="t('modalityLabel')" width="90"><template #default="scope"><el-tag size="small" effect="plain">{{ batchTypeLabel(scope.row.batch_type) }}</el-tag></template></el-table-column>
                        <el-table-column :label="t('batchTargetDurationLabel')" width="120"><template #default="scope">{{ miningBatchTargetHoursText(scope.row) }}</template></el-table-column>
                        <el-table-column :label="t('assigneeCollectors')" min-width="140"><template #default="scope"><div class="batch-name-cell"><template v-if="miningAssigneeCollectors(scope.row).length"><strong v-for="(line, idx) in miningAssigneeCollectors(scope.row)" :key="idx">{{ line }}</strong></template><span v-else class="muted">{{ t('unassigned') }}</span></div></template></el-table-column>
                        <el-table-column :label="t('assigneeDevices')" min-width="150"><template #default="scope"><div class="batch-name-cell"><template v-if="miningAssigneeDevices(scope.row).length"><template v-for="(device, idx) in miningAssigneeDevices(scope.row)" :key="idx"><strong>{{ device.name }}</strong><span>{{ device.meta }}</span></template></template><span v-else class="muted">{{ t('unassigned') }}</span></div></template></el-table-column>
                        <el-table-column :label="t('collectedDuration')" min-width="95"><template #default="scope">{{ miningBatchCollectedDurationText(scope.row) }}</template></el-table-column>
                        <el-table-column :label="t('validDuration')" min-width="95"><template #default="scope">{{ miningBatchValidDurationText(scope.row) }}</template></el-table-column>
                        <el-table-column :label="t('uploadEndTime')" width="120"><template #default="scope">{{ miningBatchUploadEndText(scope.row) }}</template></el-table-column>
                        <el-table-column :label="t('collectionTimeLabel')" width="190"><template #default="scope">{{ miningBatchCollectionTimeText(scope.row) }}</template></el-table-column>
                        <el-table-column :label="t('reviewStatusLabel')" width="105"><template #default="scope"><el-tag size="small" effect="plain" :type="miningBatchReviewStatus(scope.row).type">{{ miningBatchReviewStatus(scope.row).label }}</el-tag></template></el-table-column>
                        <el-table-column :label="t('actions')" width="280" fixed="right"><template #default="scope"><div style="display: inline-flex; align-items: center;"><el-button link type="primary" @click="openMiningInlinePackageDetail(scope.row)">{{ t('viewPackageDetails') }}</el-button><el-divider direction="vertical" style="margin: 0 8px; height: 12px;" /><el-button link type="primary" class="table-action-intake-review" @click.stop="openPackageIntakeReview(scope.row)">{{ t('openIntakeReview') }}</el-button><el-divider direction="vertical" style="margin: 0 8px; height: 12px;" /><el-button v-if="canAssignPackage(scope.row)" link type="primary" @click.stop="openMiningAssignDialog(scope.row)">{{ t('assignPackageAction') }}</el-button><el-divider v-if="canAssignPackage(scope.row)" direction="vertical" style="margin: 0 8px; height: 12px;" /><el-dropdown trigger="click" @command="(cmd) => handlePackageRowCommand(cmd, scope.row)"><el-button link type="primary" class="table-action-more"><span>{{ t('more') }}</span><span class="dropdown-caret">⌄</span></el-button><template #dropdown><el-dropdown-menu><el-dropdown-item command="csv" :disabled="isManifestUnavailable(scope.row)">{{ t('downloadManifestCsv') }}</el-dropdown-item><el-dropdown-item command="json" :disabled="isManifestUnavailable(scope.row)">{{ t('downloadManifestJson') }}</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></template></el-table-column>
                      </el-table>
                      <el-empty v-else :description="t('emptyMiningBatches')" :image-size="64" />
                      <div v-if="miningFailureRows.length && miningBatchRowsFiltered.length" class="active-filter-tags"><span class="muted">{{ t('failureReasons') }}</span><el-tag v-for="row in miningFailureRows" :key="row.key" effect="plain" type="danger">{{ row.label }} · {{ row.count }}</el-tag></div>
                    </section>
                  </template>
                  <el-empty v-else :description="t('loadFailed')" :image-size="72">
                    <el-button type="primary" @click="closeMiningTaskPackagesPage">{{ t('miningTaskList') }}</el-button>
                  </el-empty>
                </div>

                            </section>

<collection-overview v-else-if="activeView === 'miningDash'" :workspace-id="selectedWorkspaceId" :projects="collectionProjects" :locale="locale">
  <template #scope>
    <label><span>{{ t('workspace') }}</span><el-select v-model="selectedWorkspaceId" size="small" @change="switchWorkspace" :placeholder="t('workspace')"><el-option v-for="item in availableScopeWorkspaces" :key="item.id" :label="item.name" :value="item.id" /></el-select></label>
  </template>
</collection-overview>

              <section v-else-if="activeView === 'work-queue'" class="view-stack">
                <div class="page-actions work-queue-scope-actions">
                  <label v-if="queueStage === 'annotation' || queueStage === 'review'" class="work-queue-source-scope"><span>{{ t('sourceWorkspace') }}</span><el-select v-model="workQueueWorkspaceId" size="small" filterable @change="switchWorkQueueWorkspace" :placeholder="t('allWorkspaces')"><el-option :label="t('allWorkspaces')" value="" /><el-option v-for="item in workQueueWorkspaceOptions" :key="item.id" :label="item.name" :value="String(item.id)" /></el-select></label>
                  <el-segmented :model-value="queueStage" :options="queueStageOptions" @change="switchQueueStage" />
                  <el-segmented v-if="demoMode && (queueStage === 'annotation' || queueStage === 'review')" :model-value="queueDataMode" :options="queueDataModeOptions" @change="switchQueueDataMode" />
                  <el-button @click="loadWorkQueue">{{ t('refresh') }}</el-button>
                </div>
                <section class="surface-panel table-panel">
                  <template v-if="governanceQueueActive">
                    <section class="surface-panel table-panel">
                      <div v-if="demoMode && queueStage === 'review'" class="list-filter-bar work-queue-filter-bar">
                        <el-select v-model="reviewFilterState.source" clearable :placeholder="t('reviewSourceFilter')" style="min-width: 150px;">
                          <el-option :label="t('reviewSourceAll')" value="" />
                          <el-option :label="t('reviewSourceAlgorithm')" value="algorithm" />
                          <el-option :label="t('reviewSourceHuman')" value="human" />
                        </el-select>
                        <el-checkbox v-model="reviewFilterState.lowConfidence">{{ t('reviewLowConfidence') }}</el-checkbox>
                        <el-select v-if="reviewFilterState.lowConfidence" v-model="reviewFilterState.threshold" style="width: 110px;">
                          <el-option label="< 0.6" :value="0.6" />
                          <el-option label="< 0.7" :value="0.7" />
                          <el-option label="< 0.8" :value="0.8" />
                        </el-select>
                        <el-button v-if="reviewFilterActive" @click="clearReviewFilters">{{ t('clearFilters') }}</el-button>
                      </div>
                      <el-table v-if="governanceFilteredRows.length" :data="governanceFilteredRows" class="data-table" fit row-key="row_key" v-loading="loading.queue">
                        <el-table-column label="ID" width="90"><template #default="scope">{{ scope.row.id }}</template></el-table-column>
                        <el-table-column :label="t('workspace')" min-width="140" show-overflow-tooltip><template #default="scope">{{ scope.row.workspace_name || ('#' + scope.row.workspace_id) }}</template></el-table-column>
                        <el-table-column :label="t('buildBatchNameCol')" min-width="120"><template #default="scope">{{ scope.row.data_batch_name || ('#' + scope.row.data_batch_id) }}</template></el-table-column>
                        <el-table-column :label="t('assignee')" min-width="160"><template #default="scope">{{ scope.row.assignee_email || assigneeLabel(scope.row.assignee_user_id) }}</template></el-table-column>
                        <el-table-column v-if="queueStage === 'review'" :label="t('reviewSource')" width="150"><template #default="scope"><el-tag size="small" :type="reviewSourceTag(scope.row).type">{{ reviewSourceTag(scope.row).label }}</el-tag></template></el-table-column>
                        <el-table-column :label="t('status')" width="120"><template #default="scope">{{ workItemStatusLabel(scope.row.status) }}</template></el-table-column>
                        <el-table-column :label="t('returnReason')" min-width="180"><template #default="scope">{{ scope.row.return_reason || scope.row.reason || scope.row.reassign_reason || '—' }}</template></el-table-column>
                        <el-table-column :label="t('operationTime')" min-width="170"><template #default="scope"><time class="compact-date-time" :datetime="scope.row.updated_at" :title="formatDateFull(scope.row.updated_at)" tabindex="0">{{ formatDate(scope.row.updated_at) }}</time></template></el-table-column>
                        <el-table-column :label="t('operation')" width="280" fixed="right">
                          <template #default="scope">
                            <el-button link type="primary" @click.stop="openPackageWorkbench(scope.row)">{{ scope.row.queue_kind === 'review' ? (locale === 'en-US' ? 'View results' : '查看结果') : (locale === 'en-US' ? 'Open annotation' : '打开标注') }}</el-button>
                            <el-button v-if="canManageUsers" link type="primary" @click.stop="openReassignDialog(scope.row.queue_kind, scope.row)">{{ t('actionReassign') }}</el-button>
                          </template>
                        </el-table-column>
                      </el-table>
                      <el-empty v-else :description="t('emptyQueue')" />
                      <div v-if="queueTotal > 0" class="work-queue-pagination">
                        <span>{{ queueTotal }} {{ t('itemCount') }}</span>
                        <el-pagination :current-page="queuePage" :page-size="queuePageSize" :page-sizes="[50, 100]" :total="queueTotal" layout="sizes, prev, pager, next" @current-change="changeWorkQueuePage" @size-change="changeWorkQueuePageSize" />
                      </div>
                    </section>
                  </template>
                  <template v-else>
                  <div class="list-filter-bar work-queue-filter-bar">
                    <el-input v-model="queueEpisodeKeyword" clearable :placeholder="t('searchEpisode')" @input="scheduleWorkQueueEpisodeSearch" @keyup.enter="submitWorkQueueEpisodeSearch" @clear="submitWorkQueueEpisodeSearch" />
                    <el-select v-model="queueTaskSetId" clearable :placeholder="t('allTaskSets')" @change="reloadWorkQueueFromFirstPage"><el-option v-for="item in taskSets" :key="item.id" :label="item.name" :value="item.id" /></el-select>
                    <el-select v-model="queueTaskLabelId" clearable :placeholder="t('collectionTask')" @change="reloadWorkQueueFromFirstPage"><el-option v-for="label in taskLabels" :key="label.id" :label="label.name" :value="label.id" /></el-select>
                    <el-select v-model="queueStatus" clearable :placeholder="t('status')" @change="reloadWorkQueueFromFirstPage">
                      <el-option :label="t('workPending')" value="pending" />
                      <el-option :label="t('workAssigned')" value="assigned" />
                      <el-option :label="t('workInProgress')" value="in_progress" />
                      <el-option v-if="queueStage !== 'completed'" :label="t('workNeedsRework')" value="rejected" />
                      <el-option v-if="queueStage === 'completed'" :label="t('workAccepted')" value="accepted" />
                    </el-select>
                    <el-date-picker v-model="queueUpdatedRange" type="datetimerange" value-format="YYYY-MM-DDTHH:mm:ssZ" :range-separator="locale === 'en-US' ? 'to' : '至'" :start-placeholder="t('operationDate')" :end-placeholder="t('operationDate')" @change="reloadWorkQueueFromFirstPage" />
                    <el-button :disabled="!activeWorkQueueFilters.length" @click="clearWorkQueueFilters">{{ t('clearFilters') }}</el-button>
                  </div>
                  <div v-if="activeWorkQueueFilters.length" class="active-filter-tags"><el-tag v-for="filter in activeWorkQueueFilters" :key="filter.key" closable effect="plain" @close="clearWorkQueueFilter(filter.key)">{{ filter.label }}</el-tag></div>
                  <el-table v-if="queueRows.length" :key="'work-queue-' + tableLayoutVersion" :data="queueRows" :row-key="queueRowKey" v-loading="loading.queue" class="data-table work-queue-table" fit @header-dragend="(newWidth, oldWidth, column) => onTableHeaderDrag('work-queue', newWidth, column)" @sort-change="changeWorkQueueSort" @row-click="openWorkQueueEpisodeDetail">
                    <el-table-column column-key="episode_uid" prop="episode_uid" :label="t('episode')" :width="tableColumnWidth('work-queue', 'episode_uid', 190)" sortable="custom" show-overflow-tooltip><template #default="scope">{{ scope.row.episode.episode_uid }}</template></el-table-column>
                    <el-table-column :label="t('kind')" width="78"><template #default="scope">{{ scope.row.episode.kind }}</template></el-table-column>
                    <el-table-column column-key="task_label" :label="t('collectionTask')" :width="tableColumnWidth('work-queue', 'task_label', 160)" show-overflow-tooltip><template #default="scope">{{ scope.row.episode.task_label?.name || t('noTaskLabel') }}</template></el-table-column>
                    <el-table-column :label="t('quality')" width="110"><template #default="scope"><el-tag size="small" effect="plain" :type="qualityStatusType(scope.row.quality?.status)">{{ qualityStatusLabel(scope.row.quality?.status) }}</el-tag></template></el-table-column>
                    <el-table-column :label="t('preview')" width="100"><template #default="scope"><el-tag size="small" effect="plain" :type="previewStatusType(queuePreviewStatus(scope.row))">{{ previewStatusLabel(queuePreviewStatus(scope.row)) }}</el-tag></template></el-table-column>
                    <el-table-column :label="t('referenceFrames')" width="86"><template #default="scope">{{ formatNumber(scope.row.metrics?.reference_frame_count) }}</template></el-table-column>
                    <el-table-column prop="duration" :label="t('duration')" width="82" sortable="custom"><template #default="scope">{{ formatDuration(scope.row.metrics?.duration_s) }}</template></el-table-column>
                    <el-table-column :label="t('assignee')" width="92"><template #default="scope">{{ workItemStatusLabel(scope.row.work_item.status) }}</template></el-table-column>
                    <el-table-column column-key="updated_at" prop="updated_at" :label="t('operationTime')" :width="tableColumnWidth('work-queue', 'updated_at', 190)" sortable="custom"><template #default="scope"><time class="compact-date-time" :datetime="scope.row.work_item.updated_at" :title="formatDateFull(scope.row.work_item.updated_at)" tabindex="0">{{ formatDate(scope.row.work_item.updated_at) }}</time></template></el-table-column>
                    <el-table-column v-if="queueStage === 'completed'" :label="t('workType')" width="88"><template #default="scope">{{ workItemKindLabel(scope.row.work_item.kind) }}</template></el-table-column>
                    <el-table-column v-if="queueStage === 'completed'" :label="t('publication')" width="128"><template #default="scope"><el-tag size="small" effect="plain" :type="exportStatusType(scope.row.publication?.status)">{{ publicationJobStatusLabel(scope.row.publication?.status) }}</el-tag></template></el-table-column>
                    <el-table-column column-key="actions" :label="t('status')" :width="tableColumnWidth('work-queue', 'actions', 170)">
                      <template #default="scope">
                        <span class="queue-actions">
                          <el-button v-if="canEnterWorkbench(scope.row)" :data-work-item-id="scope.row.work_item.id" link type="primary" @click.stop="enterWorkbenchById(scope.row.work_item.id)">{{ t('openWorkbench') }}</el-button>
                          <el-button v-for="action in visibleQueueActions(scope.row)" :key="queueActionKey(scope.row, action)" :data-work-item-id="scope.row.work_item.id" link :type="action === 'release' ? 'danger' : 'primary'" @click.stop="runWorkActionById(scope.row.work_item.id, action)">{{ workActionLabel(action) }}</el-button>
                          <span v-if="queuePreviewStatus(scope.row) !== 'ready' && !visibleQueueActions(scope.row).length" class="muted">{{ previewStatusLabel(queuePreviewStatus(scope.row)) }}</span>
                          <span v-else-if="!canEnterWorkbench(scope.row) && !visibleQueueActions(scope.row).length" class="muted">{{ t('noAction') }}</span>
                        </span>
                      </template>
                    </el-table-column>
                  </el-table>
                  <el-empty v-else :description="t('emptyQueue')" />
                  <div v-if="queueTotal > 0" class="work-queue-pagination">
                    <span>{{ queueTotal }} {{ t('itemCount') }}</span>
                    <el-pagination :current-page="queuePage" :page-size="queuePageSize" :page-sizes="[50, 100]" :total="queueTotal" layout="sizes, prev, pager, next" @current-change="changeWorkQueuePage" @size-change="changeWorkQueuePageSize" />
                  </div>
                  </template>
                </section>
              </section>
            </template>
          </section>
        </div>


                <el-dialog v-model="governanceReportVisible" :title="t('governanceReportTitle')" width="640px">
                  <el-collapse v-if="governanceReportRow" v-model="governanceExpandedStages" class="governance-report-collapse">
                    <el-collapse-item v-for="stage in ['integrity', 'quality', 'desensitize']" :key="stage" :name="stage" :disabled="!overviewImportStageEnabled(governanceReportRow, stage)">
                      <template #title>
                        <span class="governance-stage-title">{{ governanceStageLabel(stage) }}</span>
                        <el-tag size="small" effect="plain" :type="overviewImportStageEnabled(governanceReportRow, stage) ? 'success' : 'info'">{{ overviewImportStageEnabled(governanceReportRow, stage) ? t('stageEnabled') : t('stageDisabled') }}</el-tag>
                        <span class="governance-stage-summary">{{ t('stagePassed') }} {{ governanceStageSummary(governanceReportRow, stage).counts.passed }} · {{ t('stageRejected') }} {{ governanceStageSummary(governanceReportRow, stage).counts.failed }} · {{ t('governanceError') }} {{ governanceStageSummary(governanceReportRow, stage).counts.error }}</span>
                      </template>
                      <template v-if="overviewImportStageEnabled(governanceReportRow, stage)">
                        <div v-if="!governanceStageSummary(governanceReportRow, stage).failedItems.length && !governanceStageSummary(governanceReportRow, stage).errorItems.length" class="muted governance-report-issue">{{ t('reportNoIssues') }}</div>
                        <div v-for="(f, fi) in governanceStageSummary(governanceReportRow, stage).failedItems" :key="'f-' + fi" class="governance-report-issue is-failed">{{ t('stageRejected') }}：{{ f.videoId }} · {{ f.name }} —— {{ t('reportFailedReason') }}：{{ f.reason }}</div>
                        <div v-for="(e, ei) in governanceStageSummary(governanceReportRow, stage).errorItems" :key="'e-' + ei" class="governance-report-issue is-error">{{ t('governanceError') }}：{{ e.videoId }} · {{ e.name }} —— {{ t('reportErrorReason') }}：{{ e.reason }}</div>
                      </template>
                    </el-collapse-item>
                  </el-collapse>
                  <template #footer>
                    <el-button @click="governanceReportVisible = false">{{ t('buildBatchCancel') }}</el-button>
                  </template>
                </el-dialog>
        <el-dialog v-model="reassignDialog.visible" :title="t('actionReassign')" width="440px" destroy-on-close>
          <el-form label-position="top">
            <el-form-item :label="t('assignee')" required>
              <el-select v-model="reassignDialog.to_user_id" filterable style="width: 100%;">
                <el-option v-for="user in (reassignDialog.kind === 'review' ? reviewerUserOptions : annotatorUserOptions)" :key="user.id" :label="user.email || user.name || ('#' + user.id)" :value="user.id" />
              </el-select>
            </el-form-item>
            <el-form-item :label="t('returnReason')" required>
              <el-input v-model="reassignDialog.reason" type="textarea" :rows="3" />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="reassignDialog.visible = false">{{ t('cancel') }}</el-button>
            <el-button type="primary" :loading="saving" @click="submitReassignDialog">{{ t('actionReassign') }}</el-button>
          </template>
        </el-dialog>
        <!-- Shared by settings, collection management, data overview and annotation views. -->
        <el-dialog v-model="showCollectionProjectDialog" :title="editingCollectionProject ? t('editCollectionProject') : t('createCollectionProject')" width="480px" destroy-on-close @closed="onCollectionProjectDialogClosed">
          <el-form label-position="top" @submit.prevent="saveCollectionProject">
            <el-form-item :label="t('batchName')" required>
              <el-input v-model="collectionProjectForm.name" maxlength="128" />
            </el-form-item>
            <el-form-item :label="t('description')">
              <el-input v-model="collectionProjectForm.description" type="textarea" :rows="3" maxlength="1000" />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="resetCollectionProjectDialogState">{{ t('cancel') }}</el-button>
            <el-button type="primary" :loading="savingCollectionProject" :disabled="(editingCollectionProject ? !settingsCenterWritable : !collectionProjectCreateWritable) || !collectionProjectForm.name.trim()" @click="saveCollectionProject">{{ editingCollectionProject ? (t('saveSettings') || '保存') : (t('createCollectionProject') || '创建') }}</el-button>
          </template>
        </el-dialog>
        <el-dialog v-model="showWorkspaceDialog" :title="t('createWorkspace')" width="440px" destroy-on-close><el-form label-position="top" @submit.prevent="createWorkspace"><el-form-item :label="t('workspaceName')"><el-input v-model="workspaceForm.workspace_name" /></el-form-item><el-form-item :label="t('description')"><el-input v-model="workspaceForm.desc" type="textarea" :rows="3" /></el-form-item></el-form><template #footer><el-button @click="showWorkspaceDialog = false">{{ t('cancel') }}</el-button><el-button type="primary" :loading="saving" @click="createWorkspace">{{ t('createWorkspace') }}</el-button></template></el-dialog>
        <el-dialog v-model="showTaskSetDialog" :title="t('createTaskSet')" width="440px" destroy-on-close><el-form label-position="top" @submit.prevent="createTaskSet"><el-form-item :label="t('batchName')"><el-input v-model="taskSetForm.name" /></el-form-item><el-form-item :label="t('scene')"><el-input v-model="taskSetForm.scene" /></el-form-item><el-form-item :label="t('description')"><el-input v-model="taskSetForm.description" type="textarea" :rows="3" /></el-form-item></el-form><template #footer><el-button @click="showTaskSetDialog = false">{{ t('cancel') }}</el-button><el-button type="primary" :loading="saving" @click="createTaskSet">{{ t('createTaskSet') }}</el-button></template></el-dialog>
        <el-dialog v-model="showIntakeGuide" :title="t('importData')" width="min(720px, calc(100vw - 32px))" destroy-on-close>
          <div class="view-stack">
            <el-alert type="info" :closable="false" show-icon :title="t('intakeGuideTitle')" />
            <ol class="intake-guide-steps">
              <li>{{ t('intakeGuideStepProject') }}</li>
              <li>{{ t('intakeGuideStepManifest') }}</li>
              <li>{{ t('intakeGuideStepUpload') }}</li>
              <li>{{ t('intakeGuideStepReview') }}</li>
            </ol>
            <el-alert type="warning" :closable="false" show-icon :title="t('intakeGuideOfflineHint')" />
          </div>
          <template #footer>
            <el-button @click="showIntakeGuide = false">{{ t('cancel') }}</el-button>
            <el-button type="primary" @click="openCollectionReview">{{ t('collectedData') }}</el-button>
          </template>
        </el-dialog>
        <el-dialog v-model="showTaskLabelDialog" :title="t('createTaskLabel')" width="440px" destroy-on-close>
          <el-form label-position="top" @submit.prevent="createTaskLabel">
            <el-form-item :label="t('taskLabelName')"><el-input v-model="taskLabelForm.name" maxlength="128" /></el-form-item>
            <el-form-item :label="t('taskLabelDescription')"><el-input v-model="taskLabelForm.description" type="textarea" :rows="3" maxlength="4000" show-word-limit /></el-form-item>
          </el-form>
          <template #footer><el-button @click="showTaskLabelDialog = false">{{ t('cancel') }}</el-button><el-button type="primary" :loading="saving" :disabled="!taskLabelForm.name.trim()" @click="createTaskLabel">{{ t('createTaskLabel') }}</el-button></template>
        </el-dialog>
        <el-dialog v-model="showCollectorDialog" :title="t('addCollector') || t('createCollector')" width="460px" destroy-on-close @closed="onCollectorDialogClosed">
          <div style="margin-bottom: 18px; text-align: center;">
            <el-radio-group v-model="collectorDialogMode" size="default">
              <el-radio-button label="existing">{{ t('collectorModeExisting') }}</el-radio-button>
              <el-radio-button label="create">{{ t('collectorModeCreate') }}</el-radio-button>
            </el-radio-group>
          </div>
          <div v-if="collectorDialogMode === 'existing'" v-loading="loadingAvailableCollectors">
            <el-form label-position="top" @submit.prevent="handleCollectorDialogSubmit">
              <el-form-item :label="t('selectExistingCollector')" required>
                <el-select v-model="selectedExistingCollectorId" filterable style="width:100%" :placeholder="t('selectExistingCollector')" :no-data-text="t('emptyAvailableCollectors')">
                  <el-option v-for="c in availableCollectors" :key="c.id" :label="c.name + ' (' + t('collectorNumber') + ': ' + c.profile_key + ')'" :value="c.id" />
                </el-select>
              </el-form-item>
              <div class="form-hint" style="font-size:12px;color:var(--el-text-color-secondary);margin-top:-6px;">
                {{ t('emptyAvailableCollectorsHint') }}
              </div>
            </el-form>
          </div>
          <el-form v-else label-position="top" @submit.prevent="handleCollectorDialogSubmit">
            <el-form-item :label="t('collectorName')" required><el-input v-model="collectorForm.name" maxlength="128" /></el-form-item>
            <el-form-item :label="t('collectorNumber')"><el-input v-model="collectorForm.profile_key" inputmode="numeric" maxlength="8" :placeholder="t('collectorNumberHint')" /></el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="closeCollectorDialog">{{ t('cancel') }}</el-button>
            <el-button type="primary" :loading="saving" :disabled="collectorDialogMode === 'existing' ? !selectedExistingCollectorId : !collectorProfileFormValid" @click="handleCollectorDialogSubmit">
              {{ t('confirm') || '确定' }}
            </el-button>
          </template>
        </el-dialog>
        <el-dialog v-model="showCollectorQrDialog" :title="t('qrCodes')" width="min(760px, calc(100vw - 32px))" append-to-body destroy-on-close>
          <div class="collector-qr-dialog-heading"><strong>{{ collectorDisplayLabel(selectedCollectorForQr) }}</strong><code>{{ selectedCollectorForQr?.profile_key || '—' }}</code></div>
          <div class="collector-qr-grid">
            <article v-for="card in collectorQrCards" :key="card.kind" class="collector-qr-card">
              <h3>{{ card.label }}</h3><p>{{ card.description }}</p><div class="collector-qr-svg" v-html="card.svg"></div><el-button @click="downloadCollectorQr(card)">{{ t('downloadQr') }}</el-button>
            </article>
          </div>
          <template #footer><el-button @click="showCollectorQrDialog = false">{{ t('cancel') }}</el-button><el-button type="primary" @click="printCollectorQrCodes">{{ t('print') }}</el-button></template>
        </el-dialog>
        <el-dialog v-model="showNativeLerobotDialog" :title="t('nativeLerobot')" width="min(640px, calc(100vw - 32px))" append-to-body destroy-on-close>
          <dl class="detail-list native-lerobot-detail-list"><dt>{{ t('datasetName') }}</dt><dd>{{ selectedNativeLerobotDataset?.name || selectedNativeLerobotDataset?.dataset_id || '—' }}</dd><dt>{{ t('datasetId') }}</dt><dd>{{ selectedNativeLerobotDataset?.dataset_id || '—' }}</dd><dt>{{ t('fileCount') }}</dt><dd>{{ selectedNativeLerobotDataset?.file_count ?? '—' }}</dd><dt>{{ t('size') }}</dt><dd>{{ formatBytes(selectedNativeLerobotDataset?.total_size) }}</dd><dt>{{ t('completedAt') }}</dt><dd>{{ formatDateFull(selectedNativeLerobotDataset?.completed_at) }}</dd><dt>{{ t('status') }}</dt><dd>{{ selectedNativeLerobotDataset?.status === 'active' ? t('active') : t('archived') }}</dd><dt>{{ t('nativePlatformCopy') }}</dt><dd><div class="native-lerobot-copy-detail"><el-tag size="small" effect="plain" :type="nativeCopyStatusType(selectedNativeLerobotDataset?.copy_status)">{{ nativeCopyStatusLabel(selectedNativeLerobotDataset?.copy_status) }}</el-tag><small v-if="selectedNativeLerobotDataset?.copy_status === 'failed'">{{ nativeCopyErrorLabel(selectedNativeLerobotDataset) }}</small><small v-else>{{ nativeCopySummary(selectedNativeLerobotDataset) }}</small></div></dd><dt>{{ t('nativeCopyUpdatedAt') }}</dt><dd><time class="compact-date-time" :datetime="selectedNativeLerobotDataset?.updated_at" :title="formatDateFull(selectedNativeLerobotDataset?.updated_at)" tabindex="0">{{ formatDate(selectedNativeLerobotDataset?.updated_at) }}</time></dd><dt v-if="selectedNativeLerobotBundle">ZIP</dt><dd v-if="selectedNativeLerobotBundle"><el-tag size="small" effect="plain" :type="selectedNativeLerobotBundle.download_available ? 'success' : selectedNativeLerobotBundle.status === 'failed' ? 'danger' : 'warning'">{{ selectedNativeLerobotBundle.download_available ? t('zipReady') : selectedNativeLerobotBundle.status === 'failed' ? t('zipFailed') : t('packagingZip') }}</el-tag></dd></dl>
          <el-alert type="info" :closable="false" :title="t('nativeLerobotVerified')" />
          <template #footer><el-button @click="showNativeLerobotDialog = false">{{ t('cancel') }}</el-button><template v-if="nativeLerobotCanDeliver()"><el-button type="primary" @click="copyNativeLerobotOssUri()">{{ t('copyOssUri') }}</el-button><el-button v-if="canExportDatasets && selectedNativeLerobotBundle?.download_available" type="primary" plain @click="downloadNativeLerobotBundle()">{{ t('downloadZip') }}</el-button><el-button v-else-if="canExportDatasets" :loading="nativeLerobotActionLoading" :disabled="['queued', 'running'].includes(selectedNativeLerobotBundle?.status)" @click="requestNativeLerobotBundle()">{{ nativeLerobotBundleActionLabel() }}</el-button><el-button v-if="canManageDatasets" type="danger" plain :loading="nativeLerobotActionLoading" @click="archiveNativeLerobotDataset()">{{ t('archive') }}</el-button></template><el-button v-else-if="nativeLerobotNeedsReauthorization() && canCreateBatch" :loading="nativeLerobotActionLoading" @click="openNativeLerobotSourceReauthorization()">{{ t('reauthorizeSource') }}</el-button><el-button v-else-if="selectedNativeLerobotDataset?.copy_status === 'failed' && canCreateBatch" type="primary" plain :loading="nativeLerobotActionLoading" @click="retryNativeLerobotCopy()">{{ t('retryPlatformCopy') }}</el-button></template>
        </el-dialog>
        <el-dialog v-model="showNativeLerobotReauthorizationDialog" :title="t('reauthorizeSource')" width="min(560px, calc(100vw - 32px))" append-to-body destroy-on-close>
          <p class="form-hint native-lerobot-reauthorization-hint">{{ t('reauthorizeSourceHint') }}</p>
          <el-form label-position="top"><el-form-item :label="t('selectSourceSnapshot')"><el-select v-model="nativeLerobotReauthorizationCandidate" :loading="loading.lerobotCandidates" :disabled="loading.lerobotCandidates" :placeholder="t('selectSourceSnapshot')"><el-option v-for="candidate in nativeLerobotReauthorizationCandidates()" :key="candidate.token" :label="nativeLerobotCandidateLabel(candidate)" :value="candidate.token" /></el-select></el-form-item></el-form>
          <p v-if="!loading.lerobotCandidates && !nativeLerobotReauthorizationCandidates().length" class="form-hint">{{ t('reauthorizeSourceEmpty') }}</p>
          <template #footer><el-button @click="showNativeLerobotReauthorizationDialog = false">{{ t('cancel') }}</el-button><el-button type="primary" :loading="nativeLerobotActionLoading" :disabled="!nativeLerobotReauthorizationCandidate" @click="reauthorizeNativeLerobotSource">{{ t('reauthorizeSource') }}</el-button></template>
        </el-dialog>
        <el-dialog v-model="showShortcutHelp" :title="t('shortcutHelp')" width="min(580px, calc(100vw - 32px))" append-to-body destroy-on-close>
          <div class="shortcut-help-list"><div v-for="command in workbenchShortcutCommands" :key="command.id" class="shortcut-help-row"><span>{{ t(command.labelKey) }}</span><kbd>{{ command.chord }}</kbd></div></div>
        </el-dialog>
        <el-dialog v-model="showDeviceDialog" :title="t('createDevice')" width="520px" destroy-on-close @closed="onDeviceDialogClosed"><el-form label-position="top" @submit.prevent="createCollectionDevice"><el-form-item :label="t('deviceName')" required><el-input v-model="deviceForm.name" maxlength="128" /></el-form-item><div class="two-column-form"><el-form-item :label="t('deviceType')" required><el-select v-model="deviceForm.device_type"><el-option label="iPhone" value="iphone" /><el-option label="Collection station" value="collection_station" /><el-option label="Robot" value="robot" /><el-option label="Other" value="other" /></el-select></el-form-item><el-form-item :label="t('deviceModel')"><el-input v-model="deviceForm.model" maxlength="128" /></el-form-item></div><el-form-item :label="t('serialNumber')" required><el-input v-model="deviceForm.serial_number" maxlength="128" /></el-form-item></el-form><template #footer><el-button @click="closeDeviceDialog">{{ t('cancel') }}</el-button><el-button type="primary" :loading="saving" :disabled="!deviceForm.name.trim() || !deviceForm.device_type.trim() || !deviceForm.serial_number.trim()" @click="createCollectionDevice">{{ t('createDevice') }}</el-button></template></el-dialog>
        <el-dialog v-model="showManagedUserDialog" :title="t('createManagedUser')" width="440px" destroy-on-close>
          <el-form label-position="top" @submit.prevent="createManagedUser">
            <el-form-item :label="t('email')"><el-input v-model="managedUserForm.email" type="email" autocomplete="email" maxlength="128" /></el-form-item>
            <el-form-item :label="t('password')"><el-input v-model="managedUserForm.password" type="password" show-password autocomplete="new-password" minlength="10" /></el-form-item>
            <el-form-item :label="t('userRole')"><el-select v-model="managedUserForm.role"><el-option v-for="role in assignableRoles" :key="role" :label="roleLabel(role)" :value="role" /></el-select></el-form-item>
          </el-form>
          <template #footer><el-button @click="showManagedUserDialog = false">{{ t('cancel') }}</el-button><el-button type="primary" :loading="saving" :disabled="!managedUserForm.email.trim() || managedUserForm.password.length < 10 || !managedUserForm.role" @click="createManagedUser">{{ t('createManagedUser') }}</el-button></template>
        </el-dialog>
        <el-dialog v-model="showWorkspaceMemberDialog" :title="t('grantWorkspaceMember')" width="440px" destroy-on-close>
          <el-form label-position="top" @submit.prevent="grantWorkspaceMember">
            <el-form-item :label="t('selectUser')"><el-select v-model="workspaceMemberForm.user_id" filterable><el-option v-for="managedUser in availableWorkspaceMembers" :key="managedUser.id" :label="managedUser.email" :value="managedUser.id" /></el-select></el-form-item>
          </el-form>
          <template #footer><el-button @click="showWorkspaceMemberDialog = false">{{ t('cancel') }}</el-button><el-button type="primary" :loading="saving" :disabled="!workspaceMemberForm.user_id" @click="grantWorkspaceMember">{{ t('grantWorkspaceMember') }}</el-button></template>
        </el-dialog>
        <el-drawer v-model="showEpisodeDrawer" class="episode-detail-drawer" :title="selectedEpisode?.episode_uid || t('episode')" size="min(720px, 56vw)" @closed="closeEpisodeDetail">
          <div class="episode-detail-shell">
            <div v-if="episodeDrawerError.detail" class="episode-detail-load-error">
              <el-alert :title="t('episodeDetailLoadFailed')" type="error" :closable="false" show-icon />
              <el-button link type="primary" @click="retryEpisodeDetail">{{ t('retryLoad') }}</el-button>
            </div>
            <el-skeleton v-if="!selectedEpisode" :rows="8" animated />
            <el-tabs v-else v-model="episodeDrawerTab" class="episode-detail-tabs">
              <el-tab-pane :label="t('overview')" name="overview">
                <div class="episode-detail-overview" v-loading="episodeDrawerLoading.detail">
                  <section class="episode-detail-section">
                    <h3>{{ t('identityAndLineage') }}</h3>
                    <dl class="episode-detail-grid">
                      <div><dt>{{ t('episode') }}</dt><dd>{{ selectedEpisode.episode_uid || '—' }}</dd></div>
                      <div><dt>{{ t('kind') }}</dt><dd>{{ assetKindLabel(selectedEpisode.kind) }}</dd></div>
                      <div><dt>{{ t('parentEpisode') }}</dt><dd>{{ selectedEpisode.parent_episode_uid || selectedEpisode.parent_episode_id || '—' }}</dd></div>
                      <div><dt>{{ t('derivationVersion') }}</dt><dd>{{ selectedEpisode.derivation_version || selectedEpisode.derived_version || '—' }}</dd></div>
                    </dl>
                  </section>
                  <section v-if="episodeDrawerContext?.workItem" class="episode-detail-section episode-work-context">
                    <h3>{{ t('currentWorkflow') }}</h3>
                    <dl class="episode-detail-grid">
                      <div><dt>{{ t('workType') }}</dt><dd>{{ workItemKindLabel(episodeDrawerContext.workItem.kind) }}</dd></div>
                      <div><dt>{{ t('currentStage') }}</dt><dd>{{ queueStageLabel(episodeDrawerContext.queueStage) }}</dd></div>
                      <div><dt>{{ t('status') }}</dt><dd>{{ workItemStatusLabel(episodeDrawerContext.workItem.status) }}</dd></div>
                      <div><dt>{{ t('operationTime') }}</dt><dd>{{ formatDate(episodeDrawerContext.workItem.updated_at) }}</dd></div>
                      <div><dt>{{ t('preview') }}</dt><dd>{{ previewStatusLabel(episodeDrawerContext.previewStatus) }}</dd></div>
                    </dl>
                  </section>
                  <section class="episode-detail-section">
                    <h3>{{ t('businessContext') }}</h3>
                    <dl class="episode-detail-grid">
                      <div><dt>{{ t('collectionTask') }}</dt><dd>{{ selectedEpisode.task_label?.name || selectedEpisode.task_label_name || selectedEpisode.task_name || t('noTaskLabel') }}</dd></div>
                      <div><dt>{{ t('modality') }}</dt><dd>{{ selectedEpisode.modality || '—' }}</dd></div>
                      <div><dt>{{ t('scene') }}</dt><dd>{{ selectedEpisode.scene || '—' }}</dd></div>
                      <div><dt>{{ t('collector') }}</dt><dd>{{ collectorDisplayLabel(selectedEpisode.attribution?.collector?.collector) || t('unknownCollector') }}</dd></div>
                      <div><dt>{{ t('collectionDevice') }}</dt><dd>{{ selectedEpisode.attribution?.device?.device ? selectedEpisode.attribution.device.device.name + ' · ' + selectedEpisode.attribution.device.device.serial_number : t('unknownDevice') }}</dd></div>
                    </dl>
                  </section>
                  <section class="episode-detail-section">
                    <h3>{{ t('quality') }} / {{ t('metrics') }}</h3>
                    <dl class="episode-detail-grid">
                      <div><dt>{{ t('quality') }}</dt><dd><el-tag size="small" effect="plain" :type="qualityStatusType(selectedEpisode.quality?.status || selectedEpisode.quality_status)">{{ qualityStatusLabel(selectedEpisode.quality?.status || selectedEpisode.quality_status) }}</el-tag></dd></div>
                      <div><dt>{{ t('referenceFrames') }}</dt><dd>{{ formatNumber(selectedEpisode.metrics?.reference_frame_count ?? selectedEpisode.reference_frame_count) }}</dd></div>
                      <div><dt>{{ t('duration') }}</dt><dd>{{ formatDuration(selectedEpisode.metrics?.duration_s ?? selectedEpisode.duration_s) }}</dd></div>
                      <div><dt>{{ t('frameRate') }}</dt><dd>{{ selectedEpisode.metrics?.average_rgb_rate_hz != null ? Number(selectedEpisode.metrics.average_rgb_rate_hz).toFixed(2) + ' Hz' : '—' }}</dd></div>
                    </dl>
                    <section v-if="selectedEpisode.quality_diagnostic" class="quality-diagnostic episode-quality-diagnostic"><div><strong>{{ t('qualityFailureReason') }}</strong><span>{{ qualityDiagnosticLabel(selectedEpisode.quality_diagnostic) }}</span><small>{{ selectedEpisode.quality_diagnostic.error_code }} · {{ formatDate(selectedEpisode.quality_diagnostic.occurred_at) }}</small></div></section>
                  </section>
                  <section class="episode-detail-section">
                    <h3>{{ t('lifecycle') }}</h3>
                    <dl class="episode-detail-grid">
                      <div><dt>{{ t('publication') }}</dt><dd>{{ episodePublicationLabel(selectedEpisode) }}</dd></div>
                      <div><dt>{{ t('createdAt') }}</dt><dd>{{ formatDate(selectedEpisode.created_at) }}</dd></div>
                      <div><dt>{{ t('updatedAt') }}</dt><dd>{{ formatDate(selectedEpisode.updated_at) }}</dd></div>
                      <div><dt>{{ t('publishedAt') }}</dt><dd>{{ formatDate(selectedEpisode.published_at || selectedEpisode.publication?.published_at) }}</dd></div>
                    </dl>
                  </section>
                  <section class="episode-detail-section episode-preview-section">
                    <h3>{{ t('preview') }}</h3>
                    <el-skeleton v-if="episodeDrawerLoading.preview" :rows="4" animated />
                    <el-alert v-else-if="episodeDrawerError.preview" :title="t('previewLoadFailed')" type="warning" :closable="false" show-icon />
                    <video v-else-if="episodePreview?.available" class="episode-preview-video" controls preload="metadata" :src="episodePreview.url" :aria-label="t('preview')"></video>
                    <el-empty v-else :description="t('previewUnavailable')" :image-size="64" />
                  </section>
                  <el-collapse v-model="episodeTechnicalSections" class="episode-technical-collapse">
                    <el-collapse-item :title="t('technicalInfo')" name="technical">
                      <dl class="episode-detail-grid episode-technical-grid">
                        <div><dt>{{ t('databaseId') }}</dt><dd>{{ selectedEpisode.id || '—' }}</dd></div>
                        <div><dt>{{ t('workspaceId') }}</dt><dd>{{ selectedEpisode.workspace_id || '—' }}</dd></div>
                        <div><dt>{{ t('taskSetId') }}</dt><dd>{{ selectedEpisode.task_set_id || '—' }}</dd></div>
                        <div><dt>{{ t('importSessionId') }}</dt><dd>{{ selectedEpisode.import_session_id || '—' }}</dd></div>
                        <div><dt>{{ t('embodimentId') }}</dt><dd>{{ selectedEpisode.embodiment_id || '—' }}</dd></div>
                        <div><dt>{{ t('taskLabelId') }}</dt><dd>{{ selectedEpisode.task_label_id || '—' }}</dd></div>
                        <div class="episode-detail-wide"><dt>{{ t('sourceRange') }}</dt><dd>{{ selectedEpisode.source_start_ns || '—' }} - {{ selectedEpisode.source_end_ns || '—' }}</dd></div>
                      </dl>
                    </el-collapse-item>
                  </el-collapse>
                </div>
              </el-tab-pane>
              <el-tab-pane :label="t('artifacts')" name="artifacts">
                <div class="episode-detail-tab-body" v-loading="episodeDrawerLoading.detail">
                  <el-table v-if="selectedEpisode.artifacts?.length" :data="selectedEpisode.artifacts" class="data-table episode-artifact-table" size="small" fit>
                    <el-table-column prop="artifact_type" :label="t('kind')" min-width="150" />
                    <el-table-column prop="storage_role" :label="t('storageRole')" width="110" />
                    <el-table-column :label="t('size')" width="100"><template #default="scope">{{ formatBytes(scope.row.size_bytes) }}</template></el-table-column>
                    <el-table-column prop="retention_policy" :label="t('retentionPolicy')" min-width="120" />
                    <el-table-column :label="t('retentionUntil')" min-width="155"><template #default="scope">{{ formatDate(scope.row.retention_until) }}</template></el-table-column>
                  </el-table>
                  <el-empty v-else :description="t('emptyAssets')" :image-size="72" />
                  <section class="raw-source-downloads">
                    <div class="panel-heading"><div><h3>{{ t('rawSourceDownloads') }}</h3></div><el-button :loading="episodeDrawerLoading.rawSource" @click="loadRawSourceDownloads">{{ t('loadRawSourceDownloads') }}</el-button></div>
                    <el-table v-if="rawSourceDownloads?.files?.length" :data="rawSourceDownloads.files" class="data-table raw-source-download-table" size="small" fit>
                      <el-table-column prop="name" :label="t('source')" min-width="200" />
                      <el-table-column :label="t('size')" width="110"><template #default="scope">{{ formatBytes(scope.row.size_bytes) }}</template></el-table-column>
                      <el-table-column :label="t('status')" width="120"><template #default="scope"><el-tag size="small" effect="plain" :type="scope.row.available ? 'success' : 'info'">{{ scope.row.available ? t('active') : t('rawSourceUnavailable') }}</el-tag></template></el-table-column>
                      <el-table-column :label="t('actions')" width="110" fixed="right"><template #default="scope"><el-button link type="primary" :disabled="!scope.row.available" @click="downloadRawSourceFile(scope.row)">{{ t('downloadRawFile') }}</el-button></template></el-table-column>
                    </el-table>
                    <p v-else-if="rawSourceDownloads && !rawSourceDownloads.available" class="form-hint">{{ t('rawSourceUnavailable') }}</p>
                  </section>
                </div>
              </el-tab-pane>
              <el-tab-pane :label="t('packetMetadata')" name="packet-metadata">
                <div class="episode-detail-tab-body" v-loading="episodeDrawerLoading.detail">
                  <template v-if="hasPacketMetadata(selectedEpisode.packet_metadata)">
                    <dl v-if="packetMetadataRows(selectedEpisode.packet_metadata).length" class="detail-list packet-metadata-list">
                      <template v-for="row in packetMetadataRows(selectedEpisode.packet_metadata)" :key="row.key"><dt>{{ row.label }}</dt><dd>{{ row.value }}</dd></template>
                    </dl>
                    <el-table v-if="selectedEpisode.packet_metadata.cameras?.length" :data="selectedEpisode.packet_metadata.cameras" size="small" class="data-table packet-stream-table" fit>
                      <el-table-column :label="t('packetCameras')" min-width="260"><template #default="scope"><code>{{ scope.row.topic }}</code></template></el-table-column>
                      <el-table-column :label="t('packetResolution')" width="130"><template #default="scope">{{ cameraResolutionLabel(scope.row) }}</template></el-table-column>
                      <el-table-column :label="t('packetFps')" width="90"><template #default="scope">{{ scope.row.fps != null ? scope.row.fps : '—' }}</template></el-table-column>
                    </el-table>
                    <el-table v-if="selectedEpisode.packet_metadata.sensors?.length" :data="selectedEpisode.packet_metadata.sensors" size="small" class="data-table packet-stream-table" fit>
                      <el-table-column :label="t('packetSensors')" min-width="260"><template #default="scope"><code>{{ scope.row.topic }}</code></template></el-table-column>
                      <el-table-column :label="t('packetFrequency')" width="140"><template #default="scope">{{ scope.row.frequency_hz != null ? scope.row.frequency_hz + ' Hz' : '—' }}</template></el-table-column>
                    </el-table>
                  </template>
                  <el-empty v-else :description="t('emptyPacketMetadata')" :image-size="72" />
                </div>
              </el-tab-pane>
            </el-tabs>
          </div>
        </el-drawer>
      <!-- Data package drawer and intake review dialog render outside the view chain so any view can use them. -->
<el-drawer v-model="dataPackageDrawerVisible" :title="t('dataPackageDrawerTitle')" direction="rtl" size="680px" append-to-body>
                  <div v-loading="packageDetailLoading" class="data-package-drawer-content">
                    <el-alert v-if="packageDetailError" :title="packageDetailError" type="error" :closable="false"><el-button @click="refreshPackageDetail">{{ t('refresh') }}</el-button></el-alert>
                    <template v-if="packageDetail">
                      <div class="package-header-banner" style="display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; margin-bottom: 12px;">
                        <div>
                          <strong style="font-size: 16px; color: #1e293b;">{{ packageDetail.package_uid || packageDetail.name }}</strong>
                          <div class="muted" style="font-size: 12px; margin-top: 4px;">ID: #{{ packageDetail.id }}</div>
                        </div>
                        <el-tag size="small" effect="plain" :type="dataPackageStatusType(packageDetail.status)">
                          {{ dataPackageStatusLabel(packageDetail.status) }}
                        </el-tag>
                      </div>

                      <div class="drawer-section" style="padding: 0 16px 16px;">
                        <h4 style="margin: 0 0 10px; font-size: 14px; font-weight: 600; color: #1e293b;">{{ t('metadata') || '基础信息' }}</h4>
                        <dl class="detail-list" style="padding: 12px; background: #fff; border: 1px solid #e2e8f0; border-radius: 6px;">
                          <dt>{{ t('dataPackageName') }}</dt>
                          <dd>{{ packageDetail.package_uid || packageDetail.name }}</dd>
                          <dt>{{ t('miningTaskList') }}</dt>
                          <dd>{{ packageDetail.task_name || miningSelectedTask?.name || '—' }}</dd>
                          <dt>{{ t('tagProject') }}</dt>
                          <dd>{{ packageDetail.project_name || miningSelectedTask?.tags?.project || '—' }}</dd>
                          <dt>{{ t('modalityLabel') }}</dt>
                          <dd><el-tag size="small" effect="plain">{{ batchTypeLabel(packageDetail.batch_type || packageDetail.capture_mode || miningSelectedTask?.modality) }}</el-tag></dd>
                          <dt>{{ t('packageTargetDuration') }}</dt>
                          <dd>{{ packageDetail.target_duration_hours || packageDetail.target }} {{ t('hoursUnit') }}</dd>
                          <dt>{{ t('packageCapturedDuration') }}</dt>
                          <dd>{{ packageDetail.captured_duration_hours ?? '—' }} {{ t('hoursUnit') }}</dd>
                          <dt>{{ t('packageIntakeValidDuration') }}</dt>
                          <dd>{{ packageDetail.intake_valid_duration_hours ?? '—' }} {{ t('hoursUnit') }}</dd>
                          <dt>{{ t('packageOperatorCollector') }}</dt>
                          <dd>{{ packageDetail.collector_name || (packageDetail.operator_collector_id ? '#' + packageDetail.operator_collector_id : t('unassigned')) }}</dd>
                          <dt>{{ t('assignDateLabel') || '分配时间' }}</dt>
                          <dd>{{ packageDetail.assigned_at ? formatDateFull(packageDetail.assigned_at) : '—' }}</dd>
                          <dt>{{ t('uploadEndTime') }}</dt>
                          <dd>{{ packageDetail.upload_completed_at ? formatDateFull(packageDetail.upload_completed_at) : miningBatchUploadEndText(packageDetail) }}</dd>
                        </dl>
                      </div>

                      <div class="drawer-section" style="padding: 0 16px 16px;">
                        <h4 style="margin: 0 0 6px; font-size: 14px; font-weight: 600; color: #1e293b;">{{ t('offlineManifestTitle') }}</h4>
                        <p class="muted" style="margin: 0 0 12px; font-size: 12px;">{{ t('offlineManifestHint') }}</p>
                        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                          <el-button
                            type="primary"
                            plain
                            size="small"
                            :disabled="isManifestUnavailable(packageDetail)"
                            @click="downloadPackageOfflineManifest(packageDetail, 'csv')"
                          >
                            {{ t('downloadManifestCsv') }}
                          </el-button>
                          <el-button
                            type="default"
                            plain
                            size="small"
                            :disabled="isManifestUnavailable(packageDetail)"
                            @click="downloadPackageOfflineManifest(packageDetail, 'json')"
                          >
                            {{ t('downloadManifestJson') }}
                          </el-button>
                          <span v-if="isManifestUnavailable(packageDetail)" class="muted" style="font-size: 12px;">
                            {{ t('manifestUnavailableHint') }}
                          </span>
                        </div>
                      </div>

                      <div class="drawer-section" style="padding: 0 16px 16px;">
                        <h4 style="margin: 0 0 10px; font-size: 14px; font-weight: 600; color: #1e293b;">{{ t('intakeReviewTitle') }}</h4>
                        <div v-if="packageDetail.intake_review" style="margin-bottom: 12px; padding: 12px; background: #f8fafc; border-radius: 6px; border: 1px solid #e2e8f0;">
                          <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 6px;">
                            <span class="muted" style="font-size: 13px;">{{ t('reviewStatusLabel') }}:</span>
                            <el-tag size="small" :type="packageDetail.intake_review.verdict === 'approved' ? 'success' : 'danger'">
                              {{ packageDetail.intake_review.verdict === 'approved' ? t('intakeReviewApprove') : t('intakeReviewReject') }}
                            </el-tag>
                            <span v-if="packageDetail.intake_review.reviewed_at" class="muted" style="font-size: 12px; margin-left: auto;">
                              {{ formatDateFull(packageDetail.intake_review.reviewed_at) }}
                            </span>
                          </div>
                          <div v-if="packageDetail.intake_review.reviewer_user_id" style="font-size: 12px; color: #64748b; margin-bottom: 4px;">
                            {{ locale === 'en-US' ? 'Reviewer: user #' : '审核人：用户 #' }}{{ packageDetail.intake_review.reviewer_user_id }}
                          </div>
                          <div v-if="packageDetail.intake_review.reason" style="font-size: 13px; color: #b91c1c; margin-top: 6px; padding: 6px 10px; background: #fef2f2; border-radius: 4px;">
                            <strong>{{ t('rejectReasonLabel') }}:</strong> {{ packageDetail.intake_review.reason }}
                          </div>
                        </div>

                        <p v-if="packageDetail.supplement_for_package_id">补采来源包 #{{ packageDetail.supplement_for_package_id }}：{{ packageDetail.supplement_reason }}</p>
                        <el-button v-if="canCreatePackageSupplement(packageDetail)" plain size="small" type="warning" class="table-action-create-supplement" @click="createPackageSupplement(packageDetail)">{{ locale === 'en-US' ? 'Create supplemental package' : '创建补采包' }}</el-button>
                        <div v-if="canReviewPackage(packageDetail)" style="display: flex; gap: 10px;">
                          <el-button type="primary" size="small" @click="openIntakeReview(packageDetail)">
                            {{ t('enterIntakeReviewAction') }}
                          </el-button>
                        </div>
                      </div>

                      <div class="drawer-section" style="padding: 0 16px 16px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                          <h4 style="margin: 0; font-size: 14px; font-weight: 600; color: #1e293b;">{{ t('episodesList') }}</h4>
                          <span v-if="Array.isArray(packageDetail.episodes)" class="muted" style="font-size: 12px;">
                            {{ packageDetail.episodes.length }} {{ t('itemCount') }}
                          </span>
                        </div>
                        <el-table
                          v-if="Array.isArray(packageDetail.episodes) && packageDetail.episodes.length"
                          :data="packageDetail.episodes"
                          size="small"
                          class="data-table"
                          max-height="260"
                        >
                          <el-table-column prop="episode_uid" label="Episode UID" min-width="140"><template #default="scope"><el-button link type="primary" @click="openEpisodeDetail(scope.row, { source: 'package' })">{{ scope.row.episode_uid }}</el-button></template></el-table-column>
                          <el-table-column prop="modality" :label="t('modalityLabel')" width="80">
                            <template #default="scope">
                              <el-tag size="small" effect="plain">{{ scope.row.modality || 'ego' }}</el-tag>
                            </template>
                          </el-table-column>
                          <el-table-column :label="t('validDurationLabel')" width="90">
                            <template #default="scope">
                              {{ scope.row.duration_hours ? scope.row.duration_hours + ' h' : '—' }}
                            </template>
                          </el-table-column>
                          <el-table-column :label="t('admissionStatusLabel')" width="90">
                            <template #default="scope">
                              <el-tag
                                size="small"
                                :type="scope.row.admission_status === 'passed' ? 'success' : (scope.row.admission_status === 'running' ? 'warning' : 'danger')"
                              >
                                {{ scope.row.admission_status }}
                              </el-tag>
                            </template>
                          </el-table-column>
                          <el-table-column :label="t('validityStatusLabel')" width="100">
                            <template #default="scope">
                              <el-tag size="small" effect="plain" :type="scope.row.validity_status === 'valid' ? 'success' : (scope.row.validity_status === 'intake_rejected' ? 'danger' : 'info')">
                                {{ scope.row.validity_status || 'unverified' }}
                              </el-tag>
                            </template>
                          </el-table-column>
                          <el-table-column :label="t('previewStatusLabel')" width="70">
                            <template #default="scope">
                              <el-tag size="small" :type="scope.row.preview_available ? 'success' : 'info'">
                                {{ scope.row.preview_available ? '可用' : '—' }}
                              </el-tag>
                            </template>
                          </el-table-column>
                        </el-table>
                        <el-empty v-else :description="t('noEpisodesInPackage')" :image-size="48" />
                      </div>
                    </template>
                  </div>
                </el-drawer>

                <el-dialog v-model="rejectDialogVisible" :title="t('intakeReviewReject')" width="480px">
                  <p class="muted" style="margin-bottom: 12px; font-size: 13px;">请填写驳回该数据包的具体原因，驳回后该数据包将标记为作废并记录原因。</p>
                  <label class="login-field">
                    <span>{{ t('rejectReasonLabel') }} <strong style="color: #ef4444;">*</strong></span>
                    <el-input
                      v-model="rejectReason"
                      type="textarea"
                      :rows="4"
                      maxlength="1000"
                      show-word-limit
                      :placeholder="t('rejectReasonPlaceholder')"
                    />
                  </label>
                  <template #footer>
                    <el-button @click="rejectDialogVisible = false">{{ t('cancel') }}</el-button>
                    <el-button type="danger" :loading="rejectSubmitting" :disabled="!rejectReason.trim()" @click="submitRejectPackageIntake">
                      {{ t('intakeReviewReject') }}
                    </el-button>
                  </template>
                </el-dialog>
      </main>
    `,
  }).use(ElementPlus);
  QuicStudioPageComponents.install(app);
  app.mount('#app');
})();
