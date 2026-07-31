import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL_WORKFLOW = ROOT / "workflows/krea2_comfyui_t2i_aitrepeneur_jpg_api.json"
REGIONS_WORKFLOW = ROOT / "workflows/krea2_regions_comfyui_t2i_aitrepeneur_jpg_api.json"
VIDEO_WORKFLOW = ROOT / "workflows/ltx2-3_comfyui_i2v_aitrepeneur_api.json"

STILL_SAVER = "159"
VIDEO_SAVER = "1087"


def load_workflow(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load workflow {path}: {error}") from error


def _common_still(workflow, spec, seed, save_subdir, filename):
    workflow["78:75"]["inputs"]["seed"] = seed
    if "148" in workflow:
        workflow["148"]["inputs"]["seed"] = seed
    workflow["78:76"]["inputs"].update(width=spec.width, height=spec.height)
    workflow[STILL_SAVER]["inputs"].update(
        path=save_subdir, filename=filename, seed_value=seed,
        width=spec.width, height=spec.height, time_format="")


def patch_still_workflow(base, spec, mode, seed, save_subdir, filename,
                         clip_name=None, unet_name=None):
    workflow = copy.deepcopy(base)
    if mode == "manual":
        workflow["143"]["inputs"]["value"] = spec.prompt
    else:
        region = spec.regions
        builder = workflow["14"]["inputs"]
        builder.update(
            width=spec.width, height=spec.height,
            high_level_description=region["high_level_description"],
            background=region["background"], aesthetics=region["aesthetics"],
            lighting=region["lighting"], style="photo",
            **{"style.photo": region["style"]}, medium="photograph",
            style_palette_data=json.dumps(region["palette"]) if region["palette"] else "",
            elements_data=json.dumps(region["elements"]))
    _common_still(workflow, spec, seed, save_subdir, filename)
    if clip_name:
        workflow["53"]["inputs"]["clip_name"] = clip_name
    if unet_name:
        workflow["162"]["inputs"]["unet_name"] = unet_name
    return workflow


def patch_ltx_workflow(base, prompt, load_name, seed, duration, save_prefix):
    workflow = copy.deepcopy(base)
    workflow["1070"]["inputs"]["text"] = prompt
    workflow["1077"]["inputs"]["image"] = load_name
    workflow["1073"]["inputs"]["value"] = duration
    workflow["1074"]["inputs"]["noise_seed"] = seed
    workflow[VIDEO_SAVER]["inputs"].update(
        filename_prefix=save_prefix, save_output=True)
    return workflow


def pick_artifact(artifacts, saver_node, video=False):
    candidates = [artifact for artifact in artifacts if artifact.node_id == saver_node]
    if video:
        videos = [artifact for artifact in candidates
                  if Path(artifact.filename).suffix.lower() in {".mp4", ".webm", ".mkv"}]
        audio = [artifact for artifact in videos
                 if Path(artifact.filename).stem.endswith("-audio")]
        candidates = audio or videos
    if not candidates:
        raise RuntimeError(f"workflow saver {saver_node} reported no artifact")
    return candidates[-1]
