#!/usr/bin/env python
"""Generate durable still and video prompt plans using only an LLM."""
import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from reimagine_pipeline import PIPELINE_FILENAME
from reimagine_pipeline.files import derive_dims, iter_images, sha256_file
from reimagine_pipeline.llm import ClaudeCodeLLM, OpenAILLM
from reimagine_pipeline.manifest import (
    load_pipeline, load_pipeline_tree, pipeline_paths, save_pipeline,
    save_pipeline_folder, save_pipeline_tree,
)
from reimagine_pipeline.models import PipelineItem, PipelineManifest, StillSpec, VideoSpec
from reimagine_pipeline.projections import write_projections
from reimagine_pipeline.prompting import generate_still_prompt, generate_video_prompt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptJob:
    index: int
    item_id: str
    source: Path
    relative: Path
    source_hash: str
    width: int
    height: int


def build_llm(args, input_dir):
    if args.llm_server:
        llm = OpenAILLM(
            args.llm_server, model=args.llm_model,
            max_tokens=args.llm_max_tokens, reasoning=args.llm_reasoning)
    else:
        llm = ClaudeCodeLLM(model=args.claude_model, add_dir=input_dir)
    llm.log_reasoning = args.verbose >= 2
    return llm


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--input-dir", type=Path, default=Path("input"),
                        help="Reference-image directory tree.")
    parser.add_argument("--output-dir", type=Path, default=Path("output"),
                        help="Prompt manifests and generated media directory.")
    parser.add_argument(
        "--manifest", type=Path, default=None,
        help="Use one explicit manifest instead of per-folder pipeline.yaml files.")
    parser.add_argument("--stage", choices=("all", "stills", "videos"),
                        default="all", help="Prompt stage to generate.")
    parser.add_argument("--still-mode", choices=("manual", "regions"),
                        default="manual", help="Still prompt representation.")
    parser.add_argument("--video-basis", choices=("reference", "rendered"),
                        default="reference", help="Image used to plan video motion.")
    parser.add_argument("--duration", type=int, default=10,
                        help="Generated video duration in seconds (1-30).")
    parser.add_argument("--prompt-path-prefix", type=Path,
                        default=Path("prompts"),
                        help="Directory containing system prompt files.")
    parser.add_argument("--claude-model", default="opus",
                        help="Claude Code model used without --llm-server.")
    parser.add_argument("--llm-server", default=None,
                        help="OpenAI-compatible multimodal server address.")
    parser.add_argument("--llm-model", default=None,
                        help="Model ID for the OpenAI-compatible server.")
    parser.add_argument(
        "--llm-max-tokens", type=int, default=16384,
        help="Maximum completion tokens for the OpenAI-compatible server.")
    parser.add_argument(
        "--llm-reasoning", choices=("off", "on"), default="on",
        help="Model reasoning mode for llama.cpp requests.")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate requested stages.")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Log rejected LLM responses; repeat to also log reasoning content.")
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
        jobs.append(PromptJob(
            index, item_id, source, relative, sha256_file(source), width, height))
    return jobs


def _load_planning_state(args, jobs, output_dir, manifest_path):
    existing = None
    folder_counts = {}
    for job in jobs:
        folder_counts[job.relative.parent] = (
            folder_counts.get(job.relative.parent, 0) + 1)
    if manifest_path and manifest_path.is_file():
        existing = load_pipeline(manifest_path)
    elif not manifest_path and pipeline_paths(output_dir, PIPELINE_FILENAME):
        existing = load_pipeline_tree(output_dir, filename=PIPELINE_FILENAME)
        checkpointed_parents = {
            item.source_path.parent for item in existing.items}
        checkpointed_count = sum(
            folder_counts.get(parent, -1) for parent in checkpointed_parents)
        if (not args.force and (
                existing.still_mode != args.still_mode
                or existing.item_count != checkpointed_count)):
            raise ValueError("existing pipeline mode or inventory differs; use --force")
    still_mode = (existing.still_mode if existing and args.stage == "videos"
                  else args.still_mode)
    by_id = {} if not existing else {
        item.item_id: item for item in existing.items}
    jobs_by_id = {job.item_id: job for job in jobs}
    if existing and not (args.force and args.stage == "all"):
        for item_id, item in by_id.items():
            job = jobs_by_id.get(item_id)
            if job is None or item.source_path != job.relative:
                raise ValueError("existing pipeline inventory differs; use --force")
    if args.force and args.stage == "all":
        by_id = {}
        if not manifest_path:
            for parent in folder_counts:
                save_pipeline_folder(
                    output_dir, parent,
                    PipelineManifest(args.still_mode, 0, []),
                    PIPELINE_FILENAME, prune_empty=True)
    return still_mode, by_id, folder_counts


