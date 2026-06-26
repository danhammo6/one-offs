#!/usr/bin/env python3
"""Generate genre mashup pitches with AI titles, prompts, posters, and a gallery page.

The pipeline runs the LLM (title + synopsis + poster spec) and ComfyUI (image
render) concurrently — while ComfyUI renders row N's poster, the LLM is already
drafting row N+1. Each completed row gets written to the CSV and the HTML gallery,
so a partial run is still useful.

Text LLM is selectable with --llm:
    - claude (default): shells out to the Claude Code CLI (`claude -p`), tools off.
    - llama: a local llama.cpp OpenAI-compatible server (--llm-server).

Image backend is selectable with --backend:
    - krea (default): photoreal structured poster layout for the Krea model
      (~2 MP, ~30s/render). Same JSON contract as ideogram4 but photo-first.
    - ideogram4: structured poster layout for the Ideogram 4 model (~3.5 MP,
      ~7 min/render — high quality but slow).
    - zimage: LLM emits one long natural-language prompt for the Z-Image model.

Requires:
    - Claude Code CLI on PATH (default LLM), or a llama.cpp server for --llm llama
    - ComfyUI server with the matching workflow loaded (default 127.0.0.1:8188)
    - deps from requirements.txt (websocket-client, pillow) — see README for uv setup

Usage:
    python generate_mashups.py <count> [output.csv]
                               [--llm {claude,llama}] [--claude-model NAME]
                               [--backend {krea,ideogram4,zimage}]
                               [--llm-server URL] [--comfy-server HOST:PORT]
                               [--images-dir DIR] [--html FILE] [--no-images]

Example:
    python generate_mashups.py 30
    python generate_mashups.py 30 --llm llama --backend zimage
    python generate_mashups.py 5 --no-images       # CSV only, no posters
    .venv/bin/python generate_mashups.py 50
"""

import argparse
import concurrent.futures
import copy
import csv
import json
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html import escape as html_escape
from pathlib import Path

# GENRES and CHARACTERS live in genres.json — the single source of truth shared
# with the browser game (index.html). Sub-genres and characters are deliberately
# broad anchors; the LLM supplies the absurd specificity in the title/synopsis.
_GENRES_PATH = Path(__file__).parent / "genres.json"
_genre_data = json.loads(_GENRES_PATH.read_text(encoding="utf-8"))
GENRES = _genre_data["genres"]
CHARACTERS = _genre_data["characters"]

PITCH_TEMPLATES = [
    "It's {g1} meets {g2}... but the protagonist is a {c}.",
    "Imagine if a {g1} show had a baby with a {g2} miniseries, and that baby grew up to follow a {c}.",
    "Set in a world where {g1} and {g2} coexist uneasily, our hero — a {c} — must save... something. Probably a town.",
    "Pitch: {g1} x {g2}. Tone: dread, but make it horny. Lead: a {c}.",
    "A {c} stumbles into a {g1} conspiracy that can only be solved using the rules of {g2}.",
    "Picture a {g1} setting, but everyone behaves like they're in a {g2}. Our reluctant hero: a {c}.",
    "Logline: When a {c} discovers their quiet life is actually a {g1}, they must master the genre conventions of {g2} to survive.",
    "Three-act structure: {g1} in act one, {g2} by act three, and a {c} sobbing into a microwave dinner the whole time.",
    "Cold open: a {c}, alone, in the rain. The genre? {g1}. The vibe? Unmistakably {g2}. Cue title card.",
]


def generate_mashup():
    major1 = random.choice(list(GENRES.keys()))
    major2 = random.choice([m for m in GENRES if m != major1])
    sub1 = random.choice(GENRES[major1])
    sub2 = random.choice(GENRES[major2])
    character = random.choice(CHARACTERS)
    template = random.choice(PITCH_TEMPLATES)
    pitch = template.format(g1=sub1, g2=sub2, c=character)
    return {
        "genre_1_major": major1,
        "genre_1_sub": sub1,
        "genre_2_major": major2,
        "genre_2_sub": sub2,
        "character": character,
        "pitch": pitch,
    }


ZIMAGE_SYSTEM_PROMPT = """You are a creative director writing absurd, funny film concepts.

For each mashup you receive, invent:
1. A punchy, ridiculous fake film title (3-7 words). It should feel like a real movie title — sometimes with a colon and subtitle. No emojis.
2. A 3-5 sentence streaming-service-style synopsis (a "logline-plus") that sells the film. Make it funny — lean into the absurd genre clash. You are given only a broad protagonist archetype (e.g. "Lighthouse Keeper"); invent the specific, funny details — name, predicament, and a quirk or two that fit the genre mashup. Hint at stakes, central conflict, and tone, but stay tight. Roughly 50-100 words.
3. A detailed prompt for the Z-Image stable diffusion model to generate a movie poster.

Rules for the image prompt:
- Use long, descriptive, complete sentences (not comma-separated tags). Z-Image responds well to natural language.
- Describe the scene, the central character, their costume and expression, the setting, the lighting, color palette, and visual mood.
- Explicitly include the film title as visible text on the poster, e.g.: large bold movie title text reading "THE TITLE HERE" across the top.
- Optionally include a tagline as visible text underneath.
- Aim for 80-150 words. Cinematic, painterly, vivid.
- Do NOT mention real actors or real franchises.

Think as much as you want first. Then output your final answer wrapped in EXACTLY these three XML tags, on their own lines, with nothing else inside them:

<title>The Film Title Goes Here</title>
<synopsis>The 3-5 sentence funny streaming-style synopsis goes here.</synopsis>
<positive>The full Z-Image prompt goes here as one or more long sentences, including the title in quotes as visible poster text.</positive>

The tags must appear after any thinking. Do not nest tags. Do not add attributes."""


