# 6D Pose 自动检测与 SAM2.1 Tiny 实例 Mask 计划

状态：已实施，核心链路已在真实 right-wrist RGB-D 页面完成端到端验收。

## 实施与验收记录

- 在 `/home/yixiaoshen/Repo/realman_pose_vision/` 建立独立 YOLO/SAM2 服务目录；YOLO 使用抓取项目内的 `yolov8n.pt` 自定义五类权重，SAM 使用现有 SAM2.1 Tiny 源码和 checkpoint。两个容器复用 FoundationPose 的 CUDA/PyTorch 底层镜像，不依赖源抓取仓库的运行时路径。
- 增加 `pose6d_yolo`、`pose6d_sam2` Compose 服务和 bridge WebSocket 客户端；`./rm65 build` 默认构建两种视觉服务，`./rm65 up` 一并启动视觉服务，`./rm65 down` 按 bridge、推理服务、机器人和相机的次序停止。模型容器不运行 ROS 或机器人接口，视觉 WebSocket 仅绑定 `127.0.0.1`。
- 页面实际验收：`/6d_pose.html` 采集 right-wrist `848×530` 冻结帧，YOLO 检出 16 个目标、SAM 对 16 个目标全部生成 mask；新 bridge 版本页面显示 YOLO `209.8 ms`、SAM 合计 `350.9 ms`。选择实例 `...:0` 后页面按需预览，并仅发送 observation/instance ID；FoundationPose 成功返回 4×4 位姿。
- 同一 observation 的耗时记录已追加到 `logs/pose6d_vision/timings.jsonl`，状态 `ok`、检测数 16、YOLO `209.8 ms`、SAM `350.9 ms`，宿主文件权限为 `0644`。验收期间浏览器无错误，未发送准备位、Jog 或停止运动指令。
- FoundationPose 的几何筛查返回 `insufficient_evidence`，提示当前筛查阈值尚未标定；这不影响分割和位姿输出，但位姿/吸取策略仍需人工复核，运动准备门禁保持原样。
- 后续运动模式调整：从当前 TCP 远距离移动到 +40 mm 准备位改为 BASE `MOVEJ_P`；沿吸取法线靠近/远离的 Jog 保持 `MOVEL`。该改动已构建并部署到 bridge，53 项 pose6d ROS 单测通过。工作区预检仍只采样笛卡尔直线，不预测控制器的 MOVEJ_P 关节路径；操作员仍须复核实际路径，未在验收中触发真机移动。
- 验证结果：最新 RM65 镜像构建及 colcon `24` 项测试通过；正确加载 ROS 环境后 bridge pytest `22 passed`；Playwright 桌面和移动端共 `124 passed`；`bash tests/test_rm65_entry.sh`、`bash -n rm65`、`git diff --check` 通过。真实页面回归截图：`/tmp/pose6d-live-recheck.png`。
- 后续测试加固：当前自动化测试覆盖缓存 mask 身份、尺寸/值域、owner 隔离、消息大小、耗时日志和页面正常选择流程；YOLO/SAM 容器对坏包、零检测、单实例 SAM 故障的专门故障注入及对应页面状态用例仍可补充。

## 目标

在现有 `/6d_pose.html` 单帧工作流中，采集 RGB-D 后自动调用独立 YOLO Docker 获取该帧全部目标框，再调用独立 SAM2.1 Tiny Docker 对每个目标框生成实例 mask。页面显示检测框、每个 SAM 实例及耗时；操作员选择一个实例后，直接用其 mask 执行现有 FoundationPose 注册。整个视觉链路始终使用同一个冻结 observation，不运行视频跟踪，也不向机械臂发送自动运动命令。

同时将 YOLO 和 SAM 的服务端推理耗时返回页面，并按 observation 持久化记录，便于之后比较模型性能。

## 现有实现与复用依据

