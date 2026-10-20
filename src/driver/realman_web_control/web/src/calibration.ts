import "./theme.css";
import "./calibration.css";

type EventMessage = Record<string, any> & { type: string };

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `
  <header class="topbar">
    <div><span class="eyebrow">REALMAN / CALIBRATION</span><h1>ChArUco 手眼标定</h1><p>左右腕依次独立标定 · 默认右臂</p></div>
    <div class="top-actions"><a href="/" class="button ghost">返回控制界面</a><span id="connection" class="status offline">OFFLINE</span></div>
  </header>
  <main class="grid">
    <section class="panel config-panel"><div class="panel-title"><div><span class="eyebrow">BOARD CONFIG</span><h2>标定板配置</h2></div><span id="config-state" class="status">LOADING</span></div><div id="config" class="config-list">读取 config/ros/camera_calibration.yaml…</div></section>
    <section class="panel health-panel"><div class="panel-title"><div><span class="eyebrow">CAMERA INPUT HEALTH</span><h2>当前腕相机输入状态</h2></div><span id="health-state" class="status">WAITING</span></div><div id="camera-health" class="camera-health"><p class="hint">等待标定节点上报图像与 CameraInfo 状态…</p></div></section>
    <section class="panel capture-panel"><div class="panel-title"><div><span class="eyebrow">CAPTURE SESSION</span><h2>保存样本</h2></div><span id="session-state" class="status">NO SESSION</span></div><label>标定机械臂<select id="calibration-arm"><option value="r">RIGHT / r / camera_right</option><option value="l">LEFT / l / camera_left</option></select></label><label>Session ID<input id="session" placeholder="留空则自动新建" /></label><label>历史会话<select id="session-history" disabled><option value="">读取历史会话…</option></select></label><label class="check"><input id="delete-empty-sessions" type="checkbox" checked /> 刷新时清理空会话</label><div class="session-actions"><button id="refresh-sessions" class="button ghost" type="button">刷新历史会话</button><button id="load-session" class="button ghost" type="button" disabled>加载所选会话</button><button id="delete-session" class="button danger" type="button" disabled>删除所选会话</button></div><label class="check"><input id="new-session" type="checkbox" checked /> 新建会话</label><button id="capture" class="button primary" disabled>检测并保存当前臂样本</button><p class="hint">刷新仅清理没有已接受样本的空会话；“删除所选会话”会删除其全部图片、JSON 和求解结果，需确认后执行。</p></section>
    <section class="panel solve-panel"><div class="panel-title"><div><span class="eyebrow">SOLVE</span><h2>执行标定</h2></div><span id="solve-state" class="status">WAITING</span></div><button id="solve" class="button secondary" disabled>执行当前臂手眼标定</button><p class="hint">每臂使用自己的会话，达到样本数和残差阈值后独立求解；采样期间标定板保持不动。另一臂离线不影响当前流程。</p><pre id="result">等待结果…</pre><h2>更新双臂布局</h2><label>左臂已求解 session<select id="layout-left"><option value="">选择左臂会话</option></select></label><label>右臂已求解 session<select id="layout-right"><option value="">选择右臂会话</option></select></label><p class="hint">由你负责选择标定板位置相同的两次会话。写回 three_robots.yaml 并保留 .bak；重新 bringup 后基座 TF 生效。</p><button id="update-layout" class="button secondary" disabled>使用所选会话更新布局</button><p id="layout-result" class="hint">等待选择左右臂会话。</p></section>
    <section class="panel preview-panel"><div class="panel-title"><div><span class="eyebrow">DETECTION PREVIEW</span><h2>最近一次检测画面</h2></div><span id="preview-state" class="status">WAITING</span></div><div id="previews" class="preview-grid"><p class="hint">点击检测后显示当前腕相机画面与 ChArUco 检测结果。</p></div></section>
    <section class="panel log-panel"><div class="panel-title"><div><span class="eyebrow">SERVICE LOG</span><h2>实时反馈</h2></div></div><pre id="log"></pre></section>
  </main>
`;

