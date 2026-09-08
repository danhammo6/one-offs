---
purpose: How tests are split, how to run them, and the local 4k fixture
audience: Later sessions adding coverage or measuring gallery scale
when to read: Before changing tests, running CI-like checks, or using the large fixture
related: test_pipeline.py, test_gallery_frontend.mjs, package.json, .gitignore
---

# Testing

Two suites. Python covers pipeline, cache, and gallery HTTP. Playwright covers
`index.html` interaction. Neither commits generated fixtures.

## unittest (`test_pipeline.py`)

Stdlib `unittest`. Planner/renderer isolation, manifest validation, thumbnail
paths, incremental index warm/cold behavior, malformed-manifest gallery
fallback, CLI flags including `--cache-root` and deprecated
`--thumbnail-cache-root`.

```bash
.venv/bin/python -m unittest test_pipeline.py
```

## Playwright (`test_gallery_frontend.mjs`)

`package.json` script `test:frontend` installs Playwright WebKit, then runs
`node --test test_gallery_frontend.mjs`. The same file launches **Chromium**
(system Chrome/Edge/Chromium via `PLAYWRIGHT_CHROME` or well-known paths) and
**WebKit** (Playwright-managed). Each browser gets a 4,000-item in-process
mock server — not the on-disk fixture.

WebKit approximates Safari. Real iOS Safari on iPhone/iPad, and Safari on
Mac, are still required for user-facing UI changes. That is how the gallery is
primarily tested by hand.

```bash
npm install
npm run test:frontend
```

The suite checks windowing (DOM ≤ 80 cards), metadata-on-open, HUD zones,
pinch-zoom edge taps (visible-viewport bands after pan), prompt-first-close,
HUD preserved across navigation and reset on reopen, navigation holding
painted media until the next stills can decode, portrait center alignment,
short-wide phone-landscape columns, and video controls remaining usable
with hidden chrome.

## Local 4k fixture

Ignored, bulky, generated. Do not commit.

| Path | Contents |
| --- | --- |
| `input-large-gallery-test/` | 200 folders × 20 images (4,000 stills) |
| `outputs/local-llm-regions-pipeline-test-large/` | Matching 200 folders, 200 `pipeline.yaml` files, 4,000 stills, **no videos** |

`.gitignore` ignores `input-large-gallery-test/` and all of `outputs/`. Use
this pair for real `serve.py` startup and transfer measurements. Playwright does
not require it.

Serve it as a normal outputs tree:

```bash
.venv/bin/python serve.py
# pick source local-llm-regions-pipeline-test-large
```
