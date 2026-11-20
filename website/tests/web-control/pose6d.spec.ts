import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    class Socket {
      static OPEN = 1; readyState = 0; onopen: any; onclose: any; onmessage: any; sent: any[] = [];
      constructor() { (window as any).poseSocket = this; setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 10); }
      send(data: string) { this.sent.push(JSON.parse(data)); }
      emit(data: any) { this.onmessage?.({ data: JSON.stringify(data) }); }
    }
    (window as any).WebSocket = Socket;
  });
});

async function completeSegment(page: any, observationId: string) {
  const request = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(request.type).toBe("pose6d_segment");
  expect(request.observation_id).toBe(observationId);
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_instances", request_id: request.request_id, observation_id: observationId,
    status: "ok", detections: [], timings: {},
  });
  return request;
}

test("controller TCP selection invalidates old pose review and reports readback", async ({ page }) => {
  await page.goto("6d_pose.html");
  await page.locator("#controller-tool").selectOption("tcpsuck");
  await page.locator("#apply-tool").click();
  const request = await page.evaluate(() => (window as any).poseSocket.sent.find((message: any) => message.type === "pose6d_select_tool"));
  expect(request).toEqual({ type: "pose6d_select_tool", request_id: request.request_id, tool_name: "tcpsuck" });
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_tool_selected", request_id: request.request_id, tool_name: "tcpsuck", current_tool: "tcpsuck",
    motion_allowed: true, message: "verified",
  });
  await expect(page.locator("#tool-state")).toContainText("tcpsuck");
  await expect(page.locator("#capture")).toBeEnabled();
  await expect(page.locator("#prepare")).toBeDisabled();
});

test("tool power switch reads output and waits for verified 24V or 0V before changing", async ({ page }) => {
  await page.goto("6d_pose.html");
  const request = await page.evaluate(() => (window as any).poseSocket.sent[0]);
  expect(request).toMatchObject({ type: "tool_power_status" });
  const toggle = page.locator("#tool-power");
  await expect(toggle).toBeDisabled();
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "tool_power_state", arm: "r", request_id: request.request_id, success: true,
    available: true, voltage_type: 0, voltage_v: 0, enabled: false,
  });
  await expect(toggle).toBeEnabled();
  await expect(page.locator("#tool-power-state")).toContainText("0 V");

  await toggle.check();
  const failedEnable = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(failedEnable).toMatchObject({ type: "tool_power_set", enabled: true });
  await expect(toggle).toBeChecked();
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "tool_power_state", arm: "r", request_id: failedEnable.request_id, operation: "tool_power_set",
    success: false, available: true, voltage_type: 2, voltage_v: 12, enabled: false,
    message: "tool voltage readback mismatch",
  });
  await expect(toggle).not.toBeChecked();
  await expect(page.locator("#tool-power-state")).toContainText("12 V");
  await expect(page.locator("#tool-power-state")).toHaveAttribute("data-error", "true");

  await toggle.check();
  const enable = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(enable).toMatchObject({ type: "tool_power_set", enabled: true });
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "tool_power_state", arm: "r", request_id: enable.request_id, operation: "tool_power_set",
    success: true, available: true, voltage_type: 3, voltage_v: 24, enabled: true,
  });
  await expect(toggle).toBeChecked();
  await expect(page.locator("#tool-power-state")).toContainText("24 V");

  await page.evaluate(() => (window as any).poseSocket.emit({
    type: "pose6d_motion", state: "ready", busy: false, session_id: "motion-1", lease_retained: true,
  }));
  await expect(toggle).toBeDisabled();
  await page.evaluate(() => (window as any).poseSocket.emit({
    type: "pose6d_motion", state: "idle", busy: false, lease_retained: false,
  }));
  await expect(toggle).toBeEnabled();

  await toggle.uncheck();
  const disable = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(disable).toMatchObject({ type: "tool_power_set", enabled: false });
  await expect(toggle).not.toBeChecked();
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "tool_power_state", arm: "r", request_id: disable.request_id, operation: "tool_power_set",
    success: true, available: true, voltage_type: 0, voltage_v: 0, enabled: false,
  });
  await expect(toggle).not.toBeChecked();
});

