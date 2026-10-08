/* Uploaded packages and immutable batches. Each page reads its own bounded API projection. */
const QuicStudioDataOverview = (() => {
  const states = {
    pending_assignment: ['待分配', 'Awaiting assignment'], assigned: ['待上传', 'Awaiting upload'],
    pending_upload: ['待上传', 'Awaiting upload'], uploading: ['上传中', 'Uploading'],
    parsing: ['解析中', 'Parsing'], ingested: ['已解析', 'Parsed'],
    pending_intake_review: ['待入库审核', 'Awaiting intake review'], intake_approved: ['入库审核通过', 'Intake approved'],
    batched: ['已建批', 'Batched'], governing: ['治理中', 'Processing'],
    published: ['已沉淀', 'Deposited'], parse_failed: ['解析失败', 'Parse failed'], voided: ['已作废', 'Voided'],
    open: ['已创建', 'Created'], annotating: ['标注中', 'Annotation'], reviewing: ['结果待确认', 'Awaiting review'],
    publishing: ['沉淀中', 'Depositing'], no_publishable_asset: ['无有效内容', 'No valid content'],
    failed: ['失败', 'Failed'], passed: ['通过', 'Passed'], skipped: ['未启用', 'Disabled'],
    running: ['处理中', 'Running'], queued: ['等待处理', 'Queued'], pending: ['待标注', 'Pending annotation'],
    disabled: ['未启用', 'Disabled'], completed: ['已完成', 'Completed'],
  };
  function stageIssues(stage) {
    const result = stage?.result_json || {};
    const rows = [...(Array.isArray(result.issues) ? result.issues : []), ...(Array.isArray(result.dropped) ? result.dropped : [])];
    return rows.map(row => ({ episode: row.episode_id, reason: row.reason || '' }));
  }
  function createPager(api, state) {
    let generation = 0, pending = null, pendingKey = '';
    function dispose() { generation += 1; pending = null; pendingKey = ''; }
    function load(params) {
      const key = JSON.stringify(params);
      if (pending && key === pendingKey) return pending;
      const current = ++generation;
      state.items = []; state.total = 0; state.error = ''; state.loading = false;
      if (!params.workspace_id) return Promise.resolve();
      state.loading = true; pendingKey = key;
      pending = (async () => {
        try {
          const { tab, ...query } = params;
          const data = await (tab === 'packages' ? api.listDataPackages(query) : api.listDataBatches(query));
          if (current !== generation) return;
          if (!Array.isArray(data?.items) || !Number.isInteger(data.total)) throw new Error('Invalid list response');
          state.items = data.items; state.total = data.total;
        } catch (error) {
          if (current === generation) state.error = error?.message || 'Request failed';
        } finally { if (current === generation) { state.loading = false; pending = null; } }
      })();
      return pending;
    }
    return { load, dispose };
  }
  function install(app) {
    app.component('data-overview', {
      props: { workspaceId: [String, Number], projectId: [String, Number], locale: String, refreshKey: Number },
      emits: ['view-package', 'build'],
      setup(props, { emit }) {
        const { ref, reactive, computed, watch, onBeforeUnmount } = Vue;
        const text = (zh, en) => props.locale === 'en-US' ? en : zh;
        const state = reactive({ items: [], total: 0, loading: false, error: '' });
        const pager = createPager(QuicDataAPI, state);
        const tab = ref('packages'), page = ref(1), size = ref(50), status = ref('');
        const uploadCompletedRange = ref([]);
        const filters = reactive({ creatorId: null, collectorId: null, deviceId: null, purposeLabelId: null, sceneLabelId: null, modalityLabelId: null, trainingLabelId: null });
        const batchUploadCompletedRange = ref([]);
        const batchFilters = reactive({ purposeLabelId: null, sceneLabelId: null, trainingLabelId: null, modalityLabelId: null, integrityStatus: '', qualityStatus: '', complianceStatus: '', annotationStatus: '' });
        const creators = ref([]), collectors = ref([]), devices = ref([]), labels = ref([]), optionsLoading = ref(false);
        const selected = ref([]), detail = ref(null), detailVisible = ref(false);
        const detailLoading = ref(false), detailError = ref(''), report = ref(null), reportError = ref('');
        const retrying = ref('');
        let scopeGeneration = 0, detailGeneration = 0;
        const label = (value) => states[value] ? text(...states[value]) : (value || '—');
        const tagType = value => ({
          published:'success', intake_approved:'success', passed:'success',
          failed:'danger', parse_failed:'danger', voided:'info',
          governing:'warning', running:'warning', queued:'warning', uploading:'warning',
          pending_intake_review:'warning', reviewing:'warning',
        })[value] || 'info';
        const statusOptions = computed(() => Object.entries(states).slice(0, 13).map(([value, labels]) => ({value, label:text(...labels)})));
        const governanceStatusOptions = computed(() => ['queued', 'running', 'passed', 'failed', 'skipped'].map(value => ({ value, label:label(value) })));
        const annotationStatusOptions = computed(() => ['disabled', 'pending', 'annotating', 'reviewing', 'completed'].map(value => ({ value, label:label(value) })));
        const hours = value => value === null || value === undefined || value === '' ? '—' : Number(value).toLocaleString(props.locale || 'zh-CN', {maximumFractionDigits:2});
        const date = value => value ? new Date(value).toLocaleString(props.locale || 'zh-CN') : '—';
        const collectionTime = row => date(row?.captured_started_at);
        const stageName = value => ({integrity:text('完整性检查','Integrity'), quality:text('自动质检','Automated QC'), compliance:text('脱敏','De-identification')})[value] || value;
        async function reload() {
          selected.value = [];
          await pager.load({ workspace_id:props.workspaceId, tab:tab.value, page:page.value, size:size.value,
            ...(tab.value === 'packages' ? {
              collection_project_id:props.projectId || undefined,
              status:status.value || undefined,
              operator_collector_id:filters.collectorId || undefined,
              collection_device_id:filters.deviceId || undefined,
              purpose_label_id:filters.purposeLabelId || undefined,
              scene_label_id:filters.sceneLabelId || undefined,
              modality_label_id:filters.modalityLabelId || undefined,
              training_label_id:filters.trainingLabelId || undefined,
              created_by_user_id:filters.creatorId || undefined,
              upload_completed_from:uploadCompletedRange.value?.[0] || undefined,
              upload_completed_to:uploadCompletedRange.value?.[1] || undefined,
            } : {
              collection_project_id:props.projectId || undefined,
              purpose_label_id:batchFilters.purposeLabelId || undefined,
              scene_label_id:batchFilters.sceneLabelId || undefined,
              training_label_id:batchFilters.trainingLabelId || undefined,
              modality_label_id:batchFilters.modalityLabelId || undefined,
              integrity_status:batchFilters.integrityStatus || undefined,
              quality_status:batchFilters.qualityStatus || undefined,
              compliance_status:batchFilters.complianceStatus || undefined,
              annotation_status:batchFilters.annotationStatus || undefined,
              upload_completed_from:batchUploadCompletedRange.value?.[0] || undefined,
              upload_completed_to:batchUploadCompletedRange.value?.[1] || undefined,
            }) });
        }
        function changeFilter() { page.value = 1; return reload(); }
        function labelOptions(category) {
          const categories = category === 'training' ? ['training', 'train'] : [category];
          return labels.value.filter((item) => categories.includes(item.category));
        }
        function rowLabels(row, category) {
          const categories = category === 'training' ? ['training', 'train'] : [category];
          const names = (row?.labels || []).filter((item) => categories.includes(item.category)).map((item) => item.name).filter(Boolean);
          if (category === 'modality' && !names.length) return Array.isArray(row?.modalities) ? row.modalities : [];
          return names;
        }
        function rowLabelText(row, category) { return rowLabels(row, category).join('、') || '—'; }
        async function loadOptions() {
          const workspaceId = props.workspaceId;
          creators.value = []; collectors.value = []; devices.value = []; labels.value = [];
          if (!workspaceId) return;
          optionsLoading.value = true;
          try {
            const [creatorResult, collectorResult, deviceResult, labelResult] = await Promise.all([
              QuicDataAPI.listDataPackageCreators(workspaceId),
              QuicDataAPI.listCollectorProfiles(workspaceId),
              QuicDataAPI.listCollectionDevices(workspaceId),
              QuicDataAPI.listCollectionLabels(workspaceId),
            ]);
            if (String(workspaceId) !== String(props.workspaceId)) return;
            creators.value = creatorResult?.items || [];
            collectors.value = collectorResult?.items || [];
            devices.value = deviceResult?.items || [];
            labels.value = labelResult?.items || [];
          } catch (error) {
            if (String(workspaceId) === String(props.workspaceId)) state.error = error?.message || text('筛选项加载失败','Filter options failed to load');
          } finally {
            if (String(workspaceId) === String(props.workspaceId)) optionsLoading.value = false;
          }
        }
        function resetFilters() {
          if (tab.value === 'packages') {
            status.value = '';
            uploadCompletedRange.value = [];
            Object.keys(filters).forEach((key) => { filters[key] = null; });
          } else {
            batchUploadCompletedRange.value = [];
            Object.keys(batchFilters).forEach((key) => { batchFilters[key] = key.endsWith('Status') ? '' : null; });
          }
          return changeFilter();
        }
        function closeDetail() { detailGeneration += 1; detail.value = null; report.value = null; detailVisible.value = false; detailLoading.value = false; retrying.value = ''; }
        async function openBatch(row) {
          const generation = ++detailGeneration, workspaceId = props.workspaceId;
          detail.value = null; report.value = null; detailError.value = ''; reportError.value = '';
          detailVisible.value = true; detailLoading.value = true;
          const results = await Promise.allSettled([
            QuicDataAPI.getDataBatch(row.id, { workspace_id: workspaceId }),
            QuicDataAPI.getDataBatchGovernanceReport(row.id, workspaceId),
          ]);
          if (generation !== detailGeneration || String(workspaceId) !== String(props.workspaceId)) return;
          detailLoading.value = false;
          if (results[0].status === 'fulfilled') detail.value = results[0].value;
          else detailError.value = results[0].reason?.message || text('详情加载失败','Could not load details');
          if (results[1].status === 'fulfilled') report.value = results[1].value;
          else reportError.value = results[1].reason?.message || text('治理报告暂不可用','Governance report unavailable');
        }
        async function retry(stage) {
          if (!detail.value || retrying.value) return;
          const workspaceId = props.workspaceId, row = detail.value, generation = detailGeneration;
          retrying.value = stage.stage;
          try {
            await QuicDataAPI.retryDataBatchGovernance(row.id,{workspace_id:workspaceId,stage:stage.stage});
            if (generation !== detailGeneration || String(workspaceId) !== String(props.workspaceId) || !detailVisible.value) return;
            await openBatch(row); await reload();
          } catch (error) { if (generation === detailGeneration) reportError.value = error?.message || text('重试失败','Retry failed'); }
          finally { if (String(workspaceId) === String(props.workspaceId)) retrying.value = ''; }
        }
        watch(() => props.workspaceId, () => {
          scopeGeneration += 1; closeDetail(); page.value = 1; status.value = ''; uploadCompletedRange.value = []; batchUploadCompletedRange.value = [];
          Object.keys(filters).forEach((key) => { filters[key] = null; });
          Object.keys(batchFilters).forEach((key) => { batchFilters[key] = key.endsWith('Status') ? '' : null; });
          void reload(); void loadOptions();
        }, {immediate:true});
        watch(() => props.projectId, () => { page.value = 1; void reload(); });
        watch(() => props.refreshKey, () => { void reload(); });
        onBeforeUnmount(() => { pager.dispose(); scopeGeneration += 1; detailGeneration += 1; });
        return { state, tab, page, size, status, uploadCompletedRange, filters, batchUploadCompletedRange, batchFilters, creators, collectors, devices, optionsLoading, selected, text, label, tagType, hours, date, collectionTime, stageName,
          statusOptions, governanceStatusOptions, annotationStatusOptions, reload, changeFilter, detail, detailVisible, detailLoading, detailError, report, reportError,
          openBatch, closeDetail, retry, retrying, stageIssues, labelOptions, rowLabelText, resetFilters,
          selectRows: rows => { selected.value = rows; }, canSelect: row => row.can_batch === true,
          viewPackage: row => emit('view-package',row),
          build: () => emit('build', selected.value.map(row => row.id)),
        };
      },
      template: `
        <section class="view-stack data-overview-view">
          <div class="data-overview-tabs-bar intake-tabs-bar">
            <el-tabs v-model="tab" class="intake-tabs" @tab-change="changeFilter"><el-tab-pane :label="text('采集数据','Collected data')" name="packages"/><el-tab-pane :label="text('数据批次','Data batches')" name="batches"/></el-tabs>
            <el-button v-if="tab === 'packages'" type="primary" :disabled="!selected.length || state.loading" @click="build">{{ text('构建数据批次','Create batch') }} ({{ selected.length }})</el-button>
          </div>
          <section class="surface-panel table-panel data-overview-panel" v-loading="state.loading">
            <div class="data-overview-filter-bar list-filter-bar">
              <div class="data-overview-filters">
                <slot name="scope-filters"></slot>
                <template v-if="tab === 'packages'">
                <el-select v-model="status" clearable :placeholder="text('数据包状态','Package status')" @change="changeFilter"><el-option v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label"/></el-select>
                <el-select v-model="filters.creatorId" clearable filterable :loading="optionsLoading" :placeholder="text('新建人员','Created by')" @change="changeFilter"><el-option v-for="item in creators" :key="item.id" :value="item.id" :label="item.name || item.email"/></el-select>
                <el-select v-model="filters.collectorId" clearable filterable :loading="optionsLoading" :placeholder="text('数采员','Collector')" @change="changeFilter"><el-option v-for="item in collectors" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-select v-model="filters.deviceId" clearable filterable :loading="optionsLoading" :placeholder="text('主采集设备','Primary device')" @change="changeFilter"><el-option v-for="item in devices" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-select v-model="filters.purposeLabelId" clearable filterable :loading="optionsLoading" :placeholder="text('任务用途','Task purpose')" @change="changeFilter"><el-option v-for="item in labelOptions('purpose')" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-select v-model="filters.sceneLabelId" clearable filterable :loading="optionsLoading" :placeholder="text('场景标签','Scene')" @change="changeFilter"><el-option v-for="item in labelOptions('scene')" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-select v-model="filters.modalityLabelId" clearable filterable :loading="optionsLoading" :placeholder="text('数据模态','Modality')" @change="changeFilter"><el-option v-for="item in labelOptions('modality')" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-select v-model="filters.trainingLabelId" clearable filterable :loading="optionsLoading" :placeholder="text('训练标签','Training label')" @change="changeFilter"><el-option v-for="item in labelOptions('training')" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-date-picker v-model="uploadCompletedRange" class="data-overview-date-range" type="datetimerange" value-format="YYYY-MM-DDTHH:mm:ssZ" :range-separator="locale === 'en-US' ? 'to' : '至'" :start-placeholder="text('上传完成时间','Upload completed from')" :end-placeholder="text('上传完成时间','Upload completed to')" @change="changeFilter" />
                </template>
                <template v-else>
                <el-select v-model="batchFilters.sceneLabelId" clearable filterable :loading="optionsLoading" :placeholder="text('场景标签','Scene')" @change="changeFilter"><el-option v-for="item in labelOptions('scene')" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-select v-model="batchFilters.purposeLabelId" clearable filterable :loading="optionsLoading" :placeholder="text('任务用途','Task purpose')" @change="changeFilter"><el-option v-for="item in labelOptions('purpose')" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-select v-model="batchFilters.trainingLabelId" clearable filterable :loading="optionsLoading" :placeholder="text('训练标签','Training label')" @change="changeFilter"><el-option v-for="item in labelOptions('training')" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-select v-model="batchFilters.modalityLabelId" clearable filterable :loading="optionsLoading" :placeholder="text('数据模态','Modality')" @change="changeFilter"><el-option v-for="item in labelOptions('modality')" :key="item.id" :value="item.id" :label="item.name"/></el-select>
                <el-select v-model="batchFilters.integrityStatus" clearable :placeholder="text('完整性检查','Integrity')" @change="changeFilter"><el-option v-for="item in governanceStatusOptions" :key="item.value" :value="item.value" :label="item.label"/></el-select>
                <el-select v-model="batchFilters.qualityStatus" clearable :placeholder="text('自动质检','Automated QC')" @change="changeFilter"><el-option v-for="item in governanceStatusOptions" :key="item.value" :value="item.value" :label="item.label"/></el-select>
                <el-select v-model="batchFilters.complianceStatus" clearable :placeholder="text('脱敏','De-identification')" @change="changeFilter"><el-option v-for="item in governanceStatusOptions" :key="item.value" :value="item.value" :label="item.label"/></el-select>
                <el-select v-model="batchFilters.annotationStatus" clearable :placeholder="text('标注状态','Annotation status')" @change="changeFilter"><el-option v-for="item in annotationStatusOptions" :key="item.value" :value="item.value" :label="item.label"/></el-select>
                <el-date-picker v-model="batchUploadCompletedRange" class="data-overview-date-range" type="datetimerange" value-format="YYYY-MM-DDTHH:mm:ssZ" :range-separator="locale === 'en-US' ? 'to' : '至'" :start-placeholder="text('上传完成时间','Upload completed from')" :end-placeholder="text('上传完成时间','Upload completed to')" @change="changeFilter" />
                </template>
              </div>
              <div class="data-overview-filter-summary"><el-button :disabled="state.loading" @click="resetFilters">{{ text('清除筛选','Clear') }}</el-button><el-button :disabled="state.loading" @click="reload">{{ text('刷新','Refresh') }}</el-button><span class="muted data-overview-filter-total">{{ state.total }} {{ text('项','items') }}</span></div>
            </div>
            <el-alert v-if="state.error" :title="state.error" type="error" :closable="false" show-icon/>
            <p v-if="tab === 'packages'" class="data-overview-hint">{{ text('展示当前数采工作空间的全部采集数据；只有入库审核通过、含有效 Episode 且尚未建批的数据包可以选择。','All collected data in this workspace is shown. Only intake-approved packages with valid Episodes that have not been batched can be selected.') }}</p>
            <el-table v-if="tab === 'packages'" :data="state.items" row-key="id" class="data-table" @selection-change="selectRows">
              <el-table-column type="selection" :selectable="canSelect" width="46"/>
              <el-table-column :label="text('数据包名称','Package')" min-width="320"><template #default="scope"><div class="data-overview-primary-cell"><el-button class="data-overview-package-name" link type="primary" :title="scope.row.package_uid" @click="viewPackage(scope.row)">{{ scope.row.package_uid }}</el-button><small>{{ scope.row.task_name || scope.row.collection_task_name || text('未关联采集任务','No collection task') }}</small></div></template></el-table-column>
              <el-table-column prop="project_name" :label="text('采集项目','Project')" min-width="135"/>
              <el-table-column prop="collector_name" :label="text('数采员','Collector')" min-width="120"/>
              <el-table-column prop="device_name" :label="text('主采集设备','Primary device')" min-width="150" show-overflow-tooltip/>
              <el-table-column :label="text('任务用途','Task purpose')" min-width="130" show-overflow-tooltip><template #default="scope">{{ rowLabelText(scope.row, 'purpose') }}</template></el-table-column>
              <el-table-column :label="text('场景标签','Scene')" min-width="130" show-overflow-tooltip><template #default="scope">{{ rowLabelText(scope.row, 'scene') }}</template></el-table-column>
              <el-table-column :label="text('数据模态','Modality')" min-width="120" show-overflow-tooltip><template #default="scope">{{ rowLabelText(scope.row, 'modality') }}</template></el-table-column>
              <el-table-column :label="text('训练用途','Training use')" min-width="130" show-overflow-tooltip><template #default="scope">{{ rowLabelText(scope.row, 'training') }}</template></el-table-column>
              <el-table-column prop="episode_count" label="Episodes" width="88"/>
              <el-table-column :label="text('采集时长 (h)','Captured (h)')" width="112"><template #default="scope">{{ hours(scope.row.captured_duration_hours) }}</template></el-table-column>
              <el-table-column :label="text('入库有效时长 (h)','Intake valid (h)')" width="128"><template #default="scope">{{ hours(scope.row.intake_valid_duration_hours) }}</template></el-table-column>
              <el-table-column :label="text('状态','Status')" min-width="130"><template #default="scope"><div class="data-overview-status-stack"><el-tag size="small" effect="plain" :type="tagType(scope.row.status)">{{ label(scope.row.status) }}</el-tag><small v-if="scope.row.can_batch">{{ text('可构建批次','Ready to batch') }}</small></div></template></el-table-column>
              <el-table-column :label="text('上传完成时间','Upload completed')" min-width="168"><template #default="scope">{{ date(scope.row.upload_completed_at) }}</template></el-table-column>
              <el-table-column :label="text('采集时间','Collection time')" min-width="168"><template #default="scope">{{ collectionTime(scope.row) }}</template></el-table-column>
            </el-table>
            <el-table v-else :data="state.items" row-key="id" class="data-table">
              <el-table-column :label="text('数据批次名称','Data batch')" min-width="240"><template #default="scope"><div class="data-overview-primary-cell"><el-button link type="primary" @click="openBatch(scope.row)">{{ scope.row.name }}</el-button><small>#{{ scope.row.id }} · {{ scope.row.package_count || 0 }} {{ text('个数据包','packages') }}</small></div></template></el-table-column>
              <el-table-column :label="text('状态','Status')" min-width="130"><template #default="scope"><div class="data-overview-status-stack"><el-tag size="small" effect="plain" :type="tagType(scope.row.status)">{{ label(scope.row.status) }}</el-tag></div></template></el-table-column>
              <el-table-column prop="package_count" :label="text('数据包数','Packages')" width="100"/>
              <el-table-column prop="episode_count" label="Episodes" width="100"/>
              <el-table-column :label="text('有效时长 (h)','Valid (h)')" width="130"><template #default="scope">{{ hours(scope.row.valid_duration_hours) }}</template></el-table-column>
              <el-table-column :label="text('治理状态','Governance')" min-width="260"><template #default="scope"><div v-if="scope.row.stages.length" class="data-overview-stage-list"><el-tag v-for="stage in scope.row.stages" :key="stage.stage" size="small" effect="plain" :type="tagType(stage.status)">{{ stageName(stage.stage) }} · {{ label(stage.status) }}</el-tag></div><span v-else class="muted">—</span></template></el-table-column>
              <el-table-column :label="text('标注状态','Annotation status')" width="130"><template #default="scope">{{ scope.row.annotation_status ? label(scope.row.annotation_status) : (scope.row.annotation_enabled ? text('已启用','Enabled') : text('未启用','Disabled')) }}</template></el-table-column>
              <el-table-column :label="text('上传完成时间','Upload completed')" min-width="168"><template #default="scope">{{ date(scope.row.upload_completed_at) }}</template></el-table-column>
              <el-table-column :label="text('创建时间','Created')" min-width="185"><template #default="scope">{{ date(scope.row.created_at) }}</template></el-table-column>
            </el-table>
            <el-pagination v-model:current-page="page" v-model:page-size="size" :page-sizes="[20,50,100]" :total="state.total" layout="total, sizes, prev, pager, next" @current-change="reload" @size-change="changeFilter"/>
          </section>
          <el-drawer v-model="detailVisible" :title="detail?.name || text('批次详情','Batch details')" size="min(880px, 94vw)" @closed="closeDetail">
            <div class="view-stack" v-loading="detailLoading">
              <el-alert v-if="detailError" :title="detailError" type="error" :closable="false"/>
              <template v-if="detail">
                <div>{{ label(detail.status) }} · {{ detail.episode_count }} Episodes · {{ hours(detail.valid_duration_hours) }} h</div>
                <el-table :data="detail.packages || []"><el-table-column :label="text('数据包','Package')" min-width="280"><template #default="scope"><el-button link type="primary" @click="viewPackage(scope.row)">{{ scope.row.package_uid }}</el-button></template></el-table-column><el-table-column prop="task_name" :label="text('采集任务','Task')"/><el-table-column prop="collector_name" :label="text('数采员','Collector')"/></el-table>
                <h3>{{ text('治理报告','Governance report') }}</h3>
                <el-alert v-if="reportError" :title="reportError" type="error" :closable="false"/>
                <section v-for="stage in report?.stages || []" :key="stage.stage" class="surface-panel">
                  <div class="page-actions"><strong>{{ stageName(stage.stage) }}</strong><el-tag>{{ label(stage.status) }}</el-tag><el-button v-if="stage.status === 'failed'" :loading="retrying === stage.stage" :disabled="!!retrying" @click="retry(stage)">{{ text('重试','Retry') }}</el-button></div>
                  <el-alert v-if="stage.error_message" :title="stage.error_message" type="error" :closable="false"/>
                  <ul v-if="stageIssues(stage).length"><li v-for="(issue, index) in stageIssues(stage)" :key="index">Episode {{ issue.episode }} · {{ issue.reason }}</li></ul>
                  <p v-else-if="stage.status === 'passed'" class="muted">{{ text('检查完成，无异常条目。','Check completed with no reported issues.') }}</p>
                </section>
              </template>
            </div>
          </el-drawer>
        </section>`,
    });
  }
  return { createPager, stageIssues, install };
})();
