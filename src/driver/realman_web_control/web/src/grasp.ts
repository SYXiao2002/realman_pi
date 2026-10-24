import "./theme.css";
import "./grasp.css";
import { Quaternion } from "three";
import { GraspView, matrix, wirePoints, stageFrame, contactFrame, rankColor, type Stage } from "./grasp_view";
import { validSnapshot, newerSnapshot } from "./grasp_state";
import { GraspMotion } from "./grasp_motion";

type EventMessage = Record<string, any> & { type: string };
type Candidate = Record<string, any> & { candidate_id: string; T_camera_grasp: number[][]; T_base_grasp?: number[][] | null };

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `
  <header class="grasp-topbar"><div><span class="eyebrow">REALMAN / GRASP REVIEW</span><h1>Grasp 离线评估</h1></div><span class="spacer"></span><span id="connection" class="status-pill offline">OFFLINE</span><a class="button ghost" href="/">控制台</a><a class="button ghost" href="/calibration.html">标定</a></header>
  <main class="grasp-workspace">
      <section class="panel grasp-panel grasp-wide"><h2>评估上下文</h2><div class="grasp-context">
        <label>机械臂<select id="profile"><option value="right_wrist">右腕 / r</option><option value="left_wrist">左腕 / l</option></select></label>
        <label>目标类别<select id="target"><option value="eu">EU 插头</option><option value="uk">UK 插头</option><option value="adapter">适配器</option><option value="router">路由器</option><option value="line">网线</option></select></label>
        <label>Policy backend<select id="backend"><option value="geometry">几何</option><option value="contact_graspnet">Contact-GraspNet</option></select></label>
        <label>CGN 点云范围<select id="cgn-context"><option value="mask">mask</option><option value="box">box</option><option value="full">full</option></select></label>
        <button id="evaluate" class="button primary" type="button">采集并评估</button>
      </div><div class="grasp-actions"><span class="grasp-readonly">采集与选择候选不会运动；在基座系 MOVEL 面板点击发送后执行直线运动。</span><span id="state" class="grasp-state">等待 bridge 观测</span></div></section>
      <section class="panel grasp-panel grasp-wide"><div class="grasp-layout-heading"><h2>采集时机械臂布局</h2><button id="fit-layout" class="button ghost" type="button">重置取景</button></div><canvas id="layout" class="layout-canvas" aria-label="采集时机械臂与对齐后抓取位姿"></canvas><p id="viewer-status" class="hint"></p>
        <div class="grasp-pose-heading"><h3>所选候选 · 接触中心 Pose</h3><label>候选<select id="pose-candidate" disabled><option value="">选择候选</option></select></label></div><p id="pose-status" class="hint" role="status">等待评估结果</p>
        <div class="grasp-poses"><section class="grasp-pose"><h3>对齐前</h3><div id="pose-before">—</div></section><section class="grasp-pose"><h3>对齐后</h3><div id="pose-after">—</div></section></div>
      </section>
      <section id="grasp-movel" class="panel grasp-panel grasp-wide" aria-label="基座系直线运动"></section>
    <div class="grasp-main">
      <section class="panel grasp-panel"><h2>腕部相机观测</h2><div class="grasp-images"><figure class="grasp-image"><div class="empty">等待 RGB</div><img id="rgb" alt="腕部相机 RGB" hidden><figcaption>RGB</figcaption></figure><figure class="grasp-image"><div class="empty">等待深度</div><img id="depth" alt="注册深度可视化" hidden><figcaption>深度 / 米制范围映射</figcaption></figure><figure class="grasp-image"><div class="empty">等待候选投影</div><img id="overlay" alt="Policy 候选投影" hidden><figcaption>候选投影</figcaption></figure><figure class="grasp-image"><div class="empty">等待实例分割</div><img id="instances" alt="原算法实例分割" hidden><figcaption>原算法实例分割</figcaption></figure></div></section>
      <section class="panel grasp-panel"><h2>生成后竖直对齐 · 前后对照</h2><p id="alignment-status" class="hint" role="status">等待评估结果</p><div class="grasp-compare">
        <div><h3>对齐前 · 原始生成</h3><figure class="grasp-image"><canvas id="projection" aria-label="对齐前 RGB 线框投影"></canvas><figcaption>对齐前 / RGB</figcaption></figure><figure class="grasp-image"><canvas id="cloud-view" aria-label="对齐前 RGB-D 相机系点云"></canvas><figcaption>对齐前 / 点云</figcaption></figure></div>
        <div><h3>对齐后 · 基座 −Z</h3><figure class="grasp-image"><canvas id="projection-after" aria-label="对齐后 RGB 线框投影"></canvas><figcaption>对齐后 / RGB</figcaption></figure><figure class="grasp-image"><canvas id="cloud-view-after" aria-label="对齐后 RGB-D 相机系点云"></canvas><figcaption>对齐后 / 点云</figcaption></figure></div>
        </div><p class="hint">同步旋转与缩放 · 默认 Top 5 · 两侧同编号同色。线框沿用原离线预览的算法示意尺寸，不代表实测夹爪。</p><div id="candidate-legend" class="candidate-legend" aria-label="候选显示开关"></div></section>
      <section class="panel grasp-panel"><h2>Policy 候选</h2><div class="grasp-table-wrap"><table class="grasp-candidates"><thead><tr><th>候选</th><th>类别</th><th>分数</th><th>宽度</th><th>相机系 XYZ</th><th>基座系 XYZ</th></tr></thead><tbody id="candidate-rows"><tr><td colspan="6">等待评估结果</td></tr></tbody></table></div></section>
    </div>
    <aside class="grasp-side">
      <section class="panel grasp-panel"><h2>采集信息与 readiness</h2><dl id="details" class="grasp-details"><div><dt>Profile</dt><dd>—</dd></div></dl><div id="readiness" class="readiness-list" role="status">等待 readiness</div></section>
      <section class="panel grasp-panel"><h2>位姿与评估诊断</h2><pre id="plan" class="json-view">尚无评估结果</pre></section>
      <section class="panel grasp-panel"><h2>人工复核</h2><div class="review-form"><label>候选<select id="review-candidate" disabled><option value="">选择候选</option></select></label><label>复核对象<select id="review-stage"><option value="both">前后对照</option><option value="before">对齐前</option><option value="after">对齐后</option></select></label><label>判定<select id="verdict"><option value="uncertain">待定</option><option value="accepted">接受</option><option value="rejected">拒绝</option></select></label><label>备注<textarea id="notes" placeholder="记录抓取姿态、遮挡或失败原因"></textarea></label><div class="grasp-actions"><button id="save-review" class="button secondary" type="button" disabled>保存复核</button><button id="export-review" class="button ghost" type="button" disabled>导出 JSON</button><button id="import-review" class="button ghost" type="button">导入 JSON</button><input id="review-file" type="file" accept="application/json" hidden></div><span id="review-status" class="grasp-state"></span></div></section>
    </aside>
  </main>`;

