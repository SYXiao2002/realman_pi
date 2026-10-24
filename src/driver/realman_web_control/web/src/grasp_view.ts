/** Review geometry is expressed in metres. Matrices map child -> parent. */
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import URDFLoader from "urdf-loader";

export const matrix = (rows: number[][]) => new THREE.Matrix4().set(...rows.flat());
export const rankColor = (rank: number) => ["#e41a1c", "#00bfff", "#ffbf00", "#bb66ff", "#00dd77"][rank % 5];
export type Stage = "before" | "after";
export function stageFrame(candidate: any, stage: Stage): number[][] | null {
  if (stage === "before") return candidate.T_camera_native_grasp || candidate.T_camera_grasp_before_alignment || candidate.T_camera_grasp;
  if (candidate.vertical_alignment?.applied) return candidate.T_camera_native_grasp_aligned || candidate.T_camera_grasp_after_alignment;
  // The reference online service only aligns geometric grasps.
  if (candidate.vertical_alignment?.status === "backend_not_applicable") return stageFrame(candidate, "before");
  return null;
}
/** Contact-centre frames for numeric poses and the selected alignment marker. */
export function contactFrame(candidate: any, stage: Stage): number[][] | null {
  if (stage === "before") return candidate.T_camera_grasp_before_alignment
    || (!candidate.vertical_alignment?.applied ? candidate.T_camera_grasp : null);
  if (candidate.vertical_alignment?.applied) return candidate.T_camera_grasp_after_alignment || null;
  if (candidate.vertical_alignment?.status === "backend_not_applicable") return contactFrame(candidate, "before");
  return null;
}
export function wirePoints(candidate: any, gripper: any): THREE.Vector3[] {
  if (candidate.T_camera_native_grasp) {
    // Byte-for-byte coordinates from offline_grasp/preview.py wire_points.
    // Algorithm glyph only: these dimensions do not describe the robot tool.
    const half = Number(candidate.width_m) / 2;
    const [palm, tip] = candidate.backend === "contact_graspnet" ? [.0584, .1053] : [.05, .09];
    return [[0, 0, 0], [0, 0, palm], [-half, 0, palm], [-half, 0, tip],
      [-half, 0, palm], [half, 0, palm], [half, 0, tip]].map((p) => new THREE.Vector3(...p));
  }
  // Common protocol: origin is closing centre, X closing, Z approach.
  // Show measured fingers only; never borrow Panda/URDF dimensions.
  const half = Number(candidate.width_m) / 2, depth = Number(gripper?.finger_depth_m);
  if (!Number.isFinite(half) || !Number.isFinite(depth) || depth <= 0) return [];
  return [[-half, 0, 0], [-half, 0, -depth], [half, 0, -depth], [half, 0, 0]]
    .map((point) => new THREE.Vector3(...point));
}
function dispose(group: THREE.Object3D) {
  group.traverse((item: any) => { item.geometry?.dispose();
    const materials = Array.isArray(item.material) ? item.material : [item.material];
    materials.forEach((material: any) => material?.map?.dispose());
    if (Array.isArray(item.material)) item.material.forEach((value: any) => value.dispose());
    else item.material?.dispose(); });
  group.clear();
}
function pointCloud(snapshot: any) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(snapshot.cloud.xyz_m.flat(), 3));
  geometry.setAttribute("color", new THREE.Float32BufferAttribute(snapshot.cloud.rgb.flat().map((v: number) => v / 255), 3));
  return new THREE.Points(geometry, new THREE.PointsMaterial({ size: .003, vertexColors: true }));
}
function candidatePose(candidate: any, gripper: any, selected: boolean, frame: number[][], color: string, rank: number) {
  const group = new THREE.Group(); group.matrixAutoUpdate = false; group.matrix.copy(matrix(frame));
  group.add(new THREE.AxesHelper(selected ? .065 : .03));
  const points = wirePoints(candidate, gripper);
  if (points.length) group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),
    new THREE.LineBasicMaterial({ color })));
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
      this.message("点云为相机光学系；对齐前后视角同步。拖动旋转、滚轮缩放。布局显示所选臂的采集姿态、点云和候选；对齐后接触中心的红 X 为闭合方向，绿 Y，蓝 Z 为接近方向。线框为算法示意尺寸。");
    } catch (error) { this.message(`WebGL 渲染失败：${String(error)}`); }
  }
  private describe(view: any) {
    view.canvas.dataset.view = JSON.stringify({ position: view.camera.position.toArray(), target: view.controls.target.toArray() });
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
    const bounds = new THREE.Box3();
    const active = this.robots.get(this.snapshot.arm);
    if (active) bounds.expandByObject(active.base);
    bounds.expandByObject(view.data);
    if (bounds.isEmpty()) return;
    const sphere = bounds.getBoundingSphere(new THREE.Sphere());
    const vertical = THREE.MathUtils.degToRad(view.camera.fov) / 2;
    const horizontal = Math.atan(Math.tan(vertical) * view.camera.aspect);
    const distance = Math.max(.3, sphere.radius) / Math.sin(Math.min(vertical, horizontal)) * 1.12;
    view.controls.target.copy(sphere.center);
    view.camera.position.copy(sphere.center).add(new THREE.Vector3(1, -1.4, .9).normalize().multiplyScalar(distance));
    view.controls.update(); view.renderer.render(view.scene, view.camera); this.describe(view);
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
        optical.data.add(pointCloud(snapshot));
        const rendered: string[] = [];
        snapshot.candidates.forEach((candidate: any, rank: number) => {
          const frame = stageFrame(candidate, stage);
          if (frame && this.visible.has(candidate.candidate_id)) {
            optical.data.add(candidatePose(candidate, snapshot.gripper, candidate.candidate_id === selected, frame, rankColor(rank), rank));
            rendered.push(candidate.candidate_id);
          }
        });
        optical.canvas.dataset.candidates = rendered.join(",");
      }
      const world = this.views[2], config = this.layout?.robots.find((robot: any) => robot.id === snapshot.arm);
      if (world && config && snapshot.T_base_camera) {
        const base = new THREE.Group(); base.position.set(config.transform.x, config.transform.y, config.transform.z);
        base.rotation.set(config.transform.roll, config.transform.pitch, config.transform.yaw, "ZYX");
        const camera = new THREE.Group(); camera.matrixAutoUpdate = false; camera.matrix.copy(matrix(snapshot.T_base_camera));
        camera.add(pointCloud(snapshot), new THREE.AxesHelper(.10)); base.add(camera);
        const rendered: string[] = [];
        snapshot.candidates.forEach((candidate: any, rank: number) => {
          const native = candidate.vertical_alignment?.applied ? stageFrame(candidate, "after") : stageFrame(candidate, "before");
          if (native && this.visible.has(candidate.candidate_id)) {
            const frame = matrix(snapshot.T_base_camera).multiply(matrix(native));
            const rows = Array.from({ length: 4 }, (_, r) => Array.from({ length: 4 }, (_, c) => frame.elements[c * 4 + r]));
            base.add(candidatePose(candidate, snapshot.gripper, candidate.candidate_id === selected, rows, rankColor(rank), rank));
            rendered.push(candidate.candidate_id);
            const contact = contactFrame(candidate, "after");
            if (candidate.candidate_id === selected && candidate.vertical_alignment?.applied && contact) {
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
      view.controls.update(); view.renderer.render(view.scene, view.camera); this.describe(view);
    });
    if (newObservation) this.resetLayoutView();
  }
}
