"""Copy pinned local inputs, extract a read-only preview, and bind real checks."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from .config import HDF5_EPISODES, PROJECT_ROOT
from .sources import HDF5_SHA256, HDF5_SIZE, HDF5_SOURCE
from .source import SAMPLE_FILES, MANIFEST_NAME, DEMO_MANIFEST_NAME, file_sha256

HDF5_RULES = ('required_fields','row_count','vector_shape','numeric_finite','frame_index',
               'timestamp_monotonic','timestamp_interval','image_shape','image_frame_count','image_read')
PROFILE = {'profile_version':'0.4.0','hdf5_episodes':list(HDF5_EPISODES), 'state_fields':
           ['obs/robot0_eef_pos','obs/robot0_eef_quat','obs/robot0_gripper_qpos'],
           'action':'actions','image':'obs/agentview_image','fps':20,'timestamp':'derived_frame_index/fps'}

def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def _write(path,value):
    from .runtime import _atomic_json
    _atomic_json(Path(path),value)

def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def save_upload(data: bytes, filename: str) -> Path:
    if Path(filename).suffix.lower() not in {'.hdf5','.h5'}:
        raise ValueError('当前上传入口只接受已验证配置的 HDF5 文件')
    root = Path(os.environ.get('ROBODATA_BUSINESS_ROOT',PROJECT_ROOT/'work/business'))/'uploads'/uuid.uuid4().hex
    root.mkdir(parents=True)
    path = root/'uploaded.hdf5'
    path.write_bytes(data)
    return path

def _files(root: Path, names) -> list[dict]:
    records=[]
    for name in sorted(set(names)):
        path=root/name
        records.append({'path':name,'exists':path.is_file(),
                        **({'size':path.stat().st_size,'sha256':file_sha256(path)} if path.is_file() else {})})
    return records

def input_fingerprint(source: dict) -> str:
    manifest=_json(source['manifest_path'])
    raw=Path(source['managed_path'])/'source'
    actual=_files(raw,[item['path'] for item in manifest['files']])
    return _digest({'files':actual,'interpretation':manifest['interpretation']})

def assert_input_unchanged(source: dict):
    if input_fingerprint(source)!=source['input_fingerprint']:
        raise ValueError('批次管理副本的输入文件已变化，请重新导入；当前检查和产物不可继续使用')

def verify_managed_source(source: dict) -> str:
    assert_input_unchanged(source)
    return source['input_fingerprint']

def _preview_worker(raw: Path, output: Path, label=None):
    from .conversion import CONVERSION_PYTHON
    cmd=[str(CONVERSION_PYTHON),str(PROJECT_ROOT/'tools/conversion/preview.py'),
         '--input',str(raw),'--output',str(output)]
    if label: cmd.extend(['--test-mode',label])
    env=os.environ.copy()
    env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',HF_HUB_OFFLINE='1',HF_DATASETS_OFFLINE='1')
    # This helper is a descendant of the supervised business worker and shares its
    # Windows job; timeout/parent death terminates both, without a second scheduler.
    process=subprocess.run(cmd,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace')
    (output.parent/(output.name+'.stdout.log')).write_text(process.stdout,encoding='utf-8')
    (output.parent/(output.name+'.stderr.log')).write_text(process.stderr,encoding='utf-8')
    if process.returncode:
        diagnostic=output/'diagnostic.json'
        details=_json(diagnostic) if diagnostic.is_file() else {}
        error=ValueError(details.get('message_zh') or details.get('message') or '原始 HDF5 预览解析失败，请查看独立解析日志')
        error.details=details
        raise error

def _copy(request,run):
    kind=request['source_kind']
    if kind not in {'hdf5','so100'}: raise ValueError('只支持固定 HDF5 或 SO-100 目录配置')
    source=Path(request['input_path']).resolve()
    managed=Path(request['managed_dir']).resolve()
    raw=managed/'source'
    if raw.exists(): raise ValueError('本次导入暂存目录已存在，不能覆盖既有数据')
    if source==managed or managed.is_relative_to(source):
        raise ValueError('导入目标不能位于输入目录内')
    raw.mkdir(parents=True)
    label=request.get('test_label')
    if kind=='hdf5':
        if not source.is_file(): raise FileNotFoundError('找不到待导入的 HDF5 文件')
        before=file_sha256(source)
        if not label and (before!=HDF5_SHA256 or source.stat().st_size!=HDF5_SIZE):
            raise ValueError('HDF5 与固定公开版本不一致；若为故障副本，必须填写独立测试标记')
        shutil.copy2(source,raw/'test.hdf5')
        if before!=file_sha256(raw/'test.hdf5') or before!=file_sha256(source):
            raise ValueError('复制期间 HDF5 输入发生变化，未登记批次')
        names=['test.hdf5']
    else:
        if not source.is_dir(): raise NotADirectoryError('SO-100 导入入口需要本地数据目录')
        names=[*SAMPLE_FILES,MANIFEST_NAME]
        if (source/DEMO_MANIFEST_NAME).is_file():
            names.append(DEMO_MANIFEST_NAME)
            if not label: raise ValueError('检测到故障副本清单，请填写测试标记再导入')
        pinned=_json(Path(__file__).parent/'profiles/so100_hashes.json')
        for name in names:
            file=source/name
            if not file.is_file():
                if label: continue
                raise FileNotFoundError(f'固定 SO-100 样本缺少文件：{name}')
            before=file_sha256(file)
            if not label and name in pinned['files'] and before!=pinned['files'][name]:
                raise ValueError(f'SO-100 文件与固定来源不一致：{name}')
            destination=raw/name
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(file,destination)
            if file_sha256(destination)!=before or file_sha256(file)!=before:
                raise ValueError(f'复制期间输入发生变化：{name}')
    files=_files(raw,names)
    # 批次名参与指纹：同名视为同一批次（复用既有筛选／审核／交付记录），
    # 异名视为用户要建立独立批次，便于演示或对照实验时从零跑一遍，
    # 而不必删除或改动既有批次。文件内容与来源配置仍是硬校验。
    interpretation={**PROFILE,'kind':kind,'test_label':label,
                    'batch_label':str(request.get('label') or '').strip()}
    fingerprint=_digest({'files':files,'interpretation':interpretation})
    _write(managed/'input_manifest.json',{'schema_version':1,'files':files,'interpretation':interpretation,
                                        'input_fingerprint':fingerprint})
    return {'managed_path':str(managed),'input_fingerprint':fingerprint,'manifest_path':str(managed/'input_manifest.json')}

def _preview(request,run):
    managed=Path(request['managed_dir'])
    if request['source_kind']=='hdf5':
        _preview_worker(managed/'source/test.hdf5',managed/'preview',request.get('test_label'))
        files=[p.relative_to(managed/'preview').as_posix() for p in (managed/'preview').rglob('*') if p.is_file()]
        _write(managed/'preview_manifest.json',{'files':_files(managed/'preview',files)})
        return {'preview_root':str(managed/'preview'),'metadata_path':str(managed/'preview/metadata.json')}
    from .dataset import load_metadata
    meta=load_metadata(managed/'source')
    return {'data_root':str(managed/'source'),'episode_indices':meta['episode_indices']}

def _inventory(request,run):
    managed=Path(request['managed_dir'])
    manifest=_json(managed/'input_manifest.json')
    source={'kind':request['source_kind'],'managed_path':str(managed),
            'input_fingerprint':manifest['input_fingerprint'],'manifest_path':str(managed/'input_manifest.json'),
            'import_run_id':run.name,'test_label':request.get('test_label')}
    if request['source_kind']=='hdf5':
        metadata=_json(managed/'preview/metadata.json')
        source.update(**{k:v for k,v in HDF5_SOURCE.items() if k!='format_version'},
                      hdf5_path=str(managed/'source/test.hdf5'),preview_root=str(managed/'preview'),
                      source_statistics=metadata['source_statistics'],warnings=metadata.get('warnings',[]),
                      required_rules=list(HDF5_RULES),format_version='HDF5 (source)')
        episodes=[{'episode_index':int(e['episode_index']),'row_count':int(e['row_count'])} for e in metadata['episodes']]
    else:
        from .dataset import load_metadata
        from .quality import RULES
        metadata=load_metadata(managed/'source')
        source.update(metadata['source'],data_root=str(managed/'source'),required_rules=list(RULES))
        episodes=[{'episode_index':i,'row_count':int(metadata['episodes'][i]['length'])} for i in metadata['episode_indices']]
    assert_input_unchanged(source)
    return {'label':request.get('label','本地导入批次'),'source':source,'episodes':episodes}

def _quality(request,run):
    from .business import get_store
    from .quality import run_batch, json_safe
    from .reports import write_report
    from .config import APP_VERSION
    batch=get_store(request['business_root']).get_batch(request['batch_id'])
    source=request['source']
    inventory=request['inventory']
    assert_input_unchanged(source)
    if source['kind']=='hdf5':
        fresh=run/'quality_preview'
        _preview_worker(Path(source['hdf5_path']),fresh,source.get('test_label'))
        report=_json(fresh/'quality_report.json')
        actual_sha=file_sha256(Path(source['hdf5_path']))
        if report.get('source',{}).get('sha256')!=actual_sha:
            raise ValueError('原始 HDF5 质检报告与本次输入 SHA-256 不一致')
        for result in report['episodes']:
            result.setdefault('row_count',next(e['row_count'] for e in inventory if e['episode_index']==result['episode_index']))
            result.setdefault('expected_length',result['row_count'])
            result.setdefault('video',{'status':'not_applicable','note':'原始 HDF5 图像数组，无视频编码'})
    else:
        report=run_batch(Path(source['data_root']),episode_indices=[e['episode_index'] for e in inventory],
                         source_kind='injected_test' if source.get('test_label') else 'original_public')
    if report.get('rule_version')!=request['rule_version']:
        raise ValueError('质检解析器规则版本与冻结请求不一致')
    report['dataset_input_fingerprint']=report.get('input_fingerprint')
    report['input_fingerprint']=source['input_fingerprint']
    report.update(run_id=run.name,batch_id=batch['batch_id'],app_version=APP_VERSION,
                  episode_indices=[e['episode_index'] for e in inventory])
    report['source']={**report.get('source',{}),**source,'source_kind':'injected_test' if source.get('test_label') else 'original_public'}
    if source['kind']=='hdf5':
        report['source'].update(integrity_status='differs_from_manifest' if source.get('test_label') else 'matches_manifest',
            integrity_note='管理副本已重新计算 SHA-256；故障副本保留独立测试标记，原件未修改',
            input_files=[{'path':'test.hdf5','status':'present','size_bytes':Path(source['hdf5_path']).stat().st_size,
                          'sha256':actual_sha,'hash_matches_manifest':actual_sha==HDF5_SHA256}])
    report.setdefault('params',{})
    report.setdefault('limitations',['HDF5 时间和索引为派生预览字段，不代表源硬件同步'])
    report.setdefault('started_at_utc',__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat())
    report.setdefault('runtime_seconds',0)
    report['issues']=[issue for ep in report['episodes'] for issue in ep['issues']]
    coverage=[c for ep in report['episodes'] for c in ep['coverage']]
    report['summary']={'episode_count':len(report['episodes']),'row_count':sum(e['row_count'] for e in report['episodes']),
        'issue_count':len(report['issues']),'data_issue_count':sum(i.get('category')=='data_issue' for i in report['issues']),
        'load_failure_count':sum(i.get('category')=='load_failure' for i in report['issues']),
        'not_checked_count':sum(i.get('category')=='not_checked' for i in report['issues']),
        'passed_episode_count':sum(e['status']=='passed' for e in report['episodes']),
        'coverage_total':len(coverage),'coverage_performed':sum(c['status']=='performed' for c in coverage)}
    assert_input_unchanged(source)
    report=json_safe(report)
    report['result_digest']=_digest({k:v for k,v in report.items() if k not in {'run_id','started_at_utc','runtime_seconds','dataset_input_fingerprint'}})
    paths=write_report(report,run/'reports')
    return {'report_path':str(paths['json']),'input_fingerprint':source['input_fingerprint'],'required_rules':source['required_rules']}

def preview_batch(batch: dict, episode_index: int, row: int=0) -> dict:
    import numpy as np
    import pandas as pd
    source=batch['source']
    assert_input_unchanged(source)
    if source['kind']=='so100':
        from .dataset import load_episode
        from .video import read_video_frame
        episode=load_episode(Path(source['data_root']),episode_index)
        row=max(0,min(row,len(episode.table)-1))
        image=None
        issue=[]
        try: image=read_video_frame(episode.video_path,float(episode.table.iloc[row]['timestamp']))['image']
        except Exception as exc: issue=[{'message':str(exc),'field':'video'}]
        return {'image':image,'table':episode.table,'state_names':episode.state_names,'action_names':episode.action_names,
                'fps':episode.fps,'issues':issue,'source':episode.source,'row_index':row}
    root=Path(source['preview_root'])
    expected=_json(Path(source['managed_path'])/'preview_manifest.json')['files']
    if _files(root,[e['path'] for e in expected])!=expected:
        raise ValueError('原始预览缓存已变化，请重新导入；不能用变化后的缓存代表输入')
    metadata=_json(root/'metadata.json')
    episode=next(e for e in metadata['episodes'] if e['episode_index']==episode_index)
    table=pd.read_parquet(root/episode['table_path']) if episode.get('table_path') else pd.DataFrame()
    image=None
    if episode.get('images_path'):
        with np.load(root/episode['images_path'],allow_pickle=False) as archive:
            images=archive['images']
            if 0<=row<len(images): image=images[row]
    return {'image':image,'table':table,'state_names':metadata['state_names'],'action_names':metadata['action_names'],
            'fps':metadata['fps'],'issues':episode['issues'],'source':source,'row_index':row}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',choices=['copy','preview','inventory','quality'],required=True)
    parser.add_argument('--run-dir',type=Path,required=True)
    args=parser.parse_args()
    run=args.run_dir.resolve()
    request=_json(run/'request.json')
    try:
        details=globals()['_'+args.stage](request,run)
        _write(run/'steps'/f'{args.stage}.json',{'stage':args.stage,'status':'passed','details':details})
        return 0
    except Exception as exc:
        from .errors import explain_error,error_details
        diagnostic={'stage':args.stage,'message_zh':explain_error(exc),'input_path':request.get('input_path'),
                    **error_details(exc,include_traceback=True),**getattr(exc,'details',{})}
        _write(run/'diagnostic.json',diagnostic)
        _write(run/'steps'/f'{args.stage}.json',{'stage':args.stage,'status':'failed','diagnostic':diagnostic})
        return 1

if __name__=='__main__': raise SystemExit(main())
