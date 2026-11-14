import { useEffect, useRef, useState } from 'react';
import ReactECharts from './Chart';
import { Activity, Archive, ArrowUpRight, Check, ChevronRight, CircleAlert, Database, FileCheck2, RefreshCw, Search, Upload, Video } from 'lucide-react';
import { api, isConflict, listenRun } from './api';
import { deriveStages } from './progress';
import { clearDraft, draftKey, getDraft, loadSession, saveSession } from './session';
import type { DraftValues } from './session';
import type { Batch, CameraInspection, EpisodeDetail, Issue, RouteKey, RunRecord } from './types';

const routeLabels: Record<RouteKey, string> = {
  overview: '批次总览', episodes: '数据浏览', runs: '运行诊断', delivery: '交付核验',
};

function readLocation() {
  const params = new URLSearchParams(window.location.search);
  const route = params.get('view') as RouteKey | null;
  const episode = Number(params.get('ep'));
  return {
    route: route && route in routeLabels ? route : 'overview',
    batch: params.get('batch') || '',
    episode: Number.isInteger(episode) && episode >= 0 ? episode : null,
    run: params.get('run') || '',
  };
}

function setLocation(route: RouteKey, batch: string, episode?: number, run?: string) {
  const params = new URLSearchParams();
  params.set('view', route);
  if (batch) params.set('batch', batch);
  if (episode && episode > 0) params.set('ep', String(episode));
  if (run) params.set('run', run);
  window.history.pushState({}, '', `${window.location.pathname}?${params.toString()}`);
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '操作未完成，请刷新后重试';
}

/** 当前仍有效的转换产物与交付包数量：任何 content_version 递增都会让它们失效。 */
function currentArtifactCount(batch: Batch | null | undefined) {
  return [...(batch?.outputs ?? []), ...(batch?.deliveries ?? [])]
    .filter((item) => (item as { current?: boolean }).current).length;
}

type ConflictIntent =
  | { kind: 'quality' }
  | { kind: 'recover'; runId: string }
  | { kind: 'disposition'; episode: number; disposition: string; reason: string };

function statusText(status?: string) {
  const labels: Record<string, string> = {
    passed: '检查通过', issues: '发现问题', incomplete: '检查不完整', load_failed: '读取失败',
    queued: '等待启动', running: '运行中', completed: '已完成', completed_with_errors: '部分步骤失败',
    failed: '运行失败', interrupted: '运行中断', keep: '保留', quarantine: '隔离', exclude: '排除',
    approved: '审核通过', submitted: '待审核', returned: '已退回', draft: '草稿', registered: '已登记',
    pending: '等待登记', stale: '版本已变化',
  };
  return labels[status || ''] || status || '未知';
}

function StatusTag({ value }: { value?: string }) {
  const tone = value === 'passed' || value === 'completed' || value === 'registered' || value === 'approved' ? 'good'
    : value === 'running' || value === 'queued' || value === 'submitted' ? 'active'
      : value === 'issues' || value === 'incomplete' || value === 'pending' || value === 'returned' ? 'warning'
        : value === 'failed' || value === 'load_failed' || value === 'stale' ? 'danger' : 'quiet';
  return <span className={`status-tag ${tone}`}>{statusText(value)}</span>;
}

function EChart({ option, height = 300 }: { option: object; height?: number }) {
  const colors = [cssToken('--color-accent', '#26766a'), cssToken('--color-chart-secondary', '#a85f26'), cssToken('--color-info', '#365f8b'), cssToken('--color-success', '#276844')];
  return <ReactECharts option={{ color: colors, ...option }} style={{ height, width: '100%' }} notMerge lazyUpdate />;
}

