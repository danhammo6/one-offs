#!/usr/bin/env python
"""Render saved still and video plans using only ComfyUI."""
import argparse
import logging
import sys
import time
from pathlib import Path

from reimagine_pipeline import (
    RENDER_RUN_FILENAME, RENDER_STATE_FILENAME, pipeline_filename,
)
from reimagine_pipeline.files import (
    DEFAULT_CACHE_ROOT, THUMBNAIL_CACHE_SUBDIR,
)
from reimagine_pipeline.manifest import (
    incomplete_plan_messages, load_pipeline_tree, load_render_run,
    load_render_state, load_render_state_tree, save_render_run,
    save_render_state_tree, validate_pipeline_input_dir,
)
from reimagine_pipeline.rendering import render_all, render_stills, render_videos

logger = logging.getLogger(__name__)
ROOT = Path(__file__).parent.resolve()


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=Path("output"),
                        help="Rendered-media and render-state directory.")
    parser.add_argument(
        "--input-dir", type=Path, default=Path("input"),
        help="Project-relative reference tree containing reusable pipelines.")
    parser.add_argument(
        "--pipeline-suffix", default="",
        help="Optional safe suffix selecting pipeline_<suffix>.yaml files.")
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--cache-root", dest="cache_root", type=Path,
        default=DEFAULT_CACHE_ROOT,
        help="Disposable manifest-index and thumbnail cache directory.")
    cache_group.add_argument(
        "--thumbnail-cache-root", dest="cache_root", type=Path,
        default=argparse.SUPPRESS,
        help="Deprecated alias for --cache-root; now names the unified cache.")
    parser.add_argument(
        "--state-file", type=Path, default=None,
        help="Use one explicit state file instead of per-folder render_state.yaml files.")
    parser.add_argument("--stage", choices=("all", "stills", "videos"),
                        default="all", help="Media stage to render.")
    parser.add_argument("--comfy-server", default="192.168.33.101:8188",
                        help="ComfyUI server address.")
    parser.add_argument("--comfyui-output-dir", type=Path, default=None,
                        help="Optional local or mounted ComfyUI output directory.")
    parser.add_argument("--still-workflow", type=Path, default=None,
                        help="Custom still workflow JSON path.")
    parser.add_argument("--video-workflow", type=Path, default=None,
                        help="Custom video workflow JSON path.")
    parser.add_argument("--still-save-subdir", default="reimagine",
                        help="ComfyUI still output subdirectory.")
    parser.add_argument("--video-save-subdir", default="reimagine-video",
                        help="ComfyUI video output subdirectory.")
    parser.add_argument("--clip-name", default=None,
                        help="Still workflow CLIP model override.")
    parser.add_argument("--unet-name", default=None,
                        help="Still workflow UNet model override.")
    parser.add_argument("--video-clip-name", default=None,
                        help="Video workflow text encoder override.")
    parser.add_argument("--video-unet-name", default=None,
                        help="Video workflow diffusion model override.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Base render seed; item i uses seed + its index.")
    parser.add_argument("--force", action="store_true",
                        help="Rerender requested stages.")
    return parser


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(raw_args)
    if any(
            value == "--thumbnail-cache-root"
            or value.startswith("--thumbnail-cache-root=")
            for value in raw_args):
        logger.warning(
            "--thumbnail-cache-root is deprecated; use --cache-root")
    output_dir = args.output_dir.resolve()
    try:
        configured_input = validate_pipeline_input_dir(args.input_dir)
        input_dir = (ROOT / configured_input).resolve()
        pipeline_name = pipeline_filename(args.pipeline_suffix)
    except ValueError as error:
        logger.error("error: %s", error)
        return 2
    if (output_dir == input_dir
            or output_dir.is_relative_to(input_dir)
            or input_dir.is_relative_to(output_dir)):
        logger.error("error: --output-dir and --input-dir must not overlap")
        return 2
    args.cache_root = args.cache_root.expanduser()
    args.thumbnail_cache_root = args.cache_root / THUMBNAIL_CACHE_SUBDIR
    args.state_file = args.state_file.resolve() if args.state_file else None
    args.state_root = output_dir
    if args.comfyui_output_dir:
        args.comfyui_output_dir = args.comfyui_output_dir.resolve()
    try:
        started = time.perf_counter()
        manifest = load_pipeline_tree(input_dir, filename=pipeline_name)
        if manifest.input_dir != configured_input:
            raise ValueError(
                f"{pipeline_name} records input directory {manifest.input_dir}, "
                f"not {configured_input}")
        run_path = output_dir / RENDER_RUN_FILENAME
        if run_path.is_file():
            prior = load_render_run(run_path)
            selected = (configured_input, pipeline_name)
            if prior != selected:
                raise ValueError(
                    f"output directory is already linked to {prior[0]}/**/"
                    f"{prior[1]}; choose another --output-dir")
        else:
            save_render_run(run_path, configured_input, pipeline_name)
        for message in incomplete_plan_messages(manifest, args.stage):
            logger.warning("%s; rendering available items", message)
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
        logger.info("done in %.2fs: %d rendered, %d skipped, %d failed",
                    time.perf_counter() - started, rendered, skipped, failed)
        return 1 if failed else 0
    except (ValueError, OSError) as error:
        logger.error("error: %s", error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
