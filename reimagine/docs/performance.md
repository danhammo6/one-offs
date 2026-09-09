---
purpose: Manifest index, thumbnail cache, virtualization, and known measurements
audience: Later sessions changing serve.py cache, files.py thumbnails, or gallery DOM cost
when to read: Startup time, cache layout, transfer/DOM bounds — not HUD semantics
related: serve.py, reimagine_pipeline/files.py, render_media.py, index.html, .gitignore
---

# Performance

YAML manifests remain source of truth. `.reimagine-cache/` is disposable
server/renderer output. Input and output media trees are read-only; the cache
never writes into them.

`render_media.py` and `serve.py` take `--cache-root` (default
`.reimagine-cache/`). `--thumbnail-cache-root` is a deprecated alias that now
names the same unified root; thumbnails still live under that root’s
`thumbnails/` subdirectory.

## Manifest index

Startup discovers `pipeline.yaml` files and validates them against a
persisted JSON index using stat identity: canonical path, size, `mtime_ns`,
`ctime_ns`, device, inode, mode. Unchanged files are not parsed. New and
changed files are parsed; deleted keys are dropped from that scope’s index.
Missing, incompatible, or corrupt indexes are rebuilt from YAML. Cached
`input_dir` values are revalidated with the same path rules as YAML before
reuse.

Index path:

```text
.reimagine-cache/manifest-index-v1-<24-hex-scope-hash>.json
```

The scope hash is a SHA-256 prefix of the sorted canonical discovery roots and
relevant discovery options. It contains no root names or paths. Distinct
`--outputs-dir` / `--output-dir` scopes keep separate indexes and cannot discard
each other’s warm entries. Project-relative keys use `project:…`; an output
root outside the project may appear as `absolute:…` in this server-local
file only.

Malformed manifests are stored as error records, logged, and served without
reference or prompt metadata. Media listing still walks the tree live.

## Thumbnails

Still rendering (and gallery lazy fallback) writes 512 px max-edge WebP,
quality 80, Lanczos, no upscale, EXIF orientation via `exif_transpose`, atomic
`0600` writes. Corrupt entries regenerate.

Layout:

```text
.reimagine-cache/thumbnails/{input|output}/<media-root-name>-<root-hash>/<relative-path>.<fingerprint>.webp
```

Cache keys are namespaced by media kind (`input`/`output`) + root hash +
relative path + fingerprint. The fingerprint is a 24-hex hash of the relative
path plus high-resolution file identity (size, mtime/ctime ns, device,
inode) — not a full-image hash. Versioned URLs use `?v=<fingerprint>` and
`Cache-Control: public, max-age=31536000, immutable`. Other fingerprint
versions and legacy files are retained; do not sync-delete them while a
renderer or gallery is running.

A versioned GET that already has a cache file is one `lstat` + one read of
the WebP. The server does not stat or open the source JPEG, does not take a
generation lock, and does not run a separate header-open. Hits also stay in a
256-entry in-process byte cache. That matters on HDD: the previous path
`stat`ed the source several times, opened the thumbnail twice, and let
overscan rows share the disk queue with on-screen cards.

Missing thumbs still generate on demand, but only two at a time, using
JPEG `draft()`, bilinear resize, and WebP `method=4`. Renderer-written
thumbs keep Lanczos + `method=6`. If a region has never been thumbnailed,
the first visit still pays for reading the full JPEGs; a later jump is a
cache hit.

## Frontend bounds

`index.html` keeps a keyed virtual window with **6-row overscan**. Cards in
the viewport get `fetchpriority="high"` and start loading immediately;
overscan waits until those in-view images have loaded (or 1.5s). Prompt text
is not in the stream; `/api/metadata` runs on lightbox open. `METADATA_CACHE`
clears on gallery reload and evicts failed fetches so a later open retries.

Playwright asserts a 4k logical gallery stays at **≤ 80** card nodes in the
DOM at initial view and after deep scroll/resize.

## Known measurements

Local 4k fixture (200 manifests / 4,000 stills; see [testing.md](testing.md)):

| Run | Result |
| --- | --- |
| Cold startup | ~4.9 s, 200 YAML parses |
| Warm startup | ~62 ms, 0 YAML parses |
| One changed manifest | ~101 ms, 1 parse |
| 4k gallery DOM | ≤ 80 cards |
| Thumbnail vs full-res viewport transfer | ~94% down |

Delete `.reimagine-cache/` while renderer and gallery are stopped to force a
full rebuild. Existing `.thumbnails/` trees are not migrated.
