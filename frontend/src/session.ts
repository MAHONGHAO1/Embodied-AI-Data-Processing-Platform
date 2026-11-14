import type { RouteKey } from './types';

/**
 * 本机工作台的「可恢复上下文」。
 *
 * 后端业务状态本来就是磁盘原子快照，但 route / 当前批次 / episode / 未提交的表单草稿
 * 原先只存在 React 内存里：标签页被浏览器回收、刷新、或长时间后台后回到页面，
 * 就会全部丢失并呈现「回到导入阶段」。这里把易失上下文落到 localStorage，
 * 只记录界面上下文，不记录任何业务结论。业务真相始终以 /api/batches 为准。
 */
export interface DraftValues {
  task: string;
  outcome: string;
  tags: string;
  notes: string;
  disposition: string;
  dispositionReason: string;
  reviewReason: string;
  /** 草稿绑定的批次内容版本；后台质检/筛选变更会让旧草稿自动失效。 */
  contentVersion: number;
}

export interface UiSession {
  version: 1;
  savedAt: number;
  route?: RouteKey;
  batchId?: string;
  episodeIndex?: number;
  currentFrame?: number;
  selectedRunId?: string;
  drafts: Record<string, DraftValues>;
}

const KEY = 'robodata.workbench.session.v1';
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

function blank(): UiSession {
  return { version: 1, savedAt: Date.now(), drafts: {} };
}

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function loadSession(): UiSession {
  const store = storage();
  if (!store) return blank();
  try {
    const raw = store.getItem(KEY);
    if (!raw) return blank();
    const parsed = JSON.parse(raw) as Partial<UiSession>;
    if (parsed?.version !== 1 || typeof parsed.savedAt !== 'number'
        || Date.now() - parsed.savedAt > MAX_AGE_MS) return blank();
    return { ...blank(), ...parsed, version: 1,
      drafts: parsed.drafts && typeof parsed.drafts === 'object' ? parsed.drafts : {} };
  } catch {
    return blank();
  }
}

export function saveSession(patch: Partial<Omit<UiSession, 'version' | 'savedAt'>>): void {
  const store = storage();
  if (!store) return;
  const current = loadSession();
  const next: UiSession = {
    ...current, ...patch, version: 1, savedAt: Date.now(),
    drafts: { ...current.drafts, ...(patch.drafts ?? {}) },
  };
  try { store.setItem(KEY, JSON.stringify(next)); } catch { /* 配额或隐私模式：忽略即可 */ }
}

export function draftKey(batchId: string, episodeIndex: number): string {
  return `${batchId}:${episodeIndex}`;
}

export function getDraft(batchId: string, episodeIndex: number, contentVersion: number): DraftValues | undefined {
  const key = draftKey(batchId, episodeIndex);
  const draft = loadSession().drafts[key];
  if (!draft) return undefined;
  // 内容版本变化意味着质检/筛选/审核已被后台重跑，旧草稿必须丢弃，
  // 否则会用旧表单值覆盖服务器的新业务结论。
  if (draft.contentVersion !== contentVersion) {
    clearDraft(batchId, episodeIndex);
    return undefined;
  }
  return draft;
}

export function clearDraft(batchId: string, episodeIndex: number): void {
  const store = storage();
  if (!store) return;
  const current = loadSession();
  const drafts = { ...current.drafts };
  delete drafts[draftKey(batchId, episodeIndex)];
  try {
    store.setItem(KEY, JSON.stringify({ ...current, drafts, savedAt: Date.now() }));
  } catch { /* 同上 */ }
}

export function clearSession(): void {
  try { storage()?.removeItem(KEY); } catch { /* 同上 */ }
}
