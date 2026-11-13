# 单帧 6D Pose 与吸盘法向点动实施计划

状态：软件实现、FoundationPose/ROS 容器构建、离线推理和 Web/ROS 隔离验证已完成；right wrist RGB-D 到 `/6d_pose.html` 的现场采集验证通过。右臂 `tcpsuck` 已注册，当前安装偏移与方向以 `config/ros/realman_coordinates.yaml` 和控制器匹配回读为准；6D Pose 以 `tcpsuck` 原点作为吸盘接触 TCP，运动配置已启用。实时控制器状态已回报 `current_tool=tcpsuck` 且坐标匹配。尚未执行实机运动或接触测试，吸盘轴向、本体外形及扫掠/工作区仍须现场核对。

## 目标与范围

在 `realman_pi` 中新增 `/6d_pose.html`、6D Pose bridge 和独立的 FoundationPose GPU Docker。操作员冻结一帧腕部 RGB-D，下载原分辨率 RGB，在外部制作并上传单实例 mask；后端仅运行一次 `register()`，输出物体到相机的位姿，并按 `FoundationPose/POSE_GEOMETRY_VALIDATION.md` 做几何筛查。页面展示 RGB、深度、mask、筛查告警、点云、原始 OBJ 和吸取方式；操作员选择方式后，可以先 MOVEL 到距估计接触面 40 mm 的准备位，再通过 `+/-` 以可调步长沿吸盘法向移动到有符号距离 `x`，包括负值。

首版限定一个静止物体、一帧、一个 `eu_plug` 模型、一个人工选择的吸取方式。每次调用都是独立注册，不运行跟踪；不实现自动分割、吸气控制、自动接触、抓取、抬起或力控。离线 JSON 中的四个 `pending` 状态原样展示，人工已确认的标注与实际接触成功是不同事实。

## 现有依据与复用边界

- `website/docs/development/grasping.md`：参考 `src/grasp/grasp_bridge/` 的 RGB-D 配对、D2C、停稳、采集时 TF、独立 WebSocket 和离线快照。抽取可复用组件或提供稳定接口，避免维护两份不同的配准和标定算法；不把现有夹爪候选及其碰撞盒当作吸盘模型。
- `website/docs/development/realman-web-control.md`、`realman-action-development.md`：复用 Web control、驱动 `/<arm>/execute_motion`、坐标门禁、停止及每臂 ownership；不要从网页或算法容器直接调用厂商 SDK。`grasp.html` 的手动 MOVEL 未提供完整路径碰撞检查，不能直接视为本功能的安全检查。
- `FoundationPose/web/multi_pose_viewer/README.md`、`pose2suc/docs/downstream_interface.md`：复用显示方式和标注语义；6D pose 直接变换原始 OBJ 顶点，不再居中或缩放。
- 项目 `.agents/skills/realman-pi-architecture`、`project-config-layout`、`ros2-logging-conventions`、`document-feature-updates` 和 `realman-python-driver` 是实施约束；ROS 运行日志采用官方接口，权威配置归根目录 `config/`。涉及 SDK 细节时查 `doc/02-motion-and-teaching.md`、`doc/04-coordinate-and-algorithm.md`、`doc/05-force-and-safety.md` 及相应官方文档和固件版本。

## 组件与数据流

```text
RGB + Depth + CameraInfo + 关节/TF
  -> 6d_pose_bridge: 同步、D2C、冻结 observation 与拍照 TF
  -> realman_web_control -> 6d_pose.html: 下载 RGB、上传 mask
  -> 6d_pose_bridge -> WebSocket -> FoundationPose Docker: register + 几何筛查
  -> bridge: 结果与观测绑定、转换吸取位姿、发布评审快照
  -> Web: 图像叠加、点云/OBJ/吸盘、选择方式和有符号距离
  -> 服务端运动会话: 校验并通过现有驱动 Action 发送 MOVEL
```

