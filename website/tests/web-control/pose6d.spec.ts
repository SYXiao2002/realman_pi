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

test("WAPR is selectable for a frozen SAM instance and reports its own score type", async ({ page }) => {
  await page.route("**/6d_pose_history/api/records/123/preview/score", route => route.fulfill({
    status: 200, contentType: "image/png",
    body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64"),
  }));
  await page.goto("6d_pose.html");
  await page.evaluate(() => (window as any).poseSocket.emit({
    type: "pose6d_frames", arm: "r", available: false,
    pose6d_algorithms: [
      { algorithm: "foundationpose", label: "FoundationPose", default: true },
      { algorithm: "wapr", label: "WAPR", default: false },
    ],
  }));
  await expect(page.locator("#algorithm option")).toHaveCount(2);
  await page.locator("#algorithm").selectOption("wapr");
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent.find((message: any) => message.type === "pose6d_capture"));
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_capture", request_id: capture.request_id, observation_id: "obs-wapr",
    object_id: "eu_plug", rgb: png, depth: png, profile: "right_wrist",
    width: 1, height: 1, camera_frame: "camera", calibration: "available",
  });
  const segment = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_instances", request_id: segment.request_id, observation_id: "obs-wapr", status: "ok",
    timings: {}, detections: [{ instance_id: "obs-wapr:0", detection_id: "0", class_name: "eu",
      confidence: .9, bbox: [0, 0, 1, 1], status: "ok", sam_score: .95,
      sam_predict_ms: 1, valid_depth_pixels: 4, mask_pixels: 4 }],
  });
  await page.locator("#instance-list button").click();
  const preview = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_instance_preview", request_id: preview.request_id, observation_id: "obs-wapr",
    instance_id: "obs-wapr:0", preview: png, mask_pixels: 4, valid_depth_pixels: 4,
  });
  await page.locator("#infer").click();
  const infer = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(infer).toMatchObject({ type: "pose6d_infer", algorithm: "wapr", instance_id: "obs-wapr:0" });
  await page.evaluate(({ event, png }) => (window as any).poseSocket.emit({
    type: "pose6d_result", request_id: event.request_id, observation_id: "obs-wapr", result_id: "r-wapr",
    algorithm: "wapr", algorithm_label: "WAPR", score: .4312, score_type: "wapr_score_6d",
    score_preview_available: true, history_id: "123", archive_status: "complete",
    object_id: "eu_plug", suction_available: false,
    T_camera_object: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]],
    mesh_obj: "v 0 0 0\nv .01 0 0\nv 0 .01 0\nf 1 2 3\n",
    cloud: { xyz_m: [[0, 0, .2]], rgb: [[120, 150, 180]] }, grasps: [],
    screening: { status: "insufficient_evidence", metrics: {} }, motion_ready: false,
    motion_reason: "未配置吸盘标注", overlay: png,
  }), { event: infer, png });
  await expect(page.locator("#pose-score")).toContainText("WAPR · wapr_score_6d: 0.4312");
  await expect(page.locator("#score-preview-caption")).toContainText("最高分候选");
  await expect(page.locator("#score-preview-image")).toHaveAttribute("src", "/6d_pose_history/api/records/123/preview/score");
  await expect(page.locator("#score-preview-link")).toHaveAttribute("href", "/6d_pose_history/api/records/123/files/vis_score.png");
  await expect(page.locator("#prepare")).toBeDisabled();
});

