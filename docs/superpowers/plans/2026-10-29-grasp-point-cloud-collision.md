# Grasp 点云夹爪防碰实施计划

状态：已实现（2026-10-29）

## 目标

在 `grasp_bridge` 中增加一个基于当前 RGB-D 点云的夹爪防碰模块，使用与网页现有可视化完全相同的两个手指盒体，评估每个抓取候选在接近、抓取和提起过程中的环境碰撞风险，并把结果同步展示到 `grasp.html` 和 `grasp_plan.html`。

本计划只处理两个手指盒体。明确不增加 palm、腕部、机械臂连杆、其他机械臂或闭爪过程中的动态力控模型。

## 已锁定的设计决策

- 碰撞模块属于 `src/grasp/grasp_bridge/grasp_bridge`，不新建独立 ROS 节点。
- 模块只使用 RGB-D 点云、实例分割、候选抓取 pose、profile 夹爪尺寸和规划参数；不访问串口、不调用 `gripper_manager`、不发送运动命令。
- 每个候选只生成一份规范的 `visual_finger_boxes/v1` 几何：两个平行手指盒体，开口为 `candidate.width_m + 2 * clearance_m`，再按 `min_opening_m` / `max_opening_m` 限制；手指尺寸为 `finger_thickness_m × finger_width_m × finger_depth_m`。
- `grasp_bridge` 的几何 helper 是这份模型的唯一生成处。碰撞计算使用 helper 返回的盒体；evaluation snapshot 携带同一份盒体描述；Web 对新快照只读取盒体中心和尺寸来渲染，不再按候选宽度或夹爪配置重算几何。这样碰撞体和网页显示体使用相同的尺寸、开口和局部坐标。
- `finger_width_m` 缺失或无效时，唯一生成处沿用当前 `boxGripper()` 行为，回退到 `finger_thickness_m`；Web 不另行实现回退规则。
- 不使用 `environment_collision_percent`，也不以百分比作为安全判断。
- 主要指标是有符号 `min_clearance_m` 和 `max_penetration_m`：正值表示最近环境点在盒体外的距离，负值表示点进入盒体；`max_penetration_m` 是最大穿透深度。
- 环境点必须排除候选对应的目标实例点。目标物体与手指的正常接触不算环境碰撞。
- 评估使用未抽样的工作区点云；35,000 点上限只用于 Web 显示。
- 网页显示原始 RGB 点云，当前选中候选的临近点用黄色、碰撞点用红色；碰撞候选的手指盒体和候选表状态也同步变色。
- `collision`、`warning`、`unknown` 默认都是行为树执行阻断状态；`grasp.html` 仍显示所有候选，便于离线审阅。碰撞配置关闭时新候选标为 `unknown` 并阻断计划，不能被解释成未碰撞。
- 没有有效实例分割、没有采集时 `T_base_camera`、局部环境点不足或必需夹爪尺寸不完整时，结果为 `unknown`，不能误报 `safe`。此时如果盒体尺寸仍可生成，网页照常显示同一规范盒体；几何本身不可用时不画盒体并显示 unknown。
- 结果沿用当前只读 `grasp_evaluation` 快照，不增加浏览器到 ROS 的写入接口。

## 碰撞模型契约

当前前端 `boxGripper()` 在候选接触中心坐标系中创建两个盒体。实现时由后端唯一 helper 生成规范模型，再让碰撞计算和 Web renderer 都消费该模型：

```text
opening = clamp(candidate.width_m + 2 * clearance_m,
                min_opening_m, max_opening_m)

left/right finger center x = +/- (opening / 2 + finger_thickness_m / 2)
finger box size = (finger_thickness_m, finger_width_m, finger_depth_m)
finger box center = (center_x, 0, -finger_depth_m / 2)
finger z range = [-finger_depth_m, 0]
finger y range = [-finger_width_m / 2, finger_width_m / 2]
```

规范模型包含 `model`、`frame: "grasp_contact"`、`opening_m` 和恰好两个 `boxes`；每个 box 包含 `name`、`center_m` 和 `size_m`，轴与 grasp contact frame 对齐。左、右只表示 x 方向的两侧，不引入 palm 或额外工具实体。`finger_boxes(candidate, gripper)` 是唯一执行上述公式的位置；SDF 直接消费返回的 box，不得再次从候选宽度推导尺寸。Web renderer 使用 `center_m` / `size_m` 直接创建盒体。

