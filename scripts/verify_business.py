"""Run the real local V0.4 workflow; persist checkpoints for interrupted sessions.

Uses the fixed public source. Review decisions are explicitly acceptance fixtures,
not a claim about robot success rates. Existing raw files are never modified.
"""
import json
from pathlib import Path
import re
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
import numpy as np
from robodata import business
from robodata.config import PROJECT_ROOT, APP_VERSION
from robodata.sources import HDF5_PATH, HDF5_SHA256
from robodata.source import file_sha256
from robodata.importing import preview_batch
from robodata.runtime import wait_run, get_run
from robodata.delivery import verified_delivery
from robodata.dataset import load_episode, load_metadata
from robodata.video import read_video_frame

EVIDENCE = PROJECT_ROOT/'work/v04-acceptance.json'
store = business.get_store()
record = json.loads(EVIDENCE.read_text(encoding='utf-8')) if EVIDENCE.is_file() else {'runs':{},'checks':{}}


def save():
    EVIDENCE.parent.mkdir(parents=True,exist_ok=True)
    EVIDENCE.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')


def run(name, start, expected='completed'):
    prior=record['runs'].get(name)
    if prior:
        state=get_run(prior)
        if state['status']==expected: return state
    run_id=start()
    record['runs'][name]=run_id; save()
    print(name,run_id,flush=True)
    state=wait_run(run_id,timeout=1000)
    assert state['status']==expected, (name,state['status'],state['message'])
    save()
    return state


def current(): return store.get_batch(record['batch_id'])


