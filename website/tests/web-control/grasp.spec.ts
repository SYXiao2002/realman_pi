import { expect, test } from "@playwright/test";

const snapshot = {
  type: "grasp_evaluation", schema_version: 1, read_only: true,
  observation_id: "obs-1", capture_timestamp_ns: "1000", review_request_id: "req-1",
  profile_id: "profile-1", profile_name: "right_wrist", arm: "r",
  camera_frame: "camera_right_color_optical_frame", base_frame: "r/base_link",
  target_label: "eu", backend: "geometry", model_id: null, calibration_id: null,
  tool_name: "tcpgrip", gripper: { id: "unmeasured" }, readiness_problems: ["verified calibration identity"],
  execution_ready: false, phase: "inferred", detail: "evaluation only",
  K: [[10, 0, 4], [0, 10, 4], [0, 0, 1]], T_base_camera: null,
  seed_joint_degrees: [0, 1, 2, 3, 4, 5], width: 8, height: 8,
  images: { rgb: "data:image/png;base64,iVBORw0KGgo=", depth: "data:image/png;base64,iVBORw0KGgo=", overlay: "data:image/png;base64,iVBORw0KGgo=" },
  cloud: { xyz_m: [[0, 0, 0.5]], rgb: [[255, 0, 0]] }, depth_stats: { valid_pixels: 1 },
  candidates: [{ candidate_id: "c1", class_name: "eu", score: 0.9, width_m: 0.04,
    T_camera_grasp: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], T_base_grasp: null }],
  plan: null, server_diagnostics: {},
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/layout", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ robots: [] }) }));
  await page.addInitScript(() => {
    class FakeWebSocket {
      static OPEN = 1; readyState = 0; listeners: Record<string, ((event: any) => void)[]> = {}; sent: string[] = [];
      constructor() { (window as any).__graspSocket = this; (window as any).__graspSent = this.sent; setTimeout(() => { this.readyState = 1; this.emit("open", {}); this.emit("message", { data: JSON.stringify({ type: "hello" }) }); this.emit("message", { data: JSON.stringify({ type: "grasp_bridge_status", default_profile: "right_wrist", profiles: [{ name: "right_wrist", arm: "r" }, { name: "left_wrist", arm: "l" }] }) }); }, 10); }
      addEventListener(type: string, callback: (event: any) => void) { (this.listeners[type] ||= []).push(callback); }
      emit(type: string, event: any) { for (const callback of this.listeners[type] || []) callback(event); }
      send(value: string) { this.sent.push(value); }
      close() { this.readyState = 3; }
    }
    (window as any).WebSocket = FakeWebSocket;
  });
});

test("grasp page keeps evaluation read-only and binds one observation", async ({ page }) => {
  await page.goto("grasp.html");
  await expect(page.getByRole("heading", { level: 1, name: "Grasp 离线评估" })).toBeVisible();
  await page.locator("#evaluate").click();
  const request = await page.evaluate(() => JSON.parse((window as any).__graspSent[0]));
  expect(request).toEqual({ type: "plan_grasp", request_id: expect.any(String), profile_name: "right_wrist", target_label: "eu", backend: "geometry", cgn_context: "mask" });
  expect(request.evaluation_only).toBeUndefined(); expect(request.arm).toBeUndefined();
  await page.evaluate((value) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(value) }), { ...snapshot, review_request_id: request.request_id });
  await expect(page.locator("#candidate-rows")).toContainText("c1");
  await expect(page.locator("#details")).toContainText("只读");
  await expect(page.locator("#readiness")).toContainText("verified calibration identity");
  await expect(page.locator("#plan")).toContainText("evaluation only");
  await expect(page.locator("#viewer-status")).toContainText("点云为相机光学系");
  expect(await page.locator("#cloud-view").evaluate((canvas: HTMLCanvasElement) => canvas.width > 0 && canvas.height > 0)).toBe(true);
  await page.locator("#save-review").click();
  await expect(page.locator("#review-status")).toHaveText("已保存在此浏览器");
  await page.locator("#export-review").click();
  await expect(page.locator("#export-review")).toBeEnabled();
  await page.locator("#profile").selectOption("left_wrist");
  await expect(page.locator("#candidate-rows")).toBeEmpty();
});

