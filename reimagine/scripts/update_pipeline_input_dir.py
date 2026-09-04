#!/usr/bin/env python3
"""Set input_dir in every pipeline.yaml beneath an output directory.

Both directory arguments may be symbolic links.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reimagine_pipeline.files import atomic_write_text  # noqa: E402


def project_relative_input(value):
    path = Path(value)
    if path.is_absolute():
        absolute = Path(os.path.abspath(path))
        try:
            relative = absolute.relative_to(ROOT)
        except ValueError as error:
            raise ValueError(
                f"input directory must be inside {ROOT}") from error
    else:
        relative = path
    if ".." in relative.parts or not relative.parts:
        raise ValueError("input directory must be relative to the reimagine folder")
    if not (ROOT / relative).is_dir():
        raise ValueError(f"input directory does not exist: {ROOT / relative}")
    return relative


def updated_yaml(text, input_dir):
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("pipeline is not a YAML mapping")
    value = json.dumps(input_dir.as_posix())
    replacement = f"input_dir: {value}"
    if re.search(r"^input_dir\s*:", text, flags=re.MULTILINE):
        return re.sub(
            r"^input_dir\s*:.*$", replacement, text,
            count=1, flags=re.MULTILINE)
    if re.search(r"^schema_version\s*:.*$", text, flags=re.MULTILINE):
        return re.sub(
            r"^(schema_version\s*:.*)$", rf"\1\n{replacement}", text,
            count=1, flags=re.MULTILINE)
    return f"{replacement}\n{text}"


def path_identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def find_pipeline_files(root):
    """Find pipeline files through linked directories without following cycles."""
    pending = [root]
    seen_directories = set()
    seen_files = set()
    paths = []
    while pending:
        directory = pending.pop()
        directory_identity = path_identity(directory)
        if directory_identity in seen_directories:
            continue
        seen_directories.add(directory_identity)
        for child in directory.iterdir():
            if child.is_dir():
                pending.append(child)
            elif child.name == "pipeline.yaml" and child.is_file():
                target = child.resolve()
                target_identity = path_identity(target)
                if target_identity not in seen_files:
                    seen_files.add(target_identity)
                    paths.append(target)
    return sorted(paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        help="Input path or symlink relative to the reimagine folder.")
    parser.add_argument(
        "output_dir",
        help="Output tree or symlink containing pipeline.yaml files.")
    args = parser.parse_args(argv)

    try:
        input_dir = project_relative_input(args.input_dir)
        output_arg = Path(args.output_dir)
        output_dir = (output_arg if output_arg.is_absolute()
                      else ROOT / output_arg)
        if not output_dir.is_dir():
            raise ValueError(f"output directory does not exist: {output_dir}")
        paths = find_pipeline_files(output_dir)
        if not paths:
            raise ValueError(f"no pipeline.yaml files under {output_dir}")
        changes = []
        for path in paths:
            text = path.read_text(encoding="utf-8")
            updated = updated_yaml(text, input_dir)
            if text != updated:
                changes.append((path, updated))
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))

    for path, text in changes:
        atomic_write_text(path, text)
    print(
        f"set input_dir: {input_dir.as_posix()} in {len(changes)}/{len(paths)} "
        "pipeline.yaml files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
