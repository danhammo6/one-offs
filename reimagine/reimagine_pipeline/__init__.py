"""Shared prompt-planning and media-rendering pipeline."""
import re


PIPELINE_FILENAME = "pipeline.yaml"
RENDER_RUN_FILENAME = "render_run.yaml"
RENDER_STATE_FILENAME = "render_state.yaml"

_PIPELINE_SUFFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_PIPELINE_NAME = re.compile(
    r"pipeline(?:_[A-Za-z0-9][A-Za-z0-9_-]{0,99})?\.yaml")


def pipeline_filename(suffix=""):
    """Return the safe pipeline filename selected by a CLI suffix."""
    if suffix is None or suffix == "":
        return PIPELINE_FILENAME
    if not isinstance(suffix, str) or not _PIPELINE_SUFFIX.fullmatch(suffix):
        raise ValueError(
            "pipeline suffix must start with a letter or number and contain "
            "only letters, numbers, underscores, or hyphens (100 characters max)")
    return f"pipeline_{suffix}.yaml"


def validate_pipeline_filename(value):
    """Validate a persisted pipeline filename without accepting a path."""
    if not isinstance(value, str) or not _PIPELINE_NAME.fullmatch(value):
        raise ValueError(f"invalid pipeline filename: {value!r}")
    return value