IDEOGRAM4_SYSTEM_PROMPT = r"""You are a creative director AND a poster layout designer creating absurd, funny film concepts and the movie posters that sell them.

For each mashup you receive, invent:
1. A punchy, ridiculous fake film title (3-7 words). It should feel like a real movie title — sometimes with a colon and subtitle. No emojis.
2. A 3-5 sentence streaming-service-style synopsis (a "logline-plus") that sells the film. Make it funny — lean into the absurd genre clash. You are given only a broad protagonist archetype (e.g. "Lighthouse Keeper"); invent the specific, funny details — name, predicament, and a quirk or two that fit the genre mashup. Hint at stakes, central conflict, and tone, but stay tight. Roughly 50-100 words.
3. A complete movie-poster layout for the Ideogram 4 image model, expressed as JSON.

The poster is a vertical 2:3 theatrical one-sheet. You control the full composition: background, art style, color palette, lighting, and every placed element — both illustrated objects AND text blocks — each positioned with normalized coordinates.

COORDINATE SYSTEM: x, y, w, h are floats from 0.0 to 1.0. (x, y) is the TOP-LEFT corner of the element's bounding box; w and h are its width and height as fractions of the poster. (0,0) is the top-left of the poster, (1,1) the bottom-right. Keep every box fully on-canvas: x + w <= 1.0 and y + h <= 1.0.

COMPOSE LIKE A REAL MOVIE POSTER:
- A big title treatment, usually in the upper third or lower third.
- One clear focal character or central image occupying the middle.
- An optional tagline near the title.
- A billing block (small condensed credits) and a release-date line near the bottom.
- 1-3 supporting illustrated elements for atmosphere.

Think as much as you want first. Then output your final answer wrapped in EXACTLY these three tags, on their own lines, with nothing else inside them:

<title>The Film Title Goes Here</title>
<synopsis>The 3-5 sentence funny streaming-style synopsis goes here.</synopsis>
<poster>
{
  "high_level_description": "one vivid sentence describing the whole poster",
  "background": "2-4 sentences describing the full-bleed background: color field, texture, depth, any skyline or scenery",
  "art_style": "comma-separated art-style descriptors (illustration technique, texture, era, e.g. '1960s retro movie poster illustration, flat ink shapes, screen-print texture')",
  "aesthetics": "comma-separated mood/aesthetic words",
  "lighting": "one sentence describing the lighting",
  "palette": ["#RRGGBB", "#RRGGBB", "#RRGGBB", "#RRGGBB"],
  "bg_brightness": 55,
  "elements": [
    {"type": "obj",  "text": "", "desc": "full description of an illustrated object", "x": 0.30, "y": 0.34, "w": 0.40, "h": 0.50},
    {"type": "text", "text": "THE TITLE", "desc": "describes the typography, size, color, and treatment of these words", "x": 0.10, "y": 0.05, "w": 0.80, "h": 0.18}
  ]
}
</poster>

RULES FOR THE POSTER JSON:
- It MUST be valid JSON: double quotes everywhere, no trailing commas, no comments, no code fences.
- "palette" is an array of 4-6 hex color strings. "bg_brightness" is an integer 0-100 (how bright the background reads).
- Each element "type" is "obj" for an illustrated element or "text" for rendered words.
- For a "text" element, "text" holds the literal words to render (use \n for line breaks) and "desc" describes typography, size, color, and treatment.
- For an "obj" element, "text" is an empty string "" and "desc" fully describes the illustrated object.
- Include the film title as one large "text" element. Include a billing/credits "text" block near the bottom. Invent fake studio, director, and actor names freely — but do NOT use real actors or real franchises.
- Use 5-10 elements total. Do not let text blocks overlap each other illegibly.
- The tags must appear after any thinking. Do not nest tags. Do not add attributes."""


KREA_SYSTEM_PROMPT = r"""You are a creative director AND a poster layout designer creating absurd, funny film concepts and the movie posters that sell them.

For each mashup you receive, invent:
1. A punchy, ridiculous fake film title (3-7 words). It should feel like a real movie title — sometimes with a colon and subtitle. No emojis.
2. A 3-5 sentence streaming-service-style synopsis (a "logline-plus") that sells the film. Make it funny — lean into the absurd genre clash. You are given only a broad protagonist archetype (e.g. "Lighthouse Keeper"); invent the specific, funny details — name, predicament, and a quirk or two that fit the genre mashup. Hint at stakes, central conflict, and tone, but stay tight. Roughly 50-100 words.
3. A complete movie-poster layout for the Krea image model, expressed as JSON.

The poster is a vertical 2:3 theatrical one-sheet. You control the full composition: background, photographic style, color palette, lighting, and every placed element — both photographed subjects/objects AND text blocks — each positioned with normalized coordinates.

CRITICAL — THIS IS A PHOTOREALISTIC POSTER, NOT AN ILLUSTRATION. Compose it like a real big-budget movie poster shot by a cinematographer: live-action photography or high-end photoreal CGI, real actors, real sets, real lighting. Do NOT make it look hand-drawn, painted, cartoon, anime, comic-book, or like graphic-design vector art. Even for fantasy or animated-sounding genres, render it as a photoreal live-action film still UNLESS the genre is explicitly animation.

COORDINATE SYSTEM: x, y, w, h are floats from 0.0 to 1.0. (x, y) is the TOP-LEFT corner of the element's bounding box; w and h are its width and height as fractions of the poster. (0,0) is the top-left of the poster, (1,1) the bottom-right. Keep every box fully on-canvas: x + w <= 1.0 and y + h <= 1.0.

COMPOSE LIKE A REAL MOVIE POSTER:
- A big title treatment, usually in the upper third or lower third.
- One clear focal character or central image occupying the middle.
- An optional tagline near the title.
- A billing block (small condensed credits) and a release-date line near the bottom.
- 1-3 supporting photographed elements for atmosphere.

Think as much as you want first. Then output your final answer wrapped in EXACTLY these three tags, on their own lines, with nothing else inside them:

<title>The Film Title Goes Here</title>
<synopsis>The 3-5 sentence funny streaming-style synopsis goes here.</synopsis>
<poster>
{
  "high_level_description": "one vivid sentence describing the whole poster as a photoreal film image",
  "background": "2-4 sentences describing the full-bleed photographic background: location, depth, atmosphere, any skyline or scenery",
  "art_style": "comma-separated PHOTOGRAPHIC descriptors — camera, lens, film stock, grade (e.g. 'shot on 35mm anamorphic, shallow depth of field, teal-orange cinematic grade, photorealistic, volumetric haze')",
  "aesthetics": "comma-separated mood/aesthetic words",
  "lighting": "one sentence describing the cinematic lighting",
  "palette": ["#RRGGBB", "#RRGGBB", "#RRGGBB", "#RRGGBB"],
  "bg_brightness": 55,
  "elements": [
    {"type": "obj",  "text": "", "desc": "full photoreal description of a photographed subject or object", "x": 0.30, "y": 0.34, "w": 0.40, "h": 0.50},
    {"type": "text", "text": "THE TITLE", "desc": "describes the typography, size, color, and treatment of these words", "x": 0.10, "y": 0.05, "w": 0.80, "h": 0.18}
  ]
}
</poster>

RULES FOR THE POSTER JSON:
- It MUST be valid JSON: double quotes everywhere, no trailing commas, no comments, no code fences.
- "palette" is an array of 4-6 hex color strings. "bg_brightness" is an integer 0-100 (how bright the background reads).
- Each element "type" is "obj" for a photographed element or "text" for rendered words.
- For a "text" element, "text" holds the literal words to render (use \n for line breaks) and "desc" describes typography, size, color, and treatment.
- For an "obj" element, "text" is an empty string "" and "desc" fully describes the photographed subject/object photorealistically.
- Include the film title as one large "text" element. Include a billing/credits "text" block near the bottom. Invent fake studio, director, and actor names freely — but do NOT use real actors or real franchises.
- Use 5-10 elements total. Do not let text blocks overlap each other illegibly.
- The tags must appear after any thinking. Do not nest tags. Do not add attributes."""


