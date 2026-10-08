// The local demo (?demo=1) must project every console flow without a backend.
// These assertions drive the real API client against the demo data layer, with
// `fetch` stubbed to throw so any accidental network call fails loudly.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const apiSource = readFileSync(new URL('../js/api.js', import.meta.url), 'utf8');
const demoSource = readFileSync(new URL('../js/demo-data.js', import.meta.url), 'utf8');

function createApi() {
  const sandbox = {
    window: {
      location: {
        hostname: '127.0.0.1',
        origin: 'http://127.0.0.1:8090',
        search: '?demo=1',
      },
    },
    URL,
    URLSearchParams,
    AbortController,
    console,
    structuredClone,
    setTimeout,
    clearTimeout,
    fetch: () => {
      throw new Error('demo mode must not call the network');
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(`${demoSource}\n;globalThis.QuicDataDemo = QuicDataDemo;`, sandbox, {
    filename: 'demo-data.js',
  });
  vm.runInContext(`${apiSource}\n;globalThis.QuicDataAPI = QuicDataAPI;`, sandbox, {
    filename: 'api.js',
  });
  return sandbox.QuicDataAPI;
}

function itemsOf(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.items)) return payload.items;
  if (Array.isArray(payload?.list)) return payload.list;
  return null;
}

test('demo mode lists are populated for every console view', async () => {
  const api = createApi();
  assert.equal(api.isDemoMode(), true);

  const lists = {
    workspaces: () => api.listWorkspaces(),
    taskSets: () => api.listTaskSets(1),
    users: () => api.listUsers(),
    workspaceMembers: () => api.listWorkspaceMembers(1),
    taskLabels: () => api.listTaskLabels(),
    episodes: () => api.listEpisodes({ workspace_id: 1, task_set_id: 12 }),
    episodeAssets: () => api.listEpisodeAssets({ workspace_id: 1 }),
    workQueue: () => api.listWorkQueue({ stage: 'cut' }),
    collectorProfiles: () => api.listCollectorProfiles(1),
    collectionDevices: () => api.listCollectionDevices(1),
    availableCollectors: () => api.listAvailableCollectorProfiles(1, ''),
    datasets: () => api.listDatasets({ workspace_id: 1, limit: 50, offset: 0 }),
    datasetCatalog: () => api.listDatasetCatalog({ workspace_id: 1 }),
    nativeLerobotDatasets: () => api.listNativeLerobotDatasets({ workspace_id: 1 }),
    dataBatchCandidates: () => api.listDataBatchCandidates({ workspace_id: 1 }),
    dataBatches: () => api.listDataBatches({ workspace_id: 1 }),
    annotationWorkItems: () => api.listAnnotationWorkItems({ workspace_id: 1 }),
    reviewWorkItems: () => api.listReviewWorkItems({ workspace_id: 1 }),
    dataAssets: () => api.listDataAssets({}),
    catalogDatasets: () => api.listCatalogDatasets(),
    collectionProjects: () => api.listCollectionProjects(1),
    collectionTasks: () => api.listCollectionTasks(1),
    collectionLabels: () => api.listCollectionLabels(1),
    dataPackages: () => api.listDataPackages({ workspace_id: 1 }),
    collectionOverview: () => api.getCollectionOverview(1),
    dashboard: () => api.getDashboardOverview({}),
  };

  for (const [name, load] of Object.entries(lists)) {
    const payload = await load();
    const items = itemsOf(payload);
    if (items) {
      assert.ok(items.length > 0, `${name} returned no rows`);
    } else {
      assert.ok(payload && Object.keys(payload).length > 0, `${name} returned an empty payload`);
    }
  }
});

