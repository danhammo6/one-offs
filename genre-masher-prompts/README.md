# Genre Masher

Generate absurd genre mashup pitches — with AI-rendered movie posters and synopses — by combining two random sub-genres and a randomly built character archetype.

Two pieces:

1. **`index.html`** — a single-page browser game. Click "Smash Genres!" to roll two sub-genres + a character + a quirk, get a randomized pitch, and optionally generate an AI poster via Pollinations.ai.
2. **`generate_mashups.py`** — batch generator that, for each mashup, calls a local llama.cpp server for a film title + synopsis + Z-Image prompt, drives ComfyUI to render a 1280×1664 poster, and produces a static HTML gallery.

## Files

```
.
├── index.html                     # standalone browser game (no server needed)
├── generate_mashups.py            # batch CSV + HTML + image pipeline
├── comfy_art_workflow_api.json    # ComfyUI workflow (Z-Image art variant)
├── mashups.csv                    # last batch run output
├── mashups.html                   # last batch gallery (open in a browser)
└── images/                        # generated posters (PNG, ~3 MB each)
```

## Browser game

Just open `index.html` in any browser — no server required. Spacebar rerolls; lock buttons pin individual slots. The poster generator hits `image.pollinations.ai` (free, keyless) on demand. There's a link near the top to `mashups.html`, the gallery of pre-rendered batch pitches.

Combination space: 12 major genres × ~9 sub-genres each = 99 sub-genres, paired with 56 characters and 56 quirks. **Roughly 28 million distinct pitches.** Pre-generating them all would take about 4.5 years of continuous rendering, so on-demand it is.

## Batch generator

### Requirements

- A llama.cpp-compatible chat server (any OpenAI-style `/v1/chat/completions` endpoint). The included system prompt is tuned for reasoning models like Qwen3 — it lets the model think and emits `<title>`/`<synopsis>`/`<positive>` tags after the reasoning.
- A ComfyUI server with the Z-Image art workflow loaded.
- Python 3.9+ with `websocket-client` installed.

The `realOrAi/tools/.venv` already has the right deps if you have that repo:

```bash
/Users/dhammond/spike/realOrAi/tools/.venv/bin/python generate_mashups.py 30
```

### Usage

```bash
# Default: llama.cpp at 127.0.0.1:9503, ComfyUI at 127.0.0.1:8188
python generate_mashups.py 30

# Custom servers and paths
python generate_mashups.py 50 my_run.csv \
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
   - `<positive>` — long natural-language Z-Image prompt with the title baked in as visible poster text
4. Patch the prompt + a fresh seed into the ComfyUI workflow and render a 1280×1664 PNG.
5. Save the PNG **as-is** (no transcoding, no resizing).
6. Rewrite `mashups.csv` and `mashups.html` after every row, so partial runs are usable.

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
| `image_prompt` | LLM-generated Z-Image prompt |
| `image_file` | filename of the saved PNG, relative to `images-dir` |

The HTML gallery shows poster + title + synopsis prominently, with a collapsible "Behind the scenes" panel exposing the seed pitch and image prompt.

## Tuning

- **Posters too small / too big?** Adjust `minmax(520px, 1fr)` in the CSS `grid-template-columns` rule inside `HTML_HEAD`.
- **Different aspect ratio?** Change `POSTER_W, POSTER_H` (currently 3:4 portrait at 1280×1664). ComfyUI expects multiples of 64.
- **More variety in titles?** Bump `temperature` in `_post_chat` (currently 0.95).
- **Different model?** The system prompt is model-agnostic — it just asks for tagged output after any reasoning. Should work with non-reasoning models too; they'll just emit the tags directly.
