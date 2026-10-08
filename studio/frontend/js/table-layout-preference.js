/** Compact table column width user local preference persistence module (browser-side non-authoritative storage). */
const QuicDataTableLayoutPreference = (() => {
  const VERSION = 1;
  const PREFIX = 'quicdata.table-layout.v1.';
  const MIN_WIDTH = 64;
  const MAX_WIDTH = 720;
  const KEY_PATTERN = /^[a-z][a-z0-9_-]{0,63}$/;

  function positiveId(value) {
    const id = Number(value);
    return Number.isInteger(id) && id > 0 ? id : null;
  }

  function validKey(value) {
    return typeof value === 'string' && KEY_PATTERN.test(value);
  }

  function storageKey({ userId, workspaceId, tableKey } = {}) {
    const user = positiveId(userId);
    const workspace = positiveId(workspaceId);
    if (user === null || workspace === null || !validKey(tableKey)) return '';
    return `${PREFIX}${user}.${workspace}.${tableKey}`;
  }

  function normalizeWidth(value) {
    const width = Math.round(Number(value));
    if (!Number.isFinite(width)) return null;
    return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, width));
  }

  function read(storage, scope) {
    const key = storageKey(scope);
    if (!key || !storage || typeof storage.getItem !== 'function') return { version: VERSION, columns: {} };
    try {
      const payload = JSON.parse(storage.getItem(key) || '');
      if (!payload || payload.version !== VERSION || !payload.columns || typeof payload.columns !== 'object') {
        return { version: VERSION, columns: {} };
      }
      const columns = {};
      for (const [columnKey, width] of Object.entries(payload.columns)) {
        const normalized = normalizeWidth(width);
        if (validKey(columnKey) && normalized !== null) columns[columnKey] = normalized;
      }
      return { version: VERSION, columns };
    } catch {
      return { version: VERSION, columns: {} };
    }
  }

  function writeColumn(storage, scope, columnKey, width) {
    const key = storageKey(scope);
    const normalized = normalizeWidth(width);
    if (!key || !validKey(columnKey) || normalized === null || !storage || typeof storage.setItem !== 'function') return false;
    try {
      const current = read(storage, scope);
      storage.setItem(key, JSON.stringify({
        version: VERSION,
        columns: { ...current.columns, [columnKey]: normalized },
      }));
      return true;
    } catch {
      return false;
    }
  }

  function resolveWidth(storage, scope, columnKey, fallback) {
    const stored = read(storage, scope).columns[columnKey];
    return stored === undefined ? fallback : stored;
  }

  function reset(storage, scope) {
    const key = storageKey(scope);
    if (!key || !storage || typeof storage.removeItem !== 'function') return false;
    try {
      storage.removeItem(key);
      return true;
    } catch {
      return false;
    }
  }

  function columnKey(column) {
    const value = column?.columnKey || column?.property || '';
    return validKey(value) ? value : '';
  }

  return Object.freeze({
    VERSION,
    MIN_WIDTH,
    MAX_WIDTH,
    storageKey,
    normalizeWidth,
    read,
    writeColumn,
    resolveWidth,
    reset,
    columnKey,
  });
})();

if (typeof globalThis !== 'undefined') globalThis.QuicDataTableLayoutPreference = QuicDataTableLayoutPreference;
