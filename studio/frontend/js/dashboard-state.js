/** Pure function state helper for tenant-level dashboard aggregated snapshots and refresh jobs. */
const QuicDataDashboardState = (() => {
  function positiveId(value) {
    const number = Number(value);
    return Number.isInteger(number) && number > 0 ? number : null;
  }

  function resolveScope(user, workspaceId, taskSetId) {
    const workspace_id = positiveId(workspaceId);
    const task_set_id = positiveId(taskSetId);
    if (task_set_id !== null && workspace_id === null) throw new Error('看板采集项目必须属于一个数采工作空间');
    if (workspace_id === null && user?.role !== 'admin') throw new Error('请选择数采工作空间');
    return { workspace_id, task_set_id };
  }

  function scopeKey(scope) {
    const taskSetId = positiveId(scope?.task_set_id);
    if (taskSetId !== null) return `task-set:${taskSetId}`;
    const workspaceId = positiveId(scope?.workspace_id);
    if (workspaceId !== null) return `workspace:${workspaceId}`;
    return 'global';
  }

  function isFreshSnapshot(snapshot, receipt) {
    if (!snapshot || !receipt || snapshot.scope?.key !== receipt.scope_key) return false;
    if (snapshot.source === 'live') return true;
    if (String(snapshot.etl_job_id || '') === String(receipt.job_id || '')) return true;
    if (!receipt.baseline_computed_at) return Boolean(snapshot.computed_at);
    const computedAt = Date.parse(snapshot.computed_at || '');
    const baselineAt = Date.parse(receipt.baseline_computed_at || '');
    return Number.isFinite(computedAt) && Number.isFinite(baselineAt) && computedAt > baselineAt;
  }

  function jobSucceeded(job) {
    return job?.status === 'succeeded';
  }

  function jobFailed(job) {
    return ['failed', 'cancelled'].includes(job?.status);
  }

  const FUNNEL_QUEUE_ROWS = [
    { key: 'intake', zh: '待采集', en: 'To collect' },
    { key: 'collected', zh: '待处理', en: 'To process' },
    { key: 'separated', zh: '待标注', en: 'To annotate' },
    { key: 'annotated', zh: '待标注审核', en: 'To review' },
    { key: 'stored', zh: '待入库', en: 'To store' },
  ];
  const DURATION_BUCKETS = ['lt_30s', 'bt_30_60s', 'gt_60s', 'unknown'];

  function stageBucketTotal(stage) {
    const buckets = stage?.buckets || {};
    return DURATION_BUCKETS.reduce((sum, key) => sum + Number(buckets[key] || 0), 0);
  }

  function todayQueuesFromFunnel(payload, locale = 'zh-CN') {
    const stages = Array.isArray(payload?.pipeline_funnel?.stages) ? payload.pipeline_funnel.stages : [];
    const counts = Object.fromEntries(FUNNEL_QUEUE_ROWS.map((row) => [row.key, 0]));
    stages.forEach((stage) => {
      const count = stageBucketTotal(stage);
      if (Object.prototype.hasOwnProperty.call(counts, stage.key)) counts[stage.key] += count;
      else counts.intake += count;
    });
    const total = Number(payload?.kpis?.total_episodes || 0);
    const denominator = total > 0 ? total : Object.values(counts).reduce((sum, count) => sum + count, 0);
    const english = locale === 'en-US' || locale === 'en';
    return FUNNEL_QUEUE_ROWS.map((row) => ({
      key: row.key,
      label: english ? row.en : row.zh,
      count: counts[row.key],
      pending: counts[row.key],
      share: denominator ? counts[row.key] / denominator : 0,
    }));
  }

  return Object.freeze({
    resolveScope,
    scopeKey,
    isFreshSnapshot,
    jobSucceeded,
    jobFailed,
    todayQueuesFromFunnel,
  });
})();
