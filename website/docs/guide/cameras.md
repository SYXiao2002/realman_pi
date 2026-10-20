---
title: 相机
description: 四台宿主机 Orbbec 相机的 ROS 2 RGB 与按设备开启的原始深度、分辨率与帧率配置、四路 RViz 查看和本地覆盖。
---

# 相机

生产相机运行在**连接 USB 相机的宿主机**上，不在 Docker 里。`./rm65 up` 启动四路 Orbbec RGB，并按配置同时开启右腕和后全局相机的深度；等待全部启用流的首帧后再启动 Docker 服务。相机 launch 或首帧失败时会清理已启动的相机且不启动 Docker。若相机已出帧但 Compose 启动失败，命令返回失败并保留相机流供诊断；执行 `./rm65 down` 停止服务和相机，并等待受管 launch 逐个退出、释放 USB 设备。彩色图像供标定、Web 标定页面、录制和策略桥使用。

宿主机需已安装并可运行 `orbbec_camera` 二进制 ROS 2 包。启动流程 source `/opt/ros/humble/setup.bash`；仅在显式设置 `REALMAN_ORBBEC_ROS2_SETUP` 时额外 source 该文件，然后直接执行 `ros2 launch orbbec_camera ...`。它不自动加载本仓库 install 或 vendor overlay，也不要求 RealSense 驱动。

| 相机 | 设备序列号 | ROS 命名空间 | 驱动 launch | 默认流 |
| --- | --- | --- | --- | --- |
| back GLOBAL CAM，Orbbec Gemini 335L | `CP28563000CK` | `/camera_global` | `gemini_330_series.launch.py` | RGB + depth |
| front GLOBAL CAM，Orbbec Gemini 335L | `CP28563000CV` | `/camera_global2` | `gemini_330_series.launch.py` | RGB |
| right WRIST CAM，Orbbec Gemini 305 | `CV2856D0002E` | `/camera_right` | `gemini_301_series.launch.py` | RGB + depth |
| left WRIST CAM，Orbbec Gemini 305 | `CV2856D0008H` | `/camera_left` | `gemini_301_series.launch.py` | RGB |

## 串号、分辨率、帧率与深度配置

`config/ros/cameras_ros2.yaml` 是相机启动配置的权威来源。顶层 `cameras` mapping 以 namespace 名为键，每台相机的 `serial` 绑定硬件、`launch_file` 选择官方启动文件，`enabled` 可选且默认 `true`。顶层 `defaults` 提供共享参数，`cameras.<namespace>.parameters` 覆盖单台设备。`launch.stagger_seconds` 控制设备启动间隔（秒）。

| 配置字段 | 含义 |
| --- | --- |
| `color_width` / `color_height` / `color_fps` / `color_format` | 彩色宽高（像素）、帧率（Hz）和像素格式；具体默认值见配置 |
| `depth_width` / `depth_height` / `depth_fps` / `depth_format` | 深度宽高（像素）、帧率（Hz）和格式；宽高与帧率为 `0`、格式为 `ANY` 时使用 SDK 默认档位 |
| `enable_depth` | `color` 模式是否同时开启该设备深度；共享默认关闭，当前 `camera_right` 与 `camera_global` 逐机设为 `true` |
| `depth_registration` | 深度是否对齐彩色，默认 `false`，因此当前发布原始深度 |

参数必须是对应设备与安装驱动支持的组合。下面是仅修改右腕分辨率、帧率并开启深度的配置示例，按需要合并进 `config/ros/cameras_ros2.yaml` 或本地覆盖文件：

```yaml
cameras:
  camera_right:
    parameters:
      color_width: 640
      color_height: 480
      color_fps: 30
      enable_depth: true
      depth_width: 0       # SDK 默认深度分辨率
      depth_height: 0
      depth_fps: 0         # SDK 默认深度帧率
      depth_format: ANY
```

被 `.gitignore` 忽略的 `config/ros/cameras_ros2.local.yaml` 可覆盖本机差异，也可通过 `RM65_CAMERAS_LOCAL_CONFIG` 指定覆盖 YAML；均按相同结构深合并共享或逐设备参数和串号。修改后须停止相机再启动，例如 `bash start_sensors.sh stop` 后执行 `./rm65 up`；重复 `up` 会复用正在运行的进程，不会热更新参数。旧的 `wrist_cameras` / `global_camera` schema 不兼容，迁移为 `cameras` mapping 后再使用。

监督脚本 `src/sensor_bringup/scripts/host_cameras.py` 管理四个独立的 `ros2 launch orbbec_camera <launch_file>` 子进程；`./rm65 up` 将监督进程独立于调用终端启动，等待四路 `color/image_raw` 和配置启用的两路 `depth/image_raw` 都实际出帧后才启动 Docker。重复 `up` 会继续等待或复用同一个受管进程；启动新进程前先清理配置对应的残留相机进程。RGB 图像与相机参数 topic 为：