const connection = document.querySelector<HTMLElement>("#connection")!;
const armSelect = document.querySelector<HTMLSelectElement>("#calibration-arm")!;
const sessionInput = document.querySelector<HTMLInputElement>("#session")!;
const layoutLeft = document.querySelector<HTMLSelectElement>("#layout-left")!;
const layoutRight = document.querySelector<HTMLSelectElement>("#layout-right")!;
const updateLayoutButton = document.querySelector<HTMLButtonElement>("#update-layout")!;
const layoutResult = document.querySelector<HTMLElement>("#layout-result")!;
const sessionHistory = document.querySelector<HTMLSelectElement>("#session-history")!;
const refreshSessionsButton = document.querySelector<HTMLButtonElement>("#refresh-sessions")!;
const loadSessionButton = document.querySelector<HTMLButtonElement>("#load-session")!;
const deleteSessionButton = document.querySelector<HTMLButtonElement>("#delete-session")!;
const deleteEmptySessions = document.querySelector<HTMLInputElement>("#delete-empty-sessions")!;
const newSession = document.querySelector<HTMLInputElement>("#new-session")!;
const captureButton = document.querySelector<HTMLButtonElement>("#capture")!;
const solveButton = document.querySelector<HTMLButtonElement>("#solve")!;
const sessionState = document.querySelector<HTMLElement>("#session-state")!;
const solveState = document.querySelector<HTMLElement>("#solve-state")!;
const result = document.querySelector<HTMLElement>("#result")!;
const log = document.querySelector<HTMLElement>("#log")!;
const previews = document.querySelector<HTMLElement>("#previews")!;
const previewState = document.querySelector<HTMLElement>("#preview-state")!;
const cameraHealth = document.querySelector<HTMLElement>("#camera-health")!;
const healthState = document.querySelector<HTMLElement>("#health-state")!;
let socket: WebSocket | undefined;
let captureRequest = "";
let solveRequest = "";
let layoutRequest = "";
let sessionId = "";
let minimumSamplesPerArm = 0;
let configLoaded = false;
type CalibrationSession = {
  session_id: string;
  created_at: string;
  arm_id: string;
  sample_count: number;
  recoverable: boolean;
  solved: boolean;
};
let calibrationSessions: CalibrationSession[] = [];
let healthInputs: any[] = [];


function requestId(prefix: string) { return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`; }
function writeLog(message: string) { log.textContent = `${new Date().toLocaleTimeString()} ${message}\n${log.textContent}`.slice(0, 12000); }
function canWrite() { return socket?.readyState === WebSocket.OPEN; }
function updateButtons() {
  const busy = Boolean(captureRequest || solveRequest || layoutRequest);
  captureButton.disabled = !canWrite() || !configLoaded || busy;
  solveButton.disabled = !canWrite() || !configLoaded || busy || !sessionId || newSession.checked;
  armSelect.disabled = busy || !configLoaded;
  sessionInput.disabled = busy;
  layoutLeft.disabled = busy;
  layoutRight.disabled = busy;
  updateLayoutButton.disabled = !canWrite() || busy || !layoutLeft.value || !layoutRight.value;
  newSession.disabled = busy;
  sessionHistory.disabled = busy || sessionHistory.options.length <= 1;
  refreshSessionsButton.disabled = busy;
  deleteSessionButton.disabled = busy || !sessionHistory.value;
  loadSessionButton.disabled = busy || !calibrationSessions.find((item) => item.session_id === sessionHistory.value)?.recoverable;
}
function send(message: Record<string, any>) { socket?.send(JSON.stringify(message)); }

function sessionCountText(item: CalibrationSession) {
  return `${item.arm_id ? item.arm_id.toUpperCase() : "旧格式"} ${item.sample_count}/${minimumSamplesPerArm || "?"}`;
}

function renderSessionHistory() {
  const selected = sessionId || sessionHistory.value;
  sessionHistory.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = calibrationSessions.length ? "选择一个历史会话" : "没有历史会话";
  sessionHistory.append(placeholder);
  const visible = calibrationSessions.filter((item) => !item.arm_id || item.arm_id === armSelect.value);
  visible.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.session_id;
    option.textContent = `${item.session_id} · ${sessionCountText(item)} · ${!item.recoverable ? "不可恢复，请重新采样" : item.solved ? "已求解" : "未求解"}`;
    sessionHistory.append(option);
  });
  sessionHistory.disabled = visible.length === 0;
  if (visible.some((item) => item.session_id === selected)) sessionHistory.value = selected;
  loadSessionButton.disabled = !calibrationSessions.find((item) => item.session_id === sessionHistory.value)?.recoverable;
  deleteSessionButton.disabled = !sessionHistory.value;
  for (const [select, arm] of [[layoutLeft, "l"], [layoutRight, "r"]] as const) {
    const previous = select.value;
    select.replaceChildren(new Option(`选择${arm === "l" ? "左" : "右"}臂已求解会话`, ""));
    calibrationSessions.filter((item) => item.arm_id === arm && item.solved).forEach((item) => {
      select.add(new Option(`${item.session_id} · ${item.sample_count} 张`, item.session_id));
    });
    select.value = previous;
  }
  updateButtons();
}

async function refreshSessionHistory(deleteEmpty = false) {
  refreshSessionsButton.disabled = true;
  try {
    const response = await fetch(`/api/calibration/sessions${deleteEmpty ? "?delete_empty=true" : ""}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json();
    calibrationSessions = Array.isArray(body.sessions) ? body.sessions : [];
    renderSessionHistory();
    const deleted = Array.isArray(body.deleted_session_ids) ? body.deleted_session_ids : [];
    writeLog(`已读取 ${calibrationSessions.length} 个历史标定会话${deleted.length ? `；已清理 ${deleted.length} 个空会话` : ""}`);
  } catch (error) {
    calibrationSessions = [];
    renderSessionHistory();
    writeLog(`读取历史会话失败：${String(error)}`);
  } finally {
    updateButtons();
  }
}

