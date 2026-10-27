# 手眼标定增强实施计划：关节角留痕 · 精度检验 · 自动回放标定

状态：待实施（第 3 点自动回放会执行真实机械臂运动，需低速、清场、急停可及）

## 目标

在 8765 的 `calibration.html` 手眼标定流程中增加三项能力：

1. **样本留痕**：每次“检测并保存当前臂样本”时，除已有的末端位姿外，额外保存当前臂的关节角，供后续自动化回放复用。
2. **精度检验**：新增独立检验模块，要求标定板保持不动、用户手动挪臂采集 10 个不同姿态样本，用已求解的手眼结果反算标定板位姿离散度，给出通过/告警结论。
3. **自动标定（可选）**：在 CAPTURE SESSION 模块新增“自动标定”，用户选择一个历史会话后，机械臂按其中样本记录的关节角逐个 MOVEJ 到位、停稳、自动采样；角点不足只报 warning 并继续下一个。

## 现状（代码事实）

- 采样逻辑在 `realman_camera_calibration/camera_calibration_node.py::_capture_locked`，样本落盘为 `logs/camera_calibration/<session>/batches/<batch>/<sample_id>.json`。
- 样本 JSON 已含末端位姿 `base_to_tool`（图像时间戳处 TF `base_frame → end_effector_frame` 的 4×4 矩阵），但**不含关节角**。
- 标定节点是纯服务节点（capture / solve / update_layout），**不订阅 JointState、不动机械臂**。
- 机械臂运动唯一正规通道：`realman_web_control/web_control_node.py` 的 `execute_motion` → 经 input-mode 路由器（控制权仲裁）+ `task_token` 租约 → `/{arm}/execute_motion`（`ExecuteMotion` action，含 `MOVEJ/MOVEL`、`velocity_percent`、`blend_radius_percent`）。运动是异步 action，结果经 `_goal_response`/`_action_result` 回调返回。
- 前端 `calibration.ts` 与 capture 走同一条 `/ws`；capture/solve/layout 通过 web_control_node 转发到标定服务。

## 边界与安全约束

- 标定节点保持“不动臂”职责；**只有 web_control_node 的自动回放编排允许驱动机械臂**。
- 自动回放必须走现有 input-mode 控制权通道；若遥操作/行为树持有控制权则拒绝启动。运动全程携带 `task_token`。
- 回放使用 **MOVEJ 到记录的关节角**，不使用 MOVEL 反解 IK，避免构型歧义/奇异导致的大幅意外运动。
- 回放低速执行：`velocity_percent` 取配置值（默认 5~10%），`blend_radius_percent = 0`、`connect = false`，每点完全停稳再采样。
- 回放产生的样本写入**新会话**，绝不覆盖被回放的历史会话（历史会话为不可变 provenance）。
- 检验样本进入**标记为 verify 的会话**，不出现在 solve / update_layout 候选列表，不参与正式标定。
- 会话身份强校验：`arm_id`、相机 `intrinsics_sha256`、board 必须与当前一致，跨臂/跨相机/跨 board 一律拒绝。
- 遵循项目配置集中化约定：新增参数写入 `config/ros/camera_calibration.yaml` 并附注释；急停可及、清场、低速为硬性操作前提。

## 数据流

```text
[第1点 留痕]
/{arm}/joint_states + Image + CameraInfo + TF
  -> camera_calibration_node（最近邻关节角 + base_to_tool 分解）
  -> 样本 JSON 增加 joint_degrees / end_effector_pose

[第2点 检验]
已求解会话 tool_to_camera + verify 会话 10 样本(base_to_tool, target_to_camera)
  -> VerifyCalibration 服务（base_to_board_i 离散度 + 重投影误差）
  -> web_control_node 转发 -> calibration.html VERIFY 面板

[第3点 自动回放]
历史会话样本 joint_degrees
  -> web_control_node 回放状态机（控制权 -> MOVEJ -> 停稳 -> capture_sample）
  -> 新会话样本 + auto_calibration_progress 事件 -> calibration.html
```

## 分阶段任务

### A. 第 1 点：样本保存关节角 + 显式末端位姿

- [ ] `camera_calibration_node.py`：为每个标定臂订阅 `/{arm}/joint_states`（`qos_profile_sensor_data`），维护每臂最新关节角缓存（name→position、时间戳），用 `self._lock` 保护。
- [ ] `_capture_locked`：取图像后选取与图像时间戳最近邻的关节角（超过最大允许时间差记 warning 但仍写入）。
- [ ] 样本 JSON 新增字段：`joint_degrees`（度，joint_1..6 固定顺序）、`joint_names`、`joint_stamp_ns`、`joint_age_sec`、`end_effector_pose`（`position_m` + `quaternion_wxyz`，由 `base_to_tool` 分解）；保留 `base_to_tool` 不变。
- [ ] `session.py::restore_session`：对新字段做“存在则校验、缺失则容忍”的向后兼容处理，老会话仍可加载与 solve。
- [ ] `config/ros/camera_calibration.yaml`：`robots.<arm>` 补充 `joint_state_topic`、`joint_names`（或约定用消息自带 name 排序），附注释。
- [ ] 测试：`test_session.py` 覆盖新字段兼容校验；`test_node_callbacks.py` 覆盖关节角缓存与样本字段写入。

### B. 第 2 点：精度检验模块（手动挪臂，10 样本）

