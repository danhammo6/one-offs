#!/usr/bin/env python3
"""reimagine gallery server.

A tiny stdlib HTTP server for browsing the rendered outputs against their
reference images. Walks the output tree(s) live on every request, so new
renders show up on a page refresh — handy while a batch is still running.

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
import json
import logging
import mimetypes
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reimagine_pipeline import PIPELINE_FILENAME
from reimagine_pipeline.manifest import (
    load_pipeline_tree, load_pipeline_tree_input_dir,
)
from reimagine_pipeline.prompting import regions_to_text

logger = logging.getLogger(__name__)


class HelpFormatter(
        argparse.ArgumentDefaultsHelpFormatter,
        argparse.RawDescriptionHelpFormatter):
    pass

ROOT = Path(__file__).parent.resolve()
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}
# Video extensions render_media.py may write next to a still (same stem). Ordered by
# preference when several exist for one still.
VIDEO_EXTS = (".mp4", ".webm", ".mkv")


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


def load_pipeline_metadata(output_dir):
    """Return the configured input directory and prompt metadata for a source."""
    try:
        configured_input = load_pipeline_tree_input_dir(
            output_dir, PIPELINE_FILENAME)
    except ValueError as error:
        logger.warning("could not load gallery configuration for %s: %s",
                       output_dir, error)
        return None, {}
    input_dir = (ROOT / configured_input).resolve()
    try:
        manifest = load_pipeline_tree(output_dir, filename=PIPELINE_FILENAME)
    except ValueError as error:
        logger.warning("could not load gallery metadata for %s: %s",
                       output_dir, error)
        return input_dir, {}
    metadata = {}
    for item in manifest.items:
        if not item.still:
            continue
        prompt = (item.still.prompt or regions_to_text(item.still.regions))
        metadata[item.still.output.as_posix()] = {
            "prompt": prompt,
            "video_prompt": item.video.prompt if item.video else None,
        }
    return input_dir, metadata


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
        dir_names.sort()
        for name in sorted(file_names):
            path = Path(dir_path) / name
            if path.suffix.lower() in IMAGE_EXTS:
                yield path


def iter_pairs(source_name, output_dir, on_reference=None):
    """Yield gallery records as output images are discovered."""
    input_dir, metadata = load_pipeline_metadata(output_dir)
    if output_dir.is_dir():
        for p in iter_images(output_dir):
            rel = p.relative_to(output_dir)
            ref = find_reference(rel, input_dir) if input_dir else None
            if ref and on_reference:
                on_reference(input_dir, ref)
            parts = rel.parts
            category = parts[0] if len(parts) > 1 else "(root)"
            src_q = urllib.parse.quote(source_name)
            vid = find_sibling_video(p)
            vid_rel = vid.relative_to(output_dir).as_posix() if vid else None
            prompts = metadata.get(rel.as_posix(), {})
            yield {
                "name": rel.name,
                "path": rel.as_posix(),
                "category": category,
                "output_url": f"/img/output/{src_q}/" + urllib.parse.quote(rel.as_posix()),
                "input_url": (f"/img/input/{src_q}/" + urllib.parse.quote(ref))
                             if ref else None,
                "video_url": (f"/img/output/{src_q}/" + urllib.parse.quote(vid_rel))
                             if vid_rel else None,
                "prompt": prompts.get("prompt"),
                "video_prompt": prompts.get("video_prompt") if vid else None,
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
    input_dirs = {}
    allowed_references = {}
    gallery_lock = threading.Lock()

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
                    name, allowed, input_dir, relative))
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
    args = parser.parse_args()

    if args.output_dir is not None:
        d = args.output_dir.resolve()
        Handler.sources = {d.name: d}
    else:
        Handler.sources = discover_sources(args.outputs_dir.resolve())
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