- 建议在 `src/` 下增加独立 6D pose 协议、bridge、算法服务和 suction 几何/运动会话模块，按现有 ROS 包及 Web control 约定组织；公共 ROS 接口置于 `realman_msgs` 或专用接口包，明确消息大小和生命周期。算法容器不安装 ROS、不访问机器人 SDK。
- 在 `config/docker/` 新增独立 Dockerfile/Compose，按需启动，默认不随机器人常规 bringup 启动。锁定可复现的 CUDA/PyTorch/依赖、FoundationPose 源码及权重身份；验证 GPU、CUDA、nvdiffrast 和权重可用性，提供健康/就绪状态、推理超时、错误和受控并发。源码/权重与模型的来源、构建上下文、镜像与 GPU 前提写入文档；不假设宿主 `.venv` 可用于 Docker。
- 在 `config/` 下增加有注释的服务、相机 profile、模型注册表、几何筛查、吸盘 TCP、速度、步长、行程及容差配置；不复用现有 `tcpgrip` 的单位变换冒充吸盘标定。配置缺失时仍可解算和审阅，运动显示具体未就绪原因。
- 默认预注册 `eu_plug`：`FoundationPose/demo_data/eu-plug/mesh_eu-plug/TexturedMeshSimplified.obj`、其 MTL/纹理和同目录 `eu_plug_grasps.json`。以部署可配置的只读资源目录或构建上下文提供，记录 OBJ/材质/纹理/标注哈希与版本；不依赖页面传宿主机绝对路径。服务启动校验一致性并缓存模型、网络权重和 CUDA context；请求传 `object_id` 和模型身份，不重复上传 OBJ。

## 冻结观测、mask 与接口契约

1. 操作员选择 profile 并采集。检查 RGB/depth/CameraInfo 时间差、新鲜度、分辨率、相机 frame、停稳和标定身份；沿用现有 D2C 与去畸变，生成与 RGB 同像素、同内参的深度，`16UC1` 毫米转米、`32FC1` 米，失效深度保留为无效值。显示用深度图不得用于推理。
2. 原子冻结 `observation_id`、采集时间、RGB、米制深度、K、相机光学 frame、profile、模型/标定身份，以及基于 RGB 时间查询得到的 `T_base_camera` 和关节状态。采集 TF 缺失时保留可用于相机系审阅的能力，但禁止生成基座运动目标；不得回退最新 TF 或单位阵。
3. 下载原分辨率、无损 RGB PNG，文件名包含 observation ID。上传单通道或明确转换后的二值 PNG；非零为前景，检查尺寸、空 mask、解码大小和合法范围，显示叠加预览。上传请求必须携带 observation ID，替换观测或模型后旧 mask、结果、运动会话全部失效；保留有界缓存，在替换、断线或关闭时清理，不按 observation 年龄自动过期，避免大图进入无界 ROS JSON 或 WebSocket。
4. 离线制作 mask 期间锁定该快照；不能仅凭文件名或尺寸证明 mask 来自该帧，页面提醒用户确认。视觉审阅可使用旧快照，但运动要求该物体和环境未发生变化，由操作员在运动前确认；时间过期或状态变化后失效，重新采集并制作 mask。机械臂为了移动到准备位会离开拍照姿态，因此不能用“当前位置必须等于拍照姿态”作为第二步永久门禁。
5. 版本化、限长的二进制 WebSocket 协议携带 request/observation ID、对象 ID 与模型哈希、RGB `uint8 H×W×3`（明确 RGB 顺序）、对齐米制深度 `float32 H×W`、二值 mask、`K`、图像尺寸和时间。对尺寸、dtype、数组长度、内参、非有限值、帧大小及未知模型做服务端校验；超时、重复响应、重连及取消不得混入其他观测。响应包含相同身份、`T_camera_object`、单位/坐标约定、网络排序分数、筛查状态、各项指标及告警。

## 注册与几何筛查

- 调用 `FoundationPose.register(K, rgb, depth, ob_mask)`，每次独立注册。`estimater.py` 在有效深度少于 4 点时会返回猜测平移的矩阵：封装必须识别这一退化分支并返回错误，不把它作为成功 6D。注册异常、空 mask、无效内参/深度、非有限或非刚体结果返回结构化错误，不能伪造位姿。
- 使用同一原始 OBJ 与注册输出，在 RGB 图像大小上渲染完整三角网格及透视正确的相机 Z 深度，z-buffer 处理遮挡；比较冻结观测的对齐原始深度，而不是 register 内部滤波后的深度。输出 silhouette IoU、mask/render 覆盖率、深度误差中位数及 P90、阈值内比例、mask/render/overlap/有效深度像素数，以及 mask/轮廓/残差叠加图。
- 区分 `pass_screening`、`suspect`、`insufficient_evidence`、`invalid`。有效 pose 遇筛查异常或证据不足时返回 pose + warning + 指标；渲染无效时返回 pose + invalid warning，推理无有效 pose 则返回错误。阈值、最小样本、版本和单位可配置并返回；文档样例不是通用阈值，未完成现场标定时只作诊断而不宣称通过正确性验证。网络排序分数不是概率。
- warning 始终显示，允许人工复核位姿；运动入口要求操作者逐次明确确认当前结果及 warning，不自动将结果送入运动。筛查通过同样不保证姿态真实正确。

