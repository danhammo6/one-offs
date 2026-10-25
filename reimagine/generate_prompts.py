#!/usr/bin/env python
"""Generate durable still and video prompt plans using only an LLM."""
import argparse
import contextlib
import dataclasses
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from reimagine_pipeline import pipeline_filename
from reimagine_pipeline.files import (
    common_target_dims, derive_dims, iter_images, prepare_common_image,
    sha256_file,
)
from reimagine_pipeline.llm import OpenAILLM
from reimagine_pipeline.manifest import (
    load_pipeline, load_pipeline_tree, pipeline_paths, save_pipeline_folder,
    save_pipeline_tree, validate_pipeline_input_dir,
)
from reimagine_pipeline.models import PipelineItem, PipelineManifest, StillSpec, VideoSpec
from reimagine_pipeline.prompting import generate_still_prompt, generate_video_prompt

logger = logging.getLogger(__name__)
ROOT = Path(__file__).parent.resolve()


@dataclass(frozen=True)
class PromptJob:
    index: int
    item_id: str
    source: Path
    relative: Path
    width: int
    height: int
    size: int
    mtime_ns: int
    prompt_source: Path | None = None
    source_hash: str | None = None


def build_llm(args):
    llm = OpenAILLM(
        args.llm_server, model=args.llm_model,
        api_key=(os.environ.get("OMLX_API_KEY")
                 or os.environ.get("OPENAI_API_KEY")),
        max_tokens=args.llm_max_tokens, reasoning=args.llm_reasoning)
    llm.log_reasoning = args.verbose >= 2
    return llm


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--original-dims", dest="common_dims", action="store_false",
        help="Prompt with the original reference images and derived "
             "dimensions instead of center-cropping temporary copies to the "
             "closest common 1.5 MP dimensions.")
    parser.add_argument(
        "--input-dir", type=Path, default=Path("input"),
        help="Project-relative reference tree where reusable pipeline files live.")
    parser.add_argument(
        "--pipeline-suffix", default="",
        help="Optional safe suffix for pipeline_<suffix>.yaml files.")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output"),
        help="Rendered-media directory, used when --video-basis=rendered.")
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
                        help="Directory containing user prompt templates and schemas.")
    system_group = parser.add_mutually_exclusive_group()
    system_group.add_argument(
        "--system-prompt", default=None,
        help="Optional system message applied to every LLM request.")
    system_group.add_argument(
        "--system-prompt-file", type=Path, default=None,
        help="Read the optional system message from this UTF-8 file.")
    parser.add_argument("--llm-server", default="127.0.0.1:9503",
                        help="OpenAI-compatible multimodal server address; auth "
                             "uses OMLX_API_KEY or OPENAI_API_KEY.")
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


def load_optional_system_prompt(args):
    if args.system_prompt_file is None:
        return args.system_prompt
    try:
        return args.system_prompt_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(
            f"could not read system prompt {args.system_prompt_file}: {error}") from error


def discover(input_dir, common_dims=False):
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
        width, height = (common_target_dims(source) if common_dims
                         else derive_dims(source))
        stat = source.stat()
        jobs.append(PromptJob(
            index, item_id, source, relative, width, height,
            stat.st_size, stat.st_mtime_ns))
    return jobs


def _resolve_source_hash(job, item):
    if (item and item.source_size == job.size
            and item.source_mtime_ns == job.mtime_ns):
        return item.source_sha256
    return sha256_file(job.source)


def _needs_prompt_source(args, item):
    if args.stage in {"all", "stills"} and (
            args.force or not (item and item.still)):
        return True
    return args.stage in {"all", "videos"} and args.video_basis == "reference"


def _prepare_prompt_source(job, common_dir):
    if common_dir is None:
        return job.source
    destination = common_dir / job.relative.with_suffix(".jpg")
    prepare_common_image(job.source, destination)
    return destination


def _rebind_item_paths(item, job):
    still = item.still
    if still:
        still = dataclasses.replace(still, output=job.relative.with_suffix(".jpg"))
    video = item.video
    if video:
        video_suffix = video.output.suffix or ".mp4"
        video = dataclasses.replace(video, output=job.relative.with_suffix(video_suffix))
    return dataclasses.replace(
        item, index=job.index, item_id=job.item_id, source_path=job.relative,
        still=still, video=video)