- `src/pose6d/pose6d_bridge/node.py` 已同步采集并冻结原分辨率 RGB、对齐米制深度、内参和拍照时刻 TF；`ObservationStore` 限制为一个有界 observation，并验证 mask 与 RGB-D 尺寸一致。
- 当前页面通过 `pose6d_infer.mask_png` 上传用户制作的单通道 PNG。FoundationPose 服务已使用二进制 WebSocket/MessagePack 接口并校验 RGB、深度、mask、内参和模型身份。
- YOLO 参考实现位于 `/home/yixiaoshen/Repo/chaung_wei_2_jiao_fu/backend/contact_graspnet_ros2/contact_graspnet_ros2/inference_worker.py` 的 `YOLOv8Detector`，权重位于 `backend/models/yolov8n.pt`。该权重实际是自定义五类（adapter、eu、uk、router、line），不是 COCO 通用权重；复制时读取 `model.names` 并核对权重哈希，不根据文件名推断类别。
- 源 YOLO 仓库目前有未提交改动，且 detector、类别映射和模型文件都处于工作树内容中。实施时先记录相关文件的 `git diff`、权重哈希和实际类别表，只复制实现所需的 detector、配置及权重；不清理或修改该仓库，不让新服务在运行时依赖它的绝对路径或 Python 环境。
- SAM 代码直接使用 `/home/yixiaoshen/Repo/sam2`，配置 `configs/sam2.1/sam2.1_hiera_t.yaml`，权重 `/home/yixiaoshen/Repo/sam2/checkpoints/sam2.1_hiera_tiny.pt`。使用 `SAM2ImagePredictor` 的静态图片 box prompting，不采用视频 predictor 或自动全图 mask generator。

## 目标架构

```text
6d_pose.html: 采集一帧
  -> pose6d_bridge: 冻结 observation 并触发自动视觉流程
  -> YOLO WebSocket Docker: 同帧全目标 bbox / 类别 / 置信度 / YOLO 耗时
  -> SAM2.1 Tiny WebSocket Docker: 对每个 bbox 提示分割 / 独立 mask / SAM 耗时
  -> bridge 与页面: 展示框、实例列表、各实例 mask 预览和耗时
  -> 用户选定 instance_id
  -> bridge 将该实例二值 mask 送入现有 FoundationPose WebSocket
  -> 页面展示现有位姿、几何筛查和吸取方式结果
```

YOLO 和 SAM 服务作为独立 GPU 容器，不安装 ROS、不接触机器人接口。页面只与现有 WebSocket/ROS 桥通信；bridge 是模型服务的唯一客户端，负责 observation 和请求身份校验。服务端口只绑定回环地址，沿用当前 `pose6d_bridge` 的 `127.0.0.1` 访问方式。模型服务按单请求有界执行并预加载模型；请求超时、断线或新 observation 到来时，旧响应不得覆盖新页面状态。

## 代码和 Docker 布局

- 在 `/home/yixiaoshen/Repo/realman_pose_vision/` 新建独立目录，保存从 YOLO 项目提取并适配的独立服务代码、依赖锁定文件、Dockerfile、协议和服务测试。服务不导入 `chaung_wei_2_jiao_fu` 包，也不在启动时下载未锁定权重。
- YOLO 权重和类别映射作为该独立目录部署资产的可复现副本，启动时检查文件哈希及模型类别。记录来源仓库、源文件、源提交/工作树状态、类别表、权重 SHA-256 和依赖版本。
- SAM 服务包装代码放在独立视觉服务目录；构建时通过 Docker Compose `additional_contexts` 引用现有 `/home/yixiaoshen/Repo/sam2` 源码，不复制或改写 SAM2 上游实现。Tiny checkpoint 以只读 volume 挂载，运行时校验配置、checkpoint 存在和 SHA-256。
- 在 `config/docker/compose.pose6d.yaml` 增加可选 YOLO、SAM 服务和 GPU/回环端口设置；在 `config/ros/pose6d_bridge.yaml` 增加两个 WebSocket 地址及连接、推理超时和请求上限。FoundationPose、YOLO、SAM 服务都只在显式 6D pose 部署时启动，不加入普通 bringup。
- Docker 镜像固定基础镜像和 Python/CUDA/PyTorch/Ultralytics/SAM2 依赖版本；容器使用只读根文件系统、临时目录和最小权限。GPU 前提、checkpoint 挂载和启动命令写入部署说明。

## 推理与 mask 契约

