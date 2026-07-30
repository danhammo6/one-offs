#!/usr/bin/env python3
"""reimagine — recreate reference images as dynamic-posture renders.

Walk an input tree of reference images. For each image, ask a multimodal LLM
(Claude Code CLI by default) to look at it and write ONE plain-text krea2-style
prompt that recreates it as closely as possible. Patch that prompt into the
ComfyUI krea2 workflow, render on the (Windows) ComfyUI box, and write the
resulting JPEG into an output tree mirroring the input's structure + filenames.

    input/sports/sprint.jpg   ->   output/sports/sprint.jpg

Retrieval of the rendered file: the workflow's Image Saver node writes a JPEG on
the ComfyUI host. We read it back from a local or mounted copy of its output
directory (--comfyui-output-dir), falling back to ComfyUI's HTTP /view if the
file isn't found there.

Deps (see requirements.txt): websocket-client, pillow. Set up with uv:
    uv venv --python 3.14 .venv
    uv pip install --python .venv -r requirements.txt
    .venv/bin/python reimagine.py
"""
import argparse
import concurrent.futures
import copy
import io
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# ----------------------------------------------------------------------------
# Workflow node IDs — workflows/krea2_comfyui_t2i_aitrepeneur_jpg_api.json
# ----------------------------------------------------------------------------
NODE_PROMPT = "143"    # PrimitiveStringMultiline "MANUAL PROMPT"
NODE_SAVER = "159"     # Image Saver (writes JPEG on the ComfyUI host)
NODE_KSAMPLER = "78:75"
NODE_VARIANCE = "148"  # RBG_Smart_Seed_Variance (holds a seed too)
NODE_LATENT = "78:76"  # EmptyLatentImage

# ----------------------------------------------------------------------------
# Region-mode node IDs — workflows/krea2_regions_comfyui_t2i_aitrepeneur_jpg_api.json
# Same graph as the manual workflow EXCEPT the plain-text prompt node (143) is
# replaced by an Ideogram4PromptBuilderKJ node (14) that assembles a structured,
# coordinate-placed prompt from region data. Sampler / latent / saver IDs match.
# ----------------------------------------------------------------------------
NODE_REGION_BUILDER = "14"  # Ideogram4PromptBuilderKJ

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}

DEFAULT_MANUAL_WORKFLOW = Path(
    "workflows/krea2_comfyui_t2i_aitrepeneur_jpg_api.json")
DEFAULT_REGIONS_WORKFLOW = Path(
    "workflows/krea2_regions_comfyui_t2i_aitrepeneur_jpg_api.json")

# The workflow's Image Saver writes under <comfy_output>/<PATH>/<FILENAME>.jpeg.
# We use a fixed subfolder + a unique per-job filename so retrieval is
# deterministic (no %date/%time tokens, no counter collisions). The subfolder is
# overridable (--save-subdir) so two runs sharing one ComfyUI host can stage into
# separate dirs and never clobber each other's files (clear_host_files/retrieve
# both key off it).
SAVE_SUBDIR = "reimagine"

JPEG_QUALITY = 90

# ----------------------------------------------------------------------------
# System prompts live in editable text files under prompts/ so they can be
# tuned without touching code. The retry NUDGEs below stay in-code (they're
# short, tied to the parsing/validation logic, and rarely tweaked).
# ----------------------------------------------------------------------------
PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name):
    """Read a system-prompt text file from prompts/, stripped of trailing
    whitespace. Fails loudly (at import) if the file is missing — the run can't
    proceed without it."""
    path = PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SystemExit(f"missing system prompt file {path}: {e}")


SYSTEM_PROMPT = _load_prompt("system_manual.txt")

RETRY_NUDGE = (
    "\n\nYour previous reply did not contain a usable <prompt>...</prompt> "
    "block. Read the image and output ONLY the prompt wrapped in <prompt> tags."
)