## 坐标、吸盘和三维显示

采用列向量和 `T_A_B` 把 B 系变换到 A 系，长度统一 m。`T_base_object = T_base_camera(capture) @ T_camera_object`；标注点 `c_obj` 变换为 `c_base = R_base_object c_obj + t_base_object`，朝外法向 `n_base = R_base_object n_obj`。从 `tangent_ref_obj` 转换并正交化，按 `pose2suc` 约定 `z = -n_base`、`x = normalize(tangent - (tangent·z)z)`、`y = z × x`，`R_base_contact = [x y z]`。拒绝非单位法向、退化切向、非刚体矩阵及无效标注，四元数在接口边界明确转换：`pose2suc` 为 XYZW，现有机器人 Action 为 WXYZ。

定义 `T_contact_tcp` 为从真实控制器 TCP 系到理想吸盘接触系的实测刚体变换；有符号距离 `d = x_mm / 1000` 时，`T_base_contact(d)` 的旋转不变、平移为 `c_base + d*n_base`，`T_base_tcp(d) = T_base_contact(d) @ T_contact_tcp`。YAML 用接触系表达的 TCP 原点 `xyz_m` 和 `[roll, pitch, yaw]` `rpy_rad` 表示该变换，角度单位为弧度，旋转按 `Rz(yaw) Ry(pitch) Rx(roll)` 构造；运动内部仍使用 4×4 刚体矩阵。该变换须与控制器选中的工具、真实吸盘接触点和姿态一致，避免把控制器工具偏移叠加两次。标定缺失或工具身份不匹配时禁止运动。`radius_m` 是标注接触区域半径，不等于实体吸盘半径，也不进入目标位移；实体吸盘尺寸单独配置。

页面参考 `web/multi_pose_viewer` 和现有 Three.js viewer 显示原 RGB-D 点云、OBJ、对象轴、四个吸取接触圆/法向/吸盘实体示意及选中方式；展示原始相机系 pose、基座系目标、警告和模型身份。吸盘及 OBJ 使用同一变换和原始模型坐标；可开关可见性并突出选中方式。显示是对采集帧的重建，不代表实时障碍状态。

## 运动会话与有符号点动

- 页面先选吸取方式、复核 pose 和几何告警，再显示接触点、朝外法向、`40 mm` 准备位、目标及路径。第一按钮发出 BASE 系 MOVEL 到 `d=+40 mm`；它从当前位置直线到准备位，不保证沿法向。到位须依据新鲜的实际 TCP 位姿反馈确认位置、姿态及停稳，Action 接受或单纯超时均不等于到位。
- 第二步提供 `+/-`、可调正数步长（例如 0.5、1、5 mm）和当前实际/目标 `x`。到准备位后初始目标 `x=+40 mm`；点击 `+` 远离物体，`-` 靠近物体，允许从正值越过零到负值。每次 `x_new = x_confirmed ± step`，根据冻结的接触坐标及 `T_contact_tcp` 重新生成 BASE 目标，以低速 MOVEL 直线执行；姿态保持不变。显示 `x` 相对估计表面，负值不是实测压缩量，位置控制不提供接触力限制。
- 执行前服务端从最新实际 TCP 反解接触系，检查其横向偏差、朝向偏差、轴向距离及反馈新鲜度；确认它在冻结的法向线上且与上次确认目标一致，才允许下一步。轴向与朝向容差须根据实机精度配置；从线上偏离时禁用点动并要求恢复准备位或重新采集。第一段的完整路径也要单独评估，目标可达或线段有效不代表整臂无碰撞。
- 设定经过实测的 `x_min < 0`、`x_max >= 40 mm`、步长、最大单步距离、速度、超时、横向/姿态容差、工作空间和吸盘几何；这些都是有单位的权威配置，不虚构安全数值。缺失行程上下限或 TCP 标定时运动不可用，但不妨碍可视化。每个目标及已知吸盘扫掠段进行工作区、可达性及配置的点云碰撞检查；盲区、腕部/连杆和全场景碰撞能力须明示，不能将 `unknown` 显示为安全。
- 运动协调在服务端持有观测、选中方式、目标和会话身份；每次命令独立校验身份、工具、状态、距离、边界、可达性、控制权及连接。Web 验证仅用于提示。一个 Action 在途时不排队、不合并、不重复发送，按钮禁用；成功终态并验证实际到位后才提交新 `x_confirmed`。失败、取消、停止、读回失效、连接中断、超时或状态未知时锁定会话，不猜测停止、不自动重发；重新读取状态并按现有驱动恢复流程处理。提供取消 Action 和软件停止，现场物理急停独立有效。
- 清晰的会话状态：`review → preparing → ready → jogging → ready`；异常进入 `blocked/unknown`。重新采集、切换模型/profile/方式、TF 或工具变更、外部控制造成偏移、会话失效均使旧点动会话失效。协调与现有 per-arm ownership/任务 token，不绕过竞争命令门禁；界面断线后不得在服务端继续处理缓存点动。

