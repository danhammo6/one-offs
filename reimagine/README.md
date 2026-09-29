# reimagine

Recreate reference images as dynamic-posture stills and LTX 2.3 videos. The
pipeline is deliberately split into two processes so an LLM server and ComfyUI
never need to fit in VRAM at the same time:

```text
reference images -> generate_prompts.py -> input/**/pipeline[_suffix].yaml
reusable plan    -> render_media.py     -> output JPEGs + MP4s
```

`serve.py` provides a gallery for comparing the generated media with its
references.

## Architecture / Design / UI Goals

The planner (`generate_prompts.py`) never talks to ComfyUI. The renderer
(`render_media.py`) never talks to an LLM. Each reference folder’s
`pipeline[_suffix].yaml` is the reusable, versioned handoff: prompts, paths,
dimensions, and duration. Each output set has a small `render_run.yaml` pointer
to the selected plan. The gallery follows those pointers and reads the plan
files once at startup into a disposable JSON index,
walks media live per request, and serves `index.html` as a virtualized
Safari-first comparison UI.

Lightbox chrome stays compact. Landscape pairs stack; portrait (and
short-wide phone landscape) go side-by-side with portrait images meeting at
center. A reading-style HUD hides every control so the media can use the
viewport; hidden HUD state survives navigation and resets when the lightbox
is reopened. Video playback is kept; HTTP range/chunked video serving is not
implemented.

```mermaid
flowchart LR
  input[Reference tree] --> planner[generate_prompts.py]
  planner --> yaml["input/**/pipeline[_suffix].yaml"]
  yaml --> renderer[render_media.py]
  renderer --> media[JPEGs + sibling MP4s]
  renderer --> run["output/render_run.yaml"]
  yaml --> server[serve.py]
  run --> server
  media --> server
  server --> gallery[index.html]
```

Operational flags, prompt stages, and gallery cache layout stay in the sections
below. Topic docs are for later sessions that should open only what they need:

| Doc | When to read |
| --- | --- |
| [docs/README.md](docs/README.md) | Topic-file index with the same when-to-read rules |
| [docs/architecture.md](docs/architecture.md) | Pipeline vs gallery data flow, HTTP routes, key modules |
| [docs/ui.md](docs/ui.md) | Lightbox/gallery interaction, orientation, HUD, Safari/touch |
| [docs/performance.md](docs/performance.md) | Manifest index, thumbnail cache, virtualization, measurements |
| [docs/testing.md](docs/testing.md) | unittest vs Playwright, 4k fixture, how to run tests |
| [REGION_PROMPT_EXPERIMENTS.md](REGION_PROMPT_EXPERIMENTS.md) | Historical region-prompt LLM benchmarks, not runtime architecture |

## Setup

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv -r requirements.txt
```

OMLX defaults to `127.0.0.1:9503`. If that server requires authentication, set
`OMLX_API_KEY` (or `OPENAI_API_KEY`) in the environment; the value is sent as a
Bearer token and is never logged. ComfyUI defaults to `192.168.33.101:8188`.
`--comfyui-output-dir` is optional; when
provided, artifacts are read directly from a local or mounted ComfyUI `output/`
directory. HTTP `/view` is the fallback.

## Two-phase workflow

Generate both still and video prompts while only the LLM is loaded:

```bash
.venv/bin/python generate_prompts.py \
  --stage all \
  --still-mode regions \
  --video-basis reference \
  --pipeline-suffix gemma4_regions
```

Stop the LLM server, start ComfyUI, then render all stills followed by all
videos:

```bash
.venv/bin/python render_media.py \
  --stage all \
  --pipeline-suffix gemma4_regions \
  --output-dir outputs/gemma4-regions-krea \
  --comfyui-output-dir ~/Desktop/MyShare
