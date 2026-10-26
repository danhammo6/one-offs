import hashlib
import io
import logging
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ".reimagine-cache"
THUMBNAIL_CACHE_SUBDIR = "thumbnails"
DEFAULT_CACHE_ROOT = PROJECT_ROOT / CACHE_DIR
DEFAULT_THUMBNAIL_CACHE_ROOT = DEFAULT_CACHE_ROOT / THUMBNAIL_CACHE_SUBDIR
THUMBNAIL_MAX_EDGE = 512
THUMBNAIL_QUALITY = 80
TARGET_PIXELS = 1920 * 1080
MAX_EDGE = 2048
COMMON_DIMS = {
    "Base portrait - 2:3": (1024, 1536),
    "Stable portrait - 3:4": (1088, 1440),
    "Tall mobile - 9:16": (928, 1664),
    "Base landscape - 3:2": (1536, 1024),
    "Balanced landscape - 4:3": (1440, 1088),
    "Widescreen - 16:9": (1664, 928),
    "Square format - 1:1": (1248, 1248),
}


logger = logging.getLogger(__name__)


def _replace_with_retry(source, destination):
    """Allow Windows scanners/readers time to release either file."""
    deadline = time.monotonic() + 30.0
    delay = 0.05
    warned = False
    while True:
        try:
            source.replace(destination)
            return
        except OSError as error:
            # winerror distinguishes Windows locks/access errors from ordinary
            # permission failures on other platforms. Permanent Windows access
            # failures still propagate once the bounded wait expires.
            if getattr(error, "winerror", None) not in {5, 32, 33}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            if not warned:
                logger.warning(
                    "Waiting up to 30s to save %s: Windows denied the rename; "
                    "another process may have the file open", destination)
                warned = True
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 1.0)


def _atomic_write(path, data, mode, encoding=None, durable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode=mode, encoding=encoding, dir=path.parent,
                prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        _replace_with_retry(temporary, path)
        temporary = None
        if durable:
            try:
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A scanner can also lock the temporary file against deletion.
                # Keep the original write/rename error visible to the caller.
                logger.warning("Could not remove temporary file %s", temporary,
                               exc_info=True)


def atomic_write_text(path, text, durable=False):
    _atomic_write(
        path, text, mode="w", encoding="utf-8", durable=durable)


def atomic_write_bytes(path, data):
    _atomic_write(path, data, mode="wb")


def _thumbnail_media_root_key(media_root):
    media_root = Path(media_root).resolve()
    label = "".join(
        char if char.isascii() and (char.isalnum() or char in "-_") else "-"
        for char in media_root.name
    ).strip("-_")[:48] or "media"
    canonical = os.path.normcase(str(media_root)).encode(
        errors="surrogateescape")
    return f"{label}-{hashlib.sha256(canonical).hexdigest()[:16]}"


def thumbnail_cache_path(
        cache_root, media_root, relative, namespace="output",
        fingerprint=None):
    relative = Path(relative)
    if (relative.is_absolute() or ".." in relative.parts
            or relative.suffix.lower() not in IMAGE_EXTS):
        raise ValueError(f"unsafe thumbnail path: {relative}")
    if namespace not in {"input", "output"}:
        raise ValueError(f"unsafe thumbnail namespace: {namespace}")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 24
            or any(char not in "0123456789abcdef" for char in fingerprint)):
        raise ValueError(f"unsafe thumbnail fingerprint: {fingerprint}")
    cache_root = Path(cache_root)
    if cache_root.is_symlink():
        raise ValueError(f"unsafe thumbnail cache root: {cache_root}")
    cache_root = cache_root.resolve()
    destination = (cache_root / namespace
                   / _thumbnail_media_root_key(media_root)
                   / relative.parent
                   / f"{relative.name}.{fingerprint}.webp")
    parent = destination.parent
    while parent != cache_root:
        if parent.is_symlink():
            raise ValueError(f"unsafe thumbnail cache directory: {parent}")
        parent = parent.parent
    return destination


