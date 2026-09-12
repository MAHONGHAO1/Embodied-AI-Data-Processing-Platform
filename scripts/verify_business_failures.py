"""Actual fault-copy integration checks, using the same shared runtime and store."""
import json
from pathlib import Path
import sys
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')
from robodata import business
from robodata.config import PROJECT_ROOT
from robodata.runtime import wait_run, read_events
from robodata.importing import preview_batch
from robodata_business import WorkflowError


def run(name,fn,expected='completed'):
    rid=fn(); print(name,rid,flush=True)
    state=wait_run(rid,timeout=1000)
    assert state['status']==expected,(name,state['message'])
    results['runs'][name]=rid
    return state


results={'runs':{},'checks':{}}
store=business.get_store()


def main():
    dirty=PROJECT_ROOT/'work/hdf5-failure-demo/isolated.hdf5'
    imported=run('fault_import',lambda:business.start_import(dirty,'hdf5','故障演示：NaN、缺字段与正常任务','V0.4 isolated 人为故障副本'))
    bid=imported['batch_id']; b=store.get_batch(bid)
    checked=run('fault_quality',lambda:business.start_batch_quality(bid,b['revision']))
    first_digest=checked['result_digest']; b=store.get_batch(bid)
    assert [e['disposition'] for e in b['episodes']]==['quarantine','quarantine','keep']
    assert any(i['row_index']==10 and i['field']=='actions' for i in b['episodes'][0]['quality']['issues'])
    assert any(i['rule']=='required_fields' for i in b['episodes'][1]['quality']['issues'])
    assert preview_batch(b,2,56)['image'].shape==(84,84,3)
    try: store.set_disposition(bid,0,'keep',expected_revision=b['revision'])
    except WorkflowError: pass
    else: raise AssertionError('异常任务被允许保留')
    second=run('repeat_fault_quality',lambda:business.start_batch_quality(bid,b['revision']))
    assert second['run_id']!=checked['run_id'] and second['result_digest']==first_digest
    assert any(e.get('episode_index')==0 and e.get('row_index')==10 for e in read_events(second['run_id']))
    b=store.get_batch(bid); b=store.confirm_cleaning(bid,expected_revision=b['revision'])
    b=store.save_annotation(bid,2,task='Lift',outcome='uncertain',notes='故障副本中未修改的任务；演示隔离后继续转换',expected_revision=b['revision'])
    b=store.submit_annotation(bid,2,expected_revision=b['revision'])
    b=store.review_annotation(bid,2,approve=True,expected_revision=b['revision'])
    converted=run('clean_task_conversion',lambda:business.start_batch_conversion(bid,b['revision']))
    assert converted['summary']['row_count']==57 and converted['summary']['coverage_performed']==11
    results['checks']['fault_isolation']={'batch_id':bid,'quarantine':[0,1],'kept':[2],'nan_row':10,
        'clean_conversion_frames':57,'repeat_result_digest_equal':True,'summary':checked['summary']}
    for name,path,counts in [('so100_anomalies','failure_demo',(7,0,0)),('so100_missing_video','missing_video_demo',(3,1,2))]:
        imported=run(name+'_import',lambda:business.start_import(PROJECT_ROOT/'work'/path,'so100',name+' 人为故障验收',name+' injected copy'))
        batch=store.get_batch(imported['batch_id'])
        state=run(name+'_quality',lambda:business.start_batch_quality(batch['batch_id'],batch['revision']))
        summary=state['summary']
        assert (summary['issue_count'],summary['load_failure_count'],summary['not_checked_count'])==counts,summary
        fresh=store.get_batch(batch['batch_id'])
        assert fresh['counts']['quarantine']['episodes']==1 and fresh['counts']['keep']['episodes']==4
        results['checks'][name]={'batch_id':batch['batch_id'],'summary':summary}
    accepted=json.loads((PROJECT_ROOT/'work/v04-acceptance.json').read_text(encoding='utf-8'))
    source=Path(accepted['delivery']['path'])
    target=PROJECT_ROOT/'work/hdf5-failure-demo/tampered-delivery.zip'
    with zipfile.ZipFile(source) as src,zipfile.ZipFile(target,'w') as out:
        for name in src.namelist():
            content=src.read(name)
            if name=='dataset/meta/info.json': content+=b' '
            out.writestr(name,content)
    failed=run('tampered_zip',lambda:business.start_verify_delivery(target),expected='failed')
    assert next(n for n in failed['nodes'] if n['id']=='verify')['status']=='failed'
    results['checks']['zip_tamper_rejected']=True
    results['status']='passed'
    path=PROJECT_ROOT/'work/v04-failure-acceptance.json'
    path.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS',path)


if __name__=='__main__': main()