function loadSelectedSession() {
  const selected = calibrationSessions.find((item) => item.session_id === sessionHistory.value);
  if (!selected?.recoverable || selected.arm_id !== armSelect.value) return;
  sessionId = selected.session_id;
  sessionInput.value = sessionId;
  newSession.checked = false;
  sessionState.textContent = `LOADED ${sessionCountText(selected)}`;
  solveState.textContent = selected.solved ? "SOLVED" : "READY";
  writeLog(`已加载历史会话 ${sessionId}${selected.solved ? "（已有求解结果）" : ""}`);
  updateButtons();
}

async function deleteSelectedSession() {
  const selected = calibrationSessions.find((item) => item.session_id === sessionHistory.value);
  if (!selected) return;
  if (!window.confirm(`删除 ${selected.session_id}？\n这会永久删除该会话的图片、样本 JSON 和求解结果。`)) return;
  deleteSessionButton.disabled = true;
  try {
    const response = await fetch(`/api/calibration/sessions/${encodeURIComponent(selected.session_id)}`, { method: "DELETE" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    if (sessionId === selected.session_id) {
      sessionId = "";
      sessionInput.value = "";
      sessionState.textContent = "NO SESSION";
      solveState.textContent = "WAITING";
    }
    writeLog(`已删除历史会话 ${selected.session_id}`);
    await refreshSessionHistory(false);
    updateButtons();
  } catch (error) {
    writeLog(`删除历史会话失败：${String(error)}`);
  } finally {
    deleteSessionButton.disabled = !sessionHistory.value;
  }
}

function showPreviews(arms: string[], paths: string[], statuses: string[] = [], messages: string[] = []) {
  previews.replaceChildren();
  const labels: Record<string, string> = { l: "LEFT / l", r: "RIGHT / r" };
  arms.forEach((arm, index) => {
    const figure = document.createElement("figure");
    const path = paths[index];
    if (path) {
      const image = document.createElement("img");
      image.src = path;
      image.alt = `${labels[arm] || arm} 最近一次 ChArUco 检测画面`;
      image.loading = "lazy";
      figure.append(image);
    } else {
      const missing = document.createElement("div");
      missing.className = "preview-missing";
      missing.textContent = "没有可用画面";
      figure.append(missing);
    }
    const caption = document.createElement("figcaption");
    const status = statuses[index] || "unknown";
    caption.innerHTML = `<strong>${labels[arm] || arm} · ${status === "detected" ? "已检测到 ChArUco" : "未检测到 ChArUco"}</strong><span>${messages[index] || ""}</span>`;
    figure.append(caption);
    previews.append(figure);
  });
  const detected = statuses.filter((status) => status === "detected").length;
  previewState.textContent = `${detected}/${arms.length} DETECTED`;
  previewState.className = `status ${detected === arms.length && arms.length > 0 ? "ready" : "offline"}`;
}

function formatSeconds(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 1000).toFixed(0)} ms` : "—";
}

function renderCameraHealth(inputs: any[]) {
  cameraHealth.replaceChildren();
  inputs = inputs.filter((input) => input.arm_id === armSelect.value);
  const labels: Record<string, string> = { l: "LEFT", r: "RIGHT" };
  const rank: Record<string, number> = { healthy: 0, delayed: 1, unsynchronized: 1, stale: 2, missing: 3 };
  let worst = "healthy";
  inputs.forEach((input) => {
    if ((rank[input.status] ?? 3) > (rank[worst] ?? 3)) worst = input.status;
    const card = document.createElement("article");
    card.className = `camera-card ${input.status || "missing"}`;
    card.innerHTML = `<div class="camera-card-title"><strong>${labels[input.arm_id] || input.camera_id}</strong><span>${String(input.status || "missing").toUpperCase()}</span></div><p>${input.message || "unknown camera state"}</p><dl><div><dt>Image</dt><dd>${input.image_received ? "received" : "missing"}</dd></div><div><dt>Info</dt><dd>${input.camera_info_received ? "received" : "missing"}</dd></div><div><dt>帧新鲜度</dt><dd>${formatSeconds(input.image_age_sec)}</dd></div><div><dt>时间戳延迟</dt><dd>${formatSeconds(input.image_timestamp_delay_sec)}</dd></div><div><dt>Image/Info 偏差</dt><dd>${formatSeconds(input.image_camera_info_skew_sec)}</dd></div><div><dt>分辨率</dt><dd>${input.image_width && input.image_height ? `${input.image_width} × ${input.image_height}` : "—"}</dd></div></dl>`;
    cameraHealth.append(card);
  });
  if (!inputs.length) worst = "missing";
  healthState.textContent = worst.toUpperCase();
  healthState.className = `status ${worst === "healthy" ? "ready" : "offline"}`;
}

async function loadConfig() {
  try {
    const config = await fetch("/api/calibration").then((response) => response.json());
    const board = config.board;
    minimumSamplesPerArm = Number(config.sampling.minimum_samples_per_arm);
    const arms: string[] = config.sampling.arm_order.filter((arm: string) =>
      (arm === "l" || arm === "r") && config.robots[arm],
    );
    if (!arms.length || !arms.includes(config.sampling.default_arm)) throw new Error("标定臂配置无效");
    armSelect.replaceChildren(...arms.map((arm) => {
      const option = document.createElement("option");
      option.value = arm;
      option.textContent = `${arm === "r" ? "RIGHT" : "LEFT"} / ${arm} / ${config.robots[arm].camera_id}`;
      return option;
    }));
    armSelect.value = config.sampling.default_arm;
    configLoaded = true;
    renderSessionHistory();
    renderCameraHealth(healthInputs);
    document.querySelector("#config")!.innerHTML = `<div><strong>${board.type.toUpperCase()}</strong> / ${board.dictionary}</div><div>网格：${board.squares_x} × ${board.squares_y}</div><div>方格：${board.square_length_m} m / Marker：${board.marker_length_m} m</div><div>最少角点：${board.minimum_corners}</div><div>每臂最少样本：${config.sampling.minimum_samples_per_arm}</div><div>手眼算法：${config.solver.hand_eye_method}</div>`;
    document.querySelector("#config")!.innerHTML += `<div>外形：${(Number(board.outer_width_m) * 1000).toFixed(0)} × ${(Number(board.outer_height_m) * 1000).toFixed(0)} mm</div><div>图案：${(Number(board.pattern_width_m) * 1000).toFixed(0)} × ${(Number(board.pattern_height_m) * 1000).toFixed(0)} mm</div>`;
    document.querySelector("#config-state")!.textContent = "CONFIG LOADED";
  } catch (error) { writeLog(`读取标定配置失败：${String(error)}`); }
  updateButtons();
}

function handle(message: EventMessage) {
  if (message.type === "hello") {
    connection.textContent = "CONNECTED"; connection.className = "status ready"; updateButtons();
  } else if (message.type === "camera_health") {
    healthInputs = Array.isArray(message.inputs) ? message.inputs : [];
    renderCameraHealth(healthInputs);
  } else if (message.type === "calibration_capture_result") {
    if (!captureRequest || message.request_id !== captureRequest) return;
    captureRequest = "";
    if (message.success) {
      sessionId = message.session_id; sessionInput.value = sessionId; newSession.checked = false;
      sessionState.textContent = `SAMPLES ${message.arm_id.toUpperCase()} ${message.sample_count}/${minimumSamplesPerArm}`;
      showPreviews([message.arm_id || armSelect.value], [message.preview_image_path || ""], [message.detection_status || ""], [message.detection_message || ""]);
      writeLog(`采样成功 ${message.batch_id}：${message.message}`);
      void refreshSessionHistory();
    } else {
      showPreviews([message.arm_id || armSelect.value], [message.latest_image_path || ""], [message.detection_status || ""], [message.detection_message || ""]);
      writeLog(`采样拒绝：${message.message}`);
    }
    updateButtons();
  } else if (message.type === "calibration_solve_result") {
    if (!solveRequest || message.request_id !== solveRequest) return;
    solveRequest = "";
    solveState.textContent = message.success ? "SOLVED" : "FAILED";
    result.textContent = message.result_json ? JSON.stringify(JSON.parse(message.result_json), null, 2) : message.message;
    writeLog(message.success ? `标定完成：${message.message}` : `标定失败：${message.message}`);
    if (message.success) void refreshSessionHistory();
    updateButtons();
  } else if (message.type === "calibration_layout_result") {
    if (!layoutRequest || message.request_id !== layoutRequest) return;
    layoutRequest = "";
    layoutResult.textContent = message.success
      ? `布局已写回，备份：${message.layout_backup_file}。重新 bringup 后基座 TF 生效。`
      : `布局更新失败：${message.message}`;
    writeLog(`${layoutResult.textContent}（左：${message.left_session_id}；右：${message.right_session_id}）`);
    updateButtons();
  } else if (message.type === "error") {
    if (message.request_id === captureRequest) captureRequest = "";
    if (message.request_id === solveRequest) solveRequest = "";
    if (message.request_id === layoutRequest) { layoutRequest = ""; layoutResult.textContent = message.message; }
    writeLog(`${message.code}: ${message.message}`); updateButtons();
  }
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/ws`);
  socket.addEventListener("open", () => { connection.textContent = "CONNECTED"; connection.className = "status ready"; updateButtons(); });
  socket.addEventListener("close", () => {
    captureRequest = ""; solveRequest = ""; layoutRequest = "";
    connection.textContent = "OFFLINE"; connection.className = "status offline";
    updateButtons(); window.setTimeout(connect, 2000);
  });
  socket.addEventListener("message", (event) => { try { handle(JSON.parse(event.data)); } catch { writeLog("收到无法解析的服务器消息"); } });
}

