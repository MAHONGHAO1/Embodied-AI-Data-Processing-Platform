"""Create a clearly labelled fault copy of the pinned source, never edit input."""
import argparse
import json
from pathlib import Path
import shutil

from worker import validate_file, sha256


def main():
    parser=argparse.ArgumentParser(description='生成 HDF5 故障演示副本；原始文件只读')
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scenario',choices=['isolated','image_length'],default='isolated')
    args=parser.parse_args()
    source=args.input.resolve(); target=args.output.resolve()
    validate_file(source,None)
    if source==target or target.exists(): raise ValueError('演示目标必须是新的独立文件，禁止覆盖')
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,target)
    import h5py
    import numpy as np
    injections=[]
    with h5py.File(target,'r+') as handle:
        if args.scenario=='isolated':
            handle['data/demo_0/actions'][10,2]=np.nan
            del handle['data/demo_1/obs/robot0_eef_pos']
            injections=[{'episode':0,'row':10,'field':'actions[2]','change':'NaN'},
                        {'episode':1,'field':'obs/robot0_eef_pos','change':'删除必需字段'}]
        else:
            group=handle['data/demo_0/obs']; images=group['agentview_image'][:]
            del group['agentview_image']; group.create_dataset('agentview_image',data=images[:-1])
            injections=[{'episode':0,'field':'obs/agentview_image','change':'图像数组减少最后一帧'}]
    evidence={'kind':'injected_test','test_label':f'V0.4 {args.scenario} 人为故障副本',
              'source_sha256':sha256(source),'copy_sha256':sha256(target),'injections':injections,
              'note':'只修改测试副本；不代表原始公开数据存在这些问题。'}
    target.with_suffix('.demo.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
