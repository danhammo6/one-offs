#!/usr/bin/env python3
"""Render saved still and video plans using only ComfyUI."""
import argparse
import sys
from pathlib import Path

from reimagine_pipeline import PIPELINE_FILENAME, RENDER_STATE_FILENAME
from reimagine_pipeline.manifest import (
    load_pipeline, load_pipeline_tree, load_render_state,
    load_render_state_tree, save_pipeline_tree, save_render_state_tree,
)
from reimagine_pipeline.projections import write_projections
from reimagine_pipeline.rendering import render_all, render_stills, render_videos


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument(
        "--manifest", type=Path, default=None,
        help="Use one explicit manifest instead of per-folder pipeline.yaml files.")
    parser.add_argument(
        "--state-file", type=Path, default=None,
        help="Use one explicit state file instead of per-folder render_state.yaml files.")
    parser.add_argument("--stage", choices=("all", "stills", "videos"),
                        default="all")
    parser.add_argument("--comfy-server", default="127.0.0.1:8188")
    parser.add_argument("--comfyui-output-dir", type=Path, default=None)
    parser.add_argument("--still-workflow", type=Path, default=None)
    parser.add_argument("--video-workflow", type=Path, default=None)
    parser.add_argument("--still-save-subdir", default="reimagine")
    parser.add_argument("--video-save-subdir", default="reimagine-video")
    parser.add_argument("--clip-name", default=None)
    parser.add_argument("--unet-name", default=None)
    parser.add_argument("--video-clip-name", default=None)
    parser.add_argument("--video-unet-name", default=None)
    parser.add_argument("--seed", type=int, default=42,
                        help="Base render seed; item i uses seed + its index.")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    manifest_path = args.manifest.resolve() if args.manifest else None
    args.state_file = args.state_file.resolve() if args.state_file else None
    args.state_root = output_dir
    if args.comfyui_output_dir:
        args.comfyui_output_dir = args.comfyui_output_dir.resolve()
    try:
        manifest = (load_pipeline(manifest_path, require_stage=args.stage)
                    if manifest_path else load_pipeline_tree(
                        output_dir, require_stage=args.stage,
                        filename=PIPELINE_FILENAME))
        if not manifest_path and (output_dir / PIPELINE_FILENAME).is_file():
            save_pipeline_tree(output_dir, manifest, PIPELINE_FILENAME)
        write_projections(output_dir, manifest)
        state = (load_render_state(args.state_file) if args.state_file
                 else load_render_state_tree(
                     output_dir, filename=RENDER_STATE_FILENAME))
        if not args.state_file:
            save_render_state_tree(output_dir, state, RENDER_STATE_FILENAME)
        rendered = skipped = failed = 0
        if args.stage == "all":
            counts = render_all(args, manifest, output_dir, state)
            rendered += counts[0]
            skipped += counts[1]
            failed += counts[2]
        elif args.stage == "stills":
            counts = render_stills(args, manifest, output_dir, state)
            rendered += counts[0]
            skipped += counts[1]
            failed += counts[2]
        else:
            counts = render_videos(args, manifest, output_dir, state)
            rendered += counts[0]
            skipped += counts[1]
            failed += counts[2]
        print(f"done: {rendered} rendered, {skipped} skipped, {failed} failed")
        return 1 if failed else 0
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
