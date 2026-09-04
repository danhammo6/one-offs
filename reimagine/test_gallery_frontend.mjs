import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { once } from "node:events";
import test from "node:test";
import { chromium, webkit } from "playwright-core";

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

async function waitForLoadedImages(page, selector) {
  await page.waitForFunction(imageSelector =>
    [...document.querySelectorAll(imageSelector)]
      .every(image => image.complete && image.naturalWidth), selector);
}

const BROWSERS = [
  {
    name: "Chromium",
    type: chromium,
    launchOptions: () => {
      const executablePath = chromeExecutable();
      assert.ok(executablePath, "Chrome or Chromium is required for frontend tests");
      return { executablePath, headless: true };
    },
  },
  {
    name: "WebKit",
    type: webkit,
    launchOptions: () => ({ headless: true }),
  },
];

for (const { name, type, launchOptions } of BROWSERS) {
test(`gallery windowing, navigation, metadata, and prompt semantics (${name})`, {
  timeout: 45000,
}, async () => {
  let streamRequests = 0;
  const metadataRequests = new Map();
  let releaseInitialStream = null;
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
      if (streamRequests === 1) {
        response.flushHeaders();
        response.socket?.setNoDelay(true);
        response.write(`${ITEMS.slice(0, 100)
          .map(item => JSON.stringify(item)).join("\n")}\n`);
        releaseInitialStream = () => {
          response.end(ITEMS.slice(100)
            .map(item => JSON.stringify(item)).join("\n"));
          releaseInitialStream = null;
        };
        return;
      }
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

  const browser = await type.launch(launchOptions());
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  try {
    const port = server.address().port;
    await page.goto(`http://127.0.0.1:${port}/`);
    await page.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("100 renders"),
    );
    const earlyAria = await page.evaluate(() => {
      const card = document.querySelector('.card[data-idx="0"]');
      window.__earlyVirtualItem = card.closest(".virtual-item");
      window.__earlyImage = card.querySelector("img");
      return {
        setsize: window.__earlyVirtualItem.getAttribute("aria-setsize"),
        posinset: window.__earlyVirtualItem.getAttribute("aria-posinset"),
      };
    });
    assert.deepEqual(earlyAria, { setsize: "100", posinset: "1" });
    assert.equal(typeof releaseInitialStream, "function");
    releaseInitialStream();
    await page.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("4000 renders"),
    );
    assert.deepEqual(await page.evaluate(() => {
      const card = document.querySelector('.card[data-idx="0"]');
      const item = card.closest(".virtual-item");
      return {
        sameItem: item === window.__earlyVirtualItem,
        sameImage: card.querySelector("img") === window.__earlyImage,
        setsize: item.getAttribute("aria-setsize"),
        posinset: item.getAttribute("aria-posinset"),
      };
    }), {
      sameItem: true,
      sameImage: true,
      setsize: "4000",
      posinset: "1",
    }, "stream completion updates ARIA without replacing overlapping media");

    await page.locator("#catFilter").selectOption("alpha");
    await page.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("2000 in alpha"),
    );
    assert.deepEqual(await page.evaluate(() => {
      const card = document.querySelector('.card[data-idx="0"]');
      const item = card.closest(".virtual-item");
      return {
        sameItem: item === window.__earlyVirtualItem,
        sameImage: card.querySelector("img") === window.__earlyImage,
        setsize: item.getAttribute("aria-setsize"),
        posinset: item.getAttribute("aria-posinset"),
      };
    }), {
      sameItem: true,
      sameImage: true,
      setsize: "2000",
      posinset: "1",
    }, "overlapping filter results update ARIA in place");
    await page.locator("#catFilter").selectOption("");
    await page.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("4000 renders"),
    );
    assert.equal(await page.locator('.card[data-idx="0"]')
      .locator("xpath=..").getAttribute("aria-setsize"), "4000");

    await page.evaluate(() => focusLogicalCard(2000));
    await page.waitForFunction(
      () => document.activeElement?.matches('.card[data-idx="2000"]'),
    );
    assert.deepEqual(await page.evaluate(() => {
      const card = document.querySelector('.card[data-idx="2000"]');
      const item = card.closest(".virtual-item");
      window.__betaVirtualItem = item;
      window.__betaImage = card.querySelector("img");
      return {
        setsize: item.getAttribute("aria-setsize"),
        posinset: item.getAttribute("aria-posinset"),
      };
    }), { setsize: "4000", posinset: "2001" });
    await page.locator("#catFilter").selectOption("beta");
    await page.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("2000 in beta"),
    );
    assert.deepEqual(await page.evaluate(() => {
      const card = document.querySelector('.card[data-idx="0"]');
      const item = card.closest(".virtual-item");
      return {
        sameItem: item === window.__betaVirtualItem,
        sameImage: card.querySelector("img") === window.__betaImage,
        setsize: item.getAttribute("aria-setsize"),
        posinset: item.getAttribute("aria-posinset"),
      };
    }), {
      sameItem: true,
      sameImage: true,
      setsize: "2000",
      posinset: "1",
    }, "filtering updates changed logical positions without replacing media");
    await page.locator("#catFilter").selectOption("");
    await page.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("4000 renders"),
    );
    await page.evaluate(() => window.scrollTo(0, 0));

    const cards = page.locator("#gallery .card");
    const initialCardCount = await cards.count();
    assert.ok(initialCardCount <= 80, "initial virtual window is bounded");
    await waitForLoadedImages(page, "#gallery img");
    const scrollMetrics = await page.evaluate(async () => {
      const gallery = document.querySelector("#gallery");
      let previousImages = new Map([...gallery.querySelectorAll(".card")]
        .map(card => [card.dataset.idx, card.querySelector("img")]));
      let identityReplacements = 0;
      let maxCards = 0;
      let maxVisibleIncomplete = 0;
      let addedCards = 0;
      const observer = new MutationObserver(records => {
        for (const record of records) {
          addedCards += [...record.addedNodes]
            .filter(node => node.matches?.(".virtual-item")).length;
        }
      });
      observer.observe(gallery, { childList: true });
      for (let frame = 0; frame < 120; frame += 1) {
        window.scrollBy(0, 80);
        await new Promise(requestAnimationFrame);
        const currentCards = [...gallery.querySelectorAll(".card")];
        const currentImages = new Map(currentCards
          .map(card => [card.dataset.idx, card.querySelector("img")]));
        for (const [index, image] of currentImages) {
          if (previousImages.has(index) && previousImages.get(index) !== image) {
            identityReplacements += 1;
          }
        }
        previousImages = currentImages;
        const visible = currentCards.filter(card => {
          const bounds = card.getBoundingClientRect();
          return bounds.bottom > 0 && bounds.top < innerHeight;
        });
        maxCards = Math.max(maxCards, currentCards.length);
        maxVisibleIncomplete = Math.max(maxVisibleIncomplete,
          visible.filter(card => {
            const image = card.querySelector("img");
            return image && (!image.complete || !image.naturalWidth);
          }).length);
      }
      observer.disconnect();
      return { addedCards, identityReplacements, maxCards, maxVisibleIncomplete };
    });
    assert.equal(scrollMetrics.identityReplacements, 0,
      "scrolling preserves overlapping image elements");
    assert.ok(scrollMetrics.addedCards < 300,
      "scrolling only adds cards entering the overscan window");
    assert.ok(scrollMetrics.maxCards <= 80,
      "continuous scrolling keeps the DOM bounded");
    assert.equal(scrollMetrics.maxVisibleIncomplete, 0,
      "eager overscan keeps visible images loaded during continuous scrolling");

    await page.evaluate(() => window.scrollTo(0, 0));
    const edgeCard = page.locator('.card[data-idx="0"]');
    await edgeCard.click();
    await page.locator("#lb.open").waitFor();
    const lightboxGeometry = await page.evaluate(() => {
      const lightbox = document.querySelector("#lb");
      const stage = document.querySelector("#lbStage").getBoundingClientRect();
      return {
        padding: Number.parseFloat(getComputedStyle(lightbox).paddingLeft),
        stageWidth: stage.width,
        viewportWidth: innerWidth,
      };
    });
    assert.ok(lightboxGeometry.padding <= 8, "desktop lightbox padding is compact");
    assert.ok(lightboxGeometry.stageWidth / lightboxGeometry.viewportWidth > 0.98,
      "lightbox media stage uses the desktop viewport");
    await page.mouse.click(2, 400);
    assert.equal(await page.locator("#lb").evaluate(
      lightbox => lightbox.classList.contains("open")), true,
    "side-edge navigation does not dismiss the lightbox");
    assert.equal(await page.locator("#lbName").textContent(),
      "beta/item-3999.jpg", "desktop side-edge navigation wraps backward");
    await page.locator("#lbClose").click();
    assert.equal(await page.locator("#lb").evaluate(
      lightbox => lightbox.classList.contains("open")), false,
    "the explicit close button dismisses the lightbox");

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

    const touchContext = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    });
    const touchPage = await touchContext.newPage();
    await touchPage.goto(`http://127.0.0.1:${port}/`);
    await touchPage.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("4000 renders"),
    );
    await touchPage.locator('.card[data-idx="0"]').click();
    await touchPage.locator("#lb.open").waitFor();
    await waitForLoadedImages(touchPage, "#lbStage img");
    const phoneGeometry = await touchPage.evaluate(() => {
      const lightbox = document.querySelector("#lb");
      const stage = document.querySelector("#lbStage").getBoundingClientRect();
      const image = document.querySelector("#lbStage img").getBoundingClientRect();
      const close = document.querySelector("#lbClose").getBoundingClientRect();
      return {
        height: lightbox.getBoundingClientRect().height,
        padding: Number.parseFloat(getComputedStyle(lightbox).paddingLeft),
        imageWidth: image.width,
        stageWidth: stage.width,
        closeWidth: close.width,
        closeHeight: close.height,
        viewportHeight: visualViewport?.height || innerHeight,
        viewportWidth: innerWidth,
      };
    });
    assert.ok(phoneGeometry.padding <= 8, "touch lightbox padding is compact");
    assert.ok(phoneGeometry.stageWidth / phoneGeometry.viewportWidth > 0.97,
      "lightbox media stage uses the phone viewport");
    assert.ok(phoneGeometry.imageWidth / phoneGeometry.stageWidth > 0.95,
      "portrait comparison media uses the full stage width");
    assert.ok(Math.abs(phoneGeometry.height - phoneGeometry.viewportHeight) < 1,
      "lightbox follows the dynamic visual viewport");
    assert.ok(phoneGeometry.closeWidth >= 44 && phoneGeometry.closeHeight >= 44,
      "touch close control retains a 44px target");
    await touchPage.touchscreen.tap(2, 422);
    assert.equal(await touchPage.locator("#lb").evaluate(
      lightbox => lightbox.classList.contains("open")), true,
    "touching a phone side-edge does not dismiss the lightbox");
    assert.equal(await touchPage.locator("#lbName").textContent(),
      "beta/item-3999.jpg", "touch side-edge navigation wraps backward");

    await touchPage.setViewportSize({ width: 844, height: 390 });
    const landscapeFits = await touchPage.locator("#lb").evaluate(lightbox =>
      lightbox.scrollWidth <= innerWidth && lightbox.scrollHeight <= innerHeight);
    assert.equal(landscapeFits, true, "lightbox controls fit an iPhone landscape viewport");
    await touchPage.locator("#lbClose").click();
    await touchPage.setViewportSize({ width: 820, height: 1180 });
    await touchPage.locator('.card[data-idx="0"]').click();
    await touchPage.locator("#lb.open").waitFor();
    assert.ok(await touchPage.locator("#lbStage").evaluate(stage =>
      stage.getBoundingClientRect().width / innerWidth > 0.98),
    "lightbox media stage uses the iPad viewport");
    await touchPage.locator("#lbClose").click();
    await touchContext.close();

    console.log(
      `${name} gallery metrics: initial=${initialCardCount}, `
      + `deep=${deepCardCount}, resized=${deepIndexes.length}, `
      + `scrollAdds=${scrollMetrics.addedCards}, `
      + `replacements=${scrollMetrics.identityReplacements}, `
      + `maxCards=${scrollMetrics.maxCards}, logical=4000`,
    );
  } finally {
    releaseInitialStream?.();
    releaseDelayedMetadata?.();
    await browser.close();
    server.close();
    await once(server, "close");
  }
});
}