test("selecting a WAPR batch row loads that instance into the 6D suction review", async ({ page }) => {
  await page.goto("6d_pose.html");
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  await page.evaluate(({ request, png }) => (window as any).poseSocket.emit({
    type: "pose6d_capture", request_id: request.request_id, observation_id: "obs-batch",
    object_id: "eu_plug", rgb: png, depth: png, profile: "right_wrist",
    width: 1, height: 1, camera_frame: "camera", calibration: "available",
  }), { request: capture, png });
  const segment = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  await page.evaluate(({ request }) => (window as any).poseSocket.emit({
    type: "pose6d_instances", request_id: request.request_id, observation_id: "obs-batch", status: "ok",
    timings: {}, detections: [0, 1].map((index) => ({ instance_id: `obs-batch:${index}`, detection_id: String(index),
      class_name: "eu", confidence: .9, bbox: [0, 0, 1, 1], status: "ok", sam_score: .95,
      sam_predict_ms: 1, valid_depth_pixels: 4, mask_pixels: 4 })),
  }), { request: segment });

  await page.locator("#batch-infer").click();
  const batch = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(batch.type).toBe("pose6d_batch_infer");
  await page.evaluate(({ request, png }) => (window as any).poseSocket.emit({
    type: "pose6d_batch_result", request_id: request.request_id, observation_id: "obs-batch",
    instance_count: 2, timings: { round_trip_ms: 20, registration_ms: 16 }, overlay: png,
    results: [0, 1].map((index) => ({ instance_id: `obs-batch:${index}`, status: "ok", score: .4 + index * .1,
      history_id: String(701 + index), history_path: `/6d_pose_history/${701 + index}/`, archive_status: "complete" })),
  }), { request: batch, png });

  await page.locator('#batch-results button[data-instance-id="obs-batch:0"]').click();
  const select = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(select).toMatchObject({ type: "pose6d_batch_select", observation_id: "obs-batch", instance_id: "obs-batch:0" });
  await page.evaluate(({ request, png }) => (window as any).poseSocket.emit({
    type: "pose6d_result", request_id: request.request_id, observation_id: "obs-batch", result_id: "batch-review-0",
    algorithm: "wapr", algorithm_label: "WAPR", score: .4, score_type: "wapr_score_6d",
    history_id: "701", history_path: "/6d_pose_history/701/", archive_status: "complete",
    object_id: "eu_plug", suction_available: true,
    T_camera_object: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]],
    mesh_obj: "v 0 0 0\nv .01 0 0\nv 0 .01 0\nf 1 2 3\n",
    grasps: [{ grasp_id: "top", status: "valid", vertical_down_angle_deg: 0, workspace_valid: true,
      radius_m: .01, T_camera_contact: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]],
      contact_region: { radius_m: .01 }, analytic_ik: { checked: false },
      suction_collision: { status: "clear" }, tcp_targets: { frame: "base", suction: { xyz_mm: [0, 0, 200], rpy_deg: [0, 0, 0] } } }],
    screening: { status: "pass_screening", metrics: {} }, motion_ready: true, motion_reason: "review",
    overlay: png, cloud: { xyz_m: [[0, 0, .2]], rgb: [[120, 150, 180]] },
  }), { request: select, png });

  await expect(page.locator("#overlay-caption")).toContainText("#0 · 解算结果");
  await expect(page.locator("#pose-score")).toContainText("WAPR · wapr_score_6d: 0.4000");
  await expect(page.locator("#grasp option")).toContainText("top");
  await expect(page.locator("#history-result")).toHaveAttribute("href", "/6d_pose_history/701/");
  await expect(page.locator('#batch-results button[data-instance-id="obs-batch:0"]')).toHaveAttribute("data-selected", "true");
});

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

