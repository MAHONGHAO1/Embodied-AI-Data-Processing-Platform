/** Same-origin Socket.IO real-time event subscription and reconnect snapshot alignment module. */
const QuicDataRealtime = (() => {
  const resourceEvents = [
    'batch.updated',
    'import_session.updated',
    'job_run.updated',
    'episode.updated',
    'work_item.updated',
    'publication.updated',
    'work_queue.invalidated',
  ];
  const subscriptions = new Map();
  let socket = null;
  let connectionToken = '';

  function subscriptionKey(resourceType, resourceId) {
    return `${resourceType}:${resourceId}`;
  }

  function validSubscription(resourceType, resourceId) {
    return typeof resourceType === 'string' && resourceType.length > 0
      && typeof resourceId === 'string' && resourceId.length > 0;
  }

  function currentSocket() {
    return socket && socket.connected ? socket : null;
  }

  async function refreshAndSubscribe(entry, { refresh = true } = {}) {
    const activeSocket = currentSocket();
    if (!activeSocket || subscriptions.get(entry.key) !== entry) return;
    if (refresh) {
      try {
        const snapshot = entry.refresh ? await entry.refresh() : null;
        if (snapshot && typeof snapshot === 'object') {
          const version = Number(snapshot.realtime_version);
          if (Number.isInteger(version) && version >= entry.version) {
            entry.version = version;
            entry.onUpdate(snapshot);
          } else if (entry.refreshAlways) {
            entry.onUpdate(snapshot);
          }
        }
      } catch {
        // A reconnect snapshot is best-effort. The server remains authoritative.
      }
    }
    if (currentSocket() === activeSocket && subscriptions.get(entry.key) === entry) {
      activeSocket.emit('subscribe', {
        resource_type: entry.resourceType,
        resource_id: entry.resourceId,
      });
    }
  }

  async function restoreSubscriptions() {
    for (const entry of subscriptions.values()) {
      await refreshAndSubscribe(entry, { refresh: entry.refreshOnReconnect });
    }
  }

  function deliverResourceEvent(entry, payload, resourceVersion) {
    if (resourceVersion <= entry.version || !payload.resource || typeof payload.resource !== 'object') return;
    entry.version = resourceVersion;
    entry.onUpdate({ ...payload.resource, realtime_version: resourceVersion });
  }

  function handleResourceEvent(payload) {
    if (!payload || typeof payload !== 'object') return;
    const resourceType = payload.resource_type;
    const resourceId = payload.resource_id;
    const resourceVersion = Number(payload.resource_version);
    if (!validSubscription(resourceType, resourceId) || !Number.isInteger(resourceVersion)) return;
    const entry = subscriptions.get(subscriptionKey(resourceType, resourceId));
    if (!entry || resourceVersion <= entry.version || !payload.resource || typeof payload.resource !== 'object') return;
    if (entry.deferredEventCount > 0) {
      if (!entry.deferredEvent || resourceVersion > Number(entry.deferredEvent.resource_version)) {
        entry.deferredEvent = payload;
      }
      return;
    }
    deliverResourceEvent(entry, payload, resourceVersion);
  }

  function connect({ accessToken } = {}) {
    if (typeof accessToken !== 'string' || !accessToken || typeof io !== 'function') return false;
    if (socket && connectionToken === accessToken) return true;
    if (socket) socket.disconnect();
    connectionToken = accessToken;
    const socketOrigin = typeof window !== 'undefined' && window.location?.port === '8090'
      ? `${window.location.protocol}//${window.location.hostname}:8000`
      : (typeof window !== 'undefined' ? window.location.origin : '');
    socket = io(socketOrigin, {
      path: '/socket.io',
      transports: ['websocket'],
      auth: { access_token: accessToken },
    });
    socket.on('connect', () => { void restoreSubscriptions(); });
    for (const eventName of resourceEvents) socket.on(eventName, handleResourceEvent);
    return true;
  }

  function subscribe({
    resourceType,
    resourceId,
    realtimeVersion = 0,
    refresh,
    refreshAlways = false,
    refreshOnSubscribe = true,
    refreshOnReconnect = true,
    onUpdate,
  } = {}) {
    const normalizedId = String(resourceId || '');
    if (!validSubscription(resourceType, normalizedId) || typeof onUpdate !== 'function') return () => {};
    const key = subscriptionKey(resourceType, normalizedId);
    const version = Number(realtimeVersion);
    const entry = {
      key,
      resourceType,
      resourceId: normalizedId,
      version: Number.isInteger(version) && version >= 0 ? version : 0,
      refresh,
      refreshAlways: Boolean(refreshAlways),
      refreshOnSubscribe: Boolean(refreshOnSubscribe),
      refreshOnReconnect: Boolean(refreshOnReconnect),
      deferredEventCount: 0,
      deferredEvent: null,
      onUpdate,
    };
    subscriptions.set(key, entry);
    if (currentSocket()) void refreshAndSubscribe(entry, { refresh: entry.refreshOnSubscribe });
    return () => {
      if (subscriptions.get(key) === entry) subscriptions.delete(key);
    };
  }

  function acknowledge({ resourceType, resourceId, realtimeVersion } = {}) {
    const normalizedId = String(resourceId || '');
    const version = Number(realtimeVersion);
    if (!validSubscription(resourceType, normalizedId) || !Number.isInteger(version) || version < 0) return false;
    const entry = subscriptions.get(subscriptionKey(resourceType, normalizedId));
    if (!entry) return false;
    if (version > entry.version) entry.version = version;
    return true;
  }

  function deferEvents({ resourceType, resourceId } = {}) {
    const normalizedId = String(resourceId || '');
    if (!validSubscription(resourceType, normalizedId)) return () => {};
    const key = subscriptionKey(resourceType, normalizedId);
    const entry = subscriptions.get(key);
    if (!entry) return () => {};
    entry.deferredEventCount += 1;
    let resumed = false;
    return () => {
      if (resumed) return;
      resumed = true;
      if (subscriptions.get(key) !== entry) return;
      entry.deferredEventCount = Math.max(0, entry.deferredEventCount - 1);
      if (entry.deferredEventCount > 0 || !entry.deferredEvent) return;
      const deferredEvent = entry.deferredEvent;
      entry.deferredEvent = null;
      handleResourceEvent(deferredEvent);
    };
  }

  function disconnect() {
    subscriptions.clear();
    connectionToken = '';
    if (socket) socket.disconnect();
    socket = null;
  }

  return { connect, subscribe, acknowledge, deferEvents, disconnect };
})();
