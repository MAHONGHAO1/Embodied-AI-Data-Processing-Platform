/** Workbench draft serialization and auto-save orchestrator, supporting debounced scheduling, max wait duration, and concurrency guard. */
const QuicDataWorkbenchSave = (() => {
  function create({
    capture,
    save,
    onSaved = () => {},
    onFailed = () => {},
    onState = () => {},
    setTimer = (...args) => globalThis.setTimeout(...args),
    clearTimer = (...args) => globalThis.clearTimeout(...args),
    now = () => Date.now(),
    debounceMs = 5000,
    maxWaitMs = 30000,
  } = {}) {
    if (typeof capture !== 'function' || typeof save !== 'function') {
      throw new TypeError('capture and save are required');
    }

    let localRevision = 0;
    let savedRevision = 0;
    let inFlight = null;
    let dirtySince = null;
    let debounceTimer = null;
    let maxWaitTimer = null;
    let paused = false;
    let disposed = false;
    let failed = false;

    function state() {
      return {
        dirty: localRevision > savedRevision,
        saving: Boolean(inFlight),
        failed,
        localRevision,
        savedRevision,
      };
    }

    function publish() {
      onState(state());
    }

    function clearTimers() {
      if (debounceTimer !== null) clearTimer(debounceTimer);
      if (maxWaitTimer !== null) clearTimer(maxWaitTimer);
      debounceTimer = null;
      maxWaitTimer = null;
    }

    function schedule({ resetDebounce = false } = {}) {
      if (disposed || paused || inFlight || localRevision <= savedRevision) return;
      if (resetDebounce && debounceTimer !== null) {
        clearTimer(debounceTimer);
        debounceTimer = null;
      }
      if (debounceTimer === null) {
        debounceTimer = setTimer(() => {
          debounceTimer = null;
          void flush({ reason: 'auto' });
        }, debounceMs);
      }
      if (maxWaitTimer === null) {
        const delay = Math.max(0, maxWaitMs - (now() - (dirtySince ?? now())));
        maxWaitTimer = setTimer(() => {
          maxWaitTimer = null;
          void flush({ reason: 'auto' });
        }, delay);
      }
    }

    function markDirty() {
      if (disposed) return localRevision;
      localRevision += 1;
      if (dirtySince === null) dirtySince = now();
      failed = false;
      schedule({ resetDebounce: true });
      publish();
      return localRevision;
    }

    async function saveOnce(reason) {
      clearTimers();
      const revision = localRevision;
      let snapshot;
      try {
        snapshot = capture(revision);
      } catch (error) {
        failed = true;
        onFailed(error, { revision, reason });
        publish();
        return false;
      }
      if (!snapshot) return false;

      const request = Promise.resolve().then(() => save(snapshot));
      inFlight = request;
      publish();
      try {
        const result = await request;
        savedRevision = Math.max(savedRevision, revision);
        if (savedRevision >= localRevision) dirtySince = null;
        failed = false;
        onSaved(result, { revision, reason });
        return true;
      } catch (error) {
        failed = true;
        onFailed(error, { revision, reason });
        return false;
      } finally {
        if (inFlight === request) inFlight = null;
        publish();
      }
    }

    async function flush({ drain = false, reason = 'manual' } = {}) {
      if (disposed || (paused && reason === 'auto')) return false;
      if (inFlight) {
        let previousSucceeded = true;
        try {
          await inFlight;
        } catch {
          previousSucceeded = false;
        }
        if (!previousSucceeded) return false;
        return drain && localRevision > savedRevision
          ? flush({ drain, reason })
          : true;
      }
      if (localRevision <= savedRevision) return true;

      const saved = await saveOnce(reason);
      if (!saved) return false;
      if (drain && localRevision > savedRevision) return flush({ drain, reason });
      schedule();
      return true;
    }

    function pause() {
      clearTimers();
      paused = true;
      publish();
    }

    function resume() {
      if (disposed) return;
      paused = false;
      schedule();
      publish();
    }

    function reset() {
      clearTimers();
      localRevision = 0;
      savedRevision = 0;
      dirtySince = null;
      paused = false;
      failed = false;
      publish();
    }

    function dispose() {
      clearTimers();
      disposed = true;
      paused = true;
      publish();
    }

    return { markDirty, flush, pause, resume, reset, dispose, state };
  }

  return { create };
})();
