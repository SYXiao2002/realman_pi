# Grasp Web 离线评估页实施计划

状态：已实施（只读评估，不执行机械臂或夹爪动作）

## 目标

在现有 `realman_web_control` portal 中增加 `/grasp.html`，让操作员能够选择机械臂 profile 和目标类别，查看一次 grasp observation 的 RGB、深度、policy 候选、机械臂布局与规划诊断，并把人工评价导出为 JSON。页面服务于离线评估和数据审阅，不提供运动、夹爪或行为树执行入口。

## 边界和安全约束

- 页面默认只读；任何候选点击只改变本地选择和标注。
- 不调用 `ExecuteMotion`、`ExecuteTrajectory`、速度控制、夹爪 service 或行为树启动接口。
- “运行一次规划评估”只允许调用 planning-only `PlanGrasp`，并在 UI 明确显示 dry-run/不可执行状态。
- profile、相机、标定、gripper 几何和 policy backend 必须一起显示；缺少实测几何或校验标记时显示未就绪，不隐藏原因。
- 每次观测使用 `observation_id`、采样时间、profile/config identity、camera frame 和 joint snapshot 绑定，拒绝跨观测拼接。

## 数据流

```text
腕部 RGB-D + CameraInfo + JointState + TF
  -> grasp_bridge（配对、反投影、policy、候选/规划诊断）
  -> `/grasp_bridge/evaluation` 原子 JSON snapshot + Action feedback/result
  -> realman_web_control（订阅、编码、WebSocket 事件）
  -> grasp.html（图像、候选表、3D 线框/机械臂布局、评价 JSON）
```

统一事件至少包含：`observation_id`、`capture_timestamp_ns`、`profile_id`、`arm`、`camera_frame`、`target_label`、`backend` 和 `read_only`。图像事件使用 JPEG/PNG data URL；深度同时提供米制统计和可视化灰度图，禁止把显示值当作规划输入。

## 页面信息架构

1. **评估上下文**：arm/profile、目标类别、backend、连接/bridge readiness、标定和 gripper 几何状态。
2. **观测窗口**：RGB、深度、RGB overlay、时间戳和 RGB-D 配对健康。
3. **候选与规划**：候选表（score、宽度、相机系 pose、基座系 pose、过滤原因）、候选夹爪线框/坐标轴、当前关节和 base→camera 布局；规划结果只展示 pregrasp/grasp/lift waypoints。
4. **人工复核**：候选选择、`accepted/rejected/uncertain`、失败原因、备注；保存到 localStorage 并支持下载 `review.json`。

## 分阶段任务

### A. 评估数据契约

- [x] 增加 bridge 的 RGB、registered depth、候选详细 JSON、overlay、pose/diagnostic 快照。
- [x] 在候选 JSON 中保留完整 `T_camera_grasp`、可用时的 `T_base_grasp`、score、width、object id 和 policy metrics。
- 增加 observation cache，确保页面首次连接可收到最近一次完整快照；旧 observation 到达时丢弃。
- [x] 为快照几何和只读 Action 添加离线测试。

### B. Web control ROS/WebSocket 桥

- [x] `web_control_node.py` 订阅 grasp bridge 只读快照，并缓存最近状态供新 WebSocket 客户端恢复。
- [x] 保留现有控制协议；grasp 页面不增加任何运动或夹爪写入命令。
- [x] 增加请求协议、快照校验、observation identity 和 readiness 测试。

### C. Portal 页面

- [x] 新增 `grasp.html`、`grasp.ts` 和页面样式；更新 Vite multi-page input 与静态构建产物。
- 复用现有 `theme.css`、panel/button/status 组件，移动端采用单列布局。
- [x] RGB/depth/overlay 支持 observation 绑定显示；候选表与图像投影保持同步。
- [x] 用 Canvas/Three.js 绘制候选 grasp wireframe、base/camera 轴、相机系点云和当前臂布局。

### D. 人工评价和验证

- [x] localStorage key 按 observation/profile 分区，导出带 schema version 的 JSON。
- [x] 增加 Web protocol、bridge publisher、页面静态路由和 Playwright smoke tests。
- [x] 构建 Web control，运行相关 Python 测试、`npm run build:web-control` 和网站 style gate。
- [x] 以 dry-run 和离线 fixture 验证评估流程；不连接真实运动入口，不执行机器人动作。

## 验收标准

- 页面能显示所选 profile、目标类别、RGB、深度、overlay、候选 pose/score/width、机械臂关节/布局和评估诊断。
- 所有可视化数据带 observation identity，切换新观测不会残留旧候选。
- 未批准标定时页面仍可审阅 RGB-D 和相机系点云；夹爪几何缺失时保留推理拒绝原因并标注“不可执行”。两个 backend 都依赖夹爪尺寸过滤候选，不能在尺寸未测量时保证返回 policy pose。
- 评价 JSON 可下载并可离线复现候选选择；页面不存在运动或夹爪写入命令。
- 相关测试通过；无法运行的宿主依赖在最终报告中列明。

## 实施验证

- `realman_msgs` 在 `/tmp/realman-grasp-review-*` 独立目录构建成功；grasp Action、evaluation snapshot、grasp protocol、Web protocol 和 evaluation cache 测试共 57 项通过。
- Grasp Portal Playwright 桌面/移动端共 4 项通过，`npm run build:web-control` 和网站 style gate 通过。
- VitePress 完整 build 因工作区用户文件 `config/ros/three_robots.yaml` 当前不可读而无法运行；该文件未读取、修改或纳入提交。
- 当前 `config/grasp/gripper_specs.yaml` 的左右夹爪尺寸均为 `null` 且未验证。完成物理测量并更新权威配置后，portal 才能对这只 EU 插头显示 policy 候选；标定/夹爪 readiness 未批准时不可执行。