## 分阶段实施

### 1. 协议、资源和离线基线

- [x] 固定来源和模型资源哈希，校验 `eu_plug_grasps.json` schema、模型单位和法向约定；建立有界协议及错误码，准备来自同一帧的 RGB、深度、mask、K 离线 fixture。
- [x] 用真实离线数据验证 `register()` 输出能直接变换原始 OBJ；验证退化深度分支按错误处理。定义可复现的模型和权重加载、部署前置条件。
- [x] 实现并测试纯几何转换及 `T_contact_tcp`；用带旋转平移的非单位示例检验变换方向、姿态、四元数顺序和正负距离。

### 2. 独立 FoundationPose 服务和几何筛查

- [x] 创建容器、资源注册与 WebSocket 服务，warmup、超时、状态、取消、并发/队列边界和版本身份可观察；不提供追踪接口。
- [x] 实现三角网格 Z 渲染及几何指标；用合成/真实 EU plug 数据验证输出。
- [x] 用离线 WS 回放验证真实模型注册、请求身份、无效输入和 warning/错误语义。

### 3. bridge 与 Web 审阅

- [x] 接入配对采集、D2C、冻结观测、拍照 TF、缓存、mask 上传及 ROS/Web 状态；单元测试覆盖跨观测和晚到结果失效。实时相机链路待设备验证。
- [x] 新增 `/6d_pose.html` 及 Vite 入口、导航、静态构建资源：采集、下载 RGB、上传/预览 mask、运行一次、warning 与指标、3D 点云/OBJ/四种吸取方式。无标定时可做相机系审阅。
- [x] 结果绑定吸取标注，服务端根据冻结拍照 TF 计算接触姿态和 TCP 目标；浏览器只提交身份与步长。

### 4. 运动会话与模拟验证

- [x] 实现服务端 `40 mm` 准备位与可正负点动状态机，复用现有 Action、任务租约、停止和网页心跳；浏览器只能提交身份和步长，Action/readback 未确认时不提交当前距离。
- [x] 单元/fake-action 覆盖并发请求、拒绝、超时、未知终态、坐标变化、场景/warning 确认、位置漂移、扫掠碰撞和 `40 → 20 → 0 → -10 → +10 mm`，不连接真实机器人。
- [x] 根据用户提供的 200 mm 安装关系配置右臂 `tcpsuck`，并将 6D Pose 接触 TCP 绑定到该控制器工具；检查运行时工具名与坐标回报一致。
- [ ] 现场确认吸盘接触面确在 `tcpsuck` 原点且吸盘法线沿该 TCP 的局部 `+Z`；确认本体包络、路径/工作区、速度、容差和正负行程，再做低速空载验证。
- [ ] 使用 mock 驱动图验证 ROS bridge 到真实 Action/service 的完整状态流程，再进行只读现场检查。
- [ ] 实机测试前完成真实 TCP、工具几何、工作区、行程及环境核对；先无物体低速验证准备位与法向正负方向，再从较大正距离逐步接近。负距离测试应确认吸盘允许的机械压缩、无力反馈限制及急停；记录实际位姿与结果。

