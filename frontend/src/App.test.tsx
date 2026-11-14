import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
vi.mock('./Chart', () => ({ default: () => <div data-testid="chart" /> }));
import App from './App';

const batchId = 'batch_' + 'a'.repeat(32);
const episodeDetail = {
  episode_index: 0, row_count: 120, fps: 30, state_names: ['s'], action_names: ['a'],
  cameras: [], samples: [], quality: { status: 'passed', issues: [] }, disposition: 'keep',
  annotation: { task: '拾取', outcome: 'success', tags: [], notes: '' },
  review: { status: 'submitted' },
};

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200,
    headers: { 'Content-Type': 'application/json' } });
}

function makeBatch() {
  return { batch_id: batchId, label: '审计批次', revision: 8, content_version: 4,
    source: { kind: 'so100', input_fingerprint: 'f'.repeat(64) },
    episodes: [{ episode_index: 0, row_count: 120, disposition: 'keep',
      quality: { status: 'passed', issue_count: 0, issues: [] },
      annotation: { task: '拾取', outcome: 'success', tags: [], notes: '' },
      review: { status: 'submitted' } }],
    cleaning_confirmed: true, outputs: [], deliveries: [],
    total_episodes: 1, total_rows: 120, counts: { keep: { episodes: 1, rows: 120 } } };
}

function mockFetch(batches: unknown[] = [makeBatch()]) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/batches')) return json({ batches, errors: [] });
    if (url.endsWith('/api/runs')) return json({ runs: [] });
    if (url.includes('/cameras/')) return json({ name: 'cam', fps: 30, frame_count: 120,
      width: 640, height: 480, codec: 'h264', from_timestamp: 0, to_timestamp: null,
      mapping_claim: '文件级时间映射检查；不代表硬件同步' });
    if (url.includes('/episodes/')) return json(episodeDetail);
    if (url.startsWith('/api/batches/')) return json(batches[0]);
    return json({ status: 'ok', version: '0.8.0' });
  });
}

beforeEach(() => {
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('RoboData workbench shell', () => {
  it('加载中不再误报「还没有登记批次」，空态在数据返回后才出现', async () => {
    vi.stubGlobal('fetch', mockFetch([]));
    render(<App />);
    expect(await screen.findByRole('heading', { name: '机器人数据处理工作台' })).toBeInTheDocument();
    expect(await screen.findByText('还没有登记批次')).toBeInTheDocument();
    expect(screen.getByLabelText('数据来源')).toBeInTheDocument();
  });

  it('阶段进度条按真实审核状态渲染，不再只剩「导入」完成', async () => {
    window.history.replaceState(null, '', `/?view=overview&batch=${batchId}`);
    vi.stubGlobal('fetch', mockFetch());
    render(<App />);
    expect(await screen.findByText('批次处理流程')).toBeInTheDocument();
    expect(await screen.findByText('0/1 已通过 · 1 待审核')).toBeInTheDocument();
    expect(screen.getByText('1/1 个保留任务通过')).toBeInTheDocument();
    expect(screen.queryByText('待处理')).not.toBeInTheDocument();
  });

  it('刷新后能从 URL + 本地会话恢复批次与任务上下文', async () => {
    window.localStorage.setItem('robodata.workbench.session.v1', JSON.stringify({
      version: 1, savedAt: Date.now(), route: 'episodes', batchId,
      episodeIndex: 0, currentFrame: 0, drafts: {},
    }));
    window.history.replaceState(null, '', `/?view=episodes&batch=${batchId}`);
    const fetchMock = mockFetch();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    await waitFor(() => expect(fetchMock.mock.calls.some((call) =>
      String(call[0]).includes(`/api/batches/${batchId}/episodes/0`))).toBe(true));
    expect(screen.getByText('筛选与标注')).toBeInTheDocument();
  });
});