test('demo mode detail endpoints resolve for the ids shown in the lists', async () => {
  const api = createApi();
  const episode = (await api.listEpisodeAssets({ workspace_id: 1 })).items[0];
  const dataset = (await api.listDatasets({ workspace_id: 1 })).items[0];
  const revision = (await api.listDatasetRevisions(dataset.id)).items[0];
  const nativeDataset = (await api.listNativeLerobotDatasets({ workspace_id: 1 })).items[0];
  const dataBatch = (await api.listDataBatches({ workspace_id: 1 })).items[0];
  const dataAsset = (await api.listDataAssets({})).items[0];
  const catalogDataset = (await api.listCatalogDatasets()).items[0];
  const project = (await api.listCollectionProjects(1)).items[0];
  const task = (await api.listCollectionTasks(1)).items[0];
  const collectionPackage = (await api.listDataPackages({ workspace_id: 1 })).items[0];

  const details = {
    episode: () => api.getEpisode(episode.id),
    episodePreview: () => api.getEpisodePreviewUrl(episode.id),
    episodeAssets: () => api.getEpisodeAssets(episode.id),
    episodeRawSource: () => api.getEpisodeRawSourceDownloads(episode.id),
    episodeMultimodal: () => api.getEpisodeMultimodalSession(episode.id),
    workbench: () => api.getEpisodeWorkbench(4200, 9001),
    dataset: () => api.getDataset(dataset.id),
    datasetRevisions: () => api.listDatasetRevisions(dataset.id),
    datasetRevisionCandidates: () => api.listDatasetRevisionCandidates(dataset.id, { workspace_id: 1 }),
    datasetRevision: () => api.getDatasetRevision(revision.id),
    nativeDataset: () => api.getNativeLerobotDataset(nativeDataset.id),
    nativeDatasetOssUri: () => api.getNativeLerobotDatasetOssUri(nativeDataset.id),
    dataBatch: () => api.getDataBatch(dataBatch.id, { workspace_id: 1 }),
    dataAsset: () => api.getDataAsset(dataAsset.id),
    catalogDataset: () => api.getCatalogDataset(catalogDataset.id),
    catalogVersions: () => api.listCatalogVersions(catalogDataset.id),
    collectionProject: () => api.getCollectionProject(project.id, { workspace_id: 1 }),
    collectionTask: () => api.getCollectionTask(task.id, { workspace_id: 1 }),
    collectionTaskPackages: () => api.listTaskPackages(task.id, 1),
    dataPackage: () => api.getDataPackage(collectionPackage.id, 1),
    offlineManifest: () => api.getOfflineManifest(collectionPackage.id, 1),
  };

  for (const [name, load] of Object.entries(details)) {
    const payload = await load();
    assert.ok(payload !== undefined && payload !== null, `${name} returned nothing`);
    const items = itemsOf(payload);
    if (items) {
      assert.ok(items.length > 0, `${name} returned an empty list`);
    } else {
      assert.ok(
        typeof payload === 'object' ? Object.keys(payload).length > 0 : true,
        `${name} returned an empty payload`,
      );
    }
  }
});

