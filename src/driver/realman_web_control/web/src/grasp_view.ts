/** Review geometry is expressed in metres. Matrices map child -> parent. */
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import URDFLoader from "urdf-loader";

export const matrix = (rows: number[][]) => new THREE.Matrix4().set(...rows.flat());
export function wirePoints(candidate: any, gripper: any): THREE.Vector3[] {
  // Common protocol: origin is closing centre, X closing, Z approach.
  // Show measured fingers only; never borrow Panda/URDF dimensions.
  const half = Number(candidate.width_m) / 2, depth = Number(gripper?.finger_depth_m);
  if (!Number.isFinite(half) || !Number.isFinite(depth) || depth <= 0) return [];
  return [[-half, 0, 0], [-half, 0, -depth], [half, 0, -depth], [half, 0, 0]]
    .map((point) => new THREE.Vector3(...point));
}
function dispose(group: THREE.Object3D) {
  group.traverse((item: any) => { item.geometry?.dispose();
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
function candidatePose(candidate: any, gripper: any, selected: boolean, frame: number[][]) {
  const group = new THREE.Group(); group.matrixAutoUpdate = false; group.matrix.copy(matrix(frame));
  group.add(new THREE.AxesHelper(selected ? .065 : .03));
  const points = wirePoints(candidate, gripper);
  if (points.length) group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),
    new THREE.LineBasicMaterial({ color: selected ? 0xffcc55 : 0x55d4a0 })));
  return group;
}

/** Two independently orbitable views: optical RGB-D and calibrated fleet. */
export class GraspView {
  private views: any[] = [];
  private snapshot: any;
  private selected = "";
  private layout: any;
  private robots = new Map<string, any>();
  private loadGeneration = 0;
  constructor(cloudCanvas: HTMLCanvasElement, fleetCanvas: HTMLCanvasElement,
    private message: (text: string) => void) {
    try {
      for (const [index, canvas] of [cloudCanvas, fleetCanvas].entries()) {
        const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
        renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
        const scene = new THREE.Scene(); scene.background = new THREE.Color(0x172126);
        const camera = new THREE.PerspectiveCamera(48, 1, .005, 30);
        camera.up.set(0, index === 0 ? -1 : 0, index === 0 ? 0 : 1);
        camera.position.set(index === 0 ? .3 : 1.8, index === 0 ? -.2 : -2, index === 0 ? -.3 : 1.5);
        const controls = new OrbitControls(camera, canvas); controls.target.set(0, 0, index === 0 ? .5 : .4);
        const data = new THREE.Group(), fleet = new THREE.Group(); scene.add(data, fleet);
        scene.add(new THREE.HemisphereLight(0xffffff, 0x647579, 3));
        const light = new THREE.DirectionalLight(0xffffff, 3); light.position.set(2, -3, 4); scene.add(light);
        scene.add(new THREE.AxesHelper(.15));
        if (index === 1) { const grid = new THREE.GridHelper(3, 20); grid.rotation.x = Math.PI / 2; scene.add(grid); }
        const resize = () => { const box = canvas.getBoundingClientRect();
          renderer.setSize(Math.max(1, box.width), Math.max(1, box.height), false);
          camera.aspect = Math.max(1, box.width) / Math.max(1, box.height); camera.updateProjectionMatrix(); };
        new ResizeObserver(resize).observe(canvas); resize();
        const view = { renderer, scene, camera, controls, data, fleet }; this.views.push(view);
        controls.addEventListener("change", () => renderer.render(scene, camera));
        renderer.render(scene, camera);
      }
      this.message("点云为相机光学系；布局中仅所选臂显示采集时的关节姿态，其他臂仅显示基座。");
    } catch (error) { this.message(`WebGL 渲染失败：${String(error)}`); }
  }
  async setLayout(layout: any) {
    this.layout = layout; const generation = ++this.loadGeneration;
    const view = this.views[1]; if (!view) return;
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
      this.update(this.snapshot, this.selected);
    } catch (error) { this.message(`机械臂模型加载失败：${String(error)}`); }
  }
  update(snapshot: any, selected: string) {
    this.snapshot = snapshot; this.selected = selected;
    for (const view of this.views) dispose(view.data);
    for (const [arm, { robot }] of this.robots) {
      robot.visible = arm === snapshot?.arm;
      if (robot.visible) robot.setJointValues(Object.fromEntries(snapshot.seed_joint_degrees.map((value: number, i: number) => [`joint_${i + 1}`, value * Math.PI / 180])));
    }
    if (snapshot) {
      const optical = this.views[0];
      if (optical) {
        optical.data.add(pointCloud(snapshot));
        for (const candidate of snapshot.candidates) optical.data.add(candidatePose(candidate, snapshot.gripper,
          candidate.candidate_id === selected, candidate.T_camera_grasp));
      }
      const world = this.views[1], config = this.layout?.robots.find((robot: any) => robot.id === snapshot.arm);
      if (world && config && snapshot.T_base_camera) {
        const base = new THREE.Group(); base.position.set(config.transform.x, config.transform.y, config.transform.z);
        base.rotation.set(config.transform.roll, config.transform.pitch, config.transform.yaw, "ZYX");
        const camera = new THREE.Group(); camera.matrixAutoUpdate = false; camera.matrix.copy(matrix(snapshot.T_base_camera));
        camera.add(pointCloud(snapshot), new THREE.AxesHelper(.10)); base.add(camera);
        for (const candidate of snapshot.candidates) if (candidate.T_base_grasp) base.add(candidatePose(candidate,
          snapshot.gripper, candidate.candidate_id === selected, candidate.T_base_grasp));
        world.data.add(base);
      }
    }
    this.views.forEach(({ renderer, scene, camera, controls }) => { controls.update(); renderer.render(scene, camera); });
  }
}
