"""Copy actual acceptance artifacts to a convenient, git-ignored demo folder."""
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robodata.config import PROJECT_ROOT
from robodata.conversion import latest_export

def main():
    work = PROJECT_ROOT / 'work'
    checks = json.loads((work/'v02-acceptance.json').read_text(encoding='utf-8'))
    export = latest_export()
    records = {key:value['run_id'] for key,value in checks.items() if key != 'original_repeat'}
    if export: records['converted'] = export['run_id']
    target = work/'examples'
    target.mkdir(exist_ok=True)
    for name,run_id in records.items():
        run = work/'runs'/run_id
        output = target/name
        output.mkdir(exist_ok=True)
        for source in [run/'events.jsonl', run/'state.json', run/'request.json',
                       run/'reports/quality_report.html',run/'reports/quality_report.json']:
            shutil.copy2(source, output/source.name)
        official = run/'steps/official_verify.json'
        if official.exists(): shutil.copy2(official,output/'official_verify.json')
    (target/'index.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    project = PROJECT_ROOT.parents[1]
    evidence = project/'02_过程文件'/'V0.3_验收证据'
    evidence.mkdir(exist_ok=True)
    for name in ['v02-acceptance.json','v03-acceptance.json']:
        shutil.copy2(work/name,evidence/name)
    shutil.copy2(work/'conversion-failures/gate-002/failure_checks.json',evidence/'conversion-failure-checks.json')
    shutil.copy2(work/'conversion-real/source-output-contact-sheet.png',evidence/'source-output-contact-sheet.png')
    print(target)

if __name__ == '__main__': main()
