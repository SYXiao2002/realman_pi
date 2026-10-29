import "./theme.css";
import "./grasp.css";
import { GraspView } from "./grasp_view";
import { validSnapshot, newerSnapshot } from "./grasp_state";

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `
  <header class="grasp-topbar"><div><span class="eyebrow">REALMAN / GRASP PLAN</span><h1>抓取规划可视化</h1></div><span class="spacer"></span><span id="connection" class="status-pill offline">OFFLINE</span><a class="button ghost" href="/">控制台</a><a class="button ghost" href="/grasp.html">离线评估</a></header>
  <main class="plan-viewer">
    <section class="panel grasp-panel"><div class="grasp-layout-heading"><div><h2>采集姿态、候选与工作区</h2><p class="hint">彩色点云由采集 RGB 与注册深度逐像素反投影，按拍照时外参显示在基座系。</p></div><div class="layout-tools"><label for="layout-point-size">点大小 <input id="layout-point-size" type="range" min="1" max="8" value="3.5" step="0.5"><output id="layout-point-size-value">3.5 px</output></label><span id="state" class="grasp-state">等待行为树规划</span></div></div><div class="pose-visibility-controls"><label>Pose 显示范围<select id="pose-visibility-mode" disabled><option value="pose">单个 Pose</option><option value="instance">单个实例全部 Pose</option><option value="all">所有实例 Pose</option></select></label><label>显示目标<select id="pose-visibility-target" disabled></select></label></div><canvas id="layout" class="layout-canvas" aria-label="机械臂、RGB 反投影点云、抓取候选与工作区边界"></canvas></section>
    <section class="panel grasp-panel"><h2>候选规划结果</h2><p class="hint">几何候选 ID 格式为“分割实例 ID:该实例内序号”（从 0 开始），如 8:1 表示实例 8 的第 2 个候选。</p><div class="grasp-table-wrap"><table class="grasp-candidates"><thead><tr><th>候选</th><th>分数</th><th>接触中心 XYZ / m</th><th>结果</th><th>拒绝原因</th></tr></thead><tbody id="candidates"><tr><td colspan="5">等待 plan_grasp 结果</td></tr></tbody></table></div><p id="workspace" class="hint"></p></section>
    <section class="panel grasp-panel"><h2>规划诊断</h2><pre id="diagnostics" class="json-view">等待 plan_grasp 结果</pre></section>
  </main>`;

const $ = <T extends HTMLElement>(selector: string) => document.querySelector<T>(selector)!;
const opticalBefore = document.createElement("canvas"), opticalAfter = document.createElement("canvas");
const viewer = new GraspView(opticalBefore, opticalAfter, $("#layout"),
  (message) => { $("#state").textContent = message; });
const pointSize = document.querySelector<HTMLInputElement>("#layout-point-size")!;
const pointSizeValue = document.querySelector<HTMLOutputElement>("#layout-point-size-value")!;
const visibilityModeSelect = document.querySelector<HTMLSelectElement>("#pose-visibility-mode")!;
const visibilityTargetSelect = document.querySelector<HTMLSelectElement>("#pose-visibility-target")!;
pointSize.addEventListener("input", () => { pointSizeValue.value = `${pointSize.value} px`; viewer.setPointSize(Number(pointSize.value)); });
let latest: any;
let socket: WebSocket;
type VisibilityMode = "pose" | "instance" | "all";
let visibilityMode: VisibilityMode = "pose", visibilityTarget = "";

function objectId(candidate: any) { return String(candidate.object_id ?? candidate.candidate_id.split(":", 1)[0]); }
function syncVisibilityControls(snapshot: any) {
  visibilityModeSelect.value = visibilityMode;
  visibilityModeSelect.disabled = !snapshot.candidates.length;
  visibilityTargetSelect.replaceChildren();
  if (visibilityMode === "pose") {
    snapshot.candidates.forEach((candidate: any, rank: number) => visibilityTargetSelect.add(new Option(`#${rank + 1} · ${candidate.candidate_id}`, candidate.candidate_id)));
    if (!snapshot.candidates.some((candidate: any) => candidate.candidate_id === visibilityTarget)) visibilityTarget = snapshot.plan?.candidate_id || snapshot.candidates[0]?.candidate_id || "";
  } else if (visibilityMode === "instance") {
    const groups = new Map<string, any[]>();
    snapshot.candidates.forEach((candidate: any) => groups.set(objectId(candidate), [...(groups.get(objectId(candidate)) || []), candidate]));
    groups.forEach((group, id) => visibilityTargetSelect.add(new Option(`实例 ${id} · ${group.length} 个 Pose`, id)));
    if (!groups.has(visibilityTarget)) visibilityTarget = groups.keys().next().value ?? "";
  } else {
    visibilityTargetSelect.add(new Option("所有实例", "all")); visibilityTarget = "all";
  }
  visibilityTargetSelect.disabled = visibilityMode === "all" || !snapshot.candidates.length;
  visibilityTargetSelect.value = visibilityTarget;
}
function visibleIds(snapshot: any) {
  if (visibilityMode === "pose") return visibilityTarget ? new Set([visibilityTarget]) : new Set<string>();
  if (visibilityMode === "all") return new Set(snapshot.candidates.map((candidate: any) => candidate.candidate_id));
  return new Set(snapshot.candidates.filter((candidate: any) => objectId(candidate) === visibilityTarget)
    .map((candidate: any) => candidate.candidate_id));
}

