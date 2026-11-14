import type { Batch, RunRecord } from './types';

/** 阶段进度必须由后端业务快照推导，不能用「有没有输出」硬编码推断全流程。 */
export type StageState = 'done' | 'active' | 'pending';

export interface StageView {
  name: string;
  state: StageState;
  detail: string;
}

const STAGE_NAMES = ['导入', '清洗筛选', '标注审核', '质量检查', '格式转换', '打包交付', '独立验包'] as const;

function stage(name: string, state: StageState, detail: string): StageView {
  return { name, state, detail };
}

export function deriveStages(batch: Batch | null, runs: RunRecord[] = []): StageView[] {
  if (!batch) {
    return STAGE_NAMES.map((name, index) =>
      stage(name, index === 0 ? 'active' : 'pending', index === 0 ? '尚未导入批次' : '等待批次'));
  }

  const episodes = batch.episodes ?? [];
  const kept = episodes.filter((item) => item.disposition === 'keep');
  const qualityPassed = kept.filter((item) => item.quality?.status === 'passed').length;
  const qualityChecked = episodes.filter((item) => Boolean(item.quality?.status)
    && item.quality?.status !== 'not_checked').length;
  const approved = kept.filter((item) => item.review?.status === 'approved').length;
  const submitted = kept.filter((item) => item.review?.status === 'submitted').length;
  const outputs = batch.outputs ?? [];
  const deliveries = batch.deliveries ?? [];
  const currentOutputs = outputs.filter((item) => item.current === true);
  const currentDeliveries = deliveries.filter((item) => item.current === true);
  // zip_sha256 是「打包交付」注册时写入的，不能拿来证明独立验包已完成。
  // 独立验包只认运行记录里成功完成的 verify_delivery 运行。
  const verifyRuns = runs.filter((run) => run.request?.operation === 'verify_delivery'
    && run.status === 'completed');

  return [
    stage('导入', episodes.length ? 'done' : 'active',
      episodes.length ? `${episodes.length} 个任务已登记` : '批次尚无任务'),
    stage('清洗筛选', batch.cleaning_confirmed === true ? 'done' : kept.length ? 'active' : 'pending',
      batch.cleaning_confirmed === true ? `已确认 ${kept.length} 个保留任务`
        : kept.length ? `${kept.length} 个候选待确认` : '待筛选'),
    stage('标注审核', kept.length > 0 && approved === kept.length ? 'done'
      : submitted > 0 || approved > 0 || kept.length > 0 ? 'active' : 'pending',
      `${approved}/${kept.length} 已通过${submitted ? ` · ${submitted} 待审核` : ''}`),
    stage('质量检查', kept.length > 0 && qualityPassed === kept.length ? 'done'
      : qualityChecked > 0 ? 'active' : 'pending',
      `${qualityPassed}/${kept.length} 个保留任务通过`),
    stage('格式转换', currentOutputs.length ? 'done' : outputs.length ? 'active' : 'pending',
      currentOutputs.length ? `${currentOutputs.length} 个当前输出`
        : outputs.length ? '输出已过期，需重新转换' : '尚无转换产物'),
    stage('打包交付', currentDeliveries.length ? 'done' : deliveries.length ? 'active' : 'pending',
      currentDeliveries.length ? `${currentDeliveries.length} 个当前交付包`
        : deliveries.length ? '交付包已过期' : '尚无交付包'),
    stage('独立验包', currentDeliveries.length > 0 && verifyRuns.length > 0 ? 'done'
      : currentDeliveries.length ? 'active' : 'pending',
      verifyRuns.length ? `${verifyRuns.length} 次独立验包运行完成` : '等待独立验包'),
  ];
}