点云坐标和盒体坐标的转换约定如下：

```text
p_base   = T_base_camera * p_camera
p_grasp  = inverse(T_base_grasp) * p_base
```

`T_base_grasp` 必须由碰撞模块使用的有效 `T_camera_grasp` 和采集时 `T_base_camera` 得出；该有效 pose 已包含后端对齐及 `geometry_center_offset_m` 调整，与 Web 显示的接触中心 pose 相同。距离计算使用规范模型中每个 box 的局部轴对齐尺寸。盒体有符号距离使用标准 box SDF：盒体外为正，盒体表面为 0，盒体内为负。对所有环境点、所有手指和所有阶段取最小值：

```text
min_clearance_m = min(signed_distance(point, finger_box))
max_penetration_m = max(0, -min_clearance_m)
```

阶段间同一个原始点会被多次 sweep 查询，因此 `collision_points`、`near_points` 和 `local_environment_points` 都按原始点索引去重，不按采样姿态累计。`collision_clusters` 对满足穿透阈值的环境点，以 `cluster_voxel_m` 体素建立 26 邻域连通簇；一个簇必须包含至少 `minimum_collision_points` 个原始点才构成确认碰撞。孤立点仍参与 `min_clearance_m` / `max_penetration_m`，但不单独触发 `collision` 状态。

点数定义：

- `local_environment_points`：位于四阶段手指盒体扫掠包络外扩 `warning_clearance_m` 后范围内的去重环境点数。
- `collision_points`：signed distance 小于等于 `-collision_penetration_m` 的去重环境点数。
- `near_points`：`-collision_penetration_m < signed_distance < warning_clearance_m` 的去重环境点数，与 `collision_points` 分开计数，网页分别用黄、红色显示。
- `collision_clusters`：由 `collision_points` 形成且点数达到 `minimum_collision_points` 的簇数。

所有距离数值均保留米制单位。`min_clearance_m` 是扫掠包络周围所有可见环境点的 raw minimum，孤立噪声可能令它偏小；因此安全状态还需要局部点数证据，碰撞状态则需要超过穿透阈值且有邻域簇支持。显示时保留这个最小值，不用平均值掩盖局部风险。

## 评估阶段

每个候选至少检查以下四个阶段，阶段都使用同一套手指盒体：

1. `pregrasp`：沿候选接近轴反向移动 `profile.approach_m`。
2. `approach_sweep`：从 `pregrasp` 到 `grasp`，按 `sweep_step_m` 插值检查盒体扫掠路径。
3. `grasp`：候选接触中心 pose。
4. `lift`：从抓取 pose 沿所选基座的正 Z 移动 `profile.lift_m`。

这里的碰撞几何是 Web 中显示的接触中心盒体，因此阶段 pose 直接基于 `T_base_grasp` 构造，不把 `T_grasp_tcp` 另加到显示盒体上。规划仍继续按现有 `T_grasp_tcp` 生成真实 TCP waypoints；快照中必须明确碰撞模型是“可视化手指盒体”，不声称覆盖完整工具几何。

每个阶段返回：

```json
{
  "status": "safe|warning|collision|unknown",
  "min_clearance_m": 0.0042,
  "max_penetration_m": 0.0,
  "collision_points": 0,
  "near_points": 4,
  "local_environment_points": 86,
  "collision_clusters": 0
}
```

候选总结果取所有阶段中最差的指标，并设置 `worst_stage`。状态优先级严格按以下顺序判定：

- `collision`：至少一个穿透簇达到 `minimum_collision_points`，且簇内点的穿透深度达到 `collision_penetration_m`。
- `unknown`：缺少必要输入，或局部环境点少于 `minimum_local_points` 且没有确认碰撞簇。不可计算的距离/穿透指标用 JSON `null` 表示，不伪造为 0。
- `warning`：输入和局部点数足够、没有确认碰撞簇，但 `min_clearance_m < warning_clearance_m`，包括孤立穿透点。
- `safe`：点云证据满足最小局部点数，且最小间隙达到预警阈值。

