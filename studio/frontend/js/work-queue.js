/** Pure function module for Episode and WorkItem bounded pagination primitives and work queue state management. */
const QuicDataWorkQueue = (() => {
  function normalizePage(value) {
    const page = Number(value);
    return Number.isInteger(page) && page > 0 ? page : 1;
  }

  function normalizePageSize(value, allowed = [50, 100, 200]) {
    const pageSize = Number(value);
    return allowed.includes(pageSize) ? pageSize : allowed[0];
  }

  function pageQuery({ page, pageSize }) {
    const normalizedPage = normalizePage(page);
    const normalizedPageSize = normalizePageSize(pageSize);
    return {
      limit: normalizedPageSize,
      offset: (normalizedPage - 1) * normalizedPageSize,
    };
  }

  function pageResult(data, page, pageSize) {
    const normalizedPage = normalizePage(page);
    const normalizedPageSize = normalizePageSize(pageSize);
    const items = Array.isArray(data?.items) ? data.items : [];
    const responseTotal = Number(data?.total);
    const total = Number.isFinite(responseTotal) && responseTotal >= 0
      ? responseTotal
      : items.length;
    const lastPage = Math.max(1, Math.ceil(total / normalizedPageSize));
    return {
      items,
      total,
      page: normalizedPage,
      pageSize: normalizedPageSize,
      lastPage,
      needsReload: normalizedPage > lastPage,
    };
  }

  function requestSnapshot(request, allowedPageSizes = [50, 100]) {
    const snapshot = {
      workspaceId: request?.workspaceId ?? null,
      taskSetId: request?.taskSetId || null,
      stage: String(request?.stage || ''),
      page: normalizePage(request?.page),
      pageSize: normalizePageSize(request?.pageSize, allowedPageSizes),
    };
    const reviewTargetKind = request?.stage === 'review'
      && ['cut', 'annotation'].includes(request?.reviewTargetKind)
      ? request.reviewTargetKind
      : '';
    if (reviewTargetKind) snapshot.reviewTargetKind = reviewTargetKind;
    for (const key of [
      'taskLabelId',
      'status',
      'assigneeUserId',
      'updatedFrom',
      'updatedTo',
      'episodeKeyword',
      'sortBy',
      'sortOrder',
    ]) {
      const value = request?.[key];
      if (value !== undefined && value !== null && value !== '') snapshot[key] = value;
    }
    return snapshot;
  }

  function sameRequest(left, right) {
    if (!left || !right) return false;
    return [
      'workspaceId',
      'taskSetId',
      'taskLabelId',
      'status',
      'assigneeUserId',
      'updatedFrom',
      'updatedTo',
      'episodeKeyword',
      'sortBy',
      'sortOrder',
      'reviewTargetKind',
      'stage',
      'page',
      'pageSize',
    ]
      .every((key) => String(left[key] ?? '') === String(right[key] ?? ''));
  }

  function requestQuery(request, allowedPageSizes = [50, 100]) {
    const snapshot = requestSnapshot(request, allowedPageSizes);
    const page = pageQuery({ page: snapshot.page, pageSize: snapshot.pageSize });
    return {
      workspace_id: snapshot.workspaceId,
      task_set_id: snapshot.taskSetId || undefined,
      task_label_id: snapshot.taskLabelId || undefined,
      status: snapshot.status || undefined,
      assignee_user_id: snapshot.assigneeUserId || undefined,
      updated_from: snapshot.updatedFrom || undefined,
      updated_to: snapshot.updatedTo || undefined,
      episode_keyword: snapshot.episodeKeyword || undefined,
      sort_by: snapshot.sortBy || undefined,
      sort_order: snapshot.sortOrder || undefined,
      review_target_kind: snapshot.reviewTargetKind || undefined,
      stage: snapshot.stage,
      ...page,
    };
  }

  function rowMatchesStage(row, stage) {
    const item = row?.work_item;
    if (!item?.id) return false;
    if (stage === 'completed') {
      return item.kind === 'review'
        && item.review_target_kind === 'annotation'
        && item.status === 'accepted';
    }
    const terminal = stage === 'review'
      ? ['accepted', 'rejected', 'cancelled', 'stale']
      : ['accepted', 'cancelled', 'stale'];
    return item.kind === stage && !terminal.includes(item.status);
  }

  function applyActionRow(rows, total, nextRow, stage) {
    const items = Array.isArray(rows) ? rows : [];
    const itemId = Number(nextRow?.work_item?.id);
    const index = items.findIndex((row) => Number(row?.work_item?.id) === itemId);
    const normalizedTotal = Number.isFinite(Number(total)) && Number(total) >= 0
      ? Number(total)
      : items.length;
    if (!Number.isInteger(itemId) || itemId <= 0 || index < 0) {
      return { items, total: normalizedTotal, changed: false };
    }
    if (!rowMatchesStage(nextRow, stage)) {
      return {
        items: [...items.slice(0, index), ...items.slice(index + 1)],
        total: Math.max(0, normalizedTotal - 1),
        changed: true,
      };
    }
    const updated = items.slice();
    updated[index] = nextRow;
    return { items: updated, total: normalizedTotal, changed: true };
  }

  function createRefreshCoordinator({ refresh, setTimer, clearTimer, delayMs = 250 }) {
    if (typeof refresh !== 'function' || typeof setTimer !== 'function' || typeof clearTimer !== 'function') {
      throw new Error('refresh coordinator input is invalid');
    }
    let timer = null;
    let active = null;
    let trailing = false;

    function begin() {
      if (active) {
        trailing = true;
        return active;
      }
      if (timer !== null) clearTimer(timer);
      timer = null;
      let refreshResult;
      try {
        refreshResult = refresh();
      } catch (error) {
        refreshResult = Promise.reject(error);
      }
      const current = Promise.resolve(refreshResult);
      active = current;
      const settle = () => {
        if (active !== current) return;
        active = null;
        if (!trailing) return;
        trailing = false;
        void begin();
      };
      current.then(settle, settle);
      return current;
    }

    function schedule({ immediate = false } = {}) {
      if (active) {
        trailing = true;
        return active;
      }
      if (immediate) return begin();
      if (timer !== null) return Promise.resolve();
      timer = setTimer(() => {
        timer = null;
        void begin();
      }, delayMs);
      return Promise.resolve();
    }

    function cancel() {
      if (timer !== null) clearTimer(timer);
      timer = null;
      trailing = false;
    }

    return {
      begin,
      schedule,
      cancel,
      state: () => ({ scheduled: timer !== null, running: active !== null, trailing }),
    };
  }

  return {
    normalizePage,
    normalizePageSize,
    pageQuery,
    pageResult,
    requestSnapshot,
    sameRequest,
    requestQuery,
    rowMatchesStage,
    applyActionRow,
    createRefreshCoordinator,
  };
})();
