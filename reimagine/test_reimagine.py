import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import reimagine as R


class ManifestTests(unittest.TestCase):
    def test_manual_manifest_round_trip(self):
        manifest = R.RenderManifest(
            mode="manual",
            items=[R.RenderSpec(
                index=0,
                output=Path("sports/sprint.jpg"),
                width=1920,
                height=1088,
                prompt="A sprinter accelerates from the starting blocks.",
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / R.RENDER_SPECS_YAML
            R.save_manifest(path, manifest)
            loaded = R.load_manifest(path)

        self.assertEqual(loaded, manifest)

    def test_region_manifest_preserves_exact_spec(self):
        regions = {
            "high_level_description": "A cat springs through the air.",
            "background": "A green garden.",
            "aesthetics": "Natural documentary photography.",
            "lighting": "Soft afternoon light.",
            "style": "Crisp action photograph.",
            "palette": ["#123456"],
            "elements": [{
                "type": "obj",
                "text": "",
                "desc": "A leaping tabby cat.",
                "x": 0.1234,
                "y": 0.2345,
                "w": 0.5678,
                "h": 0.6789,
                "palette": ["#abcdef"],
            }],
        }
        manifest = R.RenderManifest(
            mode="regions",
            items=[R.RenderSpec(
                index=0,
                output=Path("animals/cat-pounce.jpg"),
                width=2048,
                height=1152,
                regions=regions,
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / R.RENDER_SPECS_YAML
            R.save_manifest(path, manifest)
            loaded = R.load_manifest(path)

        self.assertEqual(loaded.items[0].regions, regions)

    def test_manifest_rejects_partial_item_indexes(self):
        data = {
            "schema_version": 1,
            "mode": "manual",
            "item_count": 2,
            "items": [{
                "index": 1,
                "output": "one.jpg",
                "width": 1920,
                "height": 1088,
                "prompt": "A sufficiently detailed prompt for an image.",
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / R.RENDER_SPECS_YAML
            path.write_text(yaml.safe_dump(data))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                R.load_manifest(path, require_complete=True)

    def test_source_hash_round_trip(self):
        source_hash = "a" * 64
        manifest = R.RenderManifest(
            mode="manual",
            items=[R.RenderSpec(
                index=0,
                output=Path("sample.jpg"),
                width=1920,
                height=1088,
                source_sha256=source_hash,
                prompt="A sufficiently detailed prompt for an image.",
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / R.RENDER_SPECS_YAML
            R.save_manifest(path, manifest)
            loaded = R.load_manifest(path)

        self.assertEqual(loaded.items[0].source_sha256, source_hash)

    def test_region_manifest_drives_workflow_without_loss(self):
        regions = {
            "high_level_description": "A cat springs through the air.",
            "background": "A green garden.",
            "aesthetics": "Natural documentary photography.",
            "lighting": "Soft afternoon light.",
            "style": "Crisp action photograph.",
            "palette": ["#123456"],
            "elements": [{
                "type": "obj", "text": "", "desc": "A tabby cat.",
                "x": 0.1234, "y": 0.2345, "w": 0.5678, "h": 0.6789,
                "palette": ["#abcdef"],
            }],
        }
        workflow = {
            R.NODE_REGION_BUILDER: {"inputs": {}},
            R.NODE_KSAMPLER: {"inputs": {}},
            R.NODE_VARIANCE: {"inputs": {}},
            R.NODE_LATENT: {"inputs": {}},
            R.NODE_SAVER: {"inputs": {}},
        }
        patched = R.patch_regions_workflow(
            workflow, regions, 42, 2048, 1152, "stage", "cat")

        builder = patched[R.NODE_REGION_BUILDER]["inputs"]
        self.assertEqual(yaml.safe_load(builder["elements_data"]),
                         regions["elements"])
        self.assertEqual(yaml.safe_load(builder["style_palette_data"]),
                         regions["palette"])

    def test_workflow_model_names_can_be_overridden(self):
        workflow = {
            R.NODE_CLIP_LOADER: {"inputs": {"clip_name": "default-clip"}},
            R.NODE_UNET_LOADER: {"inputs": {"unet_name": "default-unet"}},
        }

        result = R.override_workflow_models(
            workflow, clip_name="custom-clip", unet_name="custom-unet")

        self.assertEqual(
            result[R.NODE_CLIP_LOADER]["inputs"]["clip_name"], "custom-clip")
        self.assertEqual(
            result[R.NODE_UNET_LOADER]["inputs"]["unet_name"], "custom-unet")

    def test_workflow_model_names_keep_defaults_when_omitted(self):
        workflow = {
            R.NODE_CLIP_LOADER: {"inputs": {"clip_name": "default-clip"}},
            R.NODE_UNET_LOADER: {"inputs": {"unet_name": "default-unet"}},
        }

        R.override_workflow_models(workflow)

        self.assertEqual(
            workflow[R.NODE_CLIP_LOADER]["inputs"]["clip_name"], "default-clip")
        self.assertEqual(
            workflow[R.NODE_UNET_LOADER]["inputs"]["unet_name"], "default-unet")


class CommandIsolationTests(unittest.TestCase):
    def test_render_does_not_need_input_or_llm(self):
        manifest = R.RenderManifest(
            mode="manual",
            items=[R.RenderSpec(
                index=0,
                output=Path("sports/sprint.jpg"),
                width=1920,
                height=1088,
                prompt="A sprinter accelerates from the starting blocks.",
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "set"
            R.save_manifest(output_dir / R.RENDER_SPECS_YAML, manifest)
            with mock.patch.object(R, "build_llm", side_effect=AssertionError), \
                    mock.patch.object(R, "build_renderer") as build_renderer:
                renderer = build_renderer.return_value
                renderer.render.return_value = None
                code = R.main([
                    "render", "--output-dir", str(output_dir), "--force",
                ])

        self.assertEqual(code, 0)
        renderer.render.assert_called_once()

    def test_describe_does_not_construct_comfyui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            image = input_dir / "sample.jpg"
            from PIL import Image
            Image.new("RGB", (640, 480)).save(image)

            llm = mock.Mock(name="llm")
            llm.name = "fake"
            llm.describe.return_value = "fake llm"
            llm.chat.return_value = (
                "<prompt>A detailed photograph of a moving subject in daylight."
                "</prompt>"
            )
            with mock.patch.object(R, "build_llm", return_value=llm), \
                    mock.patch.object(R, "build_renderer",
                                      side_effect=AssertionError):
                code = R.main([
                    "describe", "--input-dir", str(input_dir),
                    "--output-dir", str(root / "output"),
                ])

            manifest = R.load_manifest(root / "output" / R.RENDER_SPECS_YAML)

        self.assertEqual(code, 0)
        self.assertEqual(len(manifest.items), 1)

    def test_render_repairs_non_mapping_prompts_yaml(self):
        manifest = R.RenderManifest(
            mode="manual",
            items=[R.RenderSpec(
                index=0,
                output=Path("sample.jpg"),
                width=1920,
                height=1088,
                prompt="A sufficiently detailed prompt for an image.",
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "sample.jpg").write_bytes(b"existing")
            (output_dir / R.PROMPTS_YAML).write_text("[]\n")
            R.save_manifest(output_dir / R.RENDER_SPECS_YAML, manifest)

            code = R.main(["render", "--output-dir", str(output_dir)])
            prompts = yaml.safe_load(
                (output_dir / R.PROMPTS_YAML).read_text())

        self.assertEqual(code, 0)
        self.assertEqual(prompts["sample.jpg"], manifest.items[0].prompt)


if __name__ == "__main__":
    unittest.main()