确认的碰撞簇即使其他局部证据稀疏仍返回 `collision`；没有碰撞簇时，几何身份/输入缺失或局部点不足返回 `unknown`；只有点数充分时才用 clearance 区分 `warning` 和 `safe`。`unknown` 必须阻断计划，因为“没有足够证据”不能等同于无碰撞。

建议初始参数放在 `config/ros/grasp_bridge.yaml`：

```yaml
collision:
  enabled: true
  warning_clearance_m: 0.003
  collision_penetration_m: 0.002
  sweep_step_m: 0.005
  cluster_voxel_m: 0.003
  minimum_local_points: 20
  minimum_collision_points: 3
  blocking_statuses: [collision, warning, unknown]
```

这些是评估阈值，不改变 `gripper_specs.yaml` 中的实际夹爪尺寸。所有阈值必须经过有限数、正数和范围校验。

## 目标实例和环境点

后端已经在 `response.diagnostics.segmap_png` 中返回实例分割。碰撞模块需要解码它并验证尺寸等于注册深度尺寸：

- `target_points`：将 `candidate.object_id` 严格转换为与实例图标签兼容的整数 ID 后，`segmap == object_id` 的有效深度点；不按类别或候选 ID 猜测目标标签。
- `environment_points`：所有有效深度点中 `segmap != object_id` 的点，标签 0 也属于环境。
- 候选缺少 `object_id`、实例图不可解码、目标实例没有有效点或目标身份无法确认时返回 `unknown`。
- 环境点只取工作区裁切后的完整深度，不使用 snapshot 的显示抽样。
- 点云不可见区域仍然是传感器限制；结果描述为“基于当前可见点云”，不把 `safe` 解释为全场景证明。

## 文件和接口变更

- 不新增 ROS 节点、Action、service 或浏览器写入接口；继续沿用只读 `grasp_evaluation` snapshot。
- 在每个候选上增加可选 `collision` 对象，其中 `geometry` 是 Python 唯一生成并被 Web 原样渲染的规范双指盒体，其他字段是状态、距离/穿透、阶段结果和显示分类 mask。
- 保持 `schema_version: 1` 并以可选字段向后兼容旧 snapshot；新格式由后端和 Web validator 严格校验。

## 实施任务分解

### Task 1：冻结碰撞配置和盒体几何契约

文件：

- 修改 `config/ros/grasp_bridge.yaml`
- 修改 `src/grasp/grasp_bridge/grasp_bridge/profiles.py`
- 新增配置校验测试

步骤：

- [x] 增加 collision 参数及解释性注释，校验阈值、步长和点数范围。
- [x] 确认规范几何 helper 保留当前可视化的 `finger_width_m` 回退和开口夹紧行为；不改动现有夹爪尺寸值。
- [x] 对缺失或非有限的必需手指尺寸形成 readiness/error；可选 `finger_width_m` 缺失或无效时按现有显示约定回退到厚度。
- [x] 写测试锁定 opening clamp、两个手指中心、盒体尺寸和坐标轴方向，并验证碰撞模块和 snapshot 使用同一规范对象。

### Task 2：实现 ROS-free 点云碰撞模块

文件：

- 新增 `src/grasp/grasp_bridge/grasp_bridge/gripper_collision.py`
- 新增 `src/grasp/grasp_bridge/test/test_gripper_collision.py`

步骤：

- [x] 实现 ROS-free 的 `finger_boxes(candidate, gripper)`，一次生成带 `model/frame/opening_m/boxes` 的规范模型；碰撞检测器接收该模型，不再自行推导几何。
- [x] 实现点从 camera/base/grasp 坐标系的严格转换和基于规范 box `center_m/size_m` 的 SDF。
- [x] 实现 pregrasp、approach sweep、grasp、lift 四阶段。
- [x] 实现目标实例排除、环境点分类、局部点数检查、碰撞点体素簇统计。
- [x] 实现 `safe`、`warning`、`collision`、`unknown` 状态和最差阶段选择。
- [x] 用合成点云覆盖盒体外、表面、盒体内、扫掠路径、目标实例和标签 0 的情况。

