# 隔离示例：ROS 2 MCAP 单传感器切片

这是一个与当前 LeRobot `Episode` / 主工作台**隔离**的最小示例。它从 ROS 2 MCAP 中解析单一 `/sensor/value` topic（`std_msgs/msg/Float64`），输出带来源 SHA-256、采样计数、时间戳摘要与问题列表的 JSON。

## 重要边界

- 仓库当前没有用户真实 ROS bag/MCAP 样本。因此随仓库的 `tests/fixtures/ros2_mcap/synthetic_float64.mcap` 是用 `make_fixture.py` 生成的**合成测试数据**，不是实机采集数据，也不能证明传感器硬件同步。
- 此切片只接受 `std_msgs/msg/Float64` + CDR 编码；不支持任意 ROS 消息、IDL schema、自动 topic/schema 推断或 ROS2 bag 数据库目录。
- 时间戳使用 MCAP `log_time` 纳秒，不改用 publish time，不转换成固定 FPS。读取器明确设为物理文件顺序（`log_time_order=False`），不由适配层按时间重排；重复／倒退时间标为问题，间隔大于正间隔中位数的 1.5 倍标为 gap。它不与当前质检器的固定 FPS 时间规则混用。
- 不生成 `action`，不构造成 `Episode`，不调用主工作台 API，也不更改 LeRobot 转换、数据指纹或交付契约。
- 代码适配的是 MCAP 标准文件格式与 ROS2 `ros2msg` schema；本次验证数据是合成文件。接入真实机器人数据前，需要用户授权的数据来源、topic/type 定义、单位、frame_id、时钟语义与许可核验。

## 安装与运行

```powershell
uv pip install --python .venv\Scripts\python.exe -r examples\ros2_mcap\requirements.txt
.\.venv\Scripts\python.exe examples\ros2_mcap\sensor_slice.py tests\fixtures\ros2_mcap\synthetic_float64.mcap --topic /sensor/value --output docs\benchmarks\ros2-synthetic-mcap-report.json
```

依赖只为示例安装：`mcap==1.6.0`、`mcap-ros2-support==0.6.0`；没有加入主项目 `pyproject.toml` / `uv.lock`。

生成新合成样本：

```powershell
.\.venv\Scripts\python.exe examples\ros2_mcap\make_fixture.py $env:TEMP\sensor.mcap --count 5 --interval-ns 50000000
```

使用自己的兼容 MCAP 文件：

```powershell
.\.venv\Scripts\python.exe examples\ros2_mcap\sensor_slice.py C:\data\robot.mcap --topic /sensor/value --output sensor-report.json
```

当前 CI 基础依赖不包含 MCAP 包；`tests/test_ros2_mcap_example.py` 在可选依赖缺失时会跳过。安装上述 requirements 后本机 MCAP 专项测试为 6 项通过。

## 结果

随仓库的合成 fixture 在本机解码得到 5 条消息、50,000,000 ns 中位间隔、0 时间异常；报告记录样本文件 SHA-256。该结果只验证示例代码的解析与检查行为，不是硬件、数据真实性或生产适配证明。
