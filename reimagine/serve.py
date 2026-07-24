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
    /img/output/NAME/<path>    raw bytes of an output image in source NAME
    /img/input/<path>          raw bytes of a reference image

No third-party deps. Serve, then open the printed URL.
"""
import argparse
import json
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}


def discover_sources(outputs_dir):
    """Return the available output sources as {name: dir} — one per immediate
    subdirectory of outputs_dir that contains at least one image (recursively).
    Sorted by name for a stable UI order. Empty if outputs_dir doesn't exist."""
    sources = {}
    if not outputs_dir.is_dir():
        return sources
    for child in sorted(outputs_dir.iterdir()):
        if not child.is_dir():
            continue
        if any(p.is_file() and p.suffix.lower() in IMAGE_EXTS
               for p in child.rglob("*")):
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


def load_prompts(dir_path):
    """Read a directory's prompts.yaml (filename -> prompt) if present. Tolerant
    of a missing file or malformed YAML — returns {} rather than raising."""
    import yaml
    yaml_path = dir_path / "prompts.yaml"
    if not yaml_path.is_file():
        return {}
    try:
        data = yaml.safe_load(yaml_path.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def list_pairs(source_name, output_dir, input_dir):
    """Walk one source's output dir recursively; return one entry per rendered
    image, grouped by its top-level category (first path segment), each with its
    reference (if any) and the prompt used (from the dir's prompts.yaml)."""
    items = []
    prompt_cache = {}
    if output_dir.is_dir():
        for p in sorted(output_dir.rglob("*")):
            if not (p.is_file() and p.suffix.lower() in IMAGE_EXTS):
                continue
            rel = p.relative_to(output_dir)
            ref = find_reference(rel, input_dir)
            parts = rel.parts
            category = parts[0] if len(parts) > 1 else "(root)"
            if p.parent not in prompt_cache:
                prompt_cache[p.parent] = load_prompts(p.parent)
            src_q = urllib.parse.quote(source_name)
            items.append({
                "name": rel.name,
                "path": rel.as_posix(),
                "category": category,
                "output_url": f"/img/output/{src_q}/" + urllib.parse.quote(rel.as_posix()),
                "input_url": ("/img/input/" + urllib.parse.quote(ref)) if ref else None,
                "prompt": prompt_cache[p.parent].get(rel.name),
            })
    return items


class Handler(BaseHTTPRequestHandler):
    # Injected by main(): the output sources ({name: dir}) and the input/ dir.
    sources = {}
    input_dir = ROOT / "input"

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

    def _send_file(self, base, rel_quoted):
        """Serve a file under `base`, guarding against path traversal."""
        rel = urllib.parse.unquote(rel_quoted)
        target = (base / rel).resolve()
        if base.resolve() not in target.parents or not target.is_file():
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
        if path == "/api/list":
            qs = urllib.parse.parse_qs(parsed.query)
            want = (qs.get("source") or [None])[0]
            name = want if want in self.sources else next(iter(self.sources), None)
            if name is None:
                return self._send_json([])
            return self._send_json(
                list_pairs(name, self.sources[name], self.input_dir))
        if path.startswith("/img/output/"):
            # /img/output/<source>/<path>
            rest = path[len("/img/output/"):]
            src_q, _, rel_q = rest.partition("/")
            src = urllib.parse.unquote(src_q)
            if src not in self.sources:
                return self._send(404, "unknown source")
            return self._send_file(self.sources[src], rel_q)
        if path.startswith("/img/input/"):
            return self._send_file(self.input_dir, path[len("/img/input/"):])
        return self._send(404, "not found")

    do_HEAD = do_GET


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--outputs-dir", type=Path, default=ROOT / "outputs",
                        help="Top-level dir whose immediate subdirectories are "
                             "the selectable output sources (labeled by name).")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Serve a single flat output dir instead of the "
                             "outputs/ tree (labeled by its own name).")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "input")
    args = parser.parse_args()

    if args.output_dir is not None:
        d = args.output_dir.resolve()
        Handler.sources = {d.name: d}
    else:
        Handler.sources = discover_sources(args.outputs_dir.resolve())
    Handler.input_dir = args.input_dir.resolve()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    if Handler.sources:
        print(f"reimagine gallery: {len(Handler.sources)} source(s):")
        for name, d in Handler.sources.items():
            n = sum(1 for p in d.rglob("*")
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
            print(f"    {name:<20} {n} render(s)  ({d})")
    else:
        print("reimagine gallery: no output sources found "
              f"(looked under {args.outputs_dir})")
    print(f"  serving http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