```text
/camera_global/color/image_raw
/camera_global/color/camera_info
/camera_global2/color/image_raw
/camera_global2/color/camera_info
/camera_right/color/image_raw
/camera_right/color/camera_info
/camera_left/color/image_raw
/camera_left/color/camera_info
```

当前额外启用的原始深度及内参 topic 为：

```text
/camera_right/depth/image_raw
/camera_right/depth/camera_info
/camera_global/depth/image_raw
/camera_global/depth/camera_info
```

IR、IMU 和点云默认关闭。同设备 RGB-D 帧同步默认 `enable_frame_sync=false`，可在 YAML 逐设备设置真实布尔值；设备支持情况须实测。跨设备触发继续关闭。开启帧同步不替代 D2C 配准、时间戳配对或停稳采集，详见[腕相机抓取](../development/grasping)。`CAMERA_START_TIMEOUT`（默认 `90` 秒）控制 `./rm65 up` / `rm65 wait` 的相机启动等待上限；`REALMAN_CAMERA_CHECK_TIMEOUT`（默认 `60` 秒）控制 supervisor 的首帧等待上限。当前生产映射中没有 D435 或 `/camera_middle`。ROS 2 topic 和设备状态可从宿主机检查：

```bash
source /opt/ros/humble/setup.bash
ros2 pkg prefix orbbec_camera
ros2 topic list | grep '/camera_.*color/'
ros2 topic hz /camera_global/color/image_raw
ros2 topic hz /camera_right/depth/image_raw
ros2 topic hz /camera_global/depth/image_raw
```

通过仓库函数单独管理相机：

```zsh
source /path/to/realman_pi/functions.zsh
rm65_camera_ros2 color
rm65_camera_ros2_status
rm65_camera_ros2_logs
rm65_camera_ros2_stop
```

`rm65_camera_ros2 [color|depth] [rviz]` 保留兼容；省略模式或执行 `./rm65 up` 时使用 `color` 模式，保留全部 RGB 并按合并后的配置启用逐机深度；显式 `depth` 切换为全部已启用设备的 depth-only 模式。旧的 `enable_color:=true enable_depth:=false use_rviz:=true` 参数形式仍可用。非 zsh 环境可使用 `bash start_sensors.sh`，单独停止相机使用 `bash start_sensors.sh stop`。停止入口即使发现监督进程已退出，也会按当前配置中的 launch 文件、相机名和串号匹配并清理残留 launch/组件进程；清理失败会返回失败并保留状态供重试。每次运行的 ROS 日志写入 `logs/<timestamp>/` 下各相机的独立子目录，`./rm65 up` 的脚本输出写入 `logs/rm65-camera.log`。

默认 ROS domain 由根目录 `.env` 的 `ROS_DOMAIN_ID` 和当前环境决定。生产宿主机、Docker 服务和远程查看器必须使用同一 ROS domain；修改后重启对应服务。

## 查看四路实拍画面

生产机启动 `./rm65 up` 后，笔记本不需要连接 USB 相机，只需加入相同 ROS domain 并启动专用相机 RViz。该查看器只订阅图像，不启动相机或 RealMan 驱动：

```zsh
source /path/to/realman_pi/functions.zsh
rm65_docker_build realman_camera_rviz  # 首次使用或镜像更新后
rm65_docker_camera_rviz                 # 前台打开 RViz，Ctrl-C 停止
```

RViz 显示以上四个 `color/image_raw` topic。也可在后台运行：

```zsh
rm65_docker_camera_rviz_start
rm65_docker_camera_rviz_status
rm65_docker_camera_rviz_logs -f
rm65_docker_camera_rviz_stop
```

若图像为空，先在笔记本执行 `ros2 topic list | grep '/camera_.*color/image_raw'`，再检查两端 `ROS_LOCALHOST_ONLY=0`、`FASTDDS_BUILTIN_TRANSPORTS=UDPv4`、DDS UDP/组播和防火墙设置。不要在笔记本执行 `rm65_camera_ros2`，否则会尝试打开笔记本的 USB 相机。

## 相机推流（已弃用）

::: warning 已弃用
RTSP/TCP SDK 推流不属于 `./rm65 up` 的生产相机链路。仅在历史排障时使用；推流 SDK 与 ROS 2 相机节点会争用 USB 设备，不得同时运行。
:::

旧入口 `rm65_camera_start`、`rm65_camera_stop`、`rm65_camera_status` 和 `rm65_camera_logs` 位于 `src/camera_stream/`，通过 Orbbec/RealSense SDK、`mediamtx` 和可选 `ros2_bridge` 提供历史流。其依赖安装、RTSP 地址和 TCP 深度端口仅适用于这套已弃用功能，不影响当前四路 ROS 2 RGB 启动。
