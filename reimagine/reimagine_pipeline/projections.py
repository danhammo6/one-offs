from collections import defaultdict

import yaml

from .files import atomic_write_text
from .prompting import regions_to_text


def write_projections(output_dir, manifest, parents=None):
    parents = set(parents) if parents is not None else None
    stills = defaultdict(dict)
    videos = defaultdict(dict)
    for item in manifest.items:
        if item.still and (parents is None or item.still.output.parent in parents):
            text = item.still.prompt or regions_to_text(item.still.regions)
            stills[item.still.output.parent][item.still.output.name] = text
        if item.video and (parents is None or item.video.output.parent in parents):
            videos[item.video.output.parent][item.video.output.name] = item.video.prompt
    for parent, values in stills.items():
        path = output_dir / parent / "prompts.yaml"
        atomic_write_text(path, yaml.safe_dump(
            values, sort_keys=True, allow_unicode=True,
            default_flow_style=False, width=1000))
    for parent, values in videos.items():
        path = output_dir / parent / "video_prompts.yaml"
        atomic_write_text(path, yaml.safe_dump(
            values, sort_keys=True, allow_unicode=True,
            default_flow_style=False, width=1000))
