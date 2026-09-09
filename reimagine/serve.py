#!/usr/bin/env python3
"""reimagine gallery server.

A tiny stdlib HTTP server for browsing the rendered outputs against their
reference images. Media files are walked live on every request, while pipeline
metadata is parsed once at startup and remains fixed for the server lifetime.

    .venv/bin/python serve.py            # serve on http://127.0.0.1:8000
    .venv/bin/python serve.py --port 9000

Multiple output sets live side by side under a single top-level dir (default
`outputs/`); each subdirectory is one selectable "source" in the UI, labeled by
its directory name (e.g. claude, local-llm, local-llm-regions). Point at a
different tree with --outputs-dir, or a single flat dir with --output-dir.

Routes:
    /                          the static gallery page (index.html)
    /api/sources               JSON: the available output sources + the default
    /api/list?source=NAME      JSON: every output image in NAME + its reference
    /api/stream?source=NAME    NDJSON: the same images streamed progressively
    /img/output/NAME/<path>    raw bytes of an output image in source NAME
    /img/input/NAME/<path>     raw bytes of a reference image

Uses the project's PyYAML dependency. Serve, then open the printed URL.
"""
import argparse
import hashlib
import json
import logging
import mimetypes
import os
import stat
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reimagine_pipeline import PIPELINE_FILENAME
from reimagine_pipeline.files import (
    DEFAULT_CACHE_ROOT, IMAGE_EXTS, THUMBNAIL_CACHE_SUBDIR,
    atomic_write_text, ensure_thumbnail, read_cached_thumbnail_bytes,
    thumbnail_cache_path, thumbnail_source_fingerprint,
)
from reimagine_pipeline.manifest import (
    load_pipeline_document, pipeline_paths, validate_pipeline_input_dir,
)
from reimagine_pipeline.prompting import regions_to_text

logger = logging.getLogger(__name__)


class HelpFormatter(
        argparse.ArgumentDefaultsHelpFormatter,
        argparse.RawDescriptionHelpFormatter):
    pass

ROOT = Path(__file__).parent.resolve()
# Video extensions render_media.py may write next to a still (same stem). Ordered by
# preference when several exist for one still.
VIDEO_EXTS = (".mp4", ".webm", ".mkv")
# Bump when the normalized document fields or gallery validation semantics change.
MANIFEST_INDEX_SCHEMA_VERSION = 1
MANIFEST_INDEX_PREFIX = f"manifest-index-v{MANIFEST_INDEX_SCHEMA_VERSION}"


