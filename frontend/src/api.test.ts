import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api, isConflict, listenRun } from './api';

type Handler = (event: { data?: string }) => void;

class FakeEventSource {
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];
  listeners: Record<string, Handler[]> = {};
  closed = false;
  readyState = 0;
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, handler: Handler) {
    (this.listeners[type] ||= []).push(handler);
  }
  close() { this.closed = true; this.readyState = FakeEventSource.CLOSED; }
  emit(type: string, data?: string) {
    for (const handler of this.listeners[type] || []) handler(data === undefined ? {} : { data });
  }
}

vi.stubGlobal('EventSource', FakeEventSource);

beforeEach(() => { FakeEventSource.instances = []; });

const snapshot = (status: string) => JSON.stringify({ state: { run_id: 'r', status }, events: [] });

describe('运行事件流必须能自行终止', () => {
  it('终态快照到达后立即关闭，杜绝浏览器无限自动重连', () => {
    let received = '';
    listenRun('run_1', ({ state }) => { received = state.status; }, () => {});
    const stream = FakeEventSource.instances[0];
    stream.emit('snapshot', snapshot('completed'));
    expect(received).toBe('completed');
    expect(stream.closed).toBe(true);
  });

  it('运行中的快照保持连接', () => {
    listenRun('run_2', () => {}, () => {});
    const stream = FakeEventSource.instances[0];
    stream.emit('snapshot', snapshot('running'));
    expect(stream.closed).toBe(false);
  });

  it('服务端下发的 done 事件会关闭连接', () => {
    listenRun('run_3', () => {}, () => {});
    const stream = FakeEventSource.instances[0];
    stream.emit('done', '{}');
    expect(stream.closed).toBe(true);
  });

  it('服务端错误事件会上报并关闭，不会静默重连', () => {
    const errors: string[] = [];
    listenRun('run_4', () => {}, (message) => errors.push(message));
    const stream = FakeEventSource.instances[0];
    stream.emit('error', JSON.stringify({ error: '运行记录不存在' }));
    expect(errors).toEqual(['运行记录不存在']);
    expect(stream.closed).toBe(true);
  });

  it('原生网络抖动（无 data）不关闭，交给 EventSource 自动重连', () => {
    const errors: string[] = [];
    listenRun('run_5', () => {}, (message) => errors.push(message));
    const stream = FakeEventSource.instances[0];
    stream.emit('error');
    expect(stream.closed).toBe(false);
    expect(errors).toEqual([]);
  });

  it('组件卸载时清理函数会关闭连接', () => {
    const dispose = listenRun('run_6', () => {}, () => {});
    const stream = FakeEventSource.instances[0];
    dispose();
    expect(stream.closed).toBe(true);
  });
});

describe('恢复登记仅在用户显式确认后携带破坏性确认标记', () => {
  it('默认不带确认体；确认时才发送 invalidate_reviews: true', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return { ok: true, json: async () => ({ status: 'registered' }) } as unknown as Response;
    }));
    await api.recoverRun('run_q');
    await api.recoverRun('run_q', true);
    expect(calls).toHaveLength(2);
    expect(calls[0].url).toBe('/api/runs/run_q/recover');
    expect(calls[0].init?.body).toBeUndefined();
    expect(JSON.parse(String(calls[1].init?.body))).toEqual({ invalidate_reviews: true });
  });
});

describe('筛选决定仅在显式确认后携带破坏性确认标记', () => {
  it('默认不带确认标记；确认时携带 invalidate_reviews: true', async () => {
    const calls: Array<{ init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      calls.push({ init });
      return { ok: true, json: async () => ({}) } as unknown as Response;
    }));
    const batch = { batch_id: 'b1', revision: 7 } as unknown as Parameters<typeof api.updateDisposition>[0];
    await api.updateDisposition(batch, 0, 'keep', '');
    await api.updateDisposition(batch, 0, 'keep', '', true);
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ revision: 7, disposition: 'keep', reason: '' });
    expect(JSON.parse(String(calls[1].init?.body))).toEqual({
      revision: 7, disposition: 'keep', reason: '', invalidate_reviews: true,
    });
  });
});

describe('失败响应必须保留状态码，供前端 409 兜底', () => {
  it('409 抛出的错误可被 isConflict 识别，且保留后端文案', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 409, json: async () => ({ detail: '需要显式确认：审核将失效' }),
    }) as unknown as Response));
    const error = await api.startRun({ operation: 'quality' }).catch((err: unknown) => err);
    expect(isConflict(error)).toBe(true);
    expect((error as Error).message).toBe('需要显式确认：审核将失效');
  });

  it('非 409 失败不会被当成需要确认的冲突', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 503, json: async () => ({}),
    }) as unknown as Response));
    const error = await api.batches().catch((err: unknown) => err);
    expect(isConflict(error)).toBe(false);
    expect((error as Error).message).toBe('请求失败（503）');
  });
});
