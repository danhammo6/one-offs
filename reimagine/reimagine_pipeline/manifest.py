import dataclasses
import hashlib
import json
from pathlib import Path

import yaml

from .files import atomic_write_text
from .models import PipelineItem, PipelineManifest, StillSpec, VideoSpec

SCHEMA_VERSION = 2


def _safe_path(value, suffixes):
    try:
        path = Path(value)
    except TypeError as error:
        raise ValueError(f"invalid pipeline path: {value!r}") from error
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() not in suffixes:
        raise ValueError(f"unsafe pipeline path: {value!r}")
    return path


def _validate_hash(value, label):
    value = str(value)
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _still_to_data(spec):
    data = {
        "output": spec.output.as_posix(), "width": spec.width,
        "height": spec.height,
    }
    if spec.prompt is not None:
        data["prompt"] = spec.prompt
    if spec.regions is not None:
        data["regions"] = spec.regions
    return data


def _video_to_data(spec):
    return {
        "output": spec.output.as_posix(), "prompt": spec.prompt,
        "prompt_basis": spec.prompt_basis, "basis_sha256": spec.basis_sha256,
        "duration": spec.duration,
    }


def save_pipeline(path, manifest):
    data = {
        "schema_version": SCHEMA_VERSION,
        "still_mode": manifest.still_mode,
        "item_count": manifest.item_count,
        "items": [],
    }
    for item in sorted(manifest.items, key=lambda value: value.index):
        entry = {
            "index": item.index, "id": item.item_id,
            "source_path": item.source_path.as_posix(),
            "source_sha256": item.source_sha256,
        }
        if item.still:
            entry["still"] = _still_to_data(item.still)
        if item.video:
            entry["video"] = _video_to_data(item.video)
        data["items"].append(entry)
    atomic_write_text(path, yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False,
        width=1000))


def _load_still(data, mode):
    if not isinstance(data, dict):
        raise ValueError("still spec is not a mapping")
    output = _safe_path(data.get("output"), {".jpg"})
    width, height = int(data.get("width", 0)), int(data.get("height", 0))
    if width <= 0 or height <= 0 or width % 64 or height % 64:
        raise ValueError(f"invalid still dimensions for {output}")
    prompt, regions = data.get("prompt"), data.get("regions")
    if mode == "manual" and (not isinstance(prompt, str) or len(prompt) < 20):
        raise ValueError(f"invalid manual prompt for {output}")
    if mode == "regions" and not isinstance(regions, dict):
        raise ValueError(f"invalid region prompt for {output}")
    return StillSpec(output, width, height, prompt=prompt, regions=regions)


def _load_video(data):
    if not isinstance(data, dict):
        raise ValueError("video spec is not a mapping")
    output = _safe_path(data.get("output"), {".mp4", ".webm", ".mkv"})
    prompt = data.get("prompt")
    basis = data.get("prompt_basis")
    if not isinstance(prompt, str) or len(prompt) < 20:
        raise ValueError(f"invalid video prompt for {output}")
    if basis not in {"reference", "rendered"}:
        raise ValueError(f"invalid video prompt basis for {output}")
    duration = int(data.get("duration", 10))
    if duration <= 0 or duration > 30:
        raise ValueError(f"invalid video duration for {output}")
    return VideoSpec(
        output, prompt, basis,
        _validate_hash(data.get("basis_sha256"), "basis_sha256"),
        duration)


def load_pipeline(path, require_stage=None):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"could not read pipeline {path}: {error}") from error
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported pipeline manifest: {path}")
    mode = data.get("still_mode")
    if mode not in {"manual", "regions"}:
        raise ValueError(f"invalid still mode: {mode!r}")
    try:
        count = int(data.get("item_count", -1))
    except (TypeError, ValueError) as error:
        raise ValueError("pipeline item_count is not an integer") from error
    raw_items = data.get("items")
    if count < 0 or not isinstance(raw_items, list):
        raise ValueError("invalid pipeline items")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("pipeline item is not a mapping")
        try:
            item = PipelineItem(
                index=int(raw["index"]), item_id=str(raw["id"]),
                source_path=_safe_path(raw["source_path"],
                                       {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}),
                source_sha256=_validate_hash(raw["source_sha256"], "source_sha256"),
                still=_load_still(raw["still"], mode) if raw.get("still") else None,
                video=_load_video(raw["video"]) if raw.get("video") else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid pipeline item: {error}") from error
        items.append(item)
    indexes = {item.index for item in items}
    ids = {item.item_id for item in items}
    source_paths = {item.source_path for item in items}
    still_outputs = {item.still.output for item in items if item.still}
    video_outputs = {item.video.output for item in items if item.video}
    if len(indexes) != len(items) or any(i < 0 or i >= count for i in indexes):
        raise ValueError("duplicate or invalid pipeline indexes")
    if (len(ids) != len(items) or len(source_paths) != len(items)
            or len(still_outputs) != len([item for item in items if item.still])
            or len(video_outputs) != len([item for item in items if item.video])):
        raise ValueError("pipeline contains duplicate IDs or paths")
    if require_stage in {"stills", "all"} and (
            indexes != set(range(count)) or any(not item.still for item in items)):
        raise ValueError("pipeline has incomplete still plans")
    if require_stage in {"videos", "all"} and (
            indexes != set(range(count))
            or any(not item.still or not item.video for item in items)):
        raise ValueError("pipeline has incomplete video plans")
    return PipelineManifest(mode, count, sorted(items, key=lambda item: item.index))


def plan_fingerprint(value):
    def normalize(item):
        if dataclasses.is_dataclass(item):
            return normalize(dataclasses.asdict(item))
        if isinstance(item, Path):
            return item.as_posix()
        if isinstance(item, dict):
            return {key: normalize(val) for key, val in sorted(item.items())}
        if isinstance(item, (list, tuple)):
            return [normalize(val) for val in item]
        return item

    payload = json.dumps(normalize(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_render_state(path):
    if not path.is_file():
        return {"schema_version": 1, "items": {}}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {"schema_version": 1, "items": {}}
    return data if isinstance(data, dict) and isinstance(data.get("items"), dict) \
        else {"schema_version": 1, "items": {}}


def save_render_state(path, state):
    atomic_write_text(path, yaml.safe_dump(
        state, sort_keys=True, allow_unicode=True, default_flow_style=False))
