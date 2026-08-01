# reimagine

Recreate reference images as dynamic-posture stills and LTX 2.3 videos. The
pipeline is deliberately split into two processes so an LLM server and ComfyUI
never need to fit in VRAM at the same time:

```text
reference images -> generate_prompts.py -> pipeline.yaml
pipeline.yaml     -> render_media.py     -> JPEGs + MP4s
```

`serve.py` provides a gallery for comparing the generated media with its
references.

## Setup

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv -r requirements.txt
```

ComfyUI defaults to `127.0.0.1:8188`. `--comfyui-output-dir` is optional; when
provided, artifacts are read directly from a local or mounted ComfyUI `output/`
directory. HTTP `/view` is the fallback.

## Two-phase workflow

Generate both still and video prompts while only the LLM is loaded:

```bash
.venv/bin/python generate_prompts.py \
  --stage all \
  --still-mode regions \
  --video-basis reference \
  --llm-server 127.0.0.1:9503 \
  --output-dir outputs/local-regions
```

Stop the LLM server, start ComfyUI, then render all stills followed by all
videos:

```bash
.venv/bin/python render_media.py \
  --stage all \
  --output-dir outputs/local-regions \
  --comfyui-output-dir ~/Desktop/MyShare
```

The renderer uploads each final local JPEG to ComfyUI's input directory before
starting LTX. The exact image shown in the gallery is therefore the video's first
frame; video rendering does not depend on stale ComfyUI output staging.

## Prompt stages

`generate_prompts.py` runs serially and checkpoints `pipeline.yaml` after each
item. It never imports or contacts ComfyUI.

| flag | default | meaning |
| --- | --- | --- |
| `--stage` | `all` | generate `stills`, `videos`, or `all` plans |
| `--still-mode` | `manual` | plain `manual` prompt or structured `regions` spec |
| `--video-basis` | `reference` | generate motion from the `reference` or actual `rendered` still |
| `--input-dir` | `input` | reference-image tree |
| `--output-dir` | `output` | output set and default manifest location |
| `--manifest` | `<output-dir>/pipeline.yaml` | alternate manifest path |
| `--duration` | `10` | video duration in seconds, 1-30 |
| `--llm-server` | *(none)* | OpenAI-compatible multimodal server; otherwise Claude Code |
| `--force` | off | regenerate requested plan stages |

The default `reference` video mode uses the original reference plus the
validated still plan. Its dedicated system prompt deliberately requests
conservative motion that does not depend on exact generated limb geometry.
Video prompt detail scales with `--duration`: the planner targets roughly
8-16 words per second (80-160 words for 10 seconds and 160-320 words for 20
seconds), up to a 500-word maximum. Longer prompts use related temporal beats,
evolving synchronized audio, and an explicit final state rather than adding
unrelated action or re-describing the first frame.

## Exact-frame prompting

For higher fidelity, use an additional LLM phase that inspects the actual still:

```bash
# 1. LLM: still plans
.venv/bin/python generate_prompts.py --stage stills --still-mode regions \
  --output-dir outputs/exact

# 2. ComfyUI: still renders
.venv/bin/python render_media.py --stage stills --output-dir outputs/exact

# 3. LLM: video prompts grounded in those exact JPEGs
.venv/bin/python generate_prompts.py --stage videos --still-mode regions \
  --video-basis rendered --output-dir outputs/exact

# 4. ComfyUI: videos
.venv/bin/python render_media.py --stage videos --output-dir outputs/exact
```

Rendered-basis video plans record the JPEG SHA-256. Rerendering a still requires
regenerating its rendered-basis video prompt before the video phase.

## Render stages

`render_media.py` never imports or constructs an LLM. It can run without the
reference tree because `pipeline.yaml` contains every required prompt, path,
dimension, and duration. Seeds are a renderer concern and are not stored in the
prompt manifest.

| flag | default | meaning |
| --- | --- | --- |
| `--stage` | `all` | render `stills`, `videos`, or all stills then all videos |
| `--output-dir` | `output` | media output set |
| `--manifest` | `<output-dir>/pipeline.yaml` | alternate pipeline manifest |
| `--state-file` | `<output-dir>/render_state.yaml` | render fingerprints and output hashes |
| `--comfy-server` | `127.0.0.1:8188` | ComfyUI server |
| `--comfyui-output-dir` | *(none)* | optional local/mounted ComfyUI output directory |
| `--still-workflow` | mode default | custom Krea API workflow |
| `--video-workflow` | checked-in LTX workflow | custom LTX API workflow |
| `--clip-name` | workflow value | optional still-workflow CLIP override |
| `--unet-name` | workflow value | optional still-workflow UNet override |
| `--video-clip-name` | workflow value | optional LTX text-encoder override |
| `--video-unet-name` | workflow value | optional LTX diffusion-model override |
| `--seed` | `42` | base render seed; item i uses seed plus its sorted index |
| `--force` | off | rerender requested stages |

`render_state.yaml` tracks plan fingerprints and output hashes. A changed still
invalidates its dependent video. Corrupt or stale files are not silently
accepted merely because a path exists.

## Manifests and gallery metadata

`pipeline.yaml` is the authoritative, versioned handoff between the two
processes. Each item stores:

- Stable source-relative ID, source path, and source SHA-256
- Exact manual prompt or complete validated region spec
- Still output path and dimensions
- Video output path, prompt, duration, prompt basis, and basis hash

The pipeline also rebuilds per-directory `prompts.yaml` and
`video_prompts.yaml`. These are gallery projections, not rendering inputs.

## Batch scripts

```bash
scripts/generate_plans.sh  # LLM-only pass
scripts/render_plans.sh    # ComfyUI-only pass
```

Both scripts accept environment overrides documented in their source.

## Gallery

```bash
.venv/bin/python serve.py              # http://127.0.0.1:8000
.venv/bin/python serve.py --port 9000
```

Output sets live under `outputs/`. The gallery discovers sets containing images,
shows references beside generated stills, and switches to sibling videos when
available.
