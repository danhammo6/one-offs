# reimagine

Recreate reference images as **dynamic-posture** renders. Walk a
tree of reference photos, ask a multimodal LLM to describe each one as a plain
krea2 prompt, render it on a (Windows) ComfyUI box, and write the results into an
output tree that mirrors the input's structure and filenames.

```
input/sports/sprint.jpg   ──►   output/sports/sprint.jpg
```

This is a sibling of `../genre-masher-prompts` and borrows its ComfyUI client and
JPEG-transcode machinery, but is driven by an input-image walk instead of random
genre mashups, and prompts the LLM **multimodally** (it looks at the reference).

## How it works

For each image under `input/` (recursively):

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
2. **Render** — the prompt is patched into node `143` of the krea2 workflow
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
3. **Retrieve** — the rendered JPEG is read back from a samba-mounted copy of the
   ComfyUI `output/` dir (`--samba-root`), transcoded to JPEG q=90, and written
   to `output/<mirrored path>.jpg`. If the samba file isn't found, it falls back
   to ComfyUI's HTTP `/view` using what the Image Saver reported to `/history`.

These stages run as a **sliding-window pipeline** (borrowed from
`../genre-masher-prompts`): the prompt-writing step is the long pole per image,
so up to `--llm-workers` prompt jobs run ahead in a thread pool while ComfyUI
renders the current image serially — keeping both the LLM and ComfyUI busy.
Results are still consumed strictly in reference order. Each `claude -p` call is
its own subprocess and parallelizes cleanly (default 3 workers); a local
`--llm-server` serves one request at a time, so it's automatically pinned to a
single worker (concurrency there just stalls).

## Setup

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv -r requirements.txt
```

**Samba share** (retrieval path). On the Windows ComfyUI box, share the ComfyUI
`output/` directory. On the Mac, mount it:

```bash
mount_smbfs '//story@192.168.33.101/e$/lib/ComfyUI_windows_portable/ComfyUI/output' ~/Desktop/MyShare
```

`./go.sh` checks whether that mount is connected and reminds you of the command
if it isn't. (The mount path is personal config; `go.sh` is committed but the
`.credits.json` scratch file and `.venv` are gitignored.)

## Usage

```bash
# Full run: all images under input/, render on the Windows ComfyUI box,
# retrieve via the samba mount, write to output/ mirroring input/.
.venv/bin/python reimagine.py \
    --samba-root ~/Desktop/MyShare \
    --comfy-server 192.168.33.101:8188

# Prompts only — see what the LLM would send, no rendering (fast, no ComfyUI).
.venv/bin/python reimagine.py --no-images

# Force-overwrite everything, even if the output already exists.
.venv/bin/python reimagine.py --samba-root ~/Desktop/MyShare --force

# Region-based structured prompting (see below).
.venv/bin/python reimagine.py --regions --samba-root ~/Desktop/MyShare

```

Each render matches its reference image's aspect ratio, scaled to a fixed
~2.07 MP budget (1920×1080 worth of pixels) and snapped to /64.

Existing outputs are skipped unless `--force` is passed, so an interrupted
run resumes cheaply. Each image uses `--seed + its index` for reproducibility.
There's always exactly one file per reference — before each render the pipeline
clears any prior host-side copy, so the Image Saver never leaves `_NNNNN`
counter versions behind. The run ends with a count of images generated vs.
skipped.

### Options

| flag | default | meaning |
| --- | --- | --- |
| `--input-dir` | `input` | reference-image tree (walked recursively) |
| `--output-dir` | `output` | mirror tree for rendered JPEGs |
| `--regions` | off | use region-based structured prompting (see below) instead of a single plain-text prompt |
| `--workflow` | *(mode default)* | ComfyUI API workflow; defaults to the manual workflow, or the regions workflow under `--regions` |
| `--comfy-server` | `192.168.33.101:8188` | ComfyUI host |
| `--samba-root` | *(none)* | local mount of ComfyUI `output/`; falls back to HTTP `/view` if unset |
| `--claude-model` | `opus` | model for the Claude Code CLI |
| `--llm-workers` | `3` | LLM prompt jobs kept in flight, drafting ahead of the serial renderer (Claude only; a local `--llm-server` is pinned to 1) |
| `--seed` | `42` | base seed; image _i_ uses `seed + i` |
| `--no-images` | off | generate prompts only, print them, skip ComfyUI |
| `--force` | off | force-overwrite: re-render even if the output exists (otherwise skip it) |

## Region prompting

By default the LLM writes one plain-text prompt per image. With `--regions` it
instead emits a **structured, coordinate-placed spec** that drives ComfyUI's
`Ideogram4PromptBuilderKJ` node — an overall description plus a list of
`elements`, each a box (`x, y, w, h` as fractions of the frame, top-left origin)
with its own description. This gives the model explicit control over *where*
things sit in the frame, not just *what's* in it.

```bash
# Region mode, prompts only (see the specs without rendering).
.venv/bin/python reimagine.py --regions --no-images --llm-server 127.0.0.1:9503

# Region mode, full render.
.venv/bin/python reimagine.py --regions --samba-root ~/Desktop/MyShare
```

The LLM returns JSON inside `<regions>…</regions>` tags (retried up to 3× if it
fails to parse or validate). Validation is deliberately lenient — every styling
field (`aesthetics`, `lighting`, `style`, `palette`) is optional, and text
elements are only used when the reference genuinely contains prominent lettering.
The spec is patched into node `14` of
`workflows/krea2_regions_comfyui_t2i_aitrepeneur_jpg_api.json`; the same
seed / aspect-ratio / single-file-per-image machinery as manual mode applies. A
readable rendering of the spec is saved to `prompts.yaml` so it shows up in the
gallery just like a plain-text prompt.

Manual (plain-text) prompting remains the default — pass `--regions` to opt in.

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

The `output` symlink (a convenience default for `reimagine.py --output-dir`)
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

- **Video** — generate LTX 2.3 videos from the rendered stills.

Done: **rich krea prompts** — the LLM can now emit a structured,
coordinate-placed spec (see [Region prompting](#region-prompting), `--regions`).