test("capture, upload and pose review keep motion disabled without calibration", async ({ page, context }, info) => {
  await context.route("**/6d_pose_history/api/records/*/preview/score", route => route.fulfill({
    contentType: "image/png",
    body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64"),
  }));
  await page.goto("6d_pose.html");
  await expect(page.locator("#score-preview")).toBeHidden();
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent.find((message: any) => message.type === "pose6d_capture"));
  expect(capture.type).toBe("pose6d_capture");
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_capture", request_id: capture.request_id,
    observation_id: "obs-1", rgb: png, depth: png, profile: "right_wrist", width: 1, height: 1, camera_frame: "camera", calibration: "available" });
  await expect(page.locator("#download")).toBeVisible();
  await completeSegment(page, "obs-1");
  await page.locator("#mask").setInputFiles({ name: "mask.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  await page.locator("#infer").click();
  const infer = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(infer.observation_id).toBe("obs-1");
  const pose = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]];
  await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_result", request_id: infer.request_id,
    observation_id: "obs-1", history_id: "1790000000000000000", archive_status: "complete",
    T_camera_object: pose, mesh_obj: "v -0.02 -0.02 0\nv 0.02 -0.02 0\nv 0 0.02 0\nf 1 2 3\n",
    cloud: { xyz_m: [[0, 0, .2], [.01, 0, .2], [0, .01, .2]], rgb: [[255, 0, 0], [0, 255, 0], [0, 0, 255]] },
    grasps: [{ grasp_id: "grasp_01", status: "pending", radius_m: .007, T_camera_contact: pose }],
    screening: { status: "insufficient_evidence", warning: "uncalibrated", metrics: { silhouette_iou: .8 } }, motion_reason: "TCP 未配置" });
  await expect(page.locator("#warning")).toContainText("uncalibrated");
  await expect(page.locator("#score-preview-image")).toBeVisible();
  await expect(page.locator("#score-preview-image")).toHaveAttribute("src", "/6d_pose_history/api/records/1790000000000000000/preview/score");
  await expect(page.locator("#score-preview-link")).toHaveAttribute("href", "/6d_pose_history/api/records/1790000000000000000/files/vis_score.png");
  await expect(page.locator("#grasp")).toHaveValue("grasp_01");
  await expect(page.locator("#prepare")).toBeDisabled();
  await expect(page.locator("#minus")).toBeDisabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  const viewer = page.locator("#viewer");
  const before = await viewer.screenshot();
  const colors = await page.evaluate(async (data) => {
    const bytes = Uint8Array.from(atob(data), (character) => character.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
    const canvas = document.createElement("canvas"); canvas.width = bitmap.width; canvas.height = bitmap.height;
    const context = canvas.getContext("2d")!; context.drawImage(bitmap, 0, 0);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    const unique = new Set<string>();
    for (let i = 0; i < pixels.length; i += 4) unique.add(`${pixels[i]},${pixels[i + 1]},${pixels[i + 2]}`);
    return unique.size;
  }, before.toString("base64"));
  expect(colors).toBeGreaterThan(10);
  const bounds = (await viewer.boundingBox())!;
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width / 2 + 60, bounds.y + bounds.height / 2 + 25, { steps: 5 });
  await page.mouse.up();
  expect((await viewer.screenshot()).equals(before)).toBe(false);
  await page.screenshot({ path: info.outputPath("pose6d-review.png") });
  // Opening this result must preserve the captured observation and pose review.
  await context.route("**/6d_pose_history/1790000000000000000/", route => route.fulfill({
    contentType: "text/html", body: "<h1>本次历史</h1>",
  }));
  const popupPromise = page.waitForEvent("popup");
  await page.locator("#history-result").click();
  const popup = await popupPromise;
  await expect(popup).toHaveURL(/\/6d_pose_history\/1790000000000000000\/$/);
  await expect(page).toHaveURL(/\/6d_pose.html$/);
  await expect(page.locator("#grasp")).toHaveValue("grasp_01");
  await expect(page.locator("#warning")).toContainText("uncalibrated");
  await popup.close();
  await page.locator("#mask").setInputFiles({ name: "other.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  await expect(page.locator("#score-preview")).toBeHidden();
  await expect(page.locator("#score-preview-image")).not.toHaveAttribute("src");
  await expect(page.locator("#grasp")).toBeDisabled();
  await expect(page.locator("#pose")).toHaveText("—");
  await page.locator("#capture").click();
  await expect(page.locator("#download")).toBeHidden();
  await expect(page.locator("#infer")).toBeDisabled();
});

