"""Library failures have Chinese explanations and retain separate diagnostics."""

import json

import pandas as pd
import pytest
import requests

from robodata.errors import error_details, explain_error
from robodata.video import inspect_video


@pytest.mark.parametrize(("exc", "expected"), [
    (FileNotFoundError("No such file or directory"), "文件不存在"),
    (PermissionError("Permission denied"), "没有权限"),
    (requests.exceptions.ProxyError("Unable to connect to proxy"), "下载代理"),
    (requests.exceptions.ReadTimeout("Read timed out"), "操作超时"),
    (requests.exceptions.ConnectionError("Name resolution failed"), "无法连接数据源"),
    (ValueError("could not convert string to float: 'bad'"), "格式或参数"),
    (RuntimeError("unexpected backend error"), "操作未完成"),
])
def test_english_errors_are_explained_without_losing_diagnostics(exc, expected):
    message = explain_error(exc, "读取任务数据")
    assert message.startswith("读取任务数据失败：")
    assert expected in message
    assert str(exc) not in message
    assert error_details(exc) == {"exception_type": type(exc).__name__, "original_message": str(exc)}


def test_invalid_json_explains_location_in_chinese():
    with pytest.raises(json.JSONDecodeError) as failure:
        json.loads('{"broken":\n}')
    assert "第 2 行" in explain_error(failure.value)
    assert "JSON 格式" in explain_error(failure.value)


def test_real_corrupt_parquet_and_video_errors_have_format_specific_messages(tmp_path):
    table = tmp_path / "corrupt.parquet"
    table.write_bytes(b"explicit failure fixture: not a parquet file")
    with pytest.raises(Exception) as failure:
        pd.read_parquet(table)
    assert "Parquet 文件可能损坏" in explain_error(failure.value)
    video = tmp_path / "corrupt.mp4"
    video.write_bytes(b"explicit failure fixture: not a video")
    with pytest.raises(Exception) as failure:
        inspect_video(video)
    assert "视频无法解码" in explain_error(failure.value)


def test_http_failure_uses_status_and_chinese_reason():
    response = requests.Response()
    response.status_code = 404
    exc = requests.exceptions.HTTPError("404 Client Error: Not Found", response=response)
    assert "找不到所需文件（HTTP 404）" in explain_error(exc)


def test_project_validation_messages_are_preserved():
    message = "请求时间超出视频可读取范围"
    assert explain_error(ValueError(message)) == message
