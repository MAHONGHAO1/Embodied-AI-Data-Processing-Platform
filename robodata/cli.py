"""Console entry points; use the same readers and checks as the web application."""
import argparse
import json
from pathlib import Path
import sys
import time

from .config import DEFAULT_DATA_ROOT, DEFAULT_REPORT_ROOT, EPISODES
from .sources import DEFAULT_HDF5_ROOT, HDF5_PATH


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="RoboData 具身数据质检工作台")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-sample", help="获取固定版本的 5 条公开任务")
    fetch.add_argument('--source', choices=('so100', 'robomimic'), default='so100')
    fetch.add_argument("--data-root", type=Path, default=None)
    fetch.add_argument("--proxy", default=None, help="可选进程级 HTTP(S) 代理；不保存、不修改系统配置")
    hdf = sub.add_parser('fetch-hdf5', help='获取固定版本 robomimic 仿真 HDF5 测试样本')
    hdf.add_argument('--data-root', type=Path, default=DEFAULT_HDF5_ROOT)
    hdf.add_argument('--proxy', default=None)
    inspect = sub.add_parser("inspect", help="读取一条任务，打印字段与样本统计")
    inspect.add_argument('--source', choices=('so100', 'converted'), default='so100')
    inspect.add_argument("--data-root", type=Path, default=None)
    inspect.add_argument("--episode", type=int, default=0)
    check = sub.add_parser("check", help="检查全部 5 条任务并输出 HTML/JSON")
    check.add_argument('--source', choices=('so100', 'converted'), default='so100')
    check.add_argument("--data-root", type=Path, default=None)
    check.add_argument("--output", type=Path, default=DEFAULT_REPORT_ROOT)
    check.add_argument("--skip-video", action="store_true", help="跳过视频检查；报告明确标为未检查")
    check.add_argument("--node-timeout", type=float, default=60, help="普通节点超时秒数")
    check.add_argument("--video-timeout", type=float, default=120, help="视频解码节点超时秒数")
    check.add_argument("--execution-demo", choices=("none", "video_timeout"), default="none",
                       help="video_timeout：明确标注的人为运行等待演示")
    check.add_argument("--runs-root", type=Path, default=None, help="可选运行记录目录")
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
    demo = sub.add_parser("make-failure-demo", help="从原始样本生成明确标注的故障演示副本")
    demo.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT, help="完整原始样本目录")
    demo.add_argument("--target", type=Path, default=None, help="独立的空目标目录；默认位于 work 下")
    demo.add_argument("--scenario", choices=("anomalies", "missing_video"), default="anomalies",
                      help="anomalies：数据异常；missing_video：单任务视频缺失")
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
            command.add_argument('--source', choices=('hdf5', 'so100'), required=True)
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
                    run_id = business.start_import(args.input, args.source, args.label, args.test_label, **options)
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
        if args.command == 'fetch-hdf5' or (args.command == 'fetch-sample' and args.source == 'robomimic'):
            from .sources import fetch_hdf5
            print(str(fetch_hdf5(args.data_root or DEFAULT_HDF5_ROOT, proxy=args.proxy, progress=print)))
            return 0
        if args.command == "fetch-sample":
            from .source import fetch_sample
            manifest = fetch_sample(args.data_root or DEFAULT_DATA_ROOT, proxy=args.proxy, progress=lambda *parts: print(*parts, flush=True))
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
            return 2 if report["summary"].get("load_failure_count", 0) else 0
        return 0
    except Exception as exc:
        from .errors import explain_error
        print(explain_error(exc, context="执行命令"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
