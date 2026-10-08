"""Status enum to display name mapping for the four-phase task workflow."""

STATUS_LABELS = {
    "pending_collect": "待收集",
    "collect_done": "待接入审核",
    "pending_preprocess": "待预处理",
    "preprocess_done": "预处理完成（待标注）",
    "pending_annotate": "待标注",
    "annotate_done": "标注完成（待审核）",
    "pending_audit": "待审核",
    "audit_passed": "审核通过",
    "storage_ready": "入库就绪",
    "rejected": "已驳回",
    "failed": "失败",
}

# Display label for new_status in transit responses
STATUS_DISPLAY = {
    **STATUS_LABELS,
    "pending_preprocess": "预处理中",
}


def display_status(status: str) -> str:
    return STATUS_DISPLAY.get(status, STATUS_LABELS.get(status, status))
