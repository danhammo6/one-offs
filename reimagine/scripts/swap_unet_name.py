#!/usr/bin/env python
"""Rewrite render_state.yaml plan fingerprints for a different UNet file.

Fingerprints hash the UNet name override, so switching to an equivalent model
file (e.g. int8 -> fp8) would otherwise look like a changed plan and rerender.
Pass the same render flags used for the original runs, including the old
--unet-name / --video-unet-name, plus --new-unet-name and/or
--new-video-unet-name for the replacement. Each record whose stored
fingerprint matches the old name is rewritten to the new one. Dry run unless
--write is given; images and output hashes are never touched.
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
        for kind, spec, workflow, make, new_name in (
                ("still", item.still, still_wf, still_overrides,
                 args.new_unet_name),
                ("video", item.video, video_wf, video_overrides,
                 args.new_video_unet_name)):
            record = item_state.get(kind)
            if not record or spec is None or not new_name:
                continue
            stored = record.get("plan_fingerprint")
            overrides = make(args, item_seed(args.seed, item.item_id))
            old = _render_fingerprint(spec, workflow, overrides)
            new = _render_fingerprint(
                spec, workflow, dict(overrides, unet_name=new_name))
            if stored == new:
                status = "current"
            elif stored == old:
                status = "swap"
            else:
                status = "unmatched"
            results.append((item.item_id, kind, status, record, new))
    return results


def build_parser():
    parser = render_media.build_parser()
    parser.description = __doc__
    parser.add_argument("--new-unet-name",
                        help="Replacement still UNet name (replaces --unet-name).")
    parser.add_argument("--new-video-unet-name",
                        help="Replacement video UNet name "
                             "(replaces --video-unet-name).")
    parser.add_argument("--write", action="store_true",
                        help="Apply the rewrite; default is a dry run.")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.new_unet_name or args.new_video_unet_name):
        parser.error("give --new-unet-name and/or --new-video-unet-name")
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
        counts[status] = counts.get(status, 0) + 1
        print(f"{kind:5} {item_id}: {status}")
        if status == "swap" and args.write:
            record["plan_fingerprint"] = new
    print("summary:", ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
          or "no render records")
    if counts.get("swap") and args.write:
        if args.state_file:
            save_render_state(args.state_file.resolve(), state)
        else:
            save_render_state_tree(output_dir, state, RENDER_STATE_FILENAME)
        print("state rewritten")
    elif counts.get("swap"):
        print("dry run: re-run with --write to apply")
    return 1 if counts.get("unmatched") else 0


if __name__ == "__main__":
    raise SystemExit(main())
