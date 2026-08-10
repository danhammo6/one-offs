#!/usr/bin/env python3
"""Collect first-attempt prompt failures from logs into a replay input tree."""
import argparse
import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

RETRY_RE = re.compile(r"^(?P<kind><[^>]+>.*?) prompt attempt 1/3 rejected")
GENERATED_RE = re.compile(r"^(?P<stage>still|video) prompt (?P<item>[^:]+):")
PLANNED_RE = re.compile(r"^\[\d+/\d+\] (?P<file>.+?)\s{2}(?:planned|failed:)")


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("logs", nargs="+", type=Path,
                        help="Prompt-generation log files to inspect.")
    parser.add_argument("--source-dir", type=Path, required=True,
                        help="Original reference-image directory tree.")
    parser.add_argument("--output-dir", type=Path,
                        default=Path("input/first-attempt-failures"),
                        help="Directory to populate with failed images.")
    return parser


def first_attempt_failures(path):
    failures = []
    pending_retries = []
    last_item = None
    for line in path.read_text(encoding="utf-8").splitlines():
        retry = RETRY_RE.match(line)
        if retry:
            pending_retries.append(retry.group("kind"))
            continue
        generated = GENERATED_RE.match(line)
        if generated:
            last_item = generated.group("item")
            if pending_retries:
                failures.extend(
                    (last_item, kind, path.name) for kind in pending_retries)
                pending_retries = []
            continue
        planned = PLANNED_RE.match(line)
        if planned and pending_retries:
            item = Path(planned.group("file")).with_suffix("").as_posix()
            failures.extend((item, kind, path.name) for kind in pending_retries)
            pending_retries = []
    return failures


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    source_dir = args.source_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    for path in args.logs:
        failures.extend(first_attempt_failures(path))
    by_item = {}
    for item, kind, log_name in failures:
        by_item.setdefault(item, []).append((kind, log_name))
    for item, reasons in sorted(by_item.items()):
        candidates = list(source_dir.glob(f"{item}.*"))
        if len(candidates) != 1:
            raise ValueError(f"expected one source image for {item}, found {candidates}")
        shutil.copy2(candidates[0], output_dir / candidates[0].name)
        logger.info("%s: %s", item, ", ".join(
            f"{kind} ({log_name})" for kind, log_name in reasons))
    logger.info("copied %d first-attempt failure image(s) to %s",
                len(by_item), output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
