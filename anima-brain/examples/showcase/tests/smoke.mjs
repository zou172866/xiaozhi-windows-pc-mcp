/** Offline browser checks against the real local API and temporary SQLite storage. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import net from "node:net";
import { chromium } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { checkVoice } from "./voice.mjs";
import { checkCalls } from "./calls.mjs";
import { checkImages } from "./images.mjs";

const here = dirname(fileURLToPath(import.meta.url)),
  repo = resolve(here, "../../..");
const output = resolve(repo, "output/showcase-checks");
await mkdir(output, { recursive: true });
const temp = await mkdtemp(resolve(tmpdir(), "companion-showcase-"));
const socket = net.createServer();
socket.listen(0, "127.0.0.1");
await once(socket, "listening");
const port = socket.address().port;
await new Promise((r) => socket.close(r));
const python = process.env.PYTHON || resolve(repo, ".venv/bin/python");
const server = spawn(
  python,
  [
    "examples/showcase/serve.py",
    "--demo",
    "--port",
    String(port),
    "--storage",
    resolve(temp, "preview.sqlite3"),
  ],
  {
    cwd: repo,
    env: {
      ...process.env,
      COMPANION_SETTINGS_FILE: resolve(temp, "providers.json"),
    },
  },
);
let logs = "";
server.stderr.on("data", (b) => {
  logs += b.toString();
});
const issues = [],
  checks = [];
let browser;
try {
  const privateUrl = await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("Server startup timed out. " + logs)),
      20000,
    );
    server.stdout.on("data", (b) => {
      const match = b
        .toString()
        .match(/http:\/\/127\.0\.0\.1:\d+\/#token=[\w-]+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
    server.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    server.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error("Server exited: " + code + " " + logs));
    });
  });
  const chrome =
    process.env.BROWSER_EXECUTABLE ||
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  browser = await chromium.launch({
    headless: true,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
    ],
    ...(existsSync(chrome) ? { executablePath: chrome } : {}),
  });
  const captureErrors = (page) => {
    page.on("pageerror", (e) => issues.push(e.message));
    page.on("console", (m) => {
      if (m.type() === "error") issues.push(m.text());
    });
  };
  async function scan(page, label) {
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all(
        document
          .getAnimations()
          .filter((a) => a.effect?.getTiming().iterations !== Infinity)
          .map((a) => a.finished.catch(() => {})),
      );
    });
    const result = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    checks.push({
      label,
      accessibility: result.violations.map((v) => ({
        id: v.id,
        impact: v.impact,
        nodes: v.nodes.map((n) => ({
          target: n.target,
          summary: n.failureSummary,
        })),
      })),
    });
    assert.equal(
      result.violations.length,
      0,
      JSON.stringify(checks.at(-1), null, 2),
    );
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
      label + " horizontal overflow",
    );
  }
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  captureErrors(page);
  await page.goto(privateUrl);
  await page.getByRole("textbox", { name: "Message", exact: true }).waitFor();
  await page.getByText("Okay, no pep talk.", { exact: false }).waitFor();
  assert.equal(new URL(page.url()).hash, "", "URL fragment must be removed");
  await scan(page, "desktop-chat");
  await page.screenshot({ path: resolve(output, "desktop-chat.png") });
  await page.getByRole("button", { name: /^Memories/ }).click();
  await page
    .getByText("I prefer quiet evenings to big parties.", { exact: true })
    .waitFor();
  await scan(page, "memory-panel");
  await page.screenshot({ path: resolve(output, "memory.png") });
  await page.getByText("Source & provenance", { exact: true }).click();
  await page.getByRole("button", { name: "View original exchange" }).click();
  await page.getByText("Exact context sent to the reply model").click();
  await page.locator(".code-details pre").first().waitFor();
  await scan(page, "context-inspector");
  await page.getByRole("button", { name: "Close panel" }).click();
  await page
    .getByRole("button", { name: "Chat with Sean", exact: true })
    .click();
  await page.getByText("Some things are").waitFor();
  await scan(page, "desktop-welcome");
  await page.screenshot({ path: resolve(output, "desktop-welcome.png") });
  await page
    .getByRole("button", { name: "Create a character", exact: true })
    .click();
  await page.getByLabel("Name", { exact: true }).fill("Aki");
  await page
    .getByLabel("Personality", { exact: true })
    .fill("A thoughtful, quietly funny fictional companion.");
  await page.getByLabel("Memory saving").selectOption("review");
  await scan(page, "character-form");
  await page.getByRole("button", { name: "Meet your companion" }).click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await page
    .getByRole("textbox", { name: "Message", exact: true })
    .fill("Remember that I prefer quiet evenings.");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await page
    .getByRole("button", { name: "Save reply & memories", exact: true })
    .waitFor();
  await scan(page, "pending-review");
  await page
    .getByRole("button", { name: "Save reply & memories", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save reply & memories", exact: true })
    .waitFor({ state: "hidden" });
  await page.reload();
  await page.locator(".chat-name").filter({ hasText: "Aki" }).waitFor();
  await page.getByRole("button", { name: /^Memories/ }).click();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByLabel("Correct this memory").fill("Prefers quiet mornings.");
  await page.getByRole("button", { name: "Save correction" }).click();
  await page.getByText("Prefers quiet mornings.", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  await page
    .getByRole("button", { name: "Remove memory", exact: true })
    .click();
  await page.getByText("Nothing here yet.", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Close panel" }).click();
  // Removing a fact must not remove the original exchange.
  await page
    .getByText("Remember that I prefer quiet evenings.", { exact: true })
    .waitFor();
  await page.getByRole("button", { name: "Export character data" }).click();
  await page.getByRole("button", { name: "Provider settings" }).click();
  await page
    .getByText("This is an offline preview", { exact: false })
    .waitFor();
  await page.keyboard.press("Escape");
  // Exercise settings with the same test provider; no model call is made or key persisted.
  await page.route("**/api/bootstrap", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ response, json: { ...data, preview_mode: false } });
  });
  await page.reload();
  await page.getByRole("button", { name: "Provider settings" }).click();
  await page
    .getByLabel("Base URL", { exact: true })
    .fill("https://test-provider.invalid/v1");
  await page.getByLabel("Model", { exact: true }).fill("test-model");
  await page.getByLabel("API key", { exact: true }).fill("fixture-secret-only");
  await scan(page, "provider-settings");
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await page.getByRole("button", { name: "Configuration saved" }).waitFor();
  assert.equal(await page.getByLabel(/API key/).inputValue(), "");
  await page.keyboard.press("Escape");
  await page.waitForFunction(
    () =>
      document.activeElement?.getAttribute("aria-label") ===
      "Provider settings",
  );
  await page.route("**/api/chat", (route) =>
    route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({ error: "Fixture input validation failed" }),
    }),
  );
  await page
    .getByRole("textbox", { name: "Message", exact: true })
    .fill("Keep my message if validation fails.");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await page
    .getByText("Fixture input validation failed", { exact: true })
    .waitFor();
  assert.equal(
    await page
      .getByRole("textbox", { name: "Message", exact: true })
      .inputValue(),
    "Keep my message if validation fails.",
  );
  // Expected 400 is not a page runtime failure; Chromium logs its network status.
  issues.splice(
    0,
    issues.length,
    ...issues.filter((x) => !x.includes("400 (Bad Request)")),
  );
  await context.close();
  // Fresh pages at each size, including reduced-motion support.
  for (const [width, height] of [
    [375, 812],
    [768, 1024],
    [1024, 768],
    [1440, 900],
  ]) {
    const ctx = await browser.newContext({
      viewport: { width, height },
      deviceScaleFactor: 1,
      reducedMotion: "reduce",
    });
    const p = await ctx.newPage();
    await p.addInitScript(() =>
      sessionStorage.setItem("companion-character", "rina-preview"),
    );
    captureErrors(p);
    await p.goto(privateUrl);
    await p.getByText("Okay, no pep talk.", { exact: false }).waitFor();
    await scan(p, `viewport-${width}`);
    await p.screenshot({
      path: resolve(output, `chat-${width}.png`),
      fullPage: true,
    });
    if (width === 375) {
      await p.getByRole("button", { name: "Choose companion" }).click();
      await p.getByRole("button", { name: /Sean/ }).click();
      await p.getByText("Some things are").waitFor();
      await scan(p, "mobile-welcome");
      await p.screenshot({
        path: resolve(output, "mobile-welcome.png"),
        fullPage: true,
      });
      await p.getByRole("button", { name: /^Memories/ }).click();
      await scan(p, "mobile-memory");
      await p.keyboard.press("Escape");
    }
    assert.equal(
      await p.evaluate(
        () =>
          getComputedStyle(document.querySelector(".composer"))
            .transitionDuration,
      ),
      "0s",
    );
    await ctx.close();
  }
  const voice = await checkVoice(browser, { privateUrl, output });
  const images = await checkImages(browser, { privateUrl, output, repo });
  await writeFile(
    resolve(output, "image-checks.json"),
    JSON.stringify(images, null, 2),
  );
  await writeFile(
    resolve(output, "voice-checks.json"),
    JSON.stringify(voice, null, 2),
  );
  const calls = await checkCalls(browser, {privateUrl, output});
  await writeFile(resolve(output, "call-checks.json"), JSON.stringify(calls,null,2));
  assert.deepEqual(issues, [], "Browser errors");
  assert.deepEqual(
    checks.filter((c) => c.accessibility.length),
    [],
    "Accessibility failures",
  );
  console.log(
    "Browser checks passed: real local chat/review/memory/context/export, settings, browser dictation, live call controls/cleanup, architecture walkthrough, 4 viewports, reduced motion, and accessibility.",
  );
} finally {
  await writeFile(
    resolve(output, "checks.json"),
    JSON.stringify({ checks, issues }, null, 2),
  );
  await browser?.close();
  if (server.pid && server.exitCode === null && server.signalCode === null) {
    const exited = once(server, "exit");
    server.kill("SIGTERM");
    await exited;
  }
  await rm(temp, { recursive: true, force: true });
}
