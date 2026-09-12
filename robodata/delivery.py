"""Create an allowlisted delivery ZIP, verify extracted bytes, then load officially."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile

from .source import file_sha256
from .dataset import dataset_fingerprint
from .runtime import _atomic_json, _read_json


def _relative(name: str) -> str:
    path=PurePosixPath(name)
    if not name or '\\' in name or ':' in name or path.is_absolute() or '..' in path.parts or '.' in path.parts:
        raise ValueError(f'交付清单包含不安全路径：{name}')
    if path.as_posix()!=name:
        raise ValueError(f'交付路径没有规范化：{name}')
    return name


def _portable(value):
    """Remove workstation locations from documents; payload bytes stay untouched."""
    if isinstance(value,dict):
        return {k:_portable(v) for k,v in value.items() if k not in {'traceback','exception_message',
                  'managed_path','preview_root','hdf5_path','data_root','manifest_path','business_root'}}
    if isinstance(value,list): return [_portable(v) for v in value]
    if isinstance(value,str):
        if re.match(r'^[A-Za-z]:[\\/]',value) or value.startswith('\\\\'):
            return '[本机路径省略]/'+re.split(r'[\\/]',value)[-1]
    return value


def _file_records(root):
    records=[]
    for path in sorted(root.rglob('*')):
        if path.is_symlink(): raise ValueError('交付包不能包含符号链接')
        if path.is_file() and path != root/'manifest.json':
            name=path.relative_to(root).as_posix()
            _relative(name)
            records.append({'path':name,'size':path.stat().st_size,'sha256':file_sha256(path)})
    return records


def _validate_payload(root: Path, expected_snapshot: str | None=None) -> dict:
    manifest=_read_json(root/'manifest.json')
    if not manifest or manifest.get('schema_version')!=1:
        raise ValueError('交付包缺少有效的 manifest.json')
    if expected_snapshot and manifest.get('snapshot_id')!=expected_snapshot:
        raise ValueError('交付包业务快照与本次冻结快照不一致')
    files=manifest.get('files')
    if not isinstance(files,list) or not files: raise ValueError('交付清单不能为空')
    names=[_relative(e['path']) for e in files]
    if len(set(names))!=len(names) or 'manifest.json' in names:
        raise ValueError('交付清单重复或递归包含自身')
    actual=_file_records(root)
    if actual!=files:
        found={x['path']:x for x in actual}; expected={x['path']:x for x in files}
        changed=[p for p in sorted(set(found)|set(expected)) if found.get(p)!=expected.get(p)]
        raise ValueError('交付文件缺失、大小或 SHA-256 不一致：'+', '.join(changed[:5]))
    info=_read_json(root/'dataset/meta/info.json')
    if not info or info.get('codebase_version')!='v3.0': raise ValueError('交付数据不是已验证的 LeRobot v3.0')
    if info['total_episodes']!=manifest['episode_count'] or info['total_frames']!=manifest['row_count']:
        raise ValueError('交付任务或帧数与清单不一致')
    return manifest


def package(request: dict, run: Path) -> dict:
    from .business import get_store
    from .importing import verify_managed_source
    spec=request['snapshot']
    batch=get_store(request['business_root']).get_batch(request['batch_id'])
    verify_managed_source(batch['source'])
    output=next(o for o in batch['outputs'] if o['output_id']==spec['output_id'])
    if get_store(request['business_root']).delivery_spec(batch['batch_id'],output['output_id'])!=spec:
        raise ValueError('交付冻结快照已过期，请重新生成交付包')
    dataset=Path(spec['artifact_path']).resolve()
    fingerprint=output['validation'].get('output_fingerprint')
    if not fingerprint or dataset_fingerprint(dataset)!=fingerprint:
        raise ValueError('已验证输出文件发生变化，禁止生成交付包')
    conversion_run=run.parent/spec['conversion_run_id']
    official=conversion_run/'steps/official_verify.json'
    quality=conversion_run/'reports/quality_report.json'
    proof=_read_json(official)
    if not proof or proof.get('status')!='passed': raise ValueError('缺少真实转换的官方验证证据')
    if file_sha256(official)!=output['validation'].get('official_evidence_sha256'):
        raise ValueError('官方验证证据已变化，不能打包')
    if not quality.is_file() or file_sha256(quality)!=output['validation'].get('report_sha256'):
        raise ValueError('输出质检证据已变化或缺失，不能打包')
    payload=run/'payload'
    if payload.exists(): raise FileExistsError('本次交付暂存目录已存在，不能覆盖')
    (payload/'dataset').mkdir(parents=True)
    # Only official files and explicitly documented annotations go into delivery.
    for folder in ('meta','data','videos'):
        for source in sorted((dataset/folder).rglob('*')):
            if source.is_symlink(): raise ValueError('输出包含不支持的符号链接')
            if source.is_file():
                if source.suffix not in {'.json','.parquet','.mp4'}: raise ValueError('输出目录存在非交付文件')
                target=payload/'dataset'/source.relative_to(dataset)
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
    for name in ('source_manifest.json','annotations.json','CONVERSION.md'):
        if (dataset/name).is_file(): shutil.copy2(dataset/name,payload/'dataset'/name)
    docs=payload/'documents'
    docs.mkdir()
    _atomic_json(docs/'annotations.json',_portable(spec['episodes']))
    _atomic_json(docs/'source.json',_portable(batch['source']))
    _atomic_json(docs/'mapping.json',{'snapshot_id':spec['snapshot_id'],'content_version':spec['content_version'],
        'episodes':[{'source_episode_index':e['source_episode_index'],'output_episode_index':e['output_episode_index'],
                     'row_count':e['row_count']} for e in spec['episodes']]})
    _atomic_json(docs/'cleaning_summary.json',{'counts':batch['counts'],'total_episodes':batch['total_episodes'],
        'total_rows':batch['total_rows'],'episodes':_portable(batch['episodes'])})
    _atomic_json(docs/'selection.json',{'batch_id':batch['batch_id'],'content_version':batch['content_version'],
        'snapshot_id':spec['snapshot_id'],'cleaning_confirmed':batch['cleaning_confirmed'],
        'episodes':_portable([{k:ep[k] for k in ('episode_index','row_count','disposition','selection_reason')} for ep in batch['episodes']])})
    _atomic_json(docs/'review.json',{'batch_id':batch['batch_id'],'content_version':batch['content_version'],
        'snapshot_id':spec['snapshot_id'],'local_single_user':True,
        'episodes':_portable([{k:ep[k] for k in ('episode_index','row_count','annotation','review')} for ep in batch['episodes'] if ep['disposition']=='keep'])})
    _atomic_json(docs/'cleaning_record.json',{'policy':'只做结构检查与任务级隔离，原始状态／动作不修复、未删帧',
        'data_modified':False,'input_fingerprint':batch['source']['input_fingerprint'],
        'base_quality_runs':sorted({ep['quality']['run_id'] for ep in batch['episodes'] if ep['quality'].get('run_id')})})
    final_quality=conversion_run/'reports/final_quality.json'
    if final_quality.is_file():
        if output['validation'].get('final_quality_required') and file_sha256(final_quality)!=output['validation'].get('final_quality_sha256'):
            raise ValueError('最终质检报告在验证后变化，不能打包')
        _atomic_json(docs/'final_quality.json',_portable(_read_json(final_quality)))
    elif output['validation'].get('final_quality_required'):
        raise ValueError('当前产物缺少审核后的最终质检报告，不能打包')
    _atomic_json(docs/'conversion_verification.json',_portable(proof))
    _atomic_json(docs/'quality_report.json',_portable(_read_json(quality)))
    source = batch.get('source', {})
    source_kind = source.get('kind', 'hdf5')
    source_repo = source.get('repo_id', 'robomimic/robomimic_datasets')
    source_revision = source.get('revision', '74fa018461f479cd9fd15b924a16103012096203')
    source_license = source.get('license', 'MIT (upstream dataset card declaration)')
    source_robot = source.get('robot', 'Panda / Lift 仿真测试数据')
    if source_kind == 'so100':
        dataset_summary = ('SO-100 真机遥操作子集；状态与动作均为 6 维关节值，保留来源顺序与原始数值；'
                           '相机为 640×480 AV1，remux 无重编码；时间按 30 Hz 派生。')
        license_block = (
            f'Source: {source_repo}\nRevision: {source_revision}\n'
            f'License: {source_license}\n'
            f'https://huggingface.co/datasets/{source_repo}/tree/{source_revision}\n'
            f'{source_robot}. Source robot numeric representation preserved.\n')
    else:
        dataset_summary = 'Panda/Lift 仿真测试片段，H.264 有损，时间按 20 Hz 派生，全部属于 train。'
        license_block = (
            f'Source: {source_repo}\nRevision: {source_revision}\n'
            f'License: {source_license}\n'
            f'https://huggingface.co/datasets/{source_repo}/tree/{source_revision}\n'
            f'{source_robot}. Source robot numeric representation preserved.\n')
    (docs/'SOURCE_LICENSE.txt').write_text(license_block, encoding='utf-8')
    (payload/'README.md').write_text(
        '# RoboData 本地交付包\n\n数据位于 dataset/，人工标注、来源、清洗和验证证据位于 documents/。\n'
        'manifest.json 保存相对文件清单与 SHA-256，不包含其自身哈希。ZIP 整包哈希在包外记录。\n'
        f'本数据为 {dataset_summary}\n'
        '使用 LeRobot 0.4.4、torch 2.7.1+cpu、torchvision 0.22.1+cpu、av 15.1.0。\n'
        '在独立环境设置 HF_HUB_OFFLINE=1、HF_DATASETS_OFFLINE=1、PYTHONUTF8=1 后：\n\n'
        '```python\nfrom lerobot.datasets.lerobot_dataset import LeRobotDataset\n'
        'from torch.utils.data import DataLoader\n'
        'ds = LeRobotDataset("local/delivery", root="dataset", video_backend="pyav")\n'
        'batch = next(iter(DataLoader(ds, batch_size=2, num_workers=0)))\n```\n',encoding='utf-8')
    manifest={'schema_version':1,'format_version':'v3.0','batch_id':batch['batch_id'],
              'content_version':spec['content_version'],'snapshot_id':spec['snapshot_id'],
              'episode_count':len(spec['episodes']),'row_count':sum(e['row_count'] for e in spec['episodes']),
              'files':_file_records(payload)}
    _atomic_json(payload/'manifest.json',manifest)
    _validate_payload(payload,spec['snapshot_id'])
    destination=Path(request.get('archive_output') or run/'delivery.zip')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists(): raise FileExistsError('交付包路径已存在，不覆盖既有交付')
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(payload.rglob('*')):
            if path.is_file(): archive.write(path,path.relative_to(payload).as_posix())
    if dataset_fingerprint(dataset)!=fingerprint: raise ValueError('打包期间已验证输出发生变化，交付未登记')
    return {'zip_path':str(destination.resolve()),'zip_sha256':file_sha256(destination),'size_bytes':destination.stat().st_size,
            'snapshot_id':spec['snapshot_id'],'episode_count':manifest['episode_count'],'row_count':manifest['row_count']}


def verify(request: dict, run: Path) -> dict:
    prior=_read_json(run/'steps/package.json',{}).get('details',{})
    path=Path(request.get('archive_path') or prior['zip_path']).resolve()
    before=file_sha256(path)
    if prior and before!=prior['zip_sha256']: raise ValueError('ZIP 与打包时整包 SHA-256 不一致')
    target=run/'unpacked'
    target.mkdir(exist_ok=False)
    with zipfile.ZipFile(path) as archive:
        entries=archive.infolist()
        names=[_relative(e.filename) for e in entries]
        if len(set(n.casefold() for n in names))!=len(names): raise ValueError('ZIP 包含重复路径')
        if sum(e.file_size for e in entries)>1024**3: raise ValueError('ZIP 展开超过本机样本交付范围 1 GB')
        for e in entries:
            if e.is_dir() or ((e.external_attr>>16)&0o170000)==0o120000:
                raise ValueError('ZIP 只允许普通文件，不允许目录记录或链接')
            out=target/e.filename
            out.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(e) as stream,out.open('wb') as sink: shutil.copyfileobj(stream,sink)
    manifest=_validate_payload(target,request.get('snapshot',{}).get('snapshot_id'))
    if file_sha256(path)!=before: raise ValueError('验证过程中 ZIP 发生变化')
    _atomic_json(run/'archive.sha256.json',{'filename':path.name,'sha256':before,'size_bytes':path.stat().st_size})
    return {'zip_path':str(path),'zip_sha256':before,'size_bytes':path.stat().st_size,'manifest_verified':True,
            'unpacked_path':str(target),'dataset_path':str(target/'dataset'),
            'episode_count':manifest['episode_count'],'row_count':manifest['row_count'],
            'snapshot_id':manifest['snapshot_id'],'format_version':manifest['format_version']}


def verified_delivery(batch: dict, delivery_id: str) -> dict:
    item=next((i for i in batch['deliveries'] if i['delivery_id']==delivery_id),None)
    if not item or not item.get('current') or item['content_version']!=batch['content_version']:
        raise ValueError('交付包不是当前批次版本，可查看历史但不能作为当前交付下载')
    path=Path(item['zip_path'])
    if not path.is_file() or file_sha256(path)!=item['zip_sha256']:
        raise ValueError('交付 ZIP 缺失或字节已变化，请重新验证')
    return {'path':str(path),'sha256':item['zip_sha256'],'size_bytes':path.stat().st_size,
            'episode_count':len(item['episodes']),'row_count':sum(e['row_count'] for e in item['episodes']),
            'format_version':'v3.0','content_version':item['content_version']}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',choices=['package','verify'],required=True)
    parser.add_argument('--run-dir',type=Path,required=True)
    args=parser.parse_args(); run=args.run_dir.resolve()
    try:
        details=globals()[args.stage](_read_json(run/'request.json'),run)
        _atomic_json(run/'steps'/f'{args.stage}.json',{'stage':args.stage,'status':'passed','details':details})
        return 0
    except Exception as exc:
        from .errors import explain_error,error_details
        diagnostic={'stage':args.stage,'message_zh':explain_error(exc),**error_details(exc,include_traceback=True)}
        _atomic_json(run/'diagnostic.json',diagnostic)
        _atomic_json(run/'steps'/f'{args.stage}.json',{'stage':args.stage,'status':'failed','diagnostic':diagnostic})
        return 1

if __name__=='__main__': raise SystemExit(main())