function cssToken(name: string, fallback: string) {
  if (typeof document === 'undefined') return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function App() {
  const initial = readLocation();
  // 只初始化一次：避免回放时每帧都读 localStorage。
  const [initialSession] = useState(loadSession);
  const restoredRoute = window.location.search ? initial.route : (initialSession.route ?? initial.route);
  const restoredBatch = initial.batch || initialSession.batchId || '';
  const restoredEpisode = initial.episode ?? initialSession.episodeIndex ?? 0;
  const restoredRunId = initial.run || initialSession.selectedRunId;
  const [route, setRoute] = useState<RouteKey>(restoredRoute);
  const [activeBatchId, setActiveBatchId] = useState(restoredBatch);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [batchErrors, setBatchErrors] = useState<Array<{ batch_id: string; message: string }>>([]);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [runEvents, setRunEvents] = useState<Array<Record<string, unknown>>>([]);
  const [episodeIndex, setEpisodeIndex] = useState(restoredEpisode);
  const [episode, setEpisode] = useState<EpisodeDetail | null>(null);
  const [cameraInfo, setCameraInfo] = useState<Record<string, CameraInspection>>({});
  const [currentFrame, setCurrentFrame] = useState(0);
  const [playing, setPlaying] = useState(false);
  const videoRefs = useRef<Record<string, HTMLVideoElement | null>>({});
  const activeBatchIdRef = useRef(restoredBatch);
  const refreshSeq = useRef(0);
  const frameRestored = useRef(false);
  const runRestored = useRef(false);
  const contextRef = useRef({ route: restoredRoute, batchId: restoredBatch, episodeIndex: restoredEpisode, currentFrame: 0, selectedRunId: restoredRunId });
  const draftRef = useRef<{ batchId: string; episodeIndex: number; values: DraftValues } | null>(null);
  activeBatchIdRef.current = activeBatchId;
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  // 后端 409 表示「服务端认为这次写是破坏性的」：保留可一键「确认并重试」的出口，
  // 而不是让用户对着一个只能关闭的红条反复点同一个按钮。
  const [conflict, setConflict] = useState<{ message: string; intent: ConflictIntent } | null>(null);

  const showError = (message: string) => { setError(message); setNotice(''); };
  const showNotice = (message: string) => { setNotice(message); setError(''); };
  const [sourceKind, setSourceKind] = useState<'hdf5' | 'so100' | 'aloha'>('aloha');
  const [sourcePath, setSourcePath] = useState('');
  const [batchLabel, setBatchLabel] = useState('ALOHA Coffee · Episode 0–4');
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [verifyPath, setVerifyPath] = useState('');
  const [disposition, setDisposition] = useState('keep');
  const [dispositionReason, setDispositionReason] = useState('');
  const [annotationTask, setAnnotationTask] = useState('');
  const [annotationOutcome, setAnnotationOutcome] = useState('uncertain');
  const [annotationTags, setAnnotationTags] = useState('');
  const [annotationNotes, setAnnotationNotes] = useState('');
  const [reviewReason, setReviewReason] = useState('');
  const [focusedCamera, setFocusedCamera] = useState<string | null>(null);
  const [inspectorHidden, setInspectorHidden] = useState(false);

  // 渲染纯性：ref 只在 commit 之后更新；切到别的视图不清空草稿。
  useEffect(() => {
    contextRef.current = { route, batchId: activeBatchId, episodeIndex, currentFrame,
      selectedRunId: selectedRun?.run_id };
    const formValues: DraftValues = {
      task: annotationTask, outcome: annotationOutcome, tags: annotationTags, notes: annotationNotes,
      disposition, dispositionReason, reviewReason, contentVersion: batch?.content_version ?? 0,
    };
    const serverValues: DraftValues | null = (episode && batch) ? {
      task: episode.annotation.task || '', outcome: episode.annotation.outcome || 'unlabeled',
      tags: (episode.annotation.tags || []).join('、'), notes: episode.annotation.notes || '',
      disposition: episode.disposition, dispositionReason: '', reviewReason: '',
      contentVersion: batch.content_version,
    } : null;
    // 只有「用户真的改过表单」才记录草稿；写成功后表单与服务器一致，草稿自动失效。
    const draftChanged = Boolean(serverValues && (Object.keys(formValues) as Array<keyof DraftValues>)
      .some((key) => formValues[key] !== serverValues[key]));
    draftRef.current = (batch && episode && serverValues && draftChanged)
      ? { batchId: batch.batch_id, episodeIndex, values: formValues }
      : null;
  });

  const flushSession = () => {
    const context = contextRef.current;
    const draft = draftRef.current;
    saveSession({
      route: context.route, batchId: context.batchId, episodeIndex: context.episodeIndex,
      currentFrame: context.currentFrame, selectedRunId: context.selectedRunId,
      ...(draft ? { drafts: { [draftKey(draft.batchId, draft.episodeIndex)]: draft.values } } : {}),
    });
  };

  const versionRef = useRef<{ id: string; version: number } | null>(null);

  const refresh = async () => {
    const seq = ++refreshSeq.current;
    try {
      const [batchResult, runResult] = await Promise.all([api.batches(), api.runs()]);
      if (seq !== refreshSeq.current) return; // 慢响应不得覆盖更新的批次上下文
      const currentId = activeBatchIdRef.current;
      const hasBatch = Boolean(currentId && batchResult.batches.some((item) => item.batch_id === currentId));
      // 详情是唯一真相来源；所有提交必须等这两个请求都完成且序列号仍最新，
      // 避免「列表已更新、详情还是旧值」的中间态被用户当成状态回退。
      const detail = hasBatch ? await api.batch(currentId) : null;
      if (seq !== refreshSeq.current) return;
      const previous = versionRef.current;
      if (previous && previous.id === currentId && detail && previous.version !== detail.content_version) {
        clearDraft(currentId, contextRef.current.episodeIndex);
        // 归因保持中性：可能是后台处理、另一个标签页，也可能是用户自己的写操作。
        setNotice(`批次内容版本已更新（${previous.version} → ${detail.content_version}）；审核状态与已登记产物可能已失效，本地草稿已清除，请重新确认。`);
      }
      setBatches(batchResult.batches);
      setBatchErrors(batchResult.errors);
      setRuns(runResult.runs);
      setSelectedRun((current) => current
        ? (runResult.runs.find((item) => item.run_id === current.run_id) ?? current) : current);
      setLoading(false);
      setLoadFailed(false);
      if (!detail) { setBatch(null); versionRef.current = null; return; }
      versionRef.current = { id: currentId, version: detail.content_version };
      setBatch(detail);
    } catch {
      if (seq !== refreshSeq.current) return;
      setLoading(false);
      // 连接失败用独立常驻横幅表达，不写进会被后续操作清掉的 error。
      setLoadFailed(true);
    }
  };

  useEffect(() => { void refresh(); }, [activeBatchId]);

  useEffect(() => {
    const onPop = () => {
      const next = readLocation();
      setRoute(next.route);
      setActiveBatchId(next.batch);
      if (next.episode !== null) setEpisodeIndex(next.episode);
      if (!next.run) setSelectedRun(null);
      // 前进/后退属于「用户主动换上下文」，允许重新触发一次性恢复。
      runRestored.current = false;
      frameRestored.current = false;
    };
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') { flushSession(); return; }
      // 长时间离开后回到页面：重新拉取服务器真相，避免用过期 revision 提交。
      // 不清 error：刚失败的操作不能因为切一次后台就被抹掉。
      void refresh();
    };
    const onUnload = () => flushSession();
    window.addEventListener('popstate', onPop);
    window.addEventListener('beforeunload', onUnload);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('popstate', onPop);
      window.removeEventListener('beforeunload', onUnload);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => flushSession(), 600);
    return () => window.clearTimeout(timer);
  }, [route, activeBatchId, episodeIndex, currentFrame, selectedRun?.run_id,
      disposition, dispositionReason, annotationTask, annotationOutcome,
      annotationTags, annotationNotes, reviewReason, episode]);

  // URL 始终携带完整上下文，刷新 / 分享 / 前进后退都能恢复，不依赖内存状态。
  useEffect(() => {
    const params = new URLSearchParams();
    params.set('view', route);
    if (activeBatchId) params.set('batch', activeBatchId);
    if (episodeIndex > 0) params.set('ep', String(episodeIndex));
    if (selectedRun?.run_id) params.set('run', selectedRun.run_id);
    window.history.replaceState(null, '', `${window.location.pathname}?${params.toString()}`);
  }, [route, activeBatchId, episodeIndex, selectedRun?.run_id]);

  // 成功提示自动过期，避免下一次失败时仍显示上一条过期成功消息。
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(''), 8000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (runRestored.current || !restoredRunId || !runs.length) return;
    // URL 里的 run 优先于本地会话；找不到时保留标记，等列表下一次刷新再试。
    const found = runs.find((item) => item.run_id === restoredRunId);
    if (!found) return;
    runRestored.current = true;
    setSelectedRun(found);
  }, [runs, restoredRunId]);

  useEffect(() => {
    if (frameRestored.current || !episode || route !== 'episodes') return;
    frameRestored.current = true;
    const saved = initialSession.currentFrame;
    const savedEpisode = initial.episode ?? initialSession.episodeIndex;
    if (typeof saved === 'number' && Number.isFinite(saved) && saved > 0
        && savedEpisode === episodeIndex) {
      setCurrentFrame(Math.min(saved, Math.max(0, episode.row_count - 1)));
    }
  }, [episode, route, initialSession, episodeIndex, initial.episode]);

  useEffect(() => {
    let alive = true;
    if (!batch || route !== 'episodes') {
      setEpisode(null); setCameraInfo({}); videoRefs.current = {}; return;
    }
    setEpisode(null);
    videoRefs.current = {};
    const draft = getDraft(batch.batch_id, episodeIndex, batch.content_version);
    api.episode(batch.batch_id, episodeIndex).then(async (value) => {
      if (!alive) return;
      setEpisode(value);
      // 本地草稿只覆盖表单文本；审核状态、质检结论始终以服务器返回为准。
      setDisposition(draft?.disposition ?? value.disposition);
      setAnnotationTask(draft?.task ?? value.annotation.task ?? '');
      setAnnotationOutcome(draft?.outcome ?? value.annotation.outcome ?? 'unlabeled');
      setAnnotationTags(draft?.tags ?? (value.annotation.tags || []).join('、'));
      setAnnotationNotes(draft?.notes ?? value.annotation.notes ?? '');
      setDispositionReason(draft?.dispositionReason ?? '');
      setReviewReason(draft?.reviewReason ?? '');
      const inspections = await Promise.all(value.cameras.map((camera) =>
        api.camera(batch.batch_id, episodeIndex, camera.name).then((result) => [camera.name, result] as const).catch(() => null)));
      if (alive) setCameraInfo(Object.fromEntries(inspections.filter((item): item is readonly [string, CameraInspection] => item !== null)));
    }).catch((err) => { if (alive) setError(errorMessage(err)); });
    return () => { alive = false; };
  }, [batch?.batch_id, route, episodeIndex]);

  useEffect(() => {
    if (!selectedRun) return;
    return listenRun(selectedRun.run_id, ({ state, events }) => {
      setSelectedRun(state);
      setRunEvents(events);
      if (!['queued', 'running'].includes(state.status)) void refresh();
    }, setError);
  }, [selectedRun?.run_id]);

  useEffect(() => {
    if (!episode) return;
    const fps = episode.fps || 30;
    const elapsed = currentFrame / fps;
    for (const camera of episode.cameras) {
      const video = videoRefs.current[camera.name];
      if (!video || !Number.isFinite(elapsed)) continue;
      const target = Math.max(0, camera.from_timestamp + elapsed);
      if (Math.abs(video.currentTime - target) > 0.16) video.currentTime = target;
      if (playing && video.paused) void video.play().catch(() => setPlaying(false));
      if (!playing && !video.paused) video.pause();
    }
  }, [currentFrame, episode, playing]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (['INPUT', 'SELECT', 'TEXTAREA'].includes(target.tagName) || target.isContentEditable)) return;
      if (event.key === 'Escape') { setFocusedCamera(null); return; }
      if (route !== 'episodes' || !episode) return;
      if (event.key === ' ') { event.preventDefault(); setPlaying((value) => !value); }
      else if (event.key === 'ArrowLeft') { setPlaying(false); setCurrentFrame((value) => Math.max(0, value - 1)); }
      else if (event.key === 'ArrowRight') { setPlaying(false); setCurrentFrame((value) => Math.min(episode.row_count - 1, value + 1)); }
      else if (/^[1-4]$/.test(event.key)) {
        const camera = episode.cameras[Number(event.key) - 1];
        if (camera) setFocusedCamera((current) => current === camera.name ? null : camera.name);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [route, episode]);

  const go = (next: RouteKey) => {
    setRoute(next);
    setFocusedCamera(null);
    setLocation(next, activeBatchId, episodeIndex, selectedRun?.run_id);
    setError('');
  };

  const pickBatch = (id: string) => {
    setActiveBatchId(id);
    setLocation(route, id, episodeIndex, selectedRun?.run_id);
    const found = batches.find((item) => item.batch_id === id);
    if (found) setBatch(found);
  };

  // 所有写操作串行化：杜绝并发 finally 提前解锁、重复提交、旧 revision 覆盖新状态。
  // intent 用于 409：把服务端门禁的拒绝变成「确认并重试」的可操作出口。
  const exclusive = async (action: () => Promise<void>, intent?: ConflictIntent) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    setConflict(null);
    try { await action(); }
    catch (err) {
      showError(errorMessage(err));
      if (intent && isConflict(err)) setConflict({ message: errorMessage(err), intent });
    }
    finally { inFlight.current = false; setBusy(false); }
  };

  // 409 兜底：先取服务器真相（最新 revision / content_version），再带显式确认标记重发一次，
  // 避免「本地判据陈旧 → 反复 409 → 无法恢复」的死循环。
  const confirmAndRetry = async () => {
    if (!conflict || inFlight.current) return;
    const { intent } = conflict;
    setConflict(null);
    inFlight.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      let latest: Batch | null = null;
      if (intent.kind !== 'recover') {
        const id = activeBatchIdRef.current;
        latest = id ? await api.batch(id) : null;
        if (latest) updateBatch(latest);
      }
      if (intent.kind === 'quality') {
        if (!latest) throw new Error('批次已不存在，请刷新后重新选择。');
        await api.startRun({ operation: 'quality', batch_id: latest.batch_id,
          revision: latest.revision, invalidate_reviews: true });
        showNotice('已确认并重发质检：审核状态与已登记产物将失效，需重新确认、转换与打包。');
      } else if (intent.kind === 'recover') {
        const result = await api.recoverRun(intent.runId, true);
        showNotice(`恢复登记结果：${String(result.message || result.status || '已核对')}`);
      } else {
        if (!latest) throw new Error('批次已不存在，请刷新后重新选择。');
        const next = await api.updateDisposition(latest, intent.episode, intent.disposition, intent.reason, true);
        applyServerBatch(next);
        showNotice('筛选决定已保存，旧审核状态与已登记产物已失效，需重新确认。');
      }
      await refresh();
    } catch (err) {
      showError(errorMessage(err));
    } finally { inFlight.current = false; setBusy(false); }
  };

  const start = (body: Record<string, unknown>, intent?: ConflictIntent) => exclusive(async () => {
    const result = await api.startRun(body);
    const run = await api.run(result.run_id);
    setSelectedRun(run);
    setRunEvents([]);
    go('runs');
    showNotice('处理已排队，运行证据会在此页更新。');
  }, intent);

  const importBatch = () => exclusive(async () => {
    let result: { run_id: string };
    if (uploadFile) {
      const form = new FormData();
      form.set('file', uploadFile);
      form.set('label', batchLabel);
      const response = await fetch('/api/imports/upload', { method: 'POST', body: form });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '上传失败');
      result = payload;
    } else {
      result = await api.startRun({ operation: 'import', source_kind: sourceKind, path: sourcePath || undefined, label: batchLabel });
    }
    const run = await api.run(result.run_id);
    setSelectedRun(run);
    setRoute('runs');
    setLocation('runs', activeBatchId, episodeIndex, run.run_id);
    showNotice('导入任务已排队；原始数据将保持只读。');
  });

  const updateBatch = (next: Batch) => {
    // 写成功后必须同步版本基准：否则下一次 refresh 会把用户自己的显式操作
    // 误报成「后台处理改了批次」，并连带清掉草稿。
    if (next.batch_id === activeBatchIdRef.current) {
      versionRef.current = { id: next.batch_id, version: next.content_version };
    }
    setBatch(next);
    setBatches((items) => items.map((item) => item.batch_id === next.batch_id ? next : item));
  };

  /** 显式业务写操作在已有当前产物时会让产物失效，先讲清后果再执行。 */
  const confirmArtifactInvalidation = (action: string, target: Batch | null = batch) => {
    const count = currentArtifactCount(target);
    if (!count || typeof window.confirm !== 'function') return true;
    return window.confirm(
      `本批次有 ${count} 项当前转换产物或交付包。\n\n${action}会让批次内容版本递增，这些产物与交付包将变为非当前版本，需要重新转换与打包。\n\n确定继续吗？`);
  };

  /** 写成功后统一说明内容版本变化对已登记产物的影响。 */
  const versionNotice = (before: Batch, after: Batch) => {
    if (after.content_version === before.content_version) return '';
    return currentArtifactCount(before) > 0
      ? `；批次内容版本 ${before.content_version} → ${after.content_version}，已登记转换产物与交付包变为非当前版本，需重新转换与打包。`
      : `；批次内容版本 ${before.content_version} → ${after.content_version}。`;
  };

  // 写成功后：清掉本地草稿，并把表单同步成服务器归一化结果。
  // 表单与服务器一致后 draftRef 自动为 null，不会被防抖保存重新写回。
  const applyServerBatch = (next: Batch) => {
    updateBatch(next);
    clearDraft(next.batch_id, episodeIndex);
    const record = next.episodes.find((item) => item.episode_index === episodeIndex);
    if (!record) return;
    setDisposition(record.disposition);
    setAnnotationTask(record.annotation?.task ?? '');
    setAnnotationOutcome(record.annotation?.outcome ?? 'unlabeled');
    setAnnotationTags((record.annotation?.tags ?? []).join('、'));
    setAnnotationNotes(record.annotation?.notes ?? '');
    setDispositionReason('');
    setReviewReason('');
  };

  const restoreFromServer = () => {
    if (!batch || !episode) return;
    clearDraft(batch.batch_id, episodeIndex);
    setDisposition(episode.disposition);
    setAnnotationTask(episode.annotation.task || '');
    setAnnotationOutcome(episode.annotation.outcome || 'unlabeled');
    setAnnotationTags((episode.annotation.tags || []).join('、'));
    setAnnotationNotes(episode.annotation.notes || '');
    setDispositionReason('');
    setReviewReason('');
    showNotice('已清除本地草稿，表单恢复为服务器内容。');
  };

  const startQuality = () => {
    if (!batch || inFlight.current) return;
    const protectedCount = batch.episodes.filter((item) =>
      item.review?.status === 'submitted' || item.review?.status === 'approved').length;
    if (protectedCount > 0 && !window.confirm(
      `该批次有 ${protectedCount} 个任务已提交或已通过审核。\n\n重跑批次质检会让这些审核失效、候选集需要重新确认，已有转换产物与交付包也会变为非当前版本。\n\n确定继续吗？`)) return;
    void start({ operation: 'quality', batch_id: batch.batch_id, revision: batch.revision,
      ...(protectedCount > 0 ? { invalidate_reviews: true } : {}) }, { kind: 'quality' });
  };

  const recoverRun = () => {
    if (!selectedRun) return;
    // 质检登记会重置审核与产物，不能让「恢复」看起来像一个无害的重试按钮。
    const registration = selectedRun.business_registration;
    const isQuality = registration?.registration_operation === 'quality'
      || (registration?.registration_operation === undefined && selectedRun.request?.operation === 'quality');
    if (isQuality && !window.confirm(
      '该运行待恢复的是批次质检登记。\n\n恢复会重置全部任务的审核状态、退回筛选确认阶段，已验证转换产物与交付包也会变为非当前版本，需重新质检、转换与打包。\n\n确定继续吗？')) return;
    void exclusive(async () => {
      const result = await api.recoverRun(selectedRun.run_id, isQuality);
      showNotice(`恢复登记结果：${String(result.message || result.status || '已核对')}`);
      await refresh();
    }, { kind: 'recover', runId: selectedRun.run_id });
  };

  const requireEpisode = () => {
    if (!batch || !episode) { showError('请先打开数据浏览，等待当前任务加载完成后再操作。'); return false; }
    return true;
  };

  const saveDisposition = () => exclusive(async () => {
    if (!requireEpisode() || !batch) return;
    const protectedCount = batch.episodes.filter((item) =>
      item.review?.status === 'submitted' || item.review?.status === 'approved').length;
    const currentArtifacts = currentArtifactCount(batch);
    const destructive = protectedCount > 0 || currentArtifacts > 0;
    if (destructive && !window.confirm(
      `该批次有 ${protectedCount} 个任务已提交或已通过审核、${currentArtifacts} 项当前产物或交付包。\n\n修改筛选决定会把全部保留任务的审核退回草稿、清洗确认失效，已验证转换产物与交付包也会变为非当前版本，需重新确认、转换与打包。\n\n确定继续吗？`)) return;
    const next = await api.updateDisposition(batch, episodeIndex, disposition, dispositionReason, destructive);
    applyServerBatch(next);
    showNotice('筛选决定已保存，旧审核状态与已登记产物已失效，需重新确认。');
  }, { kind: 'disposition', episode: episodeIndex, disposition, reason: dispositionReason });

  const saveAnnotation = () => exclusive(async () => {
    if (!requireEpisode() || !batch) return;
    if (!confirmArtifactInvalidation('保存标注')) return;
    const next = await api.saveAnnotation(batch, episodeIndex, {
      task: annotationTask, outcome: annotationOutcome,
      tags: annotationTags.split(/[、,，]/).map((item) => item.trim()).filter(Boolean), notes: annotationNotes,
    });
    applyServerBatch(next);
    showNotice(`标注已保存为草稿${versionNotice(batch, next)}`);
  });

  const submitAnnotation = () => exclusive(async () => {
    if (!requireEpisode() || !batch) return;
    const next = await api.submitAnnotation(batch, episodeIndex);
    applyServerBatch(next);
    showNotice(`标注已提交审核${versionNotice(batch, next)}`);
  });

  const reviewAnnotation = (approve: boolean) => exclusive(async () => {
    if (!requireEpisode() || !batch) return;
    if (!confirmArtifactInvalidation(approve ? '通过审核' : '退回标注')) return;
    const next = await api.reviewAnnotation(batch, episodeIndex, approve, reviewReason);
    applyServerBatch(next);
    showNotice(`${approve ? '审核已通过' : '标注已退回'}${versionNotice(batch, next)}`);
  });

  const confirmCleaning = () => exclusive(async () => {
    if (!batch) return;
    if (!confirmArtifactInvalidation('确认筛选结果')) return;
    const next = await api.confirmCleaning(batch);
    updateBatch(next);
    showNotice(`当前筛选结果已确认${versionNotice(batch, next)}`);
  });

  const activeRuns = runs.filter((run) => run.status === 'queued' || run.status === 'running').length;
  const issueCount = batch?.episodes.reduce((total, item) => total + (item.quality?.issue_count || 0), 0) || 0;
  const keptCount = batch?.episodes.filter((item) => item.disposition === 'keep').length || 0;
  const timelineReady = route === 'episodes' && Boolean(episode);
  const lastFrame = Math.max(0, (episode?.row_count || 1) - 1);
  const sampleFrames = episode ? episode.samples.map((item) => Number(item.frame_index ?? 0)) : [];
  const cursorFrame = sampleFrames.length
    ? sampleFrames.reduce((best, value) => Math.abs(value - currentFrame) < Math.abs(best - currentFrame) ? value : best, sampleFrames[0])
    : 0;

  return (
    <div className={inspectorHidden ? 'workbench-shell no-inspector' : 'workbench-shell'}>
      <header className="topbar">
        <a className="brand" href="/?view=overview" onClick={(event) => { event.preventDefault(); go('overview'); }} aria-label="RoboData 总览">
          <span className="brand-mark"><Activity size={17} strokeWidth={2.2} /></span>
          <span><strong>RoboData</strong><small>具身数据工作台</small></span>
        </a>
        <span className="shell-chip version-chip">v0.9.0 · React 工作台</span>
        <span className="shell-crumb">工作台 <ChevronRight size={13} /> {routeLabels[route]}</span>
        {batch && <span className="shell-chip">{batch.label}</span>}
        {batch && <span className="shell-chip ok">{issueCount} 个质检问题</span>}
        <div className="topbar-actions">
          <button className="shell-toggle" onClick={() => setInspectorHidden(!inspectorHidden)}>{inspectorHidden ? '展开检视' : '收起检视'}</button>
          <span className="local-state"><span className="state-mark" />仅本机运行</span>
          <button className="icon-button" aria-label="刷新数据" onClick={() => void refresh()}><RefreshCw size={17} /></button>
        </div>
      </header>

      <aside className="sidenav">
        <div className="nav-group">工作区</div>
        <nav className="primary-nav" aria-label="主导航">
          {(Object.keys(routeLabels) as RouteKey[]).map((key) => (
            <button className={route === key ? 'nav-link selected' : 'nav-link'} key={key} onClick={() => go(key)}>{routeLabels[key]}</button>
          ))}
        </nav>
        <div className="nav-group">当前批次</div>
        <div className="nav-meta">{batch ? <>{batch.label}<br />版本 {batch.content_version} · {batch.episodes.length} 个任务<br />{batch.total_rows?.toLocaleString() || '—'} 帧记录</> : '尚未选择批次'}</div>
        <div className="nav-foot">固定布局 · 局部滚动<br />空格播放 · ← → 逐帧<br />1–4 切相机 · Esc 退出<br />仅本机运行，不提供公网服务</div>
      </aside>

      <main className="page-wrap">
        <div className="page-heading">
          <div>
            <p className="breadcrumb">工作台 <ChevronRight size={14} /> {routeLabels[route]}</p>
            <h1>{route === 'episodes' ? '查看原始任务与数据证据' : route === 'runs' ? '后台处理记录' : route === 'delivery' ? '交付状态与验证证据' : '机器人数据处理工作台'}</h1>
            <p className="page-subtitle">{route === 'overview' ? '从固定来源导入，到完成质检和可追溯交付。' : route === 'episodes' ? '相机时间映射来自文件检查，不代表硬件同步。' : route === 'runs' ? '运行关闭页面后继续；业务登记以验证结果为准。' : '只有通过官方加载、输出质检和独立验包的产物才可交付。'}</p>
          </div>
          <div className="heading-actions">
            {activeBatchId && <label className="batch-select-label">当前批次
              <select value={activeBatchId} onChange={(event) => pickBatch(event.target.value)} aria-label="选择批次">
                {batches.map((item) => <option value={item.batch_id} key={item.batch_id}>{item.label}</option>)}
              </select>
            </label>}
            <button className="button secondary" onClick={() => void refresh()}><RefreshCw size={16} />刷新</button>
          </div>
        </div>

        {loadFailed && <div className="notice error-notice" role="alert">
          <CircleAlert size={17} />本机后端未响应（127.0.0.1:8765）：请确认工作台服务已启动，页面当前展示的是上一次成功读取的内容。
          <button className="button secondary" onClick={() => void refresh()}>重试连接</button>
        </div>}
        {(error || notice) && <div className="notice-stack">
          {error && <div className="notice error-notice" role="alert">
            <CircleAlert size={17} />{error}
            {conflict && <button className="button secondary" onClick={() => void confirmAndRetry()} disabled={busy}>
              确认并重试
            </button>}
            <button className="notice-close" onClick={() => { setError(''); setConflict(null); }} aria-label="关闭错误提示">×</button>
          </div>}
          {notice && <div className="notice success-notice" role="status">
            <Check size={17} />{notice}<button className="notice-close" onClick={() => setNotice('')} aria-label="关闭提示">×</button>
          </div>}
        </div>}

        {route === 'overview' && <section className="view-stack">
          <div className="summary-strip">
            <div><span>已登记批次</span><strong>{batches.length}</strong><small>原始样本按内容指纹追踪</small></div>
            <div><span>运行中</span><strong>{activeRuns}</strong><small>后台运行记录持续更新</small></div>
            <div><span>当前候选任务</span><strong>{keptCount}</strong><small>{batch?.label || '选择一个批次查看'}</small></div>
            <div><span>质检问题</span><strong>{issueCount}</strong><small>问题记录与 episode 关联</small></div>
          </div>
          <div className="overview-grid">
            <section className="panel batches-panel">
              <div className="panel-heading"><div><h2>批次</h2><p>公开样例与本机输入分别记录来源</p></div><span className="count-label">{batches.length} 个</span></div>
              {batches.length ? <div className="table-scroll"><table><thead><tr><th>批次名称</th><th>来源</th><th>任务</th><th>候选</th><th>最近阶段</th><th /></tr></thead>
                <tbody>{batches.map((item) => <tr key={item.batch_id} className={item.batch_id === activeBatchId ? 'row-selected' : ''}>
                  <td><button className="text-link" onClick={() => pickBatch(item.batch_id)}>{item.label}</button><small className="table-meta">版本 {item.content_version} · {item.batch_id.slice(-8)}</small></td>
                  <td><span className="source-label">{item.source.kind === 'aloha' ? 'ALOHA Coffee' : item.source.kind === 'so100' ? 'SO-100' : 'HDF5'}</span></td>
                  <td>{item.total_episodes || item.episodes.length} <small>tasks</small></td>
                  <td>{item.counts?.keep?.episodes || 0} / {item.episodes.length}</td>
                  <td><StatusTag value={item.cleaning_confirmed ? 'passed' : 'pending'} /></td>
                  <td><button className="icon-button small" aria-label={`浏览 ${item.label}`} onClick={() => { pickBatch(item.batch_id); go('episodes'); }}><ArrowUpRight size={15} /></button></td>
                </tr>)}</tbody></table></div>
                : loading ? <div className="empty-state"><RefreshCw size={24} /><strong>正在读取本机数据…</strong><span>首次加载会读取批次快照与运行记录。</span></div>
                  : <div className="empty-state"><Database size={24} /><strong>还没有登记批次</strong><span>导入公开样例，或选择本机已有的数据目录。</span></div>}
              {batchErrors.map((item) => <p className="inline-warning" key={item.batch_id}>{item.message} · {item.batch_id}</p>)}
            </section>
            <section className="panel import-panel">
              <div className="panel-heading"><div><h2>导入数据</h2><p>创建新的可追溯批次</p></div><Upload size={18} /></div>
              <label>数据来源<select value={sourceKind} onChange={(event) => setSourceKind(event.target.value as typeof sourceKind)}>
                <option value="aloha">ALOHA Coffee · 固定公开版本</option><option value="so100">SO-100 本机目录</option><option value="hdf5">HDF5 本机路径</option>
              </select></label>
              <label>批次名称<input value={batchLabel} onChange={(event) => setBatchLabel(event.target.value)} /></label>
              {sourceKind !== 'aloha' && <label>本机文件或目录路径<input value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} placeholder="例如 C:\\datasets\\robot-data" /></label>}
              {sourceKind === 'hdf5' && <label className="file-input-label">或上传 HDF5 文件<input type="file" accept=".h5,.hdf5" onChange={(event) => setUploadFile(event.target.files?.[0] || null)} /></label>}
              {sourceKind === 'aloha' && <div className="source-note"><strong>固定样例</strong><span>4 路相机 · 50 FPS · Episode 0–4</span><span>首次导入需联网下载并逐文件校验。</span></div>}
              <button className="button primary full-button" disabled={busy || !batchLabel.trim() || (sourceKind !== 'aloha' && !sourcePath.trim() && !uploadFile)} onClick={() => void importBatch()}>
                <Upload size={16} />{busy ? '正在提交…' : '开始导入'}
              </button>
              <p className="form-footnote">导入只复制和检查输入；源文件保持只读。</p>
            </section>
          </div>
          {batch && <section className="panel process-panel">
            <div className="panel-heading"><div><h2>批次处理流程</h2><p>{batch.label} · {batch.total_rows?.toLocaleString() || '—'} 帧记录</p></div><button className="button secondary" onClick={() => go('episodes')}>打开任务浏览器 <ChevronRight size={16} /></button></div>
            <div className="stage-track">{deriveStages(batch, runs).map((item, index) => (
              <div className={`stage-item ${item.state}`} key={item.name}>
                <span>{String(index + 1).padStart(2, '0')}</span><strong>{item.name}</strong>
                <small>{item.detail}</small>
              </div>
            ))}</div>
          </section>}
        </section>}

        {route === 'episodes' && <section className="explorer-grid">
          {!batch ? <div className="panel empty-state large-empty"><Database size={28} /><strong>请选择一个批次</strong><span>从顶部批次选择器打开数据浏览。</span></div> : <>
            <aside className="panel episode-list-panel">
              <div className="panel-heading"><div><h2>任务列表</h2><p>{batch.episodes.length} 个 episode</p></div><Search size={17} /></div>
              <div className="episode-list">{batch.episodes.map((item) => <button className={episodeIndex === item.episode_index ? 'episode-row selected' : 'episode-row'} key={item.episode_index} onClick={() => { setEpisodeIndex(item.episode_index); setCurrentFrame(0); setPlaying(false); }}>
                <span className="episode-number">{String(item.episode_index).padStart(3, '0')}</span><span className="episode-copy"><strong>Episode {item.episode_index}</strong><small>{item.row_count.toLocaleString()} 帧 · {statusText(item.disposition)}</small></span><StatusTag value={item.quality.status} />
              </button>)}</div>
            </aside>
            <div className="episode-main">
              <section className="panel video-panel">
                <div className="panel-heading"><div><h2>多相机回放</h2><p>{batch.source.repo_id || batch.source.kind || '本机数据'} · {episode?.fps || batch.source.fps || '—'} FPS</p></div><span className="mapping-note">文件时间映射检查不等于硬件同步</span></div>
                {episode ? <>
                  <div className={`camera-grid${episode.cameras.length > 1 ? '' : ' single-camera'}${focusedCamera ? ' focused' : ''}`}>
                    {episode.cameras.map((camera) => {
                      const info = cameraInfo[camera.name];
                      return <div className={`camera-frame${focusedCamera === camera.name ? ' big' : ''}`} key={camera.name} onClick={() => setFocusedCamera((current) => current === camera.name ? null : camera.name)}>
                        {camera.available ? <video ref={(element) => { videoRefs.current[camera.name] = element; }} src={camera.video_url} muted playsInline preload="metadata" onLoadedMetadata={(event) => { event.currentTarget.currentTime = camera.from_timestamp; }} onTimeUpdate={(event) => {
                          if (episode.cameras[0]?.name !== camera.name) return;
                          const frame = Math.round(Math.max(0, event.currentTarget.currentTime - camera.from_timestamp) * episode.fps);
                          setCurrentFrame(Math.min(frame, episode.row_count - 1));
                        }} /> : <div className="video-missing"><Video size={24} /><span>视频缺失</span></div>}
                        <div className="camera-caption"><strong>{camera.name.split('.').at(-1)}</strong><span>{info ? `${info.width} × ${info.height} · ${info.fps?.toFixed(1) || '—'} FPS` : camera.available ? '读取相机信息…' : '文件缺失'}</span></div>
                      </div>;
                    })}
                  </div>
                </> : <div className="loading-state">正在读取 episode 和相机信息…</div>}
              </section>
              {episode && <>
                <section className="panel chart-panel"><div className="panel-heading"><div><h2>状态与动作信号</h2><p>按当前 episode 的样本帧绘制</p></div><span className="count-label">{episode.samples.length.toLocaleString()} 个采样点</span></div>
                  <EChart height={200} option={{ tooltip: { trigger: 'axis' }, legend: { type: 'scroll', bottom: 0 }, grid: { left: 48, right: 18, top: 18, bottom: 48 }, xAxis: { type: 'category', data: episode.samples.map((item) => item.frame_index) }, yAxis: { type: 'value', scale: true }, series: [...episode.state_names.slice(0, 6).map((name, index) => ({ name: `状态 · ${name}`, type: 'line', showSymbol: false, data: episode.samples.map((item) => Array.isArray(item['observation.state']) ? (item['observation.state'] as number[])[index] : null) })), ...episode.action_names.slice(0, 6).map((name, index) => ({ name: `动作 · ${name}`, type: 'line', showSymbol: false, lineStyle: { type: 'dashed' }, data: episode.samples.map((item) => Array.isArray(item.action) ? (item.action as number[])[index] : null) })), { name: '当前帧', type: 'line', data: [], markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: cssToken('--color-focus', '#a85f26'), width: 1.5, type: 'solid' }, data: [{ xAxis: cursorFrame }] } }] }} />
                </section>
                <div className="review-grid">
                  <section className="panel form-panel"><div className="panel-heading"><div><h2>筛选与标注</h2><p>标注会进入候选集版本记录</p></div><FileCheck2 size={18} /></div>
                    <label>筛选决定<select value={disposition} onChange={(event) => setDisposition(event.target.value)}><option value="keep">保留</option><option value="quarantine">隔离</option><option value="exclude">排除</option></select></label>
                    {disposition !== 'keep' && <label>原因<input value={dispositionReason} onChange={(event) => setDispositionReason(event.target.value)} placeholder="说明隔离或排除原因" /></label>}
                    <button className="button secondary" disabled={busy} onClick={() => void saveDisposition()}>保存筛选决定</button>
                    <label>任务描述<input value={annotationTask} onChange={(event) => setAnnotationTask(event.target.value)} placeholder="说明演示任务内容" /></label>
                    <div className="form-row"><label>执行结果<select value={annotationOutcome} onChange={(event) => setAnnotationOutcome(event.target.value)}><option value="success">成功</option><option value="failure">失败</option><option value="uncertain">不确定</option><option value="unlabeled">未标注</option></select></label><label>问题标签<input value={annotationTags} onChange={(event) => setAnnotationTags(event.target.value)} placeholder="用顿号分隔" /></label></div>
                    <label>备注<textarea value={annotationNotes} onChange={(event) => setAnnotationNotes(event.target.value)} rows={2} /></label>
                    <div className="button-row"><button className="button secondary" disabled={busy} onClick={() => void saveAnnotation()}>保存草稿</button><button className="button primary" disabled={busy || !['draft', 'returned'].includes(episode.review.status || '')} onClick={() => void submitAnnotation()}>提交审核</button></div>
                    <p className="form-footnote">表单草稿仅保存在本机，业务状态始终以服务器为准。<button type="button" className="text-link" onClick={restoreFromServer}>恢复服务器内容</button></p>
                    <div className="review-inline"><span>审核状态</span><StatusTag value={episode.review.status} />{episode.review.reason && <small>{episode.review.reason}</small>}</div>
                    {episode.review.status === 'submitted' && <><label>审核意见<input value={reviewReason} onChange={(event) => setReviewReason(event.target.value)} placeholder="退回时必填" /></label><div className="button-row"><button className="button secondary" disabled={busy} onClick={() => void reviewAnnotation(false)}>退回标注</button><button className="button primary" disabled={busy} onClick={() => void reviewAnnotation(true)}>通过审核</button></div></>}
                  </section>
                  <section className="panel quality-panel"><div className="panel-heading"><div><h2>质检记录</h2><p>问题与检查覆盖来自当前批次运行</p></div><StatusTag value={episode.quality.status} /></div>
                    <div className="quality-summary"><strong>{episode.quality.issues?.length || 0}</strong><span>关联问题</span><button className="button secondary" disabled={busy} onClick={startQuality}>运行批次质检</button></div>
                    <IssueList issues={episode.quality.issues || []} />
                    <div className="camera-evidence">{episode.cameras.map((camera) => <div key={camera.name}><span>{camera.name.split('.').at(-1)}</span><StatusTag value={cameraInfo[camera.name] ? 'passed' : camera.available ? 'running' : 'load_failed'} /><small>{cameraInfo[camera.name] ? `${cameraInfo[camera.name].frame_count.toLocaleString()} 帧 · ${cameraInfo[camera.name].mapping_claim}` : '等待视频检查'}</small></div>)}</div>
                    <button className="button subtle" disabled={busy || !batch.episodes.length} onClick={() => void confirmCleaning()}>确认当前筛选候选集</button>
                  </section>
                </div>
              </>}
            </div>
          </>}
        </section>}

        {route === 'runs' && <section className="runs-layout">
          <section className="panel runs-list-panel"><div className="panel-heading"><div><h2>最近运行</h2><p>持久记录的后台操作</p></div><span className="count-label">{runs.length} 条</span></div>
            {runs.length ? runs.slice(0, 30).map((run) => <button key={run.run_id} className={selectedRun?.run_id === run.run_id ? 'run-row selected' : 'run-row'} onClick={() => { setSelectedRun(run); setRunEvents([]); }}>
              <span className="run-type">{run.request?.operation || run.kind || '数据处理'}</span><StatusTag value={run.status} /><small>{run.run_id.slice(-12)}</small><span className="run-message">{run.message || '打开查看步骤与日志'}</span>
            </button>) : <div className="empty-state"><Activity size={24} /><strong>暂无运行记录</strong><span>导入、质检或转换后将在这里保留过程证据。</span></div>}
          </section>
          <section className="panel run-detail-panel"><div className="panel-heading"><div><h2>{selectedRun ? '运行步骤' : '选择一条运行'}</h2><p>{selectedRun?.run_id || '查看节点状态、错误诊断和业务登记结果'}</p></div>{selectedRun && <StatusTag value={selectedRun.status} />}</div>
            {selectedRun ? <><p className="run-status-message">{selectedRun.message || '运行状态持续更新。'}</p>
              {selectedRun.business_registration && <div className="registration-box"><strong>业务登记</strong><StatusTag value={selectedRun.business_registration.status} /><span>{selectedRun.business_registration.message}</span>
                {['pending', 'failed'].includes(String(selectedRun.business_registration.status || '')) &&
                  <button className="button secondary" disabled={busy} onClick={recoverRun}>恢复业务登记</button>}
              </div>}
              <div className="run-nodes">{(selectedRun.nodes || []).map((node, index) => <div className="run-node" key={node.node_id}><span className="node-number">{String(index + 1).padStart(2, '0')}</span><div><strong>{node.label}</strong><small>{node.message || '等待执行'}</small></div><StatusTag value={node.status} /></div>)}</div>
              <div className="event-list"><h3>运行事件</h3>{runEvents.slice(-30).reverse().map((event, index) => <div key={`${String(event.at_utc || '')}-${index}`}><time>{String(event.at_utc || '').replace('T', ' ').slice(0, 19)}</time><span>{String(event.message || event.event || '')}</span></div>)}</div>
            </> : <div className="empty-state"><Activity size={24} /><strong>运行过程会实时更新</strong><span>运行记录会保留独立节点状态、异常信息和业务登记结果。</span></div>}
          </section>
        </section>}

        {route === 'delivery' && <section className="view-stack">
          {!batch ? <div className="panel empty-state large-empty"><Archive size={28} /><strong>请选择一个批次</strong><span>转换和交付依赖当前批次版本与审核证据。</span></div> : <>
            <section className="panel delivery-summary"><div className="panel-heading"><div><h2>{batch.label}</h2><p>内容版本 {batch.content_version} · 输入指纹 {String(batch.source.input_fingerprint || '').slice(0, 16)}…</p></div><StatusTag value={batch.cleaning_confirmed ? 'passed' : 'pending'} /></div>
              <div className="delivery-actions"><button className="button secondary" disabled={busy || !batch.cleaning_confirmed} onClick={() => void start({ operation: 'conversion', batch_id: batch.batch_id, revision: batch.revision })}><RefreshCw size={16} />开始格式转换</button>
                <button className="button secondary" disabled={busy || !batch.outputs?.some((item) => item.current)} onClick={() => {
                  const output = batch.outputs?.find((item) => item.current);
                  if (output) void start({ operation: 'delivery', batch_id: batch.batch_id, revision: batch.revision, output_id: output.output_id });
                }}><Archive size={16} />打包交付</button>
                <button className="button secondary" disabled={busy} onClick={startQuality}>运行质检</button>
              </div>
            </section>
            <div className="delivery-grid"><section className="panel"><div className="panel-heading"><div><h2>已验证输出</h2><p>只有当前批次版本可继续交付</p></div><FileCheck2 size={18} /></div>
                {batch.outputs?.length ? <div className="table-scroll"><table><thead><tr><th>输出编号</th><th>当前版本</th><th>官方加载</th><th>数值一致</th><th>记录数</th></tr></thead><tbody>{batch.outputs.map((output) => <tr key={output.output_id}><td>{output.output_id.slice(-12)}</td><td><StatusTag value={output.current ? 'passed' : 'stale'} /></td><td><StatusTag value={output.official_loader_passed ? 'passed' : 'pending'} /></td><td><StatusTag value={output.numeric_consistency_passed ? 'passed' : 'pending'} /></td><td>{String(output.row_count || '—')}</td></tr>)}</tbody></table></div> : <div className="empty-state compact"><FileCheck2 size={22} /><strong>尚无通过验证的转换产物</strong><span>先完成筛选确认、标注审核和格式转换。</span></div>}
              </section>
              <section className="panel"><div className="panel-heading"><div><h2>交付包与独立验包</h2><p>验证文件清单与官方离线加载结果</p></div><Archive size={18} /></div>
                {batch.deliveries?.length ? <div className="evidence-list">{batch.deliveries.map((item, index) => <div className="evidence-row" key={String(item.delivery_id || index)}><span>{String(item.delivery_id || item.archive_name || `交付包 ${index + 1}`)}</span><StatusTag value={item.status as string} /><small>{String(item.artifact_path || '')}</small></div>)}</div> : <div className="empty-state compact"><Archive size={22} /><strong>还没有交付包</strong><span>从已验证输出创建交付包，再执行独立验包。</span></div>}
                <label className="verify-input">ZIP 路径<input value={verifyPath} onChange={(event) => setVerifyPath(event.target.value)} placeholder="选择或粘贴交付包路径" /></label>
                <button className="button secondary" disabled={busy || !verifyPath.trim()} onClick={() => void start({ operation: 'verify_delivery', archive_path: verifyPath })}>独立验包</button>
              </section></div>
          </>}
        </section>}
        <footer className="page-footer"><span>RoboData Workbench · 本地处理与证据追踪</span><span>原始输入只读 · 失败结果不会登记为可交付</span></footer>
      </main>

      <aside className="inspector">
        <div className="insp-h"><strong>检视面板</strong><span className="insp-chip">上下文证据</span></div>
        <div className="insp-body">
          <div><div className="insp-t">当前帧</div>
            <dl className="insp-kv">
              <dt>Episode</dt><dd>{episode ? `${episodeIndex} / ${episode.row_count.toLocaleString()} 帧` : '未加载'}</dd>
              <dt>帧号</dt><dd>{episode ? currentFrame : '—'}</dd>
              <dt>时间码</dt><dd>{episode ? `t = ${(currentFrame / (episode.fps || 1)).toFixed(2)} s` : '—'}</dd>
              <dt>FPS</dt><dd>{episode?.fps ?? '—'}</dd>
              <dt>回放</dt><dd>{playing ? '四路同步播放中' : '逐帧核对'}</dd>
            </dl>
          </div>
          <div><div className="insp-t">任务列表 · {batch?.episodes.length || 0} 条</div>
            {batch ? batch.episodes.slice(0, 6).map((item) => <div className={`insp-row${episodeIndex === item.episode_index ? ' on' : ''}`} key={item.episode_index}>
              <span>Episode {item.episode_index}</span><span className="spacer" /><StatusTag value={item.quality.status} />
            </div>) : <p className="insp-note">尚未选择批次。</p>}
          </div>
          <div><div className="insp-t">逐路检查 · {episode?.cameras.length || 0} 路</div>
            {episode ? episode.cameras.map((camera) => {
              const info = cameraInfo[camera.name];
              return <div className="insp-row" key={camera.name}><span>{camera.name.split('.').at(-1)}</span><span className="spacer" /><span className="insp-v">{info ? `${Number(info.frame_count || 0).toLocaleString()} 帧` : camera.available ? '检查中' : '文件缺失'}</span></div>;
            }) : <p className="insp-note">打开数据浏览并选择一个批次后显示逐路证据。</p>}
          </div>
          <div><div className="insp-t">质检与候选</div>
            <div className="insp-row"><span>关联问题</span><span className="spacer" /><span className="insp-v">{issueCount}</span></div>
            <div className="insp-row"><span>保留任务</span><span className="spacer" /><span className="insp-v">{keptCount} / {batch?.episodes.length || 0}</span></div>
            <div className="insp-row"><span>当前输出</span><span className="spacer" /><span className="insp-v">{batch?.outputs?.length || 0} 项</span></div>
            <div className="insp-bar"><i style={{ width: batch?.episodes.length ? `${Math.round(keptCount / batch.episodes.length * 100)}%` : '0%' }} /></div>
          </div>
          <div><div className="insp-t">来源锁定</div>
            <dl className="insp-kv">
              <dt>数据集</dt><dd>{batch?.source?.repo_id || batch?.source?.kind || '—'}</dd>
              <dt>批次版本</dt><dd>{batch?.content_version ?? '—'}</dd>
              <dt>revision</dt><dd>{batch?.revision ?? '—'}</dd>
              <dt>输入指纹</dt><dd>{String(batch?.source?.input_fingerprint || '—').slice(0, 16)}</dd>
            </dl>
          </div>
          <p className="insp-note">边界：审核门禁由脚本模拟，不代表人工审核或机器人执行结果；逐路文件时间映射检查不代表相机硬件同步。曲线与相机共用同一游标，不推断控制含义。页面仅监听 127.0.0.1。</p>
        </div>
      </aside>

      <footer className="timeline">
        <button className="tl-btn primary" disabled={!timelineReady} onClick={() => setPlaying(!playing)}>{playing ? '❚❚ 暂停' : '▶ 播放'}</button>
        <button className="tl-btn" disabled={!timelineReady} onClick={() => { setPlaying(false); setCurrentFrame((value) => Math.max(0, value - 1)); }}>◀ 逐帧</button>
        <button className="tl-btn" disabled={!timelineReady} onClick={() => { setPlaying(false); setCurrentFrame((value) => Math.min(lastFrame, value + 1)); }}>逐帧 ▶</button>
        <div className="tl-mid">
          <div className="tl-meta">
            <span>{episode ? `Episode ${episodeIndex} · 帧 0–${episode.row_count.toLocaleString()} · ${episode.fps} FPS` : '打开数据浏览并选择一个批次后可回放'}</span>
            <span>空格播放 · ← → 逐帧 · 1–4 切相机 · Esc 退出放大</span>
          </div>
          <input type="range" min={0} max={lastFrame} step={1} value={timelineReady ? currentFrame : 0} disabled={!timelineReady} aria-label="时间轴帧位置" onChange={(event) => { setPlaying(false); setCurrentFrame(Number(event.target.value)); }} />
          <div className="tl-bookmarks">
            {timelineReady && episode ? [0, 0.25, 0.5, 0.75, 1].map((point) => {
              const frame = Math.round(point * lastFrame);
              return <button type="button" className={currentFrame === frame ? 'on' : ''} key={point} aria-label={`跳到 ${Math.round(point * 100)}% 位置，帧 ${frame}`} onClick={() => { setPlaying(false); setCurrentFrame(frame); }}>{Math.round(point * 100)}%</button>;
            }) : <span className="tl-idle">时间轴待命</span>}
          </div>
        </div>
        <div className="tl-out">{episode ? `t = ${(currentFrame / (episode.fps || 1)).toFixed(2)} s · 帧 ${currentFrame} / ${lastFrame}` : '未加载 episode'}</div>
      </footer>
    </div>
  );
}

function IssueList({ issues }: { issues: Issue[] }) {
  if (!issues.length) return <div className="no-issues"><Check size={16} /><span>当前没有关联问题记录</span></div>;
  return <div className="issue-list">{issues.slice(0, 6).map((issue, index) => <div key={`${issue.rule}-${issue.frame_index}-${index}`}><span className="issue-location">{issue.frame_index == null ? '—' : `帧 ${issue.frame_index}`}</span><strong>{issue.message || issue.rule || '数据问题'}</strong><small>{issue.rule || 'quality'} · {issue.severity || 'info'}</small></div>)}</div>;
}

export default App;