def thumbnail_namespace_root(cache_root, media_root, namespace="output"):
    """Return the directory that holds versioned WebPs for one media root."""
    if namespace not in {"input", "output"}:
        raise ValueError(f"unsafe thumbnail namespace: {namespace}")
    cache_root = Path(cache_root)
    if cache_root.is_symlink():
        raise ValueError(f"unsafe thumbnail cache root: {cache_root}")
    return (
        cache_root.resolve() / namespace
        / _thumbnail_media_root_key(media_root)
    )


def parse_thumbnail_name(name):
    """Split `<source name>.<24 hex fingerprint>.webp`; None for other files."""
    if not name.endswith(".webp"):
        return None
    original, separator, fingerprint = name[:-5].rpartition(".")
    if (separator != "." or not original or len(fingerprint) != 24
            or any(char not in "0123456789abcdef" for char in fingerprint)):
        return None
    return original, fingerprint


def thumbnail_fingerprint_map(cache_root, media_root, namespace="output"):
    """Map relative posix paths to fingerprints by walking cached WebPs.

    Listing can version gallery URLs from this map instead of lstat'ing every
    source JPEG. If several fingerprints exist for one path, the newest mtime
    wins.
    """
    try:
        root = thumbnail_namespace_root(cache_root, media_root, namespace)
    except ValueError:
        return {}
    if not root.is_dir():
        return {}
    found = {}
    for dir_path, dir_names, file_names in os.walk(root, followlinks=False):
        dir_names[:] = [
            name for name in dir_names if not name.startswith(".")]
        parent = Path(dir_path)
        try:
            relative_parent = parent.relative_to(root)
        except ValueError:
            continue
        for name in file_names:
            parsed = parse_thumbnail_name(name)
            if parsed is None:
                continue
            original, fingerprint = parsed
            relative = (
                original if not relative_parent.parts
                else (relative_parent / original).as_posix())
            path = parent / name
            try:
                mtime_ns = path.lstat().st_mtime_ns
            except OSError:
                continue
            previous = found.get(relative)
            if previous is None or mtime_ns >= previous[1]:
                found[relative] = (fingerprint, mtime_ns)
    return {relative: fingerprint for relative, (fingerprint, _) in found.items()}


@dataclass
class PruneStats:
    """What a prune removed (or would remove, on a dry run)."""
    files: int = 0
    bytes: int = 0
    dirs: int = 0
    skipped: list = field(default_factory=list)

    def merge(self, other):
        self.files += other.files
        self.bytes += other.bytes
        self.dirs += other.dirs
        self.skipped += other.skipped


def _media_root_is_populated(media_root):
    try:
        with os.scandir(media_root) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def prune_thumbnail_root(cache_root, media_root, namespace="output",
                         dry_run=True, tick=None):
    """Remove cached WebPs for one media root that no longer match a source.

    A thumbnail is live only if its source is still a regular file whose
    current fingerprint equals the one in the cache name, so deleted and
    re-rendered images are both dropped. A missing or empty media root (an
    unmounted drive) is skipped rather than treated as "everything deleted".
    Files that are not versioned WebPs, such as in-flight temp files, are left
    alone. `tick`, if given, is called once per cache directory so a server
    can yield to request threads.
    """
    stats = PruneStats()
    try:
        root = thumbnail_namespace_root(cache_root, media_root, namespace)
    except ValueError:
        return stats
    if not root.is_dir():
        return stats
    media_root = Path(media_root)
    if not _media_root_is_populated(media_root):
        stats.skipped.append(f"{namespace}: {media_root} missing or empty")
        return stats
    for dir_path, _dir_names, file_names in os.walk(
            root, topdown=False, followlinks=False):
        if tick is not None:
            tick()
        parent = Path(dir_path)
        relative_parent = parent.relative_to(root)
        remaining = False
        for name in file_names:
            parsed = parse_thumbnail_name(name)
            path = parent / name
            if parsed is not None:
                original, fingerprint = parsed
                relative = relative_parent / original
                if thumbnail_source_fingerprint(
                        media_root / relative, relative) != fingerprint:
                    try:
                        size = path.lstat().st_size
                        if not dry_run:
                            path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        remaining = True
                        continue
                    stats.files += 1
                    stats.bytes += size
                    continue
            remaining = True
        if not remaining and parent != root and not dry_run:
            try:
                parent.rmdir()
                stats.dirs += 1
            except OSError:
                pass
    return stats


