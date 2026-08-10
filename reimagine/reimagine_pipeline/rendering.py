import io
import logging
import time
from pathlib import Path

from .comfy import ComfyClient
from .files import atomic_write_bytes, host_name, sha256_file
from .manifest import (
    plan_fingerprint, save_render_state, save_render_state_folder,
)
from .workflows import (
    MANUAL_WORKFLOW, REGIONS_WORKFLOW, STILL_SAVER, VIDEO_SAVER, VIDEO_WORKFLOW,
    load_workflow, patch_ltx_workflow, patch_still_workflow, pick_artifact,
)

logger = logging.getLogger(__name__)


def _jpeg_bytes(raw):
    from PIL import Image
    image = Image.open(io.BytesIO(raw))
    if image.mode in {"RGBA", "LA", "P"}:
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        image = background
    else:
        image = image.convert("RGB")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    return output.getvalue()


def _client(args):
    client = ComfyClient(args.comfy_server)
    client.wait_until_up()
    return client


def _render_fingerprint(spec, workflow, overrides=None):
    return plan_fingerprint({
        "spec": spec,
        "workflow": workflow,
        "overrides": overrides or {},
    })


def _save_state(args, item_id, state):
    if args.state_file:
        save_render_state(args.state_file, state)
    else:
        save_render_state_folder(args.state_root, item_id, state)


def _snapshot_still_outputs(output_dir, save_subdir, name):
    if not output_dir:
        return {}
    directory = output_dir / save_subdir
    return {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in directory.glob(f"{name}*.jpeg")
    } if directory.is_dir() else {}


def _read_still_output(client, artifacts, output_dir, save_subdir, name, before):
    try:
        return client.read_artifact(pick_artifact(artifacts, STILL_SAVER), output_dir)
    except RuntimeError:
        if not output_dir:
            raise
        directory = output_dir / save_subdir
        candidates = [
            path for path in directory.glob(f"{name}*.jpeg")
            if before.get(path) != (path.stat().st_mtime_ns, path.stat().st_size)
        ]
        if not candidates:
            raise
        return max(candidates, key=lambda path: path.stat().st_mtime_ns).read_bytes()


def render_stills(args, manifest, output_dir, state):
    workflow_path = args.still_workflow or (
        REGIONS_WORKFLOW if manifest.still_mode == "regions" else MANUAL_WORKFLOW)
    workflow = load_workflow(workflow_path)
    pending = []
    for item in manifest.items:
        if not item.still:
            continue
        destination = output_dir / item.still.output
        record = state["items"].get(item.item_id, {}).get("still", {})
        seed = args.seed + item.index
        fingerprint = _render_fingerprint(item.still, workflow, {
            "clip_name": args.clip_name, "unet_name": args.unet_name,
            "save_subdir": args.still_save_subdir, "seed": seed,
        })
        if (not args.force and destination.is_file()
                and record.get("plan_fingerprint") == fingerprint
                and record.get("output_sha256") == sha256_file(destination)):
            continue
        pending.append((item, destination, fingerprint, seed))
    if not pending:
        return 0, len([item for item in manifest.items if item.still]), 0
    client = _client(args)
    rendered = failed = 0
    for item, destination, fingerprint, seed in pending:
        started = time.perf_counter()
        try:
            name = host_name(item.still.output)
            patched = patch_still_workflow(
                workflow, item.still, manifest.still_mode, seed,
                args.still_save_subdir, name, args.clip_name, args.unet_name)
            before = _snapshot_still_outputs(
                args.comfyui_output_dir, args.still_save_subdir, name)
            artifacts = client.run_workflow(patched)
            raw = _read_still_output(
                client, artifacts, args.comfyui_output_dir,
                args.still_save_subdir, name, before)
            atomic_write_bytes(destination, _jpeg_bytes(raw))
            item_state = state["items"].setdefault(item.item_id, {})
            item_state["still"] = {
                "plan_fingerprint": fingerprint,
                "output_sha256": sha256_file(destination),
            }
            item_state.pop("video", None)
            _save_state(args, item.item_id, state)
            rendered += 1
            logger.info("still %s: rendered in %.2fs", item.item_id,
                        time.perf_counter() - started)
        except Exception as error:
            failed += 1
            logger.error("still %s: failed after %.2fs: %s", item.item_id,
                         time.perf_counter() - started, error)
    return rendered, len(manifest.items) - len(pending), failed


