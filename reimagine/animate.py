#!/usr/bin/env python3
"""animate — turn a set of reimagine stills into short LTX 2.3 action videos.

Second stage of the pipeline. After `reimagine.py` renders a set of stills into
`outputs/<set>/`, this walks that set and, for each rendered still:

  1. asks a multimodal LLM to look at the still (the video's FIRST FRAME) and
     write a ~10-second action video prompt for the LTX 2.3 i2v model, then
  2. renders the video on the ComfyUI box and writes the resulting .mp4 right
     next to the still (same stem), so the gallery can offer it as a "video"
     view of that render.

    outputs/claude-regions/animals/cat-pounce.jpg
      -> outputs/claude-regions/animals/cat-pounce.mp4

Reuses reimagine.py's ComfyUI client, LLM backends, retrieval + async
sliding-window pipeline, so both the LLM and ComfyUI stay busy.

    .venv/bin/python animate.py --set outputs/claude-regions \
        --samba-root ~/Desktop/MyShare --comfy-server 192.168.33.101:8188

The LTX LoadImage node reads the first frame from ComfyUI's own input/ dir by a
RELATIVE filename. Getting the still onto the host into that dir is a separate
delivery step (WIP); --load-name-template controls the filename LoadImage looks
for (default matches reimagine's host-render convention).
"""
import argparse
import concurrent.futures
import copy
import json
import re
import sys
import time
from pathlib import Path

import reimagine as R

# ----------------------------------------------------------------------------
# LTX 2.3 i2v workflow node IDs — workflows/ltx2-3_comfyui_i2v_aitrepeneur_api.json
# ----------------------------------------------------------------------------
LTX_NODE_PROMPT = "1070"     # CLIPTextEncode (positive prompt text)
LTX_NODE_LOADIMAGE = "1077"  # LoadImage (first frame, relative filename)
LTX_NODE_WIDTH = "1071"      # INTConstant WIDTH
LTX_NODE_HEIGHT = "1069"     # INTConstant HEIGHT
LTX_NODE_DURATION = "1073"   # INTConstant "Audio - Video Duration" (seconds)
LTX_NODE_SEED = "1074"       # RandomNoise.noise_seed
LTX_NODE_SAVER = "1087"      # VHS_VideoCombine (writes the .mp4 on the host)

DEFAULT_LTX_WORKFLOW = Path("workflows/ltx2-3_comfyui_i2v_aitrepeneur_api.json")

# Host staging subfolder for rendered videos (kept distinct from the still
# pipeline's --save-subdir so a concurrent still run never clobbers it).
VIDEO_SUBDIR = "reimagine-video"

VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".gif")

VIDEO_SYSTEM_PROMPT = R._load_prompt("system_video.txt")

VIDEO_RETRY_NUDGE = (
    "\n\nYour previous reply did not contain a usable <video>...</video> "
    "block. Look at the image and output ONLY the video prompt wrapped in "
    "<video> tags."
)


def extract_video_prompt(text):
    """Pull the LAST <video>...</video> block (so any example inside the model's
    reasoning loses to its real final answer). None if missing/too short."""
    matches = re.findall(r"<video>(.*?)</video>", text, re.DOTALL | re.IGNORECASE)
    if not matches:
        return None
    prompt = re.sub(r"\s+", " ", matches[-1]).strip()
    return prompt if len(prompt) >= 20 else None


def video_prompt_for_image(llm, image_path, retries=3):
    """Ask the LLM to write an LTX video prompt for image_path (the first frame);
    retry on unusable output."""
    user = (
        f"Read the still image at this absolute path and write the LTX 2.3 "
        f"action-video prompt:\n{image_path}"
    )
    last = None
    for attempt in range(retries):
        msg = user + (VIDEO_RETRY_NUDGE if attempt else "")
        text = llm.chat(msg, image_path=image_path)
        prompt = extract_video_prompt(text)
        if prompt:
            return prompt
        last = text
    raise RuntimeError(
        f"no <video> after {retries} tries; last reply: {(last or '')[:160]!r}")


