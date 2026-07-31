#!/usr/bin/env python3
"""Generate durable still and video prompt plans using only an LLM."""
import argparse
import sys
from pathlib import Path

from reimagine_pipeline import PIPELINE_FILENAME
from reimagine_pipeline.files import derive_dims, iter_images, sha256_file
from reimagine_pipeline.llm import ClaudeCodeLLM, OpenAILLM
from reimagine_pipeline.manifest import load_pipeline, save_pipeline
from reimagine_pipeline.models import PipelineItem, PipelineManifest, StillSpec, VideoSpec
from reimagine_pipeline.projections import write_projections
from reimagine_pipeline.prompting import generate_still_prompt, generate_video_prompt


def build_llm(args, input_dir):
    if args.llm_server:
        return OpenAILLM(args.llm_server, model=args.llm_model)
    return ClaudeCodeLLM(model=args.claude_model, add_dir=input_dir)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("input"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--stage", choices=("all", "stills", "videos"),
                        default="all")
    parser.add_argument("--still-mode", choices=("manual", "regions"),
                        default="manual")
    parser.add_argument("--video-basis", choices=("reference", "rendered"),
                        default="reference")
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--claude-model", default="opus")
    parser.add_argument("--llm-server", default=None)
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--force", action="store_true")
    return parser


def discover(input_dir):
    images = list(iter_images(input_dir))
    if not images:
        raise ValueError(f"no reference images under {input_dir}")
    jobs = []
    ids = set()
    outputs = set()
    for index, source in enumerate(images):
        relative = source.relative_to(input_dir)
        output = relative.with_suffix(".jpg")
        item_id = relative.with_suffix("").as_posix()
        if item_id in ids or output in outputs:
            raise ValueError(f"duplicate pipeline identity at {relative}")
        ids.add(item_id)
        outputs.add(output)
        width, height = derive_dims(source)
        jobs.append((
            index, item_id, source, relative, sha256_file(source), width, height))
    return jobs


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not 1 <= args.duration <= 30:
        print("error: --duration must be between 1 and 30", file=sys.stderr)
        return 2
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    manifest_path = (args.manifest or output_dir / PIPELINE_FILENAME).resolve()
    try:
        jobs = discover(input_dir)
        existing = None
        if manifest_path.is_file():
            existing = load_pipeline(manifest_path)
            if (not args.force and (
                    existing.still_mode != args.still_mode
                    or existing.item_count != len(jobs))):
                raise ValueError("existing pipeline mode or inventory differs; use --force")
        still_mode = (existing.still_mode if existing and args.stage == "videos"
                      else args.still_mode)
        by_id = {} if not existing else {
            item.item_id: item for item in existing.items}
        jobs_by_id = {job[1]: job for job in jobs}
        if existing and not (args.force and args.stage == "all"):
            for item_id, item in by_id.items():
                job = jobs_by_id.get(item_id)
                if (job is None or item.index != job[0]
                        or item.source_path != job[3]):
                    raise ValueError(
                        "existing pipeline inventory differs; use --force")
        if args.force and args.stage == "all":
            by_id = {}
        llm = build_llm(args, input_dir if args.video_basis == "reference" else output_dir)
        print(f"generate prompts: {len(jobs)} item(s), stage={args.stage}")
        print(f"  llm:      {llm.describe()}")
        print(f"  manifest: {manifest_path}")
        failed = generated = skipped = 0
        for index, item_id, source, relative, source_hash, width, height in jobs:
            tag = f"[{index + 1}/{len(jobs)}] {relative}"
            item = by_id.get(item_id)
            if item and item.source_sha256 != source_hash:
                item = None
            still = item.still if item else None
            video = item.video if item else None
            try:
                still_is_current = still is not None
                if args.stage in {"all", "stills"} and (
                        args.force or not still_is_current):
                    result = generate_still_prompt(llm, source, still_mode)
                    still = StillSpec(
                        relative.with_suffix(".jpg"), width, height,
                        prompt=result if still_mode == "manual" else None,
                        regions=result if still_mode == "regions" else None)
                    video = None
                if args.stage in {"all", "videos"}:
                    if not still:
                        raise ValueError("video prompting requires a saved still plan")
                    if args.video_basis == "rendered":
                        basis_image = output_dir / still.output
                        if not basis_image.is_file():
                            raise ValueError(
                                f"rendered video basis is missing: {basis_image}")
                        basis_hash = sha256_file(basis_image)
                    else:
                        basis_image = source
                        basis_hash = source_hash
                    video_is_current = (
                        video is not None
                        and video.prompt_basis == args.video_basis
                        and video.basis_sha256 == basis_hash
                        and video.duration == args.duration)
                    if args.force or not video_is_current:
                        prompt = generate_video_prompt(
                            llm, basis_image, args.video_basis, still,
                            args.duration)
                        video = VideoSpec(
                            still.output.with_suffix(".mp4"), prompt,
                            args.video_basis, basis_hash, args.duration)
            except Exception as error:
                print(f"{tag}  failed: {error}")
                failed += 1
                continue
            changed = not item or item.still != still or item.video != video
            by_id[item_id] = PipelineItem(
                index, item_id, relative, source_hash, still, video)
            manifest = PipelineManifest(
                still_mode, len(jobs),
                sorted(by_id.values(), key=lambda value: value.index))
            save_pipeline(manifest_path, manifest)
            write_projections(output_dir, manifest)
            if changed:
                generated += 1
                print(f"{tag}  planned")
            else:
                skipped += 1
                print(f"{tag}  saved plan unchanged")
        print(f"done: {generated} planned, {skipped} unchanged, {failed} failed")
        return 1 if failed else 0
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
