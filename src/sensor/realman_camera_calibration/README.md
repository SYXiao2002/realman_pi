# realman_camera_calibration

左右腕相机分别运行 ChArUco eye-in-hand 标定，默认先右臂。每个会话固定一个臂、
相机序列号、安装版本、光学帧及第一张有效样本的内参，不需要另一臂或相机在线。
中臂不参与相机标定。单臂求解后可选择左右会话更新 `three_robots.yaml` 中的右臂布局。

配置由根目录 `config/ros/camera_calibration.yaml` 管理。每次重装相机后修改该
相机的 `mount_id` 并重新采样。一个会话内标定板必须相对所选基座保持静止，换臂
后若重新摆板，这两个结果就不能用于求解臂间相对基座；布局更新所选的两个会话必须使用同一标定板位置，由用户负责选择。

## 接口

- `/camera_calibration/capture_sample`：`CaptureCalibrationSample`，显式 `arm_id: r/l`，
  读取该相机图像、CameraInfo 和图像时刻法兰 TF，原子保存一张 PNG/JSON。
- `/camera_calibration/solve`：`SolveCalibration`，请求 `session_id` 与 `arm_id` 必须匹配，
  仅求解该臂。
- `/camera_calibration/update_layout`：`UpdateCalibrationLayout`，显式指定 `left_session_id`、
  `right_session_id`，从两份已求解结果计算相对基座并写回右臂布局。
- `/camera_calibration/{diagnostics,camera_health}`：输入诊断；另一臂离线不会拒绝当前臂采样。

新 service 使用单数 `arm_id/sample_count/image_path`；旧三臂数组及
`all_arms_solved` 已移除；solve 响应保留 `layout_updated=false` 以表示不会自动写布局，布局更新现在是独立 service。需重建 `realman_msgs` 和调用方。
历史三臂会话保留在磁盘，但没有单臂身份 manifest，拒绝直接恢复为新会话。

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
export REALMAN_CONFIG_ROOT=$PWD/config
ros2 launch realman_camera_calibration camera_calibration.launch.py

ros2 service call /camera_calibration/capture_sample   realman_msgs/srv/CaptureCalibrationSample   "{session_id: '', start_new_session: true, arm_id: r}"

# 后续采样使用同一 session_id、start_new_session: false、arm_id: r。
ros2 service call /camera_calibration/solve   realman_msgs/srv/SolveCalibration   "{session_id: 'session-...', arm_id: r}"
```

默认至少 30 张姿态分散的图像，求解使用该臂会话中的全部已提交样本。
结果位于 `REALMAN_LOG_ROOT/camera_calibration/<session_id>/calibration_result.json`，
含 `identity`、`intrinsics_sha256`、`calibration_id` 和
`hand_eye.<arm>.tool_to_camera`（法兰到彩色光学帧，长度单位米）。求解成功不代表
满足抓取精度，默认 `verified_for_grasp: false`；人工复核和留出数据验收之后再
接入 TF。单臂 solve 不改布局；在 Web 页选择已求解的左右会话并点击“更新布局”，
或调用下方服务。保持左、中臂原值，用 `T_left_board @ inverse(T_right_board)`
计算右臂相对左臂位姿，再乘左臂当前布局写回右臂。原文件保存为 `.yaml.bak`，
原子替换失败返回失败。`layout_config_file` 默认为配置同目录的 `three_robots.yaml`，
必须可写。当前 static TF 不自动重启，重新 bringup 后生效。

```bash
ros2 service call /camera_calibration/update_layout realman_msgs/srv/UpdateCalibrationLayout \
  "{left_session_id: 'session-...', right_session_id: 'session-...'}"
```

并发 capture/solve/update_layout 被拒绝；临时批次不进入求解，采样写入失败不增加计数。
会话恢复会检查臂、相机/安装、board、frames、内参以及每张样本身份，防止跨臂混用。
