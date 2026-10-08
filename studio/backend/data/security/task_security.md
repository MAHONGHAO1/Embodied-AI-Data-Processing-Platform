"""Celery / 异步任务参数安全审查清单（实现与评审对照）。

1. 任务入参只传 ID（task_id / export_id / qrdf_id），不传任意文件系统路径。
2. Worker 内重新 resolve 路径，并经 storage_root 沙箱校验。
3. 状态迁移仅通过 state_machine.transit_task（或等价显式 API）。
4. cleanup_* 不得删除 storage_root 外路径；跳过 cloud/ 误删防护保持。
5. 失败路径写日志，禁止静默 except: pass 吞掉安全相关错误。
6. 重试幂等：同一 ID 重复执行不产生越权副作用。
"""
