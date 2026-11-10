import { expect, test } from '@playwright/test';

test('workbench opens with the batch intake flow', async ({ page }) => {
  await page.route('**/api/batches', (route) => route.fulfill({ json: { batches: [], errors: [] } }));
  await page.route('**/api/runs', (route) => route.fulfill({ json: { runs: [] } }));
  let submittedRun: Record<string, unknown> | null = null;
  await page.route('**/api/runs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    submittedRun = route.request().postDataJSON();
    await route.fulfill({ status: 202, json: { run_id: 'demo-run-001', status: 'queued' } });
  });
  await page.route('**/api/runs/demo-run-001', (route) => route.fulfill({ json: { run_id: 'demo-run-001', status: 'queued', message: '等待后台处理', nodes: [] } }));
  await page.route('**/api/runs/demo-run-001/events', (route) => route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '机器人数据处理工作台' })).toBeVisible();
  await expect(page.getByRole('button', { name: '开始导入' })).toBeVisible();
  await page.getByRole('button', { name: '开始导入' }).click();
  await expect(page.getByRole('heading', { name: '后台处理记录' })).toBeVisible();
  await expect(page.getByText('demo-run-001')).toBeVisible();
  await expect.poll(() => submittedRun).toMatchObject({ operation: 'import', source_kind: 'aloha' });
});

test('episode browser shows separate camera checks and avoids a sync claim', async ({ page }) => {
  const cameraNames = ['observation.images.cam_high', 'observation.images.cam_left_wrist'];
  const batch = {
    batch_id: 'batch_demo', label: '公开多相机样例', revision: 1, content_version: 1,
    source: { kind: 'aloha', repo_id: 'lerobot/aloha_static_coffee', fps: 50, cameras: cameraNames, input_fingerprint: 'demo-fingerprint' },
    episodes: [{ episode_index: 0, row_count: 2, disposition: 'keep', quality: { status: 'passed', issue_count: 0, issues: [] }, annotation: { task: '公开样例任务', outcome: 'uncertain', tags: [], notes: '' }, review: { status: 'approved', reason: '' } }],
    total_episodes: 1, total_rows: 2, cleaning_confirmed: true, counts: { keep: { episodes: 1, rows: 2 } }, outputs: [], deliveries: [],
  };
  const episode = {
    episode_index: 0, row_count: 2, fps: 50, state_names: [], action_names: [], quality: { status: 'passed', issues: [], coverage: [] },
    disposition: 'keep', annotation: { task: '公开样例任务', outcome: 'uncertain', tags: [], notes: '' }, review: { status: 'approved', reason: '' }, samples: [],
    cameras: cameraNames.map((name) => ({ name, video_url: `/api/batches/batch_demo/episodes/0/video/${name}`, from_timestamp: 0, to_timestamp: 0.04, available: true })),
  };
  await page.route('**/api/batches', (route) => route.fulfill({ json: { batches: [batch], errors: [] } }));
  await page.route('**/api/batches/batch_demo', (route) => route.fulfill({ json: batch }));
  await page.route('**/api/batches/batch_demo/episodes/0', (route) => route.fulfill({ json: episode }));
  await page.route('**/api/batches/batch_demo/episodes/0/cameras/**', async (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split('/').at(-1) || '');
    await route.fulfill({ json: { name, fps: 50, frame_count: 2, width: 640, height: 480, codec: 'av1', from_timestamp: 0, to_timestamp: 0.04, mapping_claim: '时间映射检查通过' } });
  });
  await page.route('**/api/batches/batch_demo/episodes/0/video/**', (route) => route.fulfill({ status: 200, contentType: 'video/mp4', body: '' }));
  await page.route('**/api/runs', (route) => route.fulfill({ json: { runs: [] } }));

  await page.goto('/');
  await page.getByRole('button', { name: '浏览 公开多相机样例' }).click();
  await expect(page.getByRole('heading', { name: '查看原始任务与数据证据' })).toBeVisible();
  await expect(page.getByText('文件时间映射检查不等于硬件同步')).toBeVisible();
  await expect(page.getByText('640 × 480 · 50.0 FPS').first()).toBeVisible();
  await expect(page.getByText('时间映射检查通过').first()).toBeVisible();
});