test("grasp page has no horizontal overflow on mobile", async ({ page }) => {
  await page.goto("grasp.html");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});

test("CGN context is sent and changing it clears the previous observation", async ({ page }) => {
  await page.goto("grasp.html");
  await page.locator("#backend").selectOption("contact_graspnet");
  await page.locator("#cgn-context").selectOption("box");
  await page.locator("#evaluate").click();
  const request = await page.evaluate(() => JSON.parse((window as any).__graspSent[0]));
  expect(request.cgn_context).toBe("box");
  await page.evaluate((value) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(value) }),
    { ...snapshot, backend: "contact_graspnet", cgn_context: "box", review_request_id: request.request_id });
  await expect(page.locator("#candidate-rows")).toContainText("c1");
  await page.locator("#cgn-context").selectOption("mask");
  await expect(page.locator("#candidate-rows")).toBeEmpty();
});


const alignedSnapshot = () => {
  const before = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .41], [0, 0, 0, 1]];
  const after = [[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, .59], [0, 0, 0, 1]];
  const contact = [[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, .5], [0, 0, 0, 1]];
  return { ...snapshot, T_base_camera: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
    images: { rgb: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", depth: snapshot.images.depth, overlay: snapshot.images.overlay },
    server_diagnostics: { vertical_alignment_status: "applied" },
    candidates: Array.from({ length: 6 }, (_, i) => ({ ...snapshot.candidates[0], candidate_id: "c" + (i + 1), backend: "geometry", object_id: "1",
      T_camera_native_grasp: before, T_camera_native_grasp_aligned: after,
      T_camera_grasp_before_alignment: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .5], [0, 0, 0, 1]],
      T_camera_grasp_after_alignment: contact, T_camera_grasp: contact,
      vertical_alignment: { applied: true, status: "applied", wrist_offset_m: .09, approach_axis_base: [0, 0, -1] } })) };
};

test("before/after views share ranks, visibility, camera and exported stage data", async ({ page }, testInfo) => {
  await page.goto("grasp.html");
  await page.evaluate((value) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(value) }), alignedSnapshot());
  await expect(page.locator("#alignment-status")).toContainText("接触中心保持不变");
  for (const id of ["cloud-view", "cloud-view-after", "projection", "projection-after"]) {
    await expect(page.locator("#" + id)).toHaveAttribute("data-candidates", "c1,c2,c3,c4,c5");
  }
  await page.getByRole("checkbox", { name: "显示 #6 c6", exact: true }).check();
  await page.getByRole("checkbox", { name: "显示 #1 c1", exact: true }).uncheck();
  for (const id of ["cloud-view", "cloud-view-after", "projection", "projection-after"]) {
    await expect(page.locator("#" + id)).toHaveAttribute("data-candidates", "c2,c3,c4,c5,c6");
  }
  const originalCamera = await page.locator("#cloud-view").getAttribute("data-view");
  await page.locator("#cloud-view").scrollIntoViewIfNeeded();
  await page.locator("#cloud-view").hover(); await page.mouse.wheel(0, -160);
  await expect.poll(() => page.locator("#cloud-view").getAttribute("data-view")).not.toBe(originalCamera);
  const camera = JSON.parse((await page.locator("#cloud-view").getAttribute("data-view"))!);
  const afterCamera = JSON.parse((await page.locator("#cloud-view-after").getAttribute("data-view"))!);
  camera.position.forEach((value: number, i: number) => expect(afterCamera.position[i]).toBeCloseTo(value, 12));
  expect(afterCamera.target).toEqual(camera.target);
  await page.locator("#review-stage").selectOption("before");
  await page.locator("#save-review").click();
  const review = await page.evaluate(() => JSON.parse(localStorage.getItem("grasp-review/v1:profile-1:obs-1")!));
  expect(review.stage).toBe("before");
  const downloadPromise = page.waitForEvent("download"); await page.locator("#export-review").click();
  const download = await downloadPromise; const stream = await download.createReadStream();
  let exported = ""; for await (const part of stream!) exported += part.toString();
  const parsed = JSON.parse(exported);
  expect(parsed.observation.candidates[0].T_camera_native_grasp_aligned[2][2]).toBe(-1);
  expect(parsed.review.stage).toBe("before");
  await page.locator("#profile").selectOption("left_wrist");
  for (const id of ["cloud-view", "cloud-view-after", "projection", "projection-after"]) await expect(page.locator("#" + id)).toHaveAttribute("data-candidates", "");
  await page.locator("#review-file").setInputFiles({ name: "review.json", mimeType: "application/json", buffer: Buffer.from(exported) });
  await expect(page.locator("#review-status")).toContainText("已导入离线评估");
  await expect(page.locator("#cloud-view-after")).toHaveAttribute("data-candidates", "c1,c2,c3,c4,c5");
  await expect(page.locator("#movel-execute")).toBeDisabled();
  await expect(page.locator("#movel-ready")).toContainText("离线导入仅供审阅");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.locator(".grasp-compare").screenshot({ path: testInfo.outputPath("alignment-comparison.png") });
});

