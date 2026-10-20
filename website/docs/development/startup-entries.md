---
title: 启动入口索引
description: functions.zsh 中每个项目启动、构建、测试和部署入口的当前用途、组件范围和权威配置。
---

# 启动入口索引

本页同步根目录 `functions.zsh` 的当前入口，面向需要选择启动方式、定位组件边界或维护 GitHub Pages 文档的开发者。这里记录**当前 main 分支的有效行为**，不按 commit 追加历史说明；每次修改 helper、Compose 服务、launch 参数或运行配置时，应在同一次提交中更新本页、`rm65_project_help` 和相关专题页。

::: tip 时效性
`functions.zsh` 只是快捷入口。最终行为以根目录 `config/`、ROS 2 launch 文件和 Compose 服务为准。本页优先链接到权威专题页，避免复制容易过期的参数细节。
:::

## 使用方式

```zsh
source /path/to/realman_pi/functions.zsh
rm65_project_help
```

## 推荐统一入口

日常生产操作优先使用仓库根目录的 `./rm65`，它把 ROS 2 彩色相机和三臂机械臂的生命周期合并管理：

```bash
./rm65 build              # 重建 realman_bringup_remote 与 realman_web_control；不改变容器状态
./rm65 up                 # ROS 2 彩色相机 + 三臂真实驱动 + Web control + 录制/回放 + 相机健康诊断，无 RViz
./rm65 up desktop         # 同上，并启动远程 ROS 图 RViz
./rm65 up model           # 离线三臂模型 + RViz，不连接真机
./rm65 rviz2              # 在 :1 桌面前台显示四路相机 RGB
./rm65 bt <tree-name>    # 按 config/behavior-trees/<tree-name>[.xml] 启动行为树
./rm65 bt control         # 显式启动持久输入路由器；Ctrl-C 前保持运行
# 也可以不开终端：:8765 控制页“行为树”卡片选文件并点“启动”（bringup 容器内的 bt_launcher 代为运行 bt-start）
./rm65 status
./rm65 logs
./rm65 down
./rm65 sync               # 校验 main 后同步生产端
```

默认相机链路是宿主机 `rm65_camera_ros2 color`：直接使用已安装的 `orbbec_camera` 二进制包，
按串号启动两台 Gemini 335L 全局相机（`/camera_global`、`/camera_global2`）和两台 Gemini 305
腕部相机（`/camera_right`、`/camera_left`）。四路均发布 RGB，配置为 `camera_right` 与 `camera_global`
额外启用原始 depth；`./rm65 up` 等待四路 `color/image_raw` 和这两路 `depth/image_raw` 首帧，首帧失败会清理相机且不启动 Docker。相机就绪后若 Compose 启动失败，命令返回失败但
保留相机流；执行 `./rm65 down` 可停止服务并等待相机 launch 逐个退出、释放 USB。不 source 本仓库 install
或 vendor overlay，不依赖 RealSense。默认 `up` 不启动 RViz，生产端不需要 `DISPLAY` 或 `XAUTHORITY`。
详情见[相机指南](../guide/cameras)。

`./rm65 up` 同时启动录制平台（`realman_recording` 容器内的 `recording_recorder` + 只读回放网页），回放页在
`http://127.0.0.1:8770/`（默认仅绑定 loopback，认证/反向代理完成前不要暴露到局域网）。录制/回放细节见
[独立数据录制平台](./recording-platform)。

::: warning 驱动服务互斥
`realman_bringup_remote` 是生产 ROS 图中唯一启动三台 `realman_driver` 的服务。不要在它运行时启动
`realman_driver_rviz`；后者也会连接真实机械臂，导致同一命名空间出现重复驱动和多个
`/<arm>/cartesian_velocity` Action Server，键盘速度指令可能因此被多个执行器同时消费。`./rm65 up`、
`./rm65 up desktop` 和 `./rm65 up policy` 会在启动相机前检查该 Compose 服务，发现仍在运行就拒绝启动，
不会自动停止其他容器。