test('demo mode write flows update the data the console reads back', async () => {
  const api = createApi();

  const project = await api.createCollectionProject({ workspace_id: 1, name: '演示项目' });
  assert.ok(project.id);
  const projects = (await api.listCollectionProjects(1)).items;
  assert.ok(projects.some((item) => Number(item.id) === Number(project.id)), 'created project is listed');

  const task = await api.createCollectionTask({ workspace_id: 1, project_id: project.id, name: '演示采集任务' });
  const tasks = (await api.listCollectionTasks(1)).items;
  assert.ok(tasks.some((item) => Number(item.id) === Number(task.id)), 'created task is listed');

  const label = await api.createCollectionLabel({ workspace_id: 1, category: 'scene', name: '演示标签' });
  await api.deactivateCollectionLabel(label.id, 1);
  const labels = (await api.listCollectionLabels(1, null, true)).items;
  assert.equal(
    labels.find((item) => Number(item.id) === Number(label.id))?.is_active,
    false,
    'deactivated label is hidden from active filters',
  );

  const profile = await api.createCollectorProfile({ workspace_id: 1, name: '演示采集员' });
  const profiles = (await api.listCollectorProfiles(1)).items;
  assert.ok(profiles.some((item) => Number(item.id) === Number(profile.id)), 'created collector is listed');

  const device = await api.createCollectionDevice({ workspace_id: 1, name: '演示设备' });
  const devices = (await api.listCollectionDevices(1)).items;
  assert.ok(devices.some((item) => Number(item.id) === Number(device.id)), 'created device is listed');

  const dataBatch = await api.createDataBatch({ workspace_id: 1, name: '演示数据批次', data_package_ids: [5101] });
  const dataBatches = (await api.listDataBatches({ workspace_id: 1 })).items;
  assert.ok(dataBatches.some((item) => Number(item.id) === Number(dataBatch.id)), 'created data batch is listed');

  const annotation = (await api.listAnnotationWorkItems({ workspace_id: 1 })).items[0];
  await api.submitAnnotationWorkItem(annotation.id, {});
  const submitted = (await api.listAnnotationWorkItems({ workspace_id: 1 })).items
    .find((item) => Number(item.id) === Number(annotation.id));
  assert.equal(submitted.status, 'submitted', 'annotation submission updates status');

  const review = (await api.listReviewWorkItems({ workspace_id: 1 })).items[0];
  await api.approveReviewWorkItem(review.id, {});
  const approved = (await api.listReviewWorkItems({ workspace_id: 1 })).items
    .find((item) => Number(item.id) === Number(review.id));
  assert.equal(approved.status, 'approved', 'review approval updates status');

  const catalogDataset = await api.createCatalogDataset({ name: '演示目录数据集' });
  const version = await api.createCatalogVersion(catalogDataset.id, { data_asset_ids: [7501] });
  const versions = (await api.listCatalogVersions(catalogDataset.id)).items;
  assert.ok(versions.some((item) => Number(item.id) === Number(version.id)), 'created catalog version is listed');
  const exported = await api.exportCatalogVersion(version.id, { format: 'lerobot_3_0' });
  assert.equal(exported.status, 'running', 'catalog export starts a job');

  const dataset = await api.createDataset({ workspace_id: 1, name: '演示数据集' });
  const datasets = (await api.listDatasets({ workspace_id: 1 })).items;
  assert.ok(datasets.some((item) => Number(item.id) === Number(dataset.id)), 'created dataset is listed');
  const revision = await api.createDatasetRevision(dataset.id, { episode_ids: [4300], filters: {} });
  const exportJob = await api.exportDatasetRevision(revision.id, { format: 'lerobot_3_0' });
  assert.equal(exportJob.status, 'running', 'dataset export starts a job');
  const job = await api.getExport(exportJob.id);
  assert.equal(Number(job.id), Number(exportJob.id), 'export job is readable');

  const collectionPackage = (await api.listDataPackages({ workspace_id: 1 })).items
    .find((item) => item.status === 'pending_assignment');
  assert.ok(collectionPackage, 'demo exposes an unassigned package for assignment');
  await api.assignDataPackage(collectionPackage.id, { responsible_collector_id: 102, target_duration_hours: '3.00' });
  const assigned = await api.getDataPackage(collectionPackage.id, 1);
  assert.equal(Number(assigned.responsible_collector_id), 102, 'package assignment is persisted');

  const reviewTarget = (await api.listDataPackages({ workspace_id: 1, status: 'pending_intake_review' })).items[0];
  await api.reviewIntakePackage(reviewTarget.id, { workspace_id: 1, verdict: 'approved' });
  const reviewed = await api.getDataPackage(reviewTarget.id, 1);
  assert.equal(reviewed.status, 'intake_approved', 'intake approval updates package status');

  const rejectedTarget = (await api.listDataPackages({ workspace_id: 1, status: 'pending_intake_review' })).items[0];
  await api.reviewIntakePackage(rejectedTarget.id, { workspace_id: 1, verdict: 'rejected', reason: '演示退回' });
  const rejected = await api.getDataPackage(rejectedTarget.id, 1);
  assert.equal(rejected.status, 'voided', 'intake rejection voids the package');
});

test('demo mode maps every collection task to reviewable data packages', async () => {
  const api = createApi();
  const tasks = (await api.listCollectionTasks(1)).items;
  const packages = (await api.listDataPackages({ workspace_id: 1 })).items;
  const countsByTask = new Map();
  for (const row of packages) {
    const key = Number(row.collection_task_id);
    countsByTask.set(key, (countsByTask.get(key) || 0) + 1);
  }

  assert.ok(tasks.length > 0, 'collection tasks are listed');
  for (const task of tasks) {
    assert.ok((countsByTask.get(Number(task.id)) || 0) > 0, `collection task ${task.name} exposes packages`);
  }
  for (const row of packages) {
    assert.ok(row.package_uid, `data package ${row.id} keeps its uid`);
    const detail = await api.getDataPackage(row.id, 1);
    assert.equal(detail.package_uid, row.package_uid, 'package detail resolves the listed uid');
  }
  assert.ok(
    packages.some((row) => row.status === 'pending_intake_review'),
    'demo exposes packages waiting for intake review',
  );
});