def discover_sources(outputs_dir):
    """Return the available output sources as {name: dir} — one per immediate
    subdirectory of outputs_dir. Images are discovered only after selection so
    a large collection does not delay server startup. Empty if absent."""
    sources = {}
    if not outputs_dir.is_dir():
        return sources
    for child in sorted(outputs_dir.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            sources[child.name] = child
    return sources


def find_reference(rel, input_dir):
    """Given an output path relative to a source dir (e.g. sports/sprint.jpg),
    find the matching reference under input/. Outputs are always .jpg but the
    reference may have a different extension, so match on the stem in the subdir."""
    cand = input_dir / rel
    if cand.exists():
        return rel.as_posix()
    parent = input_dir / rel.parent
    if parent.is_dir():
        for p in sorted(parent.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS and p.stem == rel.stem:
                return p.relative_to(input_dir).as_posix()
    return None


def _manifest_index_key(path):
    resolved = path.resolve()
    try:
        return f"project:{resolved.relative_to(Path(ROOT).resolve()).as_posix()}"
    except ValueError:
        # Output roots may intentionally live outside the project. This JSON is
        # server-local and never exposed over HTTP.
        return f"absolute:{resolved}"


def manifest_index_path(cache_root, sources):
    """Return the stable, non-revealing index path for discovery roots."""
    roots = sorted({
        os.path.normcase(str(Path(output_dir).resolve()))
        for output_dir in sources.values()
    })
    scope = json.dumps({
        "pipeline_filename": PIPELINE_FILENAME,
        "roots": roots,
    }, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(scope).hexdigest()[:24]
    return Path(cache_root) / f"{MANIFEST_INDEX_PREFIX}-{digest}.json"


def _manifest_identity(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"pipeline is not a regular file: {path}")
    return {
        "size": info.st_size,
        "mtime_ns": getattr(
            info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
        "ctime_ns": getattr(
            info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000)),
        "device": getattr(info, "st_dev", 0),
        "inode": getattr(info, "st_ino", 0),
        "mode": stat.S_IMODE(info.st_mode),
    }


def _manifest_item_record(item):
    still_output = prompt = None
    if item.still:
        still_output = item.still.output.as_posix()
        prompt = item.still.prompt or regions_to_text(item.still.regions)
    return {
        "item_id": item.item_id,
        "source_path": item.source_path.as_posix(),
        "still_output": still_output,
        "prompt": prompt,
        "video_prompt": item.video.prompt if item.video else None,
    }


def _parse_manifest_record(path):
    input_dir, manifest = load_pipeline_document(path)
    document = {
        "input_dir": input_dir.as_posix(),
        "manifest": None,
    }
    if manifest is None:
        return {"document": document}
    document["manifest"] = {
        "still_mode": manifest.still_mode,
        "common_dims": manifest.common_dims,
        "items": [_manifest_item_record(item) for item in manifest.items],
    }
    return {"document": document}


def _safe_cache_root(cache_root):
    cache_root = Path(cache_root).expanduser()
    if cache_root.is_symlink():
        raise ValueError(f"unsafe cache root: {cache_root}")
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return cache_root.resolve()


def _read_manifest_index(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("index is not a regular file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict)
                or data.get("schema_version")
                != MANIFEST_INDEX_SCHEMA_VERSION
                or not isinstance(data.get("manifests"), dict)):
            raise ValueError("incompatible schema")
        return data["manifests"], True
    except FileNotFoundError:
        logger.info("gallery manifest index: not found; rebuilding")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        logger.warning("gallery manifest index: %s; rebuilding", error)
    return {}, False


def _cached_manifest_record(record, identity):
    if not isinstance(record, dict) or record.get("identity") != identity:
        return None
    if isinstance(record.get("error"), str):
        return record
    document = record.get("document")
    if not isinstance(document, dict):
        return None
    try:
        validate_pipeline_input_dir(document.get("input_dir"))
    except ValueError:
        return None
    manifest = document.get("manifest")
    if manifest is None:
        return record
    if (not isinstance(manifest, dict)
            or manifest.get("still_mode") not in {"manual", "regions"}
            or not isinstance(manifest.get("common_dims"), bool)
            or not isinstance(manifest.get("items"), list)):
        return None
    required = {
        "item_id", "source_path", "still_output", "prompt", "video_prompt"}
    for item in manifest["items"]:
        if not isinstance(item, dict) or set(item) != required:
            return None
        if (not isinstance(item["item_id"], str)
                or not isinstance(item["source_path"], str)
                or (item["still_output"] is not None
                    and not isinstance(item["still_output"], str))
                or (item["prompt"] is not None
                    and not isinstance(item["prompt"], str))
                or (item["video_prompt"] is not None
                    and not isinstance(item["video_prompt"], str))):
            return None
        for value in (
                item["item_id"], item["source_path"], item["still_output"]):
            if value is None:
                continue
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                return None
    return record


def _atomic_write_manifest_index(path, payload):
    serialized = json.dumps(
        payload, sort_keys=True, separators=(",", ":")) + "\n"
    atomic_write_text(path, serialized, durable=True)


def _load_source_metadata(output_dir, records):
    """Aggregate normalized manifest records with the legacy validation rules."""
    paths = [path for path, _ in records]
    if not paths:
        logger.warning("could not load gallery metadata for %s: no %s files",
                       output_dir, PIPELINE_FILENAME)
        return (ROOT / "input").resolve(), {}
    try:
        documents = []
        for path, record in records:
            if "error" in record:
                raise ValueError(record["error"])
            document = record["document"]
            documents.append((
                path, Path(document["input_dir"]), document["manifest"]))
        input_dirs = {input_dir for _, input_dir, _ in documents}
        if len(input_dirs) != 1:
            raise ValueError("pipeline files use different input directories")
    except (KeyError, TypeError, ValueError) as error:
        logger.warning("could not cache gallery metadata for %s: %s",
                       output_dir, error)
        return None, {}

    input_dir = (ROOT / input_dirs.pop()).resolve()
    manifest_documents = [
        (path, manifest)
        for path, _, manifest in documents
        if manifest is not None
    ]
    if not manifest_documents:
        return input_dir, {}
    legacy = output_dir / PIPELINE_FILENAME
    if (legacy in {path for path, _ in manifest_documents}
            and len(manifest_documents) > 1):
        root_manifest = next(
            manifest for path, manifest in manifest_documents
            if path == legacy)
        if any(
                Path(item["source_path"]).parent.parts
                for item in root_manifest["items"]):
            manifest_documents = [(legacy, root_manifest)]
    modes = {
        manifest["still_mode"] for _, manifest in manifest_documents
    }
    common_dims = {
        manifest["common_dims"] for _, manifest in manifest_documents
    }
    if len(modes) != 1 or len(common_dims) != 1:
        logger.warning("could not cache gallery metadata for %s: "
                       "pipeline files use inconsistent modes", output_dir)
        return None, {}

    metadata = {}
    item_ids = set()
    source_paths = set()
    for path, manifest in manifest_documents:
        parent = path.parent.relative_to(output_dir)
        for item in manifest["items"]:
            item_id = (parent / item["item_id"]).as_posix()
            source_path = (parent / item["source_path"]).as_posix()
            if item_id in item_ids or source_path in source_paths:
                logger.warning("could not cache gallery metadata for %s: "
                               "duplicate IDs or source paths", output_dir)
                return None, {}
            item_ids.add(item_id)
            source_paths.add(source_path)
            if not item["still_output"]:
                continue
            output = (parent / item["still_output"]).as_posix()
            if output in metadata:
                logger.warning("could not cache gallery metadata for %s: "
                               "duplicate still output %s", output_dir, output)
                return None, {}
            metadata[output] = {
                "prompt": item["prompt"],
                "video_prompt": item["video_prompt"],
            }
    return input_dir, metadata


def load_pipeline_metadata(output_dir):
    """Parse each pipeline once and return its input directory and prompts."""
    records = []
    for path in pipeline_paths(output_dir, PIPELINE_FILENAME):
        try:
            record = _parse_manifest_record(path)
        except ValueError as error:
            record = {"error": str(error)}
        records.append((path, record))
    return _load_source_metadata(output_dir, records)


def build_pipeline_metadata_cache(sources, cache_root=None):
    """Build metadata for every source before the server begins listening."""
    started = time.perf_counter()
    if cache_root is None:
        cache = {
            name: load_pipeline_metadata(output_dir)
            for name, output_dir in sources.items()
        }
        logger.info("gallery metadata cache: %d source(s) initialized in %.2fs",
                    len(cache), time.perf_counter() - started)
        return cache

    try:
        cache_root = _safe_cache_root(cache_root)
        index_path = manifest_index_path(cache_root, sources)
        previous, valid_index = _read_manifest_index(index_path)
    except (OSError, ValueError) as error:
        logger.warning("gallery manifest index unavailable: %s", error)
        previous, valid_index = {}, False
        cache_root = None
        index_path = None

    current = {}
    source_records = {}
    parsed = unchanged = 0
    for name, output_dir in sources.items():
        records = []
        for path in pipeline_paths(output_dir, PIPELINE_FILENAME):
            key = _manifest_index_key(path)
            try:
                identity = _manifest_identity(path)
                cached = _cached_manifest_record(previous.get(key), identity)
                if cached is not None:
                    record = cached
                    unchanged += 1
                else:
                    parsed += 1
                    try:
                        record = _parse_manifest_record(path)
                    except ValueError as error:
                        record = {"error": str(error)}
                    record["identity"] = identity
                current[key] = record
                records.append((path, record))
            except (OSError, ValueError) as error:
                records.append((path, {"error": str(error)}))
        source_records[name] = records

    deleted = len(set(previous) - set(current))
    cache = {
        name: _load_source_metadata(sources[name], source_records[name])
        for name in sources
    }
    if index_path is not None and (not valid_index or current != previous):
        payload = {
            "schema_version": MANIFEST_INDEX_SCHEMA_VERSION,
            "manifests": current,
        }
        try:
            _atomic_write_manifest_index(index_path, payload)
        except OSError as error:
            logger.warning("could not update gallery manifest index: %s", error)
    logger.info(
        "gallery manifest index: %d parsed, %d unchanged, %d deleted in %.2fs",
        parsed, unchanged, deleted, time.perf_counter() - started)
    logger.info("gallery metadata cache: %d source(s) initialized in %.2fs",
                len(cache), time.perf_counter() - started)
    return cache


def find_sibling_video(still_path):
    """Return the Path of a video sitting next to a still (same stem), or None.
    render_media.py writes e.g. cat-pounce.mp4 beside cat-pounce.jpg."""
    for ext in VIDEO_EXTS:
        cand = still_path.with_suffix(ext)
        if cand.is_file():
            return cand
    return None


def iter_images(output_dir):
    """Yield image paths in stable order without materializing the whole tree."""
    for dir_path, dir_names, file_names in os.walk(output_dir):
        dir_names[:] = sorted(
            name for name in dir_names if not name.startswith("."))
        for name in sorted(file_names):
            path = Path(dir_path) / name
            if path.suffix.lower() in IMAGE_EXTS:
                yield path


def versioned_media_url(url, path, relative):
    fingerprint = thumbnail_source_fingerprint(path, relative)
    return f"{url}?v={fingerprint}" if fingerprint else url


def media_url(route, source_name, relative):
    return (
        f"/img/{route}/{urllib.parse.quote(source_name)}/"
        f"{urllib.parse.quote(Path(relative).as_posix())}"
    )


def iter_pairs(source_name, output_dir, on_reference=None,
               pipeline_metadata=None):
    """Yield gallery records as output images are discovered."""
    input_dir, _ = (
        pipeline_metadata
        if pipeline_metadata is not None
        else load_pipeline_metadata(output_dir)
    )
    if output_dir.is_dir():
        for p in iter_images(output_dir):
            rel = p.relative_to(output_dir)
            ref = find_reference(rel, input_dir) if input_dir else None
            if ref and on_reference:
                on_reference(input_dir, ref)
            parts = rel.parts
            category = parts[0] if len(parts) > 1 else "(root)"
            vid = find_sibling_video(p)
            vid_rel = vid.relative_to(output_dir).as_posix() if vid else None
            output_url = media_url("output", source_name, rel)
            input_url = media_url("input", source_name, ref) if ref else None
            yield {
                "name": rel.name,
                "path": rel.as_posix(),
                "category": category,
                "output_url": output_url,
                "output_thumbnail_url": versioned_media_url(
                    media_url("thumbnail/output", source_name, rel), p, rel),
                "input_url": input_url,
                "input_thumbnail_url": versioned_media_url(
                    media_url("thumbnail/input", source_name, ref),
                    input_dir / ref, ref)
                    if input_dir and ref else None,
                "video_url": (
                    media_url("output", source_name, vid_rel)
                    if vid_rel else None),
            }


def list_pairs(source_name, output_dir):
    """Return all records for compatibility with non-streaming clients."""
    return list(iter_pairs(source_name, output_dir))


def reference_paths(output_dir, input_dir):
    """Return references that correspond to discoverable output images."""
    return {
        reference
        for path in iter_images(output_dir)
        for reference in [find_reference(path.relative_to(output_dir), input_dir)]
        if reference
    }


def safe_media_path(base, rel, extensions, allowed_paths=None,
                    allow_linked_dirs=False):
    """Resolve an allowed media path without permitting file-symlink spoofing."""
    rel_path = Path(rel)
    if (rel_path.is_absolute() or ".." in rel_path.parts
            or rel_path.suffix.lower() not in extensions
            or (allowed_paths is not None
                and rel_path.as_posix() not in allowed_paths)):
        return None
    target = base / rel_path
    if target.is_symlink() or not target.is_file():
        return None
    resolved = target.resolve()
    if (not allow_linked_dirs and base.resolve() not in resolved.parents):
        return None
    return target


class Handler(BaseHTTPRequestHandler):
    # Injected by main(): the output sources ({name: dir}).
    sources = {}
    pipeline_metadata_cache = {}
    input_dirs = {}
    allowed_references = {}
    gallery_lock = threading.Lock()
    thumbnail_locks = tuple(threading.Lock() for _ in range(32))
    thumbnail_generate = threading.Semaphore(2)
    thumbnail_memory_cache = OrderedDict()
    thumbnail_memory_lock = threading.Lock()
    THUMBNAIL_MEMORY_MAX = 256
    cache_root = DEFAULT_CACHE_ROOT
    thumbnail_cache_root = DEFAULT_CACHE_ROOT / THUMBNAIL_CACHE_SUBDIR

    def log_message(self, *_):  # keep the console quiet
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj):
        self._send(200, json.dumps(obj), "application/json",
                   extra={"Cache-Control": "no-cache"})

    def _send_ndjson(self, items):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            for item in items:
                self.wfile.write(json.dumps(item).encode("utf-8") + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    @classmethod
    def _begin_gallery_load(cls, source):
        allowed = set()
        with cls.gallery_lock:
            cls.input_dirs.pop(source, None)
            cls.allowed_references[source] = allowed
        return allowed

    @classmethod
    def _register_reference(cls, source, allowed, input_dir, relative):
        with cls.gallery_lock:
            if cls.allowed_references.get(source) is not allowed:
                return
            previous = cls.input_dirs.setdefault(source, input_dir)
            if previous == input_dir:
                allowed.add(relative)

    @classmethod
    def _reference_access(cls, source):
        with cls.gallery_lock:
            return (cls.input_dirs.get(source),
                    frozenset(cls.allowed_references.get(source, ())))

    def _send_file(self, base, rel_quoted, extensions, allowed_paths=None,
                   allow_linked_dirs=False):
        """Serve an allowed media file under base, guarding path traversal."""
        rel = urllib.parse.unquote(rel_quoted)
        target = safe_media_path(
            base, rel, extensions, allowed_paths, allow_linked_dirs)
        if target is None:
            return self._send(404, "not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype,
                   extra={"Cache-Control": "no-cache"})

    def _thumbnail_rel_allowed(self, rel, allowed_paths):
        rel_path = Path(rel)
        return not (
            rel_path.is_absolute() or ".." in rel_path.parts
            or rel_path.suffix.lower() not in IMAGE_EXTS
            or (allowed_paths is not None
                and rel_path.as_posix() not in allowed_paths))

    def _cached_thumbnail_bytes(self, destination):
        key = str(destination)
        with self.thumbnail_memory_lock:
            cached = self.thumbnail_memory_cache.get(key)
            if cached is not None:
                self.thumbnail_memory_cache.move_to_end(key)
                return cached
        data = read_cached_thumbnail_bytes(destination)
        if data is None:
            return None
        with self.thumbnail_memory_lock:
            self.thumbnail_memory_cache[key] = data
            self.thumbnail_memory_cache.move_to_end(key)
            while len(self.thumbnail_memory_cache) > self.THUMBNAIL_MEMORY_MAX:
                self.thumbnail_memory_cache.popitem(last=False)
        return data

    def _send_thumbnail(self, source_base, rel_quoted, namespace,
                        expected_fingerprint=None, allowed_paths=None,
                        allow_linked_dirs=False):
        rel = urllib.parse.unquote(rel_quoted)
        if not self._thumbnail_rel_allowed(rel, allowed_paths):
            return self._send(404, "not found")
        cache_control = (
            "public, max-age=31536000, immutable"
            if expected_fingerprint else "no-cache")
        if expected_fingerprint:
            try:
                destination = thumbnail_cache_path(
                    self.thumbnail_cache_root, source_base, rel,
                    namespace=namespace,
                    fingerprint=expected_fingerprint)
            except ValueError:
                return self._send(404, "not found")
            cached = self._cached_thumbnail_bytes(destination)
            if cached is not None:
                return self._send(
                    200, cached, "image/webp",
                    extra={"Cache-Control": cache_control})
        source = safe_media_path(
            source_base, rel, IMAGE_EXTS, allowed_paths, allow_linked_dirs)
        if source is None:
            return self._send(404, "not found")
        fingerprint = thumbnail_source_fingerprint(source, rel)
        if expected_fingerprint and expected_fingerprint != fingerprint:
            return self._send(
                409, "source changed; refresh gallery",
                extra={"Cache-Control": "no-store"})
        try:
            destination = thumbnail_cache_path(
                self.thumbnail_cache_root, source_base, rel,
                namespace=namespace,
                fingerprint=fingerprint)
        except ValueError:
            return self._send(404, "not found")
        lock = self.thumbnail_locks[hash(destination) % len(
            self.thumbnail_locks)]
        with self.thumbnail_generate:
            with lock:
                cached = self._cached_thumbnail_bytes(destination)
                if cached is not None:
                    return self._send(
                        200, cached, "image/webp",
                        extra={"Cache-Control": cache_control})
                thumbnail = ensure_thumbnail(
                    source, destination, relative=rel,
                    fingerprint=fingerprint, fast=True)
        if thumbnail is None:
            return self._send(404, "thumbnail unavailable")
        if thumbnail_source_fingerprint(source, rel) != fingerprint:
            return self._send(
                409, "source changed; refresh gallery",
                extra={"Cache-Control": "no-store"})
        try:
            thumbnail_bytes = thumbnail.read_bytes()
        except OSError:
            latest_fingerprint = thumbnail_source_fingerprint(source, rel)
            if (expected_fingerprint
                    and expected_fingerprint != latest_fingerprint):
                return self._send(
                    409, "source changed; refresh gallery",
                    extra={"Cache-Control": "no-store"})
            return self._send(
                404, "thumbnail unavailable",
                extra={"Cache-Control": "no-store"})
        if (len(thumbnail_bytes) >= 12
                and thumbnail_bytes.startswith(b"RIFF")
                and thumbnail_bytes[8:12] == b"WEBP"):
            with self.thumbnail_memory_lock:
                self.thumbnail_memory_cache[str(destination)] = thumbnail_bytes
                self.thumbnail_memory_cache.move_to_end(str(destination))
                while len(self.thumbnail_memory_cache) > self.THUMBNAIL_MEMORY_MAX:
                    self.thumbnail_memory_cache.popitem(last=False)
        self._send(
            200, thumbnail_bytes, "image/webp",
            extra={"Cache-Control": cache_control})

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            index = ROOT / "index.html"
            if not index.is_file():
                return self._send(500, "index.html missing")
            return self._send(200, index.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/sources":
            names = list(self.sources.keys())
            return self._send_json({"sources": names,
                                    "default": names[0] if names else None})
        if path == "/api/metadata":
            qs = urllib.parse.parse_qs(parsed.query)
            source = (qs.get("source") or [None])[0]
            relative = (qs.get("path") or [None])[0]
            if source not in self.sources or not relative:
                return self._send(404, "metadata not found")
            rel_path = Path(relative)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                return self._send(404, "metadata not found")
            metadata = self.pipeline_metadata_cache.get(
                source, (None, {}))[1].get(rel_path.as_posix())
            if metadata is None:
                return self._send(404, "metadata not found")
            return self._send_json(metadata)
        if path in {"/api/list", "/api/stream"}:
            qs = urllib.parse.parse_qs(parsed.query)
            want = (qs.get("source") or [None])[0]
            name = want if want in self.sources else next(iter(self.sources), None)
            if self.command == "HEAD":
                return (self._send_ndjson(())
                        if path == "/api/stream" else self._send_json([]))
            if name is None:
                return (self._send_ndjson(())
                        if path == "/api/stream" else self._send_json([]))
            allowed = self._begin_gallery_load(name)
            items = iter_pairs(
                name, self.sources[name],
                lambda input_dir, relative: self._register_reference(
                    name, allowed, input_dir, relative),
                self.pipeline_metadata_cache.get(name, (None, {})))
            return (self._send_ndjson(items)
                    if path == "/api/stream" else self._send_json(list(items)))
        if path.startswith("/img/output/"):
            # /img/output/<source>/<path>
            rest = path[len("/img/output/"):]
            src_q, _, rel_q = rest.partition("/")
            src = urllib.parse.unquote(src_q)
            if src not in self.sources:
                return self._send(404, "unknown source")
            return self._send_file(
                self.sources[src], rel_q, IMAGE_EXTS | set(VIDEO_EXTS))
        if path.startswith("/img/input/"):
            rest = path[len("/img/input/"):]
            src_q, _, rel_q = rest.partition("/")
            src = urllib.parse.unquote(src_q)
            if src not in self.sources:
                return self._send(404, "unknown source")
            input_dir, allowed = self._reference_access(src)
            if input_dir is None:
                return self._send(404, "reference not listed")
            return self._send_file(
                input_dir, rel_q, IMAGE_EXTS, allowed_paths=allowed,
                allow_linked_dirs=True)
        for namespace in ("output", "input"):
            prefix = f"/img/thumbnail/{namespace}/"
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            src_q, _, rel_q = rest.partition("/")
            src = urllib.parse.unquote(src_q)
            if src not in self.sources:
                return self._send(404, "unknown source")
            output_dir = self.sources[src]
            expected_fingerprint = (
                urllib.parse.parse_qs(parsed.query).get("v") or [None])[0]
            if namespace == "output":
                return self._send_thumbnail(
                    output_dir, rel_q, "output",
                    expected_fingerprint=expected_fingerprint)
            input_dir, allowed = self._reference_access(src)
            if input_dir is None:
                return self._send(404, "reference not listed")
            return self._send_thumbnail(
                input_dir, rel_q, "input",
                expected_fingerprint=expected_fingerprint,
                allowed_paths=allowed, allow_linked_dirs=True)
        return self._send(404, "not found")

    do_HEAD = do_GET


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=HelpFormatter)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface on which to listen.")
    parser.add_argument("--port", type=int, default=8000,
                        help="TCP port on which to listen.")
    parser.add_argument("--outputs-dir", type=Path, default=ROOT / "outputs",
                        help="Top-level dir whose immediate subdirectories are "
                             "the selectable output sources (labeled by name).")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Serve a single flat output dir instead of the "
                             "outputs/ tree (labeled by its own name).")
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--cache-root", type=Path, default=DEFAULT_CACHE_ROOT,
        help="Disposable manifest-index and thumbnail cache directory.")
    cache_group.add_argument(
        "--thumbnail-cache-root", type=Path, default=None,
        help="Deprecated alias for --cache-root; now names the unified cache.")
    args = parser.parse_args()

    if args.output_dir is not None:
        d = args.output_dir.resolve()
        Handler.sources = {d.name: d}
    else:
        Handler.sources = discover_sources(args.outputs_dir.resolve())
    if args.thumbnail_cache_root is not None:
        logger.warning(
            "--thumbnail-cache-root is deprecated; use --cache-root")
        args.cache_root = args.thumbnail_cache_root
    Handler.cache_root = args.cache_root.expanduser()
    Handler.thumbnail_cache_root = (
        Handler.cache_root / THUMBNAIL_CACHE_SUBDIR)
    Handler.pipeline_metadata_cache = build_pipeline_metadata_cache(
        Handler.sources, Handler.cache_root)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    if Handler.sources:
        logger.info("reimagine gallery: %d source(s):", len(Handler.sources))
        for name, d in Handler.sources.items():
            logger.info("    %-20s %s", name, d)
    else:
        logger.warning("reimagine gallery: no output sources found "
                       "(looked under %s)", args.outputs_dir)
    logger.info("  serving http://%s:%d  (Ctrl-C to stop)", args.host, args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        logger.info("\nbye")


if __name__ == "__main__":
    main()