test("missing calibration and CGN remain explicit rather than claiming alignment", async ({ page }) => {
  await page.goto("grasp.html");
  const missing = alignedSnapshot();
  (missing as any).base_camera_transform = { status: "tf_unavailable", detail: "采集时法兰 TF 不可用：extrapolation" };
  missing.server_diagnostics.vertical_alignment_status = "missing_base_camera";
  missing.candidates = missing.candidates.map((candidate) => ({ ...candidate,
    T_camera_native_grasp_aligned: null, T_camera_grasp_after_alignment: null,
    vertical_alignment: { ...candidate.vertical_alignment, applied: false, status: "missing_base_camera" } } as any));
  await page.evaluate((value) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(value) }), missing);
  await expect(page.locator("#alignment-status")).toContainText("对齐后不可用");
  await expect(page.locator("#alignment-status")).toContainText("采集时法兰 TF 不可用：extrapolation");
  await expect(page.locator("#cloud-view-after")).toHaveAttribute("data-candidates", "");
  await page.locator("#backend").selectOption("contact_graspnet");
  await page.evaluate((value) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(value) }), {
    ...missing, backend: "contact_graspnet", server_diagnostics: { vertical_alignment_status: "backend_not_applicable" },
    candidates: missing.candidates.map((candidate) => ({ ...candidate, backend: "contact_graspnet", vertical_alignment: { ...candidate.vertical_alignment, status: "backend_not_applicable" } })) });
  await expect(page.locator("#alignment-status")).toContainText("不执行几何竖直对齐");
  await expect(page.locator("#cloud-view-after")).toHaveAttribute("data-candidates", "c1,c2,c3,c4,c5");
});

test("insufficient instance is listed while valid poses remain visible", async ({ page }) => {
  await page.goto("grasp.html");
  const value = alignedSnapshot();
  (value.server_diagnostics as any).skipped_instances = [
    { object_id: "7", valid_points: 0, minimum_points: 32, reason: "insufficient_valid_depth" }];
  await page.evaluate((snapshot) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(snapshot) }), value);
  await expect(page.locator("#details")).toContainText("#7：0/32 点");
  await expect(page.locator("#cloud-view-after")).toHaveAttribute("data-candidates", "c1,c2,c3,c4,c5");
});

