---
title: CLI 与环境变量
description: ./rm65 统一入口、行为树启动器、.env 与 Compose 环境变量、服务端口和日志位置的速查表。
---

# CLI 与环境变量

## `./rm65` 统一入口

在仓库根目录执行。它把宿主机上的 ROS 2 相机进程和 Docker 里的机械臂/Web 服务合并成一个生命周期；日常生产操作优先使用它，`functions.zsh` 中的 `rm65_*` 函数用于专项调试（[启动入口索引](../development/startup-entries)）。

| 命令 | 作用 |
| --- | --- |
| `./rm65 build [service ...]` | 默认重建 `realman_bringup_remote` 与 `realman_web_control`，可指定如 `rm65_rviz`；使用 Bake 显式授权 `network.host` 并载入本机镜像，不改变容器状态 |
| `./rm65 up` | 生产默认：四路宿主机 Orbbec RGB + 配置启用的右腕、后全局原始 depth（所有启用流首帧确认后启动 Docker）+ 左右真实驱动和两只 42D + Web control + 数据录制 recorder 与回放网页（`:8770`）+ 标定健康诊断；无 RViz，不需要 `DISPLAY`。相机首帧失败会清理相机；之后 Compose 启动失败会保留相机流并返回失败 |
| `./rm65 up desktop` | 同上，并启动 RViz-only 的远程查看服务 `realman_remote_rviz` |
| `./rm65 up policy` | 生产图 + VLA 策略桥容器 `policy_bridge`（等待模式路由选中 `policy` 并调用 `/policy/activate`） |
| `./rm65 up model` | 离线三臂模型 + RViz，不连接真机 |
| `./rm65 rviz2` | 设置 `DISPLAY=:1`、`XAUTHORITY=/run/user/1000/gdm/Xauthority`，加载仓库 `functions.zsh` 并调用 `rm65_docker_camera_rviz`，前台显示四路相机 RGB |
| `./rm65 down` / `status` / `logs` | 停止服务并等待受管相机 launch 退出、释放 USB；或查看状态、最近日志（含相机日志） |
| `./rm65 bt <l\|m\|r\|three\|control\|name>` | 在**已运行**的 `realman_bringup_remote` 容器内执行行为树；默认 dry-run |
| `./rm65 bt-test build\|mock\|web\|all` | 构建、运行或校验隔离的行为树 mock 场景 |
| `./rm65 sync` | 校验 `main` 与 clean worktree 后 push，并 rsync 已跟踪文件到生产端 |
| `./rm65 camera` | 旧 RTSP/TCP 相机助手（已弃用，与 ROS 2 相机互斥） |

`./rm65 up` 在 `realman_driver_rviz` 仍运行时拒绝启动：两个服务都会连真机，会让同一 namespace 出现重复驱动和多个 `cartesian_velocity` Action server。`./rm65 up` 的内置 usage 文本目前没有列出 `policy`，以本表为准。

生产多臂驱动只启动 `config/ros/realman_driver.yaml` 中存在的完整节点 section，当前是 `/l/realman_driver`、`/r/realman_driver`；省略 `/m/realman_driver` 即不启动中臂。通用三臂布局、mock 和 `bt m|three` 入口仍保留，涉及中臂的真实任务需要相应硬件与配置，不能直接用于当前双臂部署。

相机串号、彩色/深度分辨率与帧率、逐机 `enable_depth` 由 `config/ros/cameras_ros2.yaml` 和可选本地覆盖决定，见[相机指南](../guide/cameras)。修改后先执行 `bash start_sensors.sh stop` 再 `./rm65 up`；重复 `up` 复用当前相机进程，不会热更新配置。专项入口 `rm65_camera_ros2 color` 使用同一配置；显式 `depth` 切换为全部已启用设备的 depth-only 模式。

### 行为树启动器 `./rm65 bt`

| 形式 | 解析到 |
| --- | --- |
| `l` / `m` / `r` | `move.xml`，对单臂 `l`/`m`/`r` 执行 MoveJ |
| `three` | `three.xml`，三臂分阶段 |
| `control` | `control.xml`，**持久**输入路由器，Ctrl-C 前一直运行 |
| `<name>` 或 `<name>.xml` | `config/behavior-trees/<name>.xml` |

启动器通过 Docker 标签 `com.docker.compose.service=realman_bringup_remote` 找到**唯一**运行中的容器，找不到或有多个都会拒绝。常用环境变量：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `REALMAN_BT_DRY_RUN` | `true` | `false` 才会发真实 Action / command，启动时会打印醒目警告 |
| `REALMAN_BT_ARM_ID` | `r` | 单臂树的臂 |
| `BT_EXIT_ON_TERMINAL` | 树自己的声明 | `true`/`false` 覆盖终态后是否退出；one-shot 树默认退出 |
| `BT_SERVER_PORT` / `BT_PUBLIC_HOST` | `8080` / `127.0.0.1` | 只读监视器端口与打印出来的访问地址 |
| `BT_RUNTIME_ARCHIVE_ROOT` | `/opt/rm65_ws/logs/behavior-trees` | 终态后归档 XML 与 `runtime.json` |

## `.env`

仓库根目录的 `.env` 被 Docker Compose 自动读取，`functions.zsh` 加载时也会读取其中的简单 `KEY=value`（终端里已设置的非空变量优先）。文件里每个变量都带候选值注释，**不要写入密钥**。修改后需要重建容器环境（`docker compose restart` 不会加载新的 `.env`）。