def remove_thumbnail_root(cache_root, media_root, namespace="output"):
    """Delete every cached thumbnail for a media root that is no longer served."""
    try:
        root = thumbnail_namespace_root(cache_root, media_root, namespace)
    except ValueError:
        return
    if root.is_dir() and not root.is_symlink():
        shutil.rmtree(root, ignore_errors=True)


def prune_thumbnail_cache(cache_root, roots, dry_run=True,
                          remove_unknown_roots=True, tick=None):
    """Prune the whole thumbnail cache against the media roots still in use.

    `roots` maps namespace ("input"/"output") to an iterable of media roots.
    Per-root directories that no listed root maps to belong to roots that are
    gone (their key is a hash, so the path cannot be recovered) and are removed
    outright unless `remove_unknown_roots` is false.
    """
    stats = PruneStats()
    for namespace, media_roots in roots.items():
        known = set()
        for media_root in media_roots:
            known.add(_thumbnail_media_root_key(media_root))
            stats.merge(prune_thumbnail_root(
                cache_root, media_root, namespace, dry_run, tick))
        if not remove_unknown_roots:
            continue
        try:
            namespace_dir = thumbnail_namespace_root(
                cache_root, ".", namespace).parent
            children = sorted(namespace_dir.iterdir())
        except (OSError, ValueError):
            continue
        for child in children:
            if child.name in known or child.is_symlink() or not child.is_dir():
                continue
            for dir_path, _dirs, file_names in os.walk(child):
                for name in file_names:
                    if parse_thumbnail_name(name) is None:
                        continue
                    stats.files += 1
                    try:
                        stats.bytes += (Path(dir_path) / name).lstat().st_size
                    except OSError:
                        pass
            stats.dirs += 1
            if not dry_run:
                shutil.rmtree(child, ignore_errors=True)
    return stats


def _source_snapshot(source, relative):
    source = Path(source)
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    try:
        source_stat = source.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(source_stat.st_mode):
        return None
    fields = (
        relative.as_posix(),
        source_stat.st_size,
        getattr(
            source_stat, "st_mtime_ns",
            int(source_stat.st_mtime * 1_000_000_000)),
        getattr(
            source_stat, "st_ctime_ns",
            int(source_stat.st_ctime * 1_000_000_000)),
        getattr(source_stat, "st_dev", 0),
        getattr(source_stat, "st_ino", 0),
    )
    encoded = "\0".join(str(field) for field in fields).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def thumbnail_source_fingerprint(source, relative):
    """Return a cheap source identity suitable for cache validation and URLs."""
    return _source_snapshot(source, relative)


@lru_cache(maxsize=16384)
def _cached_thumbnail_is_readable(path_string, mtime_ns, size, inode):
    """Return whether a cached WebP looks servable without decoding it.

    PIL verify() holds the GIL and serializes ThreadingHTTPServer thumbnail
    hits, so a jump down the gallery appears to load images one by one.
    Non-WebP corrupt files still miss and regenerate.
    """
    if size < 12:
        return False
    try:
        with open(path_string, "rb") as handle:
            header = handle.read(12)
    except OSError:
        return False
    return header.startswith(b"RIFF") and header[8:12] == b"WEBP"


