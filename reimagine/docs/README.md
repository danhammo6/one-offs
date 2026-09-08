---
purpose: Index of topic docs; choose one file, do not open all of them
audience: Later sessions discovering reimagine docs
when to read: First, then only the matching topic file
related: ../README.md, architecture.md, ui.md, performance.md, testing.md
---

# Topic docs

Start from this index (or the table in the project README). Open only the
file whose “when to read” matches the task. Do not load every file by default.

| File | Purpose | When to read |
| --- | --- | --- |
| [architecture.md](architecture.md) | Pipeline, gallery server, frontend data flow | Changing serve/planner/renderer wiring, routes, or manifests |
| [ui.md](ui.md) | Lightbox/gallery interaction and layout goals | Lightbox, HUD, comparison layout, Safari/touch behavior |
| [performance.md](performance.md) | Manifest index, thumbnails, virtualization | Cache, startup time, gallery DOM/transfer cost |
| [testing.md](testing.md) | unittest vs Playwright, 4k fixture, how to run | Adding tests or measuring the large gallery |

Each topic file starts with `purpose`, `audience`, `when to read`, and
`related` so a later session can skip it after the header.

Operational how-to (flags, prompt stages, gallery CLI) stays in
[../README.md](../README.md). Historical region-prompt numbers live in
[../REGION_PROMPT_EXPERIMENTS.md](../REGION_PROMPT_EXPERIMENTS.md).
