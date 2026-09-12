"""Portable strict JSON and self-contained HTML for one immutable run result."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .config import APP_VERSION
from .quality import RULE_LABELS, json_safe


def report_json(report: dict) -> str:
    return json.dumps(json_safe(report), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


def _text(value) -> str:
    return html.escape("—" if value is None else str(value), quote=True)


EVIDENCE_LABELS = {
    "missing_fields": "缺失字段", "expected": "期望值", "actual": "实际值",
    "field": "字段", "value": "原始值", "expected_shape": "期望维度", "actual_shape": "实际维度",
    "previous": "上一条时间值", "current": "当前时间值", "minimum_seconds": "最小时间（秒）",
    "expected_seconds": "期望间隔（秒）", "actual_seconds": "实际间隔（秒）",
    "tolerance_seconds": "允许误差（秒）", "video_frames": "视频帧数", "table_rows": "数据表行数",
    "timestamp": "样本时间（秒）", "pts_time": "视频帧时间（秒）", "error_seconds": "时间差（秒）",
    "reasons": "触发原因", "video_first_pts": "视频起始时间（秒）", "video_last_pts": "视频末帧时间（秒）",
    "readable_range_epsilon_seconds": "视频读取边界容差（秒）", "error": "原因说明",
    "file": "关联文件", "path": "文件路径", "node_id": "处理节点",
    "timeout_seconds": "超时预算（秒）", "elapsed_seconds": "已运行时间（秒）",
}
REASON_LABELS = {"same_index_time_error": "相同行位置的样本时间与视频时间差超出容差",
                 "outside_readable_video_range": "样本时间超出视频可读取范围"}


def evidence_for_display(evidence: dict) -> dict:
    """Chinese evidence labels; machine keys and raw diagnostics stay in JSON."""
    display = {}
    for key, value in evidence.items():
        if key == "diagnostics":
            continue
        if key == "reasons" and isinstance(value, list):
            value = [REASON_LABELS.get(reason, reason) for reason in value]
        display[EVIDENCE_LABELS.get(key, key)] = value
    return display


def _evidence_html(evidence: dict) -> str:
    readable = _text(json.dumps(evidence_for_display(evidence), ensure_ascii=False))
    diagnostics = evidence.get("diagnostics")
    details = ""
    if diagnostics:
        details = ("<details><summary>原始错误详情（排查用）</summary><pre>"
                   + _text(json.dumps(diagnostics, ensure_ascii=False, indent=2)) + "</pre></details>")
    return f"<code>{readable}</code>{details}"


def _execution_html(report: dict) -> str:
    execution = report.get("execution", {})
    if not report.get("run_id") and not execution:
        return ""
    status = {"pending": "待执行", "running": "运行中", "completed": "完成",
              "failed": "失败", "timed_out": "超时", "skipped": "跳过", "interrupted": "中断"}
    rows = "".join(
        f'<tr><td>{_text(node.get("episode_index"))}</td><td>{_text(node.get("label", node.get("id")))}</td>'
        f'<td>{_text(status.get(node.get("status"), node.get("status")))}</td>'
        f'<td>{_text(node.get("elapsed_seconds"))}</td><td>{_text(node.get("data_issue_count", 0))}</td>'
        f'<td>{_text(node.get("message", ""))}</td></tr>'
        for node in execution.get("nodes", []))
    demo = execution.get("execution_demo", "none")
    notice = ('<p class="notice">人为运行超时演示：仅模拟节点等待，不代表原始视频损坏。</p>'
              if demo not in (None, "none", "") else "")
    return (f'<h2>本次运行记录</h2><p>运行编号：<code>{_text(report.get("run_id"))}</code></p>'
            + notice + '<p>节点完成表示程序完成该步骤；数据是否有问题请结合异常数量和检查覆盖判断。</p>'
            + '<div class="scroll"><table><thead><tr><th>任务</th><th>节点</th><th>执行状态</th>'
            '<th>耗时（秒）</th><th>数据异常数</th><th>说明</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            + f'<p>运行日志：<code>{_text(execution.get("events_path", "运行记录页面中查看与下载"))}</code></p>')


def report_html(report: dict) -> str:
    summary = report["summary"]
    source = report["source"]
    metrics = [("任务", "episode_count"), ("样本行", "row_count"), ("数据异常", "data_issue_count"),
               ("读取失败", "load_failure_count"), ("未检查项", "not_checked_count"),
               ("全部检查通过的任务", "passed_episode_count")]
    cards = "".join(f'<div class="metric"><span>{label}</span><strong data-key="{key}">{summary[key]}</strong></div>'
                    for label, key in metrics)
    labels = {"passed": "检查通过", "issues": "发现异常", "incomplete": "检查不完整", "load_failed": "读取失败"}
    episodes = "".join(
        f'<tr><td>{item["episode_index"]}</td><td>{item.get("actual_row_count", item["row_count"])}</td><td>{_text(item["expected_length"])}</td>'
        f'<td>{labels.get(item["status"], _text(item["status"]))}</td><td>{len(item["issues"])}</td></tr>'
        for item in report["episodes"])
    categories = {"data_issue": "数据异常", "load_failure": "读取失败", "not_checked": "未检查"}
    issues = "".join(
        f'<tr><td>{item["episode_index"]}</td><td>{_text(item["row_index"])}</td><td>{_text(item["frame_index"])}</td>'
        f'<td>{categories.get(item["category"], _text(item["category"]))}</td>'
        f'<td>{_text(RULE_LABELS.get(item["rule"], item["rule"]))}</td><td>{_text(item["message"])}</td>'
        f'<td>{_evidence_html(item["evidence"])}</td></tr>'
        for item in report["issues"])
    coverage = "".join(
        f'<tr><td>{episode["episode_index"]}</td><td>{_text(RULE_LABELS.get(item["rule"], item["rule"]))}</td>'
        f'<td>{"已执行" if item["status"] == "performed" else "未执行"}</td><td>{_text(item["detail"])}</td></tr>'
        for episode in report["episodes"] for item in episode["coverage"])
    hash_labels = {True: "与下载清单一致", False: "与下载清单不一致", None: "未比较"}
    file_labels = {"present": "文件存在", "missing": "文件缺失"}
    inputs = "".join(f'<tr><td>{_text(item["path"])}</td><td>{_text(file_labels.get(item["status"], item["status"]))}</td>'
                     f'<td>{_text(item.get("size_bytes", "—"))}</td><td>{hash_labels[item.get("hash_matches_manifest")]}</td>'
                     f'<td><code>{_text(item.get("sha256", "—"))}</code></td></tr>'
                     for item in source.get("input_files", []))
    limits = "".join(f'<li>{_text(item)}</li>' for item in report.get("limitations", []))
    source_label = "故障注入测试副本" if source.get("source_kind") == "injected_test" else "公开来源样本"
    integrity_labels = {"matches_manifest": "当前文件与固定版本的本地下载清单一致",
                        "differs_from_manifest": "当前文件有缺失或与下载清单不一致，请检查输入文件",
                        "unverified": "缺少有效的下载清单，当前文件来源一致性未核验"}
    integrity_label = integrity_labels.get(source.get("integrity_status"), "当前文件来源一致性未核验")
    demo = source.get("demo_manifest")
    demo_notice = ""
    if isinstance(demo, dict):
        injection_rows = "".join(
            f'<tr><td>{_text(item.get("episode_index"))}</td><td>{_text(item.get("row_index"))}</td>'
            f'<td>{_text(item.get("field", ""))}</td><td>{_text(item.get("description", ""))}</td></tr>'
            for item in demo.get("injections", []) if isinstance(item, dict))
        demo_notice = (f'<div class="notice"><strong>故障注入测试副本</strong><p>{_text(demo.get("description", ""))}</p>'
                       '<p>以下问题为演示时主动注入，不代表原始公开数据的质量。一次注入可能触发多条检查规则。</p>'
                       '<table><thead><tr><th>任务</th><th>行位置</th><th>字段</th><th>注入说明</th></tr></thead>'
                       f'<tbody>{injection_rows}</tbody></table></div>')
    # Embed the exact report for machine inspection; escape script terminators.
    embedded = report_json(report).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>具身数据质量报告</title><style>
:root{{color-scheme:light}}body{{margin:0;background:#f5f7fa;color:#172b42;font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}}
main{{max-width:1240px;margin:auto;padding:36px 24px}}h1{{margin:0}}h2{{margin-top:32px;font-size:21px}}
.muted{{color:#526579}}.metrics{{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}}.metric{{background:white;border:1px solid #dae2eb;border-radius:10px;padding:16px;min-width:135px}}
.metric span{{display:block;color:#526579}}.metric strong{{font-size:27px}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{text-align:left;border-bottom:1px solid #dae2eb;padding:10px;vertical-align:top}}
th{{background:#e9eff5}}code{{overflow-wrap:anywhere;font-size:12px}}.scroll{{overflow-x:auto}}.notice{{background:#e9f1fa;padding:14px 18px;border-left:4px solid #346f9e}}details{{margin:18px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}
</style></head><body><main>
<p class="muted">RoboData Workbench · {_text(report.get("app_version", "历史报告未记录应用版本"))} · {_text(source_label)}</p><h1>具身数据质量报告</h1>
<p>{_text(source.get("repo_id", ""))}<br>数据版本：<code>{_text(source.get("revision", ""))}</code></p>
<p class="muted">运行时间（UTC）：{_text(report.get("started_at_utc", ""))} · 耗时 {_text(report.get("runtime_seconds", ""))} 秒</p>
<p class="notice">{_text(integrity_label)}。{_text(source.get("integrity_note", ""))}</p>
{demo_notice}
<div class="metrics">{cards}</div>
<div class="notice">检查覆盖：{summary["coverage_performed"]} / {summary["coverage_total"]} 项。未检查的数据不会计为检查通过。问题记录合计：<strong data-key="issue_count">{summary["issue_count"]}</strong>。</div>
<h2>任务结果</h2><div class="scroll"><table><thead><tr><th>任务</th><th>实际行数</th><th>元数据行数</th><th>状态</th><th>问题记录</th></tr></thead><tbody>{episodes}</tbody></table></div>
<h2>问题与定位</h2><p class="muted">行位置与帧索引从 0 开始；“—”表示无法定位到具体样本。</p>
<div class="scroll"><table><thead><tr><th>任务</th><th>行位置</th><th>帧索引</th><th>类别</th><th>规则</th><th>说明</th><th>证据</th></tr></thead><tbody>{issues or '<tr><td colspan="7">本次已执行检查未发现问题。</td></tr>'}</tbody></table></div>
<h2>检查覆盖</h2><div class="scroll"><table><thead><tr><th>任务</th><th>规则</th><th>执行情况</th><th>说明</th></tr></thead><tbody>{coverage}</tbody></table></div>
{_execution_html(report)}
<h2>结论边界</h2><ul>{limits}</ul>
<details><summary>来源、参数与结果指纹</summary><p>规则版本：{_text(report["rule_version"])}</p><pre>{_text(json.dumps(report["params"], ensure_ascii=False, indent=2))}</pre>
<p>输入指纹：<code>{_text(report["input_fingerprint"])}</code><br>结果摘要：<code>{_text(report["result_digest"])}</code></p>
<table><thead><tr><th>输入文件</th><th>状态</th><th>字节</th><th>清单比较</th><th>SHA-256</th></tr></thead><tbody>{inputs}</tbody></table></details>
<script id="report-data" type="application/json">{embedded}</script>
</main></body></html>'''


def write_report(report: dict, out_dir: Path) -> dict[str, Path]:
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {"json": destination / "quality_report.json", "html": destination / "quality_report.html"}
    paths["json"].write_text(report_json(report), encoding="utf-8")
    paths["html"].write_text(report_html(report), encoding="utf-8")
    return paths