test("one EU capture and segmentation can solve repeatedly with different OBJ models", async ({ page }) => {
  await page.goto("6d_pose.html");
  await page.evaluate(() => (window as any).poseSocket.emit({
    type: "pose6d_frames", arm: "r", available: false,
    pose6d_models: [
      { object_id: "eu_plug", label: "欧规插头", default: false },
      { object_id: "eu_plug_1124", label: "欧规插头 1124", default: true },
    ],
  }));
  await expect(page.locator("#capture-object option")).toHaveCount(1);
  await expect(page.locator("#capture-object")).toHaveValue("eu_plug");
  await expect(page.locator("#object")).toHaveValue("eu_plug_1124");
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent.find((message: any) => message.type === "pose6d_capture"));
  expect(capture.object_id).toBe("eu_plug");
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_capture", request_id: capture.request_id, observation_id: "obs-shared",
    object_id: "eu_plug", rgb: png, depth: png, profile: "right_wrist",
    width: 1, height: 1, camera_frame: "camera", calibration: "available",
  });
  const segment = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(segment.type).toBe("pose6d_segment");
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_instances", request_id: segment.request_id, observation_id: "obs-shared",
    status: "ok", detection_overlay: png, timings: {}, detections: [{
      instance_id: "obs-shared:0", detection_id: "0", class_name: "eu", confidence: .9,
      bbox: [0, 0, 1, 1],
      status: "ok", sam_score: .95, sam_predict_ms: 1, valid_depth_pixels: 4, mask_pixels: 4,
    }],
  });
  await page.locator("#instance-list button").click();
  const preview = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_instance_preview", request_id: preview.request_id, observation_id: "obs-shared",
    instance_id: "obs-shared:0", preview: png, mask_pixels: 4, valid_depth_pixels: 4,
  });
  const pose = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]];
  for (const objectId of ["eu_plug", "eu_plug_1124", "eu_plug", "eu_plug_1124"]) {
    await page.locator("#object").selectOption(objectId);
    await expect(page.locator("#rgb")).toHaveAttribute("src", png);
    await expect(page.locator("#instances-overview")).toHaveAttribute("src", png);
    await expect(page.locator("#overlay")).toHaveAttribute("src", png);
    await expect(page.locator("#instance-list button")).toHaveAttribute("data-selected", "true");
    await expect(page.locator("#infer")).toBeEnabled();
    await page.locator("#infer").click();
    const infer = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
    expect(infer).toMatchObject({ type: "pose6d_infer", object_id: objectId,
      observation_id: "obs-shared", instance_id: "obs-shared:0" });
    await page.evaluate((event) => (window as any).poseSocket.emit(event), {
      type: "pose6d_result", request_id: infer.request_id, observation_id: "obs-shared",
      object_id: objectId, result_id: `result-${infer.request_id}`, suction_available: false,
      T_camera_object: pose, mesh_obj: "v 0 0 0\nv .01 0 0\nv 0 .01 0\nf 1 2 3\n",
      cloud: { xyz_m: [[0, 0, .2]], rgb: [[120, 150, 180]] }, grasps: [],
      screening: { status: "insufficient_evidence", metrics: {} }, motion_ready: false,
      motion_reason: "该物体尚无吸盘接触标注，已禁用运动",
    });
    await expect(page.locator("#state")).toContainText("解算完成");
    await expect(page.locator("#prepare")).toBeDisabled();
  }
  // A manually supplied mask can also be reused after a successful solve.
  await page.locator("#mask").setInputFiles({ name: "mask.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  await expect(page.locator("#state")).toContainText("Mask 已选择");
  await page.locator("#object").selectOption("eu_plug");
  await expect(page.locator("#mask")).toHaveValue(/mask.png/);
  await expect(page.locator("#infer")).toBeEnabled();
  await page.locator("#infer").click();
  const manual = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  expect(manual).toMatchObject({ type: "pose6d_infer", object_id: "eu_plug", observation_id: "obs-shared", mask_png: png.split(",")[1] });
  const commands = await page.evaluate(() => (window as any).poseSocket.sent);
  expect(commands.filter((item: any) => item.type === "pose6d_capture")).toHaveLength(1);
  expect(commands.filter((item: any) => item.type === "pose6d_segment")).toHaveLength(1);
  expect(commands.filter((item: any) => item.type === "pose6d_close")).toHaveLength(0);
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
    grasps: [{ grasp_id: "grasp_01", status: "pending", radius_m: .007, T_camera_contact: pose,
      vertical_down_angle_deg: null, workspace_valid: null, workspace_reason: "缺少采集时的 BASE-相机变换" }],
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
  await expect(page.locator("#tcp-target")).toContainText("解算完成并选择吸取方式后显示目标 TCP");
  await expect(page.locator("#pose")).toHaveCount(0);
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
    grasps: [{ grasp_id: "g1", status: "reliable", radius_m: .007, vertical_down_angle_deg: 0,
      workspace_valid: true, workspace_reason: null, T_camera_contact: pose,
      tangent_camera: [1, .1, 0], T_camera_prepare_contact: [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .16], [0, 0, 0, 1]],
      tool_name: "tcpsuck", T_camera_suction_tcp: [[1, 0, 0, .01], [0, 1, 0, .02], [0, 0, 1, .2], [0, 0, 0, 1]],
      analytic_ik: { checked: true, reachable: true, valid_solution_count: 4, reason: null },
      suction_collision: { status: "clear" },
      tcp_targets: { frame: "r/base_link", suction: { xyz_mm: [110, 220, 500], rpy_deg: [0, 0, 0] },
        prepare: { xyz_mm: [110, 220, 460], rpy_deg: [0, 0, 0] } },
      T_camera_prepare_tcp: [[1, 0, 0, .01], [0, 1, 0, .02], [0, 0, 1, .16], [0, 0, 0, 1]] }],
    screening: { status: "pass_screening", metrics: {} }, motion_ready: true, motion_reason: "运动参数已配置",
    motion_waypoints: { prepare_distance_mm: 40, second_prepare_distance_mm: 5, contact_offset_mm: 5, lift_height_mm: 50,
      home_joint_degrees: [9.76, 11.964, 106.229, -1.437, 54.787, 10.141],
      place_joint_degrees: [95.045, 11.965, 106.228, -1.437, 54.787, 10.14] } });
  await expect(page.locator("#auto-path")).toContainText("40 → 5 → -5 mm");
  await expect(page.locator("#tcp-target")).toContainText("g1 · tcpsuck");
  await expect(page.locator("#grasp option")).toContainText("有解析 IK（4 解）");
  await expect(page.locator("#tcp-target")).toContainText("有解析 IK（4 解）");
  await expect(page.locator("#tcp-target")).toContainText("吸取位（接触偏移 −5.0 mm）");
  await expect(page.locator("#tcp-target")).toContainText("XYZ (r/base_link)  110.0, 220.0, 500.0 mm");
  await expect(page.locator("#tcp-target")).toContainText("准备位（+40.0 mm）");
  await expect(page.locator("#tcp-target")).toContainText("XYZ (r/base_link)  110.0, 220.0, 460.0 mm");
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
  await expect(page.locator("#auto-vacuum")).toContainText("MOVEJ 回初始姿态，确认到位后 MOVEJ 放置");
  await expect(page.locator("#auto-path")).toContainText("MOVEJ 回初始姿态（YAML） → 确认到位 → MOVEJ 放置");
  await expect(page.locator("#auto-path")).toContainText("关闭吸盘 → MOVEJ 回初始姿态");
  await expect(page.locator("#auto-path")).toContainText("初始关节角 [9.760, 11.964, 106.229, -1.437, 54.787, 10.141]°");
  await expect(page.locator("#auto-path")).toContainText("关节角 [95.045, 11.965, 106.228, -1.437, 54.787, 10.140]°");
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
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "returning_home", busy: true, lease_retained: true,
    message: "MOVEJ 回到控制器初始姿态" }), automatic.request_id);
  await expect(page.locator("#distance")).toHaveValue("返回初始姿态");
  await expect(page.locator("#stop-motion")).toBeEnabled();
  await expect(page.locator("#tool-power")).toBeChecked();
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "at_home", busy: true, lease_retained: true,
    message: "初始姿态已确认到位" }), automatic.request_id);
  await expect(page.locator("#distance")).toHaveValue("初始姿态到位");
  await expect(page.locator("#auto-suction")).toBeDisabled();
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "placing", busy: true, lease_retained: true,
    tool_power: { available: true, success: true, voltage_type: 3, voltage_v: 24, enabled: true },
    message: "初始姿态已确认到位，正在 MOVEJ 前往放置位" }), automatic.request_id);
  await expect(page.locator("#distance")).toHaveValue("前往放置位");
  await expect(page.locator("#tool-power")).toBeChecked();
  // A lifted TCP while the power command is pending must not unlock controls
  // or claim that the suction has already been released.
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "placed", busy: true, lease_retained: true,
    tool_power: { available: false, message: "工具电源切换中，等待回读" } }), automatic.request_id);
  await expect(page.locator("#tool-power-state")).toContainText("等待回读");
  await expect(page.locator("#capture")).toBeDisabled();
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "returning_after_place", busy: true, auto_suction_ready: false,
    tool_power: { available: true, success: true, voltage_type: 0, voltage_v: 0, enabled: false },
    lease_retained: true, message: "吸盘已关闭（0 V），正在 MOVEJ 返回初始姿态" }), automatic.request_id);
  await expect(page.locator("#distance")).toHaveValue("放置后回初始位");
  await expect(page.locator("#capture")).toBeDisabled();
  await expect(page.locator("#auto-suction")).toBeDisabled();
  await expect(page.locator("#stop-motion")).toBeEnabled();
  await page.evaluate((id) => (window as any).poseSocket.emit({ type: "pose6d_motion", request_id: id,
    session_id: "session-2", state: "completed", busy: false, auto_suction_ready: false,
    tool_power: { available: true, success: true, voltage_type: 0, voltage_v: 0, enabled: false },
    lease_retained: true, message: "自动运动完成；物体已放置、吸盘已关闭（0 V），已回到初始姿态" }), automatic.request_id);
  await expect(page.locator("#distance")).toHaveValue("已回初始位");
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
  const grasp = (id: string, x: number, angle: number, workspaceValid = true, workspaceReason?: string) => ({
    grasp_id: id, status: "reliable", radius_m: .007, vertical_down_angle_deg: angle,
    workspace_valid: workspaceValid, workspace_reason: workspaceReason,
    T_camera_contact: pose(x), tangent_camera: [1, 0, 0],
  });
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
  await infer([grasp("g1", -.02, 5)]);
  const single = await page.locator("#viewer").screenshot();
  await infer([grasp("g1", -.02, 5), grasp("g2", .12, 10),
    grasp("outside", .2, 0, false, "准备位 TCP 或吸盘本体超出 workspace")]);
  await expect(page.locator("#grasp")).toHaveValue("g1");
  await expect(page.locator("#grasp option")).toHaveText([
    "g1 · reliable · 向下偏角 5.0° · 解析 IK 未完成检查 · 吸取路径未检查", "g2 · reliable · 向下偏角 10.0° · 解析 IK 未完成检查 · 吸取路径未检查",
    "outside · reliable · 向下偏角 0.0° · 解析 IK 未完成检查 · 吸取路径未检查 · 警告：准备位 TCP 或吸盘本体超出 workspace",
  ]);
  await expect(page.locator('#grasp option[value="outside"]')).toHaveAttribute("disabled", "");
  expect((await page.locator("#viewer").screenshot()).equals(single)).toBe(true);
  await page.locator("#review").check();
  await page.locator("#grasp").selectOption("g2");
  await expect(page.locator("#review")).not.toBeChecked();
  await page.locator("#fit-view").click();
  const second = await page.locator("#viewer").screenshot();
  expect(second.equals(single)).toBe(false);
  await page.locator("#grasp").selectOption("g1");
  await expect(page.locator("#grasp")).toHaveValue("g1");
  await page.locator("#fit-view").click();
  expect((await page.locator("#viewer").screenshot()).equals(second)).toBe(false);
  expect(await page.evaluate(() => (window as any).poseSocket.sent.some((request: any) => request.type === "pose6d_prepare" || request.type === "pose6d_jog"))).toBe(false);
});