def read_cached_thumbnail_bytes(destination):
    """Return WebP bytes for an immutable cache file, or None if unusable.

    One lstat + one read. Used by the gallery hot path so a hit does not
    also stat the source JPEG or reopen the thumbnail for a header check.
    """
    destination = Path(destination)
    try:
        info = destination.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size < 12:
            return None
        data = destination.read_bytes()
    except OSError:
        return None
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return data
    return None


def _thumbnail_bytes(source, max_edge, *, fast=False):
    from PIL import Image, ImageOps

    with Image.open(source) as raw:
        if fast:
            try:
                raw.draft("RGB", (max_edge, max_edge))
            except (OSError, ValueError):
                pass
        image = ImageOps.exif_transpose(raw)
        image.thumbnail(
            (max_edge, max_edge),
            Image.Resampling.BILINEAR if fast else Image.Resampling.LANCZOS)
        if image.mode in {"RGBA", "LA", "P"}:
            image = image.convert("RGBA")
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        output = io.BytesIO()
        image.save(
            output, format="WEBP", quality=THUMBNAIL_QUALITY,
            method=4 if fast else 6)
    return output.getvalue()


def ensure_thumbnail(source, destination, relative=None,
                     max_edge=THUMBNAIL_MAX_EDGE, fingerprint=None,
                     fast=False):
    """Return an immutable versioned thumbnail, generating it atomically."""
    source = Path(source)
    destination = Path(destination)
    relative = Path(relative) if relative is not None else Path(source.name)
    current_fingerprint = _source_snapshot(source, relative)
    fingerprint = fingerprint or current_fingerprint
    if current_fingerprint is None or fingerprint != current_fingerprint:
        return None
    try:
        cached_stat = destination.lstat()
        if (stat.S_ISREG(cached_stat.st_mode)
                and cached_stat.st_size
                and _cached_thumbnail_is_readable(
                    str(destination), cached_stat.st_mtime_ns,
                    cached_stat.st_size, cached_stat.st_ino)):
            return destination
    except OSError:
        pass

    try:
        thumbnail = _thumbnail_bytes(source, max_edge, fast=fast)
    except (OSError, ValueError):
        return None
    if _source_snapshot(source, relative) != fingerprint:
        return None
    try:
        atomic_write_bytes(destination, thumbnail)
    except OSError:
        return None
    if _source_snapshot(source, relative) != fingerprint:
        return None
    return destination


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_images(root):
    images = []
    seen_directories = set()
    for directory, child_dirs, filenames in os.walk(root, followlinks=True):
        stat = Path(directory).stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity in seen_directories:
            child_dirs.clear()
            continue
        seen_directories.add(identity)
        child_dirs.sort()
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
                images.append(path)
    yield from sorted(images, key=lambda path: path.relative_to(root).as_posix())


def derive_dims(image_path):
    from PIL import Image
    with Image.open(image_path) as image:
        width, height = image.size
    scale = (TARGET_PIXELS / float(width * height)) ** 0.5
    scale = min(scale, MAX_EDGE / float(max(width, height)))

    def round64(value):
        return max(64, int(round(value / 64.0)) * 64)

    return round64(width * scale), round64(height * scale)


def select_common_dims(width, height):
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    source_ratio = width / height
    return min(
        COMMON_DIMS.values(),
        key=lambda dims: abs(source_ratio - dims[0] / dims[1]))


def common_target_dims(source):
    """Target size prepare_common_image would pick, from the header only."""
    from PIL import Image

    with Image.open(source) as image:
        width, height = image.size
        if image.getexif().get(0x0112) in {5, 6, 7, 8}:
            width, height = height, width
    return select_common_dims(width, height)


def prepare_common_image(source, destination):
    from PIL import Image, ImageOps

    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw)
        target = select_common_dims(*image.size)
        image = ImageOps.fit(
            image, target, method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5))
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.save(destination, format="JPEG", quality=95, subsampling=0)
    return target


def host_name(path):
    return path.as_posix().replace("/", "__").rsplit(".", 1)[0]
