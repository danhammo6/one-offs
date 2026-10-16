#!/usr/bin/env python
"""Remove orphaned thumbnails from the disposable cache.

A cached WebP is kept only if its source image still exists and has the same
fingerprint, so images deleted outside the gallery UI and images that were
re-rendered (new fingerprint, old WebP left behind) are both dropped. Cache
directories for media roots that are no longer served are removed too; those
are named by a hash, so "no longer served" means "not among the sources this
command is pointed at". Use the same --outputs-dir/--output-dir/--cache-root
as the gallery server. Dry run unless --write is given. Safe while the server
runs; anything pruned that is still wanted is regenerated on demand.
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reimagine_pipeline.files import (  # noqa: E402
    DEFAULT_CACHE_ROOT, THUMBNAIL_CACHE_SUBDIR, prune_thumbnail_cache,
)
import serve  # noqa: E402


def collect_roots(sources):
    """Return {namespace: [media roots]} for the given {name: output dir}."""
    inputs = []
    for output_dir in sources.values():
        input_dir, _ = serve.load_pipeline_metadata(output_dir)
        if input_dir is not None and input_dir not in inputs:
            inputs.append(input_dir)
    return {"output": list(sources.values()), "input": inputs}


def main():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=serve.HelpFormatter)
    parser.add_argument("--outputs-dir", type=Path, default=ROOT / "outputs",
                        help="Top-level dir of output sources.")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="A single flat output dir instead of outputs/.")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT,
                        help="Cache directory the gallery server uses.")
    parser.add_argument("--keep-unknown-roots", action="store_true",
                        help="Leave cache directories for roots not among "
                             "the sources given.")
    parser.add_argument("--write", action="store_true",
                        help="Delete files; without it only report.")
    args = parser.parse_args()

    if args.output_dir is not None:
        directory = args.output_dir.resolve()
        sources = {directory.name: directory}
    else:
        sources = serve.discover_sources(args.outputs_dir.resolve())
    if not sources:
        sys.exit("no output sources found; refusing to prune against nothing")

    stats = prune_thumbnail_cache(
        args.cache_root.expanduser() / THUMBNAIL_CACHE_SUBDIR,
        collect_roots(sources), dry_run=not args.write,
        remove_unknown_roots=not args.keep_unknown_roots)
    for note in stats.skipped:
        print(f"skipped {note}")
    verb = "removed" if args.write else "would remove"
    print(f"{verb} {stats.files} thumbnail(s), "
          f"{stats.bytes / 1_048_576:.1f} MiB"
          + ("" if not args.write else f", {stats.dirs} empty dir(s)"))
    if not args.write and stats.files:
        print("dry run; pass --write to delete")


if __name__ == "__main__":
    main()
