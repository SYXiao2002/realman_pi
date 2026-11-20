/** Review geometry is expressed in metres. Matrices map child -> parent. */
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import URDFLoader from "urdf-loader";

export const matrix = (rows: number[][]) => new THREE.Matrix4().set(...rows.flat());
export const rankColor = (rank: number) => ["#e41a1c", "#00bfff", "#ffbf00", "#bb66ff", "#00dd77"][rank % 5];
export type Stage = "before" | "after";
/** Contact-centre frames shared by numeric poses, gripper geometry and pose markers. */
export function contactFrame(candidate: any, stage: Stage): number[][] | null {
  if (stage === "before") return candidate.T_camera_grasp_before_alignment
    || (!candidate.vertical_alignment?.applied ? candidate.T_camera_grasp : null);
  if (candidate.vertical_alignment?.applied) return candidate.T_camera_grasp_after_alignment || null;
  if (candidate.vertical_alignment?.status === "backend_not_applicable") return contactFrame(candidate, "before");
  return null;
}
export function wirePoints(candidate: any, gripper: any): THREE.Vector3[] {
  if (candidate.T_camera_native_grasp) {
    // Offline preview glyph dimensions, rebased from the native wrist to the contact centre.
    // These dimensions do not describe the robot tool.
    const half = Number(candidate.width_m) / 2;
    const [palm, tip] = candidate.backend === "contact_graspnet" ? [.0584, .1053] : [.05, .09];
    const contactOffset = Number(candidate.vertical_alignment?.wrist_offset_m) || 0;
    return [[0, 0, 0], [0, 0, palm], [-half, 0, palm], [-half, 0, tip],
      [-half, 0, palm], [half, 0, palm], [half, 0, tip]]
      .map(([x, y, z]) => new THREE.Vector3(x, y, z - contactOffset));
  }
  // Common protocol: origin is closing centre, X closing, Z approach.
  // Show measured fingers only; never borrow Panda/URDF dimensions.
  const half = Number(candidate.width_m) / 2, depth = Number(gripper?.finger_depth_m);
  if (!Number.isFinite(half) || !Number.isFinite(depth) || depth <= 0) return [];
  return [[-half, 0, 0], [-half, 0, -depth], [half, 0, -depth], [half, 0, 0]]
    .map((point) => new THREE.Vector3(...point));
}
/** Render the backend-owned canonical two-box geometry; dimensions are never recomputed here. */
export function boxGripper(geometry: any, color: string): THREE.Group | null {
  if (!geometry || geometry.model !== "visual_finger_boxes/v1" || geometry.frame !== "grasp_contact"
    || !Array.isArray(geometry.boxes) || geometry.boxes.length !== 2) return null;
  const material = new THREE.MeshStandardMaterial({ color, metalness: .1, roughness: .6 });
  const group = new THREE.Group();
  for (const box of geometry.boxes) {
    const size = box.size_m.map((value: number) => Number(value));
    const center = box.center_m.map((value: number) => Number(value));
    if (size.length !== 3 || center.length !== 3 || size.some((value: number) => !Number.isFinite(value) || value <= 0)
      || center.some((value: number) => !Number.isFinite(value))) { group.clear(); return null; }
    const finger = new THREE.Mesh(new THREE.BoxGeometry(...size), material);
    finger.position.set(...center);
    group.add(finger);
  }
  return group;
}
/** Box outlines for the 2D projection, derived from the same serialized boxes. */
export function boxWirePoints(geometry: any): THREE.Vector3[] {
  if (!geometry?.boxes) return [];
  const points: THREE.Vector3[] = [];
  for (const box of geometry.boxes) {
    const [sx, sy, sz] = box.size_m.map((value: number) => Number(value) / 2);
    const [cx, cy, cz] = box.center_m.map((value: number) => Number(value));
    if (![sx, sy, sz, cx, cy, cz].every(Number.isFinite)) continue;
    points.push(...[[-sx, -sy, -sz], [sx, -sy, -sz], [sx, sy, -sz], [-sx, sy, -sz], [-sx, -sy, -sz],
      [-sx, -sy, sz], [sx, -sy, sz], [sx, sy, sz], [-sx, sy, sz], [-sx, -sy, sz]]
      .map(([x, y, z]) => new THREE.Vector3(cx + x, cy + y, cz + z)));
  }
  return points;
}
/** Legacy renderer retained only for snapshots created before collision.geometry existed. */
function legacyBoxGripper(candidate: any, gripper: any, color: string): THREE.Group | null {
  const depth = Number(gripper?.finger_depth_m), thick = Number(gripper?.finger_thickness_m);
  if (!Number.isFinite(depth) || depth <= 0 || !Number.isFinite(thick) || thick <= 0) return null;
  const width = Number.isFinite(Number(gripper?.finger_width_m)) && Number(gripper.finger_width_m) > 0
    ? Number(gripper.finger_width_m) : thick;
  const clear = Number(gripper?.clearance_m) || 0;
  const low = Number(gripper?.min_opening_m) || 0;
  const high = Number(gripper?.max_opening_m);
  let opening = Math.max(Number(candidate.width_m) + 2 * clear, low);
  if (Number.isFinite(high)) opening = Math.min(opening, high);
  const material = new THREE.MeshStandardMaterial({ color, metalness: .1, roughness: .6 });
  const group = new THREE.Group();
  for (const side of [-1, 1]) {
    const finger = new THREE.Mesh(new THREE.BoxGeometry(thick, width, depth), material);
    finger.position.set(side * (opening / 2 + thick / 2), 0, -depth / 2); group.add(finger);
  }
  return group;
}
function dispose(group: THREE.Object3D) {
  group.traverse((item: any) => { item.geometry?.dispose();
    const materials = Array.isArray(item.material) ? item.material : [item.material];
    materials.forEach((material: any) => material?.map?.dispose());
    if (Array.isArray(item.material)) item.material.forEach((value: any) => value.dispose());
    else item.material?.dispose(); });
  group.clear();
}
function pointCloud(snapshot: any, sizePx: number, selected: string) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(snapshot.cloud.xyz_m.flat(), 3));
  const colors = snapshot.cloud.rgb.map((rgb: number[]) => rgb.map((value) => value / 255));
  const candidate = snapshot.candidates.find((item: any) => item.candidate_id === selected);
  const display = candidate?.collision?.display;
  if (display && display.near_mask?.length === colors.length && display.collision_mask?.length === colors.length) {
    colors.forEach((rgb: number[], index: number) => {
      if (display.near_mask[index]) colors[index] = [1, .84, 0];
      if (display.collision_mask[index]) colors[index] = [1, .12, .08];
    });
  }
  geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors.flat(), 3));
  return new THREE.Points(geometry, new THREE.PointsMaterial({ size: sizePx, vertexColors: true, sizeAttenuation: false }));
}
export function candidateColor(candidate: any, rank: number) {
  return candidate.collision?.status === "collision" ? "#ef3b2c"
    : candidate.collision?.status === "warning" ? "#f0a51a"
      : candidate.collision?.status === "unknown" ? "#8c9aa0"
        : candidate.collision?.status === "safe" ? "#159957" : rankColor(rank);
}
function candidatePose(candidate: any, gripper: any, selected: boolean, frame: number[][], color: string, rank: number) {
  const group = new THREE.Group(); group.matrixAutoUpdate = false; group.matrix.copy(matrix(frame));
  group.add(new THREE.AxesHelper(selected ? .065 : .03));
  const boxes = candidate.collision !== undefined
    ? boxGripper(candidate.collision.geometry, color) : legacyBoxGripper(candidate, gripper, color);
  if (boxes) group.add(boxes);
  else if (candidate.collision === undefined) {
    const points = wirePoints(candidate, gripper);
    if (points.length) group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),
      new THREE.LineBasicMaterial({ color })));
  }
  const labelCanvas = document.createElement("canvas"); labelCanvas.width = 128; labelCanvas.height = 48;
  const ctx = labelCanvas.getContext("2d")!; ctx.fillStyle = color; ctx.font = "bold 30px sans-serif";
  ctx.fillText(`#${rank + 1}`, 4, 34);
  const label = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(labelCanvas), depthTest: false }));
  label.position.set(0, -.02, 0); label.scale.set(.055, .021, 1); group.add(label);
  return group;
}