def main():
    assert file_sha256(HDF5_PATH)==HDF5_SHA256
    imported=run('import',lambda:business.start_import(HDF5_PATH,'hdf5','Panda／Lift V0.4 真实闭环验收'))
    record['batch_id']=imported['batch_id']; save()
    batch=current()
    assert batch['total_episodes']==3 and batch['total_rows']==174
    assert batch['source']['source_statistics']=={'declared_total':9666,'actual_total':531,'demo_count':10}
    checks=[]
    for ep in batch['episodes']:
        for row in (0,ep['row_count']//2,ep['row_count']-1):
            preview=preview_batch(batch,ep['episode_index'],row)
            assert preview['image'].shape==(84,84,3)
            assert int(preview['table'].iloc[row]['frame_index'])==row
            assert len(preview['state_names'])==9 and len(preview['action_names'])==7
            checks.append({'episode':ep['episode_index'],'row':row,'image_shape':[84,84,3]})
    record['checks']['raw_preview']=checks
    quality=run('quality',lambda:business.start_batch_quality(batch['batch_id'],current()['revision']))
    assert quality['summary']['coverage_performed']==30 and quality['summary']['issue_count']==0
    record['checks']['input_quality']=quality['summary']
    if not record.get('review_completed'):
        b=current(); b=store.set_disposition(b['batch_id'],1,'exclude',reason='验收非连续选择 0、2；数据本身通过质检',expected_revision=b['revision'])
        b=store.confirm_cleaning(b['batch_id'],expected_revision=b['revision'])
        for i,outcome in [(0,'failure'),(2,'uncertain')]:
            b=store.save_annotation(b['batch_id'],i,task='Lift：提起方块',outcome=outcome,tags=['验收标注'],
                notes='人工流程测试标签，不作为源机器人成功率结论；原始任务为 Lift。',expected_revision=b['revision'])
            b=store.submit_annotation(b['batch_id'],i,expected_revision=b['revision'])
            if i==0:
                b=store.review_annotation(b['batch_id'],i,approve=False,reason='补充该执行结果是验收标签的说明',expected_revision=b['revision'])
                assert b['episodes'][i]['review']['status']=='returned'
                b=store.save_annotation(b['batch_id'],i,task='Lift：提起方块',outcome=outcome,tags=['验收标注'],
                    notes='流程验收人为标注为失败，证明有效失败示范可保留；不宣称公开源任务实际失败。',expected_revision=b['revision'])
                b=store.submit_annotation(b['batch_id'],i,expected_revision=b['revision'])
            b=store.review_annotation(b['batch_id'],i,approve=True,expected_revision=b['revision'])
        record['review_completed']=True; save()
    b=current(); spec=store.conversion_spec(b['batch_id'])
    assert [e['source_episode_index'] for e in spec['episodes']]==[0,2]
    record['checks']['selection']={'source':[0,2],'output':[0,1],'frames':116,'failure_label_retained':True}
    converted=run('conversion',lambda:business.start_batch_conversion(b['batch_id'],current()['revision']))
    assert converted['summary']['coverage_performed']==22 and converted['summary']['row_count']==116
    b=current(); output=next(o for o in b['outputs'] if o['run_id']==converted['run_id'])
    record['output_id']=output['output_id']; record['output_path']=output['artifact_path']
    conversion_dir=PROJECT_ROOT/'work/runs'/converted['run_id']
    official=json.loads((conversion_dir/'steps/official_verify.json').read_text(encoding='utf-8'))
    assert official['details']['checked_numeric_frames']==116 and official['details']['sample_count']==6
    root=Path(output['artifact_path']); metadata=load_metadata(root)
    assert metadata['episode_indices']==[0,1]
    assert metadata['info']['splits']=={'train':'0:2'}
    decoded=np.load(conversion_dir/'decoded_samples.npz')
    matching=[]
    for ix,(episode,row) in enumerate(zip(decoded['episode_indices'],decoded['frame_indices'])):
        ep=load_episode(root,int(episode))
        frame=read_video_frame(ep.video_path,float(ep.table.iloc[int(row)]['timestamp'])+ep.video_start_time)
        np.testing.assert_array_equal(frame['image'],decoded['images'][ix])
        matching.append({'episode':int(episode),'row':int(row),'video_start_time':ep.video_start_time,'official_pixels_equal':True})
    record['checks']['output_frames']=matching
    assert any(c['episode']==1 and c['video_start_time']==2.95 for c in matching)
    failure_target=PROJECT_ROOT/'work/v04-write-failure-target'; failure_target.mkdir(exist_ok=True)
    failed=run('zip_write_failure',lambda:business.start_delivery(b['batch_id'],output['output_id'],current()['revision'],archive_output=str(failure_target)),expected='failed')
    assert next(n for n in failed['nodes'] if n['id']=='package')['status']=='failed'
    delivered=run('delivery',lambda:business.start_delivery(b['batch_id'],output['output_id'],current()['revision']))
    b=current(); delivery=next(d for d in b['deliveries'] if d['packaging_run_id']==delivered['run_id'])
    verified=verified_delivery(b,delivery['delivery_id']); z=Path(verified['path'])
    assert verified['row_count']==116 and verified['episode_count']==2
    with zipfile.ZipFile(z) as archive:
        for name in archive.namelist():
            assert not name.startswith(('/', '\\')) and '..' not in Path(name).parts
            if name.endswith(('.json','.txt','.md')):
                text=archive.read(name).decode('utf-8')
                assert not re.search(r'\b[A-Za-z]:[\\/]',text), (name,'local absolute path')
                assert '.venv' not in name and '.env' not in name
    verify_again=run('independent_verify',lambda:business.start_verify_delivery(z))
    assert verify_again['status']=='completed'
    before_revision=b['revision']; before_count=len(b['deliveries'])
    business.reconcile_run(delivered['run_id'])
    again=current()
    assert again['revision']==before_revision and len(again['deliveries'])==before_count
    duplicate=run('duplicate_import',lambda:business.start_import(HDF5_PATH,'hdf5','重复输入验收'))
    assert duplicate['batch_id']==b['batch_id'] and current()['revision']==before_revision
    record['delivery_id']=delivery['delivery_id']; record['delivery']=verified
    record['checks'].update(callback_idempotent=True,duplicate_reuses_batch=True,zip_portable=True,zip_write_failure='package node',
                            independent_official_load=True,raw_unchanged=file_sha256(HDF5_PATH)==HDF5_SHA256)
    record.update(status='passed',app_version=APP_VERSION,source_sha256=HDF5_SHA256)
    save(); print('PASS',EVIDENCE,flush=True)


if __name__=='__main__': main()
