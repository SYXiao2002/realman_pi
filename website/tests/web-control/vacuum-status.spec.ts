import { expect, test } from "@playwright/test";

for (const path of ["/", "/6d_pose.html"]) {
  test(`${path} suction indicators show both arms, polarity, unavailable and disconnected states`, async ({ page }, info) => {
    await page.addInitScript(() => {
      class Socket {
        static OPEN = 1;
        readyState = 0;
        onopen: any; onclose: any; onmessage: any;
        listeners: Record<string, ((event: any) => void)[]> = {};
        constructor() {
          (window as any).vacuumSocket = this;
          setTimeout(() => { this.readyState = 1; this.emit("open", {}); }, 10);
        }
        addEventListener(type: string, callback: (event: any) => void) { (this.listeners[type] ||= []).push(callback); }
        emit(type: string, event: any) {
          for (const callback of this.listeners[type] || []) callback(event);
          (this as any)[`on${type}`]?.(event);
        }
        send() {}
        close() { this.readyState = 3; this.emit("close", {}); }
      }
      (window as any).WebSocket = Socket;
    });
    await page.goto(path);
    const left = page.locator('#vacuum-status [data-arm="l"]');
    const right = page.locator('#vacuum-status [data-arm="r"]');
    await expect(right).toHaveAttribute("data-state", "unknown");
    const emit = async (event: Record<string, any>) => page.evaluate(event => {
      (window as any).vacuumSocket.emit("message", { data: JSON.stringify(event) });
    }, { type: "vacuum_state", io: 3, enabled: true, available: true,
      holding: false, raw_input: 1, stale_after_sec: 10, ...event });
    await emit({ arm: "l", enabled: false, available: false, holding: null, raw_input: null });
    await emit({ arm: "r" });
    await expect(left).toHaveAttribute("data-state", "disabled");
    await expect(left).toContainText("未启用");
    await expect(right).toHaveAttribute("data-state", "empty");
    await expect(right).toContainText("未吸附");
    const red = await right.locator(".vacuum-light").evaluate(el => getComputedStyle(el).backgroundColor);
    await page.screenshot({ path: info.outputPath("vacuum-empty.png"), fullPage: true });
    await emit({ arm: "r", holding: true, raw_input: 0 });
    await expect(right).toHaveAttribute("data-state", "holding");
    await expect(right).toContainText("已吸附");
    expect(await right.locator(".vacuum-light").evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe(red);
    // State remains independent of the currently selected control arm.
    if (path === "/") {
      await page.locator("#arm-select").selectOption("l");
      await expect(right).toHaveAttribute("data-state", "holding");
    }
    await emit({ arm: "r", available: false, holding: null, raw_input: null, message: "机械臂未连接" });
    await expect(right).toHaveAttribute("data-state", "unknown");
    await expect(right).toContainText("机械臂未连接");
    await emit({ arm: "r", holding: true, raw_input: 0, stale_after_sec: .1 });
    await expect(right).toContainText("状态超时");
    await expect(right).toHaveAttribute("data-state", "unknown");
    await emit({ arm: "l", enabled: true, holding: true, raw_input: 0 });
    await emit({ arm: "r", holding: true, raw_input: 0 });
    await expect(left).toHaveAttribute("data-state", "holding");
    await expect(right).toHaveAttribute("data-state", "holding");
    await page.evaluate(() => (window as any).vacuumSocket.close());
    await expect(left).toContainText("连接中断");
    await expect(right).toContainText("连接中断");
    await expect(right).toHaveAttribute("data-state", "unknown");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  });
}
