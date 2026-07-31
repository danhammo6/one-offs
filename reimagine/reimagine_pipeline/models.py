from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class StillSpec:
    output: Path
    width: int
    height: int
    prompt: str | None = None
    regions: dict | None = None
    seed: int = 42


@dataclass(frozen=True)
class VideoSpec:
    output: Path
    prompt: str
    prompt_basis: str
    basis_sha256: str
    duration: int = 10
    seed: int = 42


@dataclass(frozen=True)
class PipelineItem:
    index: int
    item_id: str
    source_path: Path
    source_sha256: str
    still: StillSpec | None = None
    video: VideoSpec | None = None


@dataclass
class PipelineManifest:
    still_mode: str
    item_count: int
    items: list[PipelineItem] = field(default_factory=list)
    schema_version: int = 2


@dataclass
class Summary:
    generated: int = 0
    rendered: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def exit_code(self):
        return 1 if self.failed else 0
