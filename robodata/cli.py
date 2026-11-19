"""Console entry points; use the same readers and checks as the web application."""
import argparse
import json
from pathlib import Path
import sys
import time

from .config import DEFAULT_DATA_ROOT, DEFAULT_REPORT_ROOT, EPISODES, parse_episode_range
from .sources import DEFAULT_HDF5_ROOT, HDF5_PATH


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="RoboData 具身数据质检工作台")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-sample", help="获取固定版本的公开任务（默认 5 条）")
    fetch.add_argument('--source', choices=('so100', 'robomimic'), default='so100')
    fetch.add_argument("--data-root", type=Path, default=None)
    fetch.add_argument("--episodes", default=None,
                       help="SO-100 episode 范围，如 0-1；缺省用 ROBODATA_SO100_EPISODES 或 0-4")
    fetch.add_argument("--verify-only", action="store_true",
                       help="只校验本地样本与来源清单，不下载")
    fetch.add_argument("--verify-upstream", action="store_true",
                       help="配合 --verify-only，联网复核固定 revision 的上游文件标识")
    fetch.add_argument("--proxy", default=None, help="可选进程级 HTTP(S) 代理；不保存、不修改系统配置")
    fetch.add_argument("--endpoint", default=None,
                       help="下载传输端点，默认 huggingface.co；镜像只改变传输路径，revision 与哈希校验不变")
    hdf = sub.add_parser('fetch-hdf5', help='获取固定版本 robomimic 仿真 HDF5 测试样本')
    hdf.add_argument('--data-root', type=Path, default=DEFAULT_HDF5_ROOT)
    hdf.add_argument('--proxy', default=None)
    inspect = sub.add_parser("inspect", help="读取一条任务，打印字段与样本统计")
    inspect.add_argument('--source', choices=('so100', 'converted'), default='so100')
    inspect.add_argument("--data-root", type=Path, default=None)
    inspect.add_argument("--episode", type=int, default=0)
    check = sub.add_parser("check", help="检查全部 5 条任务并输出 HTML/JSON")
    check.add_argument('--source', choices=('so100', 'converted'), default='so100')
    check.add_argument("--data-root", type=Path, default=None,
                       help="任意 LeRobot v2.0/v3.0 数据目录；缺省时用内置样本")
    check.add_argument("--output", type=Path, default=DEFAULT_REPORT_ROOT)
    check.add_argument("--skip-video", action="store_true", help="跳过视频检查；报告明确标为未检查")
    check.add_argument("--node-timeout", type=float, default=60, help="普通节点超时秒数")
    check.add_argument("--video-timeout", type=float, default=120, help="视频解码节点超时秒数")
    check.add_argument("--execution-demo", choices=("none", "video_timeout"), default="none",
                       help="video_timeout：明确标注的人为运行等待演示")
    check.add_argument("--runs-root", type=Path, default=None, help="可选运行记录目录")
    check.add_argument("--format", choices=("json", "junit", "sarif"), default="json",
                       help="额外输出给 CI 的文件：junit 测试面板用，sarif 扫描视图用")
    check.add_argument("--emit-contract", type=Path, default=None,
                       help="把验收结论写进指定目录（QUALITY_REPORT.json/.md），随交付包一起走")
    check.add_argument("--fail-on-incomplete", action="store_true",
                       help="有未执行的判据时也返回非 0（用于把不完整的检查当失败）")
    for name in ('convert', 'verify-export'):
        conversion = sub.add_parser(name, help='HDF5 官方转换' if name == 'convert' else '独立离线官方加载验证')
        conversion.add_argument('--input', type=Path, default=HDF5_PATH)
        conversion.add_argument('--output-root', type=Path, default=None)
        conversion.add_argument('--runs-root', type=Path, default=None)
        if name == 'verify-export':
            conversion.add_argument('--export', type=Path, required=True)
    runs = sub.add_parser("runs", help="列出持久化运行记录")
    runs.add_argument("--runs-root", type=Path, default=None)
    show = sub.add_parser("show-run", help="查看某次运行与日志")
    show.add_argument("run_id")
    show.add_argument("--runs-root", type=Path, default=None)
    show.add_argument("--events", action="store_true", help="输出事件日志")
    suites = sub.add_parser("verify-fixtures", help="一条命令验完全部 known-bad 反例集")
    suites.add_argument("--root", type=Path, default=None, help="故障样本目录，缺省用仓库自带的")
    suites.add_argument("--json", action="store_true", dest="as_json", help="输出机器可读结果")
    demo = sub.add_parser("make-failure-demo", help="从原始样本生成明确标注的故障演示副本")
    demo.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT, help="完整原始样本目录")
    demo.add_argument("--target", type=Path, default=None, help="独立的空目标目录；默认位于 work 下")
    demo.add_argument("--scenario", choices=("anomalies", "missing_video"), default="anomalies",
                      help="anomalies：数据异常；missing_video：单任务视频缺失")
    adapters = sub.add_parser("adapters", help="列出、探测或预检数据源适配器")
    adapters_sub = adapters.add_subparsers(dest="adapters_command", required=True)
    adapters_sub.add_parser("list", help="列出已注册适配器与声明支持的版本")
    for action, help_text in (("detect", "探测一个本地输入匹配哪个适配器"),
                              ("inspect", "输出匹配适配器的结构预检摘要")):
        command = adapters_sub.add_parser(action, help=help_text)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--test-label", default=None, help="仅用于明确标注的故障副本")
    doctor = sub.add_parser("doctor", help="检查 Python、适配器、版本链与转换环境")
    doctor.add_argument("--quiet", action="store_true", help="只输出结论，不展开检查明细")
    status = sub.add_parser("status", help="查看队列、运行目录、磁盘与可清理空间")
    status.add_argument("--business-root", type=Path, default=None)
    status.add_argument("--runs-root", type=Path, default=None)
    status.add_argument("--json", action="store_true", dest="as_json")
    gc = sub.add_parser("gc", help="清理失败/中断且未被引用的运行目录")
    gc.add_argument("--business-root", type=Path, default=None)
    gc.add_argument("--runs-root", type=Path, default=None)
    gc.add_argument("--keep-last", type=int, default=5)
    gc.add_argument("--apply", action="store_true", help="真正删除；缺省只预览")
    gc.add_argument("--json", action="store_true", dest="as_json")
    process = sub.add_parser("process", help="按冻结计划跑完导入、标注、转换、验证与交付")
    process.add_argument("--plan", type=Path, required=True, help="JSON 或 TOML 计划")
    process.add_argument("--business-root", type=Path, default=None)
    process.add_argument("--runs-root", type=Path, default=None)
    process.add_argument("--timeout", type=float, default=None, help="单次运行等待上限秒数")
    process.add_argument("--dry-run", action="store_true", help="只校验计划与适配器，不启动任何运行")
    process.add_argument("--result-json", type=Path, default=None,
                         help="把计划结果写入指定 JSON（worker 使用）")
    jobs = sub.add_parser("jobs", help="本地持久任务队列")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_submit = jobs_sub.add_parser("submit", help="提交一个冻结的 process 计划")
    jobs_submit.add_argument("--plan", type=Path, required=True)
    jobs_submit.add_argument("--business-root", type=Path, default=None)
    jobs_submit.add_argument("--runs-root", type=Path, default=None)
    jobs_submit.add_argument("--max-attempts", type=int, default=1)
    jobs_submit.add_argument("--json", action="store_true", dest="as_json")
    jobs_list = jobs_sub.add_parser("list", help="列出任务")
    jobs_list.add_argument("--state", choices=("queued", "running", "succeeded", "failed", "canceled"),
                           default=None)
    jobs_list.add_argument("--limit", type=int, default=100)
    jobs_list.add_argument("--business-root", type=Path, default=None)
    jobs_list.add_argument("--json", action="store_true", dest="as_json")
    jobs_show = jobs_sub.add_parser("show", help="查看一个任务及其事件")
    jobs_show.add_argument("job_id")
    jobs_show.add_argument("--business-root", type=Path, default=None)
    jobs_show.add_argument("--json", action="store_true", dest="as_json")
    for action, help_text in (("cancel", "请求取消任务"), ("retry", "重新入队失败/取消的任务")):
        command = jobs_sub.add_parser(action, help=help_text)
        command.add_argument("job_id")
        command.add_argument("--business-root", type=Path, default=None)
        command.add_argument("--json", action="store_true", dest="as_json")
    worker = sub.add_parser("worker", help="运行单机任务 worker")
    worker.add_argument("--business-root", type=Path, default=None)
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll", type=float, default=2.0)
    worker.add_argument("--lease-seconds", type=float, default=60.0)
    worker.add_argument("--json", action="store_true", dest="as_json")
    registry = sub.add_parser("registry", help="本地数据集登记表")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    registry_list = registry_sub.add_parser("list", help="列出已登记数据集与版本")
    registry_list.add_argument("--business-root", type=Path, default=None)
    registry_show = registry_sub.add_parser("show", help="查看一个数据集的登记卡片")
    registry_show.add_argument("--dataset-id", required=True)
    registry_show.add_argument("--version", default=None)
    registry_show.add_argument("--business-root", type=Path, default=None)
    registry_register = registry_sub.add_parser("register", help="登记一个已验证的 dataset_card.json")
    registry_register.add_argument("--card", type=Path, required=True)
    registry_register.add_argument("--zip", type=Path, default=None)
    registry_register.add_argument("--artifact", type=Path, default=None)
    registry_register.add_argument("--business-root", type=Path, default=None)
    registry_verify = registry_sub.add_parser("verify", help="重算 card_digest，不信任文件自述")
    registry_verify.add_argument("--card", type=Path, required=True)
    for name, help_text in {
        'import-batch': '导入管理副本并生成原始预览', 'batches': '列出业务批次及坏批次诊断',
        'show-batch': '查看批次、清洗、审核和产物', 'check-batch': '真实质检并绑定批次',
        'convert-batch': '最终质检后转换全部审核通过的保留任务', 'deliver-batch': '打包并独立验证交付 ZIP',
        'final-check-batch': '审核后最终质检，通过后自动转换和交付', 'show-flow': '查看八节点输入输出及状态',
        'verify-delivery': '在新目录解包并官方离线加载交付 ZIP', 'reconcile-run': '恢复待回写的处理结果',
    }.items():
        command = sub.add_parser(name, help=help_text)
        command.add_argument('--business-root', type=Path, default=None)
        command.add_argument('--runs-root', type=Path, default=None)
        if name == 'import-batch':
            command.add_argument('--input', type=Path, required=True)
            command.add_argument('--source', choices=('auto', 'hdf5', 'so100', 'aloha', 'lerobot_v21', 'matching_cable'),
                                 required=True, help='auto 按目录结构选择适配器；失败时明确列出原因')
            command.add_argument('--label', required=True)
            command.add_argument('--test-label', default=None, help='仅用于明确标注的故障副本')
        if name in {'show-batch', 'show-flow', 'check-batch', 'final-check-batch', 'convert-batch', 'deliver-batch'}:
            command.add_argument('batch_id')
        if name in {'check-batch', 'final-check-batch', 'convert-batch', 'deliver-batch'}:
            command.add_argument('--revision', type=int, required=True, help='show-batch 返回的当前 revision')
        if name == 'deliver-batch':
            command.add_argument('--output-id', required=True)
        if name == 'final-check-batch':
            command.add_argument('--check-only', action='store_true', help='只生成最终质检报告，不自动转换和打包')
        if name == 'verify-delivery':
            command.add_argument('--archive', type=Path, required=True)
        if name == 'reconcile-run':
            command.add_argument('run_id')
        if name not in {'batches', 'show-batch', 'show-flow', 'reconcile-run'}:
            command.add_argument('--no-wait', action='store_true', help='只返回运行编号，后台继续处理')
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        if args.command == 'adapters':
            from .adapters import ADAPTER_CONTRACT_VERSION, descriptors, get_adapter
            from .adapters import detect as detect_sources
            if args.adapters_command == 'list':
                print(json.dumps({"contract_version": ADAPTER_CONTRACT_VERSION,
                                  "adapters": descriptors()}, ensure_ascii=False, indent=2))
                return 0
            path = args.input.expanduser().resolve()
            candidates = detect_sources(path, test_label=args.test_label)
            chosen = next((item for item in candidates if item["supported"]), None)
            if args.adapters_command == 'detect':
                print(json.dumps({"input": str(path), "detected": chosen, "candidates": candidates},
                                 ensure_ascii=False, indent=2))
                return 0 if chosen else 3
            if chosen is None:
                print(json.dumps({"input": str(path), "supported": False, "candidates": candidates},
                                 ensure_ascii=False, indent=2))
                return 3
            preflight = get_adapter(chosen["adapter_id"]).preflight(path, test_label=args.test_label)
            print(json.dumps({"input": str(path), "detection": chosen,
                              "preflight": preflight.to_dict()}, ensure_ascii=False, indent=2))
            return 0 if preflight.supported else 3
        if args.command == 'doctor':
            from .doctor import run_doctor
            report = run_doctor()
            if args.quiet:
                report = {"ok": report["ok"], "app_version": report["app_version"],
                          "failed": [item for item in report["checks"] if item["status"] == "fail"],
                          "warnings": [item for item in report["checks"] if item["status"] == "warn"]}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        if args.command == 'status':
            from .status import status
            value = status(args.business_root, args.runs_root)
            if args.as_json:
                print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            else:
                print(f"版本：{value['app']} {value['version']}")
                print(f"队列：{value['queue']['counts']}")
                print(f"运行：{value['runs']['counts']}")
                print(f"磁盘：free {value['disk']['free_gib']} GiB / "
                      f"{round(value['disk']['total_bytes'] / 1024 ** 3, 2)} GiB")
                print(f"可清理：{value['gc']['candidate_count']} 个目录 / "
                      f"{value['gc']['candidate_bytes']} bytes")
            return 0
        if args.command == 'gc':
            from .gc import apply, scan
            from .layout import Layout
            layout = Layout.current(args.business_root, args.runs_root)
            value = apply(layout, keep_last=args.keep_last) if args.apply else scan(
                layout, keep_last=args.keep_last)
            if args.as_json:
                print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            else:
                items = value.get("deleted", value.get("candidates", []))
                for item in items:
                    print(f"{item['run_id']}  {item['status']:<20}  {item['bytes']:>12}  {item['reason']}")
                if args.apply:
                    print(f"已删除 {len(value['deleted'])} 个目录，释放 {value['freed_bytes']} bytes")
                else:
                    print(f"预览 {len(items)} 个目录；加 --apply 才会删除")
            return 0
        if args.command == 'registry':
            from . import registry as registry_module
            from .business import get_store
            if args.registry_command == 'verify':
                result = registry_module.verify_card_path(args.card)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["valid"] else 3
            root = get_store(args.business_root).root
            try:
                if args.registry_command == 'list':
                    value = registry_module.list_datasets(root)
                elif args.registry_command == 'show':
                    value = registry_module.show(args.dataset_id, args.version, root)
                else:
                    value = registry_module.register_card_file(
                        args.card, root=root, zip_path=args.zip, artifact_path=args.artifact)
            except (KeyError, ValueError) as exc:
                print(json.dumps({"status": "rejected", "message": str(exc)}, ensure_ascii=False, indent=2))
                return 4
            print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == 'process':
            from .process_plan import EXIT_INVALID, PlanError, load_plan, run_process

            def write_result(value):
                if args.result_json is None:
                    return
                import os as _os
                target = Path(args.result_json)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                                default=str, allow_nan=False),
                                     encoding="utf-8")
                _os.replace(temporary, target)

            try:
                plan = load_plan(args.plan)
            except PlanError as exc:
                result = {"status": "invalid", "exit_code": EXIT_INVALID, "message": str(exc)}
                write_result(result)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return EXIT_INVALID
            result = run_process(plan, runs_root=args.runs_root, business_root=args.business_root,
                                 timeout=args.timeout, dry_run=args.dry_run)
            write_result(result)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return int(result["exit_code"])
        if args.command == 'jobs':
            from .jobs import JobStore
            from .process_plan import load_plan
            store = JobStore(args.business_root) if getattr(args, 'business_root', None) is not None \
                else JobStore()
            if args.jobs_command == 'submit':
                plan = load_plan(args.plan)
                value = store.submit(plan, plan_path=args.plan,
                                     business_root=args.business_root,
                                     runs_root=args.runs_root,
                                     max_attempts=args.max_attempts)
            elif args.jobs_command == 'list':
                value = {"jobs": store.list(args.state, args.limit), "summary": store.summary()}
            elif args.jobs_command == 'show':
                value = store.get(args.job_id)
                value["events"] = store.events(args.job_id)
            elif args.jobs_command == 'cancel':
                value = store.cancel(args.job_id)
            elif args.jobs_command == 'retry':
                value = store.retry(args.job_id)
            else:
                raise ValueError("未知 jobs 子命令")
            if args.as_json:
                print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            elif args.jobs_command == 'list':
                for item in value["jobs"]:
                    print(f"{item['id']}  {item['state']:<10}  {item['created_at']}  "
                          f"{item['error'] or ''}".rstrip())
            elif args.jobs_command == 'show':
                print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            else:
                job = value.get('job', value)
                print(f"{job['id']} {job['state']}")
            return 0
        if args.command == 'worker':
            from .worker import main as worker_main
            command = ["--business-root", str(args.business_root)] if args.business_root else []
            command += ["--poll", str(args.poll), "--lease-seconds", str(args.lease_seconds)]
            if args.once:
                command.append("--once")
            if args.as_json:
                command.append("--json")
            return worker_main(command)
        if args.command in {'import-batch', 'batches', 'show-batch', 'check-batch', 'convert-batch',
                            'deliver-batch', 'verify-delivery', 'reconcile-run', 'final-check-batch', 'show-flow'}:
            from . import business
            from .runtime import wait_run
            options = {'runs_root': args.runs_root, 'business_root': args.business_root}
            if args.command == 'batches':
                value = business.safe_list_batches(args.business_root)
            elif args.command == 'show-batch':
                value = business.get_store(args.business_root).get_batch(args.batch_id)
            elif args.command == 'show-flow':
                from .workflow import get_flow
                value = get_flow(business.get_store(args.business_root).get_batch(args.batch_id),
                                 runs_root=args.runs_root, business_root=args.business_root)
            elif args.command == 'reconcile-run':
                value = business.reconcile_run(args.run_id, runs_root=args.runs_root)
            else:
                if args.command == 'import-batch':
                    source_kind = args.source
                    if source_kind == 'auto':
                        from .adapters import best
                        source_kind = best(Path(args.input), test_label=args.test_label)["adapter_id"]
                    run_id = business.start_import(args.input, source_kind, args.label, args.test_label, **options)
                elif args.command == 'check-batch':
                    run_id = business.start_batch_quality(args.batch_id, args.revision, **options)
                elif args.command == 'final-check-batch':
                    run_id = business.start_final_quality(args.batch_id, args.revision, auto_continue=not args.check_only, **options)
                elif args.command == 'convert-batch':
                    run_id = business.start_batch_conversion(args.batch_id, args.revision, **options)
                elif args.command == 'deliver-batch':
                    run_id = business.start_delivery(args.batch_id, args.output_id, args.revision, **options)
                else:
                    run_id = business.start_verify_delivery(args.archive, **options)
                print(f'运行编号：{run_id}', flush=True)
                if args.no_wait:
                    return 0
                state = wait_run(run_id, runs_root=args.runs_root, timeout=1200)
                value = {key: state.get(key) for key in ('run_id', 'batch_id', 'status', 'message',
                           'summary', 'outcome', 'artifacts', 'business_registration')}
                print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
                return (2 if state.get('outcome')=='rejected' else 0) if state['status'] == 'completed' else 1
            print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "verify-fixtures":
            from .fixture_check import render_table, run_suite
            result = run_suite(args.root)
            if args.as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            else:
                print(render_table(result))
            return 0 if result["ok"] else 1
        if args.command in {'inspect', 'check'} and args.data_root is None:
            if args.source == 'converted':
                from .conversion import latest_export
                available = latest_export()
                if not available:
                    raise ValueError('没有可用的已验证转换输出，请先运行 convert')
                args.data_root = Path(available['output_path'])
            else:
                args.data_root = DEFAULT_DATA_ROOT
        if args.command in {'convert', 'verify-export'}:
            from .runtime import start_run, wait_run
            request = {'kind': 'conversion', 'input_path': str(args.input.resolve())}
            if args.output_root: request['output_root'] = str(args.output_root.resolve())
            if args.command == 'verify-export':
                request.update(verify_only=True, export_path=str(args.export.resolve()))
            run_id = start_run(request, runs_root=args.runs_root)
            print(f'运行编号：{run_id}', flush=True)
            state = wait_run(run_id, runs_root=args.runs_root, timeout=900)
            print(json.dumps({k: state.get(k) for k in ('run_id', 'status', 'message', 'output_path', 'report_path', 'verification_path')}, ensure_ascii=False, indent=2))
            return 0 if state['status'] == 'completed' else 1
        if args.command == 'fetch-sample' and args.source == 'robomimic' and (args.episodes or args.verify_only):
            raise ValueError("--episodes / --verify-only 仅适用于 SO-100 样本")
        if args.command == 'fetch-hdf5' or (args.command == 'fetch-sample' and args.source == 'robomimic'):
            from .sources import fetch_hdf5
            print(str(fetch_hdf5(args.data_root or DEFAULT_HDF5_ROOT, proxy=args.proxy, progress=print)))
            return 0
        if args.command == "fetch-sample":
            from .source import fetch_sample, verify_manifest, verify_sample
            root = args.data_root or DEFAULT_DATA_ROOT
            episodes = parse_episode_range(args.episodes, EPISODES) if args.episodes else None
            if args.verify_upstream and not args.verify_only:
                raise ValueError("--verify-upstream 必须与 --verify-only 一起使用")
            if args.verify_only:
                report = (verify_sample(root, episodes=episodes, proxy=args.proxy,
                                        endpoint=args.endpoint)
                          if args.verify_upstream else verify_manifest(root, episodes=episodes))
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report["ok"] else 1
            manifest = fetch_sample(root, proxy=args.proxy, episodes=episodes,
                                    endpoint=args.endpoint,
                                    progress=lambda *parts: print(*parts, flush=True))
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        elif args.command == "inspect":
            from .dataset import load_episode
            episode = load_episode(args.data_root, args.episode)
            print(json.dumps({
                "episode_index": episode.episode_index,
                "rows": len(episode.table), "expected_length": episode.expected_length,
                "fps": episode.fps, "fields": list(episode.table.columns),
                "state_names": episode.state_names, 'action_names': episode.action_names,
                'source': episode.source, "task": episode.metadata.get("episode", {}),
                "video": str(episode.video_path),
                'video_start_time': episode.video_start_time, 'video_end_time': episode.video_end_time,
                "semantics": "保留来源分量顺序及动作表示；不额外转换坐标或推定单位。",
            }, ensure_ascii=False, indent=2, default=str))
        elif args.command == "make-failure-demo":
            from .demo import create_failure_demo, read_demo_manifest
            target = create_failure_demo(args.data_root, args.target, scenario=args.scenario)
            manifest = read_demo_manifest(target)
            print(json.dumps({"演示目录": str(target), "数据类型": "故障注入测试副本",
                              "说明": manifest["description"], "注入清单": manifest["injections"]},
                             ensure_ascii=False, indent=2))
        elif args.command == "runs":
            from .runtime import list_runs
            print(json.dumps(list_runs(runs_root=args.runs_root), ensure_ascii=False, indent=2, default=str))
        elif args.command == "show-run":
            from .runtime import get_run, read_events
            value = read_events(args.run_id, runs_root=args.runs_root) if args.events else get_run(args.run_id, runs_root=args.runs_root)
            print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        elif args.command == "check":
            from .runtime import get_run, start_run
            run_id = start_run({"kind": "quality", "data_root": str(args.data_root.resolve()),
                                "check_video": not args.skip_video, "output_dir": str(args.output.resolve()),
                                "timeouts": {"default": args.node_timeout, "video_decode": args.video_timeout},
                                "execution_demo": args.execution_demo}, runs_root=args.runs_root)
            print(f"运行编号：{run_id}", flush=True)
            state = get_run(run_id, runs_root=args.runs_root)
            while state["status"] in {"queued", "running"}:
                time.sleep(0.15)
                state = get_run(run_id, runs_root=args.runs_root)
            report_path = state.get("report_path")
            report = json.loads(Path(report_path).read_text(encoding="utf-8")) if report_path and Path(report_path).is_file() else None
            print(json.dumps({"run_id": run_id, "status": state["status"],
                              "summary": report["summary"] if report else None,
                              "report_path": report_path, "artifacts": state.get("artifacts"),
                              "message": state.get("message", "")}, ensure_ascii=False, indent=2, default=str))
            if state["status"] in {"failed", "interrupted"} or report is None:
                return 1
            summary = report["summary"]
            if args.emit_contract:
                from .contract import write_contract
                written = write_contract(report, args.emit_contract)
                print(json.dumps({"contract": written}, ensure_ascii=False))
            if args.format in {"junit", "sarif"}:
                from .ci_output import to_junit, to_sarif
                rendered = to_junit(report) if args.format == "junit" else to_sarif(report)
                suffix = "xml" if args.format == "junit" else "sarif"
                ci_path = Path(report_path).with_name(f"quality_{args.format}.{suffix}")
                ci_path.write_text(rendered, encoding="utf-8")
                print(json.dumps({"ci_format": args.format, "ci_output": str(ci_path)},
                                 ensure_ascii=False))
            if summary.get("load_failure_count", 0):
                return 2
            if summary.get("data_issue_count", 0):
                return 3
            if args.fail_on_incomplete and summary.get("not_checked_count", 0):
                return 4
            return 0
        return 0
    except Exception as exc:
        from .adapters import UnsupportedSource
        from .errors import explain_error
        message = str(exc) if isinstance(exc, UnsupportedSource) else explain_error(exc, context="执行命令")
        print(message, file=sys.stderr)
        return 3 if isinstance(exc, UnsupportedSource) else 1


if __name__ == "__main__":
    raise SystemExit(main())