test("large capture layout shows aligned contact poses and keeps candidate selection in sync", async ({ page }, testInfo) => {
  await page.route("**/api/layout", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ robots: [{
    id: "r", transform: { x: 2, y: 1, z: 0, roll: 0, pitch: 0, yaw: 0 }, urdf_url: "/models/test-arm.urdf",
  }] }) }));
  await page.route("**/models/test-arm.urdf", (route) => route.fulfill({ contentType: "application/xml", body:
    '<robot name="capture"><link name="base_link"><visual><origin xyz="0 0 0.25"/><geometry><box size="0.1 0.1 0.5"/></geometry></visual></link></robot>' }));
  await page.goto("grasp.html");
  const value = alignedSnapshot();
  value.T_base_camera = [[0, -1, 0, .2], [1, 0, 0, -.1], [0, 0, 1, .3], [0, 0, 0, 1]];
  // Camera -> base includes rotation and translation; fleet world offsets must never enter pose readouts.
  value.candidates[1].T_camera_grasp_after_alignment = [[1, 0, 0, .1], [0, -1, 0, .2], [0, 0, -1, .5], [0, 0, 0, 1]];
  await page.evaluate((snapshot) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(snapshot) }), value);
  const canvas = page.locator("#layout"), box = (await canvas.boundingBox())!;
  expect(box.width).toBeGreaterThan(testInfo.project.name === "desktop" ? 1200 : 320);
  expect(box.height).toBeGreaterThanOrEqual(testInfo.project.name === "desktop" ? 440 : 350);
  await expect(canvas).toHaveAttribute("data-aligned-pose", "c1");
  await expect(canvas).toHaveAttribute("data-model-state", "ready");
  await expect(page.locator("#pose-after")).toContainText("camera_right_color_optical_frame");
  await expect(page.locator("#pose-after")).toContainText("r/base_link");
  await expect(page.locator("#pose-before")).toContainText("WXYZ     1.000000, 0.000000, 0.000000, 0.000000");
  await expect(page.locator("#pose-after")).toContainText("XYZ / m  0.000000, 0.000000, 0.500000");
  await expect(page.locator("#pose-after")).toContainText("XYZ / m  0.200000, -0.100000, 0.800000");
  await expect(page.locator("#pose-after")).toContainText("WXYZ     0.000000, 0.707107, 0.707107, 0.000000");
  await expect(page.locator("#pose-after")).not.toContainText("0.590000"); // Native wrist origin is not the contact centre.
  const initialView = JSON.parse((await canvas.getAttribute("data-view"))!);
  expect(initialView.target[0]).toBeGreaterThan(1.8); // Auto-fit follows the captured arm's world location.
  await canvas.scrollIntoViewIfNeeded(); await canvas.hover(); await page.mouse.wheel(0, -160);
  await expect.poll(() => canvas.getAttribute("data-view")).not.toBe(JSON.stringify(initialView));
  await page.locator("#fit-layout").click();
  const fitted = JSON.parse((await canvas.getAttribute("data-view"))!);
  const distance = (view: any) => Math.hypot(...view.position.map((v: number, i: number) => v - view.target[i]));
  expect(distance(fitted)).toBeCloseTo(distance(initialView), 6);
  await page.locator("#pose-candidate").selectOption("c2");
  await expect(page.locator("#review-candidate")).toHaveValue("c2");
  await expect(canvas).toHaveAttribute("data-aligned-pose", "c2");
  await expect(page.locator("#pose-after")).toContainText("XYZ / m  0.000000, 0.000000, 0.800000");
  const selectedView = JSON.parse((await canvas.getAttribute("data-view"))!);
  fitted.position.forEach((v: number, i: number) => expect(selectedView.position[i]).toBeCloseTo(v, 12));
  expect(selectedView.target).toEqual(fitted.target);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.locator("#layout").screenshot({ path: testInfo.outputPath("capture-layout.png") });
  await page.locator("#profile").selectOption("left_wrist");
  await expect(canvas).toHaveAttribute("data-aligned-pose", "");
  await expect(page.locator("#pose-after")).toHaveText("—");
  await expect(page.locator("#pose-candidate")).toBeDisabled();
});