test('demo mode exposes an annotation and review workbench queue', async () => {
  const api = createApi();

  for (const stage of ['annotation', 'review']) {
    const rows = (await api.listWorkQueue({ stage })).items;
    assert.ok(rows.length > 0, `${stage} work-item queue is populated`);
    const row = rows[0];
    assert.equal(row.work_item.kind, stage, `${stage} queue exposes matching work item kind`);
    assert.ok(
      ['assigned', 'in_progress'].includes(row.work_item.status),
      `${stage} work item is actionable`,
    );
    assert.equal(row.preview?.status, 'ready', `${stage} work item has a ready preview`);
    assert.equal(row.episode.kind, 'derived', `${stage} work targets a derived clip`);

    const snapshot = await api.getEpisodeWorkbench(row.episode.id, row.work_item.id);
    assert.equal(snapshot.work_item.kind, stage, `${stage} workbench snapshot opens the work item`);
    assert.ok(snapshot.capabilities?.[stage], `${stage} workbench exposes its capability`);

    const timeline = await api.getEpisodeTimeline(row.episode.id);
    assert.ok(timeline.end_ns, `${stage} workbench has a timeline`);
  }
});

test('demo mode exposes the training control plane', async () => {
  const api = createApi();
  const dashboard = await api.trainRequest('GET', '/dashboard');
  assert.ok(dashboard.jobs.running + dashboard.jobs.queued > 0, 'dashboard reports active jobs');
  assert.ok(dashboard.recent_jobs.length > 0, 'dashboard lists recent jobs');

  const jobs = (await api.trainRequest('GET', '/jobs?limit=50')).items;
  assert.ok(jobs.length > 0, 'job list is populated');
  const logs = await api.trainRequest('GET', `/jobs/${jobs[0].id}/logs`);
  assert.ok(logs.lines.length > 0, 'job logs are available');
  const detail = await api.trainRequest('GET', `/jobs/${jobs[0].id}`);
  assert.equal(detail.id, jobs[0].id, 'job detail is readable');

  assert.ok((await api.trainRequest('GET', '/datasets')).items.length > 0, 'train datasets are listed');
  assert.ok((await api.trainRequest('GET', '/models')).items.length > 0, 'train models are listed');
  assert.ok((await api.trainRequest('GET', '/resources')).profiles.length > 0, 'train profiles are listed');
  const ops = await api.trainRequest('GET', '/ops/status');
  assert.equal(ops.status, 'healthy', 'ops status is readable');

  const validation = await api.trainRequest('POST', '/jobs/validate', {
    body: { dataset_version_id: 'dsv_desk_0820', model_version_id: 'act_v1_4', recipe_id: 'fine_tune' },
  });
  assert.equal(validation.issues.length, 0, 'valid job request passes validation');

  const created = await api.trainRequest('POST', '/jobs', {
    body: {
      display_name: 'demo-job',
      dataset_version_id: 'dsv_desk_0820',
      model_version_id: 'act_v1_4',
      recipe_id: 'fine_tune',
      config_overrides: { 'training.steps': 1000, 'training.batch_size': 32 },
    },
  });
  assert.ok(created.job_id, 'job creation returns an id');
  const afterCreate = (await api.trainRequest('GET', '/jobs?limit=50')).items;
  assert.ok(afterCreate.some((job) => job.id === created.job_id), 'created job is listed');
  const cancelled = await api.trainRequest('POST', `/jobs/${created.job_id}/cancel`);
  assert.equal(cancelled.state, 'cancelled', 'job cancellation updates state');

  const registered = await api.trainRequest('POST', '/datasets', {
    body: { display_name: 'demo-dataset', uri: 'oss://quicstudio-train/demo/v1', status: 'READY' },
  });
  assert.ok(registered.id, 'dataset registration returns an id');
});