def build_user_prompt(mashup):
    return (
        f"Genre 1: {mashup['genre_1_sub']} (a {mashup['genre_1_major']} subgenre)\n"
        f"Genre 2: {mashup['genre_2_sub']} (a {mashup['genre_2_major']} subgenre)\n"
        f"Protagonist archetype: {mashup['character']}\n"
        f"Throwaway pitch (for inspiration only — do not just rewrite it): {mashup['pitch']}\n\n"
        f"Now produce the title, synopsis, and image prompt in the required tag format."
    )


TITLE_RE = re.compile(r"<title>\s*(.+?)\s*</title>", re.DOTALL | re.IGNORECASE)
SYNOPSIS_RE = re.compile(r"<synopsis>\s*(.+?)\s*</synopsis>", re.DOTALL | re.IGNORECASE)
POSITIVE_RE = re.compile(r"<positive>\s*(.+?)\s*</positive>", re.DOTALL | re.IGNORECASE)
POSTER_RE = re.compile(r"<poster>\s*(.+?)\s*</poster>", re.DOTALL | re.IGNORECASE)


# ----------------------------------------------------------------------------
# LLM backends — both expose chat(system_prompt, user_prompt) -> (content, reasoning)
# ----------------------------------------------------------------------------

class LlamaLLM:
    """OpenAI-compatible chat server (llama.cpp). Reasoning models split their
    answer between `content` and `reasoning_content`; we return both."""

    name = "llama"

    def __init__(self, server_url, timeout=600):
        self.server_url = server_url
        self.timeout = timeout

    def describe(self):
        return self.server_url

    def chat(self, system_prompt, user_prompt):
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.95,
            "top_p": 0.95,
            "max_tokens": 16384,
            "stream": False,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.server_url.rstrip('/')}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        msg = body["choices"][0]["message"]
        # Prefer the actual answer in `content`; fall back to reasoning_content if
        # the model only emitted tags inside its thinking stream.
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        return content, reasoning