const $ = <T extends HTMLElement>(selector: string) => document.querySelector<T>(selector)!;
const profile = $<HTMLSelectElement>("#profile"), target = $<HTMLSelectElement>("#target");
const backend = $<HTMLSelectElement>("#backend"), reviewCandidate = $<HTMLSelectElement>("#review-candidate");
const cgnContext = $<HTMLSelectElement>("#cgn-context");
const poseCandidate = $<HTMLSelectElement>("#pose-candidate");
const verdict = $<HTMLSelectElement>("#verdict"), notes = $<HTMLTextAreaElement>("#notes");
const evaluate = $<HTMLButtonElement>("#evaluate"), rows = $("#candidate-rows");
let socket: WebSocket | undefined, observation: EventMessage | undefined;
let candidates: Candidate[] = [], selectedCandidate = "", pendingRequest = "", offline = false;
let visibleCandidates = new Set<string>();
let profileArms: Record<string, string> = { right_wrist: "r", left_wrist: "l" };
const graspMotion = new GraspMotion($<HTMLElement>("#grasp-movel"), () => socket);
const viewer = new GraspView($<HTMLCanvasElement>("#cloud-view"), $<HTMLCanvasElement>("#cloud-view-after"), $<HTMLCanvasElement>("#layout"),
  (message) => { $("#viewer-status").textContent = message; });
const phaseOrder: Record<string, number> = { captured: 0, inferred: 1, rejected: 1 };
const storageKey = () => `grasp-review/v1:${observation?.profile_id}:${observation?.observation_id}`;
function status(message: string, level = "") { $("#state").textContent = message; $("#state").dataset.level = level; }
function setPending(id: string) { pendingRequest = id; evaluate.disabled = !!id; }