### 5. 文档与完成检查

- [x] 更新开发手册、ROS/config reference、架构包说明、启动方式、运动参数和碰撞局限；本计划记录剩余现场验收。
- [x] 完成最终 ROS build/test、服务/协议/几何 Python 测试、Web Playwright、`npm run build:web-control` / `npm run test:web-control` / `npm run build`、Compose 配置及 diff 检查；列明不能在无现场参数条件下执行的实机验收。

## 验收标准

1. 同一冻结观测能完成下载 RGB、上传 mask、WebSocket 单次注册和几何筛查；结果身份、单位、状态和告警清楚，输入错误不产生伪成功。
2. 点云、OBJ、坐标轴和四种吸盘方式在同一坐标系一致显示；已知坐标 fixture 的正负距离与 TCP 姿态计算可复现。
3. 无正确工具标定、无有效边界、观测失效或驱动状态未知时无法运动；准备位及每次点动均可取消/停止并有明确反馈。连续点击不会形成隐式队列或重复动作。
4. 模拟和适当的实机验证能够证明 `40 → 20 → 0 → 负值 → 正值` 按所选法向及配置边界运动；实际接触行为、标定数据和局限有记录。没有现场条件时保留实机验收未完成状态。

## 当前验证记录

- 2026-11-11 更新：预览增加原始切向参考、接触坐标轴、冻结/实时相机坐标系、实时活动 TCP 及选中方式的 +40 mm 准备吸盘。准备位由后端使用同一运动变换生成。删除 observation 年龄过期机制及配置项；关节反馈、工具、心跳和租约检查保留。修正停稳历史被 0.25 秒新鲜度窗口裁短、无法满足 0.5 秒停稳要求的问题，并在失败消息中给出具体反馈诊断。
- 2026-11-11 验证：相关 Python/ROS 回归 156 项、桌面/手机 pose6d 页面 6 项通过；Web 静态资源、文档构建、站点样式、Compose 配置与 diff 检查通过。ROS 镜像完整构建并完成镜像内可运行的 24 项检查，Python 回归在宿主 ROS 环境独立执行。`pose6d_bridge` 与 Web control 已更新到新镜像；实际 WebSocket 持续收到 `available=true` 的 TCP/相机坐标遥测。独立只读验证节点采集 `right_wrist` 848×530 RGB-D（标定 available），六关节停稳和 BASE TCP 回读成功；最新反馈约 0.017 秒、历史覆盖约 0.598 秒。未发送运动指令，现有浏览器的观测状态保留。
- 2026-11-11 归属修复：`rm65 up/down/status/logs` 纳入 `pose6d_bridge`，覆盖生产、desktop 和 policy 入口，离线 model 入口保持独立。Web control 定期及转发命令前同步实际在线 WebSocket ID；bridge 清理不在线 owner，并在进行中的采集/推理结束后清理断线审阅，显式 close 也释放观测归属。保留仍在线页面的归属和未确认释放的运动租约，不恢复 observation 年龄过期机制。
- 2026-11-11 归属验证：202 项 Python/ROS 测试、6 项桌面/手机页面测试、`tests/test_rm65_entry.sh`、文档构建与站点样式检查通过；镜像内可运行的 24 项检查通过。工具配置测试改为校验当前注册工具和接触变换，安装尺寸不再写死在测试；图层测试持续模拟真实 5 Hz 遥测。镜像已部署到 bridge/Web：真实浏览器首次采集、刷新后采集、保持 bridge 容器不变而单独重启 Web 后再采集均返回 `right_wrist` 848×530、`camera_right_color_optical_frame`、标定 available；另一个同时在线浏览器被正确拒绝。测试浏览器已关闭，无前端异常，未发送机械臂运动指令。
- 2026-11-11 工作区与候选显示：预览增加“配置工作区”checkbox，与切向参考/相机/当前 TCP/准备位开关并列。BASE 系线框来自运行中运动配置 `motion.workspace_m`，经冻结 `T_base_camera` 的逆变换与点云对齐，显示 XYZ 毫米范围；缺少手眼变换时提示待显示，实时遥测丢失不清除已知工作区。只绘制下拉框当前选中的吸取方式及其法向、切向、坐标轴和准备位；“显示全部”取景只包含可见几何。57 项相关 Python/ROS 和 10 项桌面/手机页面测试通过，覆盖工作区坐标变换、图层切换和非选中候选不参与渲染/取景；静态资源、文档构建及站点样式检查通过。
- 2026-11-11 工作区部署验证：ROS 镜像构建完成且镜像内 24 项检查通过，bridge/Web 已更新；真实浏览器采集到 `right_wrist` 848×530、手眼 available，确认采集与实时事件中的 BASE 工作区一致，checkbox 能隐藏并恢复线框。当前控制器上报 `tcpsuck · READY`；浏览器无异常，测试仅发送 `pose6d_capture`，未发送运动或工具切换。现场截图保存在 `/tmp/pose6d-workspace-live.png`。