| 变量 | 作用 |
| --- | --- |
| `ROS_DOMAIN_ID` | 同一系统的 driver、Web、相机和远程查看器必须一致；生产使用独立 domain（当前 `65`）。Compose 缺省为 `0` |
| `ROS_LOCALHOST_ONLY` | 需要跨主机发现时为 `0`，并放行 DDS UDP |
| `FASTDDS_BUILTIN_TRANSPORTS` | `UDPv4`：避免宿主与容器 Fast DDS 版本不同导致共享内存不兼容 |
| `ROS_BASE_IMAGE`、`UBUNTU_APT_MIRROR`、`ROS2_APT_MIRROR`、`PYPI_INDEX_URL`、`NPM_REGISTRY` | 构建镜像源，默认国内镜像，可改回官方；需重建镜像 |
| `REALMAN_START_ROBOTS` `START_DRIVER` `START_JOY_DRIVER` `START_CONTROLLER` `START_WEB_CONTROL` | `realman_bringup_custom` 透传给 `system.launch.py` 的开关 |
| `REALMAN_USE_RVIZ` / `REALMAN_USE_GUI` | RViz 与关节状态 GUI |
| `REALMAN_DRIVER_CONFIG_FILE` `COORDINATES_` `MOTION_` `WEB_CONTROL_` `CAMERA_CALIBRATION_` `…_CONFIG_FILE` | 覆盖对应 YAML 的**容器内路径**；`*mock.yaml` 会让 `./rm65` 自动关闭夹爪 |
| `REALMAN_START_GRIPPER`、`REALMAN_GRIPPER_42D_DEVICE` | manager 开关；左右共享宿主设备（默认 `/dev/tty_gripper_42d`），容器内只映射一次；中臂无夹爪 |
| `REALMAN_JOY_DEVICE`、`REALMAN_WAIT_FOR_JOY_DEVICE`、`REALMAN_JOY_POLL_INTERVAL` | Xbox 手柄设备与等待策略 |
| `REALMAN_RECORDING_ROOT`、`REALMAN_START_RECORDING_WEB` | 录制 session 根目录（容器内默认 `/data/realman-recordings`，宿主挂载仓库 `recordings/`）与是否启动回放网页（默认 `true`） |
| `PIP_INDEX_URL`、`PYTORCH_INDEX_URL`、`HTTP_PROXY`、`HTTPS_PROXY` | 构建镜像时 pip 阶段的包源（默认国内镜像，见[系统 Bringup：国内镜像](../development/system-bringup#国内镜像与官方源切换)）与代理；构建使用宿主网络（`network: host`），因为生产机的 systemd-resolved DNS 在桥接网络里不可达 |
| `POLICY_WS_HOST`、`POLICY_BRIDGE_CONFIG_FILE`、`POLICY_BRIDGE_ACTIVE_SIDE` | 策略服务地址、配置文件、当前驱动的一侧（`left`/`right`） |
| `DISPLAY`、`XAUTHORITY`、`LIBGL_ALWAYS_SOFTWARE` | 带 RViz 的服务需要；headless 服务不需要 |
| `REALMAN_LOG_ROOT` | 日志根目录（容器内 `/opt/rm65_ws/logs`，映射到仓库 `logs/`） |
| `REALMAN_ORBBEC_ROS2_SETUP` | 仅显式设置时额外 source 的宿主机 Orbbec ROS 2 setup 文件；默认只加载 `/opt/ros/humble/setup.bash`，不自动加载仓库 overlay |
| `RM65_CAMERAS_LOCAL_CONFIG` | 可选的宿主机相机 YAML 覆盖文件；按 `cameras_ros2.yaml` 结构覆盖默认配置 |
| `CAMERA_START_TIMEOUT` | `rm65 wait` / `./rm65 up` 等待相机启动的最长秒数，默认 `90`；超时中止启动并清理受管相机 |
| `REALMAN_CAMERA_CHECK_TIMEOUT` | 相机 supervisor 等待所有启用流首帧的最长秒数，默认 `60`；当前为四路 color + 右腕、后全局两路 depth，超时判定启动失败 |

::: warning Web 控制没有认证
Web control 不做任何身份校验：`:8765` 能被访问的浏览器就能发送运动和停止命令，所以必须只放在受信任的机器人局域网内，不要暴露到公网或经 NAT 映射。（`.env` 里曾有一个 `REALMAN_WEB_CONTROL_TOKEN`，但从来没有代码读取它，已删除，避免让人误以为有认证。）
:::

## 端口与日志

| 端口 | 服务 | 说明 |
| --- | --- | --- |
| `8765` | `realman_web_control` | 控制页 `/`、标定页 `/calibration.html`、WebSocket |
| `8770` | `realman_recording` | 录制实时页与 LeRobot 回放页，默认只绑定回环地址；无认证，不要直接暴露 |
| `8080` | 行为树只读监视器 | 仅在 `./rm65 bt …` 运行期间；所有 `/api/` 写请求返回 `405` |
| `18000` | 外部 VLA 策略服务 | `policy_bridge` 作为客户端连接，不由本仓库启动 |
| `8554`、`8100–8103` | 旧 RTSP/TCP 相机推流 | 已弃用 |

日志：每次 launch 在 `logs/YYYYMMDD_HHMMSS/` 下写 ROS 官方节点日志（彩色 `RCUTILS_COLORIZED_OUTPUT=1`）；相机在 `logs/rm65-camera.log`，PID 在 `logs/.rm65-camera.pid`；行为树终态归档在 `logs/behavior-trees/<时间>_<PID>/`。
