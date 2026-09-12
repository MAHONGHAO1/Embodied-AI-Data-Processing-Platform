"""Chinese messages for users, with separate original diagnostics for debugging."""

from __future__ import annotations

import json
import re
import traceback

import av
import pyarrow as pa
import requests


def explain_error(exc: BaseException, context: str = "") -> str:
    """Return a readable explanation without exposing a library traceback.

    ``context`` is a Chinese operation name, e.g. ``读取主相机视频``.
    The original exception is deliberately left intact; use ``error_details``
    in a collapsed diagnostic section when the exact library message is useful.
    """
    if isinstance(exc, FileNotFoundError):
        message = "所需文件不存在。请检查数据目录，并重新获取缺失的样本文件。"
    elif isinstance(exc, PermissionError):
        message = "没有权限访问文件，或文件正被其他程序占用。请检查目录权限并关闭占用程序。"
    elif isinstance(exc, json.JSONDecodeError):
        message = f"元数据不是有效的 JSON 格式，错误位于第 {exc.lineno} 行、第 {exc.colno} 列。请检查文件内容或重新获取样本。"
    elif isinstance(exc, UnicodeError):
        message = "文本文件编码无法读取。请使用 UTF-8 编码，或重新获取原始元数据。"
    elif isinstance(exc, requests.exceptions.ProxyError):
        message = "无法连接下载代理。请确认代理正在运行，并检查本次下载进程的代理配置。"
    elif isinstance(exc, requests.exceptions.SSLError):
        message = "下载连接的证书校验失败。请检查网络或代理配置后重试。"
    elif isinstance(exc, requests.exceptions.Timeout):
        message = "操作超时。请检查网络和代理是否可用，稍后重试；已完整获取的文件可以复用。"
    elif isinstance(exc, TimeoutError):
        message = "操作超时。请查看运行记录中的节点、超时参数和原始错误详情。"
    elif isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        reasons = {401: "数据源要求身份验证", 403: "数据源拒绝访问", 404: "数据源中找不到所需文件",
                   429: "访问过于频繁，数据源暂时限制请求"}
        reason = reasons.get(status, "数据源返回请求错误")
        code = f"（HTTP {status}）" if status is not None else ""
        message = f"{reason}{code}。请稍后重试，并确认固定版本仍可访问。"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        message = "无法连接数据源。请检查网络和代理是否可用，然后重试。"
    elif isinstance(exc, requests.exceptions.RequestException):
        message = "下载请求未完成。请检查网络连接，稍后重试；已完整获取的文件可以复用。"
    elif isinstance(exc, av.error.FFmpegError):
        message = "视频无法解码，文件可能损坏、内容不完整或编码不受支持。请重新获取该视频，再执行检查。"
    elif isinstance(exc, pa.ArrowException):
        message = "数据表无法读取，Parquet 文件可能损坏或格式不符合要求。请检查该文件或重新获取样本。"
    elif isinstance(exc, (ValueError, TypeError)) and re.search(r"[\u4e00-\u9fff]", str(exc)):
        # Validation errors authored by this project already describe the cause.
        message = str(exc)
    elif isinstance(exc, KeyError):
        message = "缺少操作所需的字段。请先运行基础质检，并在报告中查看缺失字段。"
    elif isinstance(exc, (IndexError, OverflowError)):
        message = "样本位置或数值超出可读取范围。请重新选择有效的行位置，并查看质检报告。"
    elif isinstance(exc, (ImportError, ModuleNotFoundError)):
        message = "运行依赖不完整。请在项目目录执行 uv sync --frozen，再重新启动工作台。"
    elif isinstance(exc, (ValueError, TypeError)):
        message = "数据格式或参数不符合要求。请检查输入字段、数值及文件格式。"
    elif isinstance(exc, OSError):
        message = "文件读写失败。请检查路径、剩余磁盘空间及目录权限，然后重试。"
    else:
        message = "操作未完成。请查看原始错误详情，确认输入文件及运行环境后重试。"
    return f"{context}失败：{message}" if context else message


def error_details(exc: BaseException, include_traceback: bool = False) -> dict[str, str]:
    """Keep machine diagnostics out of the primary, human-facing explanation."""
    details = {"exception_type": type(exc).__name__, "original_message": str(exc)}
    if include_traceback:
        details["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return details
