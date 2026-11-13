# 项目级启动问题修复记录

状态：待修复。2026-11-11 按用户要求先记录，后续单独实施。本次未验证的修复草稿已撤回，未构建或部署这两项修复。

## FIX-01：宿主相机启动误用虚拟环境 Python

### 现象与证据

用户执行 `./rm65 up` 时，宿主相机进程在导入 `rclpy` 阶段失败：

```text
host_cameras.py -> import rclpy
.../uv/python/cpython-3.12.13-linux-x86_64-gnu/lib/python3.12/importlib/...
ModuleNotFoundError: No module named 'rclpy._rclpy_pybind11'
..._rclpy_pybind11.cpython-312-x86_64-linux-gnu.so isn't present
```

当前宿主使用 Ubuntu 系统 Python 3.10 和 ROS 2 Humble 二进制包。`rclpy` 的 C 扩展针对 Python 3.10 编译；调用终端的 uv Python 3.12 排在 `PATH` 前面时，`start_sensors.sh` 的 `exec python3` 会选择错误解释器，无法加载该扩展。单独 `source /opt/ros/humble/setup.bash` 不能保证解释器版本正确。

影响入口为 `./rm65 up` → `start_sensors.sh` → `src/sensor_bringup/scripts/host_cameras.py`。相机启动失败会阻断依赖 RGB-D 的页面采集；干净终端启动成功不能证明带虚拟环境的终端也能启动。

### 项目级修复方案

- 在宿主 ROS 相机入口使用与当前 Humble 安装匹配的 `/usr/bin/python3`，不依赖用户退出 uv、venv 或 conda。
- 在入口的子进程环境中隔离继承的 `PYTHONHOME`、`PYTHONPATH`、`VIRTUAL_ENV` 与 Python 用户包，再显式加载 ROS 和指定的 Orbbec overlay；检查此处理与现有依赖是否兼容。
- 保证 `ros2` 和相机启动的 Python 子进程也使用兼容解释器，不能只修 supervisor 的最后一行。
- 检查 `rm65`、`functions.zsh` 的相机启动、停止和进程识别路径，保持显式 `REALMAN_ORBBEC_ROS2_SETUP`、ROS domain 和日志配置有效；环境隔离仅作用于项目子进程。
- 增加解释器、ROS 导入失败的清晰诊断，避免将 ABI 不兼容误报为相机包未安装。不通过给 Python 3.12 安装另一份 `rclpy` 绕过当前 Humble 依赖。

### 验收与回归

- [ ] 普通终端和 uv Python 3.12 优先的终端均能通过宿主 ROS 导入检查。
- [ ] 覆盖 venv、继承 `PYTHONPATH`/`PYTHONHOME` 和用户包污染；supervisor 与 Python 子进程均加载系统 Humble 的 `rclpy`。
- [ ] 使用隔离 fixture 测试 `start`/`stop` 入口，不打开真实相机或连接机械臂。
- [ ] 运行 `tests/test_host_cameras.py`、`tests/test_host_camera_lifecycle.py`、`tests/test_rm65_entry.sh` 和 shell 语法检查。
- [ ] 现场从受影响环境验证一次真正的相机冷启动；已有 supervisor 被复用不算覆盖。验证 `./rm65 up`、重复 `up` 与 `down` 的进程管理。
- [ ] 确认配置中启用的相机流就绪，`/6d_pose.html` 可采集 right wrist RGB-D；全过程不发送机械臂运动命令。

## FIX-02：grasp_bridge 启动与吸盘默认工具冲突

### 现象与根因

排查时另发现：右臂在 `config/ros/realman_coordinates.yaml` 中的默认工具为 `tcpsuck`，而 `config/ros/grasp_profiles.yaml` 的夹爪 profile 仍为 `tcpgrip`。`src/grasp/grasp_bridge/grasp_bridge/node.py` 在构造节点、生成 profile 列表和创建相机订阅时调用严格的 `load_profile()`，默认工具不一致使整个 `grasp_bridge` 退出。

这是独立的 bridge 共存问题；FIX-01 中用户提供的 Python 导入错误发生在宿主相机进程。

### 修复边界与验收

- [ ] 将启动配置校验与实时规划所需的默认工具校验分开，允许节点启动、订阅相机和只读审阅，不自动切换控制器工具。
- [ ] 仅在确认只读求解不依赖活动 TCP 后，允许 `evaluation_only` 在另一默认工具下使用；现有采集使用的法兰标定与坐标转换须纳入验证。
- [ ] 实时规划及执行继续严格检查工具身份和驱动状态，不全局关闭 `load_profile()` 的默认工具检查，不修改真实工具几何来消除报错。
- [ ] 添加隔离 ROS 回归：吸盘为默认工具时 bridge 存活、只读流程可用、夹爪实时规划被明确拒绝；夹爪正确选中时原流程保持可用。
- [ ] 运行 grasp bridge 相关测试后，再构建 ROS 镜像并部署验证；页面可打开或容器存活不能代替节点存活检查。

## 后续实施与文档

先完成 FIX-01 的环境隔离和回归，再独立处理 FIX-02。部署前确认当前任务与会话状态；本问题的验证不需要机械臂运动。

实施时更新现有文档，而不是将本记录当作已发布使用说明：

- `website/docs/guide/cameras.md`：宿主 ROS Python 与相机依赖。
- `website/docs/development/startup-entries.md`：统一入口的环境边界。
- `website/docs/troubleshooting.md`：`cpython-312` / `rclpy` 导入错误。
- `website/docs/development/grasping.md`：启动/只读与实时规划的工具校验边界。
- 如启动环境契约变化，更新 `website/docs/reference/cli-and-env.md`。

执行相关项目 skill 要求的测试、网站构建及文档检查，通过后再将本记录状态改为已完成，并记录实际验证范围。
