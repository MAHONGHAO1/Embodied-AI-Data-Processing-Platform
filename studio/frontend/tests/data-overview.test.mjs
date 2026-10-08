import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const dataOverviewSource = readFileSync(new URL('../js/data-overview.js', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../js/app.js', import.meta.url), 'utf8');
const cssSource = readFileSync(new URL('../css/app.css', import.meta.url), 'utf8');
const context = vm.createContext({});
vm.runInContext(`${dataOverviewSource}\nglobalThis.overview = QuicStudioDataOverview;`, context);
const { createPager, stageIssues } = context.overview;

test('page changes are bounded, deduplicated, and late scope responses are discarded', async () => {
  const requests = [], state = {};
  const pager = createPager({ listDataPackages: params => new Promise(resolve => requests.push({ params, resolve })) }, state);
  const query = { workspace_id: 1, tab: 'packages', page: 1, size: 20 };
  const first = pager.load(query);
  assert.equal(pager.load(query), first);
  const second = pager.load({ ...query, workspace_id: 2, page: 3 });
  assert.equal(requests[1].params.page, 3);
  assert.equal(requests[1].params.size, 20);
  assert.equal(requests[1].params.tab, undefined);
  requests[1].resolve({ items: [{ id: 20 }], total: 62 });
  await second;
  requests[0].resolve({ items: [{ id: 1 }], total: 1 });
  await first;
  assert.equal(state.items[0].id, 20);
  assert.equal(state.total, 62);
  assert.equal(state.loading, false);
});

test('batch list does not fetch row details or per-row governance reports', async () => {
  let listCalls = 0;
  const state = {};
  const pager = createPager({ listDataBatches: async () => { listCalls++; return { items: [{ id: 1, stages: [] }], total: 1 }; } }, state);
  await pager.load({ workspace_id: 1, tab: 'batches', page: 1, size: 50 });
  assert.equal(listCalls, 1);
  assert.equal(state.error, '');
  assert.equal(state.items.length, 1);
});

test('failed or malformed responses clear old rows and disposal ignores pending requests', async () => {
  const state = { items: [{ id: 1 }] };
  const pager = createPager({ listDataPackages: async () => ({ items: [], total: 'unknown' }) }, state);
  await pager.load({ workspace_id: 1, tab: 'packages' });
  assert.equal(state.items.length, 0);
  assert.equal(state.total, 0);
  assert.match(state.error, /Invalid list/);
  let resolve;
  const pending = createPager({ listDataPackages: () => new Promise(r => { resolve = r; }) }, state);
  const read = pending.load({ workspace_id: 2, tab: 'packages' });
  pending.dispose();
  resolve({ items: [{ id: 2 }], total: 1 });
  await read;
  assert.equal(state.items.length, 0);
});

test('reports show persisted issues and QC removals without inventing pass counts', () => {
  const result = stageIssues({ result_json: { issues: [{ episode_id: 2, reason: 'missing artifacts' }], dropped: [{ episode_id: 3, reason: 'invalid capture' }] } });
  assert.equal(result.length, 2);
  assert.equal(result[0].episode, 2);
  assert.equal(result[1].reason, 'invalid capture');
  assert.equal(stageIssues({ result_json: {} }).length, 0);
});

test('collected data shows collection time immediately after upload completion', () => {
  assert.match(
    dataOverviewSource,
    /text\('上传完成时间','Upload completed'\)[\s\S]*?text\('采集时间','Collection time'\)[^>]*>\s*<template #default="scope">\{\{ collectionTime\(scope\.row\) \}\}/,
  );
  // Collection time is the package capture start, never the assignment time.
  assert.match(dataOverviewSource, /const collectionTime = row => date\(row\?\.captured_started_at\);/);
  assert.doesNotMatch(dataOverviewSource, /date\(row\?\.assigned_at\)/);
});

test('data overview follows the redesigned reference information hierarchy', () => {
  assert.match(dataOverviewSource, /class="data-overview-tabs-bar intake-tabs-bar"/);
  assert.match(dataOverviewSource, /class="data-overview-filter-bar list-filter-bar"/);
  assert.match(dataOverviewSource, /class="data-overview-primary-cell"/);
  assert.match(dataOverviewSource, /class="data-overview-status-stack"/);
  assert.match(dataOverviewSource, /class="data-overview-stage-list"/);
});

test('data overview merges workspace and project scope into its operational filter bar', () => {
  assert.match(dataOverviewSource, /projectId: \[String, Number\]/);
  assert.match(dataOverviewSource, /collection_project_id:props\.projectId \|\| undefined/);
  assert.match(dataOverviewSource, /<slot name="scope-filters"><\/slot>/);
  assert.match(appSource, /<data-overview[\s\S]*?#scope-filters[\s\S]*?v-model="selectedWorkspaceId"[\s\S]*?v-model="selectedCollectionProjectId"/);
  const header = appSource.match(/<header class="console-header">([\s\S]*?)<\/header>/)?.[1] || '';
  assert.doesNotMatch(header, /\['intake', 'work-queue', 'settings'\]/);
  assert.match(cssSource, /\.data-overview-filter-bar \{[^}]*display:\s*flex;[^}]*flex-wrap:\s*wrap/);
  assert.match(cssSource, /\.data-overview-filters, \.data-overview-filter-summary \{\s*display:\s*contents;/);
  assert.match(cssSource, /\.data-overview-filter-summary \.el-button \{[^}]*margin-left:\s*0/);
});

test('package names get enough space and remain fully readable', () => {
  assert.match(dataOverviewSource, /text\('数据包名称','Package'\)" min-width="320"/);
  assert.match(dataOverviewSource, /class="data-overview-package-name"/);
});

test('collected data exposes operational filters and columns', () => {
  for (const field of ['collectorId', 'deviceId', 'purposeLabelId', 'sceneLabelId', 'modalityLabelId', 'trainingLabelId']) {
    assert.match(dataOverviewSource, new RegExp(`filters\\.${field}`));
  }
  for (const label of ['数采员', '主采集设备', '任务用途', '场景标签', '数据模态', '训练用途']) {
    assert.match(dataOverviewSource, new RegExp(label));
  }
});

test('data batch filters are rendered and forwarded to the paginated API', () => {
  for (const field of [
    'sceneLabelId', 'purposeLabelId', 'trainingLabelId', 'modalityLabelId',
    'integrityStatus', 'qualityStatus', 'complianceStatus', 'annotationStatus',
  ]) {
    assert.match(dataOverviewSource, new RegExp(`batchFilters\\.${field}`));
  }
  for (const parameter of [
    'scene_label_id', 'purpose_label_id', 'training_label_id', 'modality_label_id',
    'integrity_status', 'quality_status', 'compliance_status', 'annotation_status',
    'upload_completed_from', 'upload_completed_to',
  ]) {
    assert.match(dataOverviewSource, new RegExp(`${parameter}:`));
  }
  for (const label of ['场景标签', '任务用途', '训练标签', '数据模态', '完整性检查', '自动质检', '脱敏', '标注状态', '上传完成时间']) {
    assert.match(dataOverviewSource, new RegExp(label));
  }
});

test('data batch tab honours the page collection project scope', () => {
  const batchParams = dataOverviewSource.match(/\} : \{\n([\s\S]*?)\}\) \}\);/)?.[1] || '';
  assert.match(batchParams, /collection_project_id:props\.projectId \|\| undefined/);
  assert.match(batchParams, /integrity_status:batchFilters\.integrityStatus/);
});