```

The renderer uploads each final local JPEG to ComfyUI's input directory before
starting LTX. The exact image shown in the gallery is therefore the video's first
frame; video rendering does not depend on stale ComfyUI output staging.

## Prompt stages

`generate_prompts.py` runs serially and checkpoints a
`pipeline[_suffix].yaml` beside the references in each image folder. It never
imports or contacts ComfyUI. Startup and resume scan the selected input tree,
so no top-level plan grows with the total collection. Different suffixes can
coexist and be generated with different models or settings. Each plan stores
its input directory relative to this `reimagine/` folder. The default is
`input`; directories linked from within the input tree may be symbolic links.

| flag | default | meaning |
| --- | --- | --- |
| `--stage` | `all` | generate `stills`, `videos`, or `all` plans |
| `--still-mode` | `manual` | plain `manual` prompt or structured `regions` spec |
| `--video-basis` | `reference` | generate motion from the `reference` or actual `rendered` still |
| `--common-dims` | off | center-crop temporary reference copies to the closest common 1.5 MP size |
| `--input-dir` | `input` | project-relative reference tree and pipeline location |
| `--pipeline-suffix` | *(empty)* | select `pipeline_<suffix>.yaml`; empty selects `pipeline.yaml` |
| `--output-dir` | `output` | rendered still location used only with `--video-basis rendered` |
| `--duration` | `10` | video duration in seconds, 1-30 |
| `--prompt-path-prefix` | `prompts/` | directory containing user prompt templates and schemas |
| `--system-prompt` | *(none)* | optional inline system message for every request |
| `--system-prompt-file` | *(none)* | optional UTF-8 system-message file; mutually exclusive with the inline option |
| `--llm-server` | `127.0.0.1:9503` | OpenAI-compatible multimodal server |
| `--llm-max-tokens` | `16384` | maximum completion-token budget for the OpenAI-compatible server |
| `--llm-reasoning` | `on` | llama.cpp reasoning mode; use `off` to disable |
| `-v`, `--verbose` | off | log rejected LLM responses; repeat (`-vv`) to include available reasoning |
| `--force` | off | regenerate requested plan stages |

The default `reference` video mode uses the original reference plus the
validated still plan. Its dedicated user prompt deliberately requests
conservative motion that does not depend on exact generated limb geometry.
Video prompt detail scales with `--duration`: the planner targets roughly
8-16 words per second (80-160 words for 10 seconds and 160-320 words for 20
seconds), up to a 500-word maximum. Longer prompts use related temporal beats,
evolving synchronized audio, and an explicit final state rather than adding
unrelated action or re-describing the first frame.

`--prompt-path-prefix` must contain `user_manual.txt`, `user_regions.txt`,
`regions.schema.json`, `user_video.txt`, and `user_video_reference.txt`.
Relative paths are resolved from the directory where `generate_prompts.py` is

The built-in task instructions are always part of the user message. No system
role is sent by default. Use either `--system-prompt` or
`--system-prompt-file` to add your own system message without replacing the
built-in user instructions.

To create two reusable plans for one input tree, run the planner with two
suffixes:

```bash
.venv/bin/python generate_prompts.py \
  --input-dir input/sports \
  --pipeline-suffix gemma4_nothink_8ktokens \
  --llm-reasoning off \
  --llm-max-tokens 8192

.venv/bin/python generate_prompts.py \
  --input-dir input/sports \
  --pipeline-suffix qwen38_think_16ktokens
```

This creates `pipeline_gemma4_nothink_8ktokens.yaml` and
`pipeline_qwen38_think_16ktokens.yaml` in every populated reference folder.
Suffixes must start with a letter or number and may contain only letters,
numbers, underscores, and hyphens (100 characters maximum).

`--common-dims` EXIF-normalizes each reference, scales it with Lanczos
resampling, and center-crops it to the closest supported aspect ratio. The
temporary JPEG copies are used for still prompting and for reference-basis
video prompting; source files are never modified. The selected dimensions are
also saved as the still render dimensions:

| Format | Dimensions |
| --- | ---: |
| Base portrait (2:3) | 1024 x 1536 |
| Stable portrait (3:4) | 1088 x 1440 |
| Tall mobile (9:16) | 928 x 1664 |
| Base landscape (3:2) | 1536 x 1024 |
| Balanced landscape (4:3) | 1440 x 1088 |
| Widescreen (16:9) | 1664 x 928 |
| Square format (1:1) | 1248 x 1248 |

When splitting still and reference-basis video planning into separate commands,
pass `--common-dims` to both so each temporary copy uses the same deterministic
crop. The manifest records this preprocessing mode and rejects a mismatched
resume rather than silently planning against different framing. Omitting the
flag preserves the existing aspect-ratio-derived dimensions and original
reference image behavior.

Invalid tagged or region responses consume one of three prompt attempts. Retry
logs include the rejection reason and elapsed time; region JSON that parses but
fails semantic validation is retried instead of failing the item immediately.
Formatting and validation retries send the error and rejected response back to
the LLM so it can correct its prior output. For OpenAI-compatible servers, the
region schema is sent per request using the standard
`response_format.json_schema` field supported by llama.cpp and vLLM; the server
does not need to be started with a schema. Region responses are constrained to
compact, output-only JSON to avoid exhausting the completion budget on narrated
analysis.
The local
`-v` option logs the complete rejected response for diagnosing parse failures;
`-vv` also logs separate reasoning content when the backend provides it. The local
OpenAI-compatible client enables llama.cpp prompt caching and places retry-only
instructions after the image so llama.cpp can reuse the unchanged multimodal
prefix. Requests still transfer and decode the image, but a cache hit avoids
repeating the expensive vision-token evaluation. Cache usage and server timing
metadata are logged at `DEBUG` when returned by the server. A transient HTTP 500
from the completion endpoint is retried once after one second.
`--llm-reasoning` sets llama.cpp's per-request `reasoning` option; changing it
does not require restarting the server or modifying the prompt templates. The 16k
budget with reasoning enabled is the default because it was the fastest and most
reliable configuration in the 20-image schema benchmark.

## Exact-frame prompting

For higher fidelity, use an additional LLM phase that inspects the actual still:

```bash
# 1. LLM: still plans
.venv/bin/python generate_prompts.py --stage stills --still-mode regions \
  --pipeline-suffix gemma4_exact