def render_videos(args, manifest, output_dir, state):
    workflow = load_workflow(args.video_workflow or VIDEO_WORKFLOW)
    pending = []
    blocked = 0
    for item in manifest.items:
        if not item.video or not item.still:
            continue
        still_path = output_dir / item.still.output
        if not still_path.is_file():
            blocked += 1
            logger.warning("video %s: blocked; still is missing", item.item_id)
            continue
        still_hash = sha256_file(still_path)
        if (item.video.prompt_basis == "rendered"
                and item.video.basis_sha256 != still_hash):
            blocked += 1
            logger.warning(
                "video %s: blocked; rendered-basis prompt is stale", item.item_id)
            continue
        destination = output_dir / item.video.output
        record = state["items"].get(item.item_id, {}).get("video", {})
        fingerprint = _render_fingerprint(item.video, workflow, {
            "save_subdir": args.video_save_subdir,
            "seed": args.seed + item.index,
            "clip_name": args.video_clip_name,
            "unet_name": args.video_unet_name,
        })
        if (not args.force and destination.is_file()
                and record.get("plan_fingerprint") == fingerprint
                and record.get("input_still_sha256") == still_hash
                and record.get("output_sha256") == sha256_file(destination)):
            continue
        pending.append((
            item, still_path, still_hash, destination, fingerprint,
            args.seed + item.index))
    if not pending:
        return 0, len([item for item in manifest.items if item.video]) - blocked, blocked
    client = _client(args)
    rendered = failed = 0
    for item, still_path, still_hash, destination, fingerprint, seed in pending:
        started = time.perf_counter()
        try:
            remote_name = f"reimagine/{still_hash[:12]}/{host_name(item.still.output)}.jpg"
            load_name = client.upload_image(still_path, remote_name)
            prefix = f"{args.video_save_subdir}/{host_name(item.video.output)}"
            patched = patch_ltx_workflow(
                workflow, item.video.prompt, load_name, seed,
                item.video.duration, prefix, args.video_clip_name,
                args.video_unet_name)
            artifacts = client.run_workflow(patched)
            artifact = pick_artifact(artifacts, VIDEO_SAVER, video=True)
            if Path(artifact.filename).suffix.lower() != destination.suffix.lower():
                raise RuntimeError(
                    f"video workflow produced {artifact.filename}, expected "
                    f"{destination.suffix} output")
            raw = client.read_artifact(artifact, args.comfyui_output_dir)
            atomic_write_bytes(destination, raw)
            item_state = state["items"].setdefault(item.item_id, {})
            item_state["video"] = {
                "plan_fingerprint": fingerprint,
                "input_still_sha256": still_hash,
                "output_sha256": sha256_file(destination),
            }
            _save_state(args, item.item_id, state)
            rendered += 1
            logger.info("video %s: rendered in %.2fs", item.item_id,
                        time.perf_counter() - started)
        except Exception as error:
            failed += 1
            logger.error("video %s: failed after %.2fs: %s", item.item_id,
                         time.perf_counter() - started, error)
    return rendered, len(manifest.items) - len(pending) - blocked, failed + blocked


def render_all(args, manifest, output_dir, state):
    """Render every still before starting any video render."""
    still_counts = render_stills(args, manifest, output_dir, state)
    video_counts = render_videos(args, manifest, output_dir, state)
    return tuple(still_counts[index] + video_counts[index] for index in range(3))