桌面查看生产 ROS 图请使用 `realman_remote_rviz`。它只启动 `rviz2` 并订阅远程图，不启动 RealMan 驱动、
`robot_state_publisher` 或假关节状态源；`./rm65 up` 默认不启动它，只有显式执行 `./rm65 up desktop` 才会加入
这个 RViz-only 服务。出现冲突时先停止 `realman_driver_rviz`，再重新执行需要的入口。
:::

行为树命令只要求 Docker 中有且仅有一个正在运行的
`realman_bringup_remote` bringup 容器，不要求先执行 `./rm65 up`，也不要求该容器属于当前仓库的
Compose project。容器可以由 `./rm65 up`、`docker compose up -d realman_bringup_remote` 或
`rm65_docker_bringup_remote` 启动；启动器通过 Docker 的
`com.docker.compose.service=realman_bringup_remote` 标签解析容器，并直接对该容器执行行为树。
行为树执行器、只读 `bt_server` 和静态运行监视器都在该容器内；启动器依赖 `/<arm_id>/execute_motion` Action Server，
默认 `REALMAN_BT_DRY_RUN=true`。网页地址为
`http://<host>:8080/`。监视器只读 `GET /api/runtime` 快照，不提供编辑、Tick 或 Run，
网页服务先于 Action readiness 启动，所以驱动发现尚未完成时也会显示等待页；执行器仍会等待
Action Server 后再 tick。快照轮询使用 `ETag`，序号未变化时返回 `304`，并在连接中断时保留最后一次有效数据并提示过期状态。
监视器读取 schema-v2 快照，显示累计 Tick 成功/失败比例图、最近最多 200 条事件和失败节点的
`failure_reason`；事件来源为 `ACTION`、`SERVICE`、`ROS_LOG` 或 `EXECUTOR`，其中 `/rosout` 只保留指定
logger 的 WARN/ERROR。MoveJ 仍只经 `/<arm_id>/execute_motion` Action 执行；`/realman_bt_executor/start`
和 `/realman_bt_executor/stop` 仅控制 executor tick，不能替代运动接口。首次修改 Dockerfile、ROS 代码或
前端后执行 `./rm65 build`，它只重建生产 driver/Web 镜像，不停止或启动容器。只有完成安全检查后才可用
`REALMAN_BT_DRY_RUN=false ./rm65 bt r` 发送真实 MoveJ；按 `Ctrl-C` 只清理行为树进程，
不会停止驱动容器。默认 one-shot executor 在 `SUCCESS`/`FAILURE` 后等待 cancellation drain 清空，再让
executor、`ros2 launch` 和只读监视器依次退出；最终快照归档到 `logs/behavior-trees/`。容器锁禁止两次
`./rm65 bt` 并发运行，前一实例清理完即可启动下一棵树。若停止时 Action 尚未终态，executor 的
cancellation drain 会保留 pending response 或
accepted handle，直到收到拒绝或成功提交 cancel；提交异常会重试，成功提交后立即释放，不等待取消确认或
终态结果，也不重新启动树。进程退出后的机器人安全仍依赖驱动的软件停止机制和可达的急停。详细参数、失败
细节和边界见[行为树机械臂移动 Demo](./behavior-tree-motion)。

`./rm65 bt control` 不是 one-shot MoveJ：它读取 `config/behavior-trees/control.xml` 和
`config/ros/behavior_tree.yaml`，强制 `stop_on_terminal=false`、`exit_on_terminal=false`，并一直运行到
Ctrl-C。`./rm65 up` 不会替它启动 executor；只要 bringup 容器已运行即可单独启动 control。Ctrl-C 也只停止 router/:8080，不停止 driver 或 :8765 Web
服务。路由的动态 XML 目录和 Web cancellation 约定见[行为树控制权与 Mock 测试](./behavior-tree-control)。