function clearObservation() {
  observation = undefined; candidates = []; selectedCandidate = ""; viewer.update(undefined, "");
  visibleCandidates.clear(); $("#candidate-legend").replaceChildren();
  $("#alignment-status").textContent = "等待评估结果";
  $("#readiness").textContent = "等待 readiness";
  for (const kind of ["rgb", "depth", "overlay", "instances"]) { const image = $<HTMLImageElement>(`#${kind}`);
    image.hidden = true; image.onload = null; image.removeAttribute("src"); image.previousElementSibling?.removeAttribute("hidden"); }
  $("#details").replaceChildren(); $("#plan").textContent = "尚无评估结果";
  verdict.value = "uncertain"; notes.value = ""; $<HTMLSelectElement>("#review-stage").value = "both"; $("#review-status").textContent = "";
  reviewCandidate.replaceChildren(new Option("选择候选", "")); reviewCandidate.disabled = true;
  poseCandidate.replaceChildren(new Option("选择候选", "")); poseCandidate.disabled = true; renderPoses(); renderMotionTarget();
  $("#save-review").toggleAttribute("disabled", true); $("#export-review").toggleAttribute("disabled", true);
  rows.replaceChildren(); drawProjection();
}
function drawProjection() {
  for (const [stage, id] of [["before", "#projection"], ["after", "#projection-after"]] as [Stage, string][]) {
    const canvas = $<HTMLCanvasElement>(id), box = canvas.getBoundingClientRect();
    const ratio = devicePixelRatio || 1; canvas.width = Math.max(1, box.width * ratio); canvas.height = Math.max(1, box.height * ratio);
    const ctx = canvas.getContext("2d")!; ctx.scale(ratio, ratio); ctx.fillStyle = "#172126"; ctx.fillRect(0, 0, box.width, box.height);
    const image = $<HTMLImageElement>("#rgb");
    canvas.dataset.candidates = "";
    if (!observation || image.hidden || !image.complete || !image.naturalWidth) continue;
    // Preserve the camera aspect ratio so pixel overlays cannot shift or stretch.
    const scale = Math.min(box.width / observation.width, box.height / observation.height);
    const dx = (box.width - observation.width * scale) / 2, dy = (box.height - observation.height * scale) / 2;
    ctx.drawImage(image, dx, dy, observation.width * scale, observation.height * scale);
    const K = observation.K;
    const rendered: string[] = [];
    for (const [rank, item] of candidates.entries()) {
      const frame = stageFrame(item, stage);
      if (!frame || !visibleCandidates.has(item.candidate_id)) continue;
      const T = matrix(frame), points = wirePoints(item, observation.gripper).map((p) => p.applyMatrix4(T));
      const origin = { x: frame[0][3], y: frame[1][3], z: frame[2][3] };
      const project = (p: typeof origin) => [dx + (K[0][0] * p.x / p.z + K[0][2]) * scale,
        dy + (K[1][1] * p.y / p.z + K[1][2]) * scale];
      ctx.strokeStyle = rankColor(rank); ctx.fillStyle = ctx.strokeStyle; ctx.lineWidth = item.candidate_id === selectedCandidate ? 3 : 2;
      if (points.length && points.every((p) => p.z > 0)) { ctx.beginPath(); points.forEach((p, i) => { const [x, y] = project(p); if (!i) ctx.moveTo(x, y); else ctx.lineTo(x, y); }); ctx.stroke(); }
      if (origin.z > 0) { const [x, y] = project(origin); ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.stroke(); ctx.font = "bold 12px sans-serif"; ctx.fillText(`#${rank + 1}`, x + 6, y - 6); }
      rendered.push(item.candidate_id);
    }
    canvas.dataset.candidates = rendered.join(",");
  }
}
function renderVisibility() {
  $("#candidate-legend").querySelectorAll<HTMLInputElement>("input").forEach((input) => { input.checked = visibleCandidates.has(input.value); });
  viewer.update(observation, selectedCandidate, visibleCandidates); drawProjection();
}
function selectCandidate(id: string) {
  selectedCandidate = candidates.some((item) => item.candidate_id === id) ? id : "";
  reviewCandidate.value = selectedCandidate;
  poseCandidate.value = selectedCandidate;
  rows.querySelectorAll("tr[data-id]").forEach((row) => row.setAttribute("data-selected", String(row.getAttribute("data-id") === selectedCandidate)));
  $("#save-review").toggleAttribute("disabled", !observation);
  if (selectedCandidate) visibleCandidates.add(selectedCandidate);
  renderVisibility(); renderPoses(); renderDiagnostics(); renderMotionTarget();
}
function baseContactPose(candidate: Candidate, stage: Stage) {
  if (!observation) return null;
  const contact = contactFrame(candidate, stage);
  if (!contact) return null;
  const alignment = candidate.vertical_alignment;
  const frame = stage === "before" ? candidate.T_base_grasp_before_alignment
    : alignment?.applied ? candidate.T_base_grasp_after_alignment : candidate.T_base_grasp;
  // Same frozen capture transform as the pose cards; no live TF or world-layout offsets.
  return frame ? matrix(frame) : observation.T_base_camera
    ? matrix(observation.T_base_camera).multiply(matrix(contact)) : null;
}
function renderMotionTarget() {
  const candidate = candidates.find((item) => item.candidate_id === selectedCandidate);
  if (!candidate || !observation || observation.base_frame !== `${observation.arm}/base_link`) { graspMotion.fill(); return; }
  const T = baseContactPose(candidate, "after");
  const q = T ? new Quaternion().setFromRotationMatrix(T).normalize() : null;
  graspMotion.fill(T && q ? { arm: observation.arm, frame: observation.base_frame, tool: observation.tool_name,
    candidate: candidate.candidate_id, offline,
    values: [T.elements[12], T.elements[13], T.elements[14], q.w, q.x, q.y, q.z] } : undefined);
}
function renderPoses() {
  const candidate = candidates.find((item) => item.candidate_id === selectedCandidate);
  const alignment = candidate?.vertical_alignment;
  $("#pose-status").textContent = !candidate ? "等待选择候选"
    : alignment?.applied ? `${candidate.candidate_id} · 对齐后接近轴为基座 −Z；三维布局标出所选接触中心。XYZ 单位 m，四元数顺序 WXYZ；基座 Pose 可填入 MOVEL，要求 TCP 与抓取坐标系重合。`
    : alignment?.status === "backend_not_applicable" ? `${candidate.candidate_id} · CGN 不执行竖直对齐，沿用原始 Pose。XYZ 单位 m，四元数顺序 WXYZ。`
    : `${candidate.candidate_id} · 对齐后 Pose 不可用。${observation?.base_camera_transform?.detail || "此快照没有对齐结果"}`;
  for (const stage of ["before", "after"] as Stage[]) {
    const container = $(`#pose-${stage}`); container.replaceChildren();
    if (!candidate || !observation) { container.textContent = "—"; continue; }
    const frame = contactFrame(candidate, stage);
    if (!frame) { container.textContent = "不可用 · 未执行竖直对齐或缺少采集变换"; continue; }
    const base = baseContactPose(candidate, stage);
    for (const [name, T] of [[observation.camera_frame, matrix(frame)], [observation.base_frame, base]] as const) {
      const block = document.createElement("dl"); block.className = "grasp-pose-values";
      const title = document.createElement("dt"), value = document.createElement("dd"); title.textContent = name;
      if (T) {
        const q = new Quaternion().setFromRotationMatrix(T).normalize();
        const fixed = (v: number) => (Math.abs(v) < .0000005 ? 0 : v).toFixed(6);
        value.textContent = `XYZ / m  ${[T.elements[12], T.elements[13], T.elements[14]].map(fixed).join(", ")}\nWXYZ     ${[q.w, q.x, q.y, q.z].map(fixed).join(", ")}`;
      } else value.textContent = "不可用 · 缺少采集时 T_base_camera";
      block.append(title, value); container.append(block);
    }
  }
}
function renderDiagnostics() {
  if (!observation) return;
  const candidate = candidates.find((item) => item.candidate_id === selectedCandidate);
  $("#plan").textContent = JSON.stringify({ phase: observation.phase, detail: observation.detail,
    vertical_alignment: candidate?.vertical_alignment,
    T_camera_native_grasp_aligned: candidate?.T_camera_native_grasp_aligned,
    T_camera_grasp_before_alignment: candidate?.T_camera_grasp_before_alignment,
    T_camera_grasp_after_alignment: candidate?.T_camera_grasp_after_alignment,
    T_base_grasp_before_alignment: candidate?.T_base_grasp_before_alignment,
    T_base_grasp_after_alignment: candidate?.T_base_grasp_after_alignment,
    T_camera_native_grasp: candidate?.T_camera_native_grasp, candidate_metrics: candidate?.metrics,
    T_camera_grasp: candidate?.T_camera_grasp, T_base_grasp: candidate?.T_base_grasp,
    T_base_camera: observation.T_base_camera, base_camera_transform: observation.base_camera_transform,
    seed_joint_degrees: observation.seed_joint_degrees,
    depth_stats: observation.depth_stats, server: observation.server_diagnostics,
    gripper: observation.gripper, readiness_problems: observation.readiness_problems,
    evaluation_only: true, ik_checked: false, collision_checked: false }, null, 2);
}
function reviewRecord() { return { schema: "grasp-review/v1", observation_id: observation?.observation_id,
  profile_id: observation?.profile_id, stage: $<HTMLSelectElement>("#review-stage").value, candidate_id: selectedCandidate, verdict: verdict.value,
  notes: notes.value, reviewed_at: new Date().toISOString() }; }