test("suction choices display backend analytic IK and TCP values without new requests", async ({ page }) => {
  await page.goto("6d_pose.html");
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_capture", request_id: capture.request_id, observation_id: "ik-review",
    rgb: png, depth: png, profile: "right_wrist", width: 1, height: 1,
  });
  await completeSegment(page, "ik-review");
  await page.locator("#mask").setInputFiles({ name: "mask.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  await page.locator("#infer").click();
  const infer = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  const pose = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]];
  const states = [
    {
      pregrasp_ik: { checked: true, reachable: true, valid_solution_count: 8, reason: null },
      analytic_ik: { checked: true, reachable: true, valid_solution_count: 8, reason: null },
    },
    {
      pregrasp_ik: { checked: true, reachable: true, valid_solution_count: 8, reason: null },
      analytic_ik: { checked: true, reachable: false, valid_solution_count: 0, reason: "SDK_NO_SOLUTION" },
    },
    {
      pregrasp_ik: { checked: true, reachable: false, valid_solution_count: 0, reason: "SDK_NO_SOLUTION" },
      analytic_ik: { checked: true, reachable: true, valid_solution_count: 8, reason: null },
    },
    {
      pregrasp_ik: { checked: true, reachable: false, valid_solution_count: 0, reason: "SDK_NO_SOLUTION" },
      analytic_ik: { checked: true, reachable: false, valid_solution_count: 0, reason: "SDK_NO_SOLUTION" },
    },
    {
      pregrasp_ik: { checked: false, reachable: null, valid_solution_count: null, reason: "SDK_UNAVAILABLE" },
      analytic_ik: { checked: true, reachable: true, valid_solution_count: 8, reason: null },
    },
  ];
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_result", request_id: infer.request_id, observation_id: "ik-review", result_id: "ik-result",
    T_camera_object: pose, mesh_obj: "v 0 0 0\nv .01 0 0\nv 0 .01 0\nf 1 2 3\n",
    cloud: { xyz_m: [[0, 0, .2]], rgb: [[120, 150, 180]] },
    grasps: states.map(({ pregrasp_ik, analytic_ik }, index) => ({
      grasp_id: `ik-${index}`, status: "reliable", radius_m: .007, workspace_valid: true,
      T_camera_contact: pose, T_camera_suction_tcp: pose, pregrasp_ik, analytic_ik,
      // Display values are authoritative even if the scene transform differs.
      tcp_targets: { frame: "r/base_link", suction: { xyz_mm: [987.6, 123.4, 567.8], rpy_deg: [10, 20, 30] } },
    })),
    screening: { status: "pass_screening", metrics: {} }, motion_ready: true,
  });
  const labels = [
    "有解", "最终无解", "pregrasp无解", "pregrasp和最终都无解",
    "pregrasp IK 未完成检查（SDK_UNAVAILABLE）；最终有解",
  ];
  const before = await page.evaluate(() => (window as any).poseSocket.sent.length);
  for (let index = 0; index < states.length; index++) {
    await expect(page.locator(`#grasp option[value="ik-${index}"]`)).toContainText(labels[index]);
    await page.locator("#grasp").selectOption(`ik-${index}`);
    await expect(page.locator("#tcp-target")).toContainText(labels[index]);
    await expect(page.locator("#tcp-target")).toContainText("XYZ (r/base_link)  987.6, 123.4, 567.8 mm");
    await expect(page.locator("#tcp-target")).toContainText("RPY (r/base_link)  10.0, 20.0, 30.0°");
  }
  expect(await page.evaluate(() => (window as any).poseSocket.sent.length)).toBe(before);
});

