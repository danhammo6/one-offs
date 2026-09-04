import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { once } from "node:events";
import test from "node:test";
import { chromium } from "playwright-core";

const INDEX = readFileSync(new URL("./index.html", import.meta.url));
const PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);
const ITEMS = Array.from({ length: 4000 }, (_, index) => {
  const category = index < 2000 ? "alpha" : "beta";
  const path = `${category}/item-${String(index).padStart(4, "0")}.jpg`;
  return {
    name: path.split("/").at(-1),
    path,
    category,
    output_url: `/img/output/test/${path}`,
    output_thumbnail_url: `/img/thumbnail/output/test/${path}?v=test-${index}`,
    input_url: `/img/input/test/${path}`,
    input_thumbnail_url: `/img/thumbnail/input/test/${path}?v=ref-${index}`,
    video_url: index === 2001 ? "/video/test/item-2001.mp4" : null,
  };
});

function chromeExecutable() {
  const candidates = [
    process.env.PLAYWRIGHT_CHROME,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ].filter(Boolean);
  return candidates.find(existsSync);
}

async function eventually(check, message, timeout = 5000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.fail(message);
}

test("gallery windowing, navigation, metadata, and prompt semantics", {
  timeout: 30000,
}, async () => {
  let streamRequests = 0;
  const metadataRequests = new Map();
  let releaseDelayedMetadata = null;
  const server = createServer((request, response) => {
    const url = new URL(request.url, "http://gallery.test");
    if (url.pathname === "/" || url.pathname === "/index.html") {
      response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      return response.end(INDEX);
    }
    if (url.pathname === "/api/sources") {
      response.writeHead(200, { "Content-Type": "application/json" });
      return response.end(JSON.stringify({
        sources: ["test"],
        default: "test",
      }));
    }
    if (url.pathname === "/api/stream") {
      streamRequests += 1;
      response.writeHead(200, {
        "Content-Type": "application/x-ndjson; charset=utf-8",
      });
      return response.end(ITEMS.map(item => JSON.stringify(item)).join("\n"));
    }
    if (url.pathname === "/api/metadata") {
      const path = url.searchParams.get("path");
      const count = (metadataRequests.get(path) || 0) + 1;
      metadataRequests.set(path, count);
      if (path === "beta/item-2000.jpg" && count === 1) {
        response.writeHead(503, { "Content-Type": "text/plain" });
        return response.end("temporary failure");
      }
      if (path === "beta/item-2002.jpg") {
        releaseDelayedMetadata = () => {
          response.writeHead(200, { "Content-Type": "application/json" });
          response.end(JSON.stringify({
            prompt: `metadata prompt for ${path}`,
          }));
          releaseDelayedMetadata = null;
        };
        return;
      }
      response.writeHead(200, { "Content-Type": "application/json" });
      return response.end(JSON.stringify({
        prompt: `metadata prompt for ${path}`,
      }));
    }
    if (url.pathname.startsWith("/img/")) {
      response.writeHead(200, { "Content-Type": "image/png" });
      return response.end(PIXEL);
    }
    if (url.pathname.startsWith("/video/")) {
      response.writeHead(200, { "Content-Type": "video/mp4" });
      return response.end();
    }
    response.writeHead(404);
    response.end("not found");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");

  const executablePath = chromeExecutable();
  assert.ok(executablePath, "Chrome or Chromium is required for frontend tests");
  const browser = await chromium.launch({ executablePath, headless: true });
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  try {
    const port = server.address().port;
    await page.goto(`http://127.0.0.1:${port}/`);
    await page.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("4000 renders"),
    );

    const cards = page.locator("#gallery .card");
    const initialCardCount = await cards.count();
    assert.ok(initialCardCount <= 80, "initial virtual window is bounded");
    await page.evaluate(() => window.scrollTo(
      0, document.documentElement.scrollHeight * 0.7,
    ));
    await page.waitForFunction(() => {
      const indexes = [...document.querySelectorAll("#gallery .card")]
        .map(card => Number(card.dataset.idx));
      return indexes.length && Math.max(...indexes) > 2500;
    });
    const deepCardCount = await cards.count();
    assert.ok(deepCardCount <= 80, "deep virtual window is bounded");

    await page.setViewportSize({ width: 720, height: 900 });
    await page.evaluate(() => new Promise(resolve =>
      requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const deepIndexes = await cards.evaluateAll(nodes =>
      nodes.map(node => Number(node.dataset.idx)));
    assert.ok(Math.max(...deepIndexes) > 2000, "resize preserves deep position");
    assert.ok(deepIndexes.length <= 80, "resized virtual window is bounded");

    await page.locator("#catFilter").selectOption("beta");
    await page.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("2000 in beta"),
    );
    const firstCard = page.locator('.card[data-idx="0"]');
    await firstCard.waitFor();
    assert.equal(await firstCard.locator("xpath=..").getAttribute("aria-posinset"), "1");
    assert.equal(await firstCard.locator("xpath=..").getAttribute("aria-setsize"), "2000");
    await firstCard.focus();
    await firstCard.press("End");
    await page.waitForFunction(
      () => document.activeElement?.matches('.card[data-idx="1999"]'),
    );
    assert.ok(await cards.count() <= 80, "category end remains windowed");
    await page.locator('.card[data-idx="1999"]').press("Home");
    await page.waitForFunction(
      () => document.activeElement?.matches('.card[data-idx="0"]'),
    );

    const failedMetadata = page.waitForResponse(response =>
      response.url().includes("/api/metadata")
      && response.url().includes("item-2000.jpg"));
    await firstCard.press("Enter");
    assert.equal((await failedMetadata).status(), 503);
    await page.waitForFunction(
      () => document.querySelector("#lbPrompt .prompt-body")
        ?.textContent.includes("no saved prompt"),
    );
    assert.equal(await page.locator("#lb").getAttribute("aria-modal"), "true");
    assert.equal(await page.locator("#lbClose").evaluate(
      element => element === document.activeElement), true);

    await page.keyboard.press("Escape");
    assert.equal(await firstCard.evaluate(
      element => element === document.activeElement), true);
    const retriedMetadata = page.waitForResponse(response =>
      response.url().includes("/api/metadata")
      && response.url().includes("item-2000.jpg"));
    await firstCard.press("Enter");
    assert.equal((await retriedMetadata).status(), 200);
    await page.waitForFunction(
      () => document.querySelector("#lbPrompt .prompt-body")
        ?.textContent.includes("metadata prompt for beta/item-2000.jpg"),
    );
    assert.equal(metadataRequests.get("beta/item-2000.jpg"), 2);

    await page.keyboard.press("ArrowRight");
    await page.waitForFunction(
      () => document.querySelector("#lbName")?.textContent
        === "beta/item-2001.jpg",
    );
    await page.keyboard.press("Escape");
    assert.equal(await firstCard.evaluate(
      element => element === document.activeElement), true);

    await page.locator("#videoMode").check();
    const secondCard = page.locator('.card[data-idx="1"]');
    const gridVideo = secondCard.locator("video");
    await gridVideo.waitFor();
    assert.equal(
      await gridVideo.getAttribute("aria-label"),
      "Video preview for beta/item-2001.jpg",
    );
    assert.equal(await secondCard.locator(".badge.vid").textContent(), "▶ video");
    await secondCard.focus();
    await secondCard.press("Enter");
    await page.waitForFunction(
      () => document.querySelector("#lbName")?.textContent
        === "beta/item-2001.jpg",
    );
    const lightboxVideo = page.locator("#lbStage video");
    assert.deepEqual(await lightboxVideo.evaluate(video => ({
      autoplay: video.autoplay,
      controls: video.controls,
      loop: video.loop,
      muted: video.muted,
      playsInline: video.playsInline,
    })), {
      autoplay: true,
      controls: true,
      loop: true,
      muted: true,
      playsInline: true,
    });
    assert.equal(
      await lightboxVideo.getAttribute("aria-label"),
      "Reimagined video beta/item-2001.jpg",
    );
    await page.keyboard.press("ArrowLeft");
    await page.waitForFunction(
      () => document.querySelector("#lbName")?.textContent
        === "beta/item-2000.jpg"
        && document.querySelector("#lbStage video") === null,
    );
    await page.keyboard.press("ArrowRight");
    await lightboxVideo.waitFor();
    await page.keyboard.press("Escape");
    assert.equal(await secondCard.evaluate(
      element => element === document.activeElement), true);

    await firstCard.press("Enter");
    await page.waitForFunction(
      () => document.querySelector("#lbPrompt .prompt-body")
        ?.textContent.includes("metadata prompt for beta/item-2000.jpg"),
    );
    assert.equal(metadataRequests.get("beta/item-2000.jpg"), 2);
    await page.keyboard.press("Escape");
    await page.locator("#refresh").click();
    await eventually(
      () => streamRequests === 2,
      "refresh did not reload the gallery",
    );
    await page.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("2000 in beta"),
    );
    const refreshedMetadata = page.waitForResponse(response =>
      response.url().includes("/api/metadata")
      && response.url().includes("item-2000.jpg"));
    await firstCard.press("Enter");
    await refreshedMetadata;
    assert.equal(metadataRequests.get("beta/item-2000.jpg"), 3);
    await page.keyboard.press("Escape");

    await page.setViewportSize({ width: 600, height: 900 });
    await firstCard.press("Enter");
    const promptButton = page.getByRole("button", { name: "prompt" });
    await promptButton.waitFor();
    assert.equal(await promptButton.getAttribute("aria-expanded"), "false");
    await page.locator("#lbClose").press("Tab");
    assert.equal(await promptButton.evaluate(
      element => element === document.activeElement), true);
    await promptButton.press("Enter");
    assert.equal(await promptButton.getAttribute("aria-expanded"), "true");
    await page.setViewportSize({ width: 1000, height: 600 });
    assert.equal(await promptButton.evaluate(
      element => element === document.activeElement), true);
    await promptButton.press("Space");
    assert.equal(await promptButton.getAttribute("aria-expanded"), "false");

    assert.equal(await page.locator("#lbPromptBody").getAttribute("id"), "lbPromptBody");
    assert.equal(await page.locator("#lbPrompt .lbl").getAttribute("aria-controls"),
      "lbPromptBody");
    assert.equal(await page.locator("#lb img:not([alt])").count(), 0);
    assert.equal(await page.locator("#gallery img:not([alt])").count(), 0);

    await page.keyboard.press("Escape");
    const thirdCard = page.locator('.card[data-idx="2"]');
    const delayedMetadata = page.waitForResponse(response =>
      response.url().includes("/api/metadata")
      && response.url().includes("item-2002.jpg"));
    await thirdCard.focus();
    await thirdCard.press("Enter");
    await eventually(
      () => typeof releaseDelayedMetadata === "function",
      "delayed metadata request did not start",
    );
    await page.locator("#lbClose").press("Tab");
    assert.equal(await promptButton.evaluate(
      element => element === document.activeElement), true);
    await page.evaluate(() => {
      window.__focusedPromptNode = document.activeElement;
    });
    releaseDelayedMetadata();
    await delayedMetadata;
    await page.waitForFunction(
      () => document.querySelector("#lbPromptBody")?.textContent
        .includes("metadata prompt for beta/item-2002.jpg"),
    );
    assert.equal(await page.evaluate(() =>
      document.activeElement === window.__focusedPromptNode
      && document.querySelector("#lbPrompt .lbl") === window.__focusedPromptNode
      && document.querySelector("#lb").contains(document.activeElement)), true);
    console.log(
      `gallery metrics: initial=${initialCardCount}, deep=${deepCardCount}, `
      + `resized=${deepIndexes.length}, logical=4000`,
    );
  } finally {
    releaseDelayedMetadata?.();
    await browser.close();
    server.close();
    await once(server, "close");
  }
});
