---
title: ROS 2 接口总表
description: realman_pi 所有对外 ROS 2 节点、Action、Service 和 Topic 的速查表，并指向各自的契约页面。
---

# ROS 2 接口总表

本页是**速查表**：每一行只回答"接口叫什么、类型是什么、谁拥有它"，字段语义、单位、安全约束和失败方式见链接的专题页。接口以代码为准；新增或修改接口时，同一次提交必须更新本页和对应专题页。

约定：通用 `<arm>` 支持 `l`、`m`、`r`（左/中/右），当前生产只启用 `l/r`；`m` 保留为历史布局与模型参考，没有真实驱动接口。Action、Service、Topic 全部带该 namespace。驱动实例由 `realman_driver.yaml` 中存在的完整节点 section 决定。`msg`/`srv`/`action` 类型来自 [`src/driver/realman_msgs/`](https://github.com/QingTianRobot/realman_pi/tree/main/src/driver/realman_msgs) 和 [`src/gripper/gripper_ros2_msgs/`](https://github.com/QingTianRobot/realman_pi/tree/main/src/gripper/gripper_ros2_msgs)。

## 机械臂驱动 `/<arm>/realman_driver`

拥有者：`realman_robot_driver`。契约：[三臂驱动与运动控制](../development/realman-driver-scaffold)、[Action 开发与测试](../development/realman-action-development)。

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `/<arm>/joint_states` | `sensor_msgs/JointState` | 弧度；未连接时不发布 |
| `/<arm>/connected` | `std_msgs/Bool` | 连接生命周期，不等于"状态新鲜" |
| `/<arm>/coordinates/state` | `std_msgs/String`（JSON） | 坐标校验结果；reliable + transient-local，低频重发 |
| `/<arm>/cartesian_velocity/state` | `realman_msgs/CartesianVelocityState` | 指令速度与实测速度遥测 |
| `/<arm>/execute_motion` | Action `ExecuteMotion` | `MOVEJ` / `MOVEL` / `MOVEJ_P`，可取消 |
| `/<arm>/execute_trajectory` | Action `ExecuteTrajectory` | 2–256 个连续路点，整条队列占有 ownership |
| `/<arm>/cartesian_velocity` | Action `CartesianVelocity` | 开放式六轴速度 session（仅 WORK / TOOL） |
| `/<arm>/cartesian_velocity/command` | `geometry_msgs/TwistStamped`（订阅） | 刷新 session 的最新速度；非零且单调递增的 stamp |
| `/<arm>/cartesian_pose` | Action `CartesianPose` | 绝对位姿 session：IK + CANFD 关节透传 |
| `/<arm>/cartesian_pose/command` | `geometry_msgs/PoseStamped`（订阅） | 刷新位姿 session 的最新目标（base frame） |
| `/<arm>/connect` `disconnect` `stop` `status` | `std_srvs/Trigger` | 连接生命周期、软件受控停止、状态 |
| `/<arm>/task_control` | `realman_msgs/TaskControl` | 按臂获取、续期、释放/撤销与查询任务租约；同一 token 覆盖机械臂和夹爪 |
| `/<arm>/recover_motion` | `realman_msgs/RecoverMotion` | 取消后重建事件通道；不发送运动 |
| `/<arm>/controller_info` | `std_srvs/Trigger` | 只读控制器信息（JSON），含版本、关节/TCP、错误、当前轨迹及控制器状态 |
| `/<arm>/get_current_pose` | `realman_msgs/GetCurrentPose` | 关节 FK → 当前位姿（按已验证参考系） |
| `/<arm>/forward_kinematics` | `realman_msgs/ForwardKinematics` | 任意关节角的 FK |
| `/<arm>/solve_ik` | `realman_msgs/SolveIk` | 参考系目标 → 关节解（度） |
| `/<arm>/coordinates/verify` `apply` | `realman_msgs/VerifyCoordinates` | 只读回查 / 显式写入默认工具与工作坐标 |
| `/<arm>/coordinates/select_tool` `select_work` | `realman_msgs/SelectFrame` | 选择配置内的工具/工作坐标并回读 |

抓取任务通过 `/<arm>/task_control`（`realman_msgs/TaskControl`）获取、续期和释放任务租约。运动 Action 新增 `task_token`；任务持有期间，驱动拒绝其他 owner 的动作和坐标写入，对应夹爪也由同一 admission 检查。无任务时沿用原入口；`stop` 保持紧急入口。租约过期不会自动解除未确认结束的命令占用。详见[腕相机抓取](../development/grasping)。

同一时刻一只臂只允许一个运动 owner；`stop` 抢占 ownership。`reference_type` 为 `BASE=0` / `WORK=1` / `TOOL=2`，速度 session 拒绝 BASE。

## 行为树与输入路由 `/realman_bt_executor`

拥有者：`realman_bt`。契约：[行为树控制权与 Mock 测试](../development/behavior-tree-control)、[Pika 遥操作](../development/pika-teleop)。

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `/realman_bt_executor/list_input_modes` | `realman_msgs/ListInputModes` | 按 XML 顺序发现输入模式目录 |
| `/realman_bt_executor/select_input_mode` | `realman_msgs/SelectInputMode` | 请求模式（`mode_id` + 非空 `requester_id`） |
| `/realman_bt_executor/input_mode_state` | `realman_msgs/InputModeState` | `ACTIVE` / `SWITCHING` / `FAILED` 及 epoch |
| `/realman_bt_executor/bt_status` | `std_msgs/String` | 执行器状态 |
| `/realman_bt_executor/start` `stop` | `std_srvs/Trigger` | 执行器生命周期 |
| `/keyboard/<l\|r>/cartesian_velocity` | `geometry_msgs/TwistStamped` | Web 键盘 ingress（由 `realman_web_control` 发布） |
| `/keyboard/<l\|r>/gripper_command` | `std_msgs/String`（JSON） | 键盘夹爪事件（epoch / request 去重） |
| `/pika/<l\|r>/cartesian_pose` | `geometry_msgs/PoseStamped` | Pika 位姿 ingress（base frame） |
| `/pika/<l\|r>/cartesian_velocity` | `geometry_msgs/TwistStamped` | Pika 速度 ingress（`work/pikabase` frame） |
| `/pika/<l\|r>/gripper_percentage` | `std_msgs/Float32` | `0` 闭合 … `1` 张开 |

输入模式 ID（顺序以 [`control.xml`](https://github.com/QingTianRobot/realman_pi/blob/main/config/behavior-trees/control.xml) 为准）：`web`（粘性、最高优先级、不可由浏览器选择）、`keyboard`、`policy`、`pikaposition`、`pikavelocity`、`pikamixed`、`none`。

## 夹爪 `gripper_manager`

拥有者：`gripper_ros2`，独占串口。契约：[夹爪控制](../development/gripper-control)。生产配置的 `<name>` 为 `gripper_left`、`gripper_right`，左右共用 42D 总线；中臂无夹爪、无对应接口。

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `/<name>/open` `close` `reset` `grasp_check` `calibrate` | `std_srvs/Trigger` | 同步动作；42D 不支持 reset/calibrate/grasp_check，返回失败 |
| `/<name>/task_command` | `realman_msgs/TaskGripperCommand` | 携带 `task_token` 的 OPEN/CLOSE；只确认命令完成，不表示物体抓稳 |
| `/<name>/enable` | `std_srvs/SetBool` | 42D 为串口模式准备 / 软件命令门控，失能不表示硬件停止 |
| `/<name>/percentage` | `gripper_ros2_msgs/GripperPercentage` | 一次性同步目标（`0..1`） |
| `/<name>/percentage/command` | `std_msgs/Float32`（订阅） | 非阻塞目标；左右 42D 按阈值/回差转为开闭并去重 |
| `/<name>/set_limits` | `gripper_ros2_msgs/SetGripperLimits` | 校验、保存并立即应用开位/闭位（写入 `gripper_overrides.yaml`） |
| `/<name>/move_raw` | `gripper_ros2_msgs/MoveGripperRaw` | `min..max` 内的原始目标，越界拒绝；42D 仍转为开/闭 |
| `/<name>/limits` | `gripper_ros2_msgs/GripperLimits`（发布） | 当前生效的开/闭位与 `min/max`；reliable + transient-local；Web 与策略桥订阅它 |
| `/<name>/position` `speed` `current` `torque_reached` `alarm` `connected` | `Float64` / `Int32` / `Int32` / `Bool` / `Int32` / `Bool` | 每设备反馈；42D position 为状态合成端点，speed/current/alarm 为 0、torque_reached 为 false 占位；connected 表示该地址状态读取成功 |

## Web 控制 `realman_web_control`

HTTP / WebSocket `:8765`，浏览器不直接访问 ROS。它调用上面的 Action / Service、订阅 `joint_states`、`coordinates/state`、夹爪状态、`camera_health` 和 `input_mode_state`，并发布键盘 ingress。契约：[WebSocket 浏览器控制与 URDF 影子](../development/realman-web-control)。

## 相机与标定

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `/camera_left/color/image_raw`、`/camera_right/color/image_raw`，及对应 `color/camera_info` | `sensor_msgs/Image`、`CameraInfo` | 左 / 右 Gemini 305 腕部相机 |
| `/camera_global/color/image_raw`、`/camera_global2/color/image_raw`，及对应 `color/camera_info` | `sensor_msgs/Image`、`CameraInfo` | 后 / 前 Gemini 335L 全局相机 |
| `/camera_left/depth/image_raw`，及对应 `depth/camera_info` | `sensor_msgs/Image`、`CameraInfo` | 左腕 Gemini 305（`CV2856D0008H`）原始深度；设备内帧同步开启 |
| `/camera_right/depth/image_raw`，及对应 `depth/camera_info` | `sensor_msgs/Image`、`CameraInfo` | 右腕 Gemini 305（`CV2856D0002E`）原始深度；`depth_registration=false` |
| `/camera_global/depth/image_raw`，及对应 `depth/camera_info` | `sensor_msgs/Image`、`CameraInfo` | 后全局 Gemini 335L（`CP28563000CK`）原始深度；`depth_registration=false` |
| `/camera_calibration/capture_sample` | `realman_msgs/CaptureCalibrationSample` | 选定 `arm_id`，单臂原子采样图像 + 图像时刻法兰 TF；`purpose: verify` 标记固定板检验样本，隔离出求解/布局候选 |
| `/camera_calibration/solve` | `realman_msgs/SolveCalibration` | 按 `arm_id/session_id` 求解独立手眼；拒绝 `purpose: verify` 会话 |
| `/camera_calibration/verify` | `realman_msgs/VerifyCalibration` | 用已求解 `baseline_session_id` 对固定板 `verify_session_id` 反算标定板位姿离散度，写 `verification_result.json` 并返回 `passed` |
| `/camera_calibration/update_layout` | `realman_msgs/UpdateCalibrationLayout` | 用户选择 `left_session_id/right_session_id`，更新右臂布局并备份 YAML；重新 bringup 后基座 TF 生效 |
| `/camera_calibration/camera_health` | `std_msgs/String`（JSON） | 每秒发布，供 Web 标定页面使用 |
| `/camera_calibration/diagnostics` | `diagnostic_msgs/DiagnosticArray` | 标定节点诊断 |

契约：[单臂 ChArUco 手眼标定](../development/camera-calibration)、[相机指南](../guide/cameras)。

## 数据录制 `realman_recording`

只订阅驱动与夹爪输出，不创建 SDK 客户端，不发布运动命令。契约：[独立数据录制平台](../development/recording-platform)。

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `/recording/manage` | `realman_recording_msgs/ManageRecording`（Service） | 唯一的会话控制入口：`PREPARE` / `START` / `STOP` / `ADOPT` / `DISCARD` |
| `/recording/status` | `realman_recording_msgs/RecordingStatus` | 网页状态的唯一权威来源（倒计时、session、导出状态） |
| `:8770` HTTP / WebSocket | — | 实时只读页与 LeRobot 回放页；`/api/lerobot/*` 见专题页 |

## VLA 策略桥 `policy_bridge`

纯协议转换：OpenPI WebSocket ↔ ROS 2。契约：[VLA 策略桥接节点](../development/policy-bridge)。

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `/pi05_policy/<l\|r>/cartesian_velocity` | `geometry_msgs/TwistStamped` | 50 Hz，仅在 active 且模式门控通过时发布 |
| `/pi05_policy/<l\|r>/gripper_percentage` | `std_msgs/Float32` | `0..1` |
| `/policy/activate` `deactivate` | `std_srvs/SetBool` | 生命周期；`emergency_stop` 与 `force_infer` 为 `Trigger` |
| `/policy/stats` | `diagnostic_msgs/DiagnosticArray` | 计数与实时指标；暂停时 `ERROR` |

## 腕相机抓取 `grasp_bridge`

`PlanGrasp.cgn_context` 为 `mask`、`box` 或 `full`，空字符串使用 bridge 配置；`evaluation_only=true` 始终只读，不调用 IK 或产生 waypoint。实机 BT 规划沿用同一份 profile、标定、TCP 和夹爪配置，不要求人工 `verified` 标记或固定 model ID；配置结构和服务身份仍会校验。修改 Action 后需一起重建其所有 client/server。

纯规划接口，执行由 BT 编排。契约：[腕相机抓取](../development/grasping)。

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `/grasp_bridge/plan_grasp` | `realman_msgs/PlanGrasp`（Action） | 不可变观测 → WS 候选 → 抓取几何与 waypoint；按 `grasp_bridge.yaml:ik_precheck` 可选受限 IK；可取消，不发送运动/夹爪命令 |
| `/grasp_bridge/diagnostics` | `diagnostic_msgs/DiagnosticArray` | 采集、推理、筛选、配置拒绝 |
| `/grasp_bridge/plan` | `std_msgs/String`（JSON） | 带 observation/profile/expiry 和 `timings_ms` 分阶段耗时的规划结果 |
| `/grasp_bridge/candidates`、`overlay` | `geometry_msgs/PoseArray`、`sensor_msgs/Image` | 彩色光学坐标系候选与叠加图 |
| `/grasp_bridge/evaluation` | `std_msgs/String`（JSON） | 只读原子快照：离线评估含 RGB/深度显示、点云、对齐矩阵、readiness 与候选 `collision`（双指盒体、四阶段间隙/穿透和显示 mask）；行为树规划含候选结论/拒绝原因、waypoints 与 `workspace_m`（TCP 走廊）及可选 `collision_workspace_m`（更宽的碰撞检测区，省略图片）；按 `observation_id` 和采集时 `T_base_camera` 绑定，供 `/grasp_plan.html` 展示；`base_camera_transform` 记录 TF/外参来源与失败原因，`server_diagnostics.skipped_instances` 记录深度不足实例 |
| `/grasp_bridge/review_status` | `std_msgs/String`（JSON） | 当前 bridge 的 dry-run、profile 和左右腕 profile 目录 |
| `127.0.0.1:8091` WebSocket | MessagePack | 独立 `grasp_server`，输入对齐 BGR/RGB-D；返回原矩阵/分数/分割与溯源；无 ROS |

## 单帧 6D Pose 与吸盘 `pose6d_bridge`

页面为 `/6d_pose.html`；`./rm65 up` 默认启动 bridge，也可通过主 Compose 的 `pose6d` profile 独立管理。ROS 命令/事件通过 Web control 转发，基础配置和运动状态见[单帧 6D Pose 与吸盘](../development/grasping#单帧-6d-位姿开发状态)。

| 名称 | 类型 | 说明 |
| --- | --- | --- |
| `/pose6d_bridge/command` | `std_msgs/String`（JSON） | 页面请求采集、推理、准备位、有符号步进、停止、会话关闭与心跳；运动命令只接受观测/会话 ID、吸取方式和 step mm，不接受任意目标 pose。Web control 每秒及转发请求前发送内部 `clients` 在线连接快照（`client_ids`），供 bridge 清理断线归属；该命令不能通过浏览器协议提交 |
| `/pose6d_bridge/events` | `std_msgs/String`（JSON） | 请求关联的采集、位姿/告警/点云/OBJ/吸盘结果、运动状态和错误；结果包含切向参考及 +40 mm 接触/TCP 准备位。另以 5 Hz 广播 `pose6d_frames`（BASE 下实时 TCP/相机位姿及可用状态），供网页转换到冻结采集相机系。采集、推理和遥测均含 `base_frame`、运动配置的 `workspace_m`（BASE 系 `[[min x,y,z], [max x,y,z]]`，米），供可开关工作区线框使用；事件上限为 8 MiB |
| `/<arm>/get_current_pose` | `realman_msgs/GetCurrentPose` | 按当前配置工具读 BASE TCP，用于运动前/后的新鲜停稳读回 |
| `/rm65b_analytic_ik/solve_all` | `realman_msgs/SolveAnalyticIk` | RM65-B 只读解析 IK。请求为行优先的 16 元素 `target_base_tcp`（机械臂自身 BASE→TCP）和 `flange_tcp`（法兰→工具 TCP），平移单位米；内部转换为 `Rz(yaw) Ry(pitch) Rx(roll)` 的 xyzrpy，调用 `solve_all()`。响应含 `success`（检查是否完成）、`reachable`、`valid_solution_count`、`reason`、按六关节一组展开的 `solutions_rad`（弧度）。`success=false` 应展示未知；完成但无解时 `success=true, reachable=false`。`pose6d_bridge` 对每个最终吸取 TCP 调用一次，并在 `grasps[].analytic_ik` 转发可解性、解数与原因，不执行运动或路径检查 |
| `/<arm>/task_control` | `realman_msgs/TaskControl` | 在准备位及点动会话期间持有同一 per-arm 任务租约 |
| `/<arm>/tool_power`、`tool_power_status` | `std_srvs/SetBool`、`std_srvs/Trigger` | 手动设置 24 V / 0 V 和只读查询；设置后回读确认，持有任务租约时拒绝无令牌写入 |
| `/<arm>/tool_power_control` | `realman_msgs/SetToolPower` | `enabled` 加 `task_token`；驱动验证当前任务租约并回读输出。6D 自动动作开始前开 24 V，确认提升完成后关 0 V |
| `/<arm>/execute_motion` | Action `realman_msgs/ExecuteMotion` | 仅在配置启用后使用 BASE `MOVEL`；成功终态及实际 TCP 到位读回后才提交当前距离 |
| `/<arm>/stop` | `std_srvs/Trigger` | 用户停止、浏览器断线/隐藏或 watchdog 触发的受控停止；未知 Action 结果保留任务租约 |
| `/<arm>/coordinates/state`、`joint_states` | `std_msgs/String`、`sensor_msgs/JointState` | 校验活动 controller tool 与坐标状态，并验证新鲜、停稳的关节反馈 |
| `127.0.0.1:8092` WebSocket | MessagePack | 独立 FoundationPose 单帧 `register()` 与几何筛查服务；容器无 ROS/机器人控制接口 |

`grasps[].tcp_targets` 由 bridge 提供 `{frame, suction: {xyz_mm, rpy_deg}, prepare: {xyz_mm, rpy_deg}}`，用于 TCP 数值展示；缺少目标时对应项为 `null`。有采集手眼变换时使用 BASE，否则使用相机系。解析节点与 bridge 由 `ros2 launch pose6d_bridge pose6d_bridge.launch.py` 一起启动，模型参数来自 `config/ros/rm65b_analytic_ik.yaml`，可通过节点参数 `config_path` 指定其他文件。`REALMAN_CONFIG_ROOT` 生效时从其 `ros/` 子目录加载模型。

`grasps[].suction_collision` 在解析 IK 确认可达后由 bridge 本地计算，不增加 ROS 服务。字段包括 `status`（`collision/clear/unknown/skipped`）、`reason`、`elapsed_ms`；有效检测还提供相对接触点的 `start_distance_m/end_distance_m`（当前 0.01/0.04）、实际圆柱 `length_m/diameter_m`（当前 0.03/0.01）、`collision_pixels`、`largest_cluster_pixels`、`roi_pixels` 和可见证据比例 `observed_fraction`。`collision` 在页面标记“吸取路径上有物体”；碰撞与未知状态都只展示，候选保留原 workspace 与人工复核规则，运动入口不增加拦截。包络覆盖相机系标注接触点外沿法向的 10–40 mm，两端包含，直径复用当前吸盘工具配置。检测不读取 Mask，目标自身也参与；不再提供旧的目标/环境分类计数。缺少深度、遮挡或走廊不在视野内时不能解释为无障碍。

## TF 与描述

`world → <arm>/world → <arm>/base_link → <arm>/link_1 … <arm>/link_6`，另有每臂 `<arm>/robot_description`（transient-local）。工作/工具坐标见 `config/ros/realman_coordinates.yaml`（`l/work/cell`、`l/work/pikabase` 等）。详见[完整 TF 树](../architecture/tf-tree)。

## 查询当前运行图

```bash
ros2 node list
ros2 action list -t | grep -E '/(l|m|r)/'
ros2 service list -t | grep -E '/(l|m|r)/'
ros2 topic info /l/joint_states -v      # 每个 joint_states 必须只有一个发布者
```

排查"同一 Action 出现多个 server"见[行为树机械臂移动 Demo](../development/behavior-tree-motion#重复执行与-unknown-排查)。
