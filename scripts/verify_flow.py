"""Eight-node acceptance against real pinned data and the supervised runtime."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')
from robodata import business
from robodata.config import PROJECT_ROOT
from robodata.sources import HDF5_PATH,HDF5_SHA256
from robodata.source import file_sha256
from robodata.runtime import wait_run,read_events
from robodata.delivery import verified_delivery


def main():
    record={'runs':{},'source_sha256':HDF5_SHA256}
    store=business.get_store()
    def wait(name,rid):
        print(name,rid,flush=True); record['runs'][name]=rid
        state=wait_run(rid,timeout=1200)
        assert state['status']=='completed',(name,state['message'])
        return state
    imported=wait('import',business.start_import(HDF5_PATH,'hdf5','八节点工作流验收'))
    bid=imported['batch_id']; record['batch_id']=bid
    b=store.get_batch(bid)
    # Explicit re-cleaning is a new attempt; earlier delivered evidence stays historical.
    cleaned=wait('clean',business.start_batch_quality(bid,b['revision']))
    b=store.get_batch(bid)
    assert cleaned['summary']['coverage_performed']==30 and cleaned['summary']['issue_count']==0
    b=store.set_disposition(bid,1,'exclude',reason='八节点验收选择源任务 0、2；数据质量本身通过',expected_revision=b['revision'])
    b=store.confirm_cleaning(bid,expected_revision=b['revision'])
    for i in (0,2):
        b=store.save_annotation(bid,i,task='Lift：提起方块',outcome='uncertain',tags=['八节点验收'],
            notes='公开仿真测试片段，人工标注为不确定，不据此推断机器人成功率',expected_revision=b['revision'])
        b=store.submit_annotation(bid,i,expected_revision=b['revision'])
        if i==0:
            b=store.review_annotation(bid,i,approve=False,reason='补充任务动作描述后重提',expected_revision=b['revision'])
            b=store.save_annotation(bid,i,task='Lift：夹取并提起方块',outcome='uncertain',tags=['八节点验收'],
                notes='退回后已补充描述；执行结果保留不确定',expected_revision=b['revision'])
            b=store.submit_annotation(bid,i,expected_revision=b['revision'])
        b=store.review_annotation(bid,i,approve=True,expected_revision=b['revision'])
    auto=wait('quality_convert_delivery',business.start_final_quality(bid,b['revision'],auto_continue=True))
    assert auto.get('outcome')!='rejected'
    b=store.get_batch(bid)
    output=next(o for o in b['outputs'] if o['run_id']==auto['run_id'])
    delivery=next(d for d in b['deliveries'] if d['packaging_run_id']==auto['run_id'])
    value=verified_delivery(b,delivery['delivery_id'])
    assert value['row_count']==116 and value['episode_count']==2
    final=PROJECT_ROOT/'work/runs'/auto['run_id']/'reports/final_quality.json'
    report=json.loads(final.read_text(encoding='utf-8'))
    assert report['outcome']=='passed',report
    from robodata.workflow import get_flow
    flow=get_flow(b)
    record.update(status='passed',content_version=b['content_version'],delivery=value,
                  output_id=output['output_id'],delivery_id=delivery['delivery_id'],final_quality=report,
                  workflow=flow,raw_unchanged=file_sha256(HDF5_PATH)==HDF5_SHA256)
    target=PROJECT_ROOT/'work/eight-node-acceptance.json'
    target.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print('PASS',target,flush=True)


if __name__=='__main__': main()