test("selected base contact pose fills and sends a guarded MOVEL request", async ({ page }) => {
  await page.goto("grasp.html");
  const value = alignedSnapshot();
  value.T_base_camera = [[0, -1, 0, .2], [1, 0, 0, -.1], [0, 0, 1, .3], [0, 0, 0, 1]];
  value.candidates[1].T_camera_grasp_after_alignment = [[1, 0, 0, .1], [0, -1, 0, .2], [0, 0, -1, .5], [0, 0, 0, 1]];
  await page.evaluate((snapshot) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(snapshot) }), value);
  await page.locator("#pose-candidate").selectOption("c2");
  await expect(page.locator("#movel-execute")).toBeDisabled();
  await expect(page.locator("#movel-ready")).toContainText("等待机械臂在线状态");

  const emit = (message: object) => page.evaluate((event) =>
    (window as any).__graspSocket.emit("message", { data: JSON.stringify(event) }), message);
  await emit({ type: "connection", arm: "r", connected: true });
  await expect(page.locator("#movel-ready")).toContainText("等待驱动坐标状态");
  await expect(page.locator("#movel-execute")).toBeDisabled();
  await emit({ type: "coordinate_state", arm: "r", motion_allowed: false, current_tool: "tcpgrip", message: "tool mismatch" });
  await expect(page.locator("#movel-ready")).toContainText("坐标 BLOCKED");
  await expect(page.locator("#movel-execute")).toBeDisabled();
  await emit({ type: "coordinate_state", arm: "r", motion_allowed: true, current_tool: "other_tool" });
  await expect(page.locator("#movel-ready")).toContainText("当前 TCP 与候选工具不一致");
  await expect(page.locator("#movel-execute")).toBeDisabled();
  await emit({ type: "coordinate_state", arm: "r", motion_allowed: true, current_tool: "tcpgrip" });
  await expect(page.locator("#movel-x")).toHaveValue("0");
  await expect(page.locator("#movel-y")).toHaveValue("0");
  await expect(page.locator("#movel-z")).toHaveValue("0.8");
  await expect(page.locator("#movel-qw")).toHaveValue("0");
  await expect(page.locator("#movel-qx")).toHaveValue("0.7071067811865475");
  await expect(page.locator("#movel-qy")).toHaveValue("0.7071067811865476");
  await expect(page.locator("#movel-qz")).toHaveValue("0");
  await page.locator("#movel-execute").click();
  const request = await page.evaluate(() => JSON.parse((window as any).__graspSent.findLast((raw: string) => JSON.parse(raw).type === "execute_motion")));
  expect(request).toEqual({ type: "execute_motion", request_id: expect.any(String), arm: "r", goal: {
    command: 1, reference_type: 0, reference_name: "base", joint_degrees: [0, 0, 0, 0, 0, 0],
    pose_position_m: [0, 0, 0.8], pose_quaternion_wxyz: [0, expect.closeTo(Math.SQRT1_2, 12), expect.closeTo(Math.SQRT1_2, 12), 0],
    velocity_percent: 5, blend_radius_percent: 0, timeout_sec: 30,
  } });
  await emit({ type: "error", request_id: "unrelated-request", message: "unrelated failure" });
  await expect(page.locator("#movel-execute")).toBeDisabled();
  await emit({ type: "action_state", arm: "r", action: "execute_motion", request_id: request.request_id,
    state: "rejected", message: "driver rejected motion goal" });
  await expect(page.locator("#movel-execute")).toBeEnabled();
  await expect(page.locator("#movel-state")).toContainText("driver rejected motion goal");

  await page.locator("#movel-execute").click();
  const activeId = await page.evaluate(() => JSON.parse((window as any).__graspSent.findLast((raw: string) => JSON.parse(raw).type === "execute_motion")).request_id);
  await page.locator("#profile").selectOption("left_wrist");
  await page.locator("#movel-cancel").click();
  await page.locator("#movel-stop").click();
  const sent = await page.evaluate(() => (window as any).__graspSent.map((raw: string) => JSON.parse(raw)));
  expect(sent.at(-2)).toEqual({ type: "cancel_action", arm: "r", action: "execute_motion" });
  expect(sent.at(-1)).toEqual({ type: "software_stop", request_id: expect.any(String), arm: "r" });
  await emit({ type: "software_stop_result", request_id: sent.at(-1).request_id, success: true, message: "stopped" });
  await expect(page.locator("#movel-cancel")).toBeEnabled(); // Stop acknowledgement is not an Action result.
  await emit({ type: "action_feedback", arm: "r", action: "execute_motion", request_id: activeId,
    feedback: { detail: "canceling", progress: .2 } });
  await expect(page.locator("#movel-state")).toContainText("canceling · 20%");
  await emit({ type: "action_result", arm: "r", action: "execute_motion", request_id: activeId,
    result: { success: false, message: "canceled" } });
  await expect(page.locator("#movel-cancel")).toBeDisabled();
  await expect(page.locator("#movel-result")).toContainText("canceled");
  await expect(page.locator("#movel-x")).toHaveValue("");
  await expect(page.locator("#movel-execute")).toBeDisabled();
});

