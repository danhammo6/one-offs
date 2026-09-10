import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createServer } from "node:http";
import { once } from "node:events";
import test from "node:test";
import { chromium, webkit } from "playwright-core";

const INDEX = readFileSync(new URL("./index.html", import.meta.url));
function imageFixture(width, height, color) {
  return Buffer.from(
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" `
    + `viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" `
    + `fill="${color}"/></svg>`,
  );
}

const LANDSCAPE_IMAGE = imageFixture(1200, 600, "#3578b8");
const LANDSCAPE_32_IMAGE = imageFixture(1536, 1024, "#3578b8");
const PORTRAIT_IMAGE = imageFixture(600, 1200, "#8d4db8");

function fixtureImage(url) {
  const index = Number(url.pathname.match(/item-(\d+)/)?.[1]);
  const reference = url.pathname.includes("/input/");
  const portrait = index === 2001 || index === 2005
    || (index === 2003 && !reference);
  if (index === 8 || index === 2008) return LANDSCAPE_32_IMAGE;
  return portrait ? PORTRAIT_IMAGE : LANDSCAPE_IMAGE;
}
const ITEMS = Array.from({ length: 4000 }, (_, index) => {
  const category = index < 2000 ? "alpha" : "beta";
  const filename = index === 0
    ? "a-deliberately-long-gallery-filename-for-truncation-item-0000.jpg"
    : `item-${String(index).padStart(4, "0")}.jpg`;
  const path = `${category}/${filename}`;
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

async function waitForLightboxSrc(page, pathPart) {
  await page.waitForFunction(part =>
    [...document.querySelectorAll("#lbStage img, #lbStage video")]
      .some(media => (media.getAttribute("src") || "").includes(part)),
  pathPart);
}

async function scrollGallery(page, top) {
  await page.evaluate(scrollTop => {
    document.querySelector("#main").scrollTop = scrollTop;
  }, top);
}

async function waitForLoadedImages(page, selector) {
  await page.waitForFunction(imageSelector =>
    [...document.querySelectorAll(imageSelector)]
      .every(image => image.complete && image.naturalWidth), selector);
}

async function stubVisualViewport(page, viewport) {
  await page.evaluate(next => {
    const current = window.visualViewport;
    window.__restoreVisualViewport = () => {
      Object.defineProperty(window, "visualViewport", {
        configurable: true,
        value: current,
      });
    };
    Object.defineProperty(window, "visualViewport", {
      configurable: true,
      value: {
        offsetLeft: next.offsetLeft,
        offsetTop: next.offsetTop,
        width: next.width,
        height: next.height,
        scale: next.scale,
        pageLeft: next.pageLeft ?? next.offsetLeft,
        pageTop: next.pageTop ?? next.offsetTop,
        addEventListener() {},
        removeEventListener() {},
      },
    });
  }, viewport);
}

async function restoreVisualViewport(page) {
  await page.evaluate(() => window.__restoreVisualViewport?.());
}

async function dispatchLightboxTap(page, clientX, clientY) {
  await page.evaluate(({ clientX, clientY }) => {
    const target = document.querySelector("#lbStage img")
      || document.querySelector("#lbStage");
    const init = {
      bubbles: true,
      cancelable: true,
      pointerId: 1,
      pointerType: "touch",
      isPrimary: true,
      clientX,
      clientY,
      button: 0,
    };
    target.dispatchEvent(new PointerEvent("pointerdown", { ...init, buttons: 1 }));
    target.dispatchEvent(new PointerEvent("pointerup", { ...init, buttons: 0 }));
  }, { clientX, clientY });
}

async function comparisonGeometry(page, expectedLayout) {
  await page.waitForFunction(layout =>
    document.querySelector("#lbStage")?.dataset.comparisonLayout === layout,
  expectedLayout);
  await page.waitForFunction(() =>
    [...document.querySelectorAll("#lbStage img, #lbStage video")]
      .every(media => media.tagName === "IMG"
        ? media.complete && media.naturalWidth && media.naturalHeight
        : media.videoWidth && media.videoHeight));
  return page.evaluate(() => {
    const stage = document.querySelector("#lbStage");
    const figures = [...stage.querySelectorAll("figure")];
    const figureBounds = figures.map(figure => {
      const bounds = figure.getBoundingClientRect();
      return {
        left: bounds.left,
        right: bounds.right,
        top: bounds.top,
        bottom: bounds.bottom,
        width: bounds.width,
      };
    });
    const media = figures.map(figure => {
      const element = figure.querySelector("img, video");
      const bounds = element.getBoundingClientRect();
      const intrinsicWidth = element.naturalWidth || element.videoWidth;
      const intrinsicHeight = element.naturalHeight || element.videoHeight;
      const scale = Math.min(
        bounds.width / intrinsicWidth,
        bounds.height / intrinsicHeight,
      );
      const width = intrinsicWidth * scale;
      const height = intrinsicHeight * scale;
      const objectPosition = getComputedStyle(element).objectPosition;
      const [horizontal = "50%", vertical = "50%"] = objectPosition.split(" ");
      const positionFraction = value => {
        if (value === "left" || value === "top") return 0;
        if (value === "right" || value === "bottom") return 1;
        return Number.parseFloat(value) / 100;
      };
      const left = bounds.left
        + (bounds.width - width) * positionFraction(horizontal);
      const top = bounds.top
        + (bounds.height - height) * positionFraction(vertical);
      return {
        left,
        right: left + width,
        top,
        bottom: top + height,
        width,
        height,
        boxWidth: bounds.width,
        boxHeight: bounds.height,
        intrinsicWidth,
        intrinsicHeight,
        objectFit: getComputedStyle(element).objectFit,
        objectPosition,
        role: element.dataset.comparisonRole,
        tag: element.tagName,
        transform: getComputedStyle(element).transform,
      };
    });
    const stageStyle = getComputedStyle(stage);
    return {
      figures: figureBounds,
      media,
      imagePair: stage.classList.contains("image-pair"),
      stage: {
        gap: Number.parseFloat(stageStyle.gap),
        width: stage.getBoundingClientRect().width,
        height: stage.getBoundingClientRect().height,
      },
      overflow: stage.scrollWidth > stage.clientWidth
        || stage.scrollHeight > stage.clientHeight,
    };
  });
}

function assertImagePairMeetsAtCenter(geometry, label) {
  assert.equal(geometry.imagePair, true, `${label}: image-pair state is active`);
  assert.deepEqual(
    geometry.media.map(media => media.role),
    ["reference", "primary"],
    `${label}: reference/result visual order is unchanged`,
  );
  assert.ok(geometry.media.every(media =>
    media.tag === "IMG"
      && media.objectFit === "contain"
      && media.transform === "none"),
  `${label}: images remain untransformed and uncropped`);
  for (const media of geometry.media) {
    const expectedScale = Math.min(
      media.boxWidth / media.intrinsicWidth,
      media.boxHeight / media.intrinsicHeight,
    );
    assert.ok(
      Math.abs(media.width - media.intrinsicWidth * expectedScale) < 0.6
        && Math.abs(media.height - media.intrinsicHeight * expectedScale) < 0.6,
      `${label}: image dimensions continue to maximize the available box`,
    );
  }
  const centerGap = geometry.media[1].left - geometry.media[0].right;
  assert.ok(geometry.stage.gap >= 5.5 && geometry.stage.gap <= 10.5,
    `${label}: responsive gutter is 6–10px (${geometry.stage.gap}px)`);
  assert.ok(Math.abs(centerGap - geometry.stage.gap) < 0.6,
    `${label}: visible center gutter is small (${centerGap}px)`);
  assert.ok(Math.abs(
    geometry.media[0].right - geometry.figures[0].right,
  ) < 0.6, `${label}: reference is right-aligned`);
  assert.ok(Math.abs(
    geometry.media[1].left - geometry.figures[1].left,
  ) < 0.6, `${label}: result is left-aligned`);
  return geometry.stage.gap;
}

async function lightboxHudGeometry(page) {
  await page.evaluate(() => new Promise(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(resolve))));
  return page.evaluate(() => {
    const bounds = selector =>
      document.querySelector(selector).getBoundingClientRect();
    const state = selector => {
      const element = document.querySelector(selector);
      return {
        hidden: element.hidden,
        inert: element.inert,
        height: element.getBoundingClientRect().height,
      };
    };
    const stage = bounds("#lbStage");
    return {
      visible: !document.querySelector("#lb").classList.contains("hud-hidden"),
      dialogName: document.querySelector("#lb").getAttribute("aria-label"),
      stageLabel: document.querySelector("#lbStage").getAttribute("aria-label"),
      stageShortcuts: document.querySelector("#lbStage")
        .getAttribute("aria-keyshortcuts"),
      activeId: document.activeElement?.id,
      stage: {
        height: stage.height,
      },
      bar: state("#lbBar"),
      utility: state("#lbUtility"),
      prompt: state("#lbPrompt"),
      captionsHidden: [...document.querySelectorAll("#lbStage figcaption")]
        .every(caption => caption.hidden),
    };
  });
}

const comparisonMediaArea = geometry => geometry.media.reduce(
  (area, media) => area + media.width * media.height, 0);

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
  const imageRequests = [];
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
      imageRequests.push(url.pathname);
      const image = fixtureImage(url);
      if (url.pathname.startsWith("/img/output/")
          && url.pathname.includes("item-2004.jpg")) {
        response.writeHead(200, {
          "Content-Type": "image/svg+xml",
          "Cache-Control": "no-store",
        });
        return setTimeout(() => response.end(image), 120);
      }
      response.writeHead(200, { "Content-Type": "image/svg+xml" });
      return response.end(image);
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
    await scrollGallery(page, 0);

    const cards = page.locator("#gallery .card");
    const initialCardCount = await cards.count();
    assert.ok(initialCardCount <= 80, "initial virtual window is bounded");
    await waitForLoadedImages(page, "#gallery img");
    await page.evaluate(() => {
      window.__outputBeforeCompare = document.querySelector(
        '#gallery .card [data-media-role="primary"]')
        || document.querySelector("#gallery .card img");
    });
    await page.locator("#compareAll").check();
    await page.waitForFunction(() => {
      const viewport = [...document.querySelectorAll("#gallery .virtual-item")]
        .filter(node => node.dataset.inView === "1" && node.querySelector(".card"));
      return viewport.length > 0 && viewport.every(node =>
        node.querySelector('[data-media-role="reference"]')?.getAttribute("src"));
    });
    assert.deepEqual(await page.evaluate(() => {
      const card = document.querySelector("#gallery .card");
      const primary = card.querySelector('[data-media-role="primary"]');
      const reference = card.querySelector('[data-media-role="reference"]');
      const viewport = [...document.querySelectorAll("#gallery .virtual-item")]
        .filter(node => node.dataset.inView === "1" && node.querySelector(".card"));
      const overscan = [...document.querySelectorAll("#gallery .virtual-item")]
        .filter(node => node.dataset.inView !== "1" && node.querySelector(".card"));
      return {
        count: card.querySelectorAll("img").length,
        sameOutput: primary === window.__outputBeforeCompare,
        viewportRefsHaveSrc: viewport.every(node =>
          node.querySelector('[data-media-role="reference"]')?.getAttribute("src")),
        overscanRefsHaveSrc: overscan.some(node =>
          node.querySelector('[data-media-role="reference"]')?.getAttribute("src")),
        primaryPriority: primary.fetchPriority,
        refPriority: reference.fetchPriority,
      };
    }), {
      count: 2,
      sameOutput: true,
      viewportRefsHaveSrc: true,
      overscanRefsHaveSrc: false,
      primaryPriority: "high",
      refPriority: "low",
    }, "compare in grid keeps result thumbs and loads viewport references after");
    await page.locator("#compareAll").uncheck();
    await page.waitForFunction(() =>
      !document.querySelector("#gallery .imgwrap.compare"));
    assert.equal(await page.evaluate(() => {
      const card = document.querySelector("#gallery .card");
      return card.querySelector("img") === window.__outputBeforeCompare
        && card.querySelectorAll("img").length === 1;
    }), true, "turning off compare in grid keeps the result thumbnail");
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
        document.querySelector("#main").scrollBy(0, 80);
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

    await page.evaluate(() => new Promise(requestAnimationFrame));
    const requestsBeforeJump = imageRequests.length;
    await page.evaluate(async () => {
      const el = document.querySelector("#main");
      el.scrollTop = el.scrollHeight;
      await new Promise(requestAnimationFrame);
    });
    const jumpMedia = await page.evaluate(() => {
      const srcOf = media =>
        media.dataset.pendingSrc || media.currentSrc || media.src;
      const cards = [...document.querySelectorAll("#gallery .card")];
      const visible = cards
        .filter(card => {
          const bounds = card.getBoundingClientRect();
          return bounds.bottom > 0 && bounds.top < innerHeight;
        })
        .flatMap(card => [...card.querySelectorAll("img, video")].map(srcOf))
        .filter(Boolean);
      const windowed = cards
        .flatMap(card => [...card.querySelectorAll("img, video")].map(srcOf))
        .filter(Boolean);
      return { visible, windowed };
    });
    const jumpedRequests = imageRequests.slice(requestsBeforeJump)
      .filter(path => jumpMedia.windowed.some(src => src.includes(path)));
    assert.ok(jumpMedia.visible.length, "end of gallery has visible cards");
    assert.ok(jumpedRequests.length, "jumping to the end starts media requests");
    const earlyJumpRequests = jumpedRequests.slice(0, jumpMedia.visible.length);
    assert.ok(
      earlyJumpRequests.every(path =>
        jumpMedia.visible.some(src => src.includes(path))),
      "viewport thumbnails are requested before overscan after a long jump");

    await scrollGallery(page, 0);
    const edgeCard = page.locator('.card[data-idx="0"]');
    await edgeCard.click();
    await page.locator("#lb.open").waitFor();
    const lightboxGeometry = await page.evaluate(() => {
      const lightbox = document.querySelector("#lb");
      const stage = document.querySelector("#lbStage").getBoundingClientRect();
      return {
        padding: Number.parseFloat(getComputedStyle(lightbox).paddingLeft),
        stageWidth: stage.width,
        stageHeight: stage.height,
        viewportWidth: innerWidth,
        viewportHeight: innerHeight,
        promptExpanded: document.querySelector("#lbPrompt")
          .classList.contains("expanded"),
      };
    });
    assert.ok(lightboxGeometry.padding <= 8, "desktop lightbox padding is compact");
    assert.ok(lightboxGeometry.stageWidth / lightboxGeometry.viewportWidth > 0.98,
      "lightbox media stage uses the desktop viewport");
    assert.ok(lightboxGeometry.stageHeight
      / lightboxGeometry.viewportHeight >= 0.84,
    "collapsed overlay prompt preserves desktop stage height");
    assert.equal(lightboxGeometry.promptExpanded, false);
    const desktopHudVisible = await lightboxHudGeometry(page);
    const desktopVisibleMedia = await comparisonGeometry(page, "stacked");
    assert.equal(desktopHudVisible.visible, true);
    assert.equal(desktopHudVisible.dialogName,
      "Viewing alpha/a-deliberately-long-gallery-filename-for-truncation-item-0000.jpg");
    assert.equal(desktopHudVisible.stageShortcuts, "H Enter Space");
    const desktopQuarter = lightboxGeometry.viewportWidth * 0.25;
    const desktopCenterY = lightboxGeometry.viewportHeight * 0.5;

    await page.mouse.click(desktopQuarter - 1, desktopCenterY);
    assert.equal(await page.locator("#lb").evaluate(
      lightbox => lightbox.classList.contains("open")), true,
    "side-edge taps do not dismiss the lightbox");
    assert.equal(await page.locator("#lbName").textContent(),
      "alpha/a-deliberately-long-gallery-filename-for-truncation-item-0000.jpg",
    "the first left-edge tap hides the HUD instead of navigating");
    const desktopHudHidden = await lightboxHudGeometry(page);
    const desktopHiddenMedia = await comparisonGeometry(page, "stacked");
    assert.equal(desktopHudHidden.visible, false,
      "the first left/right gesture dismisses the HUD");
    assert.equal(desktopHudHidden.bar.hidden && desktopHudHidden.bar.inert, true);
    assert.equal(
      desktopHudHidden.utility.hidden && desktopHudHidden.utility.inert, true);
    assert.equal(
      desktopHudHidden.prompt.hidden && desktopHudHidden.prompt.inert, true);
    assert.equal(desktopHudHidden.captionsHidden, true);
    assert.equal(desktopHudHidden.bar.height, 0);
    assert.equal(desktopHudHidden.utility.height, 0);
    assert.ok(desktopHudHidden.stage.height > desktopHudVisible.stage.height + 90,
      "hidden desktop HUD reclaims chrome and label space");
    assert.ok(comparisonMediaArea(desktopHiddenMedia)
      > comparisonMediaArea(desktopVisibleMedia),
    "hidden desktop HUD materially enlarges media");
    assert.ok(desktopHudHidden.stageLabel.includes("Controls hidden"));
    assert.equal(desktopHudHidden.dialogName,
      "Viewing alpha/a-deliberately-long-gallery-filename-for-truncation-item-0000.jpg",
      "the dialog keeps an accessible name while filename chrome is hidden");

    await page.mouse.click(desktopQuarter - 1, desktopCenterY);
    assert.equal(await page.locator("#lbName").textContent(),
      "beta/item-3999.jpg", "desktop side-edge navigation wraps backward");
    assert.equal((await lightboxHudGeometry(page)).visible, false,
      "edge navigation preserves hidden HUD state");
    await waitForLightboxSrc(page, "item-3999");
    await page.mouse.click(desktopQuarter, desktopCenterY);
    assert.equal((await lightboxHudGeometry(page)).visible, true,
      "the exact 25% boundary belongs to the center HUD zone");

    await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator("#lbName").textContent(),
      "beta/item-3999.jpg",
    "the first Left/Right Arrow hides the HUD instead of navigating");
    assert.equal((await lightboxHudGeometry(page)).visible, false);
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator("#lbName").textContent(),
      "alpha/a-deliberately-long-gallery-filename-for-truncation-item-0000.jpg");
    assert.equal((await lightboxHudGeometry(page)).visible, false,
      "keyboard navigation preserves hidden HUD state");
    await waitForLightboxSrc(page, "item-0000");
    await page.mouse.click(desktopQuarter * 3 - 1, desktopCenterY);
    assert.equal((await lightboxHudGeometry(page)).visible, true,
      "the center zone extends through the pixel before 75%");
    await page.mouse.click(desktopQuarter * 3, desktopCenterY);
    assert.equal(await page.locator("#lbName").textContent(),
      "alpha/a-deliberately-long-gallery-filename-for-truncation-item-0000.jpg",
    "the first right-edge tap hides the HUD instead of navigating");
    assert.equal((await lightboxHudGeometry(page)).visible, false);
    await page.mouse.click(desktopQuarter * 3, desktopCenterY);
    assert.equal(await page.locator("#lbName").textContent(),
      "alpha/item-0001.jpg",
    "the exact 75% boundary belongs to next navigation");
    assert.equal((await lightboxHudGeometry(page)).visible, false,
      "edge navigation does not show the HUD");

    await page.mouse.move(600, 400);
    await page.mouse.down();
    await page.mouse.move(640, 440, { steps: 4 });
    await page.mouse.up();
    assert.equal(await page.locator("#lbName").textContent(), "alpha/item-0001.jpg");
    assert.equal((await lightboxHudGeometry(page)).visible, false,
      "mouse drag is not treated as a center tap");

    const layoutZoom = {
      offsetLeft: 280,
      offsetTop: 40,
      width: 417,
      height: 597,
      scale: 2,
    };
    await stubVisualViewport(page, layoutZoom);
    await dispatchLightboxTap(
      page,
      layoutZoom.offsetLeft + layoutZoom.width * 0.9,
      layoutZoom.height * 0.5,
    );
    assert.equal(await page.locator("#lbName").textContent(), "alpha/item-0002.jpg",
      "layout-viewport clientX still treats a zoomed right-edge tap as next");
    assert.equal((await lightboxHudGeometry(page)).visible, false,
      "zoomed right-edge navigation does not toggle the HUD");
    await dispatchLightboxTap(
      page,
      layoutZoom.offsetLeft + layoutZoom.width * 0.1,
      layoutZoom.height * 0.5,
    );
    assert.equal(await page.locator("#lbName").textContent(), "alpha/item-0001.jpg",
      "layout-viewport clientX still treats a zoomed left-edge tap as previous");
    await dispatchLightboxTap(
      page,
      layoutZoom.offsetLeft + layoutZoom.width * 0.5,
      layoutZoom.height * 0.5,
    );
    assert.equal((await lightboxHudGeometry(page)).visible, true,
      "layout-viewport clientX still treats a zoomed center tap as HUD");
    await restoreVisualViewport(page);

    await page.locator("#lbClose").focus();
    await page.keyboard.press("h");
    assert.equal((await lightboxHudGeometry(page)).activeId, "lbStage",
      "hiding focused chrome moves focus to the stable media stage");
    await page.keyboard.press("Tab");
    assert.equal((await lightboxHudGeometry(page)).activeId, "lbStage",
      "hidden chrome is absent from keyboard focus order");
    await page.keyboard.press("Enter");
    assert.equal((await lightboxHudGeometry(page)).visible, true,
      "Enter toggles the HUD from the focused media stage");
    await page.keyboard.press("Space");
    assert.equal((await lightboxHudGeometry(page)).visible, false,
      "Space also toggles the HUD from the focused media stage");
    await page.keyboard.press("H");
    assert.equal((await lightboxHudGeometry(page)).visible, true);
    await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator("#lbName").textContent(), "alpha/item-0001.jpg",
      "showing the HUD again makes the next Left/Right hide it");
    assert.equal((await lightboxHudGeometry(page)).visible, false);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#lb").evaluate(
      lightbox => lightbox.classList.contains("open")), false,
    "Escape closes while the HUD is hidden");
    await edgeCard.click();
    assert.equal((await lightboxHudGeometry(page)).visible, true,
      "a new lightbox session resets the HUD visible");
    await page.locator("#lbClose").click();
    assert.equal(await page.locator("#lb").evaluate(
      lightbox => lightbox.classList.contains("open")), false,
    "the explicit close button dismisses the lightbox");

    await page.evaluate(() => {
      const root = document.querySelector("#main");
      root.scrollTop = root.scrollHeight * 0.7;
    });
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

    await page.setViewportSize({ width: 1200, height: 800 });
    await page.evaluate(() => focusLogicalCard(6));
    await page.waitForFunction(
      () => document.activeElement?.matches('.card[data-idx="6"]'),
    );
    await page.locator('.card[data-idx="6"]').press("Enter");
    await waitForLoadedImages(page, "#lbStage img");
    const landscapeComparison = await comparisonGeometry(page, "stacked");
    assert.ok(landscapeComparison.figures[1].top
      >= landscapeComparison.figures[0].bottom,
    "landscape media compares top-to-bottom");
    assert.equal(landscapeComparison.overflow, false);
    assert.deepEqual(
      landscapeComparison.media.map(media => media.objectPosition),
      ["50% 50%", "50% 50%"],
      "stacked landscape media remains centered",
    );

    await page.keyboard.press("h");
    await page.keyboard.press("ArrowLeft");
    await page.waitForFunction(
      () => document.querySelector("#lbName")?.textContent
        === "beta/item-2005.jpg",
    );
    const portraitComparison = await comparisonGeometry(page, "side-by-side");
    assert.ok(portraitComparison.figures[1].left
      >= portraitComparison.figures[0].right,
    "portrait media compares side-by-side");
    assert.equal(portraitComparison.overflow, false);
    const desktopPortraitGap = assertImagePairMeetsAtCenter(
      portraitComparison, "desktop portrait comparison",
    );

    await page.keyboard.press("ArrowRight");
    assert.equal(await page.locator("#lbName").textContent(), "beta/item-2006.jpg",
      "the filename updates before the next stills replace the stage");
    await page.waitForFunction(() =>
      [...document.querySelectorAll("#lbStage img")]
        .some(image => (image.getAttribute("src") || "").includes("item-2006")));
    assert.equal(await page.locator("#lbStage").getAttribute(
      "data-comparison-layout"), "stacked",
    "new stills paint with the cached landscape layout");

    await page.evaluate(() => openLb(3));
    await waitForLoadedImages(page, "#lbStage img");
    assert.equal((await comparisonGeometry(page, "side-by-side")).overflow, false,
      "output orientation controls mismatched source/result layout");

    await page.evaluate(() => openLb(4));
    const heldNavigation = await page.evaluate(() => {
      const images = [...document.querySelectorAll("#lbStage img")];
      return {
        name: document.querySelector("#lbName").textContent,
        painted: images.length > 0 && images.every(image => image.naturalWidth > 0),
        pendingOutput: images.some(image =>
          (image.getAttribute("src") || "").includes("item-2004")
          && (image.getAttribute("src") || "").includes("/output/")),
      };
    });
    assert.equal(heldNavigation.name, "beta/item-2004.jpg");
    assert.ok(heldNavigation.painted,
      "navigation keeps painted media while the next still loads");
    assert.equal(heldNavigation.pendingOutput, false,
      "the empty next output is not shown before it can paint");
    await page.waitForFunction(() =>
      [...document.querySelectorAll("#lbStage img")].some(image =>
        (image.getAttribute("src") || "").includes("item-2004")
        && (image.getAttribute("src") || "").includes("/output/")));
    await page.evaluate(() => openLb(3));
    await comparisonGeometry(page, "side-by-side");

    await page.keyboard.press("h");
    await page.locator("#lbSide").uncheck();
    assert.deepEqual(await page.evaluate(() => ({
      layout: document.querySelector("#lbStage").dataset.comparisonLayout,
      figures: document.querySelectorAll("#lbStage figure").length,
      imagePair: document.querySelector("#lbStage").classList.contains("image-pair"),
      objectPosition: getComputedStyle(
        document.querySelector("#lbStage img"),
      ).objectPosition,
    })), {
      layout: "single",
      figures: 1,
      imagePair: false,
      objectPosition: "50% 50%",
    });
    await page.locator("#lbSide").check();
    await comparisonGeometry(page, "side-by-side");

    await page.evaluate(() => {
      MEDIA_DIMENSIONS.clear();
      openLb(4);
      setTimeout(() => openLb(5), 10);
    });
    await page.waitForFunction(
      () => document.querySelector("#lbName")?.textContent
        === "beta/item-2005.jpg",
    );
    await page.waitForTimeout(200);
    assert.equal(await page.locator("#lbStage").getAttribute(
      "data-comparison-layout"), "side-by-side",
    "a late load from the previous item cannot change the current layout");
    await page.keyboard.press("Escape");

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

    await page.keyboard.press("h");
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
    assert.deepEqual(await page.locator("#lbStage").evaluate(stage => ({
      imagePair: stage.classList.contains("image-pair"),
      referencePosition: getComputedStyle(
        stage.querySelector('[data-comparison-role="reference"]'),
      ).objectPosition,
    })), {
      imagePair: false,
      referencePosition: "50% 50%",
    }, "video comparison keeps its previous centering behavior");
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
    await lightboxVideo.click({ position: { x: 20, y: 20 } });
    assert.equal((await lightboxHudGeometry(page)).visible, true,
      "video interactions do not trigger HUD hit zones");
    await page.keyboard.press("h");
    assert.equal((await lightboxHudGeometry(page)).visible, false);
    assert.equal(await lightboxVideo.getAttribute("controls"), "",
      "video controls remain available when HUD chrome is hidden");
    await page.keyboard.press("ArrowLeft");
    await page.waitForFunction(
      () => document.querySelector("#lbName")?.textContent
        === "beta/item-2000.jpg"
        && document.querySelector("#lbStage video") === null,
    );
    assert.deepEqual(await page.locator("#lbStage").evaluate(stage => ({
      hidden: document.querySelector("#lb").classList.contains("hud-hidden"),
      focused: document.activeElement === stage,
    })), { hidden: true, focused: true },
    "hidden HUD and stable media focus survive video navigation");
    await page.keyboard.press("ArrowRight");
    await lightboxVideo.waitFor();
    assert.equal((await lightboxHudGeometry(page)).visible, false);
    await page.keyboard.press("h");
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
    const compactChrome = await page.evaluate(() => {
      const bounds = selector =>
        document.querySelector(selector).getBoundingClientRect();
      const title = document.querySelector("#lbName");
      const titleStyle = getComputedStyle(title);
      const stage = bounds("#lbStage");
      const utility = bounds("#lbUtility");
      const hint = bounds("#lbHint");
      const toggle = bounds("#lbPromptToggle");
      return {
        stageHeight: stage.height,
        stageBottom: stage.bottom,
        utilityTop: utility.top,
        utilityBottom: utility.bottom,
        hintTop: hint.top,
        hintBottom: hint.bottom,
        hintRight: hint.right,
        toggleTop: toggle.top,
        toggleBottom: toggle.bottom,
        toggleLeft: toggle.left,
        toggleWidth: toggle.width,
        toggleHeight: toggle.height,
        title: title.textContent,
        titleAttribute: title.title,
        titleFontSize: Number.parseFloat(titleStyle.fontSize),
        titleWhiteSpace: titleStyle.whiteSpace,
        titleOverflow: titleStyle.overflow,
      };
    });
    assert.ok(compactChrome.stageBottom <= compactChrome.utilityTop);
    assert.ok(compactChrome.hintRight <= compactChrome.toggleLeft);
    assert.ok(compactChrome.hintTop >= compactChrome.utilityTop
      && compactChrome.hintBottom <= compactChrome.utilityBottom);
    assert.ok(compactChrome.toggleTop >= compactChrome.utilityTop
      && compactChrome.toggleBottom <= compactChrome.utilityBottom);
    assert.ok(compactChrome.toggleWidth >= 44
      && compactChrome.toggleHeight >= 44,
    "prompt toggle retains a 44px pointer target");
    assert.equal(compactChrome.titleAttribute, compactChrome.title);
    assert.ok(compactChrome.titleFontSize >= 11
      && compactChrome.titleFontSize <= 12);
    assert.equal(compactChrome.titleWhiteSpace, "nowrap");
    assert.equal(compactChrome.titleOverflow, "hidden");
    await page.locator("#lbClose").press("Tab");
    assert.equal(await page.locator("#lbStage").evaluate(
      element => element === document.activeElement), true,
    "the keyboard-discoverable media stage follows header controls");
    await page.locator("#lbStage").press("Tab");
    assert.equal(await promptButton.evaluate(
      element => element === document.activeElement), true);
    await promptButton.press("Enter");
    assert.equal(await promptButton.getAttribute("aria-expanded"), "true");
    assert.equal(await promptButton.evaluate(
      element => element === document.activeElement), true);
    assert.equal(await page.locator("#lbPrompt").getAttribute("tabindex"), "0");
    await promptButton.press("Tab");
    assert.equal(await page.locator("#lbPrompt").evaluate(
      element => element === document.activeElement), true,
    "expanded scrollable prompt is keyboard reachable");
    await page.locator("#lbPrompt").press("Shift+Tab");
    assert.equal(await promptButton.evaluate(
      element => element === document.activeElement), true);
    const expandedPrompt = await page.evaluate(() => {
      const stage = document.querySelector("#lbStage").getBoundingClientRect();
      const utility = document.querySelector("#lbUtility").getBoundingClientRect();
      const prompt = document.querySelector("#lbPrompt").getBoundingClientRect();
      return {
        stageHeight: stage.height,
        promptBottom: prompt.bottom,
        utilityTop: utility.top,
      };
    });
    assert.ok(Math.abs(
      expandedPrompt.stageHeight - compactChrome.stageHeight) < 1,
    "expanded prompt overlays rather than shrinking the media stage");
    assert.ok(expandedPrompt.promptBottom <= expandedPrompt.utilityTop,
      "expanded prompt does not overlap its utility-row control");
    const promptItemName = await page.locator("#lbName").textContent();
    const compactViewport = page.viewportSize();
    const compactCenter = {
      x: compactViewport.width * 0.5,
      y: compactViewport.height * 0.25,
    };
    await page.locator("#lbPromptBody").click();
    assert.equal(await promptButton.getAttribute("aria-expanded"), "true",
      "pointer interaction inside prompt content does not toggle HUD or prompt");
    assert.equal((await lightboxHudGeometry(page)).visible, true);
    assert.equal(await page.locator("#lbName").textContent(), promptItemName);
    await page.mouse.click(compactCenter.x, compactCenter.y);
    assert.equal(await promptButton.getAttribute("aria-expanded"), "false",
      "first center tap closes the prompt");
    assert.equal((await lightboxHudGeometry(page)).visible, true,
      "closing the prompt leaves the HUD visible");
    await page.mouse.click(compactCenter.x, compactCenter.y);
    const portraitHudHidden = await lightboxHudGeometry(page);
    assert.equal(portraitHudHidden.visible, false,
      "second center tap hides the HUD");
    assert.ok(portraitHudHidden.stage.height > compactChrome.stageHeight + 90,
      "hidden portrait HUD reclaims chrome space");
    await page.mouse.click(compactCenter.x, compactCenter.y);
    assert.equal((await lightboxHudGeometry(page)).visible, true);
    await page.locator("#lbHint").click();
    assert.equal((await lightboxHudGeometry(page)).visible, true,
      "navigation hint chrome does not trigger the center zone");
    assert.equal(await page.locator("#lbName").textContent(), promptItemName);
    await promptButton.focus();
    await promptButton.press("Enter");
    assert.equal(await promptButton.getAttribute("aria-expanded"), "true");
    await page.setViewportSize({ width: 1000, height: 600 });
    assert.equal(await promptButton.evaluate(
      element => element === document.activeElement), true);
    await promptButton.press("Space");
    assert.equal(await promptButton.getAttribute("aria-expanded"), "false");
    assert.equal(await page.locator("#lbPrompt").getAttribute("tabindex"), "-1");

    assert.equal(await page.locator("#lbPromptBody").getAttribute("id"), "lbPromptBody");
    assert.equal(await page.locator("#lbPromptToggle").getAttribute("aria-controls"),
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
    await page.locator("#lbStage").press("Tab");
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
      && document.querySelector("#lbPromptToggle") === window.__focusedPromptNode
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
      const utility = document.querySelector("#lbUtility").getBoundingClientRect();
      const hint = document.querySelector("#lbHint").getBoundingClientRect();
      const toggle = document.querySelector("#lbPromptToggle").getBoundingClientRect();
      const title = document.querySelector("#lbName");
      return {
        height: lightbox.getBoundingClientRect().height,
        padding: Number.parseFloat(getComputedStyle(lightbox).paddingLeft),
        imageWidth: image.width,
        stageWidth: stage.width,
        stageHeight: stage.height,
        closeWidth: close.width,
        closeHeight: close.height,
        viewportHeight: visualViewport?.height || innerHeight,
        viewportWidth: innerWidth,
        stageBottom: stage.bottom,
        utilityTop: utility.top,
        hintRight: hint.right,
        toggleLeft: toggle.left,
        promptWidth: toggle.width,
        promptHeight: toggle.height,
        title: title.textContent,
        titleAttribute: title.title,
        titleClientWidth: title.clientWidth,
        titleScrollWidth: title.scrollWidth,
      };
    });
    assert.ok(phoneGeometry.padding <= 8, "touch lightbox padding is compact");
    assert.ok(phoneGeometry.stageWidth
      >= phoneGeometry.viewportWidth - phoneGeometry.padding * 2 - 1,
    "lightbox media stage uses the phone viewport within its safe padding");
    assert.ok(phoneGeometry.stageHeight / phoneGeometry.viewportHeight >= 0.85,
      "combined utility row preserves phone-portrait stage height");
    assert.ok(phoneGeometry.imageWidth / phoneGeometry.stageWidth > 0.95,
      "portrait comparison media uses the full stage width");
    assert.ok(Math.abs(phoneGeometry.height - phoneGeometry.viewportHeight) < 1,
      "lightbox follows the dynamic visual viewport");
    assert.ok(phoneGeometry.closeWidth >= 44 && phoneGeometry.closeHeight >= 44,
      "touch close control retains a 44px target");
    assert.ok(phoneGeometry.promptWidth >= 44 && phoneGeometry.promptHeight >= 44,
      "touch prompt control retains a 44px target");
    assert.ok(phoneGeometry.stageBottom <= phoneGeometry.utilityTop
      && phoneGeometry.hintRight <= phoneGeometry.toggleLeft,
    "combined utility row does not overlap the stage or prompt control");
    assert.equal(phoneGeometry.titleAttribute, phoneGeometry.title);
    assert.ok(phoneGeometry.titleScrollWidth > phoneGeometry.titleClientWidth,
      "long phone filename is visually truncated with full title available");
    const phoneCenter = {
      x: phoneGeometry.viewportWidth * 0.5,
      y: phoneGeometry.viewportHeight * 0.5,
    };
    const phoneQuarter = phoneGeometry.viewportWidth * 0.25;
    const phonePortraitHudVisible = await lightboxHudGeometry(touchPage);
    const phonePortraitMediaVisible = await comparisonGeometry(
      touchPage, "stacked");
    await touchPage.touchscreen.tap(phoneCenter.x, phoneCenter.y);
    const phonePortraitHudHidden = await lightboxHudGeometry(touchPage);
    const phonePortraitMediaHidden = await comparisonGeometry(
      touchPage, "stacked");
    assert.equal(phonePortraitHudHidden.visible, false);
    assert.ok(phonePortraitHudHidden.stage.height
      > phonePortraitHudVisible.stage.height + 90,
    "hidden iPhone portrait HUD reclaims chrome space");
    assert.ok(comparisonMediaArea(phonePortraitMediaHidden)
      >= comparisonMediaArea(phonePortraitMediaVisible) - 1,
    "hidden iPhone portrait HUD never reduces media");

    await touchPage.evaluate(({ x, y }) => {
      const stage = document.querySelector("#lbStage");
      const dispatch = (type, clientX, clientY, buttons) => stage.dispatchEvent(
        new PointerEvent(type, {
          bubbles: true,
          button: 0,
          buttons,
          clientX,
          clientY,
          isPrimary: true,
          pointerId: 77,
          pointerType: "touch",
        }),
      );
      dispatch("pointerdown", x, y, 1);
      dispatch("pointermove", x + 30, y + 40, 1);
      dispatch("pointerup", x + 30, y + 40, 0);
    }, phoneCenter);
    assert.equal((await lightboxHudGeometry(touchPage)).visible, false,
      "touch drag beyond tap slop does not toggle the HUD");
    assert.equal(await touchPage.locator("#lbName").textContent(),
      "alpha/a-deliberately-long-gallery-filename-for-truncation-item-0000.jpg");

    await touchPage.touchscreen.tap(Math.floor(phoneQuarter), phoneCenter.y);
    assert.equal(await touchPage.locator("#lb").evaluate(
      lightbox => lightbox.classList.contains("open")), true,
    "touching a phone side-edge does not dismiss the lightbox");
    assert.equal(await touchPage.locator("#lbName").textContent(),
      "beta/item-3999.jpg", "touch side-edge navigation wraps backward");
    assert.equal((await lightboxHudGeometry(touchPage)).visible, false,
      "touch edge navigation preserves hidden HUD state");
    await waitForLightboxSrc(touchPage, "item-3999");
    await touchPage.touchscreen.tap(Math.ceil(phoneQuarter), phoneCenter.y);
    assert.equal((await lightboxHudGeometry(touchPage)).visible, true,
      "the first integer pixel inside the phone center band toggles HUD");
    await touchPage.touchscreen.tap(
      Math.ceil(phoneQuarter * 3), phoneCenter.y);
    assert.equal(await touchPage.locator("#lbName").textContent(),
      "beta/item-3999.jpg",
    "the first right-edge tap hides the HUD instead of navigating");
    assert.equal((await lightboxHudGeometry(touchPage)).visible, false);
    await touchPage.touchscreen.tap(
      Math.ceil(phoneQuarter * 3), phoneCenter.y);
    assert.equal(await touchPage.locator("#lbName").textContent(),
      "alpha/a-deliberately-long-gallery-filename-for-truncation-item-0000.jpg",
    "the first integer pixel in the right 25% navigates");
    assert.equal((await lightboxHudGeometry(touchPage)).visible, false,
      "touch edge navigation preserves hidden HUD state");
    await waitForLightboxSrc(touchPage, "item-0000");
    await touchPage.touchscreen.tap(phoneCenter.x, phoneCenter.y);
    assert.equal((await lightboxHudGeometry(touchPage)).visible, true);

    await touchPage.setViewportSize({ width: 844, height: 390 });
    const phoneLandscape = await comparisonGeometry(
      touchPage, "side-by-side");
    assert.ok(phoneLandscape.figures[1].left
      >= phoneLandscape.figures[0].right,
    "short-wide iPhone landscape overrides landscape media to columns");
    const shortWideGap = assertImagePairMeetsAtCenter(
      phoneLandscape, "short-wide iPhone landscape comparison",
    );
    assert.equal(await touchPage.locator("#lbStage").getAttribute(
      "data-comparison-reason"), "short-wide");
    const compactLandscape = await touchPage.evaluate(() => {
      const stage = document.querySelector("#lbStage").getBoundingClientRect();
      const utility = document.querySelector("#lbUtility").getBoundingClientRect();
      const toggle = document.querySelector("#lbPromptToggle").getBoundingClientRect();
      return {
        stageHeight: stage.height,
        stageRatio: stage.height / innerHeight,
        chromeHeight: innerHeight - stage.height,
        stageBottom: stage.bottom,
        utilityTop: utility.top,
        toggleWidth: toggle.width,
        toggleHeight: toggle.height,
      };
    });
    assert.ok(compactLandscape.stageHeight >= 269
      && compactLandscape.stageRatio >= 0.69
      && compactLandscape.chromeHeight <= 121,
    "compact phone-landscape chrome materially increases media-stage height");
    assert.ok(compactLandscape.stageBottom <= compactLandscape.utilityTop);
    assert.ok(compactLandscape.toggleWidth >= 44
      && compactLandscape.toggleHeight >= 44);
    const landscapeViewport = touchPage.viewportSize();
    const landscapeCenter = {
      x: landscapeViewport.width * 0.5,
      y: landscapeViewport.height * 0.5,
    };
    await touchPage.touchscreen.tap(landscapeCenter.x, landscapeCenter.y);
    const phoneLandscapeHudHidden = await lightboxHudGeometry(touchPage);
    const phoneLandscapeMediaHidden = await comparisonGeometry(
      touchPage, "side-by-side");
    assert.equal(phoneLandscapeHudHidden.visible, false);
    assert.ok(phoneLandscapeHudHidden.stage.height
      > compactLandscape.stageHeight + 90,
    "hidden iPhone landscape HUD immediately gives space to media");
    assert.ok(comparisonMediaArea(phoneLandscapeMediaHidden)
      >= comparisonMediaArea(phoneLandscape) - 1,
    "hidden HUD never reduces short-wide media");
    await touchPage.touchscreen.tap(landscapeCenter.x, landscapeCenter.y);
    assert.equal((await lightboxHudGeometry(touchPage)).visible, true);
    await touchPage.setViewportSize({ width: 844, height: 420 });
    assert.equal(await touchPage.locator("#lbStage").getAttribute(
      "data-comparison-layout"), "side-by-side",
    "address-bar-sized viewport changes preserve the short-wide layout");
    const landscapeFits = await touchPage.locator("#lb").evaluate(lightbox =>
      lightbox.scrollWidth <= innerWidth && lightbox.scrollHeight <= innerHeight);
    assert.equal(landscapeFits, true, "lightbox controls fit an iPhone landscape viewport");
    await touchPage.locator("#lbClose").click();

    await touchPage.setViewportSize({ width: 390, height: 844 });
    await touchPage.locator("#catFilter").selectOption("beta");
    await touchPage.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("2000 in beta"),
    );
    await touchPage.locator('.card[data-idx="1"]').click();
    await waitForLoadedImages(touchPage, "#lbStage img");
    const narrowPortrait = await comparisonGeometry(touchPage, "side-by-side");
    assert.equal(narrowPortrait.overflow, false,
      "portrait comparison fits the narrow iPhone viewport");
    assert.ok(narrowPortrait.figures.every(figure => figure.width >= 180),
      "narrow iPhone retains usable side-by-side portrait columns");
    const phonePortraitGap = assertImagePairMeetsAtCenter(
      narrowPortrait, "iPhone portrait comparison",
    );
    await touchPage.setViewportSize({ width: 844, height: 390 });
    const phonePortraitLandscape = await comparisonGeometry(
      touchPage, "side-by-side");
    const phoneLandscapePortraitGap = assertImagePairMeetsAtCenter(
      phonePortraitLandscape, "portrait media in iPhone landscape",
    );
    const phonePortraitLandscapeHudVisible =
      await lightboxHudGeometry(touchPage);
    await touchPage.touchscreen.tap(landscapeCenter.x, landscapeCenter.y);
    const phonePortraitLandscapeHudHidden =
      await lightboxHudGeometry(touchPage);
    const phonePortraitLandscapeMediaHidden = await comparisonGeometry(
      touchPage, "side-by-side");
    assert.ok(phonePortraitLandscapeHudHidden.stage.height
      > phonePortraitLandscapeHudVisible.stage.height + 90);
    assert.ok(comparisonMediaArea(phonePortraitLandscapeMediaHidden)
      > comparisonMediaArea(phonePortraitLandscape),
    "hidden iPhone landscape HUD enlarges portrait media");
    await touchPage.touchscreen.tap(landscapeCenter.x, landscapeCenter.y);
    await touchPage.setViewportSize({ width: 390, height: 844 });
    await touchPage.locator("#lbClose").click();

    await touchPage.setViewportSize({ width: 820, height: 1180 });
    const galleryChrome = await touchPage.evaluate(() => {
      const header = document.querySelector("header").getBoundingClientRect();
      const main = document.querySelector("#main").getBoundingClientRect();
      return {
        headerBottom: header.bottom,
        mainTop: main.top,
        scrollbarWidth: getComputedStyle(
          document.querySelector("#main"), "::-webkit-scrollbar").width,
      };
    });
    assert.ok(Math.abs(galleryChrome.mainTop - galleryChrome.headerBottom) <= 1,
      "gallery scrollbar starts just below the header controls");
    assert.equal(galleryChrome.scrollbarWidth, "88px",
      "touch gallery scrollbar uses an 88px hit target");
    await touchPage.locator('.card[data-idx="0"]').click();
    await touchPage.locator("#lb.open").waitFor();
    assert.ok(await touchPage.locator("#lbStage").evaluate(stage =>
      stage.getBoundingClientRect().width / innerWidth > 0.98),
    "lightbox media stage uses the iPad viewport");
    assert.ok(await touchPage.locator("#lbStage").evaluate(stage =>
      stage.getBoundingClientRect().height / innerHeight > 0.895),
    "combined utility row preserves iPad stage height");
    assert.equal((await comparisonGeometry(touchPage, "stacked")).overflow, false,
      "landscape comparison stacks on iPad");
    await touchPage.keyboard.press("h");
    await touchPage.keyboard.press("ArrowRight");
    const iPadPortrait = await comparisonGeometry(touchPage, "side-by-side");
    assert.equal(iPadPortrait.overflow, false,
      "portrait comparison uses columns on iPad");
    const iPadPortraitGap = assertImagePairMeetsAtCenter(
      iPadPortrait, "iPad portrait comparison",
    );
    assert.ok(phonePortraitGap < iPadPortraitGap
      && iPadPortraitGap < desktopPortraitGap,
    "comparison gutter responds to viewport width");
    await touchPage.keyboard.press("h");
    const iPadHudVisible = await lightboxHudGeometry(touchPage);
    await touchPage.keyboard.press("h");
    const iPadHudHidden = await lightboxHudGeometry(touchPage);
    assert.ok(iPadHudHidden.stage.height > iPadHudVisible.stage.height + 90,
      "iPad HUD hiding reclaims chrome space");
    await touchPage.keyboard.press("h");
    await touchPage.locator("#lbClose").click();

    await touchPage.locator('.card[data-idx="0"]').click();
    await waitForLoadedImages(touchPage, "#lbStage img");
    await touchPage.setViewportSize({ width: 1180, height: 820 });
    assert.equal((await comparisonGeometry(touchPage, "stacked")).overflow, false,
      "2:1 landscape pairs still stack on iPad landscape when stacked is larger");
    await touchPage.locator("#lbClose").click();

    await touchPage.setViewportSize({ width: 820, height: 1180 });
    await touchPage.locator('.card[data-idx="8"]').click();
    await waitForLoadedImages(touchPage, "#lbStage img");
    await touchPage.setViewportSize({ width: 1180, height: 820 });
    const iPadLandscape = await comparisonGeometry(touchPage, "side-by-side");
    assert.ok(iPadLandscape.figures[1].left
      >= iPadLandscape.figures[0].right,
    "3:2 landscape pairs use columns on iPad landscape");
    assert.equal(await touchPage.locator("#lbStage").getAttribute(
      "data-comparison-reason"), "short-wide");
    assertImagePairMeetsAtCenter(
      iPadLandscape, "iPad landscape 3:2 comparison",
    );
    await touchPage.locator("#lbClose").click();

    const iPadZoomContext = await browser.newContext({
      viewport: { width: 834, height: 1194 },
      hasTouch: true,
      isMobile: true,
      userAgent: "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) "
        + "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    });
    const iPadZoomPage = await iPadZoomContext.newPage();
    await iPadZoomPage.goto(`http://127.0.0.1:${port}/`);
    await iPadZoomPage.waitForFunction(
      () => document.querySelector("#stat")?.textContent.includes("4000 renders"),
    );
    await iPadZoomPage.locator('.card[data-idx="0"]').click();
    await iPadZoomPage.locator("#lb.open").waitFor();
    await waitForLoadedImages(iPadZoomPage, "#lbStage img");
    await iPadZoomPage.keyboard.press("h");
    assert.equal((await lightboxHudGeometry(iPadZoomPage)).visible, false);
    const visualZoom = {
      offsetLeft: 280,
      offsetTop: 40,
      width: 417,
      height: 597,
      scale: 2,
    };
    await stubVisualViewport(iPadZoomPage, {
      ...visualZoom,
      offsetLeft: Math.round((834 - visualZoom.width) / 2),
    });
    await dispatchLightboxTap(
      iPadZoomPage, visualZoom.width * 0.9, visualZoom.height * 0.5,
    );
    assert.equal(await iPadZoomPage.locator("#lbName").textContent(),
      "alpha/item-0001.jpg",
      "a centered pinch-pan still treats the visible right edge as next");
    await stubVisualViewport(iPadZoomPage, visualZoom);
    await dispatchLightboxTap(
      iPadZoomPage, visualZoom.width * 0.9, visualZoom.height * 0.5,
    );
    assert.equal(await iPadZoomPage.locator("#lbName").textContent(),
      "alpha/item-0002.jpg",
      "iOS visual-viewport clientX treats a panned right-edge tap as next");
    assert.equal((await lightboxHudGeometry(iPadZoomPage)).visible, false,
      "a zoomed iPad right-edge tap does not toggle the HUD");
    await dispatchLightboxTap(
      iPadZoomPage, visualZoom.width * 0.1, visualZoom.height * 0.5,
    );
    assert.equal(await iPadZoomPage.locator("#lbName").textContent(),
      "alpha/item-0001.jpg",
      "iOS visual-viewport clientX treats a panned left-edge tap as previous");
    await waitForLightboxSrc(iPadZoomPage, "item-0001");
    await dispatchLightboxTap(
      iPadZoomPage, visualZoom.width * 0.5, visualZoom.height * 0.5,
    );
    assert.equal((await lightboxHudGeometry(iPadZoomPage)).visible, true,
      "iOS visual-viewport clientX treats a panned center tap as HUD");
    await restoreVisualViewport(iPadZoomPage);
    await iPadZoomContext.close();
    await touchContext.close();

    console.log(
      `${name} gallery metrics: initial=${initialCardCount}, `
      + `deep=${deepCardCount}, resized=${deepIndexes.length}, `
      + `scrollAdds=${scrollMetrics.addedCards}, `
      + `replacements=${scrollMetrics.identityReplacements}, `
      + `maxCards=${scrollMetrics.maxCards}, `
      + `phoneLandscapeStage=${compactLandscape.stageHeight}, `
      + `phoneLandscapeChrome=${compactLandscape.chromeHeight}, `
      + `portraitGaps={desktop:${desktopPortraitGap.toFixed(1)},`
      + `phonePortrait:${phonePortraitGap.toFixed(1)},`
      + `phoneLandscape:${phoneLandscapePortraitGap.toFixed(1)},`
      + `shortWide:${shortWideGap.toFixed(1)},`
      + `iPad:${iPadPortraitGap.toFixed(1)}}, `
      + `hudStage={desktop:${desktopHudVisible.stage.height.toFixed(0)}->`
      + `${desktopHudHidden.stage.height.toFixed(0)},`
      + `phonePortrait:${phonePortraitHudVisible.stage.height.toFixed(0)}->`
      + `${phonePortraitHudHidden.stage.height.toFixed(0)},`
      + `phoneLandscape:${phonePortraitLandscapeHudVisible.stage.height.toFixed(0)}->`
      + `${phonePortraitLandscapeHudHidden.stage.height.toFixed(0)},`
      + `iPad:${iPadHudVisible.stage.height.toFixed(0)}->`
      + `${iPadHudHidden.stage.height.toFixed(0)}}, `
      + `mediaArea={desktop:${comparisonMediaArea(desktopVisibleMedia).toFixed(0)}->`
      + `${comparisonMediaArea(desktopHiddenMedia).toFixed(0)},`
      + `phonePortrait:${comparisonMediaArea(phonePortraitMediaVisible).toFixed(0)}->`
      + `${comparisonMediaArea(phonePortraitMediaHidden).toFixed(0)},`
      + `phoneLandscapePortrait:${comparisonMediaArea(phonePortraitLandscape).toFixed(0)}->`
      + `${comparisonMediaArea(phonePortraitLandscapeMediaHidden).toFixed(0)}}, `
      + "logical=4000",
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
