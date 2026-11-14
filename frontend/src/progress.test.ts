import { describe, expect, it } from 'vitest';
import { deriveStages } from './progress';
import type { Batch, RunRecord } from './types';

const id = 'batch_' + 'a'.repeat(32);

function episode(review: string, quality = 'passed', disposition = 'keep') {
  return { episode_index: 0, row_count: 120, disposition,
    quality: { status: quality }, annotation: { task: '拾取', outcome: 'success', tags: [], notes: '' },
    review: { status: review } };
}

function batch(overrides: Partial<Batch> = {}): Batch {
  return { batch_id: id, label: '审计批次', revision: 8, content_version: 4, source: {},
    episodes: [episode('submitted')], cleaning_confirmed: true, outputs: [], deliveries: [],
    ...overrides } as Batch;
}

const byName = (value: Batch | null, name: string, runs: RunRecord[] = []) =>
  deriveStages(value, runs).find((item) => item.name === name)!;

describe('阶段进度必须由业务快照推导', () => {
  it('空批次只把导入标为进行中，其余为待处理', () => {
    const stages = deriveStages(null);
    expect(stages[0].state).toBe('active');
    expect(stages.slice(1).every((item) => item.state === 'pending')).toBe(true);
  });

  it('已提交审核会反映在标注审核阶段，而不是永远显示「待处理」', () => {
    expect(byName(batch(), '标注审核').state).toBe('active');
    expect(byName(batch(), '标注审核').detail).toContain('待审核');
    expect(byName(batch(), '清洗筛选').state).toBe('done');
    expect(byName(batch(), '质量检查').state).toBe('done');
    expect(byName(batch(), '导入').state).toBe('done');
  });

  it('审核通过后标注审核阶段才变为已完成', () => {
    expect(byName(batch({ episodes: [episode('approved')] }), '标注审核').state).toBe('done');
    expect(byName(batch({ episodes: [episode('approved')] }), '标注审核').detail).toContain('1/1');
  });

  it('只要有当前输出就把后续三个阶段一次性全标完成的旧逻辑已被移除', () => {
    const value = batch({ outputs: [{ output_id: 'o', current: true }] });
    expect(byName(value, '格式转换').state).toBe('done');
    expect(byName(value, '打包交付').state).toBe('pending');
    expect(byName(value, '独立验包').state).toBe('pending');
  });

  it('过期输出不能显示为已完成', () => {
    const value = batch({ outputs: [{ output_id: 'o', current: false }] });
    expect(byName(value, '格式转换').state).toBe('active');
    expect(byName(value, '格式转换').detail).toContain('过期');
  });

  it('独立验包只认 verify_delivery 运行；仅打包交付不会误标完成', () => {
    const value = batch({ deliveries: [
      { delivery_id: 'd', current: true, zip_sha256: 'f'.repeat(64) },
    ] });
    expect(byName(value, '打包交付').state).toBe('done');
    expect(byName(value, '独立验包').state).toBe('active'); // 下一步待执行，不是已完成
    const runs = [{ run_id: 'v1', status: 'completed',
      request: { operation: 'verify_delivery' } }] as RunRecord[];
    expect(byName(value, '独立验包', runs).state).toBe('done');
  });

  it('未确认清洗时不会把标注审核误标为已完成', () => {
    const value = batch({ cleaning_confirmed: false });
    expect(byName(value, '清洗筛选').state).toBe('active');
  });
});