function render(snapshot: any) {
  latest = snapshot;
  syncVisibilityControls(snapshot);
  viewer.update(snapshot, snapshot.plan?.candidate_id || "",
    visibleIds(snapshot));
  const cloudStatus = snapshot.cloud.workspace_crop_status === "applied"
    ? `workspace 点云 ${snapshot.cloud.workspace_point_count} 点`
    : snapshot.cloud.workspace_crop_status === "missing_base_camera" ? "缺少采集外参，workspace 点云为空"
      : snapshot.cloud.workspace_crop_status === "invalid_workspace" ? "workspace 无效，点云为空"
        : "旧快照，workspace 裁切状态未知";
  $("#state").textContent = `${snapshot.profile_name} · ${snapshot.phase} · ${cloudStatus}${snapshot.detail ? ` · ${snapshot.detail}` : ""}`;
  $("#workspace").textContent = `绿色线框 · 工作区 / m：min ${snapshot.workspace_m[0].map((v: number) => v.toFixed(3)).join(", ")} · max ${snapshot.workspace_m[1].map((v: number) => v.toFixed(3)).join(", ")} · ${snapshot.base_frame}`;
  const rows = $("#candidates"); rows.replaceChildren();
  for (const candidate of snapshot.candidates) {
    const row = document.createElement("tr");
    row.className = "plan-candidate-row";
    row.dataset.collisionStatus = candidate.collision?.status || "unknown";
    row.dataset.selected = String(visibilityMode === "pose" && visibilityTarget === candidate.candidate_id);
    const xyz = candidate.T_base_grasp?.slice(0, 3).map((axis: number[]) => axis[3].toFixed(3)).join(", ") || "不可用";
    const collision = candidate.collision;
    const collisionText = collision ? `${collision.status} · 间隙 ${collision.min_clearance_m == null ? "—" : (collision.min_clearance_m * 1000).toFixed(1)} mm · 穿透 ${collision.max_penetration_m == null ? "—" : (collision.max_penetration_m * 1000).toFixed(1)} mm · ${collision.collision_points ?? 0}/${collision.near_points ?? 0} 点 · ${collision.worst_stage || "—"}` : "未执行碰撞评估";
    const result = collision?.blocking === true ? `阻断 · ${collisionText}`
      : `${candidate.planning_status === "selected" ? "通过 · 选中"
        : candidate.planning_status === "rejected" ? "拒绝" : "候选"} · ${collisionText}`;
    for (const value of [candidate.candidate_id, Number(candidate.score).toFixed(3), xyz, result, candidate.planning_reason || collisionText || "—"]) {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    }
    row.addEventListener("click", () => {
      visibilityMode = "pose"; visibilityTarget = candidate.candidate_id; syncVisibilityControls(snapshot); render(snapshot);
    });
    rows.append(row);
  }
  if (!snapshot.candidates.length) {
    const row = document.createElement("tr"), cell = document.createElement("td");
    cell.colSpan = 5; cell.textContent = "本次推理没有产生候选"; row.append(cell); rows.append(row);
  }
  $("#diagnostics").textContent = JSON.stringify({ phase: snapshot.phase, detail: snapshot.detail,
    plan: snapshot.plan, planning_rejections: snapshot.planning_rejections,
    workspace_m: snapshot.workspace_m, capture_timestamp_ns: snapshot.capture_timestamp_ns }, null, 2);
}

visibilityModeSelect.addEventListener("change", () => { visibilityMode = visibilityModeSelect.value as VisibilityMode; syncVisibilityControls(latest || { candidates: [] }); if (latest) render(latest); });
visibilityTargetSelect.addEventListener("change", () => { visibilityTarget = visibilityTargetSelect.value; if (latest) render(latest); });

function connect() {
  socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.addEventListener("message", (event) => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === "hello") { $("#connection").textContent = "CONNECTED"; $("#connection").className = "status-pill ready"; }
      if (message.type === "grasp_evaluation" && message.source === "behavior_tree"
        && validSnapshot(message) && newerSnapshot(message, latest)) render(message);
    } catch { $("#state").textContent = "收到无效消息"; }
  });
  socket.addEventListener("close", () => {
    $("#connection").textContent = "OFFLINE"; $("#connection").className = "status-pill offline";
    window.setTimeout(connect, 2000);
  });
}

fetch("/api/layout").then((response) => { if (!response.ok) throw new Error("layout unavailable"); return response.json(); })
  .then((layout) => viewer.setLayout(layout)).catch((error) => { $("#state").textContent = `布局不可用：${String(error)}`; });
connect();
