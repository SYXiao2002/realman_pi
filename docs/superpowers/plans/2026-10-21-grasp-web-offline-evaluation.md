# Grasp Web 离线评估页实施计划

状态：实施中（只读评估，不执行机械臂或夹爪动作）

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
  -> ROS topics（RGB、depth、overlay、candidates、plan、diagnostics）
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

- 增加 bridge 的 RGB、registered depth、候选详细 JSON、overlay、plan/diagnostic 事件。
- 在候选 JSON 中保留完整 `T_camera_grasp`、`T_base_grasp`、score、width、object id、filter reason。
- 增加 observation cache，确保页面首次连接可收到最近一次完整快照；旧 observation 到达时丢弃。
- 为协议和候选身份添加纯 Python 测试。

### B. Web control ROS/WebSocket 桥

- `web_control_node.py` 订阅 grasp bridge 只读 topics，并把 ROS 图像安全编码后广播。
- 新增 cached grasp state，client reconnect 后可恢复最近一次 snapshot。
- 保留现有控制协议；grasp 页面不增加任何写入命令。
- 增加事件 schema 测试，确认大图、非法 JSON、旧 observation 和未就绪 profile 的处理。

### C. Portal 页面

- 新增 `grasp.html`、`grasp.ts` 和页面样式；更新 Vite multi-page input 与静态构建产物。
- 复用现有 `theme.css`、panel/button/status 组件，移动端采用单列布局。
- RGB/depth/overlay 支持最近帧显示；候选表与图像点击保持同步。
- 用 Canvas/Three.js 绘制候选 grasp wireframe、base/camera 轴和当前臂布局；没有数据时显示明确空状态。

### D. 人工评价和验证

- localStorage key 按 observation/profile 分区，导出带 schema version 的 JSON。
- 增加 Web protocol、bridge publisher、页面静态路由和 Playwright smoke tests。
- 构建 Web control，运行相关 Python 测试、`npm run build:web-control`、网站 build/style gate。
- 以 dry-run 和离线 fixture 验证完整流程；不连接真实运动入口，不执行机器人动作。

## 验收标准

- 页面能显示所选 profile、目标类别、RGB、深度、overlay、候选 pose/score/width、机械臂关节/布局和规划结果。
- 所有可视化数据带 observation identity，切换新观测不会残留旧候选。
- 未通过标定/夹爪 readiness 时页面仍可审阅图像和 policy 候选，但明确标注“不可执行”。
- 评价 JSON 可下载并可离线复现候选选择；页面不存在运动或夹爪写入命令。
- 相关测试通过；无法运行的宿主依赖在最终报告中列明。