test("suction collision is displayed without intercepting reviewed motion", async ({ page }) => {
  await page.goto("6d_pose.html");
  await page.locator("#capture").click();
  const capture = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=";
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_capture", request_id: capture.request_id, observation_id: "collision-review",
    rgb: png, depth: png, width: 1, height: 1,
  });
  await completeSegment(page, "collision-review");
  await page.locator("#mask").setInputFiles({ name: "mask.png", mimeType: "image/png", buffer: Buffer.from(png.split(",")[1], "base64") });
  await page.locator("#infer").click();
  const request = await page.evaluate(() => (window as any).poseSocket.sent.at(-1));
  const pose = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, .2], [0, 0, 0, 1]];
  await page.evaluate((event) => (window as any).poseSocket.emit(event), {
    type: "pose6d_result", request_id: request.request_id, observation_id: "collision-review", result_id: "collision-result",
    T_camera_object: pose, mesh_obj: "v 0 0 0\nv .01 0 0\nv 0 .01 0\nf 1 2 3\n",
    cloud: { xyz_m: [], rgb: [] }, screening: { status: "pass_screening", metrics: {} }, motion_ready: true,
    grasps: ["collision", "clear", "unknown"].map((status, index) => ({
      grasp_id: status, status: "reliable", radius_m: .005, vertical_down_angle_deg: index,
      T_camera_contact: pose, workspace_valid: true,
      analytic_ik: { checked: true, reachable: true, valid_solution_count: 8 },
      suction_collision: { status, start_distance_m: .01, end_distance_m: .04, length_m: .03, diameter_m: .01 },
      tcp_targets: { frame: "r/base_link", suction: { xyz_mm: [0, 0, 200], rpy_deg: [0, 0, 0] } },
    })),
  });
  await expect(page.locator("#grasp")).toHaveValue("collision");
  await expect(page.locator('#grasp option[value="collision"]')).not.toHaveAttribute("disabled", "");
  await expect(page.locator('#grasp option[value="collision"]')).toContainText("吸取路径上有物体");
  await expect(page.locator('#grasp option[value="unknown"]')).not.toHaveAttribute("disabled", "");
  await page.locator("#review").check();
  await expect(page.locator("#prepare")).toBeEnabled();
  const before = await page.evaluate(() => (window as any).poseSocket.sent.length);
  await page.locator("#grasp").selectOption("unknown");
  await expect(page.locator("#tcp-target")).toContainText("吸取路径无法判断");
  await page.locator("#review").check();
  await expect(page.locator("#prepare")).toBeEnabled();
  await page.locator("#grasp").selectOption("collision");
  await page.locator("#review").check();
  await expect(page.locator("#tcp-target")).toContainText("吸取路径上有物体");
  await expect(page.locator("#prepare")).toBeEnabled();
  expect(await page.evaluate(() => (window as any).poseSocket.sent.length)).toBe(before);
  await page.locator("#prepare").click();
  expect(await page.evaluate(() => (window as any).poseSocket.sent.at(-1))).toMatchObject({
    type: "pose6d_prepare", grasp_id: "collision", review_confirmed: true,
  });
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
  await expect(page.locator("#vision-timings")).toContainText("YOLO 总计websocket往返 19.9 ms（docker推理 12.3 ms, ROS2服务 15.5 ms）");
  await expect(page.locator("#vision-timings")).toContainText("SAM 总计websocket往返 50.0 ms（共享图像处理 23.0 + 所有实例解算 22.0 ms）");
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