### Task 3：接入 grasp_bridge 和规划阻断

文件：

- 修改 `src/grasp/grasp_bridge/grasp_bridge/node.py`
- 修改 `src/grasp/grasp_bridge/grasp_bridge/planner.py`
- 修改 `src/grasp/grasp_bridge/test/test_planning_core.py`
- 修改 `src/grasp/grasp_bridge/test/test_action_lifecycle.py`

步骤：

- [x] 在 backend adjustment 后统一 annotate 所有候选，确保 geometry offset 后的 pose 才用于防碰。
- [x] 以候选有效 `T_camera_grasp`（已包含后端 alignment）与冻结 `T_base_camera` 计算模型 pose，使碰撞模型和前端显示的 contact frame 一致。
- [x] evaluation-only 保留所有候选和碰撞结果。
- [x] 行为树路径把 blocking status 转换为结构化 rejection reason。
- [x] 确保 `ik_precheck: false` 也不能绕过碰撞 gate。
- [x] 验证取消、超时和无候选路径不会发布过期碰撞结果。

### Task 4：扩展 evaluation snapshot 和 Web 校验

文件：

- 修改 `src/grasp/grasp_bridge/grasp_bridge/evaluation.py`
- 修改 `src/driver/realman_web_control/web/src/grasp_state.ts`
- 修改 `src/grasp/grasp_bridge/test/test_evaluation_snapshot.py`

步骤：

- [x] 将 collision 结果放在各自 `candidate.collision` 下，保持候选之间隔离。
- [x] 将规范 `visual_finger_boxes/v1` 模型放在 `candidate.collision.geometry`，碰撞检测和 Web renderer 使用同一份盒体数组。
- [x] 对显示抽样保留的像素索引 `yy/xx` 同步取实例标签；用碰撞分类 helper 生成 bounded near/collision bitset，避免把目标实例点显示为环境障碍。规划指标仍来自完整点云。
- [x] bitset 长度必须与 `cloud.displayed_point_count` 一致。
- [x] 兼容旧 v1 snapshot：没有 collision 字段时显示“未执行碰撞评估”。
- [x] 拒绝非法状态、非有限数值、错误阶段名、非法盒体模型和超限 bitset；`unknown` 中不可计算的距离可为 `null`，其他数值必须有限。

### Task 5：更新两个页面的指标和颜色

文件：

- 修改 `src/driver/realman_web_control/web/src/grasp_view.ts`
- 修改 `src/driver/realman_web_control/web/src/grasp.ts`
- 修改 `src/driver/realman_web_control/web/src/grasp_plan.ts`
- 修改 `src/driver/realman_web_control/web/src/grasp.css`
- 修改 `website/tests/web-control/grasp.spec.ts`

步骤：

- [x] 将新快照的 `boxGripper()` API 改为只接收规范 geometry 和颜色；新快照不在 TypeScript 重算 opening 或 finger 尺寸。只有旧 snapshot 完全没有 collision 字段时才保留原有显示兼容路径；新 snapshot 缺少有效几何时不绘制盒体。
- [x] 点云按当前候选显示原始色、黄色 near 点和红色 collision 点。
- [x] 候选表显示 status、`min_clearance_m`、`max_penetration_m`、点数和 `worst_stage`。
- [x] 两个页面对同一 snapshot 使用相同颜色和数值语义。
- [x] 页面和测试中不新增 `environment_collision_percent`。
- [x] 覆盖 safe、warning、collision、unknown、旧快照和移动端布局。

### Task 6：文档、静态构建和回归验证

文件：

- 修改 `website/docs/development/grasping.md`
- 修改 `website/docs/development/realman-web-control.md`
- 更新 `src/driver/realman_web_control/realman_web_control/static/`

步骤：

- [x] 记录可视化手指盒体、距离/穿透指标、阶段语义和 unknown 限制。
- [x] 说明本模块不覆盖 palm、腕部、机械臂和点云遮挡区。
- [x] 执行 Python、WebSocket、Grasp bridge 和 Playwright 测试。
- [x] 执行 `npm run build:web-control`，检查生成静态文件已同步。
- [x] 执行 `cd website && npm run build`，检查开发手册和网站构建。
- [x] 若配置或启动链路有改动，再执行架构要求的 shell、Compose 和网站构建检查。