def _plan_item(args, llm, job, item, still_mode, output_dir, prompt_dir):
    still = item.still if item else None
    video = item.video if item else None
    if args.stage in {"all", "stills"} and (args.force or still is None):
        started = time.perf_counter()
        try:
            result = generate_still_prompt(
                llm, job.source, still_mode, prompt_dir=prompt_dir)
        except Exception:
            logger.error("still prompt %s: failed after %.2fs",
                         job.item_id, time.perf_counter() - started)
            raise
        logger.info("still prompt %s: generated in %.2fs",
                    job.item_id, time.perf_counter() - started)
        still = StillSpec(
            job.relative.with_suffix(".jpg"), job.width, job.height,
            prompt=result if still_mode == "manual" else None,
            regions=result if still_mode == "regions" else None)
        video = None
    if args.stage in {"all", "videos"}:
        if not still:
            raise ValueError("video prompting requires a saved still plan")
        if args.video_basis == "rendered":
            basis_image = output_dir / still.output
            if not basis_image.is_file():
                raise ValueError(f"rendered video basis is missing: {basis_image}")
            basis_hash = sha256_file(basis_image)
        else:
            basis_image = job.source
            basis_hash = job.source_hash
        video_is_current = (
            video is not None
            and video.prompt_basis == args.video_basis
            and video.basis_sha256 == basis_hash
            and video.duration == args.duration)
        if args.force or not video_is_current:
            started = time.perf_counter()
            try:
                prompt = generate_video_prompt(
                    llm, basis_image, args.video_basis, still, args.duration,
                    prompt_dir=prompt_dir)
            except Exception:
                logger.error("video prompt %s: failed after %.2fs",
                             job.item_id, time.perf_counter() - started)
                raise
            logger.info("video prompt %s: generated in %.2fs",
                        job.item_id, time.perf_counter() - started)
            video = VideoSpec(
                still.output.with_suffix(".mp4"), prompt,
                args.video_basis, basis_hash, args.duration)
    return PipelineItem(
        job.index, job.item_id, job.relative, job.source_hash, still, video)


def _save_checkpoint(output_dir, manifest_path, folder_counts, job, manifest):
    if manifest_path:
        save_pipeline(manifest_path, manifest)
    else:
        save_pipeline_folder(
            output_dir, job.relative.parent, manifest, PIPELINE_FILENAME,
            folder_counts[job.relative.parent])
    write_projections(
        output_dir, manifest,
        None if manifest_path else {job.relative.parent})


def _run(args):
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    prompt_dir = args.prompt_path_prefix.resolve()
    manifest_path = args.manifest.resolve() if args.manifest else None
    jobs = discover(input_dir)
    still_mode, by_id, folder_counts = _load_planning_state(
        args, jobs, output_dir, manifest_path)
    llm = build_llm(
        args, input_dir if args.video_basis == "reference" else output_dir)
    logger.info("generate prompts: %d item(s), stage=%s", len(jobs), args.stage)
    logger.info("  llm:      %s", llm.describe())
    logger.info("  prompts:  %s", prompt_dir)
    logger.info("  manifest: %s", manifest_path or f"{output_dir}/**/{PIPELINE_FILENAME}")
    failed = generated = skipped = 0
    manifest = PipelineManifest(still_mode, len(jobs), [])
    started = time.perf_counter()
    for job in jobs:
        tag = f"[{job.index + 1}/{len(jobs)}] {job.relative}"
        item = by_id.get(job.item_id)
        if item and item.source_sha256 != job.source_hash:
            item = None
        try:
            planned = _plan_item(
                args, llm, job, item, still_mode, output_dir, prompt_dir)
        except Exception as error:
            logger.error("%s  failed: %s", tag, error)
            failed += 1
            continue
        changed = planned != item
        by_id[job.item_id] = planned
        manifest = PipelineManifest(
            still_mode, len(jobs),
            sorted(by_id.values(), key=lambda value: value.index))
        _save_checkpoint(
            output_dir, manifest_path, folder_counts, job, manifest)
        if changed:
            generated += 1
            logger.info("%s  planned", tag)
        else:
            skipped += 1
            logger.info("%s  saved plan unchanged", tag)
    logger.info(
        "done in %.2fs: %d planned, %d unchanged, %d failed",
        time.perf_counter() - started, generated, skipped, failed)
    if not manifest_path and not failed:
        save_pipeline_tree(output_dir, manifest, PIPELINE_FILENAME)
    return 1 if failed else 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s")
    if not 1 <= args.duration <= 30:
        logger.error("error: --duration must be between 1 and 30")
        return 2
    if args.llm_max_tokens <= 0:
        logger.error("error: --llm-max-tokens must be positive")
        return 2
    try:
        return _run(args)
    except (ValueError, OSError) as error:
        logger.error("error: %s", error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
