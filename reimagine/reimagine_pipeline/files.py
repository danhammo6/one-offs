import hashlib
import os
import tempfile
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}
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


def atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(text)
            handle.flush()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def atomic_write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


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
