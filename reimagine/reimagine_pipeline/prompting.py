import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"


def load_system_prompt(name):
    try:
        return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"could not read system prompt {name}: {error}") from error


def extract_tagged(text, tag, minimum=20):
    matches = re.findall(
        rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", text,
        re.DOTALL | re.IGNORECASE)
    if not matches:
        return None
    value = re.sub(r"\s+", " ", matches[-1]).strip()
    return value if len(value) >= minimum else None


def generate_tagged(llm, system_prompt, user_prompt, image_path, tag, retries=3):
    last = ""
    for attempt in range(retries):
        request = user_prompt
        if attempt:
            request += (
                f"\n\nThe previous response was invalid. Output only one valid "
                f"<{tag}>...</{tag}> block.")
        last = llm.chat(system_prompt, request, image_path)
        value = extract_tagged(last, tag)
        if value:
            return value
    raise RuntimeError(
        f"no valid <{tag}> after {retries} tries; last reply: {last[:160]!r}")


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


def generate_still_prompt(llm, source, mode):
    if mode == "manual":
        return generate_tagged(
            llm, load_system_prompt("system_manual.txt"),
            f"Read this reference image and write the krea2 prompt:\n{source}",
            source, "prompt")
    last = ""
    system = load_system_prompt("system_regions.txt")
    for attempt in range(3):
        request = f"Read this reference image and write the region JSON spec:\n{source}"
        if attempt:
            request += "\n\nOutput only a valid <regions> JSON block."
        last = llm.chat(system, request, source)
        matches = re.findall(r"<regions>(.*?)</regions>", last,
                             re.DOTALL | re.IGNORECASE)
        if not matches:
            continue
        try:
            return validate_regions(json.loads(matches[-1].strip()))
        except (json.JSONDecodeError, ValueError):
            continue
    raise RuntimeError(f"no valid <regions> response: {last[:160]!r}")


def video_prompt_word_range(duration):
    return max(30, duration * 8), min(500, max(80, duration * 16))


def generate_video_prompt(llm, image_path, basis, still_spec, duration=10):
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
        llm, load_system_prompt(system_name),
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
