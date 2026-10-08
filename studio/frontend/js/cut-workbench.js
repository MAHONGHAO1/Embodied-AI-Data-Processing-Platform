/** EGO continuous timeline cut workbench pure function state machine module, responsible for segment management, boundary operations, and admission validation. */
const QuicDataCutWorkbench = (() => {
  const MAX_SEGMENTS = 500;
  const MAX_INCLUDED_NS = 300000000000n;
  const ELIGIBILITY = new Set(['included', 'excluded']);
  const EXCLUSION_REASONS = new Set(['off_task', 'idle_or_setup', 'privacy_sensitive', 'other']);

  function nanoseconds(value) {
    try {
      const normalized = String(value ?? '').trim();
      return /^\d+$/.test(normalized) ? BigInt(normalized) : null;
    } catch {
      return null;
    }
  }

  function boundsOf(bounds) {
    const start = nanoseconds(bounds?.start_ns);
    const end = nanoseconds(bounds?.end_ns);
    return start !== null && end !== null && start < end ? { start, end } : null;
  }

  function cloneBoundary(boundary) {
    if (!boundary || boundary.origin === 'human') return boundary ? { origin: 'human' } : null;
    return {
      origin: 'qr_event',
      suggested_timestamp_ns: String(boundary.suggested_timestamp_ns),
      adjusted: boundary.adjusted === true,
      segment_id_hint: String(boundary.segment_id_hint),
      segment_index: Number(boundary.segment_index),
      protocol_version: Number(boundary.protocol_version),
    };
  }

  function cloneSegment(segment) {
    const result = {
      id: String(segment.id),
      start_ns: String(segment.start_ns),
      end_ns: String(segment.end_ns),
      eligibility: segment.eligibility === 'excluded' ? 'excluded' : 'included',
    };
    if (typeof segment.description === 'string' && segment.description) result.description = segment.description;
    if (result.eligibility === 'excluded' && EXCLUSION_REASONS.has(segment.exclusion_reason)) {
      result.exclusion_reason = segment.exclusion_reason;
    }
    const boundary = cloneBoundary(segment.boundary_after);
    if (boundary) result.boundary_after = boundary;
    return result;
  }

  function normalizedSegments(segments) {
    if (!Array.isArray(segments) || !segments.length || segments.length > MAX_SEGMENTS) return null;
    const result = [];
    let priorEnd = null;
    for (const raw of segments) {
      if (!raw || typeof raw !== 'object' || !ELIGIBILITY.has(raw.eligibility || 'included')) return null;
      const start = nanoseconds(raw.start_ns);
      const end = nanoseconds(raw.end_ns);
      if (start === null || end === null || start >= end || (priorEnd !== null && start !== priorEnd)) return null;
      const segment = cloneSegment(raw);
      result.push({ segment, start, end });
      priorEnd = end;
    }
    for (let index = 0; index < result.length; index += 1) {
      if (index === result.length - 1 && result[index].segment.boundary_after) return null;
      if (index < result.length - 1 && !result[index].segment.boundary_after) {
        result[index].segment.boundary_after = { origin: 'human' };
      }
    }
    return result;
  }

  function nextId(segments) {
    const used = new Set(segments.map((segment) => String(segment.id)));
    for (let index = 1; index <= MAX_SEGMENTS + 1; index += 1) {
      const candidate = `cut-${index}`;
      if (!used.has(candidate)) return candidate;
    }
    return `cut-${Date.now()}`;
  }

  function validSuggestion(raw, bounds) {
    const timestamp = nanoseconds(raw?.timestamp_ns);
    const segmentId = typeof raw?.segment_id_hint === 'string' ? raw.segment_id_hint.trim() : '';
    const segmentIndex = raw?.segment_index;
    const protocolVersion = raw?.protocol_version;
    if (
      timestamp === null || timestamp <= bounds.start || timestamp >= bounds.end
      || !segmentId || segmentId.length > 128
      || !Number.isInteger(segmentIndex) || segmentIndex < 0 || segmentIndex > 1000000
      || !Number.isInteger(protocolVersion) || protocolVersion < 1 || protocolVersion > 1000000
    ) return null;
    return {
      timestamp,
      boundary: {
        origin: 'qr_event',
        suggested_timestamp_ns: timestamp.toString(),
        adjusted: false,
        segment_id_hint: segmentId,
        segment_index: segmentIndex,
        protocol_version: protocolVersion,
      },
    };
  }

  function initialize(bounds, suggestions) {
    const normalizedBounds = boundsOf(bounds);
    if (!normalizedBounds) return [];
    const unique = new Map();
    if (Array.isArray(suggestions)) {
      for (const raw of suggestions.slice(0, MAX_SEGMENTS - 1)) {
        const suggestion = validSuggestion(raw, normalizedBounds);
        if (suggestion && !unique.has(suggestion.timestamp.toString())) {
          unique.set(suggestion.timestamp.toString(), suggestion);
        }
      }
    }
    const ordered = [...unique.values()].sort((left, right) => (
      left.timestamp < right.timestamp ? -1 : left.timestamp > right.timestamp ? 1 : 0
    ));
    const segments = [];
    let cursor = normalizedBounds.start;
    for (const suggestion of ordered) {
      segments.push({
        id: `cut-${segments.length + 1}`,
        start_ns: cursor.toString(),
        end_ns: suggestion.timestamp.toString(),
        eligibility: 'included',
        boundary_after: suggestion.boundary,
      });
      cursor = suggestion.timestamp;
    }
    segments.push({
      id: `cut-${segments.length + 1}`,
      start_ns: cursor.toString(),
      end_ns: normalizedBounds.end.toString(),
      eligibility: 'included',
    });
    return segments;
  }

  function moveBoundary(segments, boundaryIndex, targetNs) {
    const normalized = normalizedSegments(segments);
    const target = nanoseconds(targetNs);
    const index = Number(boundaryIndex);
    if (!normalized || target === null || !Number.isInteger(index) || index < 0 || index >= normalized.length - 1) return null;
    if (target <= normalized[index].start || target >= normalized[index + 1].end) return null;
    const result = normalized.map((item) => cloneSegment(item.segment));
    result[index].end_ns = target.toString();
    result[index + 1].start_ns = target.toString();
    const boundary = result[index].boundary_after;
    if (boundary?.origin === 'qr_event') {
      boundary.adjusted = boundary.suggested_timestamp_ns !== target.toString();
    }
    return result;
  }

  function insertBoundary(segments, targetNs) {
    const normalized = normalizedSegments(segments);
    const target = nanoseconds(targetNs);
    if (!normalized || target === null || normalized.length >= MAX_SEGMENTS) return null;
    const result = [];
    let inserted = false;
    const id = nextId(normalized.map((item) => item.segment));
    for (const item of normalized) {
      if (!inserted && target > item.start && target < item.end) {
        const left = cloneSegment(item.segment);
        const right = cloneSegment(item.segment);
        const oldBoundary = right.boundary_after;
        left.end_ns = target.toString();
        left.boundary_after = { origin: 'human' };
        right.id = id;
        right.start_ns = target.toString();
        if (oldBoundary) right.boundary_after = oldBoundary;
        else delete right.boundary_after;
        result.push(left, right);
        inserted = true;
      } else {
        result.push(cloneSegment(item.segment));
      }
    }
    return inserted ? result : null;
  }

  function removeBoundary(segments, boundaryIndex) {
    const normalized = normalizedSegments(segments);
    const index = Number(boundaryIndex);
    if (!normalized || !Number.isInteger(index) || index < 0 || index >= normalized.length - 1) return null;
    const result = normalized.map((item) => cloneSegment(item.segment));
    const merged = cloneSegment(result[index]);
    merged.end_ns = result[index + 1].end_ns;
    if (result[index + 1].boundary_after) merged.boundary_after = cloneBoundary(result[index + 1].boundary_after);
    else delete merged.boundary_after;
    result.splice(index, 2, merged);
    return result;
  }

  function restoreQrBoundary(segments, boundaryIndex) {
    const normalized = normalizedSegments(segments);
    const index = Number(boundaryIndex);
    if (!normalized || !Number.isInteger(index) || index < 0 || index >= normalized.length - 1) return null;
    const boundary = normalized[index].segment.boundary_after;
    if (boundary?.origin !== 'qr_event') return null;
    return moveBoundary(segments, index, boundary.suggested_timestamp_ns);
  }

  function setEligibility(segments, segmentIndex, eligibility) {
    const normalized = normalizedSegments(segments);
    const index = Number(segmentIndex);
    if (!normalized || !ELIGIBILITY.has(eligibility) || !Number.isInteger(index) || index < 0 || index >= normalized.length) return null;
    const result = normalized.map((item) => cloneSegment(item.segment));
    result[index].eligibility = eligibility;
    if (eligibility === 'included') delete result[index].exclusion_reason;
    return result;
  }

  function setExclusionReason(segments, segmentIndex, reason) {
    const normalized = normalizedSegments(segments);
    const index = Number(segmentIndex);
    if (!normalized || !Number.isInteger(index) || index < 0 || index >= normalized.length) return null;
    if (reason !== '' && reason !== null && !EXCLUSION_REASONS.has(reason)) return null;
    const result = normalized.map((item) => cloneSegment(item.segment));
    if (result[index].eligibility !== 'excluded') return null;
    if (EXCLUSION_REASONS.has(reason)) result[index].exclusion_reason = reason;
    else delete result[index].exclusion_reason;
    return result;
  }

  function localRange(segments, boundaryIndex, bounds, radiusSeconds = 15) {
    const normalized = normalizedSegments(segments);
    const normalizedBounds = boundsOf(bounds);
    const index = Number(boundaryIndex);
    const seconds = Number(radiusSeconds);
    if (!normalized || !normalizedBounds || !Number.isInteger(index) || index < 0 || index >= normalized.length - 1) return null;
    if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 3600) return null;
    const radius = BigInt(Math.round(seconds * 1000000000));
    const boundary = normalized[index].end;
    const start = boundary - radius > normalizedBounds.start ? boundary - radius : normalizedBounds.start;
    const end = boundary + radius < normalizedBounds.end ? boundary + radius : normalizedBounds.end;
    return { start_ns: start.toString(), end_ns: end.toString(), boundary_ns: boundary.toString() };
  }

  function localBoundaries(segments, range, selectedBoundaryIndex) {
    const normalized = normalizedSegments(segments);
    const normalizedRange = boundsOf(range);
    const selected = selectedBoundaryIndex === null || selectedBoundaryIndex === undefined
      ? null
      : Number(selectedBoundaryIndex);
    if (!normalized || !normalizedRange) return [];
    return normalized.slice(0, -1).flatMap((item, index) => {
      if (item.end < normalizedRange.start || item.end > normalizedRange.end) return [];
      const boundary = item.segment.boundary_after;
      return [{
        index,
        timestamp_ns: item.end.toString(),
        selected: selected !== null && Number.isInteger(selected) && index === selected,
        origin: boundary?.origin === 'qr_event' ? 'qr_event' : 'human',
        adjusted: boundary?.adjusted === true,
      }];
    });
  }

  function overlappingRange(segments, startNs, endNs, pad = 2) {
    if (!Array.isArray(segments) || !segments.length) return { startIndex: 0, endIndex: 0 };
    const start = nanoseconds(startNs);
    const end = nanoseconds(endNs);
    const padding = Number.isFinite(Number(pad)) ? Math.max(0, Math.min(8, Math.floor(Number(pad)))) : 2;
    if (start === null || end === null || start >= end) {
      return { startIndex: 0, endIndex: segments.length };
    }
    let low = 0;
    let high = segments.length;
    while (low < high) {
      const mid = (low + high) >> 1;
      const segmentEnd = nanoseconds(segments[mid]?.end_ns);
      if (segmentEnd !== null && segmentEnd <= start) low = mid + 1;
      else high = mid;
    }
    const startIndex = Math.max(0, low - padding);
    let endIndex = low;
    while (endIndex < segments.length) {
      const segmentStart = nanoseconds(segments[endIndex]?.start_ns);
      if (segmentStart !== null && segmentStart >= end) break;
      endIndex += 1;
    }
    return { startIndex, endIndex: Math.min(segments.length, endIndex + padding) };
  }

  function indexContaining(segments, timestampNs) {
    if (!Array.isArray(segments) || !segments.length) return -1;
    const target = nanoseconds(timestampNs);
    if (target === null) return -1;
    let low = 0;
    let high = segments.length - 1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      const start = nanoseconds(segments[mid]?.start_ns);
      const end = nanoseconds(segments[mid]?.end_ns);
      if (start === null || end === null) return -1;
      if (target < start) high = mid - 1;
      else if (target > end || (target === end && mid < segments.length - 1)) low = mid + 1;
      else return mid;
    }
    return -1;
  }

  function nearestBoundaryIndex(segments, timestampNs) {
    if (!Array.isArray(segments) || segments.length < 2) return -1;
    const target = nanoseconds(timestampNs);
    if (target === null) return -1;
    let closestIndex = 0;
    let closestDistance = null;
    for (let index = 0; index < segments.length - 1; index += 1) {
      const boundary = nanoseconds(segments[index]?.end_ns);
      if (boundary === null) continue;
      const distance = boundary > target ? boundary - target : target - boundary;
      if (closestDistance === null || distance < closestDistance) {
        closestDistance = distance;
        closestIndex = index;
      }
    }
    return closestIndex;
  }

  function validateForSave(segments, bounds) {
    const normalized = normalizedSegments(segments);
    const normalizedBounds = boundsOf(bounds);
    return Boolean(
      normalized && normalizedBounds
      && normalized[0].start === normalizedBounds.start
      && normalized[normalized.length - 1].end === normalizedBounds.end
    );
  }

  function validateForSubmit(segments, bounds) {
    const normalized = normalizedSegments(segments);
    if (!validateForSave(segments, bounds) || !normalized) return false;
    return normalized.every((item) => (
      item.segment.eligibility === 'excluded' || item.end - item.start <= MAX_INCLUDED_NS
    ));
  }

  return {
    initialize,
    moveBoundary,
    insertBoundary,
    removeBoundary,
    restoreQrBoundary,
    setEligibility,
    setExclusionReason,
    localRange,
    localBoundaries,
    overlappingRange,
    indexContaining,
    nearestBoundaryIndex,
    validateForSave,
    validateForSubmit,
  };
})();
