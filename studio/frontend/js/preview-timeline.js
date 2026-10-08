/** Pure function module for accurate mapping between compressed preview video and original Episode nanosecond timestamps. */
const QuicDataPreviewTimeline = (() => {
  const mappingCache = new WeakMap();

  function nanoseconds(value) {
    try {
      const normalized = String(value ?? '').trim();
      return /^\d+$/.test(normalized) ? BigInt(normalized) : null;
    } catch {
      return null;
    }
  }

  function mapping(preview) {
    if (!preview || typeof preview !== 'object') return null;
    if (mappingCache.has(preview)) return mappingCache.get(preview);
    const fps = Number(preview?.encoded_fps);
    const raw = preview?.frame_timestamps_ns;
    if (!Number.isFinite(fps) || fps <= 0 || fps > 240 || !Array.isArray(raw) || !raw.length || raw.length > 100000) {
      mappingCache.set(preview, null);
      return null;
    }
    const timestamps = [];
    let prior = null;
    for (const item of raw) {
      const value = nanoseconds(item);
      if (value === null || (prior !== null && value <= prior)) {
        mappingCache.set(preview, null);
        return null;
      }
      timestamps.push(value);
      prior = value;
    }
    const encodedDuration = Number(preview?.encoded_duration_s);
    const result = {
      fps,
      timestamps,
      encodedDuration: Number.isFinite(encodedDuration) && encodedDuration > 0 ? encodedDuration : null,
    };
    mappingCache.set(preview, result);
    return result;
  }

  function sourceTimestampAt(playbackSeconds, preview) {
    const normalized = mapping(preview);
    const seconds = Number(playbackSeconds);
    if (!normalized || !Number.isFinite(seconds) || seconds < 0) return null;
    const index = normalized.encodedDuration !== null && normalized.timestamps.length > 1
      ? Math.min(
        normalized.timestamps.length - 1,
        Math.max(0, Math.round(seconds / normalized.encodedDuration * (normalized.timestamps.length - 1))),
      )
      : Math.min(
        normalized.timestamps.length - 1,
        Math.max(0, Math.floor(seconds * normalized.fps + Number.EPSILON)),
      );
    return normalized.timestamps[index].toString();
  }

  function lowerBound(values, target, start = 0, end = values.length) {
    let low = start;
    let high = end;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (values[middle] < target) low = middle + 1;
      else high = middle;
    }
    return low;
  }

  function nearestIndex(values, target, start = 0, end = values.length) {
    if (end <= start) return start;
    const upper = lowerBound(values, target, start, end);
    if (upper <= start) return start;
    if (upper >= end) return end - 1;
    return target - values[upper - 1] <= values[upper] - target ? upper - 1 : upper;
  }

  function playbackSecondsAt(sourceTimestampNs, preview) {
    const normalized = mapping(preview);
    const target = nanoseconds(sourceTimestampNs);
    if (!normalized || target === null) return null;
    const index = nearestIndex(normalized.timestamps, target);
    if (normalized.encodedDuration !== null && normalized.timestamps.length > 1) {
      return index / (normalized.timestamps.length - 1) * normalized.encodedDuration;
    }
    return index / normalized.fps;
  }

  function nearestSourceTimestamp(sourceTimestampNs, preview, minimumNs, maximumNs) {
    const normalized = mapping(preview);
    const target = nanoseconds(sourceTimestampNs);
    const minimum = nanoseconds(minimumNs);
    const maximum = nanoseconds(maximumNs);
    if (!normalized || target === null || minimum === null || maximum === null || minimum > maximum) return null;
    const first = lowerBound(normalized.timestamps, minimum);
    const afterLast = lowerBound(normalized.timestamps, maximum + 1n);
    if (first >= afterLast) return null;
    return normalized.timestamps[nearestIndex(normalized.timestamps, target, first, afterLast)].toString();
  }

  return {
    sourceTimestampAt,
    playbackSecondsAt,
    nearestSourceTimestamp,
  };
})();