行为树入口支持省略 `.xml` 后缀：`./rm65 bt move`、`./rm65 bt three`、`./rm65 bt control` 和
`./rm65 bt custom_tree.xml` 均从 `config/behavior-trees/` 解析。XML `<root>` 的启动元数据决定 arm、
所需 Action Server、允许的 launch 类型和终态策略；未声明时使用 `r`、当前 arm、`arm_move`、终态停止并退出。
`realman_launch` 仅允许 `arm_move` 或 `control_router`，不支持从 XML 执行任意 shell 命令。旧短名和旧文件名
别名仍兼容。


所有 helper 都从 `functions.zsh` 所在位置定位仓库根目录，因此可以在任意目录调用。函数加载时会读取仓库根目录 `.env` 中的简单 `KEY=value` 配置，并保留当前终端已经显式设置的非空变量。函数不会自动写入 `~/.zshrc`，也不会隐藏底层 Docker、colcon、npm、SSH 命令；遇到未覆盖的参数时，继续直接调用底层命令。

## 项目入口

| 函数 | 当前用途 | 适用场景 | 权威来源 |
| --- | --- | --- | --- |
| `rm65_project_cd` | 切回仓库根目录。 | 在多个终端目录之间切换后，快速回到 `realman_pi` 执行 Compose、colcon 或网站命令。 | `functions.zsh` 中的 `RM65_PROJECT_ROOT` |
| `rm65_project_help` | 打印分组后的 helper 索引，并指向本页。 | 忘记入口名称或需要快速判断哪个服务适合当前任务时先运行。 | `functions.zsh` 本身与本页 |

## Docker 构建和模型查看

