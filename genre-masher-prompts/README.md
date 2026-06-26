# Genre Masher

Generate absurd genre mashup pitches — with AI-rendered movie posters and synopses — by combining two random sub-genres and a broad character archetype.

Two pieces:

1. **`index.html`** — a single-page browser game. Click "Smash Genres!" to roll two sub-genres + a character archetype, get a randomized pitch, and optionally generate an AI poster via Pollinations.ai. (Loads `genres.json`, so serve it over HTTP — see below.)
2. **`generate_mashups.py`** — batch generator that, for each mashup, asks an LLM for a film title + synopsis + poster spec, drives ComfyUI to render a poster, and produces a static HTML gallery. The text LLM is selectable via `--llm` (Claude Code CLI by default; a local llama.cpp server otherwise). The image model is selectable via `--backend`:
   - **`krea`** (default) — the LLM emits a full structured, **photoreal** poster layout (palette + placed title/tagline/billing/photographed elements with normalized coordinates) for the **Krea** text-to-image model. Renders a 1152×1728 (2:3 one-sheet, ~2 MP) poster in ~30s.
   - **`ideogram4`** — same structured contract for the **Ideogram 4** model. Renders a 1536×2304 (~3.5 MP) poster with crisp text, but ~7 min each — high quality, slow.
   - **`zimage`** — the original path. The LLM emits one long natural-language prompt for the **Z-Image** model; renders a 1280×1664 (~3:4) poster.

## Files

```
.
├── index.html                     # browser game (serve over HTTP — loads genres.json)
├── genres.json                    # shared genre + character data (script AND game)
├── generate_mashups.py            # batch CSV + HTML + image pipeline
├── workflows/                     # ComfyUI API workflows, one per backend
│   ├── comfy_art_workflow_api.json            # Z-Image art variant
│   ├── ideogram4_t2i_api.json                 # Ideogram 4 structured-poster variant
│   └── krea2_comfyui_t2i_aitrepeneur_api.json # Krea structured-poster variant
├── requirements.txt               # client deps (websocket-client, pillow)
├── mashups_krea.csv / .html       # Krea batch + gallery
├── mashups_ideogram4.csv / .html  # Ideogram 4 batch + gallery
├── mashups_zimage.csv / .html     # Z-Image batch + gallery
└── images/
    ├── krea/                       # Krea posters (PNG)
    ├── ideogram4/                  # Ideogram 4 posters (PNG, ~6-8 MB each)
    └── zimage/                     # Z-Image posters (PNG)
```

Each backend writes its own CSV, its own gallery, and its own `images/<backend>/`
subdir. The galleries cross-link via a nav bar at the top, so you can flip
between the Krea, Ideogram 4, and Z-Image batches.

## Browser game

The game loads its genre data from `genres.json` via `fetch()`, so it must be **served over HTTP** rather than opened as a `file://` — run `python -m http.server` in this folder and visit the printed URL. Spacebar rerolls; lock buttons pin individual slots. The poster generator hits `image.pollinations.ai` (free, keyless) on demand. There's a link near the top to the pre-rendered batch galleries.

Combination space: 11 major genres × ~8 sub-genres each = 90 sub-genres, paired with 36 character archetypes. **Thousands of distinct genre-pairs**, and the LLM invents fresh specifics on top of each one, so the practical space is effectively unbounded.

## Batch generator

### Requirements

- A text LLM, selected with `--llm`:
  - **`claude`** (default) — the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) on your `PATH`. Each pitch is a fresh `claude -p` invocation with tools disabled (pure generation). Much higher quality than a local model.
  - **`llama`** — any llama.cpp-compatible OpenAI-style `/v1/chat/completions` server (`--llm-server`). The system prompts are tuned for reasoning models like Qwen3 — they let the model think, then emit tags after the reasoning.
  - Either way the model emits tags after any reasoning: `<title>`/`<synopsis>`/`<poster>` (ideogram4) or `<title>`/`<synopsis>`/`<positive>` (zimage).
- A ComfyUI server with the matching workflow loaded (`workflows/krea2_comfyui_t2i_aitrepeneur_api.json`, `workflows/ideogram4_t2i_api.json`, or `workflows/comfy_art_workflow_api.json`).
- Python 3.9+ with the deps in `requirements.txt`.

