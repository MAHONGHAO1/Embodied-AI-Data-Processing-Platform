/** Versioned local scope preference storage module for console workspaces and task sets (does not participate in server authorization). */
const QuicDataScopePreference = (() => {
  const VERSION = 1;
  const PREFIX = 'quicdata.scope-preference.v1.';

  function positiveId(value) {
    const id = Number(value);
    return Number.isInteger(id) && id > 0 ? id : null;
  }

  function storageKey(userId) {
    const id = positiveId(userId);
    return id === null ? '' : `${PREFIX}${id}`;
  }

  function parse(value) {
    if (typeof value !== 'string' || !value) return null;
    try {
      const parsed = JSON.parse(value);
      const workspaceId = positiveId(parsed?.workspace_id);
      const taskSetId = positiveId(parsed?.task_set_id);
      if (parsed?.version !== VERSION || workspaceId === null || taskSetId === null) return null;
      return { version: VERSION, workspace_id: workspaceId, task_set_id: taskSetId };
    } catch {
      return null;
    }
  }

  function read(storage, userId) {
    const key = storageKey(userId);
    if (!key || !storage || typeof storage.getItem !== 'function') return null;
    try {
      return parse(storage.getItem(key));
    } catch {
      return null;
    }
  }

  function write(storage, userId, scope) {
    const key = storageKey(userId);
    const workspaceId = positiveId(scope?.workspace_id);
    const taskSetId = positiveId(scope?.task_set_id);
    if (!key || workspaceId === null || taskSetId === null || !storage || typeof storage.setItem !== 'function') return false;
    try {
      storage.setItem(key, JSON.stringify({ version: VERSION, workspace_id: workspaceId, task_set_id: taskSetId }));
      return true;
    } catch {
      return false;
    }
  }

  function includesId(items, value) {
    const id = positiveId(value);
    return id !== null && Array.isArray(items) && items.some((item) => positiveId(item?.id) === id);
  }

  function resolve({ workspaceItems, taskSetItems, saved, fallbackWorkspaceId, fallbackTaskSetId } = {}) {
    const savedWorkspaceId = includesId(workspaceItems, saved?.workspace_id) ? positiveId(saved.workspace_id) : null;
    const workspaceId = savedWorkspaceId || (includesId(workspaceItems, fallbackWorkspaceId) ? positiveId(fallbackWorkspaceId) : null);
    const savedTaskSetId = savedWorkspaceId && includesId(taskSetItems, saved?.task_set_id)
      ? positiveId(saved.task_set_id) : null;
    const taskSetId = savedTaskSetId || (includesId(taskSetItems, fallbackTaskSetId) ? positiveId(fallbackTaskSetId) : null);
    return { workspace_id: workspaceId, task_set_id: taskSetId };
  }

  return Object.freeze({ VERSION, storageKey, read, write, resolve });
})();

if (typeof globalThis !== 'undefined') globalThis.QuicDataScopePreference = QuicDataScopePreference;