function applyReview(record: any) {
  $<HTMLSelectElement>("#review-stage").value = ["before", "after", "both"].includes(record?.stage) ? record.stage : "both";
  verdict.value = ["accepted", "rejected", "uncertain"].includes(record?.verdict) ? record.verdict : "uncertain";
  notes.value = typeof record?.notes === "string" ? record.notes : "";
  selectCandidate(record?.candidate_id || candidates[0]?.candidate_id || "");
}
function renderSnapshot(message: EventMessage) {
  const previousId = observation?.observation_id, previousSelection = selectedCandidate;
  observation = message; candidates = message.candidates;
  if (previousId !== message.observation_id || !visibleCandidates.size) visibleCandidates = new Set(candidates.slice(0, 5).map((c) => c.candidate_id));
  const alignment = message.server_diagnostics?.vertical_alignment_status;
  $("#alignment-status").textContent = alignment === "applied" ? "已复用原项目竖直对齐：接近轴对齐基座 −Z，接触中心保持不变。"
    : alignment === "backend_not_applicable" ? "Contact-GraspNet 沿用原项目规则，不执行几何竖直对齐；右侧显示原始结果。"
    : alignment === "missing_base_camera" ? `缺少采集时 T_base_camera：${message.base_camera_transform?.detail || "采集变换不可用"}。只能显示对齐前结果，对齐后不可用。`
    : message.phase === "captured" ? "已采集，等待算法结果。" : "此快照没有竖直对齐结果；右侧不可用。";
  $("#candidate-legend").replaceChildren(...candidates.map((item, rank) => {
    const label = document.createElement("label"), input = document.createElement("input"), swatch = document.createElement("span");
    input.type = "checkbox"; input.value = item.candidate_id; input.checked = visibleCandidates.has(item.candidate_id);
    input.setAttribute("aria-label", `显示 #${rank + 1} ${item.candidate_id}`);
    swatch.className = "rank-swatch"; swatch.style.backgroundColor = rankColor(rank);
    input.addEventListener("change", () => { if (input.checked) visibleCandidates.add(item.candidate_id); else visibleCandidates.delete(item.candidate_id); renderVisibility(); });
    label.append(input, swatch, document.createTextNode(`#${rank + 1} · ${item.candidate_id} · object ${item.object_id || '—'} · ${Number(item.score).toFixed(3)} · ${(item.width_m * 1000).toFixed(1)} mm`));
    return label;
  }));
  const values = [["Profile", `${message.profile_name} / ${message.arm}`], ["Target / backend", `${message.target_label} / ${message.backend}`],
    ["Observation", message.observation_id], ["采样时间", new Date(Number(message.capture_timestamp_ns) / 1e6).toLocaleString()],
    ["Camera → base", `${message.camera_frame} → ${message.base_frame}`], ["标定 / model", `${message.calibration_id || "未批准"} / ${message.model_id || "未推理"}`],
    ["Tool / gripper", `${message.tool_name} / ${message.gripper?.id}`], ["状态", "只读 · 未验证 IK / 碰撞"]];
  const skipped = message.server_diagnostics?.skipped_instances;
  if (Array.isArray(skipped) && skipped.length) values.push(["跳过的深度不足实例", skipped.map((item: any) =>
    `#${item.object_id}：${item.valid_points}/${item.minimum_points} 点`).join("；")]);
  if (message.server_diagnostics?.reason === "no_valid_instances") values.push(["候选结果", "所有实例有效深度不足，本帧无可用实例"]);
  $("#details").replaceChildren(...values.map(([name, value]) => { const block = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = String(name); dd.textContent = String(value); block.append(dt, dd); return block; }));
  const readiness = $<HTMLDivElement>("#readiness");
  readiness.replaceChildren();
  const title = document.createElement("strong"); title.textContent = "不可执行 · 离线评估"; readiness.append(title);
  if (message.readiness_problems.length) {
    const list = document.createElement("ul");
    for (const problem of message.readiness_problems) { const item = document.createElement("li"); item.textContent = problem; list.append(item); }
    readiness.append(list);
  } else {
    const note = document.createElement("p"); note.textContent = "配置项已就绪；候选未验证 IK / 碰撞，手动 MOVEL 需另外点击发送。"; readiness.append(note);
  }
  for (const kind of ["rgb", "depth", "overlay", "instances"]) {
    const image = $<HTMLImageElement>(`#${kind}`), id = message.observation_id;
    if (!message.images[kind]) { image.hidden = true; image.onload = null; image.removeAttribute("src"); image.previousElementSibling?.removeAttribute("hidden"); continue; }
    image.hidden = true; image.onload = () => { if (observation?.observation_id !== id) return;
      image.hidden = false; image.previousElementSibling?.setAttribute("hidden", ""); if (kind === "rgb") drawProjection(); };
    image.src = message.images[kind];
  }
  rows.replaceChildren(); reviewCandidate.replaceChildren(new Option("选择候选", "")); poseCandidate.replaceChildren(new Option("选择候选", ""));
  for (const item of candidates) {
    const tr = document.createElement("tr"); tr.dataset.id = item.candidate_id;
    const xyz = (T?: number[][] | null) => T?.slice(0, 3).map((row) => row[3].toFixed(3)).join(", ") || "未标定";
    [item.candidate_id, item.class_name, Number(item.score).toFixed(3), `${(item.width_m * 1000).toFixed(1)} mm`, xyz(item.T_camera_grasp), xyz(item.T_base_grasp)].forEach((value, i) => {
      const td = document.createElement("td"); if (!i) { const button = document.createElement("button"); button.textContent = String(value); button.addEventListener("click", () => selectCandidate(item.candidate_id)); td.append(button); }
      else td.textContent = String(value); tr.append(td);
    }); rows.append(tr); reviewCandidate.add(new Option(item.candidate_id, item.candidate_id)); poseCandidate.add(new Option(item.candidate_id, item.candidate_id));
  }
  if (!candidates.length) { const tr = document.createElement("tr"), td = document.createElement("td"); td.colSpan = 6; td.textContent = "该 observation 没有 policy 候选"; tr.append(td); rows.append(tr); }
  reviewCandidate.disabled = !candidates.length; $("#export-review").toggleAttribute("disabled", false);
  poseCandidate.disabled = !candidates.length;
  if (previousId === message.observation_id) selectCandidate(previousSelection || candidates[0]?.candidate_id || "");
  else { let saved = {}; try { saved = JSON.parse(localStorage.getItem(storageKey()) || "{}"); } catch { /* local annotation is optional */ }
    applyReview(saved); $("#review-status").textContent = ""; }
  status(`${offline ? "离线文件" : "只读观测"} · ${message.phase}${message.detail ? `：${message.detail}` : ""}`, message.phase === "rejected" ? "bad" : "");
}
function handle(message: EventMessage) {
  graspMotion.handle(message);
  if (message.type === "hello") { $("#connection").textContent = "CONNECTED"; $("#connection").className = "status-pill ready"; }
  else if (message.type === "grasp_bridge_status") {
    const profiles = Array.isArray(message.profiles) ? message.profiles.filter((p: any) => typeof p.name === "string" && ["r", "l"].includes(p.arm)) : [];
    if (profiles.length && !offline) { const selected = profile.value; profileArms = Object.fromEntries(profiles.map((p: any) => [p.name, p.arm]));
      profile.replaceChildren(...profiles.map((p: any) => new Option(`${p.name} / ${p.arm.toUpperCase()}`, p.name)));
      profile.value = profileArms[selected] ? selected : message.default_profile || profiles[0].name;
    }
  } else if (message.type === "grasp_evaluation") {
    if (offline || !validSnapshot(message) || message.profile_name !== profile.value || message.arm !== profileArms[profile.value]
      || message.target_label !== target.value || message.backend !== backend.value
      || (message.cgn_context && message.cgn_context !== cgnContext.value)
      || (pendingRequest && message.review_request_id !== pendingRequest) || !newerSnapshot(message, observation)) return;
    renderSnapshot(message);
  } else if (["grasp_plan_result", "grasp_plan_feedback", "error"].includes(message.type) && message.request_id === pendingRequest) {
    if (message.type === "grasp_plan_feedback") status(`${message.feedback?.phase}：${message.feedback?.detail}`);
    else if (message.state !== "submitted") { setPending(""); status(message.success ? "评估完成 · 只读 · 未做 IK / 碰撞检查" : `评估拒绝：${message.message}`, message.success ? "good" : "bad"); }
  }
}
function connect() {
  socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.addEventListener("message", (event) => { try { handle(JSON.parse(event.data)); } catch { status("消息无效", "bad"); } });
  socket.addEventListener("close", () => { $("#connection").textContent = "OFFLINE"; $("#connection").className = "status-pill offline";
    setPending(""); graspMotion.disconnected(); window.setTimeout(connect, 2000); });
}
evaluate.addEventListener("click", () => {
  if (socket?.readyState !== WebSocket.OPEN) { status("WebSocket 未连接", "bad"); return; }
  offline = false; clearObservation(); setPending(`grasp-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`);
  status("正在请求新的 RGB-D observation"); socket.send(JSON.stringify({ type: "plan_grasp", request_id: pendingRequest,
    profile_name: profile.value, target_label: target.value, backend: backend.value, cgn_context: cgnContext.value }));
});
for (const select of [profile, target, backend, cgnContext]) select.addEventListener("change", () => { setPending(""); offline = false; clearObservation(); status("上下文已切换，等待新评估"); });
reviewCandidate.addEventListener("change", () => selectCandidate(reviewCandidate.value));
poseCandidate.addEventListener("change", () => selectCandidate(poseCandidate.value));
$("#fit-layout").addEventListener("click", () => viewer.resetLayoutView());
$("#save-review").addEventListener("click", () => { if (!observation) return; try { localStorage.setItem(storageKey(), JSON.stringify(reviewRecord())); $("#review-status").textContent = "已保存在此浏览器"; } catch { $("#review-status").textContent = "本地存储不可用，请导出 JSON"; } });
$("#export-review").addEventListener("click", () => {
  if (!observation) return; const payload = { schema: "grasp-review/v1", observation, review: reviewRecord() };
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${observation.observation_id}-review.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
const fileInput = $<HTMLInputElement>("#review-file");
$("#import-review").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", async () => {
  const file = fileInput.files?.[0]; if (!file) return;
  try {
    if (file.size > 24 * 1024 * 1024) throw new Error("文件超过 24 MiB");
    const value = JSON.parse(await file.text());
    if (value.schema !== "grasp-review/v1" || !validSnapshot(value.observation)) throw new Error("无效的评估快照");
    const data = value.observation, review = value.review;
    if (review && (review.observation_id !== data.observation_id || review.profile_id !== data.profile_id)) throw new Error("评价与 observation 身份不一致");
    setPending(""); offline = true; clearObservation();
    if (!Array.from(profile.options).some((p) => p.value === data.profile_name)) profile.add(new Option(data.profile_name, data.profile_name));
    cgnContext.value = data.cgn_context || "mask"; profileArms[data.profile_name] = data.arm; profile.value = data.profile_name; target.value = data.target_label; backend.value = data.backend;
    renderSnapshot(data); if (review) applyReview(review); $("#review-status").textContent = "已导入离线评估，可断网审阅";
  } catch (error) { $("#review-status").textContent = `导入失败：${String(error)}`; }
  finally { fileInput.value = ""; }
});
window.addEventListener("resize", drawProjection);
fetch("/api/layout").then((response) => { if (!response.ok) throw new Error("layout unavailable"); return response.json(); })
  .then((layout) => viewer.setLayout(layout)).catch((error) => { $("#viewer-status").textContent = `布局不可用：${String(error)}`; });
drawProjection(); connect();
