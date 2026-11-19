import assert from "node:assert/strict";
import { resolve } from "node:path";
import AxeBuilder from "@axe-core/playwright";

/** Actual reference upload/persistence. A fixture image result checks chat rendering;
 * Python tests cover provider routing, journal replay, and persisted chat artifacts. */
export async function checkImages(browser, { privateUrl, output, repo }) {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
  });
  context.setDefaultTimeout(15000);
  try {
    await context.addInitScript(() =>
      sessionStorage.setItem("companion-character", "rina-preview"),
    );
    const page = await context.newPage();
    const edit = async () => {
      await page.getByRole("button", { name: "Choose companion" }).click();
      await page
        .getByRole("button", { name: "Edit Rina", exact: true })
        .click();
    };
    await page.goto(privateUrl);
    await edit();
    const input = page.getByRole("dialog").locator('input[type="file"]');
    await input.setInputFiles({
      name: "bad.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("not an image"),
    });
    await page
      .getByText("Choose a PNG, JPEG or WebP image.", { exact: true })
      .waitFor();
    await input.setInputFiles(resolve(repo, "docs/assets/rina.jpg"));
    await page
      .getByAltText("Selected character appearance reference")
      .waitFor();
    await page
      .getByLabel("Appearance notes")
      .fill("Use the reference for facial identity.");
    await page
      .getByRole("button", { name: "Save character", exact: true })
      .click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    await page.reload();
    await edit();
    const image = page.getByAltText("Saved character appearance reference");
    await image.waitFor();
    await page.waitForFunction(
      () => document.querySelector(".reference-preview img")?.naturalWidth > 0,
    );
    assert.equal(
      await page.getByLabel("Appearance notes").inputValue(),
      "Use the reference for facial identity.",
    );
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
    );
    const a11y = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    assert.deepEqual(a11y.violations, [], "Appearance form accessibility");
    await page.screenshot({ path: resolve(output, "appearance-mobile.png") });
    await page.keyboard.press("Escape");
    const token = new URL(privateUrl).hash.slice("#token=".length);
    const boot = await context.request.get(
      new URL("/api/bootstrap", privateUrl).href,
      { headers: { Authorization: "Bearer " + token } },
    );
    const reference = (await boot.json()).appearance_references["rina-preview"];
    assert.ok(reference.artifact);
    // The image served here is the actual authenticated upload, not a remote placeholder.
    await page.route("**/api/conversation?*", async (route) => {
      const response = await route.fetch(),
        data = await response.json();
      data.turns[0].image_result = {
        status: "succeeded",
        artifacts: [reference],
      };
      await route.fulfill({ response, json: data });
    });
    await page.reload();
    await page.getByAltText("Generated character image").waitFor();
    await page.waitForFunction(
      () => document.querySelector(".reply-images img")?.naturalWidth > 0,
    );
    await page
      .getByRole("link", { name: "Download generated image" })
      .waitFor();
    await page.screenshot({
      path: resolve(output, "chat-image-mobile.png"),
      fullPage: true,
    });
    await page.unroute("**/api/conversation?*");
    await edit();
    await page
      .getByRole("button", { name: "Remove appearance reference" })
      .click();
    await page
      .getByRole("button", { name: "Save character", exact: true })
      .click();
    await page.getByRole("dialog").waitFor({ state: "hidden" });
    await page.reload();
    await edit();
    assert.equal(
      await page.getByAltText("Saved character appearance reference").count(),
      0,
    );
    return {
      upload: true,
      invalidType: true,
      reload: true,
      scopedMedia: true,
      chatDisplay: "fixture result with real media",
      removal: true,
      accessibilityViolations: 0,
    };
  } finally {
    await context.close();
  }
}