test("MOVEL prefers the snapshot base pose and validates manual inputs and read-only mode", async ({ page }) => {
  await page.goto("grasp.html");
  const value = alignedSnapshot();
  value.T_base_camera = null as any;
  (value.candidates[0] as any).T_base_grasp_after_alignment = [[0, -1, 0, .3], [1, 0, 0, -.2], [0, 0, 1, .4], [0, 0, 0, 1]];
  const emit = (message: object) => page.evaluate((event) =>
    (window as any).__graspSocket.emit("message", { data: JSON.stringify(event) }), message);
  await emit(value);
  await emit({ type: "connection", arm: "r", connected: true });
  await emit({ type: "coordinate_state", arm: "r", motion_allowed: true, current_tool: "tcpgrip" });
  await expect(page.locator("#movel-x")).toHaveValue("0.3");
  await expect(page.locator("#movel-y")).toHaveValue("-0.2");
  await expect(page.locator("#movel-z")).toHaveValue("0.4");
  await expect(page.locator("#movel-execute")).toBeEnabled();
  await page.locator("#movel-qw").fill("0");
  await page.locator("#movel-qz").fill("0");
  await expect(page.locator("#movel-execute")).toBeDisabled();
  await page.locator("#movel-qw").fill("1");
  await page.locator("#movel-velocity").fill("0");
  await expect(page.locator("#movel-execute")).toBeDisabled();
  await page.locator("#movel-velocity").fill("5");
  await expect(page.locator("#movel-execute")).toBeEnabled();
  await emit({ type: "hello", read_only: true });
  await expect(page.locator("#movel-ready")).toContainText("服务只读");
  await expect(page.locator("#movel-execute")).toBeDisabled();
  await emit({ type: "hello", read_only: false });
  await page.locator("#pose-candidate").selectOption("c2");
  await expect(page.locator("#movel-x")).toHaveValue("");
  await expect(page.locator("#movel-execute")).toBeDisabled();
  expect(await page.evaluate(() => (window as any).__graspSent.some((raw: string) => JSON.parse(raw).type === "execute_motion"))).toBe(false);
});

test("pose cards identify missing alignment and CGN passthrough", async ({ page }) => {
  await page.goto("grasp.html");
  const value = alignedSnapshot();
  value.T_base_camera = null as any;
  value.server_diagnostics.vertical_alignment_status = "missing_base_camera";
  value.candidates = value.candidates.map((candidate) => ({ ...candidate,
    T_camera_native_grasp_aligned: null, T_camera_grasp_after_alignment: null,
    vertical_alignment: { ...candidate.vertical_alignment, applied: false, status: "missing_base_camera" } } as any));
  await page.evaluate((snapshot) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(snapshot) }), value);
  await expect(page.locator("#pose-after")).toContainText("不可用");
  await expect(page.locator("#pose-before")).toContainText("缺少采集时 T_base_camera");
  await page.locator("#backend").selectOption("contact_graspnet");
  value.backend = "contact_graspnet";
  value.server_diagnostics.vertical_alignment_status = "backend_not_applicable";
  value.candidates = value.candidates.map((candidate) => ({ ...candidate, backend: "contact_graspnet",
    vertical_alignment: { ...candidate.vertical_alignment, status: "backend_not_applicable" } } as any));
  await page.evaluate((snapshot) => (window as any).__graspSocket.emit("message", { data: JSON.stringify(snapshot) }), value);
  await expect(page.locator("#pose-status")).toContainText("沿用原始 Pose");
  await expect(page.locator("#pose-after")).toHaveText((await page.locator("#pose-before").textContent())!);
});
