# Genre Masher

Generate absurd genre mashup pitches — with AI-rendered movie posters and synopses — by combining two random sub-genres and a randomly built character archetype.

Two pieces:

1. **`index.html`** — a single-page browser game. Click "Smash Genres!" to roll two sub-genres + a character + a quirk, get a randomized pitch, and optionally generate an AI poster via Pollinations.ai.
2. **`generate_mashups.py`** — batch generator that, for each mashup, calls a local llama.cpp server for a film title + synopsis + poster spec, drives ComfyUI to render a poster, and produces a static HTML gallery. Two image backends are selectable via `--backend`:
   - **`ideogram4`** (default) — the LLM emits a full structured poster layout (palette + placed title/tagline/billing/illustrated elements with normalized coordinates) for the **Ideogram 4** text-to-image model. Renders a 1536×2304 (2:3 theatrical one-sheet) poster with crisp, legible text.
   - **`zimage`** — the original path. The LLM emits one long natural-language prompt for the **Z-Image** model; renders a 1280×1664 (~3:4) poster.

## Files

```
.
├── index.html                     # standalone browser game (no server needed)
├── generate_mashups.py            # batch CSV + HTML + image pipeline
├── workflows/                     # ComfyUI API workflows, one per backend
│   ├── comfy_art_workflow_api.json            # Z-Image art variant
│   ├── ideogram4_t2i_api.json                 # Ideogram 4 structured-poster variant
│   └── krea2_comfyui_t2i_aitrepeneur_api.json # Krea (future backend)
├── requirements.txt               # client deps (websocket-client, pillow)
├── mashups_zimage.csv / .html     # Z-Image batch + gallery
├── mashups_ideogram4.csv / .html  # Ideogram 4 batch + gallery
└── images/
    ├── zimage/                     # Z-Image posters (PNG)
    └── ideogram4/                  # Ideogram 4 posters (PNG, ~6-8 MB each)
```

Each backend writes its own CSV, its own gallery, and its own `images/<backend>/`
subdir. The galleries cross-link via a nav bar at the top, so you can flip
between the Z-Image and Ideogram 4 batches.

## Browser game

Just open `index.html` in any browser — no server required. Spacebar rerolls; lock buttons pin individual slots. The poster generator hits `image.pollinations.ai` (free, keyless) on demand. There's a link near the top to the pre-rendered batch galleries (`mashups_ideogram4.html` / `mashups_zimage.html`).

Combination space: 12 major genres × ~9 sub-genres each = 99 sub-genres, paired with 56 characters and 56 quirks. **Roughly 28 million distinct pitches.** Pre-generating them all would take about 4.5 years of continuous rendering, so on-demand it is.

## Batch generator

### Requirements

- A llama.cpp-compatible chat server (any OpenAI-style `/v1/chat/completions` endpoint). The included system prompts are tuned for reasoning models like Qwen3 — they let the model think, then emit tags after the reasoning: `<title>`/`<synopsis>`/`<poster>` (ideogram4) or `<title>`/`<synopsis>`/`<positive>` (zimage).
- A ComfyUI server with the matching workflow loaded (`workflows/ideogram4_t2i_api.json` or `workflows/comfy_art_workflow_api.json`).
- Python 3.9+ with the deps in `requirements.txt`.

Set up a dedicated virtualenv with [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -r requirements.txt

.venv/bin/python generate_mashups.py 30
```

### Usage

```bash
# Default: ideogram4 backend, llama.cpp at 127.0.0.1:9503, ComfyUI at 127.0.0.1:8188
# Writes mashups_ideogram4.csv/.html and images/ideogram4/
python generate_mashups.py 30

# Use the original Z-Image backend instead
# Writes mashups_zimage.csv/.html and images/zimage/
python generate_mashups.py 30 --backend zimage

# Custom servers and paths
python generate_mashups.py 50 my_run.csv \
    --backend ideogram4 \
    --llm-server http://127.0.0.1:9503 \
    --comfy-server 127.0.0.1:8188 \
    --html my_run.html \
    --images-dir my_run_images

# Skip image generation (CSV only — fast)
python generate_mashups.py 100 --no-images

# Skip the LLM entirely (just base mashups, no titles/synopses)
python generate_mashups.py 100 --no-llm
```

### How it works

For each pitch:

1. Roll two random sub-genres (different majors), a character, and a quirk.
2. Build a "seed pitch" from a template — used only as inspiration for the LLM, not as final copy.
3. Ask the LLM (one round-trip) to produce three tagged outputs:
   - `<title>` — punchy 3–7 word fake film title
   - `<synopsis>` — 3–5 sentence streaming-style logline
   - the poster spec, which depends on the backend:
     - **ideogram4**: a `<poster>` block containing a JSON object — `high_level_description`, `background`, `art_style`, `aesthetics`, `lighting`, a hex `palette`, `bg_brightness`, and an `elements` array of placed text/illustration blocks each with normalized `x`/`y`/`w`/`h` coordinates. The JSON is validated and lightly repaired (code fences stripped, off-canvas boxes clamped) before use.
     - **zimage**: a `<positive>` block — one long natural-language prompt with the title baked in as visible poster text.
4. Patch the poster spec + a fresh seed into the matching ComfyUI workflow and render the PNG (1536×2304 for ideogram4, 1280×1664 for zimage).
5. Save the PNG **as-is** (no transcoding, no resizing).
6. Rewrite the backend's CSV and gallery (e.g. `mashups_ideogram4.csv` / `.html`) after every row, so partial runs are usable.

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
| `character` | character archetype (e.g. *HOA-President Lich*) |
| `quirk` | character quirk (e.g. *with a vendetta against a specific seagull*) |
| `pitch` | template-built seed pitch (used as LLM inspiration) |
| `title` | LLM-generated film title |
| `synopsis` | LLM-generated 3–5 sentence synopsis |
| `image_prompt` | LLM-generated poster spec — the Z-Image prompt string, or the Ideogram poster JSON (pretty-printed) |
| `image_file` | filename of the saved PNG (lives in `images/<backend>/`) |

The HTML gallery shows poster + title + synopsis prominently, with a collapsible "Behind the scenes" panel exposing the seed pitch and image prompt.

> **Note:** ideogram4 posters render much slower than Z-Image (~7 min vs ~50s each on the test rig), because Ideogram 4 is a larger model. Plan batch sizes accordingly.

## Tuning

- **Posters too small / too big in the gallery?** Adjust `minmax(520px, 1fr)` in the CSS `grid-template-columns` rule inside `HTML_HEAD`.
- **Different aspect ratio or size?** Change `poster_w`/`poster_h` for the backend in the `BACKENDS` registry. ComfyUI expects multiples of 64.
- **More variety in titles?** Bump `temperature` in `_post_chat` (currently 0.95).
- **A new image backend?** Add an entry to the `BACKENDS` registry: a system prompt, a `<tag>` extractor, a workflow-patch function, and poster dimensions. The pipeline, CSV, and gallery are backend-agnostic.
- **Different model?** The system prompts are model-agnostic — they just ask for tagged output after any reasoning. Should work with non-reasoning models too; they'll just emit the tags directly.
