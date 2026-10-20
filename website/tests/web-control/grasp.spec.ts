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
  expect(request).toEqual({ type: "plan_grasp", request_id: expect.any(String), profile_name: "right_wrist", target_label: "eu", backend: "geometry" });
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