def _load_planning_state(
        args, jobs, pipeline_root, pipeline_name, configured_input):
    existing = None
    folder_counts = {}
    for job in jobs:
        folder_counts[job.relative.parent] = (
            folder_counts.get(job.relative.parent, 0) + 1)
    existing_paths = pipeline_paths(pipeline_root, pipeline_name)
    if existing_paths:
        existing = load_pipeline_tree(pipeline_root, filename=pipeline_name)
        if not args.force and args.stage != "videos" and (
                existing.still_mode != args.still_mode
                or existing.common_dims != args.common_dims):
            raise ValueError(
                "existing pipeline mode or preprocessing differs; "
                "use --force")
    if (existing and args.stage == "videos"
            and existing.common_dims != args.common_dims):
        raise ValueError(
            "existing pipeline source preprocessing differs; use the matching "
            "--original-dims setting")
    still_mode = (existing.still_mode if existing and args.stage == "videos"
                  else args.still_mode)
    if args.force and args.stage == "all":
        for path in existing_paths:
            if path.parent.relative_to(pipeline_root) not in folder_counts:
                path.unlink(missing_ok=True)
        for parent in folder_counts:
            save_pipeline_folder(
                pipeline_root, parent,
                PipelineManifest(
                    args.still_mode, 0, [], common_dims=args.common_dims,
                    input_dir=configured_input),
                pipeline_name, prune_empty=True)
        return still_mode, {}, {}, folder_counts

    by_id = {}
    by_hash = {}
    if existing:
        jobs_by_id = {job.item_id: job for job in jobs}
        unmatched = []
        for item in existing.items:
            job = jobs_by_id.get(item.item_id)
            if job is not None and item.source_path == job.relative:
                by_id[item.item_id] = item
            else:
                unmatched.append(item)
        for item in unmatched:
            by_hash.setdefault(item.source_sha256, item)

        reconciled_manifest = PipelineManifest(
            still_mode, len(jobs),
            sorted(by_id.values(), key=lambda value: value.index),
            common_dims=args.common_dims, input_dir=configured_input)
        existing_parents = {
            path.parent.relative_to(pipeline_root) for path in existing_paths}
        for parent in sorted(existing_parents):
            if parent not in folder_counts:
                (pipeline_root / parent / pipeline_name).unlink(missing_ok=True)
            else:
                save_pipeline_folder(
                    pipeline_root, parent, reconciled_manifest, pipeline_name,
                    folder_counts[parent])
    if len(configured_input.parts) > 1 and pipeline_root.parent.is_dir():
        for sibling_path in pipeline_paths(pipeline_root.parent, pipeline_name):
            if sibling_path.is_relative_to(pipeline_root):
                continue
            try:
                sibling_manifest = load_pipeline(sibling_path)
            except ValueError:
                continue
            if (sibling_manifest.still_mode != still_mode
                    or sibling_manifest.common_dims != args.common_dims):
                continue
            for item in sibling_manifest.items:
                by_hash.setdefault(item.source_sha256, item)
    return still_mode, by_id, by_hash, folder_counts


def _plan_item(
        args, llm, job, item, still_mode, output_dir, prompt_dir,
        system_prompt):
    still = item.still if item else None
    video = item.video if item else None
    if args.stage in {"all", "stills"} and (args.force or still is None):
        started = time.perf_counter()
        try:
            result = generate_still_prompt(
                llm, job.prompt_source, still_mode, prompt_dir=prompt_dir,
                system_prompt=system_prompt)
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
            basis_image = job.prompt_source
            basis_hash = sha256_file(basis_image)
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
                    prompt_dir=prompt_dir, system_prompt=system_prompt)
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
        job.index, job.item_id, job.relative, job.source_hash, still, video,
        job.size, job.mtime_ns)


def _save_checkpoint(
        pipeline_root, pipeline_name, folder_counts, job, manifest):
    save_pipeline_folder(
        pipeline_root, job.relative.parent, manifest, pipeline_name,
        folder_counts[job.relative.parent])