1. `/6d_pose.html` 收到 `pose6d_capture` 后，使用该 `observation_id` 自动发起一次检测分割命令并显示 `YOLO -> SAM` 进度。不能重新取相机最新帧，也不能从页面上传另一张图替代冻结帧。
2. YOLO 接收 RGB `uint8[H,W,3]`，颜色顺序明确为 RGB；返回所有未过滤检测，bbox 使用冻结图像坐标下的整数 `[x1,y1,x2,y2]`，边界裁剪到图像范围，并返回稳定的检测序号、原始 class name 和 confidence。置信阈值可配置并回传；不能按当前 FoundationPose 对象名过滤检测。
3. SAM 对同一张 RGB 只执行一次 `set_image()`，然后按 YOLO 检测框逐个调用 box prompt，首版 `multimask_output=False`，每个 bbox 产出一个独立实例。返回 YOLO 检测 ID、SAM score、前景像素数及逐实例耗时。一个框失败时保留其他成功实例并报告该实例错误。
4. SAM mask 必须与冻结 RGB 完全同尺寸。若上游 API 返回尺寸不同，只能用 nearest-neighbor 恢复到输入尺寸并在响应中校验；不做拉伸、裁剪或与其他实例融合。内部统一为 `uint8[H,W]` 且值仅为 `{0,1}`。桥接端在进入 FoundationPose 前再校验 observation、instance、尺寸、前景和有效深度像素；转换到现有 FoundationPose 请求格式时不经手工 PNG 编辑。
5. bridge 为每个 mask 分配 observation 范围内稳定的 `instance_id`，将所有 mask 保存在当前 observation 的有界缓存中。新采集、owner 断开、observation 失效或显式清理时一起释放检测和 mask。选 instance 后 `pose6d_infer` 只发送 `observation_id`、`instance_id` 和 request ID；bridge 查缓存并填入现有 `ObservationStore`，不能信任前端回传的任意 mask 数据。
6. 当前 mask 上传入口保留为显式的手工兜底路径；自动检测和分割是默认流程。手工 mask 不能伪装成 SAM 结果，耗时记录中应标注输入来源。
7. YOLO、SAM 和页面事件均校验协议版本、请求 ID、observation ID、图像维度、dtype、实例数量和消息字节数。对最大检测数、总 mask 数据量和 Web/ROS 消息上限设置明确配置/校验；不得将无界数量的全分辨率 mask 一次性塞进 JSON。实例列表只传元数据，页面按需请求当前选择实例的 PNG 预览，完整二值 mask 留在 bridge。

## 页面交互

- 采集按钮行为改为“采集并自动分割”：先显示冻结 RGB-D 与采集状态，再显示 YOLO 进度、检测数量、YOLO 耗时和检测框叠加图，最后显示 SAM 进度与结果。
- 在同一结果区列出所有 YOLO 检测及对应 SAM 状态、类别、置信度、SAM score、mask 面积、`SAM ms`。点击某一项显示该实例单独的彩色 mask overlay；检测框总览与 SAM mask 预览可切换，避免多个重叠 mask 混在一张图上。
- 只有 SAM 成功且通过尺寸/有效深度检查的实例可选为 FoundationPose 输入。选择后显式显示当前 `instance_id` 和 mask 预览，再点击“解算位姿”；从 FoundationPose 返回后继续使用既有 warning、几何指标、3D 显示和人工运动复核门禁。
- 显示 YOLO 单次耗时、SAM 每实例耗时与合计耗时，并区分“模型推理时间”和“端到端请求时间”。错误、零检测、全实例分割失败、缓存失效、超时和重试状态都要有可见反馈；不能显示上一 observation 的框或 mask。

## 耗时定义与持久化

- 服务端用 `time.perf_counter()` 计时。`yolo_inference_ms` 覆盖 Ultralytics 模型调用（含其预处理/NMS），`yolo_request_ms` 覆盖该服务的完整请求处理；`sam_inference_ms` 是每个 bbox 的 predictor 调用耗时，`sam_inference_ms_total` 是逐实例耗时之和，`sam_request_ms` 覆盖整次 SAM 请求。另由 bridge 记录从发起到收到响应的 `yolo_round_trip_ms`、`sam_round_trip_ms`，这些值可能包含 WebSocket/序列化和等待时间，不能与 GPU 推理时间混称。
- 成功和失败都生成一条按 observation/request 关联的 JSONL 记录，写入 `realman_pi/logs/pose6d_vision/timings.jsonl`，由 bridge 在现有 logs volume 中追加。字段至少含 UTC 时间、observation/request ID、图像尺寸、YOLO/SAM 模型与权重哈希、检测数、成功/失败实例数、上述耗时、状态/错误码及 `mask_source`；不写入 RGB、深度或 mask 像素。页面返回值使用同一组服务端时间字段。
- 验证并发写入、进程重启后的追加、日志目录不可写时的清晰告警；日志失败不能把已成功的推理伪装成失败。记录文件轮转和保留量遵循项目日志管理约定，避免无限增长。