| 函数 | 当前用途 | 启动/影响的组件 | 适用场景 | 权威配置与文档 |
| --- | --- | --- | --- | --- |
| `rm65_docker_build [service ...]` | 从仓库根目录构建一个或多个 Compose 服务；不传参数时构建 `realman_bringup`。 | 只执行 Docker build，不启动 ROS 图。 | 首次运行、Dockerfile/依赖变化后，或切换镜像源后。 | `config/docker/compose.yaml`、`config/docker/ros2-humble-rviz.Dockerfile`、[系统 Bringup](./system-bringup#国内镜像与官方源切换) |
| `rm65_docker_rviz [model]` | 前台启动单臂 RViz 查看器，默认型号为 `RM65-B` 或当前 `RM65_MODEL`。 | `rm65_description/display.launch.py`、`rviz2`、单台 URDF/TF。 | 只检查某个 RM65 型号的 URDF、网格和关节树，不需要三臂布局。 | `src/rm65_description/urdf/`、[支持型号](../models/) |
| `rm65_docker_three_rviz` | 前台启动三臂 RViz 场景。 | `three_robots.launch.py`、三组 `/l` `/m` `/r` 描述、TF 和 RViz。 | 验证 `three_robots.yaml` 中三台机械臂的位置、朝向、命名空间和 TF 前缀。 | `config/ros/three_robots.yaml`、[三臂配置驱动可视化](./three-arm-visualization) |

## 独立测试入口

| 函数 | 当前用途 | 启动/影响的组件 | 适用场景 | 权威配置与文档 |
| --- | --- | --- | --- | --- |
| `rm65_docker_xbox_test` | 前台启动独立 Xbox 输入测试。 | SDL `game_controller_node`、`/input/joy`、`xbox_controller_node`；不启动三臂、驱动或 RViz。 | 先确认实体手柄、Linux input 权限、SDL 映射和按键边沿日志是否正常。 | `config/ros/xbox_controller.yaml`、[Xbox 手柄输入](./xbox-controller#独立实体手柄测试) |
| `rm65_docker_driver_test` | 前台启动三臂 mock 驱动测试。 | `/l` `/m` `/r` 三个 `realman_driver` mock 实例；不访问真实控制器。 | 没有机械臂时验证 ROS topic/service、mock 连接和关节状态发布。 | `config/ros/realman_driver_mock.yaml`、[睿尔曼三臂驱动与运动控制](./realman-driver-scaffold#独立启动) |
| `rm65_docker_driver_rviz` | 前台启动真实三臂驱动和 RViz。 | 三台真实 `realman_driver`、三臂描述/TF、RViz；不启动 Joy 或 Xbox 输入。 | 已连接控制器网络时，只观察真实关节回读和模型姿态，不引入操作输入。 | `config/ros/realman_driver.yaml`、[睿尔曼三臂驱动与运动控制](./realman-driver-scaffold) |

## Bringup 固定入口

| 函数 | 当前用途 | 启动/影响的组件 | 适用场景 | 权威配置与文档 |
| --- | --- | --- | --- | --- |
| `rm65_docker_bringup` | 前台启动完整本地系统。 | 三臂描述/TF、真实驱动、RViz、SDL Joy、Xbox 输入；等待实体手柄。 | 本机同时连接显示环境、控制器网络和实体 Xbox 手柄时的完整联调。 | `config/docker/compose.yaml`、[系统 Bringup](./system-bringup#docker-服务) |
| `rm65_docker_bringup_remote` | 前台启动 headless 远程目标。 | 三臂描述/TF、真实驱动、Xbox 处理节点；不启动 Joy 设备读取和 RViz，可选 Web 控制。 | 工控机作为 ROS 图和真实 SDK 连接的运行端，桌面机另行启动远程 RViz。 | `realman_bringup_remote` 服务、[系统 Bringup](./system-bringup#远程验证) |

## Bringup 配置入口

`realman_bringup_custom` 把 `.env` 中的开关透传给 `realman_bringup/system.launch.py`。这些函数用于快速切换当前有效组合；每个 profile 都在子 shell 中设置变量，执行结束后不会污染当前终端环境。

| 函数 | 当前用途 | 启动/影响的组件 | 适用场景 | 权威配置与文档 |
| --- | --- | --- | --- | --- |
| `rm65_docker_bringup_custom` | 前台运行 `.env` 参数化 bringup。 | 按 `REALMAN_START_*`、`REALMAN_USE_*` 和 `REALMAN_*_CONFIG_FILE` 创建组件。 | 需要试验 launch 组合，但不想编辑 Compose 文件时。 | `.env`、`config/docker/compose.yaml`、[系统 Bringup：参数化组合](./system-bringup#参数化组合) |
| `rm65_docker_bringup_custom_start` | 后台启动 `.env` 参数化 bringup 并打印服务状态。 | `docker compose up -d realman_bringup_custom`。 | 需要让组合在终端返回后继续运行，并通过 status/logs 管理生命周期。 | `realman_bringup_custom` 服务 |
| `rm65_docker_bringup_custom_stop` | 停止后台参数化 bringup。 | `docker compose stop realman_bringup_custom`。 | 结束后台 ROS 图；不会删除镜像或修改配置。 | `realman_bringup_custom` 服务 |
| `rm65_docker_bringup_custom_status` | 查看后台参数化 bringup 状态。 | `docker compose ps realman_bringup_custom`。 | 判断容器是否仍在运行。 | Docker Compose |
| `rm65_docker_bringup_custom_logs [-f]` | 查看最近 100 行或持续跟踪参数化 bringup 日志。 | `docker compose logs --tail=100 ... realman_bringup_custom`。 | 排查 launch、驱动、Web 控制或 DDS 连接问题；`-f` 只跟踪日志，不停止服务。 | Docker Compose 与 ROS 日志 |
| `rm65_docker_bringup_custom_args launch_arg:=value ...` | 临时把 launch 参数直接交给 `system.launch.py`。 | 使用图形版服务的挂载和设备配置，但不读取 `.env` profile。 | 一次性验证 `start_driver:=false use_rviz:=true` 等组合，不想改 `.env`。 | `realman_bringup/system.launch.py`、[系统 Bringup：启动入口](./system-bringup#启动入口) |

## Bringup profiles

| 函数 | 当前用途 | 组件组合 | 适用场景 | 权威配置与文档 |
| --- | --- | --- | --- | --- |
| `rm65_docker_bringup_model` | 只显示配置中的机器人模型。 | `start_robots=true`、`start_driver=false`、`use_rviz=true`、输入和 Web 关闭。 | 离线检查三臂模型、TF 和 RViz，不连接真实机械臂。 | `config/ros/three_robots.yaml`、[三臂配置驱动可视化](./three-arm-visualization) |
| `rm65_docker_bringup_hardware` | 连接真实驱动并显示 RViz，不启用输入。 | `start_robots=true`、`start_driver=true`、`use_rviz=true`、Joy/Xbox/Web 关闭。 | 已接入控制器网络时，先验证真实关节回读与 RViz 显示。 | `config/ros/realman_driver.yaml`、[睿尔曼三臂驱动与运动控制](./realman-driver-scaffold) |
| `rm65_docker_bringup_headless` | 启动真实驱动和 Xbox 处理节点，不显示 GUI。 | `start_robots=true`、`start_driver=true`、`start_controller=true`、`use_rviz=false`、Joy/Web 关闭。 | 远程主机上运行 headless ROS 图，并允许其他节点发布 `/input/joy`。 | [系统 Bringup](./system-bringup#典型启动组合) |
| `rm65_docker_bringup_input` | 只启动 Joy 和 Xbox 输入链。 | `start_robots=false`、`start_driver=false`、`start_joy_driver=true`、`start_controller=true`，等待设备。 | 独立验证实体手柄输入，不触碰机械臂或 RViz。 | `config/ros/xbox_controller.yaml`、[Xbox 手柄输入](./xbox-controller) |
| `rm65_docker_bringup_web` | 启动真实驱动和 Web 控制，不显示 RViz。 | `start_robots=true`、`start_driver=true`、`start_web_control=true`、RViz/Joy/Xbox 关闭。 | 工控机上提供浏览器 Action 控制和 URDF 状态影子。 | `config/ros/realman_web_control.yaml`、[WebSocket 浏览器控制与 URDF 影子](./realman-web-control) |

## 相机入口

相机主线运行在直接连接 USB 相机的宿主机上。`start_sensors.sh` source `/opt/ros/humble/setup.bash`，
仅在显式设置 `REALMAN_ORBBEC_ROS2_SETUP` 时额外 source 该文件，
由 `src/sensor_bringup/scripts/host_cameras.py` 管理四个独立的 `ros2 launch orbbec_camera` 子进程；
`./rm65 up` 等待四路真实 RGB 及配置启用的两路 depth 首帧后才继续。不需要构建
`src/sensor_bringup` 或已移除的厂商源码驱动。完整串号、命名空间及检查步骤见[相机指南](../guide/cameras)。

| 函数 | 当前用途 | 启动/影响的组件 | 权威配置与文档 |
| --- | --- | --- | --- |
| `rm65_camera_ros2 [color|depth] [rviz]` | 默认启动四路 RGB 及配置开启的逐机 depth，等待所有启用流首帧；显式 `depth` 为全部已启用设备的 depth-only 模式，`rviz` 额外打开 RViz2。 | 宿主机 `orbbec_camera` 的 Gemini 330 series / 301 series launch；发布 `color/*`，启用深度的设备还发布 `depth/image_raw` 与 `depth/camera_info`。 | `config/ros/cameras_ros2.yaml`、可选 `RM65_CAMERAS_LOCAL_CONFIG`、`.env`、`config/rviz/cameras.rviz` |
| `rm65_camera_ros2_stop` | 停止项目管理的相机进程并释放 USB；监督进程已退出时仍按当前配置清理残留 launch/组件进程。 | 相机启动器及其 launch 子进程，不影响 RealMan 驱动。 | `src/sensor_bringup/scripts/host_cameras.py` |
| `rm65_camera_ros2_status` | 检查受管相机进程和日志位置。 | 只读检查。 | `logs/<timestamp>/` |
| `rm65_camera_ros2_logs` | 查看最近一次相机运行日志。 | 官方 ROS 日志。 | `logs/<timestamp>/` |

### ROS2 图像节点与 RViz2

```zsh
source /path/to/realman_pi/functions.zsh
rm65_camera_ros2 color
rm65_camera_ros2_status
rm65_camera_ros2_logs
rm65_camera_ros2_stop
```

不依赖 zsh 时可执行 `bash start_sensors.sh [color|depth] [rviz]`，使用 `bash start_sensors.sh stop` 单独停止相机；旧的 `enable_color:=true enable_depth:=false`
与 `use_rviz:=true` 形式仍兼容。`./rm65 up` 和省略模式时启用 RGB，并按逐机配置同时启用深度；显式 `depth` 为全部已启用相机的 depth-only 模式。
需要本地 GUI 时执行 `rm65_camera_ros2 color rviz`；生产端通常不带桌面，应使用笔记本的
[相机 RViz](../guide/cameras#查看四路实拍画面)。

顶层 `cameras` mapping 以 `camera_global`、`camera_global2`、`camera_right`、`camera_left`
为键，每项配置 `serial`、`launch_file`，`enabled` 可选且默认开启，`parameters` 用于设备级覆盖。顶层 `defaults` 提供共享参数；
`launch.stagger_seconds` 为启动间隔（秒）。`color_*` 和 `depth_*` 的宽高、帧率与格式由配置决定，宽高与帧率 `0`、格式 `ANY` 使用 SDK 默认档位。`enable_depth` 在 `color` 模式按合并后的设备配置生效，当前仅 `camera_right` 和 `camera_global` 为 `true`；`depth_registration=false` 发布原始深度。IR、IMU、点云和帧同步仍关闭。`config/ros/cameras_ros2.local.yaml` 可覆盖共享或逐设备参数、串号等叶子字段，也可通过 `RM65_CAMERAS_LOCAL_CONFIG` 指定覆盖文件；修改后须停止相机再启动，重复 `up` 不会热更新。旧的 `wrist_cameras` / `global_camera` 结构必须迁移，不能继续加载。参数示例见[相机配置](../guide/cameras#串号、分辨率、帧率与深度配置)。

四路预期 RGB topic：

```text
/camera_global/color/image_raw
/camera_global2/color/image_raw
/camera_right/color/image_raw
/camera_left/color/image_raw
```

每路同时发布 `color/camera_info`；`camera_right` 与 `camera_global` 另发布 `depth/image_raw` 和 `depth/camera_info`。
节点存在或 topic publisher 存在都不足以证明设备出图，以四路 RGB 和两路 depth 持续收到真实帧为准。首帧检查失败时先核对串号、宿主机 `orbbec_camera` launch 文件和 USB
连接，再查看本次 `logs/<timestamp>/` 日志。宿主机与 Docker 共同读取 `.env` 的 ROS domain 与
`FASTDDS_BUILTIN_TRANSPORTS=UDPv4`，修改后重启相关节点。

### 已弃用的 SDK 推流

`rm65_camera_start` / `stop` / `status` / `logs` 是 `src/camera_stream/` 下的 RTSP/TCP SDK
历史入口，不属于 `./rm65 up`。它们直接使用 Orbbec/RealSense SDK、`mediamtx` 和可选
`ros2_bridge`；不要与 ROS 2 相机节点同时打开同一 USB 设备。详情见[相机指南](../guide/cameras#相机推流已弃用)。

## Web 控制入口

| 函数 | 当前用途 | 启动/影响的组件 | 适用场景 | 权威配置与文档 |
| --- | --- | --- | --- | --- |
| `rm65_docker_web_control` | 前台启动独立 Web 控制服务。 | `realman_web_control/web_control.launch.py`；加入已有 ROS 图，但不启动驱动。 | 已有 `realman_bringup_remote` 或其他驱动图运行时，单独观察 WebSocket 协议和页面日志。 | `config/ros/realman_web_control.yaml`、[WebSocket 浏览器控制与 URDF 影子](./realman-web-control) |
| `rm65_docker_web_control_start` | 后台启动独立 Web 控制服务并打印状态。 | `docker compose up -d realman_web_control`。 | 工控机长期提供 `http://<host>:8765/` 浏览器入口。 | `realman_web_control` Compose 服务 |
| `rm65_docker_web_control_stop` | 停止后台 Web 控制服务。 | `docker compose stop realman_web_control`。 | 关闭浏览器控制桥；不停止真实驱动容器。 | Docker Compose |
| `rm65_docker_web_control_status` | 查看 Web 控制服务状态。 | `docker compose ps realman_web_control`。 | 判断 Web 服务是否仍在后台运行。 | Docker Compose |
| `rm65_docker_web_control_logs [-f]` | 查看或跟踪 Web 控制服务日志。 | `docker compose logs --tail=100 ... realman_web_control`。 | 排查 WebSocket、Action client、URDF 资源或授权配置。 | Docker Compose 与 `realman_web_control` 日志 |
| `rm65_web_control_url [host]` | 打印浏览器控制台 URL。 | 不启动服务，只根据 host 和 `REALMAN_WEB_CONTROL_PORT` 输出 `http://host:port/`。 | 启动 Web 控制后，把正确地址复制给同网段开发机浏览器。 | `config/ros/realman_web_control.yaml` 中的端口配置 |

## 远程 RViz 入口

`./rm65 rviz2` 是四路相机查看快捷入口：设置 `DISPLAY=:1` 和
`XAUTHORITY=/run/user/1000/gdm/Xauthority` 后，加载仓库 `functions.zsh` 并调用
`rm65_docker_camera_rviz`。需要对应桌面会话及可读的授权文件；ROS domain 沿用已有 helper 从 `.env`
加载的配置。命令前台运行，按 `Ctrl-C` 退出查看器；相机需已启动。

| 函数 | 当前用途 | 启动/影响的组件 | 适用场景 | 权威配置与文档 |
| --- | --- | --- | --- | --- |
| `rm65_docker_remote_rviz [domain]` | 前台启动 RViz-only 远程查看器；省略 `domain` 时读取 `.env` 的 `ROS_DOMAIN_ID`。 | `remote_rviz.launch.py` 和 `rviz2`；不启动本地 driver、robot_state_publisher 或假关节状态源。 | 首次排查远程 DDS、X11 授权或 RViz 配置时，保留终端日志。 | `.env`、[快速开始：远程 RViz 函数详解](../guide/remote-rviz#远程-rviz-函数详解) |
| `rm65_docker_remote_rviz_start [domain]` | 后台启动 RViz-only 远程查看器并打印状态；省略 `domain` 时读取 `.env`。 | `docker compose up -d realman_remote_rviz`。 | 日常在桌面机持续观察工控机 `realman_bringup_remote` 发布的三臂状态。 | `.env`、`realman_remote_rviz` Compose 服务 |
| `rm65_docker_remote_rviz_stop` | 停止后台远程 RViz。 | `docker compose stop realman_remote_rviz`。 | 关闭桌面机 RViz-only 服务，不影响工控机驱动和机械臂。 | Docker Compose |
| `rm65_docker_remote_rviz_status` | 查看后台远程 RViz 状态。 | `docker compose ps realman_remote_rviz`。 | 确认 RViz 容器是否仍在运行。 | Docker Compose |
| `rm65_docker_remote_rviz_logs [-f]` | 查看或跟踪远程 RViz 日志。 | `docker compose logs --tail=100 ... realman_remote_rviz`。 | 排查 DDS 发现、TF、joint state 或显示授权问题。 | Docker Compose、[故障排查](../troubleshooting) |
| `rm65_docker_camera_rviz [domain]` | 前台显示生产机四路 Orbbec RGB；省略 `domain` 时读取 `.env` 的 `ROS_DOMAIN_ID`。 | `realman_camera_rviz` 和只含彩色 Image display 的 `config/rviz/cameras.rviz`；不启动驱动或本地相机。 | 查看 `/camera_left`、`/camera_right`、`/camera_global`、`/camera_global2` 实拍画面。 | `.env`、[快速开始：查看四路实拍画面](../guide/cameras#查看四路实拍画面) |
| `rm65_docker_camera_rviz_start [domain]` | 后台启动四路相机 RViz；省略 `domain` 时读取 `.env`。 | `docker compose up -d realman_camera_rviz`。 | 日常持续查看相机画面。 | `.env`、`realman_camera_rviz` Compose 服务 |
| `rm65_docker_camera_rviz_stop` | 停止后台相机 RViz。 | `docker compose stop realman_camera_rviz`。 | 关闭笔记本上的相机查看器，不影响生产机相机。 | Docker Compose |
| `rm65_docker_camera_rviz_status` | 查看后台相机 RViz 状态。 | `docker compose ps realman_camera_rviz`。 | 确认相机 RViz 容器是否运行。 | Docker Compose |
| `rm65_docker_camera_rviz_logs [-f]` | 查看或跟踪相机 RViz 日志。 | `docker compose logs --tail=100 ... realman_camera_rviz`。 | 排查 X11、DDS domain 或图像订阅问题。 | Docker Compose |

## 本机构建、网站和部署入口

| 函数 | 当前用途 | 影响范围 | 适用场景 | 权威配置与文档 |
| --- | --- | --- | --- | --- |
| `rm65_ros_build [args ...]` | 加载本机 ROS 2 Humble 后执行 `colcon build --symlink-install --packages-up-to realman_bringup realman_robot_driver`。 | 本机 `install/`、`build/`、`log/`。 | 不使用 Docker 时构建 ROS 包，或给 `colcon` 追加调试参数。 | `src/` 下 ROS packages、[系统 Bringup：构建与验证](./system-bringup#构建与验证) |
| `rm65_web_build` | 在 `website/` 中构建 VitePress 网站。 | 先同步三臂模型资源，再输出 `website/docs/.vitepress/dist`。 | push 前确认开发者手册、模型资源和 GitHub Pages 构建不会失败。 | `website/package.json`、`website/scripts/sync-three-robots.mjs` |
| `rm65_web_test` | 在 `website/` 中运行 Playwright 网站测试。 | 桌面和移动端文档路由、首页 WebGL 场景和生成 JSON。 | 修改网站页面、导航、生成资源或 docs 结构后。 | `website/tests/`、`config/website/playwright.config.mjs` |
| `rm65_deploy_sync` | 本地 `main` 提交并 `git push origin main` 后，用 `rsync` 同步当前干净工作树到生产主机。 | 更新生产目录文件；排除 `.git/`、构建产物、日志、Node 依赖、测试结果和 Python 缓存；不自动重建或重启容器。 | GitHub 已有最新提交，但生产主机拉取 GitHub 不稳定或希望以本地文件为同步源时。 | `REALMAN_PRODUCTION_HOST`、`REALMAN_PRODUCTION_DIR`、[系统 Bringup：生产端代码部署](./system-bringup#生产端代码部署) |
| `rm65_deploy_update` | 兼容入口：通过 SSH 到生产主机执行 `git fetch origin main` 和 `git merge --ff-only origin/main`。 | 更新生产端 Git checkout 元数据和文件；不自动重建或重启容器。 | 生产主机可稳定访问 GitHub，且需要让远端 checkout 的 `main` 快进到 `origin/main` 时。 | `REALMAN_PRODUCTION_HOST`、`REALMAN_PRODUCTION_DIR`、[系统 Bringup：生产端代码部署](./system-bringup#生产端代码部署) |

## 维护规则

- 修改 `functions.zsh` 新增、重命名或删除入口时，同步更新本页、`rm65_project_help`、`website/docs/development/index.md` 和 `website/tests/site.spec.ts` 路由列表。
- 修改 Compose 服务、launch 参数或 `.env` 变量时，优先更新对应专题页，再让本页链接到新的权威说明。涉及 ROS 通信时，确认相机、机械臂、Web 控制和远程 RViz 的 `ROS_DOMAIN_ID` 仍来自同一配置源。
- 不把真实 token、临时 IP、现场 SSH 密钥或未验证的真机行为写入文档。真实硬件行为应标明验证状态。
- 运行文档验证：

```bash
cd website
npm run build
npm run test:e2e
```