/** Synchronized before/after optical views, plus the calibrated fleet. */
export class GraspView {
  private views: any[] = [];
  private snapshot: any;
  private selected = "";
  private visible = new Set<string>();
  private syncing = false;
  private layout: any;
  private robots = new Map<string, any>();
  private loadGeneration = 0;
  private pointSize = 3.5;
  constructor(cloudCanvas: HTMLCanvasElement, alignedCanvas: HTMLCanvasElement, fleetCanvas: HTMLCanvasElement,
    private message: (text: string) => void) {
    try {
      for (const [index, canvas] of [cloudCanvas, alignedCanvas, fleetCanvas].entries()) {
        const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
        renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
        const scene = new THREE.Scene(); scene.background = new THREE.Color(0x172126);
        const camera = new THREE.PerspectiveCamera(48, 1, .005, 30);
        camera.up.set(0, index < 2 ? -1 : 0, index < 2 ? 0 : 1);
        camera.position.set(index < 2 ? 0 : 1.8, index < 2 ? -.2 : -2, index < 2 ? -2 : 1.5);
        const controls = new OrbitControls(camera, canvas); controls.target.set(0, 0, index < 2 ? .5 : .4);
        const data = new THREE.Group(), fleet = new THREE.Group(); scene.add(data, fleet);
        scene.add(new THREE.HemisphereLight(0xffffff, 0x647579, 3));
        const light = new THREE.DirectionalLight(0xffffff, 3); light.position.set(2, -3, 4); scene.add(light);
        scene.add(new THREE.AxesHelper(.15));
        if (index === 2) { const grid = new THREE.GridHelper(3, 20); grid.rotation.x = Math.PI / 2; scene.add(grid); }
        const resize = () => { const box = canvas.getBoundingClientRect();
          renderer.setSize(Math.max(1, box.width), Math.max(1, box.height), false);
          camera.aspect = Math.max(1, box.width) / Math.max(1, box.height); camera.updateProjectionMatrix(); renderer.render(scene, camera); };
        new ResizeObserver(resize).observe(canvas); resize();
        const view = { renderer, scene, camera, controls, data, fleet, canvas }; this.views.push(view);
        controls.addEventListener("change", () => {
          if (index < 2 && !this.syncing) {
            this.syncing = true; const other = this.views[1 - index];
            if (other) { other.camera.position.copy(camera.position); other.camera.quaternion.copy(camera.quaternion);
              other.camera.up.copy(camera.up); other.controls.target.copy(controls.target); other.controls.update();
              other.renderer.render(other.scene, other.camera); this.describe(other); }
            this.syncing = false;
          }
          renderer.render(scene, camera); this.describe(view);
        });
        renderer.render(scene, camera);
      }
      this.message("点云为相机光学系；对齐前后视角同步。布局以所选抓取 pose 为轨道中心：左键旋转、滚轮缩放、右键平移，初始视角对齐腕部相机俯视。对齐后接触中心的红 X 为闭合方向，绿 Y，蓝 Z 为接近方向。线框为算法示意尺寸。");
    } catch (error) { this.message(`WebGL 渲染失败：${String(error)}`); }
  }
  private describe(view: any) {
    view.canvas.dataset.view = JSON.stringify({ position: view.camera.position.toArray(), target: view.controls.target.toArray() });
  }
  setPointSize(sizePx: number) {
    if (!Number.isFinite(sizePx)) return;
    this.pointSize = Math.min(8, Math.max(1, sizePx));
    this.update(this.snapshot, this.selected, this.visible);
  }
  async setLayout(layout: any) {
    this.layout = layout; const generation = ++this.loadGeneration;
    const view = this.views[2]; if (!view) return;
    view.canvas.dataset.modelState = "loading";
    dispose(view.fleet); this.robots.clear();
    try {
      const loader = new URDFLoader(); loader.packages = { rm65_description: `${location.origin}/models` };
      for (const config of layout.robots) {
        const base = new THREE.Group(); base.position.set(config.transform.x, config.transform.y, config.transform.z);
        base.rotation.set(config.transform.roll, config.transform.pitch, config.transform.yaw, "ZYX");
        base.add(new THREE.AxesHelper(.12)); view.fleet.add(base);
        const robot = await loader.loadAsync(`${location.origin}${config.urdf_url}`);
        if (generation !== this.loadGeneration) { dispose(robot); return; }
        base.add(robot); robot.visible = false; this.robots.set(config.id, { base, robot });
      }
      this.update(this.snapshot, this.selected, this.visible);
      this.resetLayoutView();
      view.canvas.dataset.modelState = "ready";
    } catch (error) { view.canvas.dataset.modelState = "failed"; this.message(`机械臂模型加载失败：${String(error)}`); }
  }
  resetLayoutView() {
    const view = this.views[2]; if (!view || !this.snapshot) return;
    // turntable 极轴固定为世界竖直：俯视抓取点也不会像“相机 up=光学−Y”那样在极点翻滚。
    view.camera.up.set(0, 0, 1);
    // 优先以“实际抓取 pose”为轨道中心、从腕部相机光心俯视，使初始画面≈采集视角；
    // 缺少外参或抓取 pose 时回退到点云/机械臂包围球取景。
    if (this.frameOnGraspPose(view)) return;
    const bounds = new THREE.Box3().setFromObject(view.data);
    if (bounds.isEmpty()) {
      const active = this.robots.get(this.snapshot.arm);
      if (active) bounds.expandByObject(active.base);
    }
    if (bounds.isEmpty()) return;
    const sphere = bounds.getBoundingSphere(new THREE.Sphere());
    const vertical = THREE.MathUtils.degToRad(view.camera.fov) / 2;
    const horizontal = Math.atan(Math.tan(vertical) * view.camera.aspect);
    const distance = Math.max(.3, sphere.radius) / Math.sin(Math.min(vertical, horizontal)) * 1.12;
    view.controls.target.copy(sphere.center);
    view.camera.position.copy(sphere.center).add(new THREE.Vector3(1, -1.4, .9).normalize().multiplyScalar(distance));
    view.controls.update(); view.renderer.render(view.scene, view.camera); this.describe(view);
  }
  /** 以所选抓取 pose 为轨道中心、从腕部相机光心俯视取景；成功返回 true。
   *  OrbitControls 保持启用：左键绕抓取点公转、滚轮缩放、右键平移，交互与其它视图一致。 */
  private frameOnGraspPose(view: any): boolean {
    const config = this.layout?.robots.find((robot: any) => robot.id === this.snapshot.arm);
    if (!config) return false;
    // 与 update() 一致的基座世界变换：position + Euler(roll, pitch, yaw, "ZYX")。
    const base = new THREE.Matrix4().compose(
      new THREE.Vector3(config.transform.x, config.transform.y, config.transform.z),
      new THREE.Quaternion().setFromEuler(new THREE.Euler(config.transform.roll, config.transform.pitch, config.transform.yaw, "ZYX")),
      new THREE.Vector3(1, 1, 1));
    const target = this.selectedGraspWorldPoint(base);
    if (!target) return false;
    view.controls.target.copy(target);
    // 初始视点放到腕部相机光心，朝向≈采集画面。无外参、或光心与抓取点连线接近平行世界
    // 竖直（OrbitControls 会在极点退化翻滚）时，退让到标准斜俯视，保证轨道交互始终自然。
    let position: THREE.Vector3;
    if (this.snapshot.T_base_camera) {
      const camWorld = base.clone().multiply(matrix(this.snapshot.T_base_camera));
      const eye = new THREE.Vector3(camWorld.elements[12], camWorld.elements[13], camWorld.elements[14]);
      const span = eye.distanceTo(target);
      const offAxis = new THREE.Vector3().subVectors(target, eye).cross(new THREE.Vector3(0, 0, 1)).length();
      position = (span < 1e-3 || offAxis < .15 * span)
        ? target.clone().add(new THREE.Vector3(1, -1.4, .9).normalize().multiplyScalar(Math.max(span, .35)))
        : eye;
    } else {
      position = target.clone().add(new THREE.Vector3(1, -1.4, .9).normalize().multiplyScalar(.45));
    }
    view.camera.position.copy(position);
    view.controls.update(); view.renderer.render(view.scene, view.camera); this.describe(view);
    return true;
  }
  /** 所选候选（无选中则取 rank 最高）的实际抓取 pose 世界坐标；与 update() 里 marker 同源。 */
  private selectedGraspWorldPoint(base: THREE.Matrix4): THREE.Vector3 | null {
    const list = this.snapshot?.candidates;
    if (!Array.isArray(list) || !list.length) return null;
    const candidate = list.find((c: any) => c.candidate_id === this.selected) || list[0];
    if (!candidate) return null;
    // 优先后端给出的基座系 pose（对齐后 > 通用 > 对齐前）；否则用冻结外参 T_base_camera
    // 把接触中心矩阵换算到基座系，与 pose 卡片和布局 marker 完全一致。
    const baseRows = candidate.T_base_grasp_after_alignment || candidate.T_base_grasp
      || candidate.T_base_grasp_before_alignment;
    let local: THREE.Matrix4 | null = null;
    if (baseRows) local = matrix(baseRows);
    else if (this.snapshot.T_base_camera) {
      const cam = contactFrame(candidate, "after") || contactFrame(candidate, "before");
      if (cam) local = matrix(this.snapshot.T_base_camera).multiply(matrix(cam));
    }
    if (!local) return null;
    return new THREE.Vector3(local.elements[12], local.elements[13], local.elements[14]).applyMatrix4(base);
  }
  update(snapshot: any, selected: string, visible?: Set<string>) {
    const newObservation = snapshot && (snapshot.observation_id !== this.snapshot?.observation_id || snapshot.phase !== this.snapshot?.phase);
    this.snapshot = snapshot; this.selected = selected; this.visible = visible || new Set(snapshot?.candidates.slice(0, 5).map((c: any) => c.candidate_id));
    for (const view of this.views) dispose(view.data);
    const layoutCanvas = this.views[2]?.canvas;
    if (layoutCanvas) { layoutCanvas.dataset.candidates = ""; layoutCanvas.dataset.alignedPose = ""; layoutCanvas.dataset.arm = snapshot?.arm || ""; }
    for (const [arm, { robot }] of this.robots) {
      robot.visible = arm === snapshot?.arm;
      if (robot.visible) robot.setJointValues(Object.fromEntries(snapshot.seed_joint_degrees.map((value: number, i: number) => [`joint_${i + 1}`, value * Math.PI / 180])));
    }
    if (snapshot) {
      if (newObservation && snapshot.cloud.xyz_m.length) {
        const bounds = new THREE.Box3().setFromPoints(snapshot.cloud.xyz_m.map((p: number[]) => new THREE.Vector3(...p)));
        const center = bounds.getCenter(new THREE.Vector3());
        const distance = Math.max(.25, bounds.getSize(new THREE.Vector3()).length() * 1.5);
        for (const view of this.views.slice(0, 2)) {
          view.controls.target.copy(center); view.camera.position.copy(center).add(new THREE.Vector3(0, -.1, -1).normalize().multiplyScalar(distance));
        }
      }
      for (const [index, stage] of (["before", "after"] as Stage[]).entries()) {
        const optical = this.views[index]; if (!optical) continue;
        if (snapshot.cloud.workspace_crop_status === "applied") {
          optical.data.add(pointCloud(snapshot, this.pointSize, selected));
        }
        const rendered: string[] = [];
        snapshot.candidates.forEach((candidate: any, rank: number) => {
          const frame = contactFrame(candidate, stage);
          if (frame && this.visible.has(candidate.candidate_id)) {
            optical.data.add(candidatePose(candidate, snapshot.gripper, candidate.candidate_id === selected, frame,
              candidateColor(candidate, rank), rank));
            rendered.push(candidate.candidate_id);
          }
        });
        optical.canvas.dataset.candidates = rendered.join(",");
      }
      const world = this.views[2], config = this.layout?.robots.find((robot: any) => robot.id === snapshot.arm);
      if (world && config && snapshot.T_base_camera) {
        const base = new THREE.Group(); base.position.set(config.transform.x, config.transform.y, config.transform.z);
        base.rotation.set(config.transform.roll, config.transform.pitch, config.transform.yaw, "ZYX");
        if (snapshot.workspace_m) {
          const workspaceBounds = new THREE.Box3(new THREE.Vector3(...snapshot.workspace_m[0]), new THREE.Vector3(...snapshot.workspace_m[1]));
          const workspace = new THREE.Box3Helper(workspaceBounds, 0x4ade80);
          (workspace.material as THREE.Material).transparent = true;
          (workspace.material as THREE.Material).opacity = 0.85;
          base.add(workspace);
        }
        // Amber box = wider collision-detection region (superset of the green TCP
        // corridor). Only drawn when it actually differs from the workspace.
        if (snapshot.collision_workspace_m
            && JSON.stringify(snapshot.collision_workspace_m) !== JSON.stringify(snapshot.workspace_m)) {
          const collisionBounds = new THREE.Box3(new THREE.Vector3(...snapshot.collision_workspace_m[0]), new THREE.Vector3(...snapshot.collision_workspace_m[1]));
          const collision = new THREE.Box3Helper(collisionBounds, 0xfbbf24);
          (collision.material as THREE.Material).transparent = true;
          (collision.material as THREE.Material).opacity = 0.6;
          base.add(collision);
        }
        const camera = new THREE.Group(); camera.matrixAutoUpdate = false; camera.matrix.copy(matrix(snapshot.T_base_camera));
        if (snapshot.cloud.workspace_crop_status === "applied") camera.add(pointCloud(snapshot, this.pointSize, selected));
        base.add(camera);
        const rendered: string[] = [];
        snapshot.candidates.forEach((candidate: any, rank: number) => {
          const contact = candidate.vertical_alignment?.applied ? contactFrame(candidate, "after") : contactFrame(candidate, "before");
          if (contact && this.visible.has(candidate.candidate_id)) {
            const frame = matrix(snapshot.T_base_camera).multiply(matrix(contact));
            const rows = Array.from({ length: 4 }, (_, r) => Array.from({ length: 4 }, (_, c) => frame.elements[c * 4 + r]));
            base.add(candidatePose(candidate, snapshot.gripper, candidate.candidate_id === selected, rows,
              candidateColor(candidate, rank), rank));
            rendered.push(candidate.candidate_id);
            if (candidate.candidate_id === selected && candidate.vertical_alignment?.applied) {
              const marker = new THREE.Group(); marker.matrixAutoUpdate = false;
              marker.matrix.copy(matrix(snapshot.T_base_camera).multiply(matrix(contact)));
              marker.add(new THREE.AxesHelper(.12), new THREE.Mesh(new THREE.SphereGeometry(.006),
                new THREE.MeshBasicMaterial({ color: rankColor(rank) })));
              base.add(marker);
              world.canvas.dataset.alignedPose = candidate.candidate_id;
            }
          }
        });
        world.data.add(base);
        world.canvas.dataset.candidates = rendered.join(",");
      }
    }
    this.views.forEach((view) => {
      if (!snapshot) view.canvas.dataset.candidates = "";
      view.controls.update();
      view.renderer.render(view.scene, view.camera); this.describe(view);
    });
    const cropStatus = snapshot?.cloud?.workspace_crop_status;
    if (cropStatus === "applied") {
      this.message(`已按 base_frame 工作区裁切点云：${snapshot.cloud.workspace_point_count} 点，显示 ${snapshot.cloud.displayed_point_count} 点。`);
    } else if (snapshot && cropStatus === "missing_base_camera") {
      this.message("缺少采集时 T_base_camera，无法判断 workspace 内点云；点云已隐藏。");
    } else if (snapshot && cropStatus === "invalid_workspace") {
      this.message("workspace_m 无效，无法裁切工作区点云；点云已隐藏。");
    } else if (snapshot) {
      this.message("快照没有 workspace 裁切标记，点云已隐藏。");
    }
    if (newObservation) this.resetLayoutView();
  }
}