# 2. ComfyUI: still renders
.venv/bin/python render_media.py --stage stills \
  --pipeline-suffix gemma4_exact --output-dir outputs/exact

# 3. LLM: video prompts grounded in those exact JPEGs
.venv/bin/python generate_prompts.py --stage videos --still-mode regions \
  --pipeline-suffix gemma4_exact \
  --video-basis rendered --output-dir outputs/exact

# 4. ComfyUI: videos
.venv/bin/python render_media.py --stage videos \
  --pipeline-suffix gemma4_exact --output-dir outputs/exact
```

Rendered-basis video plans record the JPEG SHA-256. Rerendering a still requires
regenerating its rendered-basis video prompt before the video phase.

## Render stages

`render_media.py` never imports or constructs an LLM. It reads the selected
pipeline tree beside the references, but does not load reference image bytes;
the plan contains every required prompt, path, dimension, and duration. Seeds
are a renderer concern and are not stored in prompt plans.

| flag | default | meaning |
| --- | --- | --- |
| `--stage` | `all` | render `stills`, `videos`, or all stills then all videos |
| `--output-dir` | `output` | media output set |
| `--input-dir` | `input` | project-relative reference tree containing the plans |
| `--pipeline-suffix` | *(empty)* | select `pipeline_<suffix>.yaml`; empty selects `pipeline.yaml` |
| `--state-file` | per-folder tree | use one explicit legacy/single-file render state instead |
| `--comfy-server` | `192.168.33.101:8188` | ComfyUI server |
| `--comfyui-output-dir` | *(none)* | optional local/mounted ComfyUI output directory |
| `--still-workflow` | mode default | custom Krea API workflow |
| `--video-workflow` | checked-in LTX workflow | custom LTX API workflow |
| `--clip-name` | workflow value | optional still-workflow CLIP override |
| `--unet-name` | workflow value | optional still-workflow UNet override |
| `--video-clip-name` | workflow value | optional LTX text-encoder override |
| `--video-unet-name` | workflow value | optional LTX diffusion-model override |
| `--seed` | `42` | base render seed; item i uses seed plus its sorted index |
| `--force` | off | rerender requested stages |

Each image folder's `render_state.yaml` tracks its render fingerprints and
output hashes. A changed still invalidates its dependent video. Corrupt or
stale files are not silently accepted merely because a path exists.

Planner and renderer progress uses Python logging. Normal runs emit per-item
prompt or render durations and a total elapsed-time summary at `INFO`; retries,
blocked work, and unavailable services use `WARNING`; failures use `ERROR`.
All command help includes argument defaults.

## Plans, render provenance, and gallery metadata

The per-reference-folder `pipeline[_suffix].yaml` files are the authoritative,
versioned handoff between the two processes. Each item stores:

- Stable source-relative ID, source path, and source SHA-256
- Exact manual prompt or complete validated region spec
- Still output path and dimensions
- Video output path, prompt, duration, prompt basis, and basis hash

Reference folders contain any number of suffixed plans beside their source
images. Rendered media folders contain per-folder `render_state.yaml` files and
one top-level `render_run.yaml`, which records the project-relative input tree
and exact pipeline filename used by that output set. The renderer refuses to
switch an existing output directory to a different plan; use another output
directory instead. This makes the same prompts reusable across model, workflow,
seed, and override experiments without copying them into each output run.

The gallery follows `render_run.yaml` to the selected input-side plan. For
read-only compatibility it can still inspect an old output-side
`pipeline.yaml`, but the planner and renderer no longer expose a `--manifest`
option and new runs never write prompt plans under outputs.

## Batch scripts

```bash
scripts/generate_plans.sh  # LLM-only pass
scripts/render_plans.sh    # ComfyUI-only pass
```

Both scripts accept environment overrides documented in their source.

To collect images whose initial LLM response needed a retry for a focused
diagnostic rerun:

```bash
.venv/bin/python analyze_prompt_failures.py outputs/*.log \
  --source-dir /path/to/references
```

This copies the matching references to `input/first-attempt-failures/` by
default. Rerun that directory with `-v` or `-vv` to inspect rejected responses.

## Gallery

Interaction model and HUD goals: [docs/ui.md](docs/ui.md). Cache layout and
startup measurements: [docs/performance.md](docs/performance.md). Tests:
[docs/testing.md](docs/testing.md).

```bash
.venv/bin/python serve.py              # http://127.0.0.1:8000
.venv/bin/python serve.py --port 9000
```

Output sets live under `outputs/`. The gallery discovers each set directory,
uses each set's `render_run.yaml` to find its reusable input-side plan, shows
those references beside generated stills, and switches to sibling videos when
available. In the
lightbox, tap the outer quarter of either side, swipe, or use Left/Right Arrow
to navigate. Tap the middle half or press `H` to hide/show all lightbox chrome
and give the media more room; a center tap closes an open prompt before hiding
the HUD. Closing and reopening the lightbox restores visible controls.
Pipeline metadata is loaded before the server starts listening and remains
fixed for the lifetime of the process; restart the gallery after changing a
manifest. A disposable
`.reimagine-cache/manifest-index-v2-<scope-hash>.json` index keeps the validated
result of each YAML plan. The scope hash is a stable SHA-256 prefix derived
from the sorted canonical discovery roots and relevant discovery options; it
contains no root names or paths. A normal invocation creates one obvious index,
while servers using the same cache root for different discovery scopes keep
separate indexes and cannot discard each other's warm entries. On restart,
unchanged manifests are recognized by canonical path, size, and high-resolution
file identity fields and do not need to be parsed again. New and changed
manifests are parsed, deleted manifests are dropped from that scope's index, and
missing, incompatible, or corrupt indexes are rebuilt from the authoritative
YAML files. Cached input-directory values are revalidated with the same
authoritative path rules as YAML before reuse. A source with malformed or
inconsistent manifests is logged and remains browseable without reference or
prompt metadata. Media files are still discovered live on each request.
Project manifests use project-relative index keys. If an explicitly served
output root is outside the project, its canonical absolute manifest key may
appear only in this server-local JSON; index contents are never returned by the
HTTP API.

Still rendering writes 512 px maximum-edge WebP thumbnails under the same
project-level disposable cache, preserving aspect ratio and EXIF orientation
without upscaling. The gallery uses the cache for lazy fallback; input and
output media trees remain read-only. `render_media.py` and `serve.py` both
accept `--cache-root` to relocate the complete cache. The older
`--thumbnail-cache-root` spelling remains a deprecated alias and now also names
the unified cache root, so thumbnails for either spelling are written beneath
the selected root's `thumbnails/` subdirectory.

The thumbnail layout is
`.reimagine-cache/thumbnails/{input|output}/<media-root-name>-<root-hash>/<relative-path>.<source-fingerprint>.webp`.
The root hash namespaces different pipeline and reference roots without placing
absolute paths in URLs or cache paths. Complete source filenames and immutable
source fingerprints prevent extension, root, and version collisions. Thumbnail
writes are atomic; corrupt entries are regenerated. A lightweight fingerprint
of the relative path and high-resolution file identity fields drives both disk
paths and versioned URLs without hashing every full image during gallery
listing. Versioned and legacy files are retained because another process or
active request may still need them; perform cleanup offline while renderers and
gallery servers are stopped. Versioned responses use long-lived immutable
browser caching, while the lightbox loads full-resolution media.

Manifest indexes and thumbnails can be deleted while the renderer and gallery
are stopped; the next render or server startup recreates what it needs.
Deleting a scope's index forces a full YAML rebuild for that scope. Existing `.thumbnails/` data is
not migrated and can be removed because it contains only generated artifacts.
If cache permissions or stale local state cause startup warnings, stop all
gallery/renderer processes, remove `.reimagine-cache/`, and restart.

Gallery records are streamed as they are discovered, without embedding prompt
text. Prompt metadata is requested from `/api/metadata` only when a lightbox
item opens and is served from the startup manifest cache. The page uses a
windowed grid with a small overscan buffer, keeping card and media-node counts
bounded as the collection grows while retaining navigation across the full
logical result set. In-view thumbnails are requested before overscan so a jump
down the page is not queued behind off-screen rows.