def patch_ltx_workflow(base, prompt, load_name, seed, width, height,
                       duration, save_prefix):
    """Fill the LTX i2v workflow: prompt text, first-frame filename, W/H, seed,
    duration, and the video saver's filename prefix. Returns a patched copy."""
    wf = copy.deepcopy(base)
    wf[LTX_NODE_PROMPT]["inputs"]["text"] = prompt
    wf[LTX_NODE_LOADIMAGE]["inputs"]["image"] = load_name
    wf[LTX_NODE_WIDTH]["inputs"]["value"] = width
    wf[LTX_NODE_HEIGHT]["inputs"]["value"] = height
    wf[LTX_NODE_DURATION]["inputs"]["value"] = duration
    wf[LTX_NODE_SEED]["inputs"]["noise_seed"] = seed
    s = wf[LTX_NODE_SAVER]["inputs"]
    # VHS_VideoCombine writes <output>/<prefix>_NNNNN.mp4. A subfolder in the
    # prefix stages our videos apart from anything else on the host.
    s["filename_prefix"] = save_prefix
    s["save_output"] = True
    return wf


def clear_host_videos(samba_root, save_subdir, base):
    """Delete any prior host-side artifact for this base name (via samba), so the
    freshest render is unambiguous. The save subdir is video-only staging, so we
    clear every artifact for the base — the .png first frame and both .mp4s
    (silent + -audio). No-op without samba. Returns count removed."""
    if not samba_root:
        return 0
    d = Path(samba_root) / save_subdir
    if not d.is_dir():
        return 0
    removed = 0
    for p in d.glob(f"{base}*"):
        if not p.is_file():
            continue
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            print(f"      (could not remove stale {p.name}: {e})", flush=True)
    return removed


def _is_video(name):
    return Path(name).suffix.lower() in VIDEO_EXTS


def _prefer_audio(names):
    """Choose the keeper among a render's video files. LTX writes a silent
    `<prefix>_NNNNN.mp4` and then a muxed `<prefix>_NNNNN-audio.mp4` — the latter
    (which we keep, to preserve LTX's own generated audio) sorts last and is the
    largest/last-written, so prefer a `-audio` stem, else fall back to the last
    name given. `names` is expected pre-sorted by write order / mtime."""
    audio = [n for n in names if Path(n).stem.endswith("-audio")]
    return (audio or names)[-1] if names else None


def retrieve_video(comfy, reported, samba_root, base, save_subdir):
    """Return (bytes, suffix) for the rendered clip. Prefer the samba-mounted
    `-audio.mp4` (LTX muxes its own audio into that final artifact); fall back to
    ComfyUI's HTTP /view. `reported` is the list of artifacts render() collected
    from /history (used for the HTTP fallback)."""
    if samba_root:
        d = Path(samba_root) / save_subdir
        hits = [p for p in d.glob(f"{base}*") if p.is_file() and _is_video(p.name)]
        if hits:
            hits.sort(key=lambda p: p.stat().st_mtime)  # oldest -> newest
            pick = Path(_prefer_audio([p.name for p in hits]))
            chosen = d / pick.name
            return chosen.read_bytes(), chosen.suffix
        print(f"      (samba: no {base}* video under {d}; trying HTTP)",
              flush=True)
    # HTTP fallback: pick the -audio.mp4 among the reported outputs.
    vids = [r for r in (reported or []) if _is_video(r.get("filename", ""))]
    if vids:
        pick_name = _prefer_audio([r["filename"] for r in vids])
        r = next(r for r in vids if r["filename"] == pick_name)
        raw = comfy._view(r["filename"], r.get("subfolder", ""),
                          r.get("type", "output"))
        return raw, Path(r["filename"]).suffix or ".mp4"
    raise RuntimeError("could not retrieve rendered video (no samba hit, "
                       "no video reported to /history)")


