import json
import logging
import re
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
logger = logging.getLogger(__name__)
MAX_CORRECTION_RESPONSE_CHARS = 2000


def load_system_prompt(name, prompt_dir=PROMPTS_DIR):
    path = Path(prompt_dir) / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"could not read system prompt {path}: {error}") from error


def extract_tagged(text, tag, minimum=20):
    matches = re.findall(
        rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", text,
        re.DOTALL | re.IGNORECASE)
    if not matches:
        return None
    value = re.sub(r"\s+", " ", matches[-1]).strip()
    return value if len(value) >= minimum else None


def _generate_with_retries(
        llm, system_prompt, user_prompt, image_path, kind, parse, retries=3):
    last = ""
    last_error = "invalid response"
    for attempt in range(retries):
        correction = None
        if attempt:
            if len(last) <= MAX_CORRECTION_RESPONSE_CHARS:
                previous = last
            else:
                previous = (
                    f"[Previous response omitted because it was {len(last)} "
                    "characters and was likely truncated.]")
            correction = (
                "The previous response was invalid. Start over; do not "
                "continue, quote, or discuss it. Correct the response using the "
                f"error below.\nError: {last_error}\n"
                "--- BEGIN PREVIOUS RESPONSE ---\n"
                f"{previous}\n"
                "--- END PREVIOUS RESPONSE ---\n"
                f"Output only one valid {kind} block.")
        started = time.perf_counter()
        last = llm.chat(
            system_prompt, user_prompt, image_path, correction=correction)
        try:
            return parse(last)
        except (json.JSONDecodeError, ValueError) as error:
            last_error = str(error)
            logger.warning(
                "%s prompt attempt %d/%d rejected after %.2fs: %s",
                kind, attempt + 1, retries, time.perf_counter() - started,
                last_error)
            logger.debug(
                "%s prompt rejected response:\n%s", kind, last)
    raise RuntimeError(
        f"no valid {kind} after {retries} tries ({last_error}); "
        f"last reply: {last[:160]!r}")


def generate_tagged(llm, system_prompt, user_prompt, image_path, tag, retries=3):
    def parse(text):
        value = extract_tagged(text, tag)
        if value is None:
            raise ValueError(f"missing or too-short <{tag}>...</{tag}> block")
        return value

    return _generate_with_retries(
        llm, system_prompt, user_prompt, image_path, f"<{tag}>...</{tag}>",
        parse, retries)


def validate_regions(spec):
    if not isinstance(spec, dict):
        raise ValueError("region spec is not an object")
    high_level = str(spec.get("high_level_description") or "").strip()
    if not high_level:
        raise ValueError("high_level_description is required")
    raw_elements = spec.get("elements")
    if not isinstance(raw_elements, list) or not raw_elements:
        raise ValueError("elements must be a non-empty array")
    elements = []
    for raw in raw_elements:
        if not isinstance(raw, dict):
            continue
        kind = "text" if str(raw.get("type", "")).lower() == "text" else "obj"
        text = str(raw.get("text") or "").strip()
        description = str(raw.get("desc") or "").strip()
        if kind == "text" and not text:
            kind = "obj"
        if not description and not text:
            continue
        try:
            x, y = float(raw.get("x", 0)), float(raw.get("y", 0))
            width, height = float(raw.get("w", 0.2)), float(raw.get("h", 0.2))
        except (TypeError, ValueError):
            continue
        x, y = max(0.0, min(1.0, x)), max(0.0, min(1.0, y))
        width = min(max(0.0, width), 1.0 - x)
        height = min(max(0.0, height), 1.0 - y)
        if width <= 0 or height <= 0:
            continue
        palette = [str(color).strip() for color in raw.get("palette", [])
                   if re.fullmatch(r"#[0-9a-fA-F]{6}", str(color).strip())]
        elements.append({
            "type": kind, "text": text if kind == "text" else "",
            "desc": description, "x": round(x, 4), "y": round(y, 4),
            "w": round(width, 4), "h": round(height, 4), "palette": palette,
        })
    if not elements:
        raise ValueError("no usable region elements")
    palette = [str(color).strip() for color in spec.get("palette", [])
               if re.fullmatch(r"#[0-9a-fA-F]{6}", str(color).strip())]
    return {
        "high_level_description": high_level,
        "background": str(spec.get("background") or "").strip(),
        "aesthetics": str(spec.get("aesthetics") or "").strip(),
        "lighting": str(spec.get("lighting") or "").strip(),
        "style": str(spec.get("style") or "").strip(),
        "palette": palette,
        "elements": elements,
    }


def _parse_regions(text):
    matches = re.findall(
        r"<regions>(.*?)</regions>", text, re.DOTALL | re.IGNORECASE)
    if not matches:
        raise ValueError("missing <regions>...</regions> block")
    try:
        spec = yaml.safe_load(matches[-1].strip())
    except yaml.YAMLError as error:
        raise ValueError(f"invalid region YAML: {error}") from error
    return validate_regions(spec)


def generate_still_prompt(llm, source, mode, prompt_dir=PROMPTS_DIR):
    if mode == "manual":
        return generate_tagged(
            llm, load_system_prompt("system_manual.txt", prompt_dir),
            f"Read this reference image and write the krea2 prompt:\n{source}",
            source, "prompt")
    return _generate_with_retries(
        llm, load_system_prompt("system_regions.txt", prompt_dir),
        f"Read this reference image and write the region YAML spec:\n{source}",
        source, "<regions> YAML </regions>", _parse_regions)


def video_prompt_word_range(duration):
    return max(30, duration * 8), min(500, max(80, duration * 16))


def generate_video_prompt(
        llm, image_path, basis, still_spec, duration=10,
        prompt_dir=PROMPTS_DIR):
    if basis == "rendered":
        system_name = "system_video.txt"
        context = "The supplied image is the exact LTX first frame."
    else:
        system_name = "system_video_reference.txt"
        still_context = (still_spec.prompt if still_spec.prompt is not None
                         else json.dumps(still_spec.regions, ensure_ascii=False))
        context = (
            "The supplied image is the original reference. The generated still "
            "will follow this validated still plan:\n" + still_context)
    minimum_words, maximum_words = video_prompt_word_range(duration)
    return generate_tagged(
        llm, load_system_prompt(system_name, prompt_dir),
        f"{context}\nWrite a controlled {duration}-second LTX motion prompt. "
        f"Aim for {minimum_words}-{maximum_words} words so the described beats "
        "fill the full runtime without rushing.",
        image_path, "video")


def regions_to_text(spec):
    lines = [spec["high_level_description"]]
    for key in ("background", "aesthetics", "lighting", "style"):
        if spec.get(key):
            lines.append(f"{key}: {spec[key]}")
    if spec.get("palette"):
        lines.append("palette: " + ", ".join(spec["palette"]))
    lines += ["", f"elements ({len(spec['elements'])}):"]
    for element in spec["elements"]:
        box = (f"[x{element['x']:.2f} y{element['y']:.2f} "
               f"w{element['w']:.2f} h{element['h']:.2f}]")
        label = f'"{element["text"]}" - ' if element["text"] else ""
        lines.append(f"  * {box} {label}{element['desc']}")
    return "\n".join(lines)