### 新增文件

- `src/grasp/grasp_bridge/grasp_bridge/gripper_collision.py`：夹爪盒体生成、box SDF、阶段扫掠、点分类、簇统计和候选结果。
- `src/grasp/grasp_bridge/test/test_gripper_collision.py`：纯 NumPy 的碰撞模块测试。

### 修改 Python

- `src/grasp/grasp_bridge/grasp_bridge/node.py`
  - 初始化和校验 collision 配置。
  - 在 backend geometry offset 后、可视化和规划前调用碰撞模块。
  - 评估模式发布所有候选及碰撞结果。
  - 行为树模式只把安全结果交给后续规划；保留被拒绝候选和结构化原因。
- `src/grasp/grasp_bridge/grasp_bridge/planner.py`
  - 在公共 `_candidate_geometry()` 路径中增加 collision gate；`calculate_plan()` 和 `choose_plan()` 共用该路径，不能被 `ik_precheck` 开关绕过。
  - `collision.blocking_statuses` 中的候选产生明确的 `collision: ...` 或 `collision result unavailable` 拒绝原因。
- `src/grasp/grasp_bridge/grasp_bridge/evaluation.py`
  - 将候选碰撞指标、规范几何和阶段结果序列化到各候选。
  - 在已有显示点抽样后，用保留的 `yy/xx` 取对应实例标签，生成与 `cloud.xyz_m` 一一对应的候选碰撞/预警 bitset。
  - 快照不增加碰撞百分比字段。
  - 保留 `collision` 指标和 `worst_stage`，并保留 bounded 的显示 mask。
- `src/grasp/grasp_bridge/grasp_bridge/profiles.py`
  - 将可选 `finger_width_m` 的回退交给唯一几何 helper；缺失、非有限或非正值时沿用当前可视化行为回退到厚度，必需的 depth/thickness 仍须有效。
  - readiness 中区分结构尺寸缺失和 collision 配置关闭。

### 修改配置

- `config/ros/grasp_bridge.yaml`：增加 collision 阈值、扫掠步长、局部点数、阻断状态和启用开关，并写解释性注释。
- `config/grasp/gripper_specs.yaml`：沿用现有两个可视化手指盒体尺寸，不新增 palm 字段；只有确认配置缺值或错误时才修改对应尺寸。

### 修改 Web

- `src/driver/realman_web_control/web/src/grasp_state.ts`
  - 校验可选 collision 对象、状态、有限数、阶段结果、规范几何和 bitset 大小；只允许 `unknown` 的不可用距离为 `null`。
  - 兼容没有 collision 字段的旧 v1 snapshot，并显示“未执行碰撞评估”。
- `src/driver/realman_web_control/web/src/grasp_view.ts`
  - 将新快照的 `boxGripper(geometry, color)` 改为只消费规范 `boxes` 的 `center_m` / `size_m`；其接口不接收 `candidate.width_m` 或 `gripper`，因此不能自行计算另一套盒体。只有旧 snapshot 完全没有 collision 字段时才保留旧显示兼容路径；新 snapshot 的 geometry 不可用时不绘制盒体并显示原因。
  - `pointCloud()` 接受当前候选，按 bitset 将 near 点改为黄色、collision 点改为红色。
  - 候选盒体在 `warning` / `collision` / `unknown` 时使用对应状态颜色；安全候选继续使用 rank 色。
  - `data-collision-status`、`data-collision-candidate` 写入 canvas，供 Playwright 验证。
- `src/driver/realman_web_control/web/src/grasp.ts`
  - 候选表增加状态、最小间隙、最大穿透、碰撞点数、最差阶段。
  - 选中候选时显示“基于可见点云”的碰撞摘要和阈值说明。
  - 颜色图例增加安全、临近、碰撞、未知。
- `src/driver/realman_web_control/web/src/grasp_plan.ts`
  - 规划候选表展示同样的碰撞字段。
  - 选中的行为树候选如果是 collision、warning 或 unknown，显示阻断原因；不允许页面把它显示为通过。
