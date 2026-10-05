#!/usr/bin/env python
"""Rewrite render_state.yaml plan fingerprints from the legacy scheme.

Legacy fingerprints hashed `seed + plan index` and the ComfyUI save subdir.
Current ones hash `seed + a stable offset from the item id` and omit the save
subdir. The legacy index is not stored, so each record is matched by trying
every candidate index. Pass the same render flags used for the original runs
(this accepts every render_media.py flag). Dry run unless --write is given;
images and output hashes are never touched.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import render_media  # noqa: E402
from reimagine_pipeline import (  # noqa: E402
    RENDER_STATE_FILENAME, pipeline_filename,
)
from reimagine_pipeline.manifest import (  # noqa: E402
    load_pipeline_tree, load_render_state, load_render_state_tree,
    save_render_state, save_render_state_tree, validate_pipeline_input_dir,
)
from reimagine_pipeline.rendering import (  # noqa: E402
    _render_fingerprint, item_seed, still_overrides, video_overrides,
)
from reimagine_pipeline.workflows import (  # noqa: E402
    MANUAL_WORKFLOW, REGIONS_WORKFLOW, VIDEO_WORKFLOW, load_workflow,
)


def plan(args, manifest, state):
    still_wf = load_workflow(args.still_workflow or (
        REGIONS_WORKFLOW if manifest.still_mode == "regions"
        else MANUAL_WORKFLOW))
    video_wf = load_workflow(args.video_workflow or VIDEO_WORKFLOW)
    results = []  # (item_id, kind, status, record, new_fingerprint)
    for item in manifest.items:
        item_state = state["items"].get(item.item_id, {})
        for kind, spec, workflow, make, subdir in (
                ("still", item.still, still_wf, still_overrides,
                 args.still_save_subdir),
                ("video", item.video, video_wf, video_overrides,
                 args.video_save_subdir)):
            record = item_state.get(kind)
            if not record or spec is None:
                continue
            stored = record.get("plan_fingerprint")
            seed = item_seed(args.seed, item.item_id)
            new = _render_fingerprint(spec, workflow, make(args, seed))
            if stored == new:
                results.append((item.item_id, kind, "current", record, new))
                continue
            matched = None
            for index in range(manifest.item_count):
                old = _render_fingerprint(spec, workflow, dict(
                    make(args, args.seed + index), save_subdir=subdir))
                if old == stored:
                    matched = index
                    break
            if matched is None:
                results.append((item.item_id, kind, "unmatched", record, new))
            else:
                results.append((
                    item.item_id, kind, f"legacy (index {matched})", record, new))
    return results


def main(argv=None):
    parser = render_media.build_parser()
    parser.description = __doc__
    parser.add_argument("--write", action="store_true",
                        help="Apply the rewrite; default is a dry run.")
    args = parser.parse_args(argv)
    configured_input = validate_pipeline_input_dir(args.input_dir)
    input_dir = (render_media.ROOT / configured_input).resolve()
    output_dir = args.output_dir.resolve()
    manifest = load_pipeline_tree(
        input_dir, filename=pipeline_filename(args.pipeline_suffix))
    state = (load_render_state(args.state_file.resolve()) if args.state_file
             else load_render_state_tree(
                 output_dir, filename=RENDER_STATE_FILENAME))
    results = plan(args, manifest, state)
    counts = {}
    for item_id, kind, status, record, new in results:
        key = status.split(" ")[0]
        counts[key] = counts.get(key, 0) + 1
        print(f"{kind:5} {item_id}: {status}")
        if status.startswith("legacy") and args.write:
            record["plan_fingerprint"] = new
    print("summary:", ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
          or "no render records")
    if counts.get("legacy") and args.write:
        if args.state_file:
            save_render_state(args.state_file.resolve(), state)
        else:
            save_render_state_tree(output_dir, state, RENDER_STATE_FILENAME)
        print("state rewritten")
    elif counts.get("legacy"):
        print("dry run: re-run with --write to apply")
    return 1 if counts.get("unmatched") else 0


if __name__ == "__main__":
    raise SystemExit(main())