def iter_stills(set_dir):
    """Yield rendered still image paths under set_dir, sorted for stable order."""
    for p in sorted(set_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in R.IMAGE_EXTS:
            yield p


def save_video_prompt_yaml(dir_path, video_name, prompt):
    """Record the video prompt into a per-directory video_prompts.yaml, keyed by
    the .mp4 filename. Kept separate from the still prompts.yaml."""
    import yaml
    dir_path.mkdir(parents=True, exist_ok=True)
    yaml_path = dir_path / "video_prompts.yaml"
    data = {}
    if yaml_path.exists():
        try:
            data = yaml.safe_load(yaml_path.read_text()) or {}
        except yaml.YAMLError:
            data = {}
    data[video_name] = prompt
    yaml_path.write_text(
        yaml.safe_dump(data, sort_keys=True, allow_unicode=True,
                       default_flow_style=False, width=1000))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", dest="set_dir", type=Path, required=True,
                        help="A rendered still set to animate, e.g. "
                             "outputs/claude-regions. Videos are written next to "
                             "each still (same stem, .mp4).")
    parser.add_argument("--workflow", type=Path, default=DEFAULT_LTX_WORKFLOW,
                        help="LTX 2.3 i2v ComfyUI API workflow JSON.")
    parser.add_argument("--comfy-server", default="192.168.33.101:8188")
    parser.add_argument("--samba-root", default=None,
                        help="Local mount of the ComfyUI output/ dir. If set, "
                             "rendered videos are read from here; otherwise HTTP "
                             "/view is used.")
    parser.add_argument("--claude-model", default="opus")
    parser.add_argument("--llm-server", default=None,
                        help="OpenAI-compatible server (e.g. 127.0.0.1:9503) to "
                             "use for prompting instead of the Claude Code CLI.")
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--llm-workers", type=int, default=3,
                        help="LLM prompt jobs kept in flight, drafting ahead of "
                             "the serial ComfyUI renderer (Claude only; a local "
                             "--llm-server is pinned to 1). Default: %(default)s.")
    parser.add_argument("--load-name-template",
                        default="../output/reimagine/{base}.jpeg",
                        help="Path the LTX LoadImage node reads the first frame "
                             "from. LoadImage resolves relative to ComfyUI's "
                             "input/ dir, so '../output/...' reaches the still "
                             "reimagine.py already staged on the host. {base} is "
                             "the still's path with '/' -> '__' (matching "
                             "reimagine's host filename). Default: %(default)s "
                             "(for a still run staged under a different "
                             "--save-subdir, point this at ../output/<that>/"
                             "{base}.jpeg; an absolute path also works).")
    parser.add_argument("--save-subdir", default=VIDEO_SUBDIR,
                        help="Host output/ subfolder where VHS stages rendered "
                             "videos (default: %(default)s).")
    parser.add_argument("--duration", type=int, default=10,
                        help="Video duration in seconds (default: %(default)s).")
    parser.add_argument("--width", type=int, default=None,
                        help="Override video width (default: derived from the "
                             "still's aspect ratio, snapped to /64).")
    parser.add_argument("--height", type=int, default=None,
                        help="Override video height (default: derived).")
    parser.add_argument("--seed", type=int, default=42,
                        help="Base seed; video i uses seed + its index.")
    parser.add_argument("--no-videos", action="store_true",
                        help="Only generate video prompts (skip ComfyUI); print "
                             "them. Useful for previewing without a render.")
    parser.add_argument("--force", action="store_true",
                        help="Re-render even if the .mp4 already exists. Without "
                             "this, existing videos are skipped so a run resumes.")
    args = parser.parse_args()

    set_dir = args.set_dir.resolve()
    if not set_dir.is_dir():
        sys.exit(f"still set not found: {set_dir}")

    stills = list(iter_stills(set_dir))
    if not stills:
        sys.exit(f"no still images found under {set_dir}")

    if args.llm_server:
        llm = R.OpenAILLM(args.llm_server, model=args.llm_model,
                          system_prompt=VIDEO_SYSTEM_PROMPT)
    else:
        # Grant the Read tool access to the still set so Claude can view frames.
        llm = R.ClaudeCodeLLM(model=args.claude_model, add_dir=set_dir,
                              system_prompt=VIDEO_SYSTEM_PROMPT)

    workflow_base = None
    comfy = None
    if not args.no_videos:
        workflow_base = json.loads(args.workflow.read_text())
        comfy = R.ComfyClient(args.comfy_server)
        if not comfy.ping():
            print(f"ComfyUI at {args.comfy_server} not reachable yet.")
        comfy.wait_until_up()

    print(f"animate: {len(stills)} still(s) to bring to life")
    print(f"  set:     {set_dir}")
    print(f"  llm:     {llm.describe()}")
    if not args.no_videos:
        print(f"  comfy:   {args.comfy_server}  ({args.duration}s per clip)")
        print(f"  samba:   {args.samba_root or '(none — HTTP /view fallback)'}")
    print()

    n = len(stills)

    def llm_task(idx):
        """Worker-thread job: write the video prompt for still idx. Does not
        touch ComfyUI — rendering stays serial on the main thread."""
        still = stills[idx]
        rel = still.relative_to(set_dir)
        dest = still.with_suffix(".mp4")
        info = {"idx": idx, "still": still, "rel": rel, "dest": dest,
                "tag": f"[{idx + 1}/{n}] {rel}"}
        if dest.exists() and not args.force and not args.no_videos:
            info["action"] = "skip"
            return info
        t0 = time.time()
        try:
            info["prompt"] = video_prompt_for_image(llm, str(still))
        except Exception as e:
            info["action"] = "prompt_error"
            info["error"] = e
            return info
        info.update(action="ready", llm_time=time.time() - t0)
        return info

    n_workers = max(1, args.llm_workers) if llm.name == "claude" else 1
    if n_workers > 1:
        print(f"  workers: {n_workers} LLM jobs in flight (drafting ahead)\n")

    ok = fail = skipped = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {idx: pool.submit(llm_task, idx)
                   for idx in range(min(n_workers, n))}

        for i in range(n):
            info = futures.pop(i).result()
            ahead = i + n_workers
            if ahead < n:
                futures[ahead] = pool.submit(llm_task, ahead)

            tag = info["tag"]
            if info["action"] == "skip":
                print(f"{tag}  ✓ exists, skip")
                skipped += 1
                continue
            if info["action"] == "prompt_error":
                print(f"{tag}  ✗ prompt failed: {info['error']}")
                fail += 1
                continue

            prompt = info["prompt"]
            if args.no_videos:
                print(f"{tag}  ({info['llm_time']:.1f}s)\n      {prompt}\n")
                ok += 1
                continue

            rel, still, dest = info["rel"], info["still"], info["dest"]
            t_render = time.time()
            seed = args.seed + i
            if args.width and args.height:
                width, height = args.width, args.height
            else:
                width, height = R.derive_dims(still)
            base = rel.as_posix().replace("/", "__").rsplit(".", 1)[0]
            load_name = args.load_name_template.format(base=base)
            save_prefix = f"{args.save_subdir}/{base}"
            wf = patch_ltx_workflow(workflow_base, prompt, load_name, seed,
                                    width, height, args.duration, save_prefix)
            try:
                comfy.wait_until_up()
                clear_host_videos(args.samba_root, args.save_subdir, base)
                reported = comfy.render(wf, all_outputs=True)
                raw, ext = retrieve_video(comfy, reported, args.samba_root,
                                          base, args.save_subdir)
                # Keep the video next to its still; normalize odd extensions to
                # what the saver actually produced.
                out = dest if ext.lower() == ".mp4" else dest.with_suffix(ext)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(raw)
            except Exception as e:
                print(f"{tag}  ✗ render failed after "
                      f"{time.time() - t_render:.1f}s: {e}")
                fail += 1
                continue
            save_video_prompt_yaml(dest.parent, out.name, prompt)
            print(f"{tag}  ✓ llm {info['llm_time']:.1f}s + render "
                  f"{time.time() - t_render:.1f}s  {width}x{height} "
                  f"-> {out.relative_to(set_dir.parent)}")
            ok += 1

    noun = "prompt" if args.no_videos else "video"
    print(f"\ndone: {ok} {noun}(s) generated, {skipped} skipped (already existed)"
          + (f", {fail} failed" if fail else ""))


if __name__ == "__main__":
    main()