- `src/driver/realman_web_control/web/src/grasp.css`
  - 增加紧凑的 collision 状态样式、数字指标布局和移动端折行规则。

### 文档和构建产物

- `website/docs/development/grasping.md`：说明可视化手指盒体、阶段检查、距离/穿透指标、unknown 语义和当前不覆盖的 palm/腕部/遮挡限制。
- `website/docs/development/realman-web-control.md`：补充两个页面的候选碰撞列和点云颜色含义。
- `src/driver/realman_web_control/realman_web_control/static/`：通过 `npm run build:web-control` 更新提交的静态页面产物。

## 快照数据契约

为保持现有离线 review JSON 和旧快照可读，保留当前 `schema_version: 1`；collision 字段采用向后兼容的可选扩展。新快照对每个候选都增加 collision 对象；评估未运行或必要数据缺失时使用 `unknown` 并附带可读原因：

`collision` 位于对应的 `candidates[]` 元素内，而不是快照顶层；这样不同候选的手指姿态、最差阶段和显示 mask 不会混淆。

```json
"candidates": [{
  "candidate_id": "1:0",
  "collision": {
    "geometry": {
      "model": "visual_finger_boxes/v1",
      "frame": "grasp_contact",
      "opening_m": 0.04,
      "boxes": [
        {"name": "left", "center_m": [0.025, 0.0, -0.025], "size_m": [0.01, 0.02, 0.05]},
        {"name": "right", "center_m": [-0.025, 0.0, -0.025], "size_m": [0.01, 0.02, 0.05]}
      ]
    },
    "status": "warning",
    "blocking": true,
    "min_clearance_m": 0.0011,
    "max_penetration_m": 0.0,
    "collision_points": 0,
    "near_points": 8,
    "local_environment_points": 114,
    "collision_clusters": 0,
    "worst_stage": "approach_sweep",
    "stages": {
      "pregrasp": {"status": "safe", "min_clearance_m": 0.006, "max_penetration_m": 0.0},
      "approach_sweep": {"status": "warning", "min_clearance_m": 0.0011, "max_penetration_m": 0.0},
      "grasp": {"status": "safe", "min_clearance_m": 0.004, "max_penetration_m": 0.0},
      "lift": {"status": "safe", "min_clearance_m": 0.004, "max_penetration_m": 0.0}
    },
    "display": {
      "near_mask_b64": "...",
      "collision_mask_b64": "..."
    }
  }
}]
```

实际实现可以省略各阶段中重复的计数，但 `status`、`min_clearance_m`、`max_penetration_m`、`worst_stage` 和每个候选的显示 mask 必须保留。bitset 长度必须等于 `cloud.displayed_point_count`，并且经过 WebSocket 消息大小限制。

`geometry.boxes` 是碰撞计算和新页面渲染共同使用的唯一几何来源。若状态为 `unknown` 且几何仍有效，保留 geometry 并照常画盒体；若没有可计算的距离，距离字段为 `null`，没有可靠分类时不发送 near/collision mask。旧快照完全没有 `collision` 字段时，页面保留旧渲染行为并标明未执行碰撞评估。

## 测试计划

### Python 单元测试

在 `test_gripper_collision.py` 覆盖：

- Python 测试锁定规范盒体的开口、中心和尺寸；Web 测试验证 renderer 原样消费同一份 `collision.geometry.boxes`，不再实现第二套几何公式。
- 盒体外点返回正确正距离，盒体内点返回正确负距离和穿透深度。
- 左手指、右手指分别命中时结果正确。
- 候选接近扫掠阶段的单个障碍能被发现，最终 grasp 阶段没有障碍不能掩盖 sweep 碰撞。
- lift 阶段沿基座 +Z 的障碍能被发现。
- 目标实例点被排除，其他实例点和标签 0 点计入环境。
- 单个孤立浅穿透点不会绕过 `minimum_collision_points` / cluster 规则触发 `collision`，但应进入 `warning` 并影响 raw clearance；达到局部点数不足条件时无确认碰撞簇应为 `unknown`。
- 多个空间相邻且超过穿透阈值的点组成确认簇；相同数量的分散孤立点不应合成一个碰撞簇。
- 缺失实例图、错误尺寸、缺失 `T_base_camera`、局部点数不足返回 unknown。
- 不使用显示抽样；显示 mask 与抽样后的点索引准确对应。