- [ ] 新增 `realman_msgs/srv/VerifyCalibration.srv`：请求 `arm_id`、`solved_session_id`、`verify_session_id`；响应 `success`、`sample_count`、`translation_spread_m`、`rotation_spread_rad`、`mean/max_reprojection_error_px`、`passed`、`result_json`、`message`；在 `CMakeLists.txt` 注册。
- [ ] `camera_calibration_node.py`：新增 `/camera_calibration/verify` 服务，加载已求解结果取 `tool_to_camera`（复用 `_update_layout` 的 hash/identity 校验），对 verify 会话样本算 `base_to_board_i = base_to_tool_i @ tool_to_camera @ target_to_camera_i`，用 `mean_transform_and_spread` 得离散度 + 重投影误差统计，与阈值比较得 `passed`。
- [ ] 会话隔离：manifest 增加 `purpose: "verify"` 标记（`session.py` 支持），verify 会话不进 solve/layout 候选；样本数下限用检验阈值（10），不复用 `minimum_samples_per_arm`。
- [ ] `config/ros/camera_calibration.yaml`：新增 `verification` 段（`sample_count=10`、`maximum_translation_spread_m`、`maximum_rotation_spread_rad`、`maximum_reprojection_error_px`），阈值可比 solver 更严，附注释。
- [ ] `web_control_node.py`：新增 verify client 与消息类型 `verify_calibration`，结果以 `calibration_verify_result` 事件推送（对齐 `calibration_solve_result` 模式）。
- [ ] 前端：`calibration.html`/`calibration.ts`/`calibration.css` 新增 VERIFY 面板——选基准已求解会话、`已采 x/10` 进度、复用 capture 逻辑但目标为 verify 会话、提示“板不动、手动挪臂到不同姿态再采样”、“计算精度”按钮、指标表 + 通过/告警状态。
- [ ] 测试：verify 服务纯计算单测（构造离散度已知的样本）；web protocol 转发测试。

### C. 第 3 点：服务端自动回放编排

- [ ] `web_control_node.py`：新增回放**状态机**（契合异步 action/service + `_drain_commands` 定时器，非阻塞循环），新消息类型 `run_auto_calibration`、`stop_auto_calibration`。
- [ ] 启动前校验：读取历史会话样本（`REALMAN_LOG_ROOT/camera_calibration/<session>/batches/*/*.json`，复用 `restore_session` 身份校验）；校验 `arm_id`/`intrinsics_sha256`/board 一致；样本须含 `joint_degrees`，否则拒绝并提示“老会话无关节角”。
- [ ] 获取控制权：经 `_input_modes` 拿到该臂控制权/`task_token`；被占用则拒绝启动。
- [ ] 单点序列：`MOVEJ`+记录关节角发 `execute_motion`（经 `_reserve_action`/`_send_goal`，复用限位校验/租约/事件流），低速、`blend_radius=0`、`connect=false` → `_action_result` 判 `SUCCEEDED` 进 settle，异常/限位拒绝记 warning 跳下一个 → 一次性定时器停稳 `settle_sec`（默认 1.0s） → 调 `capture_calibration_sample` 写入新会话 → 角点不足记 warning 继续。
- [ ] 守卫与中止：相邻样本关节角跳变超 `max_joint_jump_deg` 告警/跳过；用户 stop、motion 异常、连续 N 次相机不健康、控制权/租约丢失、急停（复用 `_result_transport_event` 识别）时立即停、臂保持当前姿态、释放控制权。
- [ ] 事件：`auto_calibration_progress`（i/N、每点 success/warning/skip、累计统计）、`auto_calibration_done`（汇总），复用现有 `error`。
- [ ] `config/ros/camera_calibration.yaml`（或 web-control 配置）：新增 `auto_calibration` 段——`velocity_percent`(5~10)、`settle_sec`(1.0)、`max_consecutive_camera_failures`、`max_joint_jump_deg`、可选 `transit_pose`，附注释。
- [ ] 前端：CAPTURE SESSION 面板新增“自动标定（可选）”子区——选可回放历史会话（`recoverable` 且含关节角）、开始按钮、进度列表 + 实时 warning、醒目“停止”按钮、安全提示文案；回放完成后刷新历史会话列表，用户可对新会话执行 solve。
- [ ] 测试：回放状态机单测（mock action/service，覆盖成功、角点不足跳过、motion 异常、中止、控制权丢失）；协议与事件测试。

## 验收标准

- 新采样本 JSON 含 `joint_degrees` 与 `end_effector_pose`；老会话仍可正常加载与 solve，不因缺字段报错。
- 检验模块：板不动、手动采满 10 个不同姿态样本后可计算并显示平移/旋转离散度与重投影误差，给出通过/告警；verify 会话不出现在 solve/layout 候选。
- 自动标定：选历史会话后机械臂以 MOVEJ、低速、逐点停稳回放并自动采样，角点不足只报 warning 并继续；样本进新会话不覆盖历史；无关节角的老会话被拒绝；被占用控制权时拒绝启动；stop/异常/急停能立即安全停止并释放控制权。
- 所有新增参数集中在 `config/ros/camera_calibration.yaml` 且有注释；相关 Python 测试、`npm run build:web-control` 与网站 style gate 通过。

## 待确认

- 检验阈值（`verification` 段）具体数值，是否比 solver 阈值更严。
- 自动回放是否需要 `transit_pose` 过渡姿态（默认按记录顺序直接关节空间移动）。