# ----------------------------------------------------------------------------
# Region mode: instead of one plain-text prompt, the LLM emits a structured
# spec — an overall description plus coordinate-placed regions ("elements") —
# that feeds ComfyUI's Ideogram4PromptBuilderKJ node. Adapted from the
# genre-masher poster prompt, but this recreates a REAL reference photo (no
# movie-poster framing, no forced title text) and treats every styling field as
# optional, per the node's own defaults.
# ----------------------------------------------------------------------------
REGIONS_SYSTEM_PROMPT = _load_prompt("system_regions.txt")

REGIONS_RETRY_NUDGE = (
    "\n\nYour previous reply did not contain a usable <regions>...</regions> "
    "JSON block (or it failed validation). Read the image and output ONLY the "
    "JSON spec wrapped in <regions> tags, following the schema exactly."
)


# ----------------------------------------------------------------------------
# LLM: multimodal Claude Code CLI
# ----------------------------------------------------------------------------
class ClaudeCodeLLM:
    """Shells out to `claude -p`, scoped to the read-only Read tool so it can
    view the reference image, but nothing else. Returns the raw result text."""

    name = "claude"

    def __init__(self, model="opus", timeout=300, cli="claude", add_dir=None,
                 system_prompt=SYSTEM_PROMPT):
        self.model = model
        self.timeout = timeout
        self.cli = cli
        self.add_dir = add_dir  # abs path granted to the Read tool
        self.system_prompt = system_prompt

    def describe(self):
        return f"Claude Code CLI ({self.model}, multimodal via Read)"

    def chat(self, user_prompt, image_path=None):
        # Claude Code views the image itself via its Read tool (path is embedded
        # in user_prompt), so image_path is unused here.
        cmd = [
            self.cli, "-p",
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", self.system_prompt,
            "--allowedTools", "Read",   # multimodal image read, nothing else
        ]
        if self.add_dir:
            cmd += ["--add-dir", str(self.add_dir)]
        proc = subprocess.run(
            cmd, input=user_prompt, capture_output=True, text=True,
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            env = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"claude returned non-JSON: {proc.stdout.strip()[:200]!r}")
        if env.get("is_error") or env.get("subtype") != "success":
            raise RuntimeError(
                f"claude error envelope: subtype={env.get('subtype')}")
        return (env.get("result") or "").strip()


# ----------------------------------------------------------------------------
# LLM: OpenAI-compatible multimodal HTTP server (e.g. a local llama.cpp server)
# ----------------------------------------------------------------------------
class OpenAILLM:
    """Talks to an OpenAI-compatible /v1/chat/completions endpoint, sending the
    reference image inline as a base64 data URI (multimodal). Stdlib only."""

    name = "openai"

    def __init__(self, base_url, model=None, api_key=None, timeout=300,
                 system_prompt=SYSTEM_PROMPT):
        # Accept "127.0.0.1:9503", "http://127.0.0.1:9503", or a full /v1 URL.
        if "://" not in base_url:
            base_url = "http://" + base_url
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.system_prompt = system_prompt

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def resolve_model(self):
        """Pick a model id if none was given: the server's first listed model."""
        if self.model:
            return self.model
        req = urllib.request.Request(self.base_url + "/v1/models")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
        models = body.get("data") or body.get("models") or []
        if not models:
            raise RuntimeError(f"no models listed at {self.base_url}/v1/models")
        self.model = models[0].get("id") or models[0].get("model")
        return self.model

    def describe(self):
        return f"OpenAI-compatible server {self.base_url} (model={self.model})"

    def chat(self, user_prompt, image_path=None):
        if self.model is None:
            self.resolve_model()
        content = [{"type": "text", "text": user_prompt}]
        if image_path:
            import base64
            raw = Path(image_path).read_bytes()
            mime = "image/png" if str(image_path).lower().endswith(".png") else "image/jpeg"
            b64 = base64.b64encode(raw).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": content},
            ],
            "temperature": 0.7,
            # Generous budget: this can be a reasoning model that spends tokens
            # on hidden chain-of-thought before emitting the final content.
            "max_tokens": 8192,
            # Each image is an independent request (no chat history carries over),
            # and every call ships a different reference image, so a retained KV
            # cache across calls buys nothing and just grows server memory. This
            # is a llama.cpp hint; OpenAI-proper and other servers ignore it.
            "cache_prompt": False,
        }
        resp = self._post("/v1/chat/completions", payload)
        choices = resp.get("choices") or []
        if not choices:
            raise RuntimeError(f"no choices in response: {str(resp)[:200]}")
        msg = choices[0].get("message", {})
        text = (msg.get("content") or "").strip()
        # Some reasoning servers put the visible answer in reasoning_content when
        # content comes back empty; fall back to it so extract_prompt can scan it.
        if not text:
            text = (msg.get("reasoning_content") or "").strip()
        return text