### Grasp bridge / planner 测试

- `test_planning_core.py` 增加 safe、warning、collision、unknown 候选，验证 blocking statuses 不会被 `ik_precheck` 开关绕过。
- `test_evaluation_snapshot.py` 验证 collision 指标、阶段、显示 mask 和旧快照兼容。
- 验证碰撞计算使用的规范模型经 JSON 序列化后，其两个 box 的 `center_m` / `size_m` 完整保留，pose 与有效 `T_camera_grasp` 一致。
- Action lifecycle 测试验证 evaluation-only 会发布碰撞结果，行为树路径会保留结构化拒绝原因。
- 配置/profile 测试验证新阈值和 `finger_width_m` 校验。

### Web / Playwright 测试

- `grasp.html` 显示最小间隙、最大穿透、最差阶段和状态；不存在 environment collision percent 文案或字段。
- `grasp_plan.html` 与 `grasp.html` 对同一 snapshot 显示相同 collision 状态。
- 选中候选后 canvas 写入正确 collision data attributes，点云 canvas 非空。
- 选中 safe、warning、collision、unknown 候选时，候选盒体和表格状态颜色正确。
- 使用后端导出的规范模型 fixture，验证 Three.js 中两个 Mesh 的尺寸、中心和有效 pose 与 fixture 一致；盒体八个角经坐标变换后与后端一致，采用明确的浮点容差。`boxGripper()` 只接受 geometry，测试不得从候选宽度或 profile 尺寸二次推导。
- 旧 snapshot 没有 collision 字段时页面仍能审阅候选，并显示未执行碰撞评估。
- 桌面和移动端无水平溢出。

## 实施顺序

1. 先增加失败的纯 Python 碰撞测试和配置校验测试。
2. 实现盒体 helper、box SDF、阶段 sweep、目标/环境点分类和状态机。
3. 接入 `grasp_bridge`，在 inference 后统一 annotate candidate；补齐 planner gate。
4. 将碰撞指标和显示 mask 接入 evaluation snapshot 与 Web validator。
5. 更新两个页面的表格、颜色图例、点云着色和状态说明。
6. 更新文档，构建 Web 静态产物。
7. 运行完整验证并检查消息大小、旧快照兼容和 behavior-tree 规划拒绝原因。

## 验收标准

- 对新快照，碰撞检测与可视化直接使用同一份 `collision.geometry.boxes`；开口、尺寸、盒体中心和有效 contact pose 均一致，不存在 Python/TypeScript 各自计算的两套模型。
- 一个落入手指盒体的非目标点会让候选至少成为 `collision` 或在配置的噪声阈值下成为 `warning`，不会显示为 `safe`。
- 同一目标物体点落入手指盒体不会被误判为环境碰撞。
- 只在 approach sweep 或 lift 阶段发生的碰撞能够被指出，并显示 `worst_stage`。
- 页面显示 `min_clearance_m`、`max_penetration_m`、碰撞点数和状态，不出现 `environment_collision_percent`。
- 两个页面使用同一份 snapshot 时，指标、状态和点云颜色一致。
- collision、warning、unknown 候选不会进入可执行行为树计划；评估页面仍可查看并导出这些候选。
- 不完整的实例分割、坐标或点云数据只产生 `unknown`，不会误报安全。
- 现有 grasp、gripper、WebSocket、静态构建和 Playwright 测试保持通过。

## 验证命令

```bash
PYTHONPATH=src/grasp:src/grasp/grasp_bridge \
  python3 -m pytest src/grasp/grasp_bridge/test src/grasp/tests -q

PYTHONPATH=src/driver/realman_web_control \
  python3 -m pytest src/driver/realman_web_control/test -q

cd website
npm run build:web-control
npm run test:web-control -- grasp.spec.ts
```

若完成了启动或配置接入，再补充架构要求的 `bash -n rm65`、`zsh -n functions.zsh`、`docker compose config` 和网站构建；碰撞模块本身不需要连接真实夹爪或运行运动命令即可完成单元和离线验收。
