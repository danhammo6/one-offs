import json
import logging
import re
import time
from pathlib import Path

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


def load_json_schema(name, prompt_dir=PROMPTS_DIR):
    path = Path(prompt_dir) / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"could not read JSON schema {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON schema {path}: {error}") from error


def extract_tagged(text, tag, minimum=20):
    matches = re.findall(
        rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", text,
        re.DOTALL | re.IGNORECASE)
    if not matches:
        return None
    value = re.sub(r"\s+", " ", matches[-1]).strip()
    return value if len(value) >= minimum else None


def _generate_with_retries(
        llm, system_prompt, user_prompt, image_path, kind, parse, retries=3,
        json_schema=None):
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
                f"Output only one valid {kind}.")
        started = time.perf_counter()
        last = llm.chat(
            system_prompt, user_prompt, image_path, correction=correction,
            json_schema=json_schema)
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
    background = str(spec.get("background") or "").strip()
    if not background:
        raise ValueError("background is required")
    raw_elements = spec.get("elements")
    if not isinstance(raw_elements, list) or not 2 <= len(raw_elements) <= 6:
        raise ValueError("elements must contain 2 to 6 entries")
    elements = []
    for raw in raw_elements:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "").lower()
        if kind not in {"obj", "text"}:
            raise ValueError("element type must be obj or text")
        text = str(raw.get("text") or "").strip()
        description = str(raw.get("desc") or "").strip()
        if kind == "text" and not text:
            raise ValueError("text elements require literal text")
        if not description:
            raise ValueError("element desc is required")
        try:
            x, y = float(raw["x"]), float(raw["y"])
            width, height = float(raw["w"]), float(raw["h"])
        except (TypeError, ValueError):
            raise ValueError("element coordinates must be numbers") from None
        except KeyError as error:
            raise ValueError(f"element coordinate {error.args[0]} is required") from None
        if not (0 <= x <= 1 and 0 <= y <= 1
                and width > 0 and height > 0
                and x + width <= 1 and y + height <= 1):
            raise ValueError("element box must be positive and fully on-canvas")
        palette = [str(color).strip() for color in raw.get("palette", [])
                   if re.fullmatch(r"#[0-9a-fA-F]{6}", str(color).strip())]
        elements.append({
            "type": kind, "text": text if kind == "text" else "",
            "desc": description, "x": round(x, 4), "y": round(y, 4),
            "w": round(width, 4), "h": round(height, 4), "palette": palette,
        })
    if len(elements) != len(raw_elements):
        raise ValueError("all region elements must be usable")
    palette = [str(color).strip() for color in spec.get("palette", [])
               if re.fullmatch(r"#[0-9a-fA-F]{6}", str(color).strip())]
    return {
        "high_level_description": high_level,
        "background": background,
        "aesthetics": str(spec.get("aesthetics") or "").strip(),
        "lighting": str(spec.get("lighting") or "").strip(),
        "style": str(spec.get("style") or "").strip(),
        "palette": palette,
        "elements": elements,
    }


def _parse_regions(text):
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid region JSON: {error}") from error
    return validate_regions(spec)


def generate_still_prompt(llm, source, mode, prompt_dir=PROMPTS_DIR):
    if mode == "manual":
        return generate_tagged(
            llm, load_system_prompt("system_manual.txt", prompt_dir),
            f"Read this reference image and write the krea2 prompt:\n{source}",
            source, "prompt")
    return _generate_with_retries(
        llm, load_system_prompt("system_regions.txt", prompt_dir),
        f"Inspect this reference image and return its region JSON object:\n{source}",
        source, "region JSON object", _parse_regions,
        json_schema=load_json_schema("regions.schema.json", prompt_dir))


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