def extract_prompt(text):
    """Pull the LAST <prompt>...</prompt> block (so any example inside the
    model's reasoning loses to its real final answer). None if missing/short."""
    import re
    matches = re.findall(r"<prompt>(.*?)</prompt>", text, re.DOTALL | re.IGNORECASE)
    if not matches:
        return None
    prompt = re.sub(r"\s+", " ", matches[-1]).strip()
    if len(prompt) < 20:
        return None
    return prompt


def prompt_for_image(llm, image_path, retries=3):
    """Ask the LLM to describe image_path as a krea prompt; retry on bad output."""
    user = (
        f"Read the reference image at this absolute path and write the krea2 "
        f"prompt:\n{image_path}"
    )
    last = None
    for attempt in range(retries):
        msg = user + (RETRY_NUDGE if attempt else "")
        text = llm.chat(msg, image_path=image_path)
        prompt = extract_prompt(text)
        if prompt:
            return prompt
        last = text
    raise RuntimeError(
        f"no <prompt> after {retries} tries; last reply: {(last or '')[:160]!r}")


# ----------------------------------------------------------------------------
# Region mode: parse + validate the structured JSON spec
# ----------------------------------------------------------------------------
def extract_regions(text):
    """Pull the LAST <regions>...</regions> JSON object (so any example inside
    the model's reasoning loses to its real final answer). Returns the parsed
    dict, or None if there's no block / it doesn't parse."""
    matches = re.findall(r"<regions>(.*?)</regions>", text,
                         re.DOTALL | re.IGNORECASE)
    if not matches:
        return None
    blob = matches[-1].strip()
    # Be forgiving of ```json fences the model may wrap the object in.
    blob = re.sub(r"^```(?:json)?|```$", "", blob.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _clamp01(v, default=0.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _clean_palette(pal):
    """Keep only well-formed #rrggbb hex strings; return [] if none/invalid."""
    if not isinstance(pal, list):
        return []
    return [c for c in pal if isinstance(c, str) and HEX_RE.match(c.strip())]


def validate_regions(spec):
    """Normalize + validate a region spec from the LLM. Lenient by design: every
    styling field is optional (per the node's defaults and the user's guidance —
    no forced palette, no forced text element). Raises ValueError on anything
    that can't be salvaged. Returns a clean dict ready for patch_regions()."""
    if not isinstance(spec, dict):
        raise ValueError("spec is not a JSON object")

    hld = str(spec.get("high_level_description") or "").strip()
    if not hld:
        raise ValueError("high_level_description is required")
    background = str(spec.get("background") or "").strip()

    raw_elements = spec.get("elements")
    if not isinstance(raw_elements, list) or not raw_elements:
        raise ValueError("elements must be a non-empty array")

    elements = []
    for e in raw_elements:
        if not isinstance(e, dict):
            continue
        etype = "text" if str(e.get("type", "")).strip().lower() == "text" else "obj"
        desc = str(e.get("desc") or "").strip()
        text = str(e.get("text") or "").strip()
        if etype == "text" and not text:
            # A text region with no words is really just an object region.
            etype = "obj"
        if not desc and not (etype == "text" and text):
            continue  # nothing to place
        x, y = _clamp01(e.get("x")), _clamp01(e.get("y"))
        w = _clamp01(e.get("w", 0.2)) or 0.2
        h = _clamp01(e.get("h", 0.2)) or 0.2
        # Keep the box on-canvas.
        w = min(w, 1.0 - x)
        h = min(h, 1.0 - y)
        elements.append({
            "type": etype,
            "text": text if etype == "text" else "",
            "desc": desc,
            "x": round(x, 4), "y": round(y, 4),
            "w": round(w, 4), "h": round(h, 4),
            "palette": _clean_palette(e.get("palette")),
        })
    if not elements:
        raise ValueError("no usable elements after validation")

    return {
        "high_level_description": hld,
        "background": background,
        "aesthetics": str(spec.get("aesthetics") or "").strip(),
        "lighting": str(spec.get("lighting") or "").strip(),
        "style": str(spec.get("style") or "").strip(),
        "palette": _clean_palette(spec.get("palette")),
        "elements": elements,
    }


def regions_for_image(llm, image_path, retries=3):
    """Ask the LLM for a structured region spec for image_path; retry on bad
    output. Returns the validated dict from validate_regions()."""
    user = (
        f"Read the reference image at this absolute path and write the "
        f"region-based JSON spec:\n{image_path}"
    )
    last = None
    for attempt in range(retries):
        msg = user + (REGIONS_RETRY_NUDGE if attempt else "")
        text = llm.chat(msg, image_path=image_path)
        spec = extract_regions(text)
        if spec is not None:
            try:
                return validate_regions(spec)
            except ValueError as e:
                last = f"{text[:120]} (validation: {e})"
                continue
        last = text
    raise RuntimeError(
        f"no valid <regions> after {retries} tries; last: {(last or '')[:200]!r}")


# ----------------------------------------------------------------------------
# ComfyUI client
# ----------------------------------------------------------------------------
class ComfyClient:
    def __init__(self, server):
        if "://" in server:
            server = urllib.parse.urlparse(server).netloc or server.split("://", 1)[1]
        self.server = server.rstrip("/")
        self.client_id = str(uuid.uuid4())

    def _queue(self, prompt, prompt_id):
        data = json.dumps({
            "prompt": prompt, "client_id": self.client_id, "prompt_id": prompt_id,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.server}/prompt", data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def _view(self, filename, subfolder, ftype):
        qs = urllib.parse.urlencode(
            {"filename": filename, "subfolder": subfolder, "type": ftype})
        with urllib.request.urlopen(
                f"http://{self.server}/view?{qs}", timeout=120) as r:
            return r.read()

    def _history(self, prompt_id):
        with urllib.request.urlopen(
                f"http://{self.server}/history/{prompt_id}", timeout=30) as r:
            return json.loads(r.read())

    def ping(self):
        try:
            with urllib.request.urlopen(
                    f"http://{self.server}/system_stats", timeout=5) as r:
                return r.status == 200
        except Exception:
            return False

    def wait_until_up(self, poll=30.0, log_every=4):
        tries = 0
        while not self.ping():
            if tries % log_every == 0:
                print(f"      …ComfyUI at {self.server} unreachable; waiting "
                      f"{poll:.0f}s", flush=True)
            time.sleep(poll)
            tries += 1
        if tries:
            print(f"      …ComfyUI back up after {tries} poll(s)", flush=True)

    def render(self, workflow, all_outputs=False):
        """Queue the workflow, block until done, return the saver's reported
        output(s) — used for HTTP fallback. By default returns the FIRST output
        dict (filename, subfolder, type) or None; with all_outputs=True returns
        the full list of every reported artifact (e.g. LTX writes a .png first
        frame, a silent .mp4, and a muxed -audio.mp4), so the caller can choose
        which one to fetch."""
        import websocket  # websocket-client
        ws = websocket.WebSocket()
        ws.connect(f"ws://{self.server}/ws?clientId={self.client_id}", timeout=30)
        try:
            prompt_id = str(uuid.uuid4())
            self._queue(workflow, prompt_id)
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
        # Image savers report under "images"; VHS_VideoCombine (and other
        # animated savers) report under "gifs" — collect both.
        items = []
        for _node, output in hist.get("outputs", {}).items():
            items += (output.get("images") or []) + (output.get("gifs") or [])
        if all_outputs:
            return items
        return items[0] if items else None  # {filename, subfolder, type}


# ----------------------------------------------------------------------------
# Workflow patching + rendering
# ----------------------------------------------------------------------------
def patch_workflow(base, prompt, seed, width, height, save_path, filename):
    wf = copy.deepcopy(base)
    wf[NODE_PROMPT]["inputs"]["value"] = prompt
    wf[NODE_KSAMPLER]["inputs"]["seed"] = seed
    if NODE_VARIANCE in wf:
        wf[NODE_VARIANCE]["inputs"]["seed"] = seed
    wf[NODE_LATENT]["inputs"]["width"] = width
    wf[NODE_LATENT]["inputs"]["height"] = height
    s = wf[NODE_SAVER]["inputs"]
    s["path"] = save_path            # subfolder under comfy output/
    s["filename"] = filename         # literal, no % tokens
    s["seed_value"] = seed
    s["width"] = width
    s["height"] = height
    s["time_format"] = ""            # keep filename literal
    return wf


def patch_regions_workflow(base, spec, seed, width, height, save_path, filename):
    """Patch the region workflow: fill the Ideogram4PromptBuilderKJ node (14)
    with the validated spec, and set seed / dims / saver like patch_workflow.
    `spec` is the dict from validate_regions()."""
    wf = copy.deepcopy(base)
    b = wf[NODE_REGION_BUILDER]["inputs"]
    b["width"] = width
    b["height"] = height
    b["high_level_description"] = spec["high_level_description"]
    b["background"] = spec["background"]
    b["aesthetics"] = spec["aesthetics"]
    b["lighting"] = spec["lighting"]
    # style is a COMBO: "none" | "photo" | "art_style". We recreate real photos,
    # so pin "photo" and route any style phrase through the style.photo field.
    b["style"] = "photo"
    b["style.photo"] = spec["style"]
    b["medium"] = "photograph"
    b["style_palette_data"] = json.dumps(spec["palette"]) if spec["palette"] else ""
    b["elements_data"] = json.dumps(spec["elements"])

    wf[NODE_KSAMPLER]["inputs"]["seed"] = seed
    if NODE_VARIANCE in wf:
        wf[NODE_VARIANCE]["inputs"]["seed"] = seed
    wf[NODE_LATENT]["inputs"]["width"] = width
    wf[NODE_LATENT]["inputs"]["height"] = height
    s = wf[NODE_SAVER]["inputs"]
    s["path"] = save_path
    s["filename"] = filename
    s["seed_value"] = seed
    s["width"] = width
    s["height"] = height
    s["time_format"] = ""
    return wf


def png_bytes_to_jpeg(raw):
    """Transcode raw bytes (PNG/JPEG) to JPEG at JPEG_QUALITY. Flattens alpha."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True,
            progressive=True)
    return out.getvalue()


def retrieve(comfy, reported, comfyui_output_dir, filename, save_path):
    """Return rendered image bytes. Prefer the ComfyUI output directory; fall
    back to HTTP /view using what the Image Saver reported to /history."""
    if comfyui_output_dir:
        base = Path(comfyui_output_dir) / save_path
        # Image Saver appends the extension and, if the name already exists on
        # the host, a _NNNNN counter (e.g. foo.jpeg -> foo_00001.jpeg). Grab the
        # MOST RECENTLY WRITTEN match so a fresh render always wins over any
        # stale file left from an earlier run under the same base filename.
        hits = list(base.glob(f"{filename}*.jpeg")) + \
            list(base.glob(f"{filename}*.jpg")) + \
            list(base.glob(f"{filename}*.png"))
        if hits:
            newest = max(hits, key=lambda p: p.stat().st_mtime)
            return newest.read_bytes()
        print(f"      (output dir: no {filename}* under {base}; trying HTTP)",
              flush=True)
    if reported:
        return comfy._view(
            reported["filename"], reported.get("subfolder", ""),
            reported.get("type", "output"))
    raise RuntimeError("could not retrieve rendered image (no output-dir hit, "
                       "nothing reported to /history)")


def clear_host_files(comfyui_output_dir, save_path, filename):
    """Delete existing host-side renders through the ComfyUI output directory,
    so Image Saver writes one clean file with no _NNNNN counter suffix. Returns
    the number removed. No-op without an output directory."""
    if not comfyui_output_dir:
        return 0
    base = Path(comfyui_output_dir) / save_path
    if not base.is_dir():
        return 0
    removed = 0
    for ext in ("jpeg", "jpg", "png"):
        for p in base.glob(f"{filename}*.{ext}"):
            try:
                p.unlink()
                removed += 1
            except OSError as e:
                print(f"      (could not remove stale {p.name}: {e})", flush=True)
    return removed


def regions_to_text(spec):
    """Render a validated region spec as a readable multi-line string, so the
    same prompts.yaml + gallery UI (which expect a plain string) work in region
    mode. Shows the overall fields then each placed element."""
    lines = [spec["high_level_description"]]
    for key in ("background", "aesthetics", "lighting", "style"):
        if spec.get(key):
            lines.append(f"{key}: {spec[key]}")
    if spec.get("palette"):
        lines.append("palette: " + ", ".join(spec["palette"]))
    lines.append("")
    lines.append(f"elements ({len(spec['elements'])}):")
    for e in spec["elements"]:
        box = f"[x{e['x']:.2f} y{e['y']:.2f} w{e['w']:.2f} h{e['h']:.2f}]"
        label = f'"{e["text"]}" — ' if e["type"] == "text" and e["text"] else ""
        lines.append(f"  • {box} {label}{e['desc']}")
    return "\n".join(lines)


PROMPTS_YAML = "prompts.yaml"


def save_prompt_yaml(output_dir, rel, prompt):
    """Record the prompt used for one render into a per-directory prompts.yaml,
    keyed by the output image's filename. One YAML file per output subdirectory;
    updates in place so re-runs and resumes accumulate rather than clobber."""
    import yaml
    dest_dir = (output_dir / rel).with_suffix(".jpg").parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = dest_dir / PROMPTS_YAML
    data = {}
    if yaml_path.exists():
        try:
            data = yaml.safe_load(yaml_path.read_text()) or {}
        except yaml.YAMLError:
            data = {}
    data[rel.with_suffix(".jpg").name] = prompt
    yaml_path.write_text(
        yaml.safe_dump(data, sort_keys=True, allow_unicode=True,
                       default_flow_style=False, width=1000))


def iter_images(input_dir):
    """Yield reference image paths under input_dir, sorted for stable ordering."""
    for p in sorted(input_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            yield p


# Per-image renders match the reference's aspect ratio but are normalized to a
# fixed total pixel budget (a 1920x1080 frame's worth of pixels), so every render
# is about the same size and cost regardless of shape. References are scaled both
# down (if larger) AND up (if smaller) to hit this target, standardizing output
# resolution across a set of mixed-size references.
TARGET_PIXELS = 1920 * 1080

# Cap on the longest edge, so an extreme aspect ratio can't blow past the turbo
# model's ~2k comfort zone even while hitting the area target.
MAX_EDGE = 2048


def _round64(n):
    """Round to the nearest positive multiple of 64 (ComfyUI latent requirement)."""
    return max(64, int(round(n / 64.0)) * 64)


def derive_dims(image_path):
    """Render dimensions matching the reference image's aspect ratio, scaled so the
    total area is ~TARGET_PIXELS (scaling up small references and down large ones)
    but with neither edge exceeding MAX_EDGE, then snapped to multiples of 64."""
    from PIL import Image
    with Image.open(image_path) as im:
        w, h = im.size
    scale = (TARGET_PIXELS / float(w * h)) ** 0.5
    # Rein in extreme aspect ratios: keep the longest edge within MAX_EDGE.
    scale = min(scale, MAX_EDGE / float(max(w, h)))
    return _round64(w * scale), _round64(h * scale)


# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", type=Path, default=Path("input"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--regions", action="store_true",
                        help="Use region-based structured prompting (the "
                             "Ideogram4PromptBuilderKJ workflow) instead of the "
                             "default single plain-text prompt.")
    parser.add_argument("--workflow", type=Path, default=None,
                        help="ComfyUI API workflow JSON. Defaults to the manual "
                             "workflow, or the regions workflow under --regions.")
    parser.add_argument("--comfy-server", default="127.0.0.1:8188")
    parser.add_argument("--comfyui-output-dir", type=Path, default=None,
                        help="Local or mounted ComfyUI output/ dir. If set, "
                              "rendered files are read from here; otherwise HTTP "
                              "/view is used.")
    parser.add_argument("--claude-model", default="opus")
    parser.add_argument("--llm-server", default=None,
                        help="OpenAI-compatible server (e.g. 127.0.0.1:9503). If "
                             "set, use it for multimodal prompting instead of the "
                             "Claude Code CLI.")
    parser.add_argument("--llm-model", default=None,
                        help="Model id for --llm-server (default: the server's "
                             "first listed model).")
    parser.add_argument("--llm-workers", type=int, default=3,
                        help="How many LLM prompt jobs to keep in flight, "
                             "drafting ahead of the (serial) ComfyUI renderer so "
                             "it's never starved (default: %(default)s). Only "
                             "applies to the Claude Code CLI — each call is its "
                             "own subprocess. A local --llm-server serves one "
                             "request at a time, so it's pinned to 1 worker.")
    parser.add_argument("--save-subdir", default=SAVE_SUBDIR,
                        help="Subfolder under the ComfyUI host output/ where this "
                             "run stages its renders (default: %(default)s). Give "
                             "concurrent runs on the same ComfyUI host DISTINCT "
                             "subdirs so they don't clobber each other's files.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Base seed; each image uses seed + its index.")
    parser.add_argument("--no-images", action="store_true",
                        help="Only generate prompts (skip ComfyUI); prints them.")
    parser.add_argument("--force", action="store_true",
                        help="Force overwrite: re-render every image even if its "
                             "output already exists. Without this, existing "
                             "outputs are skipped so an interrupted run resumes.")
    args = parser.parse_args()

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    if not input_dir.is_dir():
        sys.exit(f"input dir not found: {input_dir}")

    images = list(iter_images(input_dir))
    if not images:
        sys.exit(f"no images found under {input_dir}")

    system_prompt = REGIONS_SYSTEM_PROMPT if args.regions else SYSTEM_PROMPT
    if args.llm_server:
        llm = OpenAILLM(args.llm_server, model=args.llm_model,
                        system_prompt=system_prompt)
    else:
        llm = ClaudeCodeLLM(model=args.claude_model, add_dir=input_dir,
                            system_prompt=system_prompt)

    workflow_path = args.workflow or (
        DEFAULT_REGIONS_WORKFLOW if args.regions else DEFAULT_MANUAL_WORKFLOW)

    workflow_base = None
    comfy = None
    if not args.no_images:
        workflow_base = json.loads(workflow_path.read_text())
        comfy = ComfyClient(args.comfy_server)
        if not comfy.ping():
            print(f"ComfyUI at {args.comfy_server} not reachable yet.")
        comfy.wait_until_up()

    print(f"reimagine: {len(images)} reference image(s)")
    print(f"  input:   {input_dir}")
    print(f"  output:  {output_dir}")
    print(f"  mode:    {'regions (structured)' if args.regions else 'manual (plain text)'}")
    print(f"  llm:     {llm.describe()}")
    if not args.no_images:
        print(f"  comfy:   {args.comfy_server}  "
              f"(per-image aspect, ~{TARGET_PIXELS:,} px)")
        output_source = args.comfyui_output_dir or "(none — HTTP /view fallback)"
        print(f"  comfyui output: {output_source}")
    print()

    # Pipeline (mirrors ../genre-masher-prompts): the LLM prompt-write is the
    # long pole per image, while ComfyUI renders serially. We keep up to
    # --llm-workers prompt jobs in flight in a thread pool, drafting ahead of the
    # renderer so it's never starved, but still CONSUME results strictly in order
    # (await job i, render it on the main thread, then submit job i+workers).
    # Each Claude call is its own subprocess and parallelizes cleanly; a local
    # --llm-server serves one request at a time, so it's pinned to 1 worker.
    n = len(images)

    def llm_task(idx):
        """Worker-thread job: produce the prompt/spec for image idx. Does NOT
        touch ComfyUI — rendering stays serial on the main thread. Returns a
        dict the main thread acts on (skip / prompt_error / ready)."""
        img = images[idx]
        rel = img.relative_to(input_dir)
        dest = (output_dir / rel).with_suffix(".jpg")
        info = {"idx": idx, "img": img, "rel": rel, "dest": dest,
                "tag": f"[{idx + 1}/{n}] {rel}"}
        if dest.exists() and not args.force and not args.no_images:
            info["action"] = "skip"
            return info
        t0 = time.time()
        try:
            if args.regions:
                spec = regions_for_image(llm, str(img))
                prompt = regions_to_text(spec)  # readable form for YAML/UI
            else:
                spec = None
                prompt = prompt_for_image(llm, str(img))
        except Exception as e:
            info["action"] = "prompt_error"
            info["error"] = e
            return info
        info.update(action="ready", spec=spec, prompt=prompt,
                    llm_time=time.time() - t0)
        return info

    n_workers = max(1, args.llm_workers) if llm.name == "claude" else 1
    if n_workers > 1:
        print(f"  workers: {n_workers} LLM jobs in flight (drafting ahead)\n")

    ok = fail = skipped = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        # Prime the window: submit the first n_workers jobs up front.
        futures = {idx: pool.submit(llm_task, idx)
                   for idx in range(min(n_workers, n))}

        for i in range(n):
            info = futures.pop(i).result()
            # Keep the window full: queue the job n_workers ahead so the next
            # prompt writes run alongside this image's render.
            ahead = i + n_workers
            if ahead < n:
                futures[ahead] = pool.submit(llm_task, ahead)

            tag = info["tag"]
            if info["action"] == "skip":
                print(f"{tag}  ✓ exists, skip")
                skipped += 1
                continue
            if info["action"] == "prompt_error":
                print(f"{tag}  ✗ prompt failed: {info['error']}")
                fail += 1
                continue

            prompt = info["prompt"]
            if args.no_images:
                indented = prompt.replace("\n", "\n      ")
                print(f"{tag}  ({info['llm_time']:.1f}s)\n      {indented}\n")
                ok += 1
                continue

            rel, img, dest, spec = (info["rel"], info["img"],
                                    info["dest"], info["spec"])
            t_render = time.time()
            seed = args.seed + i
            # Match the reference's own aspect ratio, scaled to the pixel budget.
            width, height = derive_dims(img)
            # Host filename mirrors the image's path (no seed) so it's stable
            # across runs: clear_host_files can then wipe ANY prior version
            # before rendering, leaving exactly one file per reference on the
            # host. The seed still drives the render, it just doesn't leak into
            # the filename.
            filename = rel.as_posix().replace("/", "__").rsplit(".", 1)[0]
            save_path = args.save_subdir
            if args.regions:
                wf = patch_regions_workflow(workflow_base, spec, seed, width,
                                            height, save_path, filename)
            else:
                wf = patch_workflow(workflow_base, prompt, seed, width,
                                    height, save_path, filename)
            try:
                comfy.wait_until_up()
                # Remove any prior host-side render for this name first, so the
                # Image Saver writes a single file (no _NNNNN counter version).
                clear_host_files(args.comfyui_output_dir, save_path, filename)
                reported = comfy.render(wf)
                raw = retrieve(comfy, reported, args.comfyui_output_dir,
                               filename, save_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(png_bytes_to_jpeg(raw))
            except Exception as e:
                print(f"{tag}  ✗ render failed after "
                      f"{time.time() - t_render:.1f}s: {e}")
                fail += 1
                continue
            save_prompt_yaml(output_dir, rel, prompt)
            print(f"{tag}  ✓ llm {info['llm_time']:.1f}s + render "
                  f"{time.time() - t_render:.1f}s  {width}x{height} "
                  f"-> {dest.relative_to(output_dir.parent)}")
            ok += 1

    noun = "prompt" if args.no_images else "image"
    print(f"\ndone: {ok} {noun}(s) generated, {skipped} skipped (already existed)"
          + (f", {fail} failed" if fail else ""))


if __name__ == "__main__":
    main()