class ClaudeCodeLLM:
    """Shells out to the Claude Code CLI (`claude -p`). Each call is a fresh,
    stateless invocation: system prompt via --system-prompt, user text on stdin,
    tools disabled (pure generation), JSON envelope parsed for `.result`."""

    name = "claude"

    def __init__(self, model="sonnet", timeout=300, cli="claude"):
        self.model = model
        self.timeout = timeout
        self.cli = cli

    def describe(self):
        return f"Claude Code CLI ({self.model})"

    def chat(self, system_prompt, user_prompt):
        cmd = [
            self.cli, "-p",
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", system_prompt,
            "--disallowedTools", "*",   # pure text generation, no agentic tool use
        ]
        proc = subprocess.run(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude returned non-JSON: {proc.stdout.strip()[:200]!r}")
        if env.get("is_error") or env.get("subtype") != "success":
            raise RuntimeError(f"claude error envelope: subtype={env.get('subtype')}")
        # Claude doesn't expose a separate reasoning stream here; it's all in result.
        return (env.get("result") or "").strip(), ""


def _extract_title_synopsis(text):
    """Return (title, synopsis) from the LAST tag occurrence, or (None, None) if the
    tags are missing or fail sanity checks. Shared by all backends."""
    title_matches = TITLE_RE.findall(text)
    syn_matches = SYNOPSIS_RE.findall(text)
    if not title_matches or not syn_matches:
        return None, None
    title = title_matches[-1].strip().strip('"').strip("'").strip()
    synopsis = syn_matches[-1].strip()
    # Collapse line breaks the model sometimes puts in a title — both real
    # newlines and the literal two-char "\n" / "\r\n" escapes it occasionally emits.
    title = re.sub(r"\\[rn]|[\r\n]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    if not title or len(title) > 120 or len(title.split()) > 12:
        return None, None
    if len(synopsis) < 30 or len(synopsis) > 2000:
        return None, None
    return title, synopsis


def _extract_zimage(text):
    """Z-Image: return (title, synopsis, positive_prompt) or (None, None, None).

    Uses the LAST tag occurrence so example quotations inside the model's reasoning
    lose to its actual final answer."""
    title, synopsis = _extract_title_synopsis(text)
    pos_matches = POSITIVE_RE.findall(text)
    if not title or not pos_matches:
        return None, None, None
    positive = pos_matches[-1].strip()
    if len(positive) < 50 or len(positive) > 4000:
        return None, None, None
    return title, synopsis, positive


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _validate_poster(poster):
    """Validate/normalize the Ideogram poster dict. Returns the cleaned dict or None.

    Lenient on purpose — a small Q4 model produces slightly sloppy JSON, so we
    coerce where we safely can and only reject when the layout is unusable."""
    if not isinstance(poster, dict):
        return None
    required_text = ["high_level_description", "background", "art_style", "lighting"]
    if any(not isinstance(poster.get(k), str) or not poster[k].strip() for k in required_text):
        return None

    palette = poster.get("palette")
    if not isinstance(palette, list) or not (3 <= len(palette) <= 8):
        return None
    palette = [str(c).strip() for c in palette]
    if not all(re.fullmatch(r"#[0-9a-fA-F]{6}", c) for c in palette):
        return None

    elements = poster.get("elements")
    if not isinstance(elements, list) or not (3 <= len(elements) <= 16):
        return None
    has_text_element = False
    clean_elements = []
    for el in elements:
        if not isinstance(el, dict):
            return None
        etype = el.get("type")
        if etype not in ("obj", "text"):
            return None
        desc = el.get("desc")
        if not isinstance(desc, str) or not desc.strip():
            return None
        text = el.get("text") or ""
        if not isinstance(text, str):
            return None
        if etype == "text" and text.strip():
            has_text_element = True
        try:
            x, y, w, h = (float(el["x"]), float(el["y"]), float(el["w"]), float(el["h"]))
        except (KeyError, TypeError, ValueError):
            return None
        # Clamp boxes back on-canvas rather than rejecting the whole layout.
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        w = min(max(w, 0.01), 1.0 - x)
        h = min(max(h, 0.01), 1.0 - y)
        clean_elements.append({"type": etype, "text": text, "desc": desc.strip(),
                               "palette": [], "x": x, "y": y, "w": w, "h": h})
    if not has_text_element:  # a poster with no rendered words isn't a movie poster
        return None

    brightness = poster.get("bg_brightness", 55)
    try:
        brightness = int(round(float(brightness)))
    except (TypeError, ValueError):
        brightness = 55
    brightness = min(max(brightness, 0), 100)

    return {
        "high_level_description": poster["high_level_description"].strip(),
        "background": poster["background"].strip(),
        "art_style": poster["art_style"].strip(),
        "aesthetics": (poster.get("aesthetics") or "").strip(),
        "lighting": poster["lighting"].strip(),
        "palette": palette,
        "bg_brightness": brightness,
        "elements": clean_elements,
    }


def _extract_ideogram(text):
    """Ideogram 4: return (title, synopsis, poster_dict) or (None, None, None)."""
    title, synopsis = _extract_title_synopsis(text)
    poster_matches = POSTER_RE.findall(text)
    if not title or not poster_matches:
        return None, None, None
    raw = _FENCE_RE.sub("", poster_matches[-1].strip()).strip()
    try:
        poster = json.loads(raw)
    except json.JSONDecodeError:
        return None, None, None
    poster = _validate_poster(poster)
    if poster is None:
        return None, None, None
    return title, synopsis, poster


def call_llm(llm, mashup, backend, max_attempts=3):
    """Call the given LLM backend and extract the image-backend's tagged output.

    Retries on missing/malformed tags up to max_attempts. On retry, the retry
    nudge is appended to the user prompt (both LLM backends are stateless here).
    Returns (title, synopsis, payload). The payload type depends on the image
    backend: a prose string for Z-Image, a validated dict for Ideogram/Krea.
    Raises RuntimeError if all attempts fail.
    """
    last_error = None
    last_snippet = ""

    for attempt in range(1, max_attempts + 1):
        try:
            user_prompt = build_user_prompt(mashup)
            if attempt > 1:
                user_prompt += "\n\n" + backend.retry_nudge

            content, reasoning = llm.chat(backend.system_prompt, user_prompt)
            title, synopsis, payload = backend.extract(content)
            if not (title and synopsis and payload):
                title, synopsis, payload = backend.extract(content + "\n" + reasoning)
            if title and synopsis and payload:
                return title, synopsis, payload
            last_error = "tags not found / failed validation"
            last_snippet = (content[-200:] or reasoning[-200:]).replace("\n", " ")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_error = f"network: {e}"
        except subprocess.TimeoutExpired as e:
            last_error = f"claude timeout after {e.timeout}s"
        except Exception as e:
            last_error = f"unexpected: {e}"

    raise RuntimeError(f"{last_error} (last response snippet: {last_snippet!r})")


def format_eta(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# ----------------------------------------------------------------------------
# ComfyUI workflow patching (per backend)
# ----------------------------------------------------------------------------

# --- Z-Image art workflow ---------------------------------------------------
ZIMAGE_NODE_POSITIVE = "133"
ZIMAGE_NODE_NEGATIVE = "132"
ZIMAGE_NODE_LATENT = "130"
ZIMAGE_NODE_SEED = "509"


def _patch_zimage(base, payload, seed, poster_w, poster_h):
    """payload is the positive prompt string."""
    wf = copy.deepcopy(base)
    wf[ZIMAGE_NODE_POSITIVE]["inputs"]["text"] = payload
    wf[ZIMAGE_NODE_NEGATIVE]["inputs"]["text"] = (
        "watermark, blurry, low quality, distorted text, misspelled text"
    )
    wf[ZIMAGE_NODE_LATENT]["inputs"]["width"] = poster_w
    wf[ZIMAGE_NODE_LATENT]["inputs"]["height"] = poster_h
    wf[ZIMAGE_NODE_SEED]["inputs"]["noise_seed"] = seed
    return wf


# --- Ideogram 4 text-to-image workflow --------------------------------------
IDEOGRAM_NODE_BUILDER = "200"   # Ideogram4PromptBuilderKJ
IDEOGRAM_NODE_SEED = "165"      # RandomNoise
IDEOGRAM_NODE_WIDTH = "204"     # INTConstant WIDTH
IDEOGRAM_NODE_HEIGHT = "205"    # INTConstant HEIGHT


def _patch_ideogram(base, payload, seed, poster_w, poster_h):
    """payload is the validated poster dict from _validate_poster."""
    wf = copy.deepcopy(base)
    b = wf[IDEOGRAM_NODE_BUILDER]["inputs"]
    # Overwrite EVERY content field so no values from the example workflow's
    # baked-in poster (palette, copy, layout) can leak into a generated row.
    b["high_level_description"] = payload["high_level_description"]
    b["background"] = payload["background"]
    b["style"] = "art_style"
    b["style.art_style"] = payload["art_style"]
    b["aesthetics"] = payload["aesthetics"]
    b["lighting"] = payload["lighting"]
    b["medium"] = "graphic_design"  # deliberate poster default, not inherited
    b["bg_brightness"] = payload["bg_brightness"]
    # The builder node takes palette + elements as JSON-encoded strings.
    b["style_palette_data"] = json.dumps(payload["palette"])
    b["elements_data"] = json.dumps(payload["elements"])
    b["width"] = poster_w
    b["height"] = poster_h
    wf[IDEOGRAM_NODE_WIDTH]["inputs"]["value"] = poster_w
    wf[IDEOGRAM_NODE_HEIGHT]["inputs"]["value"] = poster_h
    wf[IDEOGRAM_NODE_SEED]["inputs"]["noise_seed"] = seed
    return wf


# --- Krea text-to-image workflow --------------------------------------------
# Krea reuses the same Ideogram4PromptBuilderKJ node and our validated poster
# dict, so _extract_ideogram / _validate_poster carry over unchanged. It differs
# in node IDs and in being photo-first (style="photo", medium="photograph").
KREA_NODE_BUILDER = "14"      # Ideogram4PromptBuilderKJ
KREA_NODE_LATENT = "78:76"    # EmptyLatentImage
KREA_NODE_SAMPLER = "78:75"   # KSampler (holds the seed)


def _patch_krea(base, payload, seed, poster_w, poster_h):
    """payload is the validated poster dict from _validate_poster."""
    wf = copy.deepcopy(base)
    b = wf[KREA_NODE_BUILDER]["inputs"]
    # Overwrite EVERY content field so nothing from the example workflow's
    # baked-in poster leaks into a generated row.
    b["high_level_description"] = payload["high_level_description"]
    b["background"] = payload["background"]
    b["style"] = "photo"                       # photo-first, not illustration
    b["style.photo"] = payload["art_style"]    # photographic descriptors
    b["aesthetics"] = payload["aesthetics"]
    b["lighting"] = payload["lighting"]
    b["medium"] = "photograph"                 # deliberate photoreal default
    b["bg_brightness"] = payload["bg_brightness"]
    b["style_palette_data"] = json.dumps(payload["palette"])
    b["elements_data"] = json.dumps(payload["elements"])
    b["width"] = poster_w
    b["height"] = poster_h
    wf[KREA_NODE_LATENT]["inputs"]["width"] = poster_w
    wf[KREA_NODE_LATENT]["inputs"]["height"] = poster_h
    wf[KREA_NODE_SAMPLER]["inputs"]["seed"] = seed
    return wf


# ----------------------------------------------------------------------------
# Backend registry
# ----------------------------------------------------------------------------

ZIMAGE_RETRY_NUDGE = (
    "Your previous response did not contain valid <title>...</title>, "
    "<synopsis>...</synopsis>, and <positive>...</positive> tags. Please respond "
    "again following the exact tag format from the system instructions."
)
IDEOGRAM_RETRY_NUDGE = (
    "Your previous response did not contain valid <title>...</title>, "
    "<synopsis>...</synopsis>, and <poster>...</poster> tags, or the <poster> block "
    "was not valid JSON matching the required schema. Respond again following the "
    "exact tag format from the system instructions. The <poster> block must be a "
    "single valid JSON object — double quotes only, no trailing commas, no code fences."
)


class Backend:
    """Bundles everything that differs between the Z-Image and Ideogram 4 pipelines:
    the ComfyUI workflow file, the LLM system prompt + retry nudge, the output
    extractor, the workflow-patch function, and the poster dimensions."""

    def __init__(self, name, workflow_file, system_prompt, retry_nudge,
                 extract, patch, poster_w, poster_h):
        self.name = name
        self.workflow_file = Path(__file__).parent / workflow_file
        self.system_prompt = system_prompt
        self.retry_nudge = retry_nudge
        self.extract = extract
        self._patch = patch
        self.poster_w = poster_w
        self.poster_h = poster_h
        self._workflow_base = None

    def load_workflow(self):
        if not self.workflow_file.exists():
            raise FileNotFoundError(
                f"Workflow file not found: {self.workflow_file}"
            )
        if self._workflow_base is None:
            self._workflow_base = json.loads(self.workflow_file.read_text())
        return self._workflow_base

    def patch(self, payload, seed):
        return self._patch(self.load_workflow(), payload, seed,
                           self.poster_w, self.poster_h)


# Movie-poster one-sheet is 2:3. Both backends render a 2:3 portrait; the
# dimensions are multiples of 64 (ComfyUI requirement) at roughly equal MP.
BACKENDS = {
    "zimage": Backend(
        name="zimage",
        workflow_file="workflows/comfy_art_workflow_api.json",
        system_prompt=ZIMAGE_SYSTEM_PROMPT,
        retry_nudge=ZIMAGE_RETRY_NUDGE,
        extract=_extract_zimage,
        patch=_patch_zimage,
        poster_w=1280, poster_h=1664,   # ~3:4, ~2.1 MP (unchanged Z-Image default)
    ),
    "ideogram4": Backend(
        name="ideogram4",
        workflow_file="workflows/ideogram4_t2i_api.json",
        system_prompt=IDEOGRAM4_SYSTEM_PROMPT,
        retry_nudge=IDEOGRAM_RETRY_NUDGE,
        extract=_extract_ideogram,
        patch=_patch_ideogram,
        poster_w=1536, poster_h=2304,   # exact 2:3 one-sheet, ~3.5 MP
    ),
    "krea": Backend(
        name="krea",
        workflow_file="workflows/krea2_comfyui_t2i_aitrepeneur_api.json",
        system_prompt=KREA_SYSTEM_PROMPT,
        retry_nudge=IDEOGRAM_RETRY_NUDGE,   # same <poster> JSON contract
        extract=_extract_ideogram,          # same structured-poster format
        patch=_patch_krea,
        poster_w=1152, poster_h=1728,   # exact 2:3 one-sheet, ~2.0 MP (fast)
    ),
}


class ComfyClient:
    def __init__(self, server):
        # Accept "host:port" or "http://host:port"
        if "://" in server:
            server = urllib.parse.urlparse(server).netloc or server.split("://", 1)[1]
        self.server = server.rstrip("/")
        self.client_id = str(uuid.uuid4())

    def _queue(self, prompt, prompt_id):
        data = json.dumps({
            "prompt": prompt,
            "client_id": self.client_id,
            "prompt_id": prompt_id,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.server}/prompt",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def _view(self, filename, subfolder, ftype):
        qs = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": ftype})
        with urllib.request.urlopen(f"http://{self.server}/view?{qs}", timeout=120) as r:
            return r.read()

    def _history(self, prompt_id):
        with urllib.request.urlopen(f"http://{self.server}/history/{prompt_id}", timeout=30) as r:
            return json.loads(r.read())

    def generate(self, prompt):
        try:
            import websocket  # websocket-client
        except ImportError:
            raise RuntimeError(
                "websocket-client is required for ComfyUI. "
                "Run with the realOrAi venv or: pip install websocket-client"
            )
        ws = websocket.WebSocket()
        ws.connect(f"ws://{self.server}/ws?clientId={self.client_id}", timeout=30)
        try:
            prompt_id = str(uuid.uuid4())
            self._queue(prompt, prompt_id)
            while True:
                msg = ws.recv()
                if not isinstance(msg, str):
                    continue
                data = json.loads(msg)
                t = data.get("type")
                if t == "executing":
                    d = data["data"]
                    if d.get("node") is None and d.get("prompt_id") == prompt_id:
                        break
                elif t == "execution_error":
                    raise RuntimeError(f"Comfy execution error: {data.get('data')}")
        finally:
            ws.close()

        hist = self._history(prompt_id).get(prompt_id, {})
        for _node, output in hist.get("outputs", {}).items():
            for img in output.get("images", []) or []:
                return self._view(img["filename"], img.get("subfolder", ""), img.get("type", "output"))
        raise RuntimeError("No image in ComfyUI history output")


# ----------------------------------------------------------------------------
# HTML gallery
# ----------------------------------------------------------------------------

HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Genre Masher: AI-Generated Pitches</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Abril+Fatface&family=Lora:ital,wght@0,400;0,600;1,400&family=Oswald:wght@600;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  body {
    font-family: 'Lora', Georgia, 'Times New Roman', serif;
    background: #0e0a1a;
    color: #f4eafc;
    margin: 0; padding: 40px 20px;
  }
  h1 { font-family: 'Abril Fatface', 'Playfair Display', Georgia, serif;
       font-size: clamp(2.5rem, 6vw, 4.5rem); text-align: center; margin: 0 0 6px;
       text-shadow: 4px 4px 0 #000, -2px -2px 0 #ff006e; letter-spacing: 1px;
       font-weight: normal; }
  .tagline { text-align: center; opacity: 0.7; font-style: italic; margin-bottom: 50px;
             font-size: 1.1rem; }
  /* Single centered column — posters render large on big/4K monitors. */
  .grid { display: grid; gap: 64px; max-width: 900px; margin: 0 auto;
          grid-template-columns: 1fr; }
  .card { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1);
          border-radius: 14px; overflow: hidden; display: flex; flex-direction: column;
          transition: transform 0.2s, box-shadow 0.2s; }
  .card:hover { transform: translateY(-4px); box-shadow: 0 14px 36px rgba(255,0,110,0.3); }
  .poster { width: 100%; aspect-ratio: __POSTER_RATIO__; object-fit: contain; background: #1a0033;
            display: block; }
  .poster.missing { display: flex; align-items: center; justify-content: center;
                    color: rgba(255,255,255,0.3); font-style: italic; padding: 20px;
                    text-align: center; aspect-ratio: __POSTER_RATIO__; }
  .meta { padding: 26px 30px 28px; }
  .title { font-family: 'Oswald', 'Helvetica Neue', sans-serif;
           font-size: 1.7rem; font-weight: 700; margin: 0 0 12px; line-height: 1.15;
           letter-spacing: 0.5px; text-transform: uppercase; }
  .badges { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
  .badge { font-family: 'Oswald', sans-serif; font-size: 0.75rem; padding: 4px 12px;
           border-radius: 12px; background: rgba(255,190,11,0.2); color: #ffbe0b;
           letter-spacing: 1px; text-transform: uppercase; }
  .pitch { font-family: 'Lora', Georgia, serif; font-size: 1.05rem; line-height: 1.6;
           opacity: 0.92; margin: 0 0 14px; }
  .character { font-family: 'Lora', Georgia, serif; font-size: 0.95rem; opacity: 0.75;
                font-style: italic; }
  details { margin-top: 14px; font-size: 0.9rem; opacity: 0.65; }
  details summary { cursor: pointer; font-family: 'Oswald', sans-serif;
                    text-transform: uppercase; letter-spacing: 1px; font-size: 0.8rem; }
  details pre { white-space: pre-wrap; word-break: break-word; margin: 4px 0 12px;
                font-family: 'Lora', Georgia, serif; font-size: 0.9rem; line-height: 1.55; }
  .bts-label { font-family: 'Oswald', sans-serif; font-weight: bold; margin-top: 10px;
                opacity: 0.85; text-transform: uppercase; letter-spacing: 0.5px;
                font-size: 0.8rem; }
  footer { text-align: center; opacity: 0.55; margin-top: 60px; font-size: 0.9rem;
            font-style: italic; line-height: 1.7; }
  footer a { color: #ffbe0b; text-decoration: none;
              border-bottom: 1px dashed rgba(255,190,11,0.5); }
  footer a:hover { color: #fff; border-bottom-color: #fff; }
  .breadcrumb {
    position: absolute;
    top: 16px; left: 20px;
    font-size: 0.85rem;
    opacity: 0.65;
  }
  .breadcrumb a {
    color: #fff;
    text-decoration: none;
    border-bottom: 1px dashed rgba(255,255,255,0.4);
    margin-right: 14px;
  }
  .breadcrumb a:hover { opacity: 1; border-bottom-color: #fff; }
  .gallery-nav { display: flex; justify-content: center; gap: 10px; flex-wrap: wrap;
                 margin: -34px 0 46px; }
  .gallery-nav .nav-link {
    font-family: 'Oswald', sans-serif; text-transform: uppercase; letter-spacing: 1px;
    font-size: 0.85rem; padding: 7px 18px; border-radius: 20px;
    border: 1px solid rgba(255,255,255,0.18); text-decoration: none;
    color: #f4eafc; opacity: 0.7; transition: opacity 0.2s, background 0.2s; }
  .gallery-nav a.nav-link:hover { opacity: 1; background: rgba(255,255,255,0.08); }
  .gallery-nav .nav-link.active {
    opacity: 1; color: #ffbe0b; border-color: rgba(255,190,11,0.6);
    background: rgba(255,190,11,0.12); cursor: default; }
</style>
</head>
<body>
  <div class="breadcrumb">
    <a href="../">&larr; one-offs</a>
    <a href="index.html">&larr; play the game</a>
  </div>
  <h1>Genre Masher 3000</h1>
  <div class="tagline">AI-generated pitches that nobody asked for</div>
  <nav class="gallery-nav">
    __GALLERY_NAV__
  </nav>
  <div class="grid">
"""

HTML_FOOT = """  </div>
  <footer>
    Generated by generate_mashups.py &middot; {count} pitches &middot; {timestamp}<br>
    Posters rendered with <a href="https://www.comfy.org/" target="_blank" rel="noopener">ComfyUI</a>
    {credit}
  </footer>
</body>
</html>
"""

# Per-backend gallery presentation: label, poster aspect ratio, default image
# subdir, output gallery filename, and credit line. The `label`/`html` entries
# also drive the cross-gallery navigation links, so adding a new backend's
# gallery is a single entry here.
GALLERY_META = {
    "zimage": {
        "label": "Z-Image",
        "ratio": "3/4",
        "images_dir": "images/zimage",
        "html": "mashups_zimage.html",
        "credit": ('using a Z-Image art workflow by '
                   '<a href="https://www.patreon.com/c/aitrepreneur" target="_blank" '
                   'rel="noopener">Aitrepreneur</a>.'),
    },
    "ideogram4": {
        "label": "Ideogram 4",
        "ratio": "2/3",
        "images_dir": "images/ideogram4",
        "html": "mashups_ideogram4.html",
        "credit": "using an Ideogram 4 text-to-image workflow.",
    },
    "krea": {
        "label": "Krea",
        "ratio": "2/3",
        "images_dir": "images/krea",
        "html": "mashups_krea.html",
        "credit": ('using a Krea text-to-image workflow by '
                   '<a href="https://www.patreon.com/c/aitrepreneur" target="_blank" '
                   'rel="noopener">Aitrepreneur</a>.'),
    },
}


def _gallery_nav(current_backend):
    """Build the cross-gallery navigation links (one per backend gallery),
    marking the current one as active."""
    links = []
    for name, meta in GALLERY_META.items():
        if name == current_backend:
            links.append(f'<span class="nav-link active">{html_escape(meta["label"])}</span>')
        else:
            links.append(
                f'<a class="nav-link" href="{html_escape(meta["html"])}">'
                f'{html_escape(meta["label"])}</a>'
            )
    return '\n    '.join(links)


def write_gallery(html_path, rows, images_href, backend_name="ideogram4"):
    """images_href is the relative path prefix from the gallery HTML to the
    poster files, e.g. "images/ideogram4"."""
    cards = []
    for row in rows:
        title = row.get("title") or "(untitled)"
        synopsis = row.get("synopsis") or ""
        seed_pitch = row.get("pitch") or ""
        g1 = row.get("genre_1_sub") or ""
        g2 = row.get("genre_2_sub") or ""
        character = row.get("character") or ""
        image_prompt = row.get("image_prompt") or ""
        image_file = row.get("image_file") or ""

        if image_file:
            poster_html = (
                f'<img class="poster" loading="lazy" '
                f'src="{html_escape(images_href)}/{html_escape(image_file)}" '
                f'alt="{html_escape(title)}">'
            )
        else:
            poster_html = '<div class="poster missing">No poster generated</div>'

        body_text = synopsis or seed_pitch  # fall back to seed pitch if LLM failed

        cards.append(f"""    <div class="card">
      {poster_html}
      <div class="meta">
        <div class="title">{html_escape(title)}</div>
        <div class="badges">
          <span class="badge">{html_escape(g1)}</span>
          <span class="badge">x</span>
          <span class="badge">{html_escape(g2)}</span>
        </div>
        <p class="pitch">{html_escape(body_text)}</p>
        <div class="character">Starring: <b>{html_escape(character)}</b></div>
        <details><summary>Behind the scenes</summary>
          <div class="bts-label">Seed pitch:</div><pre>{html_escape(seed_pitch)}</pre>
          <div class="bts-label">Image prompt:</div><pre>{html_escape(image_prompt)}</pre>
        </details>
      </div>
    </div>""")

    meta = GALLERY_META.get(backend_name, GALLERY_META["ideogram4"])
    head = (HTML_HEAD
            .replace("__POSTER_RATIO__", meta["ratio"])
            .replace("__GALLERY_NAV__", _gallery_nav(backend_name)))
    html = head + "\n".join(cards) + "\n" + HTML_FOOT.format(
        count=len(rows),
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        credit=meta["credit"],
    )
    html_path.write_text(html, encoding="utf-8")


# ----------------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------------

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text, max_len=60):
    s = SLUG_RE.sub("-", text.lower()).strip("-")
    return (s or "untitled")[:max_len]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("count", type=int, help="Number of mashups to generate")
    parser.add_argument("output", type=Path, nargs="?", default=None,
                        help="Output CSV path (default: mashups_<backend>.csv)")
    parser.add_argument("--llm", choices=["claude", "llama"], default="claude",
                        help="Text LLM: 'claude' (Claude Code CLI, default) or "
                             "'llama' (local llama.cpp server)")
    parser.add_argument("--claude-model", default="sonnet",
                        help="Model for the Claude Code CLI (default: sonnet)")
    parser.add_argument("--llm-server", default="http://127.0.0.1:9503",
                        help="llama.cpp server URL when --llm llama (default: http://127.0.0.1:9503)")
    parser.add_argument("--comfy-server", default="127.0.0.1:8188",
                        help="ComfyUI server host:port (default: 127.0.0.1:8188)")
    parser.add_argument("--backend", choices=sorted(BACKENDS), default="krea",
                        help="Image backend / workflow (default: krea)")
    parser.add_argument("--images-dir", type=Path, default=None,
                        help="Directory to write poster PNGs (default: ./images/<backend>)")
    parser.add_argument("--html", type=Path, default=None,
                        help="Output HTML gallery path (default: mashups_<backend>.html)")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip ComfyUI image generation")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM (no titles/prompts/images, just base mashups)")
    args = parser.parse_args()

    if args.count < 1:
        print("Error: count must be at least 1.", file=sys.stderr)
        sys.exit(1)

    do_images = not args.no_images and not args.no_llm
    backend = BACKENDS[args.backend]
    gallery_meta = GALLERY_META[backend.name]

    # Build the text LLM backend.
    llm = None
    if not args.no_llm:
        if args.llm == "claude":
            if shutil.which("claude") is None:
                print("Error: 'claude' CLI not found on PATH (needed for --llm claude).",
                      file=sys.stderr)
                sys.exit(1)
            llm = ClaudeCodeLLM(model=args.claude_model)
        else:
            llm = LlamaLLM(args.llm_server)

    # Resolve per-backend default paths (CSV / HTML / images dir) so each backend
    # writes to its own files and its own images/<backend>/ subdir.
    if args.output is None:
        args.output = Path(f"mashups_{backend.name}.csv")
    if args.html is None:
        args.html = Path(gallery_meta["html"])
    if args.images_dir is None:
        args.images_dir = Path(gallery_meta["images_dir"])

    # Relative path from the gallery HTML to the poster files. When the HTML and
    # the images dir share a parent (the normal case), this is just the images
    # dir relative to the HTML's directory.
    try:
        images_href = args.images_dir.relative_to(args.html.parent).as_posix()
    except ValueError:
        images_href = args.images_dir.as_posix()

    fieldnames = ["genre_1_major", "genre_1_sub", "genre_2_major",
                  "genre_2_sub", "character", "pitch",
                  "title", "synopsis", "image_prompt", "image_file"]

    print(f"Generating {args.count} mashup{'s' if args.count != 1 else ''}")
    print(f"  CSV:     {args.output}")
    print(f"  HTML:    {args.html}")
    if not args.no_llm:
        print(f"  LLM:     {llm.describe()}")
    if do_images:
        print(f"  Backend: {backend.name} ({backend.poster_w}x{backend.poster_h})")
        print(f"  Comfy:   {args.comfy_server}")
        print(f"  Images:  {args.images_dir}/")
        args.images_dir.mkdir(parents=True, exist_ok=True)
    print()

    # Fail fast if the chosen backend's workflow file is missing.
    if do_images:
        backend.load_workflow()
    comfy = ComfyClient(args.comfy_server) if do_images else None

    completed_rows = []
    failures = 0
    start = time.monotonic()

    def llm_task(idx):
        """Returns (mashup_dict, error_message_or_None).

        Stores two things from the LLM: m["_payload"] is the raw value handed to
        the backend's workflow patcher (a prose string for Z-Image, a dict for
        Ideogram), while m["image_prompt"] is a human-readable rendering of it for
        the CSV and the gallery's "behind the scenes" panel."""
        m = generate_mashup()
        m["title"] = ""
        m["synopsis"] = ""
        m["image_prompt"] = ""
        m["image_file"] = ""
        m["_payload"] = None
        if args.no_llm:
            return m, None
        try:
            title, synopsis, payload = call_llm(llm, m, backend)
            m["title"] = title
            m["synopsis"] = synopsis
            m["_payload"] = payload
            m["image_prompt"] = (
                payload if isinstance(payload, str)
                else json.dumps(payload, indent=2, ensure_ascii=False)
            )
            return m, None
        except Exception as e:
            return m, f"llm: {e}"

    def write_outputs():
        with args.output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in completed_rows:
                writer.writerow({k: r.get(k, "") for k in fieldnames})
        write_gallery(args.html, completed_rows, images_href, backend.name)

    # Pipeline:
    #   While ComfyUI renders row N's poster, the LLM is already drafting row N+1.
    #   We use a single-worker pool for the LLM so we always have at most one
    #   LLM call in flight ahead of the current row.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as llm_pool:
        # Kick off the first LLM job
        next_future = llm_pool.submit(llm_task, 1) if args.count > 0 else None

        for i in range(1, args.count + 1):
            row_start = time.monotonic()

            # Wait for the LLM job for THIS row to finish.
            mashup, llm_err = next_future.result()
            llm_done = time.monotonic()

            # Immediately queue the LLM job for the NEXT row so it runs
            # alongside this row's image render.
            if i < args.count:
                next_future = llm_pool.submit(llm_task, i + 1)

            status_bits = []
            if llm_err:
                status_bits.append(llm_err[:50])

            # Image generation for this row (only if we have a usable payload)
            img_time = 0.0
            if do_images and mashup.get("_payload"):
                img_start = time.monotonic()
                seed = random.randint(1, 2**31 - 1)
                wf = backend.patch(mashup["_payload"], seed)
                slug = f"{i:04d}-{slugify(mashup['title'] or 'untitled')}"
                fname = f"{slug}.png"
                dest = args.images_dir / fname
                try:
                    raw = comfy.generate(wf)
                    dest.write_bytes(raw)  # save PNG as-is, no transcoding
                    mashup["image_file"] = fname
                except Exception as e:
                    status_bits.append(f"img: {e}"[:50])
                img_time = time.monotonic() - img_start

            if not status_bits:
                status = "ok"
            else:
                failures += 1
                status = " | ".join(status_bits)

            completed_rows.append(mashup)
            write_outputs()  # rewrite CSV + HTML after every row

            elapsed = time.monotonic() - start
            row_time = time.monotonic() - row_start
            avg = elapsed / i
            remaining = avg * (args.count - i)

            llm_time = llm_done - row_start
            title_preview = (mashup.get("title") or "(no title)")[:48]
            timing = f"llm {llm_time:5.1f}s"
            if do_images:
                timing += f" img {img_time:5.1f}s"
            print(
                f"[{i:>4}/{args.count}] {timing}  total {row_time:5.1f}s  "
                f"avg {avg:5.1f}s  eta {format_eta(remaining):>7}  → {title_preview}  "
                f"[{status}]",
                flush=True,
            )

    total = time.monotonic() - start
    print()
    print(f"Done in {format_eta(total)}. {args.count - failures}/{args.count} succeeded.")
    print(f"  CSV:  {args.output}")
    print(f"  HTML: {args.html}")
    if failures:
        print(f"  ⚠  {failures} row{'s' if failures != 1 else ''} had errors.")


if __name__ == "__main__":
    main()
