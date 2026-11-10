import type { Batch, CameraInspection, EpisodeDetail, RunRecord } from './types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `请求失败（${response.status}）`);
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
  updateDisposition: (batch: Batch, episode: number, disposition: string, reason: string) => request<Batch>(`/api/batches/${batch.batch_id}/episodes/${episode}/disposition`, { method: 'PATCH', body: JSON.stringify({ revision: batch.revision, disposition, reason }) }),
  confirmCleaning: (batch: Batch) => request<Batch>(`/api/batches/${batch.batch_id}/cleaning/confirm`, { method: 'POST', body: JSON.stringify({ revision: batch.revision }) }),
  saveAnnotation: (batch: Batch, episode: number, annotation: Record<string, unknown>) => request<Batch>(`/api/batches/${batch.batch_id}/episodes/${episode}/annotation`, { method: 'PATCH', body: JSON.stringify({ ...annotation, revision: batch.revision }) }),
  submitAnnotation: (batch: Batch, episode: number) => request<Batch>(`/api/batches/${batch.batch_id}/episodes/${episode}/annotation/submit`, { method: 'POST', body: JSON.stringify({ revision: batch.revision }) }),
  reviewAnnotation: (batch: Batch, episode: number, approve: boolean, reason: string) => request<Batch>(`/api/batches/${batch.batch_id}/episodes/${episode}/review`, { method: 'POST', body: JSON.stringify({ revision: batch.revision, approve, reason }) }),
};

export function listenRun(runId: string, onSnapshot: (snapshot: { state: RunRecord; events: Array<Record<string, unknown>> }) => void, onError: (message: string) => void) {
  const stream = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  stream.addEventListener('snapshot', (event) => onSnapshot(JSON.parse((event as MessageEvent).data)));
  stream.addEventListener('error', (event) => {
    if ('data' in event && typeof (event as MessageEvent).data === 'string') {
      try { onError(JSON.parse((event as MessageEvent).data).error); } catch { onError('运行事件流连接中断'); }
    }
  });
  return () => stream.close();
}