test("reviewed suction motion sends only signed steps and displays confirmed distance", async ({ page }, info) => {
  await page.goto("6d_pose.html");
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent.find((message: any) => message.type === "pose6d_capture"));
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_capture", request_id: capture.request_id,
    observation_id: "obs-2", rgb: png, depth: png, profile: "right_wrist", width: 1, height: 1, camera_frame: "camera", calibration: "available",
    base_frame: "r/base_link", workspace_m: [[.03, .08, .4], [.19, .32, .58]],
    T_base_camera: [[1, 0, 0, .1], [0, 1, 0, .2], [0, 0, 1, .3], [0, 0, 0, 1]] });
  await completeSegment(page, "obs-2");
  await page.locator("#mask").setInputFiles({ name: "mask.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  await page.locator("#infer").click();
  const infer = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  const pose = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]];
  await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_result", request_id: infer.request_id,
    observation_id: "obs-2", result_id: "result-2", T_camera_object: pose, mesh_obj: "v 0 0 0\nv .01 0 0\nv 0 .01 0\nf 1 2 3\n",
    cloud: { xyz_m: [[0, 0, .2]], rgb: [[120, 150, 180]] },
    grasps: [{ grasp_id: "g1", status: "reliable", radius_m: .007, T_camera_contact: pose,
      tangent_camera: [1, .1, 0], T_camera_prepare_contact: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .16], [0, 0, 0, 1]] }],
    screening: { status: "pass_screening", metrics: {} }, motion_ready: true, motion_reason: "运动参数已配置",
    motion_waypoints: { prepare_distance_mm: 40, second_prepare_distance_mm: 5, lift_height_mm: 50 } });
  await page.evaluate(() => {
    const frames = { type: "pose6d_frames", arm: "r", available: true,
      tool_name: "tcpsuck", T_base_tcp: [[1, 0, 0, .15], [0, 1, 0, .2], [0, 0, 1, .35], [0, 0, 0, 1]],
      T_base_camera_live: [[1, 0, 0, .1], [0, 1, 0, .2], [0, 0, 1, .3], [0, 0, 0, 1]] };
    (window as any).poseSocket.emit(frames);
    // Match the real 5 Hz stream while comparing all layers; a single message
    // can expire during screenshots on a busy host and hide the live axes.
    (window as any).framesTimer = setInterval(() => (window as any).poseSocket.emit(frames), 200);
  });
  await expect(page.locator("#frames-state")).toContainText("当前 TCP · tcpsuck · 实时更新");
  // Each layer must change the rendered image, not just a checkbox value.
  for (const id of ["show-tangent", "show-prepare", "show-camera", "show-tool", "show-workspace"]) {
    await page.locator("#fit-view").click();
    const before = await page.locator("#viewer").screenshot();
    await page.locator(`#${id}`).uncheck();
    const after = await page.locator("#viewer").screenshot();
    expect(after.equals(before), `layer ${id} must change the viewer`).toBe(false);
    await page.locator(`#${id}`).check();
  }
  await page.evaluate(() => {
    clearInterval((window as any).framesTimer);
    (window as any).poseSocket.emit({ type: "pose6d_frames", arm: "r", available: false, message: "feedback gap" });
  });
  await expect(page.locator("#frames-state")).toContainText("feedback gap");
  await page.locator("#review").check();
  await expect(page.locator("#prepare")).toBeEnabled();
  await page.locator("#prepare").click();
  const prepare = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(prepare.type).toBe("pose6d_prepare");
  expect(prepare.review_confirmed).toBe(true);
  await page.evaluate((id) => {
    const socket = (window as any).poseSocket;
    socket.emit({ type: "pose6d_motion", request_id: id, session_id: "session-2", state: "preparing", busy: true, lease_retained: true });
    socket.emit({ type: "pose6d_motion", request_id: id, session_id: "session-2", state: "ready", busy: false,
      confirmed_mm: 40, auto_suction_ready: true, lease_retained: true });
  }, prepare.request_id);
  await expect(page.locator("#distance")).toHaveValue("40.0");
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  expect(await page.evaluate(() => (window as any).poseSocket.sent.map((item: any) => item.type).filter((type: string) => !type.startsWith("tool_power_"))))
    .toEqual(["pose6d_capture", "pose6d_segment", "pose6d_infer", "pose6d_prepare"]);
  await page.locator("#step").fill("50");
  await page.locator("#minus").click();
  const jog = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(jog.type).toBe("pose6d_jog");
  expect(jog.step_mm).toBe(-50);
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "ready", busy: false, confirmed_mm: -10, lease_retained: true }), jog.request_id);
  await expect(page.locator("#distance")).toHaveValue("-10.0");
  await page.evaluate(() => (window as any).poseSocket.emit({ type: "pose6d_motion", session_id: "session-2",
    state: "ready", busy: false, confirmed_mm: 40, auto_suction_ready: true, lease_retained: true }));
  await expect(page.locator("#auto-vacuum")).toContainText("开始时开启 24 V，提升完成后关闭至 0 V");
  await expect(page.locator("#auto-suction")).toBeEnabled();
  await page.locator("#auto-suction").click();
  const automatic = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(automatic.type).toBe("pose6d_auto_suction");
  expect(automatic.session_id).toBe("session-2");
  expect(automatic).not.toHaveProperty("vacuum_confirmed");
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "auto_suction", busy: true, lease_retained: true,
    tool_power: { available: true, success: true, voltage_type: 3, voltage_v: 24, enabled: true },
    message: "MOVEL approaching suction position slowly" }), automatic.request_id);
  await expect(page.locator("#tool-power")).toBeChecked();
  await expect(page.locator("#tool-power")).toBeDisabled();
  await expect(page.locator("#tool-power-state")).toContainText("24 V");
  await expect(page.locator("#auto-suction")).toBeDisabled();
  // A lifted TCP while the power command is pending must not unlock controls
  // or claim that the suction has already been released.
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "lifted", busy: true, lease_retained: true,
    tool_power: { available: false, message: "工具电源切换中，等待回读" } }), automatic.request_id);
  await expect(page.locator("#tool-power-state")).toContainText("等待回读");
  await expect(page.locator("#capture")).toBeDisabled();
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "lifted", busy: false, auto_suction_ready: false,
    tool_power: { available: true, success: true, voltage_type: 0, voltage_v: 0, enabled: false },
    lease_retained: true, message: "自动运动完成；提升已确认，吸盘已关闭（0 V）" }), automatic.request_id);
  await expect(page.locator("#distance")).toHaveValue("提升完成");
  await expect(page.locator("#tool-power")).not.toBeChecked();
  await expect(page.locator("#tool-power-state")).toContainText("0 V");
  expect(await page.evaluate(() => (window as any).poseSocket.sent.some((request: any) => request.type === "tool_power_set"))).toBe(false);
  await expect(page.locator("#auto-suction")).toBeDisabled();
  await expect(page.locator("#minus")).toBeDisabled();
  await page.screenshot({ path: info.outputPath("pose6d-auto-lift.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});

test("automatic power errors display unknown output and keep motion locked", async ({ page }) => {
  await page.goto("6d_pose.html");
  await page.evaluate(() => (window as any).poseSocket.emit({
    type: "pose6d_motion", session_id: "power-failure", state: "ready", busy: false,
    auto_suction_ready: true, lease_retained: true,
  }));
  await page.locator("#auto-suction").click();
  const request = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  await page.evaluate((id) => (window as any).poseSocket.emit({
    type: "pose6d_error", request_id: id, message: "power response lost",
    motion: { session_id: "power-failure", state: "blocked", busy: false, auto_suction_ready: false,
      lease_retained: true, tool_power: { available: false, success: false, message: "工具电源切换未确认" } },
  }), request.request_id);
  await expect(page.locator("#tool-power-state")).toHaveText("工具电源切换未确认");
  await expect(page.locator("#tool-power-state")).toHaveAttribute("data-error", "true");
  await expect(page.locator("#auto-suction")).toBeDisabled();
  await expect(page.locator("#minus")).toBeDisabled();
  await expect(page.locator("#stop-motion")).toBeEnabled();
  expect(await page.evaluate(() => (window as any).poseSocket.sent.some((item: any) => item.type === "tool_power_set"))).toBe(false);
});

test("workspace uses capture-time BASE transform, toggles without robot commands and survives telemetry loss", async ({ page }) => {
  await page.goto("6d_pose.html");
  await page.locator("#show-camera").uncheck();
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  const bounds = [[-.05, -.08, .15], [.05, .08, .25]];
  async function capture(offset: number[] | null) {
    await page.locator("#capture").click();
    const request = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
    await page.evaluate((event) => (window as any).poseSocket.emit(event), {
      type: "pose6d_capture", request_id: request.request_id, observation_id: request.request_id, rgb: png, depth: png,
      profile: "right_wrist", width: 1, height: 1, camera_frame: "camera", base_frame: "r/base_link",
      workspace_m: bounds.map(row => row.map((value, i) => value + (offset?.[i] ?? 0))),
      T_base_camera: offset ? [[1, 0, 0, offset[0]], [0, 1, 0, offset[1]], [0, 0, 1, offset[2]], [0, 0, 0, 1]] : null,
    });
    await completeSegment(page, request.request_id);
  }
  await capture([0, 0, 0]);
  await expect(page.locator("#workspace-state")).toContainText("r/base_link");
  await expect(page.locator("#workspace-state")).toContainText("X -50～50");
  const visible = await page.locator("#viewer").screenshot();
  await page.locator("#show-workspace").uncheck();
  const hidden = await page.locator("#viewer").screenshot();
  expect(hidden.equals(visible)).toBe(false);
  await page.locator("#show-workspace").check();
  expect((await page.locator("#viewer").screenshot()).equals(visible)).toBe(true);
  // Moving the BASE box and capture transform by the same amount leaves its
  // appearance in the captured camera frame unchanged.
  await capture([.2, -.1, .3]);
  expect((await page.locator("#viewer").screenshot()).equals(visible)).toBe(true);
  await page.evaluate(() => (window as any).poseSocket.emit({ type: "pose6d_frames", arm: "r", available: false, message: "feedback gap" }));
  expect((await page.locator("#viewer").screenshot()).equals(visible)).toBe(true);
  const sent = await page.evaluate(() => (window as any).poseSocket.sent);
  expect(sent.map((request: any) => request.type).filter((type: string) => !type.startsWith("tool_power_"))).toEqual([
    "pose6d_capture", "pose6d_segment", "pose6d_capture", "pose6d_segment",
  ]);
  await capture(null);
  await expect(page.locator("#workspace-state")).toContainText("获得手眼变换后显示");
  expect((await page.locator("#viewer").screenshot()).equals(hidden)).toBe(true);
});

test("only selected suction method and its reference geometry are rendered", async ({ page }) => {
  await page.goto("6d_pose.html");
  await page.locator("#show-camera").uncheck();
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_capture", request_id: capture.request_id,
    observation_id: "selected-only", rgb: png, depth: png, profile: "right_wrist", width: 1, height: 1 });
  await completeSegment(page, "selected-only");
  await page.locator("#mask").setInputFiles({ name: "mask.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  const pose = (x: number) => [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]];
  const grasp = (id: string, x: number) => ({ grasp_id: id, status: "reliable", radius_m: .007,
    T_camera_contact: pose(x), tangent_camera: [1, 0, 0] });
  async function infer(grasps: any[]) {
    await page.locator("#infer").click();
    const request = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
    await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_result", request_id: request.request_id,
      observation_id: "selected-only", result_id: request.request_id, T_camera_object: pose(0),
      mesh_obj: "v -.02 -.02 0\nv .02 -.02 0\nv 0 .02 0\nf 1 2 3\n",
      cloud: { xyz_m: [[0, 0, .2]], rgb: [[120, 150, 180]] }, grasps,
      screening: { status: "pass_screening", metrics: {} }, motion_ready: true });
    await page.locator("#fit-view").click();
  }
  await infer([grasp("g1", -.02)]);
  const single = await page.locator("#viewer").screenshot();
  await infer([grasp("g1", -.02), grasp("g2", .12)]);
  await expect(page.locator("#grasp")).toHaveValue("g1");
  expect((await page.locator("#viewer").screenshot()).equals(single)).toBe(true);
  await page.locator("#review").check();
  await page.locator("#grasp").selectOption("g2");
  await expect(page.locator("#review")).not.toBeChecked();
  await page.locator("#fit-view").click();
  expect((await page.locator("#viewer").screenshot()).equals(single)).toBe(false);
  await page.locator("#grasp").selectOption("g1");
  await page.locator("#fit-view").click();
  expect((await page.locator("#viewer").screenshot()).equals(single)).toBe(true);
  expect(await page.evaluate(() => (window as any).poseSocket.sent.some((request: any) => request.type === "pose6d_prepare" || request.type === "pose6d_jog"))).toBe(false);
});

test("automatic SAM instances show timings and send the selected instance to FoundationPose", async ({ page }) => {
  await page.goto("6d_pose.html");
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_capture", request_id: capture.request_id, observation_id: "obs-auto", rgb: png, depth: png,
    profile: "right_wrist", width: 1, height: 1, camera_frame: "camera", calibration: "available",
  });
  const segment = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(segment.type).toBe("pose6d_segment");
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_instances", request_id: segment.request_id, observation_id: "obs-auto", status: "ok",
    detections: [{ instance_id: "obs-auto:0", detection_id: "0", class_name: "eu", confidence: .94,
      bbox: [0, 0, 1, 1], status: "ok", sam_score: .88, sam_predict_ms: 12.5, mask_pixels: 9, valid_depth_pixels: 8 },
    { instance_id: "obs-auto:7", detection_id: "7", class_name: "eu", confidence: .91,
      bbox: [0, 0, 1, 1], status: "ok", sam_score: .85, sam_predict_ms: 10, mask_pixels: 8, valid_depth_pixels: 7 }],
    detection_overlay: png,
    timings: { yolo_inference_ms: 12.3, yolo_request_ms: 15.5, yolo_round_trip_ms: 19.9,
      sam_inference_ms_total: 45, sam_image_ms: 23, sam_predict_ms_total: 22, sam_round_trip_ms: 50 },
  });
  await expect(page.locator("#vision-timings")).toContainText("YOLO 12.3 ms");
  await expect(page.locator("#vision-timings")).toContainText("SAM 总计 45.0 ms");
  await expect(page.locator("#instances-overview")).toHaveAttribute("src", png);
  await expect(page.locator('#instance-list button[data-instance-id="obs-auto:7"]')).toContainText("#7 · eu");
  const viewerBounds = (await page.locator("#viewer").boundingBox())!;
  for (const id of ["rgb", "depth", "instances-overview", "overlay"]) {
    const bounds = (await page.locator(`#${id}`).boundingBox())!;
    expect(bounds.y + bounds.height, `${id} must be above the point cloud`).toBeLessThan(viewerBounds.y);
  }
  const instance = page.locator('#instance-list button[data-instance-id="obs-auto:0"]');
  await expect(instance).toContainText("#0 · eu · 0.94");
  await instance.click();
  const previewRequest = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(previewRequest).toMatchObject({ type: "pose6d_preview", observation_id: "obs-auto", instance_id: "obs-auto:0" });
  const preview = await page.evaluate(() => document.createElement("canvas").toDataURL());
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_instance_preview", request_id: previewRequest.request_id, observation_id: "obs-auto",
    instance_id: "obs-auto:0", preview, mask_pixels: 9, valid_depth_pixels: 8,
  });
  await expect(page.locator("#overlay")).toHaveAttribute("src", preview);
  await expect(page.locator("#overlay-caption")).toHaveText("#0 · SAM 实例");
  await expect(page.locator("#instances-overview")).toHaveAttribute("src", png);
  await expect(page.locator("#infer")).toBeEnabled();
  await page.locator("#infer").click();
  const infer = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(infer).toMatchObject({ type: "pose6d_infer", observation_id: "obs-auto", instance_id: "obs-auto:0" });
  expect(infer).not.toHaveProperty("mask_png");
});
