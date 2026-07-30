# reimagine

Recreate reference images as **dynamic-posture** renders. Walk a
tree of reference photos, ask a multimodal LLM to describe each one as a plain
krea2 prompt, render it on a (Windows) ComfyUI box, and write the results into an
output tree that mirrors the input's structure and filenames.

```
input/sports/sprint.jpg   ──►   output/sports/sprint.jpg   ──►   output/sports/sprint.mp4
                                 (reimagine.py: still)            (animate.py: LTX video)
```

This is a sibling of `../genre-masher-prompts` and borrows its ComfyUI client and
JPEG-transcode machinery, but is driven by an input-image walk instead of random
genre mashups, and prompts the LLM **multimodally** (it looks at the reference).
An optional second stage (`animate.py`) turns each rendered still into a short
LTX 2.3 action video — see [Video (LTX 2.3)](#video-ltx-23).

## How it works

For each image under `input/` (recursively), either in one `run` command or in
separate `describe` and `render` commands:

1. **Prompt** — `claude -p` is invoked with only the read-only **Read** tool
   enabled and `--add-dir input/`, so it can view the reference image but do
   nothing else. A system prompt instructs it to write ONE plain-text
   krea2-style prompt that recreates the image — emphasizing the subject's
   **dynamic posture / motion** (and its **facing direction**, since image
   models otherwise mirror the subject at random) — and to keep it fully clothed
   and free of text/logos. The answer is returned inside `<prompt>…</prompt>`
   tags (retried up to 3× if malformed). The system prompts live in editable
   text files under `prompts/` (`system_manual.txt`, `system_regions.txt`) and
   are loaded at startup; the short retry nudges stay in code.
2. **Persist** — the exact manual prompt or validated region structure, output
   path, dimensions, stable item index, and source content hash are written to
   the output set's versioned `render_specs.yaml`. This manifest is sufficient
   to render later without the LLM server or original reference images.
   `prompts.yaml` remains a readable gallery projection.
3. **Render** — the prompt is patched into node `143` of the krea2 workflow
   (`workflows/krea2_comfyui_t2i_aitrepeneur_jpg_api.json`), along with a fresh
   seed and render dimensions **derived from the reference image's own aspect
   ratio** (scaled to a fixed ~2.07 MP budget — a 1920×1080 frame's worth of
   pixels — and snapped to multiples of 64). The workflow's **Image Saver** node
   (`159`) writes a JPEG on the ComfyUI host under `output/reimagine/`.

   With **`--regions`**, step 1 instead asks the LLM for a structured JSON spec —
   an overall description plus coordinate-placed regions (`elements`) — which is
   patched into the `Ideogram4PromptBuilderKJ` node (`14`) of
   `workflows/krea2_regions_comfyui_t2i_aitrepeneur_jpg_api.json`. See
   [Region prompting](#region-prompting) below.
4. **Retrieve** — the rendered JPEG is read back from a local or mounted copy of
   the ComfyUI `output/` dir (`--comfyui-output-dir`), transcoded to JPEG q=90,
   and written to `output/<mirrored path>.jpg`. If the file isn't found there,
   retrieval falls back to ComfyUI's HTTP `/view` using what the Image Saver
   reported to `/history`.

Description generation uses an ordered sliding window: up to `--llm-workers`
jobs run in a thread pool and results are consumed in reference order. Each
`claude -p` call is its own subprocess and parallelizes cleanly (default 3
workers); a local `--llm-server` is automatically pinned to one worker. Rendering
is serial because ComfyUI's queue serializes these workflows.

## Setup

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv -r requirements.txt
```

**ComfyUI output directory** (optional retrieval path). If ComfyUI is remote,
share its `output/` directory and mount it locally. For example, on macOS:

```bash
mkdir -p ~/Desktop/MyShare
mount_smbfs "//$COMFYUI_USER@127.0.0.1/$COMFYUI_SHARE" ~/Desktop/MyShare
```

`./go.sh` checks whether that mount is connected and reports the expected path
if it isn't. Set `COMFYUI_OUTPUT_DIR` if it is mounted somewhere other than
`~/Desktop/MyShare`. The `.credits.json` scratch file and `.venv` are gitignored.

## Usage

```bash
# Full run: describe and render in one process.
.venv/bin/python reimagine.py run \
    --comfyui-output-dir ~/Desktop/MyShare \
    --comfy-server 127.0.0.1:8188

# Run 1 on a memory-constrained machine: only the LLM server must be loaded.
.venv/bin/python reimagine.py describe \
    --llm-server 127.0.0.1:9503 \
    --output-dir outputs/local-llm

# Stop the LLM server and start ComfyUI. Run 2 needs only render_specs.yaml.
.venv/bin/python reimagine.py render \
    --output-dir outputs/local-llm \
    --comfyui-output-dir ~/Desktop/MyShare

# Optionally override model filenames embedded in the workflow.
.venv/bin/python reimagine.py render \
    --output-dir outputs/local-llm \
    --clip-name qwen3vl_4b_fp8_scaled.safetensors \
    --unet-name krea2_turbo_fp8.safetensors

# Force only the relevant phase.
.venv/bin/python reimagine.py describe --output-dir outputs/local-llm --force
.venv/bin/python reimagine.py render --output-dir outputs/local-llm --force

```

Each render matches its reference image's aspect ratio, scaled to a fixed
~2.07 MP budget (1920×1080 worth of pixels) and snapped to /64.

Existing manifest entries and outputs are skipped unless `--force` is passed,
so interrupted describe and render runs resume independently. Each successful
description is persisted immediately. Each image uses `--seed + its saved index`
for reproducibility.
If a reference changes at the same path, its content hash prevents a stale
description from being reused.
Adding, removing, or renaming references changes stable item indexes; use
`describe --force` to rebuild that output set's manifest after changing its
reference inventory.
There's always exactly one file per reference — before each render the pipeline
clears any prior host-side copy, so the Image Saver never leaves `_NNNNN`
counter versions behind. The run ends with a count of images generated vs.
skipped.

### Options

| command / flag | default | meaning |
| --- | --- | --- |
| `describe` | — | generate and save descriptions; never starts ComfyUI |
| `render` | — | render a complete manifest; never loads references or starts an LLM |
| `run` | — | describe missing entries and render in one process |
| `--input-dir` | `input` | reference-image tree (walked recursively) |
| `--output-dir` | `output` | mirror tree for rendered JPEGs |
| `--spec-file` | `<output-dir>/render_specs.yaml` | alternate render-manifest path |
| `--regions` | off | use region-based structured prompting (see below) instead of a single plain-text prompt |
| `--workflow` | *(mode default)* | ComfyUI API workflow; defaults from the manifest's manual or regions mode |
| `--comfy-server` | `127.0.0.1:8188` | ComfyUI host |
| `--comfyui-output-dir` | *(none)* | local or mounted ComfyUI `output/`; falls back to HTTP `/view` if unset |
| `--claude-model` | `opus` | model for the Claude Code CLI |
| `--llm-workers` | `3` | concurrent description jobs (Claude only; a local `--llm-server` is pinned to 1) |
| `--seed` | `42` | base seed; image _i_ uses `seed + i` |
| `--clip-name` | *(workflow value)* | override the CLIP model filename for `render` or `run` |
| `--unet-name` | *(workflow value)* | override the UNet model filename for `render` or `run` |
| `--force` | off | replace descriptions for `describe`, images for `render`, or both for `run` |

## Region prompting

By default the LLM writes one plain-text prompt per image. With `--regions` it
instead emits a **structured, coordinate-placed spec** that drives ComfyUI's
`Ideogram4PromptBuilderKJ` node — an overall description plus a list of
`elements`, each a box (`x, y, w, h` as fractions of the frame, top-left origin)
with its own description. This gives the model explicit control over *where*
things sit in the frame, not just *what's* in it.

```bash
# Region mode, descriptions only.
.venv/bin/python reimagine.py describe --regions \
    --llm-server 127.0.0.1:9503 --output-dir outputs/local-regions

# Render mode is inferred from the saved manifest; --regions is not needed.
.venv/bin/python reimagine.py render --output-dir outputs/local-regions \
    --comfyui-output-dir ~/Desktop/MyShare
```

The LLM returns JSON inside `<regions>…</regions>` tags (retried up to 3× if it
fails to parse or validate). Validation is deliberately lenient — every styling
field (`aesthetics`, `lighting`, `style`, `palette`) is optional, and text
elements are only used when the reference genuinely contains prominent lettering.
The spec is patched into node `14` of
`workflows/krea2_regions_comfyui_t2i_aitrepeneur_jpg_api.json`; the same
seed / aspect-ratio / single-file-per-image machinery as manual mode applies. A
complete validated spec is saved to `render_specs.yaml`; a readable rendering is
also saved to `prompts.yaml` so it shows up in the gallery like a plain-text
prompt.

Manual (plain-text) prompting remains the default — pass `--regions` to opt in.

## Video (LTX 2.3)

`animate.py` is a **second stage**: once a still set is rendered, it turns each
still into a short action video with the LTX 2.3 image-to-video model. For every
rendered still it asks the LLM to look at that frame (the video's first frame)
and write a ~10-second motion prompt, then renders the clip and writes the `.mp4`
**next to the still** (same stem):

```
outputs/claude-regions/animals/cat-pounce.jpg
  -> outputs/claude-regions/animals/cat-pounce.mp4
```

```bash
# Animate a rendered set. Same async pipeline + output-dir retrieval as reimagine.py.
.venv/bin/python animate.py --set outputs/claude-regions \
    --comfyui-output-dir ~/Desktop/MyShare --comfy-server 127.0.0.1:8188

# Prompts only — preview the motion prompts without rendering.
.venv/bin/python animate.py --set outputs/claude-regions --no-videos \
    --llm-server 127.0.0.1:9503
```

The prompt is patched into node `1070` of
`workflows/ltx2-3_comfyui_i2v_aitrepeneur_api.json`; the first frame is read by
the `LoadImage` node (`1077`). LoadImage resolves relative to ComfyUI's `input/`
dir, so the default `--load-name-template` (`../output/reimagine/{base}.jpeg`)
reaches the still that `reimagine.py` already staged on the host — no extra
upload step. `{base}` is the still's path with `/` → `__` (matching reimagine's
host filename); point the template elsewhere (or at an absolute path) if the
stills were staged under a different `--save-subdir`. The video system prompt
lives in `prompts/system_video.txt`; the retry nudge stays in code. `--duration`
(default 10s), `--seed`, `--force`, and `--no-videos` control the video pipeline.
The workflow derives its dimensions internally. The motion prompt for
each clip is saved to a per-directory `video_prompts.yaml`.

## Gallery

`serve.py` is a tiny stdlib HTTP server for browsing the results. It walks
`output/` and `input/` live on every request, so new renders appear on a page
refresh while a batch is still running.

```bash
.venv/bin/python serve.py            # http://127.0.0.1:8000
.venv/bin/python serve.py --port 9000
```

Renders are grouped by category. Click any card for a lightbox that shows the
render **side-by-side** with its reference (← / → to move between images, `S`
to toggle the split, `Esc` to close), or tick "compare in grid" to show both
in every tile. No build step and no third-party deps — it reads the disk directly.

If a still has a sibling video (from `animate.py`), the card gets a **▶ video**
badge; tick the **video** toggle in the header to swap the static render for the
looping clip wherever one exists (cards play on hover; the lightbox autoplays and
shows the motion prompt). Stills without a video fall back to the image.

### Multiple output sets

Output sets live side by side under a single top-level `outputs/` directory —
one subdirectory per run, e.g.:

```
outputs/claude/              # renders from the Claude Code CLI
outputs/local-llm/           # plain-text prompts from the local LLM
outputs/local-llm-regions/   # region-based prompts from the local LLM
```

Point a run at its own folder with `--output-dir outputs/<name>`. The gallery
discovers every set automatically and offers a **source switcher** in the header
(labeled by directory name) so you can flip between them for side-by-side
comparison; the current reference-image compare works within whichever set is
selected. Kept sets stick around for posterity.

```bash
.venv/bin/python serve.py                       # browse all of outputs/
.venv/bin/python serve.py --output-dir outputs/claude   # just one set
```

The `output` symlink (the default `--output-dir` for each `reimagine.py` command)
points at whichever set is "current".

## Reference images

`input/` holds ~20 free-to-use reference photos from Wikimedia Commons (CC0 /
CC BY / public domain), grouped into four **dynamic-posture** categories:

- `sports/` — sprint, cycling, gymnastics, martial arts, climbing
- `dance/` — ballet leap, breakdance, yoga, contemporary, flamenco
- `everyday/` — construction, chef, gardener, musician, painter
- `animals/` — horse gallop, dog leap, bird takeoff, cat pounce, dolphin breach

Attribution for every image is recorded in `input/CREDITS.md`. They were fetched
with `fetch_refs.py` (a one-shot seeding helper, polite-paced with backoff to
respect Wikimedia's rate limits — not part of the render pipeline).

## Future iterations

Done: **rich krea prompts** — the LLM can now emit a structured,
coordinate-placed spec (see [Region prompting](#region-prompting), `--regions`).

Done: **video** — `animate.py` generates LTX 2.3 videos from the rendered stills
(see [Video (LTX 2.3)](#video-ltx-23)).
