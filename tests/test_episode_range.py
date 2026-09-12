"""数据范围可配置：主环境解析与转换环境选择逻辑保持一致。

默认范围必须保持 V0.4 验收所用的 3 条（demo_0-2 / 174 帧），
扩容通过 ROBODATA_HDF5_EPISODES 环境变量完成，不修改代码。
"""
import importlib.util
import os

import pytest

from robodata.config import PROJECT_ROOT, parse_episode_range

FULL_DEMOS = {"demo_0": 59, "demo_1": 58, "demo_2": 57, "demo_3": 55, "demo_4": 51,
              "demo_5": 58, "demo_6": 49, "demo_7": 49, "demo_8": 44, "demo_9": 51}


def worker_module(episodes=None):
    """在指定 ROBODATA_HDF5_EPISODES 下加载转换环境 worker，用完恢复原环境。"""
    spec = importlib.util.spec_from_file_location(
        "conversion_worker_range_test", PROJECT_ROOT / "tools/conversion/worker.py")
    module = importlib.util.module_from_spec(spec)
    previous = os.environ.get("ROBODATA_HDF5_EPISODES")
    try:
        if episodes is None:
            os.environ.pop("ROBODATA_HDF5_EPISODES", None)
        else:
            os.environ["ROBODATA_HDF5_EPISODES"] = episodes
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop("ROBODATA_HDF5_EPISODES", None)
        else:
            os.environ["ROBODATA_HDF5_EPISODES"] = previous
    return module


# --- 主环境：范围解析 ---

def test_default_range_keeps_accepted_three():
    assert parse_episode_range("", (0, 1, 2)) == [0, 1, 2]


def test_range_supports_closed_interval():
    assert parse_episode_range("0-9", None) == list(range(10))


def test_range_supports_mixed_interval_and_points():
    assert parse_episode_range("0-2,5,7-9", None) == [0, 1, 2, 5, 7, 8, 9]


def test_range_deduplicates_and_sorts():
    assert parse_episode_range("3,1,3,0", None) == [0, 1, 3]


@pytest.mark.parametrize("raw", ["abc", "5-2", "-1", "0-", "1,,x"])
def test_invalid_range_fails_loudly(raw):
    with pytest.raises(ValueError):
        parse_episode_range(raw, None)


# --- 转换环境：demo 选择 ---

def test_worker_defaults_to_three_demos():
    module = worker_module()
    assert module.DEMOS == {"demo_0": 59, "demo_1": 58, "demo_2": 57}


def test_worker_reads_environment_variable_at_load():
    module = worker_module("0-9")
    assert len(module.DEMOS) == 10
    assert sum(module.DEMOS.values()) == 531


def test_worker_supports_subset_of_full_range():
    module = worker_module()
    assert module._select_demos("0-1,4") == {"demo_0": 59, "demo_1": 58, "demo_4": 51}


def test_worker_frozen_lengths_cover_full_source():
    """冻结清单必须覆盖固定 test.hdf5 的全部 10 条 demo，合计 531 帧。"""
    module = worker_module()
    assert module._ALL_DEMO_LENGTHS == FULL_DEMOS
    assert sum(module._ALL_DEMO_LENGTHS.values()) == 531


def test_worker_rejects_out_of_range_demo():
    module = worker_module()
    with pytest.raises(module.ConversionError) as excinfo:
        module._select_demos("0-10")
    assert "demo_10" in str(excinfo.value)


def test_worker_rejects_invalid_range():
    module = worker_module()
    with pytest.raises(module.ConversionError):
        module._select_demos("x-y")
