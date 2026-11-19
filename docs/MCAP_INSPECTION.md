# ROS 2 MCAP 检查（inspection-only）

v0.15 提供的是 **MCAP 检查适配器**，不是 MCAP 转换器。它能回答：

- 这是不是一个可读的 `.mcap`；
- 里面有哪些 topic、消息类型、消息数量；
- 如果提供了 `robodata_mcap.json` 映射，它引用的 topic 是否真的存在。

它**不**回答：

- 哪个 topic 是 state、哪个是 action、相机怎么对齐；
- 时间戳该用 `log_time`、`publish_time` 还是 `header.stamp`；
- QoS、clock、frame_id 和硬件同步语义；
- 如何把它转成 LeRobot。

这些必须来自真实数据和冻结的映射配置，不能由工具猜。

## 安装可选依赖

```powershell
uv pip install --python .venv\Scripts\python.exe -r examples\ros2_mcap\requirements.txt
```

依赖：`mcap==1.6.0`、`mcap-ros2-support==0.6.0`。没有安装时，适配器会识别扩展名并提示安装；不会假装检查成功。

## 命令

```powershell
uv run --locked robodata adapters detect --input C:\data\robot.mcap
uv run --locked robodata adapters inspect --input C:\data\robot.mcap
```

`adapters list` 会把 `mcap` 标为 `inspection_only: true`；`import-batch --source auto` 不会选它，显式导入会明确拒绝。

## 可选映射：`robodata_mcap.json`

放在 MCAP 同目录，或命名为 `<file>.robodata.json`：

```json
{
  "schema_version": 1,
  "profile": "robodata_mcap_v1",
  "timestamp": "log_time",
  "state": {"topic": "/joint_states", "schema": "sensor_msgs/msg/JointState", "field": "position"},
  "action": {"topic": "/commands", "schema": "std_msgs/msg/Float64MultiArray", "field": "data"},
  "cameras": [
    {"name": "observation.images.camera_0", "topic": "/camera0/image_raw/compressed",
     "schema": "sensor_msgs/msg/CompressedImage", "encoding": "jpeg"}
  ]
}
```

当前只校验 `state.topic`、`action.topic`、`cameras[].topic` 是否存在；不会读取消息内容，也不会执行时间对齐。映射配置不是“已支持转换”的声明。

## 已验证范围

- 合成 fixture：`std_msgs/msg/Float64` topic 能被 `adapters detect` 识别，schema 与消息计数可见；
- 映射引用不存在的 topic 时，`preflight` 返回 `profile_missing_topics`；
- 仓库**没有**真实 ROS 2 MCAP 样本，也没有外部复现记录。

## 什么时候升级为可转换

至少需要：

1. 用户授权的真实 MCAP 样本；
2. 冻结的 topic/schema/单位/frame_id 配置；
3. 明确的时间戳策略与 QoS 边界；
4. 一个 golden 样本 + 坏样本 + 未知 schema 测试；
5. 转换 worker 与官方 loader 验证。

在这些条件满足之前，`mcap` 保持 inspection-only。这是边界，不是“计划中”的模糊承诺。
