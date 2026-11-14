import { beforeEach, describe, expect, it } from 'vitest';
import { clearDraft, clearSession, draftKey, getDraft, loadSession, saveSession } from './session';

const KEY = 'robodata.workbench.session.v1';

function draft(task: string, contentVersion = 3) {
  return { task, outcome: 'success', tags: '验收', notes: '',
    disposition: 'keep', dispositionReason: '', reviewReason: '', contentVersion };
}

beforeEach(() => {
  clearSession();
  window.localStorage.clear();
});

describe('工作台可恢复上下文', () => {
  it('保存后可完整恢复 route / 批次 / episode / 帧 / 运行', () => {
    saveSession({ route: 'episodes', batchId: 'batch_x', episodeIndex: 3,
      currentFrame: 412, selectedRunId: 'run_y' });
    const value = loadSession();
    expect(value.route).toBe('episodes');
    expect(value.batchId).toBe('batch_x');
    expect(value.episodeIndex).toBe(3);
    expect(value.currentFrame).toBe(412);
    expect(value.selectedRunId).toBe('run_y');
  });

  it('多次部分更新不会丢掉先前的字段', () => {
    saveSession({ batchId: 'batch_x' });
    saveSession({ episodeIndex: 2 });
    expect(loadSession().batchId).toBe('batch_x');
    expect(loadSession().episodeIndex).toBe(2);
  });

  it('超过 7 天或版本不符的会话被视为失效', () => {
    window.localStorage.setItem(KEY, JSON.stringify({
      version: 1, savedAt: Date.now() - 8 * 24 * 3600 * 1000,
      batchId: 'stale', drafts: {},
    }));
    expect(loadSession().batchId).toBeUndefined();
    window.localStorage.setItem(KEY, JSON.stringify({ version: 99, savedAt: Date.now(), batchId: 'x' }));
    expect(loadSession().batchId).toBeUndefined();
  });

  it('损坏的 JSON 不会让页面白屏', () => {
    window.localStorage.setItem(KEY, '{broken');
    expect(loadSession().drafts).toEqual({});
  });

  it('草稿按 批次:任务 隔离，清除只影响目标键', () => {
    saveSession({ drafts: { [draftKey('batch_a', 0)]: draft('第一'),
      [draftKey('batch_a', 1)]: draft('第二') } });
    expect(getDraft('batch_a', 0, 3)?.task).toBe('第一');
    clearDraft('batch_a', 0);
    expect(getDraft('batch_a', 0, 3)).toBeUndefined();
    expect(getDraft('batch_a', 1, 3)?.task).toBe('第二');
  });

  it('内容版本变化后旧草稿自动失效，不得覆盖服务器新结论', () => {
    saveSession({ drafts: { [draftKey('batch_a', 0)]: draft('旧标注', 3) } });
    expect(getDraft('batch_a', 0, 3)?.task).toBe('旧标注');
    expect(getDraft('batch_a', 0, 4)).toBeUndefined();
    expect(getDraft('batch_a', 0, 3)).toBeUndefined();
  });
});