def _run(args):
    output_dir = args.output_dir.resolve()
    configured_input = validate_pipeline_input_dir(args.input_dir)
    input_dir = (ROOT / configured_input).resolve()
    pipeline_name = pipeline_filename(args.pipeline_suffix)
    prompt_dir = args.prompt_path_prefix.resolve()
    system_prompt = load_optional_system_prompt(args)
    preprocess = args.common_dims
    directory = (tempfile.TemporaryDirectory(prefix="reimagine-common-")
                 if preprocess else contextlib.nullcontext(None))
    with directory as common_dir:
        common_dir = Path(common_dir) if common_dir else None
        jobs = discover(input_dir, preprocess)
        still_mode, by_id, by_hash, folder_counts = _load_planning_state(
            args, jobs, input_dir, pipeline_name, configured_input)
        llm = build_llm(args)
        logger.info("generate prompts: %d item(s), stage=%s", len(jobs), args.stage)
        logger.info("  llm:      %s", llm.describe())
        logger.info("  prompts:  %s", prompt_dir)
        logger.info("  pipeline: %s/**/%s", input_dir, pipeline_name)
        logger.info("  system:   %s", args.system_prompt_file or (
            "inline" if args.system_prompt is not None else "none"))
        if common_dir:
            logger.info("  source:   temporary common-dimension crops")
        failed = generated = skipped = 0
        manifest = PipelineManifest(
            still_mode, len(jobs), [], common_dims=args.common_dims,
            input_dir=configured_input)
        started = time.perf_counter()
        for job in jobs:
            tag = f"[{job.index + 1}/{len(jobs)}] {job.relative}"
            item = by_id.get(job.item_id)
            rebound = False
            dimensions_changed = (
                args.stage in {"all", "stills"} and item and item.still
                and (item.still.width, item.still.height) != (job.width, job.height))
            try:
                source_hash = _resolve_source_hash(job, item)
                if item and (item.source_sha256 != source_hash
                             or dimensions_changed):
                    item = None
                if item is None:
                    matched = by_hash.get(source_hash)
                    if matched and (
                            args.stage == "videos"
                            or not matched.still
                            or (matched.still.width, matched.still.height) == (job.width, job.height)):
                        item = _rebind_item_paths(matched, job)
                        rebound = True
                job = dataclasses.replace(
                    job, source_hash=source_hash,
                    prompt_source=(_prepare_prompt_source(job, common_dir)
                                   if _needs_prompt_source(args, item)
                                   else None))
                planned = _plan_item(
                    args, llm, job, item, still_mode, output_dir, prompt_dir,
                    system_prompt)
            except Exception as error:
                logger.error("%s  failed: %s", tag, error)
                failed += 1
                continue
            # Stored indexes are renumbered per folder on save, so they differ
            # from discovery indexes whenever an earlier item is missing.
            changed = item is None or dataclasses.replace(
                planned, index=item.index, source_size=item.source_size,
                source_mtime_ns=item.source_mtime_ns) != item
            stat_changed = item is not None and (
                planned.source_size != item.source_size
                or planned.source_mtime_ns != item.source_mtime_ns)
            by_id[job.item_id] = planned
            manifest = PipelineManifest(
                still_mode, len(jobs),
                sorted(by_id.values(), key=lambda value: value.index),
                common_dims=args.common_dims, input_dir=configured_input)
            if changed or rebound or stat_changed:
                _save_checkpoint(
                    input_dir, pipeline_name, folder_counts, job, manifest)
            if changed:
                generated += 1
                logger.info("%s  planned", tag)
            elif rebound:
                skipped += 1
                logger.info("%s  reused moved plan", tag)
            else:
                skipped += 1
                logger.info("%s  saved plan unchanged", tag)
        logger.info(
            "done in %.2fs: %d planned, %d unchanged, %d failed",
            time.perf_counter() - started, generated, skipped, failed)
        if not failed:
            save_pipeline_tree(input_dir, manifest, pipeline_name)
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
    except KeyboardInterrupt:
        logger.info("interrupted")
        return 130
    except (ValueError, OSError) as error:
        logger.error("error: %s", error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