captureButton.addEventListener("click", () => {
  captureRequest = requestId("capture");
  send({ type: "capture_calibration_sample", request_id: captureRequest, session_id: newSession.checked ? "" : sessionInput.value.trim(), start_new_session: newSession.checked, arm_id: armSelect.value });
  writeLog(`正在检测 ${armSelect.value.toUpperCase()} 腕相机的 ChArUco…`); updateButtons();
});
solveButton.addEventListener("click", () => {
  solveRequest = requestId("solve");
  send({ type: "solve_calibration", request_id: solveRequest, session_id: sessionId, arm_id: armSelect.value });
  solveState.textContent = "SOLVING"; writeLog(`正在独立求解 ${armSelect.value.toUpperCase()} 臂手眼标定…`); updateButtons();
});
layoutLeft.addEventListener("change", updateButtons);
layoutRight.addEventListener("change", updateButtons);
updateLayoutButton.addEventListener("click", () => {
  layoutRequest = requestId("layout");
  send({ type: "update_calibration_layout", request_id: layoutRequest, left_session_id: layoutLeft.value, right_session_id: layoutRight.value });
  layoutResult.textContent = "正在使用所选会话更新布局…";
  updateButtons();
});
armSelect.addEventListener("change", () => {
  sessionId = ""; sessionInput.value = ""; newSession.checked = true;
  sessionState.textContent = "NO SESSION"; solveState.textContent = "WAITING";
  result.textContent = "等待结果…"; previews.replaceChildren(); previewState.textContent = "WAITING";
  renderSessionHistory(); renderCameraHealth(healthInputs); updateButtons();
});
newSession.addEventListener("change", updateButtons);
sessionInput.addEventListener("input", () => { sessionId = sessionInput.value.trim(); updateButtons(); });
sessionHistory.addEventListener("change", () => {
  loadSessionButton.disabled = !calibrationSessions.find((item) => item.session_id === sessionHistory.value)?.recoverable;
  deleteSessionButton.disabled = !sessionHistory.value;
});
refreshSessionsButton.addEventListener("click", () => { void refreshSessionHistory(deleteEmptySessions.checked); });
loadSessionButton.addEventListener("click", loadSelectedSession);
deleteSessionButton.addEventListener("click", () => { void deleteSelectedSession(); });
loadConfig(); void refreshSessionHistory(); connect();
