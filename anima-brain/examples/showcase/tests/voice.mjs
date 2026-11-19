import assert from "node:assert/strict";
import { resolve } from "node:path";
import AxeBuilder from "@axe-core/playwright";

/** Uses Chrome's synthetic microphone, not the host's physical audio input. */
export async function checkVoice(browser, { privateUrl, output }) {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    reducedMotion: "reduce",
  });
  context.setDefaultTimeout(15000);
  const errors = [];
  const requests = [];
  try {
    await context.addInitScript(() => {
      sessionStorage.setItem("companion-character", "rina-preview");
      window.__voiceStreams = [];
      window.__voiceRecordings = [];
      const NativeRecorder = window.MediaRecorder;
      window.MediaRecorder = class extends NativeRecorder {
        constructor(...args) {
          super(...args);
          const capture = { bytes: 0 };
          window.__voiceRecordings.push(capture);
          this.addEventListener("dataavailable", (event) => {
            capture.bytes += event.data.size;
          });
        }
      };
      const getUserMedia = navigator.mediaDevices.getUserMedia.bind(
        navigator.mediaDevices,
      );
      window.__voiceGetUserMedia = async (constraints) => {
        const stream = await getUserMedia(constraints);
        window.__voiceStreams.push(stream);
        return stream;
      };
      navigator.mediaDevices.getUserMedia = window.__voiceGetUserMedia;
      window.__voiceSupports =
        MediaRecorder.isTypeSupported.bind(MediaRecorder);
    });
    const page = await context.newPage();
    const waitForAudio = async () => {
      await page
        .getByRole("button", { name: "Stop recording and transcribe" })
        .waitFor();
      await page.waitForFunction(
        () => window.__voiceRecordings.at(-1)?.bytes > 0,
      );
    };
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(privateUrl);
    await page.getByRole("button", { name: "Record voice message" }).waitFor();
    assert.equal(
      await page.getByRole("button", { name: "Generate an image" }).count(),
      0,
    );
    // No microphone prompt or upload before a transcription provider is configured.
    await page.getByRole("button", { name: "Record voice message" }).click();
    await page.getByRole("dialog").waitFor();
    assert.equal(
      await page
        .getByRole("button", { name: "Transcription", exact: true })
        .getAttribute("aria-pressed"),
      "true",
    );
    assert.equal(await page.evaluate(() => window.__voiceStreams.length), 0);
    await page.keyboard.press("Escape");
    await page.route("**/api/bootstrap", async (route) => {
      const response = await route.fetch(),
        data = await response.json();
      await route.fulfill({
        response,
        json: {
          ...data,
          capabilities: { ...data.capabilities, transcription: true },
        },
      });
    });
    let reply = async (route) =>
      route.fulfill({ json: { text: "I could use a quiet evening." } });
    await page.route("**/api/transcribe", async (route) => {
      requests.push(route.request().postDataJSON());
      await reply(route);
    });
    await page.reload();
    const input = page.getByRole("textbox", { name: "Message", exact: true });
    await input.waitFor();
    await input.fill("Already typed.");
    let finish;
    reply = (route) =>
      new Promise((resolve) => {
        finish = async () => {
          await route.fulfill({
            json: { text: "I could use a quiet evening." },
          });
          resolve();
        };
      });
    await page.getByRole("button", { name: "Record voice message" }).click();
    await page
      .getByRole("button", { name: "Stop recording and transcribe" })
      .waitFor();
    await page.waitForFunction(
      () =>
        document.querySelector(".recording-status time")?.textContent !==
        "0:00",
    );
    const accessibility = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    assert.deepEqual(
      accessibility.violations.map((v) => v.id),
      [],
    );
    await page.screenshot({ path: resolve(output, "voice-recording.png") });
    await waitForAudio();
    await page
      .getByRole("button", { name: "Stop recording and transcribe" })
      .click();
    await page.getByText("Transcribing your voice…", { exact: true }).waitFor();
    await page.waitForFunction(() =>
      window.__voiceStreams.every((s) =>
        s.getTracks().every((t) => t.readyState === "ended"),
      ),
    );
    assert.equal(
      await page
        .getByRole("button", { name: "Send message", exact: true })
        .isDisabled(),
      true,
    );
    assert.equal(requests.length, 1);
    assert.ok(Buffer.from(requests[0].audio, "base64").length > 100);
    assert.ok(
      ["audio/webm", "audio/mp4", "audio/ogg"].includes(requests[0].mime),
    );
    assert.ok(!requests[0].mime.includes("codecs"));
    await finish();
    await page.waitForFunction(
      () =>
        document.querySelector('textarea[aria-label="Message"]').value ===
        "Already typed.\nI could use a quiet evening.",
    );
    // Dictation is a draft, not a newly committed chat message.
    assert.equal(
      await page
        .locator(".user-message")
        .filter({ hasText: "Already typed." })
        .count(),
      0,
    );
    await page.screenshot({ path: resolve(output, "voice-transcribed.png") });
    const draft = await input.inputValue();
    reply = (route) => route.fulfill({ json: { text: "Another sentence." } });

    // Cancel must close the device and never send the captured bytes.
    await page.getByRole("button", { name: "Record voice message" }).click();
    await page
      .getByRole("button", { name: "Stop recording and transcribe" })
      .waitFor();
    await page.getByRole("button", { name: "Cancel recording" }).click();
    await page.waitForFunction(() =>
      window.__voiceStreams.every((s) =>
        s.getTracks().every((t) => t.readyState === "ended"),
      ),
    );
    assert.equal(requests.length, 1);
    assert.equal(await input.inputValue(), draft);

    // Permission denial is recoverable and preserves the draft.
    await page.evaluate(() => {
      navigator.mediaDevices.getUserMedia = async () => {
        throw new DOMException("fixture denied", "NotAllowedError");
      };
    });
    await page.getByRole("button", { name: "Record voice message" }).click();
    await page
      .getByRole("alert")
      .filter({ hasText: "Microphone access was blocked" })
      .waitFor();
    assert.equal(await input.inputValue(), draft);
    assert.equal(requests.length, 1);

    // A late permission grant after cancellation must not start a recorder or upload.
    await page.evaluate(() => {
      navigator.mediaDevices.getUserMedia = (constraints) =>
        new Promise((resolve) => {
          window.__grantLate = async () =>
            resolve(await window.__voiceGetUserMedia(constraints));
        });
    });
    await page.getByRole("button", { name: "Record voice message" }).click();
    await page.getByText("Allow microphone access…", { exact: true }).waitFor();
    await page.getByRole("button", { name: "Cancel recording" }).click();
    await page.evaluate(() => window.__grantLate());
    await page.waitForFunction(() =>
      window.__voiceStreams.every((s) =>
        s.getTracks().every((t) => t.readyState === "ended"),
      ),
    );
    assert.equal(requests.length, 1);
    await page.evaluate(() => {
      navigator.mediaDevices.getUserMedia = window.__voiceGetUserMedia;
    });

    // A provider failure is displayed, without retrying or changing typed text.
    reply = (route) =>
      route.fulfill({
        status: 502,
        json: { error: "Transcription fixture unavailable; not retried" },
      });
    await page.getByRole("button", { name: "Record voice message" }).click();
    await waitForAudio();
    await page
      .getByRole("button", { name: "Stop recording and transcribe" })
      .click();
    await page
      .getByRole("alert")
      .filter({ hasText: "Transcription fixture unavailable" })
      .waitFor();
    assert.equal(requests.length, 2);
    assert.equal(await input.inputValue(), draft);
    assert.equal(
      await page.evaluate(() =>
        window.__voiceStreams.every((s) =>
          s.getTracks().every((t) => t.readyState === "ended"),
        ),
      ),
      true,
    );

    // No-speech result leaves the existing draft alone.
    reply = (route) => route.fulfill({ json: { text: "" } });
    await page.getByRole("button", { name: "Record voice message" }).click();
    await waitForAudio();
    await page
      .getByRole("button", { name: "Stop recording and transcribe" })
      .click();
    await page
      .getByText("No speech was detected. Try recording again.", {
        exact: true,
      })
      .waitFor();
    assert.equal(await input.inputValue(), draft);
    assert.equal(requests.length, 3);

    // Exercise MP4 fallback with the actual encoder when this browser supports it.
    if (await page.evaluate(() => window.__voiceSupports("audio/mp4"))) {
      await page.evaluate(() => {
        MediaRecorder.isTypeSupported = (type) => type === "audio/mp4";
      });
      reply = (route) => route.fulfill({ json: { text: "MP4 recording." } });
      await page.getByRole("button", { name: "Record voice message" }).click();
      await waitForAudio();
      await page
        .getByRole("button", { name: "Stop recording and transcribe" })
        .click();
      await page.waitForFunction(() =>
        document
          .querySelector('textarea[aria-label="Message"]')
          .value.endsWith("MP4 recording."),
      );
      assert.equal(requests.at(-1).mime, "audio/mp4");
      await page.evaluate(() => {
        MediaRecorder.isTypeSupported = window.__voiceSupports;
      });
    }

    // The duration cap stops recording and submits exactly once.
    const count = requests.length;
    reply = (route) => route.fulfill({ json: { text: "Timed recording." } });
    await page.clock.install();
    await page.getByRole("button", { name: "Record voice message" }).click();
    await page
      .getByRole("button", { name: "Stop recording and transcribe" })
      .waitFor();
    await waitForAudio();
    await page.clock.fastForward(121000);
    await page.waitForFunction(() =>
      document
        .querySelector('textarea[aria-label="Message"]')
        .value.endsWith("Timed recording."),
    );
    assert.equal(requests.length, count + 1);
    assert.equal(
      await page.evaluate(() =>
        window.__voiceStreams.every((s) =>
          s.getTracks().every((t) => t.readyState === "ended"),
        ),
      ),
      true,
    );

    // Leaving the page discards an active recording; no delayed upload.
    await page.getByRole("button", { name: "Record voice message" }).click();
    await page
      .getByRole("button", { name: "Stop recording and transcribe" })
      .waitFor();
    await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
    await page.waitForFunction(() =>
      window.__voiceStreams.every((s) =>
        s.getTracks().every((t) => t.readyState === "ended"),
      ),
    );
    assert.equal(requests.length, count + 1);
    assert.deepEqual(errors, []);
    return {
      real_browser_recording: true,
      transcription_requests: requests.length,
      real_provider_calls: 0,
      formats: [...new Set(requests.map((r) => r.mime))],
      permission_and_lifecycle_checks: "passed",
    };
  } finally {
    await context.close();
  }
}
