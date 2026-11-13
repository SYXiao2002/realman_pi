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

test("controller TCP selection invalidates old pose review and reports readback", async ({ page }) => {
  await page.goto("6d_pose.html");
  await page.locator("#controller-tool").selectOption("tcpsuck");
  await page.locator("#apply-tool").click();
  const request = await page.evaluate(() => (window as any).poseSocket.sent[0]);
  expect(request).toEqual({ type: "pose6d_select_tool", request_id: request.request_id, tool_name: "tcpsuck" });
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_tool_selected", request_id: request.request_id, tool_name: "tcpsuck", current_tool: "tcpsuck",
    motion_allowed: true, message: "verified",
  });
  await expect(page.locator("#tool-state")).toContainText("tcpsuck");
  await expect(page.locator("#capture")).toBeEnabled();
  await expect(page.locator("#prepare")).toBeDisabled();
});

test("capture, upload and pose review keep motion disabled without calibration", async ({ page }, info) => {
  await page.goto("6d_pose.html");
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent[0]);
  expect(capture.type).toBe("pose6d_capture");
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_capture", request_id: capture.request_id,
    observation_id: "obs-1", rgb: png, depth: png, profile: "right_wrist", width: 1, height: 1, camera_frame: "camera", calibration: "available" });
  await expect(page.locator("#download")).toBeVisible();
  await page.locator("#mask").setInputFiles({ name: "mask.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  await page.locator("#infer").click();
  const infer = await page.evaluate(() => (window as any).poseSocket.sent[1]);
  expect(infer.observation_id).toBe("obs-1");
  const pose = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]];
  await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_result", request_id: infer.request_id,
    observation_id: "obs-1", T_camera_object: pose, mesh_obj: "v -0.02 -0.02 0\nv 0.02 -0.02 0\nv 0 0.02 0\nf 1 2 3\n",
    cloud: { xyz_m: [[0, 0, .2], [.01, 0, .2], [0, .01, .2]], rgb: [[255, 0, 0], [0, 255, 0], [0, 0, 255]] },
    grasps: [{ grasp_id: "grasp_01", status: "pending", radius_m: .007, T_camera_contact: pose }],
    screening: { status: "insufficient_evidence", warning: "uncalibrated", metrics: { silhouette_iou: .8 } }, motion_reason: "TCP 未配置" });
  await expect(page.locator("#warning")).toContainText("uncalibrated");
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
  await page.locator("#mask").setInputFiles({ name: "other.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  await expect(page.locator("#grasp")).toBeDisabled();
  await expect(page.locator("#pose")).toHaveText("—");
  await page.locator("#capture").click();
  await expect(page.locator("#download")).toBeHidden();
  await expect(page.locator("#infer")).toBeDisabled();
});

test("reviewed suction motion sends only signed steps and displays confirmed distance", async ({ page }) => {
  await page.goto("6d_pose.html");
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent[0]);
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_capture", request_id: capture.request_id,
    observation_id: "obs-2", rgb: png, depth: png, profile: "right_wrist", width: 1, height: 1, camera_frame: "camera", calibration: "available",
    base_frame: "r/base_link", workspace_m: [[.03, .08, .4], [.19, .32, .58]],
    T_base_camera: [[1, 0, 0, .1], [0, 1, 0, .2], [0, 0, 1, .3], [0, 0, 0, 1]] });
  await page.locator("#mask").setInputFiles({ name: "mask.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  await page.locator("#infer").click();
  const infer = await page.evaluate(() => (window as any).poseSocket.sent[1]);
  const pose = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]];
  await page.evaluate((event) => (window as any).poseSocket.emit(event), { type: "pose6d_result", request_id: infer.request_id,
    observation_id: "obs-2", result_id: "result-2", T_camera_object: pose, mesh_obj: "v 0 0 0\nv .01 0 0\nv 0 .01 0\nf 1 2 3\n",
    cloud: { xyz_m: [[0, 0, .2]], rgb: [[120, 150, 180]] },
    grasps: [{ grasp_id: "g1", status: "reliable", radius_m: .007, T_camera_contact: pose,
      tangent_camera: [1, .1, 0], T_camera_prepare_contact: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .16], [0, 0, 0, 1]] }],
    screening: { status: "pass_screening", metrics: {} }, motion_ready: true, motion_reason: "运动参数已配置" });
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
    socket.emit({ type: "pose6d_motion", request_id: id, session_id: "session-2", state: "ready", busy: false, confirmed_mm: 40, lease_retained: true });
  }, prepare.request_id);
  await expect(page.locator("#distance")).toHaveValue("40.0");
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  expect(await page.evaluate(() => (window as any).poseSocket.sent.map((item: any) => item.type)))
    .toEqual(["pose6d_capture", "pose6d_infer", "pose6d_prepare"]);
  await page.locator("#step").fill("50");
  await page.locator("#minus").click();
  const jog = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(jog.type).toBe("pose6d_jog");
  expect(jog.step_mm).toBe(-50);
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "ready", busy: false, confirmed_mm: -10, lease_retained: true }), jog.request_id);
  await expect(page.locator("#distance")).toHaveValue("-10.0");
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
  expect(sent.every((request: any) => request.type === "pose6d_capture")).toBe(true);
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