## 实施分阶段

### 阶段 1：独立 YOLO 服务

- [x] 检查并记录源仓库相关改动、权重哈希和真实 `model.names`；挑选最小必要实现和依赖，不复制 MobileSAM、Contact-GraspNet 或整份 worker。
- [x] 新建 `/home/yixiaoshen/Repo/realman_pose_vision/yolo/`，实现预加载 YOLO、WebSocket 请求校验、结构化检测结果、ready/hello、超时、模型身份和计时。
- [x] 为自定义权重配置显式路径与 SHA-256，禁止在模型缺失时静默下载官方 COCO 权重。
- [x] 构建并运行 GPU Docker；实际 right-wrist RGB-D 检测验证了类别、原图坐标、检测数量和计时字段。坏包/零检测故障注入待补。

### 阶段 2：SAM2.1 Tiny 服务

- [x] 新建同一独立目录下的 `sam2/` 服务包装和 Docker 配置；通过额外 build context 使用现有 SAM2 源码与只读 Tiny checkpoint。
- [x] 实现预加载 image predictor、按 bbox 分割所有 YOLO 结果、mask 尺寸/二值化校验、逐实例耗时、模型身份、错误隔离和就绪状态。
- [x] 构建并运行 GPU Docker；真实帧中 16 个 bbox 均返回同尺寸 mask、score 和耗时。单实例失败故障注入待补。

### 阶段 3：ROS bridge、WebSocket 协议与页面

- [x] 扩展 `ObservationStore` 和 `pose6d_bridge`，新增自动检测分割命令、分阶段事件、YOLO/SAM WebSocket 客户端、timeout/owner/generation 校验和有界 instance mask 缓存。
- [x] 增加 FoundationPose 的 instance ID 选择路径；既有注册、模型哈希、mask 有效深度、筛查和结果门禁保持有效。
- [x] 更新 `realman_web_control` 的命令解析、事件类型和大小限制；确保页面不能通过跨客户端 ID 读取 observation 或 mask。
- [x] 更新 `web/src/pose6d.ts`、HTML/CSS 和构建后的静态页面：自动处理进度、框和实例展示、按需预览、选择与计时显示。新增消息带 observation/request 身份，重采集立即清空 UI 选择。
- [x] 在 bridge 记录 JSONL 耗时，并更新配置、Compose 和 `rm65` 入口；视觉端口仅回环绑定。

### 阶段 4：验收

- [x] bridge、ObservationStore 和页面测试覆盖实例/observation 绑定、mask 尺寸和值域、有效深度、owner 隔离、消息大小、耗时日志及正常选择流程；容器坏包/零检测/单实例故障注入留待补充。
- [x] 真实冻结 RGB-D -> YOLO -> 每目标 SAM -> 页面实例预览 -> 选定实例 -> FoundationPose 位姿端到端通过。
- [x] Playwright 页面正常采集、实例选择和计时展示回归覆盖桌面、移动视口；零检测和 SAM 失败交互待补充。
- [x] 使用 Compose 构建并运行三个 GPU 服务；实测 YOLO/SAM 健康状态、模型哈希、GPU 推理、回环端口和日志落盘。
- [x] 在真实相机页面完成 right-wrist RGB-D 视觉验收；检查结果同帧同尺寸且没有发送机械臂运动指令，warning 和人工运动门禁仍有效。

## 不在本计划范围内

- 视频跟踪、跨帧实例 ID、SAM 全图自动 mask generator、自动挑选目标或自动选择 mask。
- FoundationPose 模型/物体网格更换、位姿筛查阈值标定、吸盘几何调整和运动策略变更。
- 修改 `chaung_wei_2_jiao_fu` 的抓取 worker、现有 YOLO 训练权重或 SAM2 上游代码。
