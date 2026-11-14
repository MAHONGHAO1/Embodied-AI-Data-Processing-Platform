import type { Batch, CameraInspection, EpisodeDetail, RunRecord } from './types';

/** 保留 HTTP 状态码，让调用方能区分「需要二次确认的 409」与真正的失败。 */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export function isConflict(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 409;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(payload.detail || `请求失败（${response.status}）`, response.status);
  return payload as T;
}

export const api = {
  health: () => request<{ status: string; version: string }>('/api/health'),
  batches: () => request<{ batches: Batch[]; errors: Array<{ batch_id: string; message: string }> }>('/api/batches'),
  batch: (id: string) => request<Batch>(`/api/batches/${encodeURIComponent(id)}`),
  episode: (id: string, index: number) => request<EpisodeDetail>(`/api/batches/${encodeURIComponent(id)}/episodes/${index}`),
  camera: (id: string, index: number, name: string) => request<CameraInspection>(`/api/batches/${encodeURIComponent(id)}/episodes/${index}/cameras/${encodeURIComponent(name)}`),
  runs: () => request<{ runs: RunRecord[] }>('/api/runs'),
  run: (id: string) => request<RunRecord>(`/api/runs/${encodeURIComponent(id)}`),
  startRun: (body: Record<string, unknown>) => request<{ run_id: string; status: string }>('/api/runs', { method: 'POST', body: JSON.stringify(body) }),
  updateDisposition: (batch: Batch, episode: number, disposition: string, reason: string, invalidateReviews = false) => request<Batch>(`/api/batches/${batch.batch_id}/episodes/${episode}/disposition`, { method: 'PATCH', body: JSON.stringify({ revision: batch.revision, disposition, reason, ...(invalidateReviews ? { invalidate_reviews: true } : {}) }) }),
  confirmCleaning: (batch: Batch) => request<Batch>(`/api/batches/${batch.batch_id}/cleaning/confirm`, { method: 'POST', body: JSON.stringify({ revision: batch.revision }) }),
  saveAnnotation: (batch: Batch, episode: number, annotation: Record<string, unknown>) => request<Batch>(`/api/batches/${batch.batch_id}/episodes/${episode}/annotation`, { method: 'PATCH', body: JSON.stringify({ ...annotation, revision: batch.revision }) }),
  submitAnnotation: (batch: Batch, episode: number) => request<Batch>(`/api/batches/${batch.batch_id}/episodes/${episode}/annotation/submit`, { method: 'POST', body: JSON.stringify({ revision: batch.revision }) }),
  reviewAnnotation: (batch: Batch, episode: number, approve: boolean, reason: string) => request<Batch>(`/api/batches/${batch.batch_id}/episodes/${episode}/review`, { method: 'POST', body: JSON.stringify({ revision: batch.revision, approve, reason }) }),
  // 显式恢复中断运行的业务登记；读接口不再承担回写副作用。
  recoverRun: (runId: string, invalidateReviews = false) => request<{ status: string; message?: string }>(
    `/api/runs/${encodeURIComponent(runId)}/recover`,
    { method: 'POST', ...(invalidateReviews ? { body: JSON.stringify({ invalidate_reviews: true }) } : {}) }),
};

export function listenRun(runId: string, onSnapshot: (snapshot: { state: RunRecord; events: Array<Record<string, unknown>> }) => void, onError: (message: string) => void) {
  const stream = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  let closed = false;
  const close = () => { if (!closed) { closed = true; stream.close(); } };
  stream.addEventListener('snapshot', (event) => {
    const snapshot = JSON.parse((event as MessageEvent).data);
    onSnapshot(snapshot);
    // 双保险：即使服务端没来得及下发 done，终态快照也立即关闭，杜绝无限自动重连。
    if (snapshot?.state?.status && !['queued', 'running'].includes(snapshot.state.status)) close();
  });
  stream.addEventListener('done', () => close());
  stream.addEventListener('error', (event) => {
    if (closed) return;
    if ('data' in event && typeof (event as MessageEvent).data === 'string') {
      try { onError(JSON.parse((event as MessageEvent).data).error); } catch { onError('运行事件流连接中断'); }
      close();
      return;
    }
    // 网络抖动时 EventSource 会自动重连，不打扰用户；只有连接彻底关闭才收尾。
    if (stream.readyState === EventSource.CLOSED) close();
  });
  return () => close();
}