- 本机 GPU 上使用 `single_ground01_eu-plug` 的同帧 RGB、深度、mask 和内参完成了单次注册，并通过 WebSocket 收到同一请求 ID 的结果。原始 OBJ 筛查的轮廓 IoU 约 0.827，重合区域深度误差中位数约 2.06 mm；阈值尚未校准，返回 `insufficient_evidence`。
- 几何变换、带旋转/平移的 TCP、`40 → 20 → 0 → -10 → +10 mm` 点动、越界拒绝、错误 owner、并发 Action、警告确认、工具坐标变化、点云接触/背景碰撞、心跳丢失与未知结果锁定通过隔离测试；ROS Action fake 测试不连接真实驱动。
- 使用本机 `nvcr.io/nvidian/bundlesdf:latest`、预装 PyTorch3D 和固定提交的本地 nvdiffrast 缓存完成容器构建；默认基础镜像路径尚未验证。容器真实样例的轮廓 IoU 约 0.843，重合区域深度误差中位数约 1.66 mm；阈值未标定，返回 `insufficient_evidence`。
- ROS 桥的推理方法已用冻结的真实样例接入容器，返回 4 个吸取方式及点云；桌面和移动端 Playwright 覆盖采集、上传、告警、可视化和画布旋转。
- ROS 驱动适配已实现并随新 ROS 镜像构建；临时容器可导入 bridge，fake-action 测试覆盖 Action 和租约边界。真实 ROS 图中 right wrist RGB-D 到页面的采集通过；实时坐标状态显示右臂当前工具 `tcpsuck`、配置和控制器回读匹配。运动配置已启用；真实 Action 终态、吸盘物理轴向、本体包络及接触行为尚未现场验证，本记录不代表已经完成吸取测试。
- 2026-11-10 最终验证：`colcon build --symlink-install --packages-up-to pose6d_bridge realman_web_control` 成功；新建 `/tmp/pose6d-colcon-test-results` 后对两个目标包执行 `colcon test`，结果为 281 项通过。另运行 `src/pose6d/tests`（26 项）和 Web 协议测试（33 项），全部通过。
- 2026-11-10 最终 Web 验证：pose6d 专项桌面/移动测试 4 项、WebControl 全量桌面/移动测试 116 项通过；`npm run build:web-control`、`npm run build`、站点样式检查、Compose 配置和 `git diff --check` 通过。
- 2026-11-10 最终容器验证：FoundationPose 服务使用 DaoCloud PyTorch 基础镜像和 USTC/NJU 包源构建，镜像 `realman-pose6d-server:local` 已运行；WebSocket hello 返回 `ready=true`、`object_id=eu_plug` 和模型 SHA-256。ROS 镜像 `rm65-humble-rviz:local` 成功重建，临时无网络容器确认 `pose6d_bridge` 已安装；镜像构建中的 ROS 测试 24 项通过。构建日志显示部分包因该镜像 Python 环境中未发现 pytest 而没有注册 Python 用例，Python 测试由上述宿主 ROS 环境单独执行。
- 2026-11-10 right wrist 现场采集：主机相机、生产 ROS 图、Web control、FoundationPose 服务均运行后，通过 `docker compose --profile pose6d up -d pose6d_bridge` 启动 bridge。用 Chrome 打开 `http://127.0.0.1:8765/6d_pose.html` 并点击“采集一帧”，页面返回 `right_wrist`、`camera_right_color_optical_frame`、RGB/Depth 848×530、标定状态 `available`；没有下发机械臂运动。