Set up a dedicated virtualenv with [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -r requirements.txt

.venv/bin/python generate_mashups.py 30
```

### Usage

```bash
# Default: Claude Code LLM, ideogram4 backend, ComfyUI at 127.0.0.1:8188
# Writes mashups_ideogram4.csv/.html and images/ideogram4/
python generate_mashups.py 30

# Use the local llama.cpp server instead of Claude Code
python generate_mashups.py 30 --llm llama --llm-server http://127.0.0.1:9503

# Use the original Z-Image backend instead
# Writes mashups_zimage.csv/.html and images/zimage/
python generate_mashups.py 30 --backend zimage

# Custom servers and paths
python generate_mashups.py 50 my_run.csv \
    --backend krea \
    --comfy-server 192.168.33.101:8188 \
    --html my_run.html \
    --images-dir my_run_images

# Skip image generation (CSV only — fast)
python generate_mashups.py 100 --no-images

# Skip the LLM entirely (just base mashups, no titles/synopses)
python generate_mashups.py 100 --no-llm
```

### How it works

For each pitch:

1. Roll two random sub-genres (different majors) and a broad character archetype.
2. Build a "seed pitch" from a template — used only as inspiration for the LLM, not as final copy.
3. Ask the LLM (one round-trip) to produce three tagged outputs:
   - `<title>` — punchy 3–7 word fake film title
   - `<synopsis>` — 3–5 sentence streaming-style logline (the LLM invents the protagonist's specific name, predicament, and quirks from the broad archetype)
   - the poster spec, which depends on the backend:
     - **krea** / **ideogram4**: a `<poster>` block containing a JSON object — `high_level_description`, `background`, `art_style`, `aesthetics`, `lighting`, a hex `palette`, `bg_brightness`, and an `elements` array of placed text/illustration blocks each with normalized `x`/`y`/`w`/`h` coordinates. krea's prompt steers it toward photoreal/cinematic; ideogram4's allows stylized illustration. The JSON is validated and lightly repaired (code fences stripped, off-canvas boxes clamped) before use.
     - **zimage**: a `<positive>` block — one long natural-language prompt with the title baked in as visible poster text.
4. Patch the poster spec + a fresh seed into the matching ComfyUI workflow and render the PNG (1152×1728 for krea, 1536×2304 for ideogram4, 1280×1664 for zimage).
5. Save the PNG **as-is** (no transcoding, no resizing).
6. Rewrite the backend's CSV and gallery (e.g. `mashups_krea.csv` / `.html`) after every row, so partial runs are usable.

### Pipelining

LLM and image generation run concurrently. While ComfyUI renders row N's poster, the LLM is already drafting row N+1's title and synopsis. In steady state, log rows show `llm 0.0s` because the next row's text was ready before the current image finished. A 45-row run averaged ~67s per pitch end-to-end.

### Resilience

- The LLM call retries up to 3 times if the response is missing or fails the tag-extraction sanity checks.
- CSV and HTML are flushed after every successful row. If you Ctrl-C halfway through, you keep what you have.
- Image rendering errors are logged but don't kill the run — the row still gets a CSV entry, just with no `image_file`.

### Output format

CSV columns:

| column | meaning |
| --- | --- |
| `genre_1_major` / `genre_1_sub` | first sub-genre and its major category |
| `genre_2_major` / `genre_2_sub` | second sub-genre (always a different major) |
| `character` | character archetype (e.g. *Lighthouse Keeper*) |
| `pitch` | template-built seed pitch (used as LLM inspiration) |
| `title` | LLM-generated film title |
| `synopsis` | LLM-generated 3–5 sentence synopsis |
| `image_prompt` | LLM-generated poster spec — the Z-Image prompt string, or the structured poster JSON (pretty-printed) for krea/ideogram4 |
| `image_file` | filename of the saved PNG (lives in `images/<backend>/`) |

The HTML gallery shows poster + title + synopsis prominently, with a collapsible "Behind the scenes" panel exposing the seed pitch and image prompt.

> **Note:** ideogram4 posters render much slower than Z-Image (~7 min vs ~50s each on the test rig), because Ideogram 4 is a larger model. Plan batch sizes accordingly.

## Tuning

- **Posters too small / too big in the gallery?** The gallery is a single centered column; adjust `max-width: 900px` on `.grid` inside `HTML_HEAD` (raise it for bigger posters on a 4K monitor).
- **Different aspect ratio or size?** Change `poster_w`/`poster_h` for the backend in the `BACKENDS` registry. ComfyUI expects multiples of 64.
- **More variety in titles (llama)?** Bump `temperature` in `LlamaLLM.chat` (currently 0.95).
- **A new image backend?** Add an entry to the `BACKENDS` registry (system prompt, `<tag>` extractor, workflow-patch function, dimensions) and a matching `GALLERY_META` entry (label, ratio, paths, credit). The pipeline, CSV, and gallery are backend-agnostic.
- **A different Claude model?** Pass `--claude-model` (e.g. `opus`, `haiku`). For llama, point `--llm-server` at any OpenAI-compatible endpoint.
