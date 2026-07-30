#!/usr/bin/env python3
"""reimagine — recreate reference images as dynamic-posture renders.

Generate render descriptions from a tree of reference images, render saved
descriptions through a ComfyUI krea2 workflow, or do both in one run. The split
commands let the multimodal LLM server and ComfyUI run at different times on a
machine that cannot hold both models in VRAM.

    input/sports/sprint.jpg   ->   output/sports/sprint.jpg

Retrieval of the rendered file: the workflow's Image Saver node writes a JPEG on
the ComfyUI host. We read it back from a local or mounted copy of its output
directory (--comfyui-output-dir), falling back to ComfyUI's HTTP /view if the
file isn't found there.

Deps (see requirements.txt): websocket-client, pillow. Set up with uv:
    uv venv --python 3.14 .venv
    uv pip install --python .venv -r requirements.txt
    .venv/bin/python reimagine.py --help
"""
import argparse
import concurrent.futures
import copy
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# ----------------------------------------------------------------------------
# Workflow node IDs — workflows/krea2_comfyui_t2i_aitrepeneur_jpg_api.json
# ----------------------------------------------------------------------------
NODE_PROMPT = "143"    # PrimitiveStringMultiline "MANUAL PROMPT"
NODE_SAVER = "159"     # Image Saver (writes JPEG on the ComfyUI host)
NODE_KSAMPLER = "78:75"
NODE_VARIANCE = "148"  # RBG_Smart_Seed_Variance (holds a seed too)
NODE_LATENT = "78:76"  # EmptyLatentImage
NODE_CLIP_LOADER = "53"
NODE_UNET_LOADER = "162"

# ----------------------------------------------------------------------------
# Region-mode node IDs — workflows/krea2_regions_comfyui_t2i_aitrepeneur_jpg_api.json
# Same graph as the manual workflow EXCEPT the plain-text prompt node (143) is
# replaced by an Ideogram4PromptBuilderKJ node (14) that assembles a structured,
# coordinate-placed prompt from region data. Sampler / latent / saver IDs match.
# ----------------------------------------------------------------------------
NODE_REGION_BUILDER = "14"  # Ideogram4PromptBuilderKJ

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}

ROOT = Path(__file__).parent
DEFAULT_MANUAL_WORKFLOW = ROOT / (
    "workflows/krea2_comfyui_t2i_aitrepeneur_jpg_api.json")
DEFAULT_REGIONS_WORKFLOW = ROOT / (
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
PROMPTS_DIR = ROOT / "prompts"


def _load_prompt(name):
    """Read a system-prompt text file from prompts/, stripped of trailing
    whitespace. Fails loudly (at import) if the file is missing — the run can't
    proceed without it."""
    path = PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SystemExit(f"missing system prompt file {path}: {e}")


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
                 system_prompt=None):
        self.model = model
        self.timeout = timeout
        self.cli = cli
        self.add_dir = add_dir  # abs path granted to the Read tool
        self.system_prompt = system_prompt or _load_prompt("system_manual.txt")

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
                  system_prompt=None):
        # Accept "127.0.0.1:9503", "http://127.0.0.1:9503", or a full /v1 URL.
        if "://" not in base_url:
            base_url = "http://" + base_url
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.system_prompt = system_prompt or _load_prompt("system_manual.txt")

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
RENDER_SPECS_YAML = "render_specs.yaml"
RENDER_SPECS_VERSION = 1


@dataclass(frozen=True)
class RenderSpec:
    """Everything ComfyUI needs to render one image without the reference or LLM."""

    index: int
    output: Path
    width: int
    height: int
    source_sha256: str | None = None
    prompt: str | None = None
    regions: dict | None = None


@dataclass
class RenderManifest:
    """Versioned, portable handoff between description and rendering runs."""

    mode: str
    items: list[RenderSpec]
    item_count: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ImageJob:
    index: int
    source: Path
    output: Path
    width: int
    height: int
    source_sha256: str


@dataclass
class RunSummary:
    generated: int = 0
    rendered: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def exit_code(self):
        return 1 if self.failed else 0


