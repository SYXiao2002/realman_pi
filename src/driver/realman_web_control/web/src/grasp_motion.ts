import { parsePoseGoal, parseMotionSettings, motionRequest } from "./motion_goal";

type Message = Record<string, any> & { type: string };
type Target = { arm: string; frame: string; tool: string; candidate: string; offline: boolean; values: number[] };
const ids = ["x", "y", "z", "qw", "qx", "qy", "qz"];
const requestId = (prefix: string) => `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;

/** Manual MOVEL shares the console's validation and WebSocket/ROS Action path. */
export class GraspMotion {
  private target?: Target;
  private pending?: { id: string; arm: string };
  private hello = false;
  private readOnly = true;
  private timer = 0;
  private stopRequest = "";
  private coordinates: Record<string, Message> = {};
  private connections: Record<string, boolean> = {};
  private fields: HTMLInputElement[];
  private velocity: HTMLInputElement;
  private timeout: HTMLInputElement;
  private execute: HTMLButtonElement;
  private cancel: HTMLButtonElement;
  private stop: HTMLButtonElement;
  private state: HTMLElement;
  private result: HTMLElement;

  constructor(private host: HTMLElement, private socket: () => WebSocket | undefined) {
    host.innerHTML = `<h2>基座系 MOVEL</h2><p id="movel-source" class="hint">选择候选后自动填入基座系 Pose</p>
      <p class="hint">按 TCP 与抓取坐标系重合使用所选 Pose。点击发送后直线移动到目标；默认速度 5%。</p>
      <div class="movel-inputs movel-position">${ids.slice(0, 3).map((id) => `<label>${id.toUpperCase()} / m<input id="movel-${id}" type="number" step="any" inputmode="decimal"></label>`).join("")}</div>
      <div class="movel-inputs movel-quaternion">${ids.slice(3).map((id) => `<label>Q${id[1].toUpperCase()}<input id="movel-${id}" type="number" step="any" inputmode="decimal"></label>`).join("")}</div>
      <div class="movel-inputs movel-settings"><label>速度 / %<input id="movel-velocity" type="number" min="1" max="100" step="1" value="5"></label><label>超时 / s<input id="movel-timeout" type="number" min="0.1" step="any" value="30"></label></div>
      <div id="movel-ready" class="hint" role="status"></div><div class="grasp-actions">
        <button id="movel-execute" class="button primary" type="button" disabled>发送 MOVEL</button>
        <button id="movel-cancel" class="button secondary" type="button" disabled>取消 Action</button>
        <button id="movel-stop" class="button danger" type="button" disabled>软件停止</button></div>
      <p id="movel-state" class="hint" role="status">尚未发送 MOVEL</p><pre id="movel-result" class="json-view" hidden></pre>`;
    const get = <T extends HTMLElement>(id: string) => host.querySelector<T>(`#movel-${id}`)!;
    this.fields = ids.map((id) => get<HTMLInputElement>(id));
    this.velocity = get("velocity"); this.timeout = get("timeout"); this.execute = get("execute");
    this.cancel = get("cancel"); this.stop = get("stop"); this.state = get("state"); this.result = get("result");
    [...this.fields, this.velocity, this.timeout].forEach((field) => field.addEventListener("input", () => this.update()));
    this.execute.addEventListener("click", () => this.submit());
    this.cancel.addEventListener("click", () => {
      if (this.writable() && this.pending) this.send({ type: "cancel_action", arm: this.pending.arm, action: "execute_motion" });
    });
    this.stop.addEventListener("click", () => {
      const arm = this.pending?.arm || this.target?.arm;
      if (this.writable() && arm) {
        this.stopRequest = requestId("stop");
        this.send({ type: "software_stop", request_id: this.stopRequest, arm });
      }
    });
  }

  fill(target?: Target) {
    this.target = target;
    this.fields.forEach((field, index) => { field.value = target ? String(target.values[index]) : ""; });
    this.host.querySelector("#movel-source")!.textContent = target
      ? `${target.arm.toUpperCase()} · BASE / ${target.frame} · ${target.tool} · ${target.candidate} · WXYZ`
      : "选择具有基座系 Pose 的候选后自动填入";
    this.update();
  }

  disconnected() {
    this.hello = false; this.coordinates = {}; this.connections = {};
    if (this.pending) this.state.textContent = `${this.pending.arm.toUpperCase()} 连接中断，运动状态未知；请检查机械臂，勿重复发送。`;
    this.update();
  }

  handle(message: Message) {
    if (message.type === "hello") { this.hello = true; this.readOnly = message.read_only === true; }
    if (message.type === "coordinate_state") this.coordinates[message.arm] = message;
    if (message.type === "connection") this.connections[message.arm] = message.connected === true;
    if (message.type === "software_stop_result" && message.request_id === this.stopRequest) {
      this.state.textContent = `软件停止${message.success ? "成功" : "失败"}：${message.message || ""}`;
      // Stop service response is not an Action terminal result.
    }
    if (this.pending && message.request_id === this.pending.id &&
      (message.type === "error" || (message.action === "execute_motion" && message.arm === this.pending.arm))) {
      const arm = this.pending.arm.toUpperCase();
      if (message.type === "action_feedback") {
        window.clearTimeout(this.timer);
        this.state.textContent = `${arm} MOVEL：${message.feedback?.detail || "执行中"} · ${Math.round((message.feedback?.progress || 0) * 100)}%`;
      } else if (message.type === "action_result") {
        this.state.textContent = `${arm} MOVEL ${message.result?.success ? "成功" : "未成功"}：${message.result?.message || ""}`;
        this.result.hidden = false; this.result.textContent = JSON.stringify(message.result, null, 2);
        this.finish();
      } else if (message.type === "error" || message.type === "action_state") {
        this.state.textContent = `${arm} MOVEL：${message.message || message.state || message.code}`;
        if (message.type === "error" || ["rejected", "stopped"].includes(message.state)) this.finish();
        // An Action transport error may leave hardware executing. Keep the pending lock.
      }
    }
    this.update();
  }

  private writable() { return this.hello && !this.readOnly && this.socket()?.readyState === WebSocket.OPEN; }
  private send(message: object) { this.socket()!.send(JSON.stringify(message)); }
  private settings() { return parseMotionSettings(this.velocity.value, this.timeout.value); }
  private pose() { return parsePoseGoal(this.fields.map((field) => field.value)); }
  private reason() {
    if (!this.writable()) return this.readOnly && this.hello ? "服务只读，无法发送运动" : "WebSocket 未连接";
    if (this.pending) return `${this.pending.arm.toUpperCase()} MOVEL 等待终态；请勿重复发送`;
    if (!this.target) return "缺少候选基座系 Pose";
    if (this.target.offline) return "离线导入仅供审阅，请重新采集后发送";
    if (this.connections[this.target.arm] !== true) return "等待机械臂在线状态";
    const coordinate = this.coordinates[this.target.arm];
    if (!coordinate) return "等待驱动坐标状态";
    if (coordinate.motion_allowed !== true) return `坐标 BLOCKED：${coordinate.message || "工具 / 工作坐标不匹配"}`;
    if (coordinate.current_tool !== this.target.tool) return "当前 TCP 与候选工具不一致";
    if (!this.pose()) return "请输入完整、有限的 XYZ 和非零四元数 WXYZ";
    if (!this.settings()) return "速度须为 1–100 的整数，超时须大于 0";
    return "";
  }
  private update() {
    const reason = this.reason();
    this.host.querySelector("#movel-ready")!.textContent = reason || "手动 MOVEL 就绪 · 复用控制台运动通道";
    this.execute.disabled = !!reason;
    this.cancel.disabled = !this.writable() || !this.pending;
    this.stop.disabled = !this.writable() || !(this.pending || this.target);
  }
  private finish() { window.clearTimeout(this.timer); this.pending = undefined; }
  private submit() {
    this.update();
    if (this.execute.disabled || !this.target) return;
    const id = requestId("movel"), arm = this.target.arm;
    this.pending = { id, arm };
    this.result.hidden = true;
    this.state.textContent = `${arm.toUpperCase()} MOVEL / BASE / 等待 feedback…`;
    try { this.send(motionRequest(id, arm, 1, { type: 0, name: "base" }, this.pose(), this.settings()!)); }
    catch { this.state.textContent = "发送失败，请检查连接"; this.finish(); }
    if (this.pending) this.timer = window.setTimeout(() => {
      if (this.pending?.id === id) this.state.textContent = `${arm.toUpperCase()} MOVEL 尚未收到反馈，运动状态未知；请勿重复发送，可使用软件停止。`;
    }, 8000);
    this.update();
  }
}
