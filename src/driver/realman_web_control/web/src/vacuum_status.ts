import "./vacuum_status.css";

type VacuumState = {
  type: "vacuum_state";
  arm: "l" | "r";
  enabled: boolean;
  available: boolean;
  holding: boolean | null;
  io: number;
  raw_input: number | null;
  message: string;
  stale_after_sec: number;
};

/** Shared read-only suction indicators; a missing heartbeat clears old readings. */
export class VacuumIndicators {
  private states = new Map<string, VacuumState>();
  private timers = new Map<string, number>();

  constructor(private root: HTMLElement) {
    root.classList.add("vacuum-status");
    for (const [arm, label] of [["l", "左臂吸盘"], ["r", "右臂吸盘"]]) {
      const row = document.createElement("div");
      row.className = "vacuum-indicator";
      row.dataset.arm = arm;
      row.dataset.state = "unknown";
      row.setAttribute("role", "status");
      row.setAttribute("aria-live", "polite");
      row.innerHTML = `<span class="vacuum-light" aria-hidden="true"></span><span class="vacuum-label">${label}</span><span class="vacuum-value">等待状态</span>`;
      root.append(row);
    }
  }

  receive(message: Record<string, any>) {
    if (message.type !== "vacuum_state" || !["l", "r"].includes(message.arm) ||
        typeof message.enabled !== "boolean" || typeof message.available !== "boolean") return;
    const state = message as VacuumState;
    this.states.set(state.arm, state);
    window.clearTimeout(this.timers.get(state.arm));
    this.render(state.arm);
    if (state.enabled) {
      const seconds = Number(state.stale_after_sec);
      this.timers.set(state.arm, window.setTimeout(() => {
        this.render(state.arm, "状态超时");
      }, Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 2000));
    }
  }

  disconnect() {
    for (const arm of ["l", "r"]) {
      window.clearTimeout(this.timers.get(arm));
      this.render(arm, "连接中断");
    }
  }

  private render(arm: string, unavailable?: string) {
    const row = this.root.querySelector<HTMLElement>(`[data-arm="${arm}"]`)!;
    const state = this.states.get(arm);
    let color = "unknown", text = unavailable || "等待状态";
    if (state) {
      if (!state.enabled) { color = "disabled"; text = "未启用"; }
      else if (!unavailable && state.available && typeof state.holding === "boolean") {
        color = state.holding ? "holding" : "empty";
        text = state.holding ? "已吸附" : "未吸附";
      } else { text = unavailable || state.message || "状态未知"; }
      row.title = `${arm.toUpperCase()} · IO${state.io} · ${text}${state.raw_input === null || unavailable ? "" : ` · 输入 ${state.raw_input}`}`;
    }
    row.dataset.state = color;
    const value = row.querySelector<HTMLElement>(".vacuum-value")!;
    if (value.textContent !== text) value.textContent = text;
  }
}