def _atomic_write_text(path, text):
    """Replace path atomically so an interrupted write keeps the prior file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(text)
            f.flush()
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    tmp.replace(path)


def _atomic_write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    tmp.replace(path)


def _validate_output_path(value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".jpg":
        raise ValueError(f"invalid manifest output path: {value!r}")
    return path


def _spec_to_data(spec, mode):
    data = {
        "index": spec.index,
        "output": spec.output.as_posix(),
        "width": spec.width,
        "height": spec.height,
    }
    if spec.source_sha256:
        data["source_sha256"] = spec.source_sha256
    if mode == "manual":
        data["prompt"] = spec.prompt
    else:
        data["regions"] = spec.regions
    return data


def _spec_from_data(data, mode):
    if not isinstance(data, dict):
        raise ValueError("manifest item is not a mapping")
    try:
        index = int(data["index"])
        output = _validate_output_path(data["output"])
        width = int(data["width"])
        height = int(data["height"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"invalid manifest item: {e}") from e
    if index < 0 or width <= 0 or height <= 0 or width % 64 or height % 64:
        raise ValueError(f"invalid index or dimensions for {output}")
    source_sha256 = data.get("source_sha256")
    if source_sha256 is not None:
        source_sha256 = str(source_sha256)
        if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise ValueError(f"invalid source_sha256 for {output}")
    if mode == "manual":
        prompt = str(data.get("prompt") or "").strip()
        if len(prompt) < 20:
            raise ValueError(f"missing or invalid prompt for {output}")
        return RenderSpec(index, output, width, height, source_sha256, prompt=prompt)
    try:
        regions = validate_regions(data.get("regions"))
    except ValueError as e:
        raise ValueError(f"invalid regions for {output}: {e}") from e
    return RenderSpec(index, output, width, height, source_sha256,
                      regions=regions)


def save_manifest(path, manifest):
    """Persist a complete or partial render manifest atomically."""
    if manifest.mode not in ("manual", "regions"):
        raise ValueError(f"invalid manifest mode: {manifest.mode!r}")
    items = sorted(manifest.items, key=lambda item: item.index)
    data = {
        "schema_version": RENDER_SPECS_VERSION,
        "mode": manifest.mode,
        "item_count": (manifest.item_count if manifest.item_count is not None
                       else len(items)),
        "items": [_spec_to_data(item, manifest.mode) for item in items],
    }
    import yaml
    _atomic_write_text(path, yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False,
        width=1000))


def load_manifest(path, require_complete=False):
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"could not read render manifest {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"render manifest {path} is not a mapping")
    if data.get("schema_version") != RENDER_SPECS_VERSION:
        raise ValueError(
            f"unsupported render manifest version: {data.get('schema_version')!r}")
    mode = data.get("mode")
    if mode not in ("manual", "regions"):
        raise ValueError(f"invalid manifest mode: {mode!r}")
    try:
        item_count = int(data["item_count"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("manifest item_count must be an integer") from e
    raw_items = data.get("items")
    if item_count < 0 or not isinstance(raw_items, list):
        raise ValueError("manifest item_count/items are invalid")
    items = [_spec_from_data(item, mode) for item in raw_items]
    indexes = [item.index for item in items]
    outputs = [item.output for item in items]
    if len(set(indexes)) != len(indexes) or len(set(outputs)) != len(outputs):
        raise ValueError("manifest contains duplicate indexes or output paths")
    if any(index < 0 or index >= item_count for index in indexes):
        raise ValueError("manifest index is outside item_count")
    if require_complete and set(indexes) != set(range(item_count)):
        raise ValueError("render manifest is incomplete; run describe first")
    return RenderManifest(mode, sorted(items, key=lambda item: item.index),
                          item_count=item_count)


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
    if not isinstance(data, dict):
        data = {}
    data[rel.with_suffix(".jpg").name] = prompt
    _atomic_write_text(yaml_path, yaml.safe_dump(
        data, sort_keys=True, allow_unicode=True, default_flow_style=False,
        width=1000))


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


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ----------------------------------------------------------------------------
# Commands and orchestration
# ----------------------------------------------------------------------------
def add_common_output_args(parser):
    parser.add_argument("--output-dir", type=Path, default=Path("output"),
                        help="Output set and default manifest directory.")
    parser.add_argument("--spec-file", type=Path, default=None,
                        help="Render manifest (default: OUTPUT_DIR/render_specs.yaml).")
    parser.add_argument("--force", action="store_true",
                        help="Replace descriptions or rendered images for this command.")


def add_describe_args(parser):
    parser.add_argument("--input-dir", type=Path, default=Path("input"),
                        help="Reference-image tree.")
    parser.add_argument("--regions", action="store_true",
                        help="Generate structured region descriptions.")
    parser.add_argument("--claude-model", default="opus")
    parser.add_argument("--llm-server", default=None)
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--llm-workers", type=int, default=3)


def add_render_args(parser):
    parser.add_argument("--workflow", type=Path, default=None,
                        help="ComfyUI API workflow; inferred from manifest mode.")
    parser.add_argument("--comfy-server", default="127.0.0.1:8188",
                        help="ComfyUI host (default: %(default)s).")
    parser.add_argument("--comfyui-output-dir", type=Path, default=None,
                        help="Local or mounted ComfyUI output/ directory.")
    parser.add_argument("--save-subdir", default=SAVE_SUBDIR,
                        help="ComfyUI output staging subdirectory.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Base seed; item i uses seed + its saved index.")
    parser.add_argument("--clip-name", default=None,
                        help="Override the workflow CLIP model filename.")
    parser.add_argument("--unet-name", default=None,
                        help="Override the workflow UNet model filename.")


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    describe = commands.add_parser(
        "describe", help="Generate and persist render descriptions; no ComfyUI.")
    add_common_output_args(describe)
    add_describe_args(describe)
    render = commands.add_parser(
        "render", help="Render a saved manifest; no reference images or LLM.")
    add_common_output_args(render)
    add_render_args(render)
    run = commands.add_parser(
        "run", help="Describe missing items, then render them in one process.")
    add_common_output_args(run)
    add_describe_args(run)
    add_render_args(run)
    return parser


def manifest_path(args, output_dir):
    return (args.spec_file or output_dir / RENDER_SPECS_YAML).resolve()


def build_llm(args, input_dir):
    system_prompt = _load_prompt(
        "system_regions.txt" if args.regions else "system_manual.txt")
    if args.llm_server:
        return OpenAILLM(args.llm_server, model=args.llm_model,
                         system_prompt=system_prompt)
    return ClaudeCodeLLM(model=args.claude_model, add_dir=input_dir,
                         system_prompt=system_prompt)


class ImageRenderer:
    def __init__(self, comfy, workflow, mode, output_dir, comfyui_output_dir,
                 save_subdir, base_seed):
        self.comfy = comfy
        self.workflow = workflow
        self.mode = mode
        self.output_dir = output_dir
        self.comfyui_output_dir = comfyui_output_dir
        self.save_subdir = save_subdir
        self.base_seed = base_seed

    def render(self, spec):
        destination = self.output_dir / spec.output
        filename = spec.output.as_posix().replace("/", "__").rsplit(".", 1)[0]
        seed = self.base_seed + spec.index
        if self.mode == "regions":
            workflow = patch_regions_workflow(
                self.workflow, spec.regions, seed, spec.width, spec.height,
                self.save_subdir, filename)
        else:
            workflow = patch_workflow(
                self.workflow, spec.prompt, seed, spec.width, spec.height,
                self.save_subdir, filename)
        self.comfy.wait_until_up()
        clear_host_files(self.comfyui_output_dir, self.save_subdir, filename)
        reported = self.comfy.render(workflow)
        raw = retrieve(self.comfy, reported, self.comfyui_output_dir, filename,
                       self.save_subdir)
        _atomic_write_bytes(destination, png_bytes_to_jpeg(raw))


def override_workflow_models(workflow, clip_name=None, unet_name=None):
    """Override model filenames in a loaded still workflow when requested."""
    overrides = (
        (clip_name, NODE_CLIP_LOADER, "clip_name"),
        (unet_name, NODE_UNET_LOADER, "unet_name"),
    )
    for value, node_id, input_name in overrides:
        if value is None:
            continue
        try:
            workflow[node_id]["inputs"][input_name] = value
        except (KeyError, TypeError) as e:
            raise ValueError(
                f"workflow has no {input_name} input at node {node_id}") from e
    return workflow


def build_renderer(args, manifest, output_dir):
    workflow_path = args.workflow or (
        DEFAULT_REGIONS_WORKFLOW if manifest.mode == "regions"
        else DEFAULT_MANUAL_WORKFLOW)
    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"could not load workflow {workflow_path}: {e}") from e
    override_workflow_models(workflow, args.clip_name, args.unet_name)
    comfy = ComfyClient(args.comfy_server)
    if not comfy.ping():
        print(f"ComfyUI at {args.comfy_server} not reachable yet.")
    comfy.wait_until_up()
    return ImageRenderer(
        comfy, workflow, manifest.mode, output_dir, args.comfyui_output_dir,
        args.save_subdir, args.seed)


def discover_jobs(input_dir):
    images = list(iter_images(input_dir))
    if not images:
        raise ValueError(f"no images found under {input_dir}")
    jobs = []
    outputs = set()
    host_names = set()
    for index, source in enumerate(images):
        output = source.relative_to(input_dir).with_suffix(".jpg")
        host_name = output.as_posix().replace("/", "__").rsplit(".", 1)[0]
        if output in outputs or host_name in host_names:
            raise ValueError(f"output or host filename collision at {output}")
        outputs.add(output)
        host_names.add(host_name)
        width, height = derive_dims(source)
        jobs.append(ImageJob(index, source, output, width, height,
                             sha256_file(source)))
    return jobs


def _description_for_job(llm, job, mode):
    started = time.time()
    if mode == "regions":
        regions = regions_for_image(llm, str(job.source))
        spec = RenderSpec(job.index, job.output, job.width, job.height,
                          job.source_sha256, regions=regions)
    else:
        prompt = prompt_for_image(llm, str(job.source))
        spec = RenderSpec(job.index, job.output, job.width, job.height,
                          job.source_sha256, prompt=prompt)
    return spec, time.time() - started


def describe_specs(args, output_dir, path):
    input_dir = args.input_dir.resolve()
    if not input_dir.is_dir():
        raise ValueError(f"input dir not found: {input_dir}")
    jobs = discover_jobs(input_dir)
    mode = "regions" if args.regions else "manual"
    existing = None
    if path.is_file() and not args.force:
        existing = load_manifest(path)
        if existing.mode != mode or existing.item_count != len(jobs):
            raise ValueError("existing manifest mode or item count does not match")
        for item in existing.items:
            job = jobs[item.index]
            if (item.output != job.output or item.width != job.width
                    or item.height != job.height
                    or item.source_sha256 != job.source_sha256):
                raise ValueError(
                    f"existing manifest no longer matches input at {job.output}")
    manifest = RenderManifest(
        mode, [] if args.force or existing is None else list(existing.items),
        item_count=len(jobs))
    by_index = {item.index: item for item in manifest.items}
    pending = [job for job in jobs if job.index not in by_index]
    if not pending:
        print(f"describe: {len(jobs)} description(s) already saved")
        return manifest, RunSummary(skipped=len(jobs))

    llm = build_llm(args, input_dir)
    workers = max(1, args.llm_workers) if llm.name == "claude" else 1
    print(f"describe: {len(pending)} of {len(jobs)} description(s)")
    print(f"  input:    {input_dir}")
    print(f"  manifest: {path}")
    print(f"  mode:     {mode}")
    print(f"  llm:      {llm.describe()}")
    summary = RunSummary(skipped=len(jobs) - len(pending))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        submit_at = 0
        while submit_at < min(workers, len(pending)):
            job = pending[submit_at]
            futures[job.index] = pool.submit(
                _description_for_job, llm, job, mode)
            submit_at += 1
        for job in pending:
            future = futures.pop(job.index)
            if submit_at < len(pending):
                ahead = pending[submit_at]
                futures[ahead.index] = pool.submit(
                    _description_for_job, llm, ahead, mode)
                submit_at += 1
            tag = f"[{job.index + 1}/{len(jobs)}] {job.output}"
            try:
                spec, elapsed = future.result()
            except Exception as e:
                print(f"{tag}  ✗ description failed: {e}")
                summary.failed += 1
                continue
            by_index[spec.index] = spec
            manifest.items = sorted(by_index.values(), key=lambda item: item.index)
            save_manifest(path, manifest)
            display = (regions_to_text(spec.regions) if mode == "regions"
                       else spec.prompt)
            save_prompt_yaml(output_dir, spec.output, display)
            print(f"{tag}  ✓ described in {elapsed:.1f}s")
            summary.generated += 1
    return manifest, summary


def _display_text(spec, mode):
    return regions_to_text(spec.regions) if mode == "regions" else spec.prompt


def render_specs(args, manifest, output_dir):
    summary = RunSummary()
    pending = []
    for spec in manifest.items:
        save_prompt_yaml(output_dir, spec.output, _display_text(spec, manifest.mode))
        destination = output_dir / spec.output
        if destination.exists() and not args.force:
            summary.skipped += 1
        else:
            pending.append(spec)
    if not pending:
        print(f"render: {len(manifest.items)} image(s) already exist")
        return summary

    renderer = build_renderer(args, manifest, output_dir)
    print(f"render: {len(pending)} of {len(manifest.items)} image(s)")
    print(f"  output: {output_dir}")
    print(f"  mode:   {manifest.mode}")
    for spec in pending:
        tag = f"[{spec.index + 1}/{manifest.item_count}] {spec.output}"
        started = time.time()
        try:
            renderer.render(spec)
        except Exception as e:
            print(f"{tag}  ✗ render failed after {time.time() - started:.1f}s: {e}")
            summary.failed += 1
            continue
        print(f"{tag}  ✓ rendered in {time.time() - started:.1f}s  "
              f"{spec.width}x{spec.height}")
        summary.rendered += 1
    return summary


def print_summary(command, summary):
    print(f"\ndone ({command}): {summary.generated} described, "
          f"{summary.rendered} rendered, {summary.skipped} skipped"
          + (f", {summary.failed} failed" if summary.failed else ""))


def main(argv=None):
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    path = manifest_path(args, output_dir)
    try:
        if args.command == "describe":
            _, summary = describe_specs(args, output_dir, path)
        elif args.command == "render":
            manifest = load_manifest(path, require_complete=True)
            summary = render_specs(args, manifest, output_dir)
        else:
            manifest, described = describe_specs(args, output_dir, path)
            rendered = render_specs(args, manifest, output_dir)
            summary = RunSummary(
                generated=described.generated,
                rendered=rendered.rendered,
                skipped=rendered.skipped,
                failed=described.failed + rendered.failed)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print_summary(args.command, summary)
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
