---
purpose: Lightbox/gallery interaction model and Safari/touch layout goals
audience: Later sessions changing index.html or gallery UX tests
when to read: HUD, comparison orientation, virtualization UX, touch/keyboard
related: index.html, test_gallery_frontend.mjs, serve.py
---

# UI

Primary user testing is Safari on Mac, iPad, and iPhone. Playwright WebKit
approximates Safari; it is not a substitute for a real device pass. The page
is a single `index.html` with compact chrome, Apple-system fonts, and
safe-area padding in the lightbox.

## Gallery

The grid is windowed. Cards that are in the scroll window (plus two rows of
overscan) exist in the DOM; the rest of the logical set stays in memory for
filter and lightbox navigation. Nodes are keyed
(`item:<source>:<path>`, `heading:<category>`) and reused across scrolls.
In-view thumbnails start first with `fetchpriority="high"`. Overscan waits
until those images have loaded (or 1.5s) at `low`, so a scrollbar jump does
not share an HDD queue with off-screen rows. Source selection persists in
`localStorage`. On coarse pointers the gallery scrollbar stays visually
compact but uses an 88px hit strip.

## Comparison layout

Lightbox compare uses the **result** orientation when source and result
disagree.

| Case | Layout |
| --- | --- |
| Landscape pair | Stacked when that paints clearly more pixels; otherwise side-by-side |
| Portrait pair | Side-by-side columns |

Landscape columns are typical on phone landscape and tablet landscape for
3:2 stills. Very wide 2:1 pairs often stay stacked because that uses more of
the stage.

Portrait image pairs set `object-position` so the two stills meet at
center (reference right-aligned, primary left-aligned). Hiding the HUD
must reclaim chrome height and must not shrink the media.

```mermaid
stateDiagram-v2
  [*] --> Closed
  Closed --> Open: open card
  Open --> Closed: Escape / Close
  Open --> HudHidden: H / Enter / Space / center tap / Left/Right / edge / swipe
  HudHidden --> Open: H / Enter / Space / center tap
  HudHidden --> HudHidden: Left/Right / edge tap / swipe
  note right of Open: reopen resets HUD visible
  note right of HudHidden: prompt closed first if expanded
```

## Reading-style HUD

All chrome hides together: filename, Close, compare toggle, figure labels,
hint, prompt toggle, prompt panel. Hidden nodes are `hidden` and `inert`.
Background header/main are inert while the lightbox is open.

| Input | Behavior |
| --- | --- |
| Middle 50% tap | Toggle HUD (after prompt-first-close) |
| Outer 25% tap | Hide HUD if visible; otherwise previous / next. Exact 25% pixel is center |
| Left/Right Arrow, swipe | Same as outer 25% tap |
| H, Enter, Space | Toggle HUD (Enter/Space only when the stage is focused) |
| Escape | Close lightbox |
| Pointer move > 14 px | Not a tap; no HUD toggle or edge nav |
| Video / chrome | Not HUD hit targets; video controls stay usable when HUD is hidden |

Tap bands are relative to the **visible** viewport, including while pinch-zoomed. iOS Safari reports tap `clientX`/`clientY` in that visual space already, so a right-edge tap must not be shifted by `visualViewport.offsetLeft` after a pan.

A center tap (or HUD toggle) **closes an open prompt first** and leaves the HUD
visible. A second center tap hides the HUD.

When the HUD is visible, the first Left/Right Arrow, edge tap, or swipe
hides it and stays on the current item. Those same inputs navigate only
after the HUD is hidden. Showing the HUD again means the next left/right
hides it once more.

Hidden HUD state is preserved across item navigation. Closing and reopening
the lightbox resets the HUD to visible. Navigation keeps the current stills
painted until the next media can decode, so a cache miss does not flash the
empty stage.

Video mode still compares against the reference still. Range/chunked video
delivery is a server gap, not a UI feature.
