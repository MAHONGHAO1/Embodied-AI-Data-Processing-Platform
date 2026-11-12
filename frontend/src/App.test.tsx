import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
vi.mock('./Chart', () => ({ default: () => <div data-testid="chart" /> }));
import App from './App';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('RoboData workbench shell', () => {
  it('shows the local data workflow and loads an empty state without a backend batch', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.endsWith('/api/batches') ? { batches: [], errors: [] }
        : url.endsWith('/api/runs') ? { runs: [] }
          : { status: 'ok', version: '0.8.0' };
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));
    render(<App />);
    expect(await screen.findByRole('heading', { name: '机器人数据处理工作台' })).toBeInTheDocument();
    expect(screen.getByText('还没有登记批次')).toBeInTheDocument();
    expect(screen.getByLabelText('数据来源')).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
