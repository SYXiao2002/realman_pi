import "./theme.css";
import "./pose6d.css";
import { VacuumIndicators } from "./vacuum_status";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";

type Message = Record<string, any>;
const app = document.querySelector<HTMLElement>("#app")!;
app.innerHTML = `<div class="pose-page"><header class="pose-header"><h1>6D 位姿与吸盘</h1><span id="connection">未连接</span><a href="/6d_pose_history/">6D 历史</a><a href="/">控制台</a></header>
<main class="pose-main"><div class="pose-toolbar"><label>采集分割对象<select id="capture-object"><option value="eu_plug">欧标插头</option></select></label><button id="capture" class="button primary">采集并自动分割</button><a id="download" class="button secondary" hidden>下载 RGB</a><label>手工 Mask<input id="mask" type="file" accept="image/png" disabled></label><label>解算 OBJ<select id="object"><option value="eu_plug">欧规插头</option></select></label><button id="infer" class="button primary" disabled>解算所选实例</button></div>
<a id="history-result" href="/6d_pose_history/" target="_blank" rel="noopener" hidden>查看本次历史</a><div id="state" class="pose-status" role="status">等待采集</div><section class="pose-detections"><div class="pose-detection-head"><h2>检测与分割</h2><p id="vision-timings">等待采集</p></div><div id="instance-list" class="pose-instance-list" aria-live="polite"></div></section><div class="pose-workspace"><div><div class="pose-images"><figure><img id="rgb" alt="采集 RGB"><figcaption>RGB</figcaption></figure><figure><img id="depth" alt="对齐深度"><figcaption>Depth</figcaption></figure><figure class="pose-instance-image"><img id="instances-overview" alt="全部实例 ID 与检测框总览"><figcaption>实例总览 · #编号对应上方选择列表</figcaption></figure><figure class="pose-instance-image"><img id="overlay" alt="所选 SAM 实例、手工 Mask 或解算结果预览"><figcaption id="overlay-caption">所选实例 / 解算结果</figcaption></figure><figure id="score-preview" class="pose-score-image" hidden><figcaption>vis_score · 最高分候选 · RGB / Depth 对比</figcaption><a id="score-preview-link" target="_blank" rel="noopener" title="打开完整 vis_score 图"><img id="score-preview-image" alt="vis_score 最高分候选，含候选 ID、分数与 RGB / Depth 对比" hidden></a><p id="score-preview-status" role="status"></p></figure></div><canvas id="viewer" class="pose-viewer" aria-label="RGB-D 点云、模型、选中吸取方式、坐标系、准备位与配置工作区"></canvas><div class="pose-legend"><label><input id="show-tangent" type="checkbox" checked>切向参考 t（青）</label><label><input id="show-camera" type="checkbox" checked>相机坐标系</label><label><input id="show-tool" type="checkbox" checked>当前 TCP</label><label><input id="show-prepare" type="checkbox" checked><span id="prepare-legend">准备位 +40 mm（紫）</span></label><label><input id="show-workspace" type="checkbox" checked>配置工作区</label><button id="fit-view" class="button secondary">显示全部</button><small>坐标轴 X 红 / Y 绿 / Z 蓝；橙色为朝外法向 n，接触系 +Z = −n。</small><small id="frames-state">采集后显示当前 TCP；相机坐标系为冻结采集帧。</small><small id="workspace-state">等待配置工作区。</small></div></div>
<aside class="pose-side"><section><h2>观测</h2><p id="observation">尚未采集</p></section><section><h2>几何验证</h2><p id="warning">尚未解算</p><pre id="metrics" class="pose-metrics"></pre></section><section><h2>吸盘与运动</h2><div id="vacuum-status" aria-label="吸盘状态"></div><div class="tool-power-control"><label><input id="tool-power" type="checkbox" disabled><span>右臂工具端 24V</span></label><span id="tool-power-state" role="status">等待电源状态</span></div><div class="pose-controls"><label class="wide">控制器 TCP<select id="controller-tool"><option value="tcpsuck">吸盘 · tcpsuck</option><option value="tcpgrip">夹爪 · tcpgrip</option></select></label><button id="apply-tool" class="button secondary wide">切换 TCP</button><p id="tool-state" class="wide">等待控制器工具状态</p><label class="wide">吸取方式<select id="grasp" disabled></select></label><div class="tcp-target-panel wide"><h2>吸取 TCP 目标</h2><pre id="tcp-target" class="pose-metrics">选择吸取方式后显示</pre><small id="tcp-target-note">显示所选吸取方式的目标 TCP；吸取位与准备位姿态以 BASE 坐标系表示。</small></div><label>当前 x / mm<input id="distance" type="text" value="—" readonly></label><label>单次步长 / mm<input id="step" type="number" min="0.1" max="500" step="0.1" value="1"></label><label class="wide review"><input id="review" type="checkbox" disabled><span>已复核位姿、告警、完整运动路径与吸盘方向；路径未做障碍物碰撞检测</span></label><button id="prepare" class="button secondary wide" disabled>移动到 +40 mm 准备位</button><div class="step-buttons wide"><button id="minus" class="button secondary" title="靠近物体" aria-label="靠近物体" disabled>−</button><button id="plus" class="button secondary" title="远离物体" aria-label="远离物体" disabled>+</button></div><small id="auto-vacuum" class="wide">提升后读取真空开关：已吸附则 MOVEJ 回初始姿态，确认到位后 MOVEJ 放置并关闭吸盘，再 MOVEJ 回初始姿态；未吸附则在提升位直接关闭。</small><button id="auto-suction" class="button primary wide" disabled>自动接近并提升</button><small id="auto-path" class="wide">自动路径：40 → 5 → 0 mm，BASE +Z 提升 50 mm</small><button id="stop-motion" class="button secondary wide" disabled>停止 / 关闭运动会话</button><p id="motion" class="wide">运动未就绪</p><small id="cup-info" class="wide">吸盘尺寸由配置提供；细线圈表示人工标注接触区域。</small></div></section></aside></div></main></div>`;
const el = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const vacuumIndicators = new VacuumIndicators(el("vacuum-status"));
const canvas = el<HTMLCanvasElement>("viewer"), renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const scene = new THREE.Scene(); scene.background = new THREE.Color("#e0e6e3");
const camera = new THREE.PerspectiveCamera(48, 1, .001, 10); camera.position.set(.2, -.25, -.35); camera.up.set(0, -1, 0);
const controls = new OrbitControls(camera, canvas); controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xffffff, 0x586a62, 2));
const light = new THREE.DirectionalLight(0xffffff, 2); light.position.set(-1, -2, -1); scene.add(light);
const content = new THREE.Group(); scene.add(content);
function resize() { const rect = canvas.getBoundingClientRect(); renderer.setSize(rect.width, rect.height, false); camera.aspect = rect.width / rect.height; camera.updateProjectionMatrix(); }
new ResizeObserver(resize).observe(canvas); resize();
function tick() { controls.update(); renderer.render(scene, camera); requestAnimationFrame(tick); } tick();
function matrix(value: number[][]) { return new THREE.Matrix4().set(...value.flat() as [number, number, number, number, number, number, number, number, number, number, number, number, number, number, number, number]); }
function clearScene(root: THREE.Object3D = content) { while (root.children.length) { const item = root.children[0]; root.remove(item); item.traverse((o: any) => { o.geometry?.dispose(); const materials = Array.isArray(o.material) ? o.material : [o.material]; materials.forEach((m: any) => { m?.map?.dispose(); m?.dispose(); }); }); } }
function label(text: string, color = "#182625") {
  const bitmap = document.createElement("canvas"), ctx = bitmap.getContext("2d")!;
  ctx.font = "32px sans-serif"; bitmap.width = Math.ceil(ctx.measureText(text).width) + 24; bitmap.height = 48;
  ctx.fillStyle = "rgba(255,255,255,0.9)"; ctx.fillRect(0, 0, bitmap.width, bitmap.height);
  ctx.font = "32px sans-serif"; ctx.fillStyle = color; ctx.textAlign = "center"; ctx.fillText(text, bitmap.width / 2, 36);
  const texture = new THREE.CanvasTexture(bitmap); texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: false, depthWrite: false, sizeAttenuation: false, toneMapped: false }));
  sprite.scale.set(.032 * bitmap.width / bitmap.height, .032, 1); sprite.position.set(0, -.012, 0); sprite.renderOrder = 10; return sprite;
}
function frame(text: string, size: number) { const group = new THREE.Group(); group.add(new THREE.AxesHelper(size), label(text)); return group; }
const cameraAxes = frame("相机 · 采集帧", .055), toolAxes = frame("当前 TCP", .045), liveCameraAxes = frame("相机 · 当前", .035);
liveCameraAxes.children[1].position.y = .015;
scene.add(cameraAxes, toolAxes, liveCameraAxes); toolAxes.visible = liveCameraAxes.visible = false;
let frozenBaseCamera: THREE.Matrix4 | null = null, latestFrames: Message | null = null, framesReceivedAt = 0;
const workspace = new THREE.Group(); workspace.visible = false; workspace.matrixAutoUpdate = false; scene.add(workspace);
let workspaceBounds: number[][] | null = null, workspaceFrame = "", workspaceKey = "";
function receiveWorkspace(msg: Message) {
  if (!("workspace_m" in msg)) return;
  const bounds = msg.workspace_m;
  const valid = Array.isArray(bounds) && bounds.length === 2 && bounds.every(row => Array.isArray(row) && row.length === 3 && row.every(Number.isFinite))
    && bounds[0].every((value: number, i: number) => value < bounds[1][i]) && typeof msg.base_frame === "string" && !!msg.base_frame;
  const key = JSON.stringify([valid ? bounds : null, msg.base_frame]);
  if (key === workspaceKey) return;
  workspaceKey = key; clearScene(workspace);
  workspaceBounds = valid ? bounds : null; workspaceFrame = valid ? msg.base_frame : "";
  if (workspaceBounds) {
    const lo = new THREE.Vector3(...workspaceBounds[0] as [number, number, number]), hi = new THREE.Vector3(...workspaceBounds[1] as [number, number, number]);
    const box = new THREE.BoxGeometry(...hi.clone().sub(lo).toArray() as [number, number, number]);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(box), new THREE.LineBasicMaterial({ color: 0x2f7585, transparent: true, opacity: .85, depthTest: false }));
    box.dispose(); edges.position.copy(lo.clone().add(hi).multiplyScalar(.5)); workspace.add(edges);
    const text = label("配置工作区"); text.position.copy(hi); workspace.add(text);
  }
}
function updateWorkspace() {
  workspace.visible = !!workspaceBounds && !!frozenBaseCamera && el<HTMLInputElement>("show-workspace").checked;
  // The configured axis-aligned box is in BASE; keep it fixed in the same
  // captured camera frame as the point cloud, rather than the moving camera.
  if (workspaceBounds && frozenBaseCamera) workspace.matrix.copy(frozenBaseCamera.clone().invert());
  if (!workspaceBounds) el("workspace-state").textContent = "配置工作区未提供。";
  else if (!frozenBaseCamera) el("workspace-state").textContent = "配置工作区：采集并获得手眼变换后显示。";
  else {
    const axes = ["X", "Y", "Z"].map((axis, i) => `${axis} ${Number((workspaceBounds![0][i] * 1000).toFixed(1))}～${Number((workspaceBounds![1][i] * 1000).toFixed(1))}`);
    el("workspace-state").textContent = `${workspaceFrame} · ${axes.join(" / ")} mm`;
  }
}
function updateFrames() {
  updateWorkspace();
  cameraAxes.visible = el<HTMLInputElement>("show-camera").checked;
  const live = latestFrames?.available && performance.now() - framesReceivedAt < 1500 && frozenBaseCamera;
  toolAxes.visible = !!live && el<HTMLInputElement>("show-tool").checked;
  liveCameraAxes.visible = !!live && !!latestFrames?.T_base_camera_live && cameraAxes.visible;
  if (live) {
    const cameraBase = frozenBaseCamera!.clone().invert();
    toolAxes.matrixAutoUpdate = false; toolAxes.matrix.copy(cameraBase.clone().multiply(matrix(latestFrames!.T_base_tcp)));
    if (latestFrames!.T_base_camera_live) { liveCameraAxes.matrixAutoUpdate = false; liveCameraAxes.matrix.copy(cameraBase.multiply(matrix(latestFrames!.T_base_camera_live))); }
    el("frames-state").textContent = `当前 TCP · ${latestFrames!.tool_name} · 实时更新；原点为采集相机，所有数据对齐到同一冻结帧。`;
  } else el("frames-state").textContent = !frozenBaseCamera ? "采集并获得手眼变换后显示当前 TCP。" : `当前 TCP 不可用：${latestFrames?.message || "等待新鲜反馈"}`;
}
window.setInterval(updateFrames, 500);
let grasps: Message[] = [], groups: THREE.Group[] = [], preparationGroups: THREE.Group[] = [], tangentArrows: THREE.Object3D[] = [];
function showLayers() {
  const selected = el<HTMLSelectElement>("grasp").value || grasps[0]?.grasp_id;
  groups.forEach((group, i) => { group.visible = grasps[i].grasp_id === selected; });
  tangentArrows.forEach(arrow => { arrow.visible = el<HTMLInputElement>("show-tangent").checked; });
  preparationGroups.forEach((group, i) => { group.visible = el<HTMLInputElement>("show-prepare").checked && grasps[i].grasp_id === selected; });
  updateFrames();
}
function fitView() {
  scene.updateMatrixWorld(true);
  const box = new THREE.Box3();
  content.traverseVisible((item: any) => { if (item.isMesh) box.expandByObject(item); });
  if (cameraAxes.visible) box.expandByPoint(new THREE.Vector3());
  if (toolAxes.visible) box.expandByPoint(new THREE.Vector3().setFromMatrixPosition(toolAxes.matrixWorld));
  if (workspace.visible) box.expandByObject(workspace);
  if (box.isEmpty()) box.setFromCenterAndSize(new THREE.Vector3(), new THREE.Vector3(.15, .15, .15));
  const center = box.getCenter(new THREE.Vector3()), radius = Math.max(.07, box.getSize(new THREE.Vector3()).length() / 2);
  const fov = Math.min(THREE.MathUtils.degToRad(camera.fov), 2 * Math.atan(Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) * camera.aspect));
  const distance = radius / Math.sin(fov / 2) * 1.2;
  controls.target.copy(center); camera.position.copy(center).add(new THREE.Vector3(.5, -.6, -1).normalize().multiplyScalar(distance)); controls.update();
}
function draw(result: Message) {
  clearScene(); groups = []; preparationGroups = []; tangentArrows = []; grasps = result.grasps;
  const positions = new Float32Array(result.cloud.xyz_m.flat()), colors = new Float32Array(result.cloud.rgb.flat().map((v: number) => v / 255));
  const geo = new THREE.BufferGeometry(); geo.setAttribute("position", new THREE.BufferAttribute(positions, 3)); geo.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  content.add(new THREE.Points(geo, new THREE.PointsMaterial({ size: .0018, vertexColors: true })));
  const object = new OBJLoader().parse(result.mesh_obj); object.traverse((item: any) => { if (item.isMesh) item.material = new THREE.MeshStandardMaterial({ color: 0x77ad9c, transparent: true, opacity: .7, side: THREE.DoubleSide }); });
  object.matrixAutoUpdate = false; object.matrix.copy(matrix(result.T_camera_object)); content.add(object);
  const axes = new THREE.AxesHelper(.035); axes.matrixAutoUpdate = false; axes.matrix.copy(matrix(result.T_camera_object)); content.add(axes);
  const cupRadius = Number(result.suction?.cup_diameter_m ?? .01) / 2;
  grasps.forEach((grasp, index) => {
    const group = new THREE.Group(); group.matrixAutoUpdate = false; group.matrix.copy(matrix(grasp.T_camera_contact));
    const material = new THREE.MeshBasicMaterial({ color: index === 0 ? 0xe96d42 : 0x2f7585, transparent: true, opacity: .75, side: THREE.DoubleSide });
    group.add(new THREE.Mesh(new THREE.CircleGeometry(cupRadius, 32), new THREE.MeshBasicMaterial({ color: 0xf4b942, transparent: true, opacity: .58, side: THREE.DoubleSide })));
    const ring = new THREE.Mesh(new THREE.RingGeometry(grasp.radius_m * .98, grasp.radius_m, 32), material); group.add(ring); group.userData.ring = ring;
    group.add(new THREE.ArrowHelper(new THREE.Vector3(0, 0, -1), new THREE.Vector3(), .025, 0xe96d42, .006, .003));
    group.add(new THREE.AxesHelper(.018), label(grasp.grasp_id));
    // The annotation's reference can have a normal component; display it as
    // supplied, separately from the projected contact +X used for motion.
    const tangent = grasp.tangent_camera ? new THREE.Vector3(...grasp.tangent_camera as [number, number, number]).transformDirection(group.matrix.clone().invert()) : new THREE.Vector3(1, 0, 0);
    const arrow = new THREE.Group(); arrow.add(new THREE.ArrowHelper(tangent, new THREE.Vector3(), .03, 0x008ba3, .006, .003));
    const text = label("切向参考 t", "#008ba3"); text.position.copy(tangent.clone().multiplyScalar(.04)); arrow.add(text); group.add(arrow); tangentArrows.push(arrow);
    content.add(group); groups.push(group);
    const prepared = new THREE.Group();
    const preparePose = grasp.T_camera_prepare_contact ? matrix(grasp.T_camera_prepare_contact) : group.matrix.clone().multiply(new THREE.Matrix4().makeTranslation(0, 0, -prepareDistanceMm / 1000));
    prepared.matrixAutoUpdate = false; prepared.matrix.copy(preparePose);
    prepared.add(new THREE.Mesh(new THREE.CircleGeometry(cupRadius, 32), new THREE.MeshBasicMaterial({ color: 0x8656b0, transparent: true, opacity: .55, side: THREE.DoubleSide })));
    prepared.add(new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), new THREE.Vector3(), .04, 0x8656b0, .005, .002));
    const readyAxes = frame(`准备位吸盘 · +${prepareDistanceMm.toFixed(0)} mm`, .025); prepared.add(readyAxes);
    if (grasp.T_camera_prepare_tcp) {
      const tcp = frame("准备位 TCP", .02); tcp.matrixAutoUpdate = false; tcp.matrix.copy(preparePose.clone().invert().multiply(matrix(grasp.T_camera_prepare_tcp)));
      // Avoid two overlapping frames when the controller TCP is the contact.
      if (!tcp.matrix.equals(new THREE.Matrix4())) prepared.add(tcp);
    }
    content.add(prepared); preparationGroups.push(prepared);
  });
  showLayers(); fitView();
}
function highlight() { const id = el<HTMLSelectElement>("grasp").value; el<HTMLInputElement>("review").checked = false; groups.forEach((g, i) => g.userData.ring.material.color.set(grasps[i].grasp_id === id ? 0xe96d42 : 0x2f7585)); showLayers(); updateTcpTarget(); update(); }
function graspOptions(candidates: Message[]) {
  return candidates.map((grasp, index) => ({ grasp, index })).sort((a, b) => {
    const eligibility = Number(b.grasp.workspace_valid === true) - Number(a.grasp.workspace_valid === true);
    if (eligibility) return eligibility;
    const angleA = Number.isFinite(a.grasp.vertical_down_angle_deg) ? a.grasp.vertical_down_angle_deg : Infinity;
    const angleB = Number.isFinite(b.grasp.vertical_down_angle_deg) ? b.grasp.vertical_down_angle_deg : Infinity;
    return angleA - angleB || a.index - b.index;
  }).map(({ grasp }) => {
    const angle = Number.isFinite(grasp.vertical_down_angle_deg)
      ? `向下偏角 ${Number(grasp.vertical_down_angle_deg).toFixed(1)}°` : "角度未知";
    const reason = grasp.workspace_valid === true ? "" : ` · 警告：${grasp.workspace_reason || "workspace 未通过或无法判定"}`;
    const option = new Option(`${grasp.grasp_id} · ${grasp.status} · ${angle}${reason}`, grasp.grasp_id);
    option.disabled = grasp.workspace_valid !== true;
    option.title = grasp.workspace_valid === true ? `${angle} · workspace 合格`
      : grasp.workspace_reason || "workspace 未通过或无法判定";
    return option;
  });
}
let socket: WebSocket | undefined, observation = "", resultId = "", pending = "", mask = "", maskRevision = 0, requestTimer = 0;
let instances: Message[] = [], selectedInstance = "";
let modelChoicesKey = "", maskPreview = "", maskPreviewCaption = "所选实例 / 解算结果";
let motionReady = false, motionState = "review", motionSession = "", motionPending = "", stopPending = "";
let autoSuctionReady = false, prepareDistanceMm = 40, secondPrepareDistanceMm = 5, liftHeightMm = 50;
let contactOffsetMm = 0, poseBaseFrame = "BASE";
let placeJointDegrees: number[] = [];
let toolPowerAvailable = false, toolPowerVoltage: number | null = null;
let toolPowerSetPending = "", toolPowerSetRequested: boolean | null = null;
let toolPowerStatusPending = "", toolPowerError = "";
const state = (text: string, error = false) => { el("state").textContent = text; el("state").dataset.error = String(error); };
function formatPose(pose: THREE.Matrix4, frameName: string) {
  const position = new THREE.Vector3().setFromMatrixPosition(pose).multiplyScalar(1000);
  const rpy = new THREE.Euler().setFromRotationMatrix(pose, "ZYX");
  const degrees = [rpy.x, rpy.y, rpy.z].map(value => THREE.MathUtils.radToDeg(value));
  return `XYZ (${frameName})  ${position.toArray().map(value => value.toFixed(1)).join(", ")} mm\nRPY (${frameName})  ${degrees.map(value => value.toFixed(1)).join(", ")}°`;
}
function updateTcpTarget() {
  const selected = grasps.find(grasp => grasp.grasp_id === el<HTMLSelectElement>("grasp").value);
  const output = el("tcp-target");
  const note = el("tcp-target-note");
  if (!selected) {
    output.textContent = "解算完成并选择吸取方式后显示目标 TCP。";
    note.textContent = "吸取位已计入配置的接触偏移；准备位按配置的预备距离计算。";
    return;
  }
  if (!Array.isArray(selected.T_camera_suction_tcp)) {
    output.textContent = "当前吸取方式没有有效的 T_contact_tcp 配置。";
    note.textContent = "请检查 pose6d_bridge.yaml 中的运动工具与 T_contact_tcp 配置。";
    return;
  }
  const frameName = frozenBaseCamera ? poseBaseFrame : "相机";
  const toDisplayFrame = (value: number[][]) => {
    const pose = matrix(value);
    return frozenBaseCamera ? frozenBaseCamera.clone().multiply(pose) : pose;
  };
  const suctionPose = toDisplayFrame(selected.T_camera_suction_tcp);
  const preparePose = Array.isArray(selected.T_camera_prepare_tcp)
    ? toDisplayFrame(selected.T_camera_prepare_tcp) : null;
  const lines = [
    `${selected.grasp_id} · ${selected.tool_name || "tcpsuck"}`,
    `吸取位（接触偏移 −${contactOffsetMm.toFixed(1)} mm）`,
    formatPose(suctionPose, frameName),
  ];
  if (preparePose) lines.push(`准备位（+${prepareDistanceMm.toFixed(1)} mm）`, formatPose(preparePose, frameName));
  output.textContent = lines.join("\n");
  note.textContent = frozenBaseCamera
    ? `按采集时的 T_base_camera 换算到 ${frameName}；目标取自当前吸取方式的 ${selected.tool_name || "tcpsuck"} 运动路径。`
    : "缺少采集时的手眼变换，当前显示相机坐标系下的目标 TCP。";
}
function clearScorePreview() {
  el("score-preview").hidden = true;
  const image = el<HTMLImageElement>("score-preview-image");
  image.onload = null; image.onerror = null; image.hidden = true; image.removeAttribute("src");
  el<HTMLAnchorElement>("score-preview-link").removeAttribute("href");
  el("score-preview-status").textContent = "";
}
function showScorePreview(message: Message) {
  clearScorePreview();
  if (typeof message.history_id !== "string" || !/^\d+$/.test(message.history_id)) return;
  el("score-preview").hidden = false;
  const status = el("score-preview-status");
  if (message.archive_status !== "complete") {
    status.textContent = "本次历史归档不可用，无法加载评分候选图"; return;
  }
  const root = `/6d_pose_history/api/records/${message.history_id}`;
  const image = el<HTMLImageElement>("score-preview-image");
  el<HTMLAnchorElement>("score-preview-link").href = `${root}/files/vis_score.png`;
  status.textContent = "正在加载最高分候选…";
  image.onload = () => { image.hidden = false; status.textContent = ""; };
  image.onerror = () => { image.hidden = true; status.textContent = "本次结果的最高分候选图暂不可用"; };
  image.src = `${root}/preview/score`;
}
function clearReview() { clearScorePreview(); el<HTMLAnchorElement>("history-result").hidden = true; clearScene(); groups = []; preparationGroups = []; tangentArrows = []; grasps = []; resultId = ""; motionReady = false; motionState = "review"; motionSession = ""; motionPending = ""; stopPending = ""; autoSuctionReady = false; const select = el<HTMLSelectElement>("grasp"); select.replaceChildren(); select.disabled = true; el<HTMLInputElement>("review").checked = false; el<HTMLInputElement>("review").disabled = true; el("warning").textContent = "尚未解算"; el("metrics").textContent = ""; el("motion").textContent = "运动未就绪"; el<HTMLInputElement>("distance").value = "—"; updateTcpTarget(); }
function clearObservation() { maskPreview = ""; maskPreviewCaption = "所选实例 / 解算结果"; ++maskRevision; observation = ""; mask = ""; instances = []; selectedInstance = ""; frozenBaseCamera = null; updateFrames(); clearReview(); el<HTMLInputElement>("mask").value = ""; el("instance-list").replaceChildren(); el("vision-timings").textContent = "等待采集"; for (const id of ["rgb", "depth", "instances-overview", "overlay"]) el<HTMLImageElement>(id).removeAttribute("src"); el<HTMLAnchorElement>("download").hidden = true; el("observation").textContent = "尚未采集"; }
function receiveModelChoices(message: Message) {
  if (!Array.isArray(message.pose6d_models)) return;
  const models = message.pose6d_models.filter((item: Message) => typeof item?.object_id === "string" && item.object_id);
  const key = JSON.stringify(models);
  if (key === modelChoicesKey || !models.length) return;
  const select = el<HTMLSelectElement>("object"), previous = modelChoicesKey ? select.value : "";
  modelChoicesKey = key;
  select.replaceChildren(...models.map((item: Message) => new Option(
    item.mesh_file ? `${item.label || item.object_id} · ${item.mesh_file}` : String(item.label || item.object_id), item.object_id)));
  select.value = models.some((item: Message) => item.object_id === previous)
    ? previous : (models.find((item: Message) => item.default)?.object_id || models[0].object_id);
  update();
}
function send(type: string, fields: Message = {}) { if (!socket || socket.readyState !== WebSocket.OPEN || pending) return; pending = crypto.randomUUID(); const id = pending; socket.send(JSON.stringify({ type, request_id: id, ...fields })); requestTimer = window.setTimeout(() => { if (pending !== id) return; pending = ""; clearObservation(); state("请求超时，请重新采集", true); update(); }, 180000); update(); }
function sendMotion(type: string, fields: Message = {}) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  if (type === "stop") {
    if (stopPending) return;
    stopPending = crypto.randomUUID(); socket.send(JSON.stringify({ type: "pose6d_stop", request_id: stopPending, ...fields }));
  } else {
    if (motionPending) return;
    motionPending = crypto.randomUUID(); socket.send(JSON.stringify({ type: `pose6d_${type}`, request_id: motionPending, ...fields }));
  }
  update();
}
function requestToolPowerStatus() {
  if (!socket || socket.readyState !== WebSocket.OPEN || toolPowerStatusPending || toolPowerSetPending || motionPending) return;
  toolPowerStatusPending = crypto.randomUUID();
  socket.send(JSON.stringify({ type: "tool_power_status", request_id: toolPowerStatusPending }));
}
function applyToolPowerState(message: Message) {
  toolPowerAvailable = message.available === true && [0, 1, 2, 3].includes(message.voltage_type);
  toolPowerVoltage = toolPowerAvailable && Number.isFinite(Number(message.voltage_v)) ? Number(message.voltage_v) : null;
  toolPowerError = message.success === false ? String(message.message || "工具电源请求失败") : "";
  const state = el("tool-power-state");
  const unexpectedVoltage = toolPowerAvailable && toolPowerVoltage !== 0 && toolPowerVoltage !== 24;
  state.dataset.error = String(!!toolPowerError || !toolPowerAvailable || unexpectedVoltage);
  if (!toolPowerAvailable) state.textContent = toolPowerError || String(message.message || "电源状态未知");
  else if (toolPowerVoltage === 24) state.textContent = toolPowerError ? `24 V · ${toolPowerError}` : "24 V · 已供电";
  else if (toolPowerVoltage === 0) state.textContent = toolPowerError ? `0 V · ${toolPowerError}` : "0 V · 已关闭";
  else state.textContent = `${toolPowerVoltage} V · 非预期输出${toolPowerError ? ` · ${toolPowerError}` : ""}`;
  update();
}
function setToolPower(enabled: boolean) {
  if (!socket || socket.readyState !== WebSocket.OPEN || toolPowerSetPending) return;
  toolPowerStatusPending = "";
  toolPowerSetPending = crypto.randomUUID();
  toolPowerSetRequested = enabled;
  toolPowerError = "";
  el("tool-power-state").textContent = enabled ? "正在请求 24 V…" : "正在请求 0 V…";
  socket.send(JSON.stringify({ type: "tool_power_set", request_id: toolPowerSetPending, enabled }));
  update();
}
function update() {
  const online = !!socket && socket.readyState === WebSocket.OPEN;
  const power = el<HTMLInputElement>("tool-power");
  power.checked = toolPowerSetPending && toolPowerSetRequested !== null
    ? toolPowerSetRequested
    : toolPowerAvailable && toolPowerVoltage === 24;
  power.disabled = !online || !toolPowerAvailable || !!toolPowerSetPending || !!pending || !!motionPending || !!motionSession;
  el<HTMLButtonElement>("apply-tool").disabled = !online || !!pending || !!motionPending || !!motionSession;
  el<HTMLButtonElement>("capture").disabled = !online || !!pending || !!motionPending || !!motionSession;
  el<HTMLSelectElement>("capture-object").disabled = !online || !!pending || !!motionPending || !!motionSession;
  el<HTMLSelectElement>("object").disabled = !online || !!pending || !!motionPending || !!motionSession;
  el<HTMLInputElement>("mask").disabled = !observation || !!pending || !!motionPending || !!motionSession;
  el<HTMLButtonElement>("infer").disabled = !observation || (!mask && !selectedInstance) || !!pending || !!motionPending || !!motionSession;
  const checked = el<HTMLInputElement>("review").checked, locked = !!motionPending || !!toolPowerSetPending || !online;
  const selectedGrasp = grasps.find(grasp => grasp.grasp_id === el<HTMLSelectElement>("grasp").value);
  el<HTMLButtonElement>("prepare").disabled = !motionReady || selectedGrasp?.workspace_valid !== true || !checked || locked || motionState !== "review";
  el<HTMLButtonElement>("minus").disabled = locked || motionState !== "ready";
  el<HTMLButtonElement>("plus").disabled = locked || motionState !== "ready";
  el<HTMLButtonElement>("auto-suction").disabled = !autoSuctionReady || locked;
  el<HTMLButtonElement>("stop-motion").disabled = !motionSession || !online || !!stopPending;
  el<HTMLSelectElement>("grasp").disabled = !grasps.length || !!motionSession || locked;
  for (const button of Array.from(el("instance-list").querySelectorAll<HTMLButtonElement>("button[data-instance-id]"))) {
    const item = instances.find((candidate) => candidate.instance_id === button.dataset.instanceId);
    button.disabled = !item || item.status !== "ok" || Number(item.valid_depth_pixels) < 4 || !!pending || !!motionSession;
  }
}
function applyMotion(msg: Message) {
  if (typeof msg.state === "string") motionState = msg.state;
  autoSuctionReady = msg.auto_suction_ready === true;
  if (msg.tool_power && typeof msg.tool_power === "object") {
    toolPowerStatusPending = "";
    applyToolPowerState(msg.tool_power);
  }
  if (msg.session_id) motionSession = msg.session_id;
  else if (!msg.lease_retained && !msg.busy) motionSession = "";
  if (msg.confirmed_mm != null) el<HTMLInputElement>("distance").value = Number(msg.confirmed_mm).toFixed(1);
  else if (msg.state === "returning_home") el<HTMLInputElement>("distance").value = "返回初始姿态";
  else if (msg.state === "at_home") el<HTMLInputElement>("distance").value = "初始姿态到位";
  else if (msg.state === "returning_after_place") el<HTMLInputElement>("distance").value = "放置后回初始位";
  else if (msg.state === "completed") el<HTMLInputElement>("distance").value = "已回初始位";
  else if (msg.state === "placing") el<HTMLInputElement>("distance").value = "前往放置位";
  else if (msg.state === "placed") el<HTMLInputElement>("distance").value = "已到放置位";
  else if (msg.state === "lifted") el<HTMLInputElement>("distance").value = "提升完成";
  else if (msg.state === "blocked") el<HTMLInputElement>("distance").value = "位置未确认";
  if (msg.message) el("motion").textContent = msg.message;
  if (msg.state === "ready") el<HTMLInputElement>("review").disabled = true;
  else if (msg.state === "review") {
    el<HTMLInputElement>("review").disabled = !resultId || !motionReady;
    if (!msg.session_id && !msg.lease_retained) motionReady = false;
  }
  update();
}
function onMotionEvent(msg: Message) {
  if (msg.type === "pose6d_error" && msg.motion) {
    if (msg.request_id === motionPending) motionPending = "";
    if (msg.request_id === stopPending || !msg.motion.busy) stopPending = "";
    applyMotion(msg.motion); state(msg.message || "运动失败，请检查锁定状态", true); update(); return;
  }
  if (msg.type !== "pose6d_motion") return;
  applyMotion(msg);
  if (msg.request_id === motionPending && !msg.busy) motionPending = "";
  if (stopPending && !msg.busy) stopPending = "";
  update();
}
function connect() {
  socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.onopen = () => {
    el("connection").textContent = "已连接"; requestToolPowerStatus(); update();
  };
  socket.onclose = () => { vacuumIndicators.disconnect(); window.clearTimeout(requestTimer); pending = ""; motionPending = ""; stopPending = ""; toolPowerAvailable = false; toolPowerVoltage = null; toolPowerSetPending = ""; toolPowerSetRequested = null; toolPowerStatusPending = ""; toolPowerError = "连接中断，电源状态未知"; el("tool-power-state").textContent = toolPowerError; el("tool-power-state").dataset.error = "true"; el("connection").textContent = "未连接"; state("连接中断；已下发的运动可能继续，请确认实际 TCP", true); clearObservation(); update(); setTimeout(connect, 2000); };
  socket.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    vacuumIndicators.receive(msg);
    if (msg.type === "tool_power_state" && msg.arm === "r") {
      const isStatusResponse = msg.request_id === toolPowerStatusPending;
      const isSetResponse = msg.request_id === toolPowerSetPending;
      if (!isStatusResponse && !isSetResponse) return;
      if (isStatusResponse) toolPowerStatusPending = "";
      if (isStatusResponse && toolPowerSetPending) return;
      if (isSetResponse) { toolPowerSetPending = ""; toolPowerSetRequested = null; }
      applyToolPowerState(msg); return;
    }
    if (msg.type === "error" && (msg.request_id === toolPowerStatusPending || msg.request_id === toolPowerSetPending)) {
      if (msg.request_id === toolPowerStatusPending) toolPowerStatusPending = "";
      if (msg.request_id === toolPowerSetPending) { toolPowerSetPending = ""; toolPowerSetRequested = null; }
      toolPowerAvailable = false; toolPowerVoltage = null; toolPowerError = String(msg.message || "电源服务不可用");
      el("tool-power-state").textContent = toolPowerError; el("tool-power-state").dataset.error = "true"; update(); return;
    }
    if (msg.type === "pose6d_frames" && msg.arm === "r") { latestFrames = msg; receiveModelChoices(msg); receiveWorkspace(msg); framesReceivedAt = performance.now(); updateFrames(); return; }
    if (msg.type === "coordinate_state" && msg.arm === "r") {
      const tool = String(msg.current_tool ?? "");
      if (["tcpsuck", "tcpgrip"].includes(tool)) el<HTMLSelectElement>("controller-tool").value = tool;
      el("tool-state").textContent = `当前 ${tool || "未知"} · ${msg.motion_allowed ? "READY" : "BLOCKED"}`;
    }
    if (msg.type === "pose6d_motion" || (msg.type === "pose6d_error" && msg.motion)) onMotionEvent(msg);
    if (msg.type === "pose6d_tool_selected" && msg.request_id === pending) {
      window.clearTimeout(requestTimer); pending = ""; clearObservation();
      el<HTMLSelectElement>("controller-tool").value = msg.tool_name;
      el("tool-state").textContent = `当前 ${msg.tool_name} · 已验证匹配`;
      state(`${msg.tool_name} 已选中，请重新采集和解算`); update(); return;
    }
    if (msg.type === "pose6d_vision_progress" && msg.request_id === pending && msg.observation_id === observation) {
      state(msg.message || (msg.stage === "yolo" ? "YOLO 正在检测" : "SAM2 正在分割"));
      if (msg.stage === "yolo") el("vision-timings").textContent = "YOLO 正在检测…";
      else if (msg.stage === "sam2") {
        el("vision-timings").textContent = `YOLO ${Number(msg.yolo_inference_ms || 0).toFixed(1)} ms · ${msg.detection_count} 个目标 · SAM2 正在分割…`;
      }
      return;
    }
    if (!msg.request_id || msg.request_id !== pending) return;
    window.clearTimeout(requestTimer); pending = "";
    if (msg.type === "pose6d_error" || msg.type === "error") state(msg.message, true);
    else if (msg.type === "pose6d_capture") {
      clearObservation(); observation = msg.observation_id; el<HTMLImageElement>("rgb").src = msg.rgb; el<HTMLImageElement>("depth").src = msg.depth;
      frozenBaseCamera = msg.T_base_camera ? matrix(msg.T_base_camera) : null; poseBaseFrame = msg.base_frame || "BASE"; receiveWorkspace(msg); updateFrames(); fitView();
      const download = el<HTMLAnchorElement>("download"); download.href = msg.rgb; download.download = `rgb-${observation}.png`; download.hidden = false;
      const objectLabel = el<HTMLSelectElement>("capture-object").selectedOptions[0]?.textContent || msg.object_id;
      el("observation").textContent = `${objectLabel} · ${msg.profile} · ${msg.width}×${msg.height} · ${msg.camera_frame} · ${msg.calibration}`;
      state("图像已冻结，正在运行 YOLO 检测和 SAM 分割");
      send("pose6d_segment", { observation_id: observation });
    } else if (msg.type === "pose6d_instances" && msg.observation_id === observation) {
      instances = Array.isArray(msg.detections) ? msg.detections : [];
      mask = ""; selectedInstance = ""; el<HTMLImageElement>("instances-overview").src = msg.detection_overlay || "";
      el<HTMLImageElement>("overlay").removeAttribute("src"); el("overlay-caption").textContent = "所选实例 / 解算结果";
      const timing = msg.timings || {};
      el("vision-timings").textContent = `YOLO ${Number(timing.yolo_inference_ms || 0).toFixed(1)} ms（服务 ${Number(timing.yolo_request_ms || 0).toFixed(1)} ms / 往返 ${Number(timing.yolo_round_trip_ms || 0).toFixed(1)} ms）；SAM 总计 ${Number(timing.sam_inference_ms_total || 0).toFixed(1)} ms（图像 ${Number(timing.sam_image_ms || 0).toFixed(1)} + 实例 ${Number(timing.sam_predict_ms_total || 0).toFixed(1)} ms，往返 ${Number(timing.sam_round_trip_ms || 0).toFixed(1)} ms）`;
      const list = el("instance-list"); list.replaceChildren();
      if (!instances.length) { const empty = document.createElement("p"); empty.textContent = "YOLO 没有检测到目标"; list.append(empty); }
      for (const item of instances) {
        const button = document.createElement("button"); button.type = "button"; button.className = "pose-instance";
        button.dataset.instanceId = item.instance_id;
        button.disabled = item.status !== "ok" || Number(item.valid_depth_pixels) < 4;
        button.textContent = `#${item.detection_id} · ${item.class_name} · ${Number(item.confidence).toFixed(2)} · SAM ${item.status === "ok" ? `${Number(item.sam_score).toFixed(2)} / ${Number(item.sam_predict_ms).toFixed(1)} ms` : "失败"} · ${item.mask_pixels} px`;
        button.title = `${item.instance_id} · ${item.message || `${item.bbox.join(", ")} · 有效深度 ${item.valid_depth_pixels} px`}`;
        button.onclick = () => { selectedInstance = item.instance_id; mask = ""; maskPreview = ""; maskPreviewCaption = `#${item.detection_id} · SAM 实例`;
          el<HTMLImageElement>("overlay").removeAttribute("src"); el("overlay-caption").textContent = `#${item.detection_id} · SAM 实例`; el<HTMLInputElement>("mask").value = ""; clearReview();
          for (const row of Array.from(list.querySelectorAll("button"))) row.dataset.selected = String(row === button);
          send("pose6d_preview", { observation_id: observation, instance_id: selectedInstance }); state("正在预览所选 SAM 实例"); };
        list.append(button);
      }
      state(msg.status === "ok" || msg.status === "partial" ? `分割完成：${instances.length} 个 YOLO 目标` : msg.message, msg.status === "error");
    } else if (msg.type === "pose6d_instance_preview" && msg.observation_id === observation && msg.instance_id === selectedInstance) {
      maskPreview = msg.preview; maskPreviewCaption = `#${instances.find(item => item.instance_id === selectedInstance)?.detection_id} · SAM 实例`;
      el<HTMLImageElement>("overlay").src = maskPreview;
      state(`已选择 ${selectedInstance} · 前景 ${msg.mask_pixels} px · 有效深度 ${msg.valid_depth_pixels} px`);
    } else if (msg.type === "pose6d_result" && msg.observation_id === observation) {
      const historyLink = el<HTMLAnchorElement>("history-result");
      if (typeof msg.history_id === "string" && /^\d+$/.test(msg.history_id)) {
        historyLink.href = `/6d_pose_history/${msg.history_id}/`; historyLink.hidden = false;
        historyLink.textContent = msg.archive_status === "failed" ? "本次历史归档失败" : "查看本次历史";
      }
      showScorePreview(msg);
      if (msg.mask_source === "sam2") el("vision-timings").textContent += ` · FoundationPose 输入 ${msg.instance_id}`;
      if (msg.T_base_camera) frozenBaseCamera = matrix(msg.T_base_camera);
      poseBaseFrame = msg.base_frame || poseBaseFrame;
      receiveWorkspace(msg);
      const waypoints = msg.motion_waypoints || {};
      prepareDistanceMm = Number(waypoints.prepare_distance_mm ?? 40);
      secondPrepareDistanceMm = Number(waypoints.second_prepare_distance_mm ?? 5);
      contactOffsetMm = Number(waypoints.contact_offset_mm ?? 0);
      liftHeightMm = Number(waypoints.lift_height_mm ?? 50);
      placeJointDegrees = Array.isArray(waypoints.place_joint_degrees) && waypoints.place_joint_degrees.length === 6
        && waypoints.place_joint_degrees.every(Number.isFinite) ? waypoints.place_joint_degrees : [];
      el("prepare-legend").textContent = `准备位 +${prepareDistanceMm.toFixed(0)} mm（紫）`;
      el<HTMLButtonElement>("prepare").textContent = `移动到 +${prepareDistanceMm.toFixed(0)} mm 准备位`;
      const configuredHome = waypoints.home_joint_degrees;
      const yamlHome = Array.isArray(configuredHome) && configuredHome.length === 6 && configuredHome.every(Number.isFinite);
      const homeLabel = yamlHome ? "初始姿态（YAML）" : "控制器初始姿态";
      const homeDescription = yamlHome ? `初始关节角 [${configuredHome.map((value: number) => value.toFixed(3)).join(", ")}]°；` : "";
      el("auto-path").textContent = `自动路径：${prepareDistanceMm.toFixed(0)} → ${secondPrepareDistanceMm.toFixed(0)} → ${(-contactOffsetMm).toFixed(0)} mm，BASE +Z 提升 ${liftHeightMm.toFixed(0)} mm；${homeDescription}吸到后 MOVEJ 回${homeLabel} → 确认到位 → MOVEJ 放置，关节角 ${placeJointDegrees.length ? `[${placeJointDegrees.map(value => value.toFixed(3)).join(", ")}]°` : "未配置"} → 关闭吸盘 → MOVEJ 回初始姿态`;
      draw(msg); resultId = msg.result_id; motionReady = msg.motion_ready; motionState = "review";
      const cupMm = Number(msg.suction?.cup_diameter_m ?? .01) * 1000;
      el("cup-info").textContent = `吸盘实物直径 ${cupMm.toFixed(1)} mm，细线圈表示人工标注接触区域。`;
      const select = el<HTMLSelectElement>("grasp"); select.replaceChildren(...graspOptions(msg.grasps));
      if (!select.value && select.options.length) select.options[0].selected = true;
      highlight();
      el<HTMLInputElement>("review").disabled = !msg.grasps.some((g: Message) => g.workspace_valid === true) || !motionReady;
      if (msg.overlay) { el<HTMLImageElement>("overlay").src = msg.overlay; el("overlay-caption").textContent = selectedInstance ? `#${instances.find(item => item.instance_id === selectedInstance)?.detection_id} · 解算结果` : "解算结果"; }
      el("warning").textContent = `${msg.screening.status}${msg.screening.warning ? ` · ${msg.screening.warning}` : ""}`;
      el("metrics").textContent = JSON.stringify(msg.screening.metrics, null, 2);
      el("motion").textContent = msg.motion_reason;
      el("cup-info").textContent = msg.suction_available === false
        ? "该模型尚未标注吸盘接触区域；当前仅提供 6D 位姿计算与审阅，运动控制已禁用。"
        : `吸盘实物直径 ${cupMm.toFixed(1)} mm，细线圈表示人工标注接触区域。`;
      state("解算完成，请复核模型、告警和吸盘方向");
    }
    update();
  };
}
el<HTMLButtonElement>("capture").onclick = () => { const objectId = el<HTMLSelectElement>("capture-object").value; clearObservation(); send("pose6d_capture", { object_id: objectId }); state("正在采集"); };
el<HTMLSelectElement>("object").onchange = () => {
  clearReview();
  if (maskPreview) el<HTMLImageElement>("overlay").src = maskPreview;
  else el<HTMLImageElement>("overlay").removeAttribute("src");
  el("overlay-caption").textContent = maskPreviewCaption;
  state(observation ? "解算 OBJ 已切换，可使用当前图像和 Mask 重新解算" : "解算 OBJ 已切换，请先采集并分割");
  update();
};
el<HTMLButtonElement>("apply-tool").onclick = () => {
  const tool = el<HTMLSelectElement>("controller-tool").value;
  clearObservation(); send("pose6d_select_tool", { tool_name: tool }); state(`正在切换到 ${tool}`);
};
el<HTMLInputElement>("mask").onchange = async (event) => {
  const file = (event.target as HTMLInputElement).files?.[0], revision = ++maskRevision, captured = observation;
  mask = ""; selectedInstance = ""; maskPreview = ""; maskPreviewCaption = "手工 Mask"; clearReview(); el<HTMLImageElement>("overlay").removeAttribute("src"); el("overlay-caption").textContent = "手工 Mask";
  for (const button of Array.from(el("instance-list").querySelectorAll("button"))) button.dataset.selected = "false";
  update(); if (!file) return;
  if (file.size > 5 * 1024 * 1024) { state("Mask 文件过大", true); return; }
  try { const data = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = reject; reader.readAsDataURL(file); });
    if (revision !== maskRevision || !captured || captured !== observation) return; mask = data.split(",", 2)[1] || ""; maskPreview = data; el<HTMLImageElement>("overlay").src = data; state("Mask 已选择"); update();
  } catch { if (revision === maskRevision) state("Mask 读取失败", true); }
};
el<HTMLButtonElement>("infer").onclick = () => { clearScorePreview(); send("pose6d_infer", { object_id: el<HTMLSelectElement>("object").value, observation_id: observation, ...(mask ? { mask_png: mask } : { instance_id: selectedInstance }) }); state("正在解算 FoundationPose 位姿"); };
el<HTMLSelectElement>("grasp").onchange = highlight;
for (const id of ["show-tangent", "show-camera", "show-tool", "show-prepare", "show-workspace"]) el<HTMLInputElement>(id).onchange = showLayers;
el<HTMLButtonElement>("fit-view").onclick = fitView;
el<HTMLInputElement>("review").onchange = update;
el<HTMLInputElement>("tool-power").onchange = (event) => setToolPower((event.target as HTMLInputElement).checked);
el<HTMLButtonElement>("prepare").onclick = () => sendMotion("prepare", { observation_id: observation, result_id: resultId, grasp_id: el<HTMLSelectElement>("grasp").value, review_confirmed: true, warning_confirmed: true });
el<HTMLButtonElement>("minus").onclick = () => sendMotion("jog", { session_id: motionSession, step_mm: -Math.abs(Number(el<HTMLInputElement>("step").value)) });
el<HTMLButtonElement>("plus").onclick = () => sendMotion("jog", { session_id: motionSession, step_mm: Math.abs(Number(el<HTMLInputElement>("step").value)) });
el<HTMLButtonElement>("auto-suction").onclick = () => sendMotion("auto_suction", { session_id: motionSession });
el<HTMLButtonElement>("stop-motion").onclick = () => sendMotion("stop", { session_id: motionSession });
window.setInterval(() => requestToolPowerStatus(), 5000);
connect(); update();
