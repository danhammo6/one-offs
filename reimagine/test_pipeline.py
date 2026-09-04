import dataclasses
import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from PIL import Image
import yaml

from reimagine_pipeline import files as pipeline_files
from reimagine_pipeline.manifest import (
    load_pipeline, load_pipeline_tree, load_render_state_tree, save_pipeline,
    save_pipeline_folder, save_pipeline_tree, save_render_state,
    save_render_state_tree,
)
from reimagine_pipeline.models import PipelineItem, PipelineManifest, StillSpec, VideoSpec
from reimagine_pipeline.files import (
    COMMON_DIMS, DEFAULT_CACHE_ROOT, DEFAULT_THUMBNAIL_CACHE_ROOT,
    THUMBNAIL_CACHE_SUBDIR, ensure_thumbnail, iter_images, prepare_common_image,
    select_common_dims, sha256_file,
    thumbnail_cache_path, thumbnail_source_fingerprint,
)
from reimagine_pipeline.llm import (
    ClaudeCodeLLM, OpenAILLM, _cli_popen_kwargs, _kill_process_group,
    _run_interruptible,
)
from reimagine_pipeline.workflows import patch_ltx_workflow, pick_artifact
from reimagine_pipeline.comfy import ComfyArtifact
from reimagine_pipeline.manifest import load_render_state
from reimagine_pipeline.prompting import (
    generate_still_prompt, generate_tagged, generate_video_prompt,
    load_system_prompt, video_prompt_word_range,
)
from reimagine_pipeline.rendering import _read_still_output, render_stills

import generate_prompts
import render_media


class PipelineManifestTests(unittest.TestCase):
    def test_common_dimensions_cover_supported_aspect_ratios(self):
        cases = [
            ((1200, 1800), (1024, 1536)),  # 2:3 portrait
            ((1000, 1497), (1024, 1536)),  # near 2:3
            ((1080, 1440), (1088, 1440)),  # 3:4 portrait
            ((1080, 1439), (1088, 1440)),  # near 3:4
            ((1080, 1920), (928, 1664)),   # 9:16 mobile
            ((1077, 1920), (928, 1664)),   # near 9:16
            ((1920, 1280), (1536, 1024)),  # 3:2 landscape
            ((1917, 1280), (1536, 1024)),  # near 3:2
            ((640, 480), (1440, 1088)),    # 4:3 standard definition
            ((644, 484), (1440, 1088)),    # near 4:3
            ((1920, 1080), (1664, 928)),   # 16:9 full HD
            ((1918, 1080), (1664, 928)),   # near 16:9
            ((1024, 1024), (1248, 1248)),  # square
            ((1000, 1003), (1248, 1248)),  # near square
        ]

        self.assertEqual(
            set(COMMON_DIMS.values()), {expected for _, expected in cases})
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(select_common_dims(*source), expected)

    def test_prepare_common_image_center_crops_and_saves_jpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            destination = root / "prepared/reference.jpg"
            image = Image.new("RGB", (2000, 1000), "red")
            image.paste((0, 255, 0), (250, 0, 1750, 1000))
            image.save(source)

            dimensions = prepare_common_image(source, destination)
            with Image.open(destination) as prepared:
                size = prepared.size
                center = prepared.getpixel((size[0] // 2, size[1] // 2))

        self.assertEqual(dimensions, (1664, 928))
        self.assertEqual(size, dimensions)
        self.assertGreater(center[1], center[0])

    def test_thumbnail_applies_orientation_without_upscaling_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            relative = Path("category/source.jpg")
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (800, 400), "red").save(source, exif=exif)
            fingerprint = thumbnail_source_fingerprint(source, relative)
            destination = thumbnail_cache_path(
                root / "cache", root / "media", relative,
                fingerprint=fingerprint)

            self.assertEqual(
                ensure_thumbnail(
                    source, destination, relative=relative,
                    fingerprint=fingerprint), destination)
            with Image.open(destination) as thumbnail:
                self.assertEqual(thumbnail.format, "WEBP")
                self.assertEqual(thumbnail.size, (256, 512))
            modified = destination.stat().st_mtime_ns
            with mock.patch(
                    "reimagine_pipeline.files.atomic_write_bytes") as write:
                self.assertEqual(
                    ensure_thumbnail(
                        source, destination, relative=relative,
                        fingerprint=fingerprint), destination)
            write.assert_not_called()
            self.assertEqual(destination.stat().st_mtime_ns, modified)

            small = root / "small.png"
            small_relative = Path("category/small.png")
            Image.new("RGB", (100, 50), "blue").save(small)
            small_fingerprint = thumbnail_source_fingerprint(
                small, small_relative)
            small_thumbnail = thumbnail_cache_path(
                root / "cache", root / "media", small_relative,
                fingerprint=small_fingerprint)
            ensure_thumbnail(
                small, small_thumbnail, relative=small_relative,
                fingerprint=small_fingerprint)
            with Image.open(small_thumbnail) as thumbnail:
                self.assertEqual(thumbnail.size, (100, 50))

    def test_thumbnail_replaces_corrupt_cache_and_rejects_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            relative = Path("source.jpg")
            Image.new("RGB", (640, 480), "green").save(source)
            fingerprint = thumbnail_source_fingerprint(source, relative)
            destination = thumbnail_cache_path(
                root / "cache", root / "media", relative,
                fingerprint=fingerprint)
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"not an image")
            os.utime(destination, ns=(
                source.stat().st_mtime_ns + 1,
                source.stat().st_mtime_ns + 1))

            self.assertEqual(
                ensure_thumbnail(
                    source, destination, relative=relative,
                    fingerprint=fingerprint), destination)
            with Image.open(destination) as thumbnail:
                thumbnail.verify()
            with self.assertRaisesRegex(ValueError, "unsafe thumbnail path"):
                thumbnail_cache_path(
                    root / "cache", root / "media",
                    Path("../private.jpg"), fingerprint="0" * 24)
            with self.assertRaisesRegex(
                    ValueError, "unsafe thumbnail fingerprint"):
                thumbnail_cache_path(
                    root / "cache", root / "media",
                    Path("private.jpg"), fingerprint="../unsafe")
            outside = root / "outside"
            outside.mkdir()
            cache_root = root / "unsafe-cache"
            cache_root.mkdir()
            (cache_root / "output").symlink_to(
                outside, target_is_directory=True)
            with self.assertRaisesRegex(
                    ValueError, "unsafe thumbnail cache directory"):
                thumbnail_cache_path(
                    cache_root, root / "media", Path("sample.jpg"),
                    fingerprint="0" * 24)

            linked_thumbnail = root / "linked.webp"
            Image.new("RGB", (32, 32), "blue").save(
                linked_thumbnail, format="WEBP")
            os.utime(linked_thumbnail, ns=(
                source.stat().st_mtime_ns + 1,
                source.stat().st_mtime_ns + 1))
            destination.unlink()
            destination.symlink_to(linked_thumbnail)

            self.assertEqual(
                ensure_thumbnail(
                    source, destination, relative=relative,
                    fingerprint=fingerprint), destination)
            self.assertFalse(destination.is_symlink())
            with Image.open(destination) as thumbnail:
                self.assertEqual(thumbnail.size, (512, 384))

    def test_thumbnail_cache_path_preserves_source_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "sources"
            source_dir.mkdir()
            jpeg_source = source_dir / "example.jpg"
            png_source = source_dir / "example.png"
            Image.new("RGB", (32, 32), "red").save(jpeg_source)
            Image.new("RGB", (32, 32), "blue").save(png_source)
            jpeg_relative = Path("category/example.jpg")
            png_relative = Path("category/example.png")
            jpeg_fingerprint = thumbnail_source_fingerprint(
                jpeg_source, jpeg_relative)
            png_fingerprint = thumbnail_source_fingerprint(
                png_source, png_relative)
            jpeg = thumbnail_cache_path(
                root / "cache", root / "media", jpeg_relative,
                fingerprint=jpeg_fingerprint)
            png = thumbnail_cache_path(
                root / "cache", root / "media", png_relative,
                fingerprint=png_fingerprint)
            ensure_thumbnail(
                jpeg_source, jpeg, relative=jpeg_relative,
                fingerprint=jpeg_fingerprint)
            ensure_thumbnail(
                png_source, png, relative=png_relative,
                fingerprint=png_fingerprint)
            with Image.open(jpeg) as thumbnail:
                jpeg_center = thumbnail.convert("RGB").getpixel((16, 16))
            with Image.open(png) as thumbnail:
                png_center = thumbnail.convert("RGB").getpixel((16, 16))

        self.assertNotEqual(jpeg, png)
        self.assertEqual(
            jpeg.name, f"example.jpg.{jpeg_fingerprint}.webp")
        self.assertEqual(
            png.name, f"example.png.{png_fingerprint}.webp")
        self.assertGreater(jpeg_center[0], jpeg_center[2])
        self.assertGreater(png_center[2], png_center[0])

    def test_thumbnail_cache_is_centralized_and_root_namespaced(self):
        expected_default = (
            Path(pipeline_files.__file__).resolve().parents[1]
            / ".reimagine-cache" / "thumbnails")
        self.assertEqual(DEFAULT_THUMBNAIL_CACHE_ROOT, expected_default)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_root = root / "project-cache"
            thumbnail_root = cache_root / THUMBNAIL_CACHE_SUBDIR
            media_a = root / "pipeline-a" / "renders"
            media_b = root / "pipeline-b" / "renders"
            relative = Path("shared/example.png")
            source = media_a / relative
            source.parent.mkdir(parents=True)
            media_b.mkdir(parents=True)
            Image.new("RGB", (32, 32), "purple").save(source)
            fingerprint = thumbnail_source_fingerprint(source, relative)

            output_a = thumbnail_cache_path(
                thumbnail_root, media_a, relative, namespace="output",
                fingerprint=fingerprint)
            input_a = thumbnail_cache_path(
                thumbnail_root, media_a, relative, namespace="input",
                fingerprint=fingerprint)
            output_b = thumbnail_cache_path(
                thumbnail_root, media_b, relative, namespace="output",
                fingerprint=fingerprint)
            result = ensure_thumbnail(
                source, output_a, relative=relative,
                fingerprint=fingerprint)

            self.assertEqual(result, output_a)
            self.assertTrue(
                output_a.is_relative_to(cache_root.resolve()))
            self.assertEqual(
                output_a.relative_to(cache_root.resolve()).parts[0],
                THUMBNAIL_CACHE_SUBDIR)
            self.assertNotEqual(output_a, input_a)
            self.assertNotEqual(output_a, output_b)
            self.assertNotIn(str(media_a), str(output_a))
            self.assertFalse((media_a / ".thumbnails").exists())
            self.assertFalse((media_b / ".thumbnails").exists())
            self.assertEqual(output_a.parts[-4], "output")

    def test_thumbnail_refreshes_same_size_replacement_with_reused_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative = Path("category/source.bmp")
            source = root / "source.bmp"
            Image.new("RGB", (64, 64), "red").save(source)
            original_mtime = source.stat().st_mtime_ns
            first_fingerprint = thumbnail_source_fingerprint(
                source, relative)
            first_destination = thumbnail_cache_path(
                root / "cache", root / "media", relative,
                fingerprint=first_fingerprint)
            ensure_thumbnail(
                source, first_destination, relative=relative,
                fingerprint=first_fingerprint)
            legacy = first_destination.parent / f"{relative.name}.webp"
            legacy.write_bytes(b"legacy")
            legacy_sidecar = first_destination.parent / (
                f"{relative.name}.webp.source")
            legacy_sidecar.write_text(first_fingerprint)
            outside = root / "outside.webp"
            outside.write_bytes(b"outside")
            ambiguous_legacy = (
                first_destination.parent / relative.with_suffix(".webp").name)
            ambiguous_legacy.symlink_to(outside)

            replacement = root / "replacement.bmp"
            Image.new("RGB", (64, 64), "blue").save(replacement)
            os.utime(replacement, ns=(original_mtime, original_mtime))
            replacement.replace(source)
            second_fingerprint = thumbnail_source_fingerprint(
                source, relative)
            second_destination = thumbnail_cache_path(
                root / "cache", root / "media", relative,
                fingerprint=second_fingerprint)
            ensure_thumbnail(
                source, second_destination, relative=relative,
                fingerprint=second_fingerprint)
            with Image.open(second_destination) as thumbnail:
                center = thumbnail.convert("RGB").getpixel((32, 32))
            first_exists = first_destination.exists()
            legacy_exists = legacy.exists()
            sidecar_exists = legacy_sidecar.exists()
            ambiguous_is_symlink = ambiguous_legacy.is_symlink()
            outside_bytes = outside.read_bytes()

        self.assertNotEqual(first_fingerprint, second_fingerprint)
        self.assertNotEqual(first_destination, second_destination)
        self.assertTrue(first_exists)
        self.assertTrue(legacy_exists)
        self.assertTrue(sidecar_exists)
        self.assertTrue(ambiguous_is_symlink)
        self.assertEqual(outside_bytes, b"outside")
        self.assertGreater(center[2], center[0])

    def test_thumbnail_rejects_source_change_during_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative = Path("category/source.bmp")
            source = root / "source.bmp"
            Image.new("RGB", (64, 64), "red").save(source)
            original_mtime = source.stat().st_mtime_ns
            fingerprint = thumbnail_source_fingerprint(source, relative)
            destination = thumbnail_cache_path(
                root / "cache", root / "media", relative,
                fingerprint=fingerprint)
            original_thumbnail_bytes = pipeline_files._thumbnail_bytes
            calls = 0

            def replace_during_first_read(path, max_edge):
                nonlocal calls
                calls += 1
                result = original_thumbnail_bytes(path, max_edge)
                if calls == 1:
                    replacement = root / "replacement.bmp"
                    Image.new("RGB", (64, 64), "blue").save(replacement)
                    os.utime(
                        replacement, ns=(original_mtime, original_mtime))
                    replacement.replace(source)
                return result

            with mock.patch.object(
                    pipeline_files, "_thumbnail_bytes",
                    side_effect=replace_during_first_read):
                result = ensure_thumbnail(
                    source, destination, relative=relative,
                    fingerprint=fingerprint)

        self.assertIsNone(result)
        self.assertEqual(calls, 1)
        self.assertFalse(destination.exists())

    def test_concurrent_generators_retain_both_immutable_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative = Path("category/source.bmp")
            source = root / "source.bmp"
            Image.new("RGB", (64, 64), "red").save(source)
            original_mtime = source.stat().st_mtime_ns
            original_snapshot = pipeline_files._source_snapshot
            fingerprint_a = original_snapshot(source, relative)
            destination_a = thumbnail_cache_path(
                root / "cache", root / "media", relative,
                fingerprint=fingerprint_a)
            generator_a_ready = threading.Event()
            release_generator_a = threading.Event()
            result_a = []
            snapshot_calls_a = 0

            def pause_generator_a_before_return(path, source_relative):
                nonlocal snapshot_calls_a
                result = original_snapshot(path, source_relative)
                if threading.current_thread().name == "generator-a":
                    snapshot_calls_a += 1
                    if snapshot_calls_a == 3:
                        generator_a_ready.set()
                        self.assertTrue(release_generator_a.wait(5))
                return result

            def generate_a():
                result_a.append(ensure_thumbnail(
                    source, destination_a, relative=relative,
                    fingerprint=fingerprint_a))

            with mock.patch.object(
                    pipeline_files, "_source_snapshot",
                    side_effect=pause_generator_a_before_return):
                thread_a = threading.Thread(
                    target=generate_a, name="generator-a", daemon=True)
                thread_a.start()
                self.assertTrue(generator_a_ready.wait(5))

                replacement = root / "replacement.bmp"
                Image.new("RGB", (64, 64), "blue").save(replacement)
                os.utime(
                    replacement, ns=(original_mtime, original_mtime))
                replacement.replace(source)
                fingerprint_b = original_snapshot(source, relative)
                destination_b = thumbnail_cache_path(
                    root / "cache", root / "media", relative,
                    fingerprint=fingerprint_b)
                result_b = ensure_thumbnail(
                    source, destination_b, relative=relative,
                    fingerprint=fingerprint_b)

                release_generator_a.set()
                thread_a.join(5)

            with Image.open(destination_a) as thumbnail_a:
                center_a = thumbnail_a.convert("RGB").getpixel((32, 32))
            with Image.open(destination_b) as thumbnail_b:
                center_b = thumbnail_b.convert("RGB").getpixel((32, 32))

        self.assertFalse(thread_a.is_alive())
        self.assertEqual(result_a, [destination_a])
        self.assertEqual(result_b, destination_b)
        self.assertNotEqual(destination_a, destination_b)
        self.assertGreater(center_a[0], center_a[2])
        self.assertGreater(center_b[2], center_b[0])

    def test_still_pipeline_backfills_thumbnail_for_existing_render(self):
        manifest = PipelineManifest(
            "manual", 1,
            [PipelineItem(
                0, "sample", Path("sample.jpg"), "a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 640, 480,
                    prompt="A detailed action photograph of a moving athlete."),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            still = output_dir / "sample.jpg"
            Image.new("RGB", (640, 480), "purple").save(still)
            args = mock.Mock(
                still_workflow=Path("workflow.json"), seed=42,
                clip_name=None, unet_name=None, still_save_subdir="stills",
                force=False, thumbnail_cache_root=output_dir.parent / "cache",
            )
            state = {"items": {"sample": {"still": {
                "plan_fingerprint": "fingerprint",
                "output_sha256": sha256_file(still),
            }}}}
            with mock.patch(
                    "reimagine_pipeline.rendering.load_workflow",
                    return_value={}), \
                    mock.patch(
                        "reimagine_pipeline.rendering._render_fingerprint",
                        return_value="fingerprint"):
                counts = render_stills(args, manifest, output_dir, state)

            relative = Path("sample.jpg")
            thumbnail = thumbnail_cache_path(
                args.thumbnail_cache_root, output_dir, relative,
                fingerprint=thumbnail_source_fingerprint(still, relative))
            thumbnail_exists = thumbnail.is_file()
            nested_cache_exists = (
                output_dir / ".thumbnails").exists()

        self.assertEqual(counts, (0, 1, 0))
        self.assertTrue(thumbnail_exists)
        self.assertFalse(nested_cache_exists)

    def test_round_trip_preserves_still_and_video_specs(self):
        manifest = PipelineManifest(
            still_mode="manual",
            item_count=1,
            common_dims=True,
            input_dir=Path("input/sports"),
            items=[PipelineItem(
                index=0,
                item_id="animals/cat-pounce",
                source_path=Path("animals/cat-pounce.jpg"),
                source_sha256="a" * 64,
                still=StillSpec(
                    output=Path("animals/cat-pounce.jpg"),
                    width=1920,
                    height=1088,
                    prompt="A detailed action photograph of a leaping tabby cat.",
                ),
                video=VideoSpec(
                    output=Path("animals/cat-pounce.mp4"),
                    prompt="The cat lands smoothly as the camera tracks left; soft paw impacts and garden ambience are audible.",
                    prompt_basis="reference",
                    basis_sha256="a" * 64,
                    duration=10,
                ),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.yaml"
            save_pipeline(path, manifest)
            loaded = load_pipeline(path)

        self.assertEqual(loaded, manifest)

    def test_pipeline_manifest_does_not_store_render_seeds(self):
        manifest = PipelineManifest(
            still_mode="manual", item_count=1,
            items=[PipelineItem(
                index=0, item_id="sample", source_path=Path("sample.jpg"),
                source_sha256="a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 1920, 1088,
                    prompt="A detailed action photograph of a moving subject."),
                video=VideoSpec(
                    Path("sample.mp4"),
                    "The subject moves smoothly while the camera tracks; quiet ambience follows.",
                    "reference", "a" * 64),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.yaml"
            save_pipeline(path, manifest)
            data = yaml.safe_load(path.read_text())

        self.assertNotIn("seed", data["items"][0]["still"])
        self.assertNotIn("seed", data["items"][0]["video"])

    def test_legacy_manifest_defaults_common_dims_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.yaml"
            path.write_text(
                "schema_version: 2\n"
                "still_mode: manual\n"
                "item_count: 0\n"
                "items: []\n")

            manifest = load_pipeline(path)

        self.assertFalse(manifest.common_dims)
        self.assertEqual(manifest.input_dir, Path("input"))

    def test_pipeline_tree_round_trip_uses_one_manifest_per_folder(self):
        items = []
        for index, name in enumerate(("animals/cat", "sports/run")):
            path = Path(name)
            items.append(PipelineItem(
                index=index, item_id=name,
                source_path=path.with_suffix(".jpg"),
                source_sha256=str(index + 1) * 64,
                still=StillSpec(
                    path.with_suffix(".jpg"), 1920, 1088,
                    prompt=f"A detailed action photograph of {name}."),
            ))
        manifest = PipelineManifest(
            "manual", 2, items, input_dir=Path("input/collection"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_pipeline_tree(root, manifest)
            loaded = load_pipeline_tree(root, require_stage="stills")

            self.assertTrue((root / "animals/pipeline.yaml").is_file())
            self.assertTrue((root / "sports/pipeline.yaml").is_file())
            self.assertFalse((root / "pipeline.yaml").exists())
            local = yaml.safe_load(
                (root / "animals/pipeline.yaml").read_text())
            self.assertEqual(local["items"][0]["id"], "cat")
            self.assertEqual(local["items"][0]["source_path"], "cat.jpg")
            self.assertEqual(local["items"][0]["still"]["output"], "cat.jpg")
            self.assertEqual(local["input_dir"], "input/collection")

        self.assertEqual([item.item_id for item in loaded.items],
                         ["animals/cat", "sports/run"])
        self.assertEqual(loaded.input_dir, Path("input/collection"))

    def test_pipeline_tree_preserves_root_and_nested_items(self):
        items = [
            PipelineItem(
                0, "root", Path("root.jpg"), "a" * 64,
                still=StillSpec(
                    Path("root.jpg"), 640, 480,
                    prompt="A detailed action photograph of a root subject.")),
            PipelineItem(
                1, "nested/child", Path("nested/child.jpg"), "b" * 64,
                still=StillSpec(
                    Path("nested/child.jpg"), 640, 480,
                    prompt="A detailed action photograph of a nested subject.")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_pipeline_tree(root, PipelineManifest("manual", 2, items))

            loaded = load_pipeline_tree(root)
            save_pipeline_tree(root, loaded)

            self.assertTrue((root / "pipeline.yaml").is_file())
            self.assertTrue((root / "nested/pipeline.yaml").is_file())

        self.assertEqual(
            [item.item_id for item in loaded.items],
            ["nested/child", "root"])

    def test_pipeline_save_removes_obsolete_prompt_projections(self):
        manifest = PipelineManifest("manual", 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prompts.yaml").write_text("old: prompt\n")
            (root / "video_prompts.yaml").write_text("old: prompt\n")

            save_pipeline(root / "pipeline.yaml", manifest)

            self.assertFalse((root / "prompts.yaml").exists())
            self.assertFalse((root / "video_prompts.yaml").exists())

    def test_pruning_root_manifest_preserves_input_only_configuration(self):
        manifest = PipelineManifest(
            "manual", 0, input_dir=Path("input/sports"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            save_pipeline_folder(
                root, Path(), manifest, prune_empty=True)

            data = yaml.safe_load((root / "pipeline.yaml").read_text())

        self.assertEqual(data, {"input_dir": "input/sports"})

    def test_render_state_tree_round_trip_uses_one_state_per_folder(self):
        state = {"schema_version": 1, "items": {
            "animals/cat": {"still": {"output_sha256": "a" * 64}},
            "sports/run": {"video": {"output_sha256": "b" * 64}},
        }}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_render_state_tree(root, state)
            loaded = load_render_state_tree(root)

            self.assertTrue((root / "animals/render_state.yaml").is_file())
            self.assertTrue((root / "sports/render_state.yaml").is_file())
            self.assertFalse((root / "render_state.yaml").exists())
            local = yaml.safe_load(
                (root / "animals/render_state.yaml").read_text())
            self.assertEqual(list(local["items"]), ["cat"])

        self.assertEqual(loaded, state)

    def test_top_level_files_migrate_to_folder_layout(self):
        item = PipelineItem(
            index=0, item_id="animals/cat",
            source_path=Path("animals/cat.jpg"),
            source_sha256="a" * 64,
            still=StillSpec(
                Path("animals/cat.jpg"), 1920, 1088,
                prompt="A detailed action photograph of a moving cat."),
        )
        manifest = PipelineManifest("manual", 1, [item])
        state = {"schema_version": 1, "items": {
            "animals/cat": {"still": {"output_sha256": "b" * 64}},
        }}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_pipeline(root / "pipeline.yaml", manifest)
            save_render_state(root / "render_state.yaml", state)

            loaded_manifest = load_pipeline_tree(root)
            loaded_state = load_render_state_tree(root)
            save_pipeline_tree(root, loaded_manifest)
            save_render_state_tree(root, loaded_state)

            self.assertFalse((root / "pipeline.yaml").exists())
            self.assertFalse((root / "render_state.yaml").exists())
            self.assertTrue((root / "animals/pipeline.yaml").is_file())
            self.assertTrue((root / "animals/render_state.yaml").is_file())

        self.assertEqual(loaded_manifest.items[0].item_id, "animals/cat")
        self.assertIn("animals/cat", loaded_state["items"])

    def test_seed_is_only_a_renderer_option(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            generate_prompts.build_parser().parse_args(["--seed", "100"])
        args = render_media.build_parser().parse_args(["--seed", "100"])
        self.assertEqual(args.seed, 100)

    def test_renderer_thumbnail_cache_root_default_and_override(self):
        default_args = render_media.build_parser().parse_args([])
        custom_args = render_media.build_parser().parse_args([
            "--thumbnail-cache-root", "custom-cache",
        ])
        unified_args = render_media.build_parser().parse_args([
            "--cache-root", "unified-cache",
        ])

        self.assertEqual(default_args.cache_root, DEFAULT_CACHE_ROOT)
        self.assertEqual(custom_args.cache_root, Path("custom-cache"))
        self.assertEqual(unified_args.cache_root, Path("unified-cache"))

    def test_ltx_patch_uses_video_plan_and_uploaded_first_frame(self):
        workflow = {
            "235": {"inputs": {"unet_name": "default-unet.gguf"}},
            "914": {"inputs": {"clip_name1": "default-clip.safetensors"}},
            "1070": {"inputs": {}},
            "1077": {"inputs": {}},
            "1073": {"inputs": {}},
            "1074": {"inputs": {}},
            "1087": {"inputs": {}},
        }
        patched = patch_ltx_workflow(
            workflow, "A controlled motion prompt with camera and audio.",
            "reimagine/run/cat.jpg", 43, 10, "videos/cat")

        self.assertEqual(patched["1077"]["inputs"]["image"],
                         "reimagine/run/cat.jpg")
        self.assertEqual(patched["1070"]["inputs"]["text"],
                         "A controlled motion prompt with camera and audio.")

    def test_ltx_patch_overrides_video_models(self):
        workflow = {
            "235": {"inputs": {"unet_name": "default-unet.gguf"}},
            "914": {"inputs": {"clip_name1": "default-clip.safetensors"}},
            "1070": {"inputs": {}},
            "1077": {"inputs": {}},
            "1073": {"inputs": {}},
            "1074": {"inputs": {}},
            "1087": {"inputs": {}},
        }

        patched = patch_ltx_workflow(
            workflow, "A controlled video prompt.", "frame.jpg", 42, 10,
            "videos/sample", "custom-clip.safetensors", "custom-unet.gguf")

        self.assertEqual(
            patched["914"]["inputs"]["clip_name1"],
            "custom-clip.safetensors")
        self.assertEqual(
            patched["235"]["inputs"]["unet_name"], "custom-unet.gguf")

    def test_parser_accepts_video_model_overrides(self):
        args = render_media.build_parser().parse_args([
            "--video-clip-name", "custom-clip.safetensors",
            "--video-unet-name", "custom-unet.gguf",
        ])

        self.assertEqual(args.video_clip_name, "custom-clip.safetensors")
        self.assertEqual(args.video_unet_name, "custom-unet.gguf")

    def test_video_prompt_word_range_scales_with_duration(self):
        self.assertEqual(video_prompt_word_range(10), (80, 160))
        self.assertEqual(video_prompt_word_range(20), (160, 320))
        self.assertEqual(video_prompt_word_range(30), (240, 480))

    def test_video_prompt_request_includes_duration_word_range(self):
        llm = mock.Mock()
        llm.chat.return_value = (
            "<video>A controlled sequence unfolds through several related "
            "beats while the camera follows and ambient sound evolves.</video>")
        still = StillSpec(
            Path("sample.jpg"), 1920, 1088,
            prompt="A detailed action photograph of a moving subject.")

        generate_video_prompt(
            llm, Path("/tmp/sample.jpg"), "rendered", still, duration=20)

        request = llm.chat.call_args.args[1]
        self.assertIn("20-second", request)
        self.assertIn("160-320 words", request)

    def test_region_validation_failure_consumes_retry_then_succeeds(self):
        llm = mock.Mock()
        llm.chat.side_effect = [
            '{"high_level_description":"A moving subject",'
            '"background":"A field","elements":[]}',
            '{"high_level_description":"A moving subject",'
            '"background":"A field","elements":['
            '{"type":"obj","desc":"subject","x":0.1,"y":0.1,'
            '"w":0.5,"h":0.5},'
            '{"type":"obj","desc":"field","x":0,"y":0.6,'
            '"w":1,"h":0.4}]}',
        ]

        result = generate_still_prompt(
            llm, Path("/tmp/sample.jpg"), "regions")

        self.assertEqual(result["high_level_description"], "A moving subject")
        self.assertEqual(llm.chat.call_count, 2)
        self.assertIsNone(llm.chat.call_args_list[0].kwargs["correction"])
        self.assertIn("elements must contain 2 to 6 entries",
                      llm.chat.call_args_list[1].kwargs["correction"])
        self.assertIn('"elements":[]',
                      llm.chat.call_args_list[1].kwargs["correction"])
        schema = llm.chat.call_args_list[0].kwargs["json_schema"]
        self.assertEqual(schema["properties"]["elements"]["minItems"], 2)

    def test_region_json_formatting_error_is_sent_back_for_correction(self):
        llm = mock.Mock()
        malformed = '{"high_level_description":"A moving subject"'
        llm.chat.side_effect = [
            malformed,
            '{"high_level_description":"A moving subject",'
            '"background":"A field","elements":['
            '{"type":"obj","desc":"subject","x":0.1,"y":0.1,'
            '"w":0.5,"h":0.5},'
            '{"type":"obj","desc":"field","x":0,"y":0.6,'
            '"w":1,"h":0.4}]}',
        ]

        result = generate_still_prompt(
            llm, Path("/tmp/sample.jpg"), "regions")

        correction = llm.chat.call_args_list[1].kwargs["correction"]
        self.assertEqual(result["high_level_description"], "A moving subject")
        self.assertIn("invalid region JSON", correction)
        self.assertIn(malformed, correction)
        self.assertIn("region JSON object", llm.chat.call_args_list[0].args[1])

    def test_region_system_prompt_requires_schema_compatible_json(self):
        prompt = load_system_prompt("system_regions.txt")

        self.assertNotIn("<|think|>", prompt)
        self.assertIn("bare JSON object", prompt)
        self.assertIn("under 700 words", prompt)
        self.assertIn("2 to 6 useful regions", prompt)
        self.assertIn("Do not use YAML, XML tags, or Markdown code fences", prompt)

    def test_system_prompts_do_not_embed_thinking_control_tokens(self):
        for name in (
                "system_manual.txt", "system_regions.txt", "system_video.txt",
                "system_video_reference.txt"):
            self.assertNotIn("<|think|>", load_system_prompt(name), name)

    def test_manual_system_prompt_does_not_request_reasoning(self):
        prompt = load_system_prompt("system_manual.txt")

        self.assertNotIn("Think first", prompt)
        self.assertIn("without narrating analysis", prompt)

    def test_region_validation_exhausts_exact_retry_budget(self):
        llm = mock.Mock()
        llm.chat.return_value = (
            '{"high_level_description":"A moving subject",'
            '"background":"A field","elements":[]}')

        with self.assertLogs("reimagine_pipeline.prompting", "WARNING") as logs, \
                self.assertRaisesRegex(RuntimeError, "after 3 tries"):
            generate_still_prompt(llm, Path("/tmp/sample.jpg"), "regions")

        self.assertEqual(llm.chat.call_count, 3)
        self.assertEqual(len(logs.output), 3)
        self.assertIn("attempt 3/3", logs.output[-1])

    def test_region_semantic_validation_rejects_off_canvas_box(self):
        llm = mock.Mock()
        llm.chat.return_value = json.dumps({
            "high_level_description": "A subject moving right",
            "background": "A field",
            "elements": [
                {"type": "obj", "desc": "Subject facing right", "x": 0.8,
                 "y": 0.1, "w": 0.4, "h": 0.6},
                {"type": "obj", "desc": "Field", "x": 0, "y": 0.7,
                 "w": 1, "h": 0.3},
            ],
        })

        with self.assertRaisesRegex(RuntimeError, "fully on-canvas"):
            generate_still_prompt(llm, Path("/tmp/sample.jpg"), "regions")

    def test_tagged_retry_logs_rejection_and_appends_correction(self):
        llm = mock.Mock()
        llm.chat.side_effect = ["not tagged", "<prompt>valid detailed prompt text</prompt>"]

        with self.assertLogs("reimagine_pipeline.prompting", "WARNING") as logs:
            result = generate_tagged(
                llm, "system", "request", Path("/tmp/sample.jpg"), "prompt")

        self.assertEqual(result, "valid detailed prompt text")
        self.assertEqual(llm.chat.call_count, 2)
        self.assertIn("attempt 1/3", logs.output[0])
        self.assertIn("missing or too-short", logs.output[0])
        self.assertIsNotNone(llm.chat.call_args_list[1].kwargs["correction"])

    def test_retry_omits_oversized_previous_response(self):
        llm = mock.Mock()
        oversized = "analysis " * 1000
        llm.chat.side_effect = [
            oversized,
            "<prompt>valid detailed prompt text</prompt>",
        ]

        result = generate_tagged(
            llm, "system", "request", Path("/tmp/sample.jpg"), "prompt")

        correction = llm.chat.call_args_list[1].kwargs["correction"]
        self.assertEqual(result, "valid detailed prompt text")
        self.assertIn("Start over", correction)
        self.assertIn("Previous response omitted", correction)
        self.assertNotIn(oversized, correction)

    def test_verbose_retry_logs_rejected_response(self):
        llm = mock.Mock()
        llm.chat.side_effect = [
            "complete but untagged response",
            "<prompt>valid detailed prompt text</prompt>",
        ]

        with self.assertLogs("reimagine_pipeline.prompting", "DEBUG") as logs:
            generate_tagged(
                llm, "system", "request", Path("/tmp/sample.jpg"), "prompt")

        self.assertTrue(any(
            "complete but untagged response" in message
            for message in logs.output))

    def test_llm_transport_failure_is_not_retried(self):
        llm = mock.Mock()
        llm.chat.side_effect = RuntimeError("server unavailable")

        with self.assertRaisesRegex(RuntimeError, "server unavailable"):
            generate_tagged(
                llm, "system", "request", Path("/tmp/sample.jpg"), "prompt")

        llm.chat.assert_called_once()

    def test_custom_prompt_directory_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_dir = Path(tmp)
            (prompt_dir / "system_manual.txt").write_text("custom system")
            llm = mock.Mock()
            llm.chat.return_value = (
                "<prompt>A sufficiently detailed custom image prompt.</prompt>")

            generate_still_prompt(
                llm, Path("/tmp/sample.jpg"), "manual", prompt_dir=prompt_dir)

        self.assertEqual(llm.chat.call_args.args[0], "custom system")

    def test_custom_region_prompt_directory_loads_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_dir = Path(tmp)
            (prompt_dir / "system_regions.txt").write_text("custom system")
            schema = json.loads(
                (Path("prompts") / "regions.schema.json").read_text())
            (prompt_dir / "regions.schema.json").write_text(json.dumps(schema))
            llm = mock.Mock()
            llm.chat.return_value = json.dumps({
                "high_level_description": "A subject moving left",
                "background": "A field",
                "elements": [
                    {"type": "obj", "desc": "Subject facing left",
                     "x": 0.1, "y": 0.1, "w": 0.5, "h": 0.6},
                    {"type": "obj", "desc": "Field", "x": 0, "y": 0.7,
                     "w": 1, "h": 0.3},
                ],
            })

            generate_still_prompt(
                llm, Path("/tmp/sample.jpg"), "regions",
                prompt_dir=prompt_dir)

        self.assertEqual(llm.chat.call_args.args[0], "custom system")
        self.assertEqual(llm.chat.call_args.kwargs["json_schema"], schema)

    def test_missing_custom_prompt_reports_full_path(self):
        missing = Path("/tmp/reimagine-missing-prompts/system_manual.txt")

        with self.assertRaisesRegex(ValueError, str(missing)):
            load_system_prompt("system_manual.txt", missing.parent)

    def test_openai_retry_payload_keeps_correction_after_image(self):
        client = OpenAILLM(
            "127.0.0.1:9503", model="test", max_tokens=16384)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok"}}]
        }).encode()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "sample.jpg"
            image.write_bytes(b"image")
            with mock.patch.object(urllib.request, "urlopen",
                                   return_value=response) as urlopen:
                client.chat("system", "original", image, correction="retry")

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        content = payload["messages"][1]["content"]
        self.assertTrue(payload["cache_prompt"])
        self.assertEqual(payload["max_tokens"], 16384)
        self.assertEqual(content[0], {"type": "text", "text": "original"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[2], {"type": "text", "text": "retry"})

    def test_openai_payload_adds_standard_json_schema_per_request(self):
        client = OpenAILLM("127.0.0.1:9503", model="test")
        schema = {"type": "object", "required": ["answer"], "properties": {
            "answer": {"type": "string"}}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": '{"answer":"ok"}'}}]
        }).encode()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "sample.jpg"
            image.write_bytes(b"image")
            with mock.patch.object(urllib.request, "urlopen",
                                   return_value=response) as urlopen:
                client.chat("system", "request", image, json_schema=schema)

        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertNotIn("json_schema", payload)
        self.assertEqual(payload["response_format"], {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_response",
                "schema": schema,
                "strict": True,
            },
        })

    def test_openai_payload_enables_llama_reasoning_per_request(self):
        client = OpenAILLM(
            "127.0.0.1:9503", model="test", reasoning="on")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "ok"}}]
        }).encode()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "sample.jpg"
            image.write_bytes(b"image")
            with mock.patch.object(urllib.request, "urlopen",
                                   return_value=response) as urlopen:
                client.chat("system", "request", image)

        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["reasoning"], "on")

    def test_claude_request_includes_json_schema_in_prompt(self):
        client = ClaudeCodeLLM(add_dir=Path("/tmp"))
        envelope = '{"subtype":"success","result":"{}"}'
        schema = {"type": "object", "required": ["answer"]}
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (envelope, "")
        with mock.patch(
                "reimagine_pipeline.llm.subprocess.Popen",
                return_value=process) as popen:
            client.chat(
                "system", "request", Path("/tmp/sample.jpg"),
                json_schema=schema)

        if os.name == "nt":
            self.assertEqual(
                popen.call_args.kwargs.get("creationflags"),
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        else:
            self.assertTrue(popen.call_args.kwargs.get("start_new_session"))
        request_prompt = process.communicate.call_args.kwargs["input"]
        self.assertIn("Return JSON matching this schema", request_prompt)
        self.assertIn(json.dumps(schema, separators=(",", ":")), request_prompt)

    def test_openai_retries_http_500_once(self):
        client = OpenAILLM("127.0.0.1:9503", model="test")
        failure = urllib.error.HTTPError(
            "http://127.0.0.1:9503/v1/chat/completions", 500,
            "Internal Server Error", {}, None)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "recovered"}}]
        }).encode()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "sample.jpg"
            image.write_bytes(b"image")
            with mock.patch.object(
                    urllib.request, "urlopen",
                    side_effect=[failure, response]) as urlopen, \
                    mock.patch("reimagine_pipeline.llm.time.sleep") as sleep:
                result = client.chat("system", "request", image)

        self.assertEqual(result, "recovered")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_openai_does_not_retry_non_500_http_errors(self):
        client = OpenAILLM("127.0.0.1:9503", model="test")
        failure = urllib.error.HTTPError(
            "http://127.0.0.1:9503/v1/chat/completions", 429,
            "Too Many Requests", {}, None)
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "sample.jpg"
            image.write_bytes(b"image")
            with mock.patch.object(
                    urllib.request, "urlopen", side_effect=failure) as urlopen, \
                    self.assertRaises(urllib.error.HTTPError):
                client.chat("system", "request", image)

        urlopen.assert_called_once()

    def test_openai_double_verbose_logs_reasoning(self):
        client = OpenAILLM("127.0.0.1:9503", model="test")
        client.log_reasoning = True
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {
                "content": "answer", "reasoning_content": "private reasoning"}}]
        }).encode()
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "sample.jpg"
            image.write_bytes(b"image")
            with mock.patch.object(urllib.request, "urlopen",
                                   return_value=response), \
                    self.assertLogs("reimagine_pipeline.llm", "DEBUG") as logs:
                result = client.chat("system", "original", image)

        self.assertEqual(result, "answer")
        self.assertTrue(any("private reasoning" in line for line in logs.output))

    def test_claude_request_includes_image_path(self):
        client = ClaudeCodeLLM(add_dir=Path("/tmp"))
        envelope = '{"subtype":"success","result":"ok"}'
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (envelope, "")
        with mock.patch(
                "reimagine_pipeline.llm.subprocess.Popen", return_value=process):
            client.chat("system", "request", Path("/tmp/frame.jpg"))

        self.assertIn("/tmp/frame.jpg", process.communicate.call_args.kwargs["input"])

    def test_run_interruptible_returns_worker_result(self):
        self.assertEqual(_run_interruptible(lambda: 7), 7)

    def test_run_interruptible_reraises_worker_error(self):
        with self.assertRaises(ValueError):
            _run_interruptible(lambda: (_ for _ in ()).throw(ValueError("x")))

    def test_run_interruptible_invokes_on_interrupt(self):
        called = []
        hold = threading.Event()

        def worker():
            hold.wait(5)

        try:
            with mock.patch(
                    "reimagine_pipeline.llm.threading.Thread.join",
                    side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    _run_interruptible(
                        worker, on_interrupt=lambda: called.append(True))
        finally:
            hold.set()
        self.assertEqual(called, [True])

    def test_kill_process_group_sends_sigkill(self):
        process = mock.Mock(pid=99)
        process.poll.return_value = None
        with mock.patch("reimagine_pipeline.llm.os.killpg") as killpg:
            _kill_process_group(process)
        killpg.assert_called_once_with(99, signal.SIGKILL)
        process.kill.assert_called_once()

    def test_kill_process_group_skips_exited_child(self):
        process = mock.Mock(pid=99)
        process.poll.return_value = 0
        with mock.patch("reimagine_pipeline.llm.os.killpg") as killpg:
            _kill_process_group(process)
        killpg.assert_not_called()
        process.kill.assert_not_called()

    def test_kill_process_group_uses_taskkill_on_windows(self):
        process = mock.Mock(pid=99)
        process.poll.return_value = None
        with mock.patch("reimagine_pipeline.llm.os.name", "nt"), \
                mock.patch("reimagine_pipeline.llm.subprocess.Popen") as popen, \
                mock.patch("reimagine_pipeline.llm.os.killpg") as killpg:
            _kill_process_group(process)
        self.assertEqual(
            popen.call_args.args[0][:4],
            ["taskkill.exe", "/F", "/T", "/PID"])
        self.assertEqual(popen.call_args.args[0][4], "99")
        killpg.assert_not_called()
        process.kill.assert_called_once()

    def test_kill_process_group_uses_msys_taskkill_flags(self):
        process = mock.Mock(pid=99)
        process.poll.return_value = None
        with mock.patch("reimagine_pipeline.llm.os.name", "posix"), \
                mock.patch("reimagine_pipeline.llm.sys.platform", "msys"), \
                mock.patch("reimagine_pipeline.llm.subprocess.Popen") as popen, \
                mock.patch("reimagine_pipeline.llm.os.killpg") as killpg:
            _kill_process_group(process)
        self.assertEqual(
            popen.call_args.args[0][:4],
            ["taskkill.exe", "//F", "//T", "//PID"])
        killpg.assert_not_called()
        process.kill.assert_called_once()

    def test_cli_popen_kwargs_use_windows_process_group(self):
        with mock.patch("reimagine_pipeline.llm.os.name", "nt"):
            kwargs = _cli_popen_kwargs()
        self.assertEqual(
            kwargs["creationflags"],
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        self.assertNotIn("start_new_session", kwargs)

    def test_claude_keyboard_interrupt_kills_process_group(self):
        client = ClaudeCodeLLM(add_dir=Path("/tmp"))
        hold = threading.Event()
        process = mock.Mock(pid=4242, returncode=None)
        process.poll.return_value = None
        process.communicate.side_effect = lambda *args, **kwargs: hold.wait(5)
        try:
            with mock.patch(
                    "reimagine_pipeline.llm.subprocess.Popen",
                    return_value=process), \
                    mock.patch(
                        "reimagine_pipeline.llm.threading.Thread.join",
                        side_effect=KeyboardInterrupt), \
                    mock.patch("reimagine_pipeline.llm.os.killpg") as killpg, \
                    self.assertRaises(KeyboardInterrupt):
                client.chat("system", "request", Path("/tmp/frame.jpg"))
            killpg.assert_called_once_with(4242, signal.SIGKILL)
        finally:
            hold.set()

    def test_generate_prompts_interrupt_returns_130(self):
        with mock.patch.object(
                generate_prompts, "_run", side_effect=KeyboardInterrupt):
            self.assertEqual(generate_prompts.main([]), 130)

    def test_parsers_show_defaults_and_prompt_prefix(self):
        prompt_args = generate_prompts.build_parser().parse_args([])
        self.assertEqual(prompt_args.prompt_path_prefix, Path("prompts"))
        self.assertEqual(prompt_args.llm_max_tokens, 16384)
        self.assertEqual(prompt_args.llm_reasoning, "on")
        self.assertEqual(
            generate_prompts.build_parser().parse_args(["-vv"]).verbose, 2)
        prompt_help = " ".join(generate_prompts.build_parser().format_help().split())
        render_help = " ".join(render_media.build_parser().format_help().split())
        self.assertIn("(default: prompts)", prompt_help)
        self.assertIn("(default: 127.0.0.1:8188)", render_help)

    def test_serve_help_shows_defaults(self):
        import serve

        with mock.patch("sys.argv", ["serve.py", "--help"]), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                self.assertRaises(SystemExit):
            serve.main()

        self.assertIn("(default: 8000)", output.getvalue())

    def test_video_artifact_prefers_muxed_audio(self):
        artifacts = [
            ComfyArtifact("1087", "clip_00001.mp4", "video", "output"),
            ComfyArtifact("1087", "clip_00001-audio.mp4", "video", "output"),
        ]

        chosen = pick_artifact(artifacts, "1087", video=True)

        self.assertEqual(chosen.filename, "clip_00001-audio.mp4")

    def test_still_artifact_falls_back_to_new_shared_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            save_dir = output_dir / "reimagine"
            save_dir.mkdir()
            old = save_dir / "animals__cat.jpeg"
            old.write_bytes(b"old")
            before = {old: (old.stat().st_mtime_ns, old.stat().st_size)}
            new = save_dir / "animals__cat_01.jpeg"
            new.write_bytes(b"new")

            raw = _read_still_output(
                mock.Mock(), [], output_dir, "reimagine",
                "animals__cat", before)

        self.assertEqual(raw, b"new")


class ProcessIsolationTests(unittest.TestCase):
    @staticmethod
    def _save_gallery_manifest(path, name, prompt=None):
        save_pipeline(path, PipelineManifest(
            "manual", 1,
            [PipelineItem(
                0, name, Path(f"{name}.png"), "a" * 64,
                still=StillSpec(
                    Path(f"{name}.jpg"), 64, 64,
                    prompt=prompt or f"A detailed photograph of {name}."),
            )],
        ))

    def test_image_discovery_follows_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            Image.new("RGB", (640, 480)).save(target / "sample.jpg")
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "linked").symlink_to(target, target_is_directory=True)
            (target / "cycle").symlink_to(input_dir, target_is_directory=True)

            images = list(iter_images(input_dir))

        self.assertEqual(
            [path.relative_to(input_dir) for path in images],
            [Path("linked/sample.jpg")])

    def test_prompt_generation_never_constructs_comfyui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (640, 480)).save(input_dir / "sample.jpg")
            llm = mock.Mock()
            llm.describe.return_value = "fake llm"
            llm.chat.side_effect = [
                "<prompt>A detailed action photograph of a moving subject in daylight.</prompt>",
                "<video>The subject moves smoothly across the frame while the camera tracks steadily; quiet ambient sound follows the motion.</video>",
            ]
            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm), \
                    mock.patch("reimagine_pipeline.comfy.ComfyClient",
                               side_effect=AssertionError):
                code = generate_prompts.main([
                    "--output-dir", str(root / "output"),
                    "--stage", "all",
                    "--video-basis", "reference",
                ])

            manifest = load_pipeline(root / "output" / "pipeline.yaml")

        self.assertEqual(code, 0)
        self.assertIsNotNone(manifest.items[0].still)
        self.assertIsNotNone(manifest.items[0].video)

    def test_input_only_pipeline_selects_project_relative_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input/sports"
            input_dir.mkdir(parents=True)
            Image.new("RGB", (640, 480)).save(input_dir / "sample.jpg")
            output_dir = root / "outputs/sports-model"
            output_dir.mkdir(parents=True)
            (output_dir / "pipeline.yaml").write_text(
                "input_dir: input/sports\n")
            llm = mock.Mock()
            llm.describe.return_value = "fake"
            llm.chat.return_value = (
                "<prompt>A detailed action photograph of a moving athlete."
                "</prompt>")

            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(
                        generate_prompts, "build_llm", return_value=llm):
                code = generate_prompts.main([
                    "--output-dir", str(output_dir), "--stage", "stills",
                ])
            manifest = load_pipeline(output_dir / "pipeline.yaml")

        self.assertEqual(code, 0)
        self.assertEqual(manifest.input_dir, Path("input/sports"))
        self.assertEqual(manifest.items[0].source_path, Path("sample.jpg"))

    def test_prompt_generator_no_longer_accepts_input_dir_option(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            generate_prompts.build_parser().parse_args([
                "--input-dir", "input/sports",
            ])

    def test_gallery_reads_input_dir_and_defers_pipeline_prompts(self):
        import serve

        manifest = PipelineManifest(
            "manual", 1,
            [PipelineItem(
                0, "sample", Path("sample.png"), "a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 640, 480,
                    prompt="A detailed action photograph of a moving athlete."),
            )],
            input_dir=Path("input/sports"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input/sports"
            input_dir.mkdir(parents=True)
            Image.new("RGB", (640, 480)).save(input_dir / "sample.png")
            output_dir = root / "outputs/model"
            output_dir.mkdir(parents=True)
            Image.new("RGB", (640, 480)).save(output_dir / "sample.jpg")
            save_pipeline(output_dir / "pipeline.yaml", manifest)

            with mock.patch.object(serve, "ROOT", root):
                pipeline_metadata = serve.load_pipeline_metadata(output_dir)
                items = list(serve.iter_pairs(
                    "model", output_dir,
                    pipeline_metadata=pipeline_metadata))

        self.assertEqual(items[0]["input_url"], "/img/input/model/sample.png")
        self.assertNotIn("prompt", items[0])
        self.assertEqual(
            pipeline_metadata[1]["sample.jpg"]["prompt"],
            "A detailed action photograph of a moving athlete.")

    def test_gallery_pair_iterator_registers_references_progressively(self):
        import serve

        manifest = PipelineManifest(
            "manual", 1,
            [PipelineItem(
                0, "sample", Path("sample.png"), "a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 640, 480,
                    prompt="A detailed action photograph of a moving athlete."),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (64, 64)).save(input_dir / "sample.png")
            output_dir = root / "output"
            output_dir.mkdir()
            Image.new("RGB", (64, 64)).save(output_dir / "sample.jpg")
            save_pipeline(output_dir / "pipeline.yaml", manifest)
            references = []

            with mock.patch.object(serve, "ROOT", root):
                records = serve.iter_pairs(
                    "model", output_dir,
                    lambda base, relative: references.append((base, relative)))
                self.assertEqual(references, [])
                first = next(records)

        self.assertEqual(first["path"], "sample.jpg")
        self.assertEqual(references, [(input_dir.resolve(), "sample.png")])

    def test_gallery_source_discovery_does_not_scan_for_images(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "empty-source").mkdir()
            (root / ".hidden").mkdir()

            sources = serve.discover_sources(root)

        self.assertEqual(list(sources), ["empty-source"])

    def test_gallery_ignores_references_from_an_aborted_stream(self):
        import serve

        source = "model"
        self.addCleanup(serve.Handler.input_dirs.pop, source, None)
        self.addCleanup(serve.Handler.allowed_references.pop, source, None)
        stale = serve.Handler._begin_gallery_load(source)
        current = serve.Handler._begin_gallery_load(source)

        serve.Handler._register_reference(
            source, stale, Path("/stale"), "stale.png")
        serve.Handler._register_reference(
            source, current, Path("/current"), "current.png")
        input_dir, allowed = serve.Handler._reference_access(source)

        self.assertEqual(input_dir, Path("/current"))
        self.assertEqual(allowed, {"current.png"})

    def test_gallery_head_request_preserves_reference_access(self):
        import serve

        source = "head-test"
        manifest = PipelineManifest(
            "manual", 1,
            [PipelineItem(
                0, "sample", Path("sample.png"), "a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 64, 64,
                    prompt="A detailed action photograph of a moving athlete."),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (64, 64)).save(input_dir / "sample.png")
            output_dir = root / "output"
            output_dir.mkdir()
            Image.new("RGB", (64, 64)).save(output_dir / "sample.jpg")
            save_pipeline(output_dir / "pipeline.yaml", manifest)

            class TestHandler(serve.Handler):
                sources = {source: output_dir}
                thumbnail_cache_root = root / "cache"

            with mock.patch.object(serve, "ROOT", root):
                TestHandler.pipeline_metadata_cache = (
                    serve.build_pipeline_metadata_cache(
                        TestHandler.sources))

            server = serve.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                urllib.request.urlopen(
                    f"{base}/api/list?source={source}").read()
                before = TestHandler._reference_access(source)
                request = urllib.request.Request(
                    f"{base}/api/stream?source={source}", method="HEAD")
                urllib.request.urlopen(request).read()
                after = TestHandler._reference_access(source)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                TestHandler.input_dirs.pop(source, None)
                TestHandler.allowed_references.pop(source, None)

        self.assertEqual(after, before)
        self.assertEqual(after[1], {"sample.png"})

    def test_manifest_index_cold_and_warm_startup(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs/model"
            self._save_gallery_manifest(
                output_dir / "animals/pipeline.yaml", "antelope")
            self._save_gallery_manifest(
                output_dir / "sports/pipeline.yaml", "runner")
            sources = {"model": output_dir}
            cache_root = root / ".reimagine-cache"

            with mock.patch.object(serve, "ROOT", root), mock.patch.object(
                    serve, "load_pipeline_document",
                    wraps=serve.load_pipeline_document) as load:
                cold = serve.build_pipeline_metadata_cache(
                    sources, cache_root)
                self.assertEqual(load.call_count, 2)
                warm = serve.build_pipeline_metadata_cache(
                    sources, cache_root)
                self.assertEqual(load.call_count, 2)

            index_path = serve.manifest_index_path(cache_root, sources)
            index = json.loads(index_path.read_text())
            index_mode = index_path.stat().st_mode & 0o777

        self.assertEqual(cold, warm)
        self.assertEqual(
            list(cold["model"][1]), ["animals/antelope.jpg",
                                     "sports/runner.jpg"])
        self.assertEqual(index["schema_version"],
                         serve.MANIFEST_INDEX_SCHEMA_VERSION)
        self.assertEqual(len(index["manifests"]), 2)
        self.assertTrue(all(
            key.startswith("project:outputs/model/")
            for key in index["manifests"]))
        self.assertEqual(index_mode, 0o600)

    def test_manifest_index_reparses_semantically_invalid_cached_input_dir(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs/model"
            manifest_path = output_dir / "pipeline.yaml"
            self._save_gallery_manifest(
                manifest_path, "sample")
            sources = {"model": output_dir}
            cache_root = root / "cache"
            index_path = serve.manifest_index_path(cache_root, sources)
            with mock.patch.object(serve, "ROOT", root):
                serve.build_pipeline_metadata_cache(sources, cache_root)
                for invalid in (
                        "/private/references", "../references",
                        "input/../references", "", ".", None, [], {}):
                    with self.subTest(input_dir=invalid):
                        index = json.loads(index_path.read_text())
                        record = next(iter(index["manifests"].values()))
                        record["document"]["input_dir"] = invalid
                        index_path.write_text(
                            json.dumps(index), encoding="utf-8")
                        with mock.patch.object(
                                serve, "load_pipeline_document",
                                wraps=serve.load_pipeline_document) as load:
                            repaired = serve.build_pipeline_metadata_cache(
                                sources, cache_root)
                            self.assertEqual(load.call_count, 1)
                            warm = serve.build_pipeline_metadata_cache(
                                sources, cache_root)
                            self.assertEqual(load.call_count, 1)
                        self.assertEqual(
                            repaired["model"][0], (root / "input").resolve())
                        self.assertEqual(warm, repaired)
                        repaired_index = json.loads(index_path.read_text())
                        repaired_record = next(iter(
                            repaired_index["manifests"].values()))
                        self.assertEqual(
                            repaired_record["document"]["input_dir"], "input")

                manifest_path.write_text(
                    "input_dir: ../references\n", encoding="utf-8")
                index = json.loads(index_path.read_text())
                record = next(iter(index["manifests"].values()))
                record["identity"] = serve._manifest_identity(manifest_path)
                record["document"]["input_dir"] = "../references"
                index_path.write_text(json.dumps(index), encoding="utf-8")
                with mock.patch.object(
                        serve, "load_pipeline_document",
                        wraps=serve.load_pipeline_document) as load:
                    malformed = serve.build_pipeline_metadata_cache(
                        sources, cache_root)
                    self.assertEqual(load.call_count, 1)
                    malformed_warm = serve.build_pipeline_metadata_cache(
                        sources, cache_root)
                    self.assertEqual(load.call_count, 1)

        self.assertEqual(malformed["model"], (None, {}))
        self.assertEqual(malformed_warm, malformed)

    def test_manifest_index_scope_name_is_stable_and_non_revealing(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "outputs/first"
            second = root / "outputs/second"
            cache_root = root / "cache"
            ordered = {"first": first, "second": second}
            reversed_order = {"second": second, "first": first}

            first_path = serve.manifest_index_path(cache_root, ordered)
            cwd = Path.cwd()
            try:
                os.chdir(root)
                reordered_path = serve.manifest_index_path(
                    cache_root, reversed_order)
            finally:
                os.chdir(cwd)
            distinct_path = serve.manifest_index_path(
                cache_root, {"first": first})

        self.assertEqual(first_path, reordered_path)
        self.assertNotEqual(first_path, distinct_path)
        self.assertRegex(
            first_path.name, r"^manifest-index-v1-[0-9a-f]{24}\.json$")
        self.assertNotIn("outputs", first_path.name)
        self.assertNotIn("first", first_path.name)

    def test_manifest_index_sequential_disjoint_scopes_remain_warm(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "outputs/first"
            second = root / "outputs/second"
            self._save_gallery_manifest(first / "pipeline.yaml", "first")
            self._save_gallery_manifest(second / "pipeline.yaml", "second")
            first_sources = {"first": first}
            second_sources = {"second": second}
            cache_root = root / "cache"
            with mock.patch.object(serve, "ROOT", root), mock.patch.object(
                    serve, "load_pipeline_document",
                    wraps=serve.load_pipeline_document) as load:
                serve.build_pipeline_metadata_cache(
                    first_sources, cache_root)
                serve.build_pipeline_metadata_cache(
                    second_sources, cache_root)
                self.assertEqual(load.call_count, 2)
                load.reset_mock()
                first_warm = serve.build_pipeline_metadata_cache(
                    first_sources, cache_root)
                second_warm = serve.build_pipeline_metadata_cache(
                    second_sources, cache_root)
                self.assertEqual(load.call_count, 0)
            indexes = list(cache_root.glob("manifest-index-v1-*.json"))

        self.assertIn("first.jpg", first_warm["first"][1])
        self.assertIn("second.jpg", second_warm["second"][1])
        self.assertEqual(len(indexes), 2)

    def test_manifest_index_concurrent_disjoint_scopes_remain_warm(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "outputs/first"
            second = root / "outputs/second"
            self._save_gallery_manifest(first / "pipeline.yaml", "first")
            self._save_gallery_manifest(second / "pipeline.yaml", "second")
            scopes = [
                {"first": first},
                {"second": second},
            ]
            cache_root = root / "cache"

            def run_concurrently():
                barrier = threading.Barrier(len(scopes))
                results = []
                errors = []

                def build(sources):
                    try:
                        barrier.wait()
                        results.append(serve.build_pipeline_metadata_cache(
                            sources, cache_root))
                    except Exception as error:
                        errors.append(error)

                threads = [
                    threading.Thread(target=build, args=(sources,))
                    for sources in scopes
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                return results, errors

            with mock.patch.object(serve, "ROOT", root), mock.patch.object(
                    serve, "load_pipeline_document",
                    wraps=serve.load_pipeline_document) as load:
                cold, cold_errors = run_concurrently()
                self.assertEqual(load.call_count, 2)
                load.reset_mock()
                warm, warm_errors = run_concurrently()
                self.assertEqual(load.call_count, 0)
            indexes = list(cache_root.glob("manifest-index-v1-*.json"))

        self.assertEqual(cold_errors + warm_errors, [])
        self.assertEqual(len(cold), 2)
        self.assertEqual(len(warm), 2)
        self.assertEqual(len(indexes), 2)

    def test_manifest_index_updates_only_changed_new_and_deleted_files(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs/model"
            first = output_dir / "a/pipeline.yaml"
            second = output_dir / "b/pipeline.yaml"
            third = output_dir / "c/pipeline.yaml"
            self._save_gallery_manifest(first, "a")
            self._save_gallery_manifest(second, "b")
            sources = {"model": output_dir}
            cache_root = root / "cache"
            with mock.patch.object(serve, "ROOT", root), mock.patch.object(
                    serve, "load_pipeline_document",
                    wraps=serve.load_pipeline_document) as load:
                serve.build_pipeline_metadata_cache(sources, cache_root)
                load.reset_mock()

                changed_prompt = (
                    "A detailed changed photograph of an athlete in motion.")
                self._save_gallery_manifest(first, "a", changed_prompt)
                changed = serve.build_pipeline_metadata_cache(
                    sources, cache_root)
                self.assertEqual(load.call_count, 1)
                self.assertEqual(
                    changed["model"][1]["a/a.jpg"]["prompt"],
                    changed_prompt)
                load.reset_mock()

                self._save_gallery_manifest(third, "c")
                added = serve.build_pipeline_metadata_cache(
                    sources, cache_root)
                self.assertEqual(load.call_count, 1)
                self.assertIn("c/c.jpg", added["model"][1])
                load.reset_mock()

                second.unlink()
                deleted = serve.build_pipeline_metadata_cache(
                    sources, cache_root)
                self.assertEqual(load.call_count, 0)
                self.assertNotIn("b/b.jpg", deleted["model"][1])
                load.reset_mock()

                first.write_text("not: [valid", encoding="utf-8")
                malformed = serve.build_pipeline_metadata_cache(
                    sources, cache_root)
                self.assertEqual(load.call_count, 1)
                load.reset_mock()
                malformed_warm = serve.build_pipeline_metadata_cache(
                    sources, cache_root)
                self.assertEqual(load.call_count, 0)

            index = json.loads(
                serve.manifest_index_path(cache_root, sources).read_text())

        self.assertEqual(malformed["model"], (None, {}))
        self.assertEqual(malformed_warm, malformed)
        self.assertEqual(len(index["manifests"]), 2)
        self.assertTrue(any(
            "error" in record for record in index["manifests"].values()))

    def test_manifest_index_recovers_from_corrupt_and_incompatible_json(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs/model"
            self._save_gallery_manifest(
                output_dir / "pipeline.yaml", "sample")
            sources = {"model": output_dir}
            cache_root = root / "cache"
            index_path = serve.manifest_index_path(cache_root, sources)
            with mock.patch.object(serve, "ROOT", root):
                serve.build_pipeline_metadata_cache(sources, cache_root)
                index_path.write_text("{broken", encoding="utf-8")
                with mock.patch.object(
                        serve, "load_pipeline_document",
                        wraps=serve.load_pipeline_document) as load, \
                        self.assertLogs("serve", "WARNING") as corrupt_logs:
                    corrupt = serve.build_pipeline_metadata_cache(
                        sources, cache_root)
                self.assertEqual(load.call_count, 1)
                index_path.write_text(json.dumps({
                    "schema_version": 999, "manifests": {},
                }), encoding="utf-8")
                with mock.patch.object(
                        serve, "load_pipeline_document",
                        wraps=serve.load_pipeline_document) as load, \
                        self.assertLogs("serve", "WARNING") as schema_logs:
                    incompatible = serve.build_pipeline_metadata_cache(
                        sources, cache_root)
                self.assertEqual(load.call_count, 1)

        self.assertEqual(corrupt, incompatible)
        self.assertIn("rebuilding", corrupt_logs.output[0])
        self.assertIn("incompatible schema", schema_logs.output[0])

    def test_manifest_index_write_failure_does_not_block_startup(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs/model"
            self._save_gallery_manifest(
                output_dir / "pipeline.yaml", "sample")
            sources = {"model": output_dir}
            cache_root = root / "cache"
            with mock.patch.object(serve, "ROOT", root), \
                    mock.patch.object(
                        serve, "_atomic_write_manifest_index",
                        side_effect=OSError("simulated replace failure")), \
                    self.assertLogs("serve", "WARNING") as logs:
                metadata = serve.build_pipeline_metadata_cache(
                    sources, cache_root)
            index_path = serve.manifest_index_path(cache_root, sources)
            index_path.write_text('{"existing":true}\n', encoding="utf-8")
            with mock.patch.object(
                    pipeline_files.Path, "replace",
                    side_effect=OSError("simulated atomic replace failure")):
                with self.assertRaisesRegex(
                        OSError, "simulated atomic replace failure"):
                    serve._atomic_write_manifest_index(index_path, {
                        "schema_version": serve.MANIFEST_INDEX_SCHEMA_VERSION,
                        "manifests": {},
                    })
            temporary_files = list(
                cache_root.glob(f".{index_path.name}.*"))
            preserved = index_path.read_text(encoding="utf-8")

        self.assertIn("sample.jpg", metadata["model"][1])
        self.assertIn("simulated replace failure", logs.output[-1])
        self.assertEqual(temporary_files, [])
        self.assertEqual(preserved, '{"existing":true}\n')

    def test_manifest_index_concurrent_writers_remain_valid(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "outputs/model"
            for number in range(12):
                self._save_gallery_manifest(
                    output_dir / f"{number:02d}/pipeline.yaml",
                    f"sample-{number:02d}")
            sources = {"model": output_dir}
            cache_root = root / "cache"
            barrier = threading.Barrier(4)
            results = []
            errors = []

            def build():
                try:
                    barrier.wait()
                    results.append(serve.build_pipeline_metadata_cache(
                        sources, cache_root))
                except Exception as error:
                    errors.append(error)

            with mock.patch.object(serve, "ROOT", root):
                threads = [threading.Thread(target=build) for _ in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            index = json.loads(
                serve.manifest_index_path(cache_root, sources).read_text())

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(len(result["model"][1]) == 12
                            for result in results))
        self.assertEqual(len(index["manifests"]), 12)

    def test_gallery_eager_cache_prevents_request_reparsing(self):
        import serve

        source = "cached-model"
        manifest = PipelineManifest(
            "manual", 1,
            [PipelineItem(
                0, "sample", Path("sample.png"), "a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 64, 64,
                    prompt="A detailed action photograph of a moving athlete."),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (64, 64)).save(input_dir / "sample.png")
            output_dir = root / "output"
            output_dir.mkdir()
            Image.new("RGB", (64, 64)).save(output_dir / "sample.jpg")
            save_pipeline(output_dir / "pipeline.yaml", manifest)

            class TestHandler(serve.Handler):
                sources = {source: output_dir}
                thumbnail_cache_root = root / "cache"

            with mock.patch.object(serve, "ROOT", root), \
                    mock.patch.object(
                        yaml, "safe_load", wraps=yaml.safe_load) as safe_load:
                TestHandler.pipeline_metadata_cache = (
                    serve.build_pipeline_metadata_cache(
                        TestHandler.sources))
                self.assertEqual(safe_load.call_count, 1)

                server = serve.ThreadingHTTPServer(
                    ("127.0.0.1", 0), TestHandler)
                thread = threading.Thread(
                    target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    thumbnail_mtime = None
                    for iteration in range(2):
                        listed = json.loads(urllib.request.urlopen(
                            f"{base}/api/list?source={source}").read())
                        streamed = urllib.request.urlopen(
                            f"{base}/api/stream?source={source}").read()
                        metadata = json.loads(urllib.request.urlopen(
                            f"{base}/api/metadata?source={source}"
                            "&path=sample.jpg").read())
                        urllib.request.urlopen(
                            f"{base}/img/output/{source}/sample.jpg").read()
                        urllib.request.urlopen(
                            f"{base}/img/input/{source}/sample.png").read()
                        output_thumbnail = urllib.request.urlopen(
                            f"{base}{listed[0]['output_thumbnail_url']}")
                        input_thumbnail = urllib.request.urlopen(
                            f"{base}{listed[0]['input_thumbnail_url']}")
                        self.assertEqual(
                            output_thumbnail.headers.get_content_type(),
                            "image/webp")
                        self.assertIn(
                            "immutable",
                            output_thumbnail.headers["Cache-Control"])
                        output_thumbnail.read()
                        input_thumbnail.read()
                        output_relative = Path("sample.jpg")
                        cached_thumbnail = thumbnail_cache_path(
                            TestHandler.thumbnail_cache_root, output_dir,
                            output_relative,
                            fingerprint=thumbnail_source_fingerprint(
                                output_dir / output_relative,
                                output_relative))
                        if iteration == 0:
                            thumbnail_mtime = (
                                cached_thumbnail.stat().st_mtime_ns)
                        else:
                            self.assertEqual(
                                cached_thumbnail.stat().st_mtime_ns,
                                thumbnail_mtime)
                    stale_url = listed[0]["output_thumbnail_url"]
                    original_stat = (
                        output_dir / "sample.jpg").stat()
                    replacement = output_dir / "replacement.jpg"
                    Image.new("RGB", (64, 64), "blue").save(replacement)
                    os.utime(replacement, ns=(
                        original_stat.st_atime_ns,
                        original_stat.st_mtime_ns))
                    replacement.replace(output_dir / "sample.jpg")
                    with self.assertRaises(
                            urllib.error.HTTPError) as stale_error:
                        urllib.request.urlopen(f"{base}{stale_url}")
                    self.assertEqual(stale_error.exception.code, 409)
                    self.assertEqual(
                        stale_error.exception.headers["Cache-Control"],
                        "no-store")
                    stale_error.exception.close()
                    refreshed = json.loads(urllib.request.urlopen(
                        f"{base}/api/list?source={source}").read())
                    self.assertNotEqual(
                        refreshed[0]["output_thumbnail_url"], stale_url)
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(
                            f"{base}/img/thumbnail/output/{source}/"
                            "%2e%2e%2Fprivate.jpg")
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
                    self.assertFalse(
                        (output_dir / ".thumbnails").exists())
                    self.assertFalse(
                        (input_dir / ".thumbnails").exists())
                    self.assertTrue(
                        TestHandler.thumbnail_cache_root.is_dir())
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                    TestHandler.input_dirs.pop(source, None)
                    TestHandler.allowed_references.pop(source, None)

                self.assertEqual(safe_load.call_count, 1)

        self.assertEqual(len(listed), 1)
        self.assertNotIn("prompt", listed[0])
        self.assertNotIn("video_prompt", listed[0])
        self.assertIn("output_thumbnail_url", listed[0])
        self.assertEqual(len(streamed.splitlines()), 1)
        self.assertNotIn(b'"prompt"', streamed)
        self.assertEqual(
            metadata["prompt"],
            "A detailed action photograph of a moving athlete.")

    def test_versioned_thumbnail_survives_stale_renderer_write_race(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "output"
            relative = Path("category/source.bmp")
            source_path = output_dir / relative
            source_path.parent.mkdir(parents=True)
            Image.new("RGB", (64, 64), "red").save(source_path)
            original_mtime = source_path.stat().st_mtime_ns
            stale_fingerprint = thumbnail_source_fingerprint(
                source_path, relative)
            stale_destination = thumbnail_cache_path(
                root / "cache", output_dir, relative,
                fingerprint=stale_fingerprint)

            renderer_waiting = threading.Event()
            release_renderer = threading.Event()
            renderer_done = threading.Event()
            renderer_result = []
            original_atomic_write = pipeline_files.atomic_write_bytes
            original_read_bytes = Path.read_bytes

            def controlled_atomic_write(path, data):
                if threading.current_thread().name == "stale-renderer":
                    renderer_waiting.set()
                    self.assertTrue(release_renderer.wait(5))
                original_atomic_write(path, data)

            def run_stale_renderer():
                try:
                    renderer_result.append(ensure_thumbnail(
                        source_path, stale_destination, relative=relative,
                        fingerprint=stale_fingerprint))
                finally:
                    renderer_done.set()

            with mock.patch.object(
                    pipeline_files, "atomic_write_bytes",
                    side_effect=controlled_atomic_write):
                renderer = threading.Thread(
                    target=run_stale_renderer, name="stale-renderer")
                renderer.start()
                self.assertTrue(renderer_waiting.wait(5))

                replacement = root / "replacement.bmp"
                Image.new("RGB", (64, 64), "blue").save(replacement)
                os.utime(
                    replacement, ns=(original_mtime, original_mtime))
                replacement.replace(source_path)
                current_fingerprint = thumbnail_source_fingerprint(
                    source_path, relative)
                current_destination = thumbnail_cache_path(
                    root / "cache", output_dir, relative,
                    fingerprint=current_fingerprint)

                class TestHandler(serve.Handler):
                    sources = {"race": output_dir}
                    pipeline_metadata_cache = {"race": (None, {})}
                    thumbnail_cache_root = root / "cache"

                def read_after_stale_renderer(path):
                    if path == current_destination:
                        release_renderer.set()
                        self.assertTrue(renderer_done.wait(5))
                    return original_read_bytes(path)

                server = serve.ThreadingHTTPServer(
                    ("127.0.0.1", 0), TestHandler)
                server_thread = threading.Thread(
                    target=server.serve_forever, daemon=True)
                server_thread.start()
                url = (
                    f"http://127.0.0.1:{server.server_port}"
                    f"/img/thumbnail/output/race/{relative.as_posix()}"
                    f"?v={current_fingerprint}")
                try:
                    with mock.patch.object(
                            Path, "read_bytes",
                            new=read_after_stale_renderer):
                        response = urllib.request.urlopen(url)
                        thumbnail_bytes = response.read()
                        response.close()
                finally:
                    release_renderer.set()
                    server.shutdown()
                    server.server_close()
                    server_thread.join()
                    renderer.join()

            with Image.open(io.BytesIO(thumbnail_bytes)) as thumbnail:
                center = thumbnail.convert("RGB").getpixel((32, 32))
            stale_exists = stale_destination.exists()
            current_exists = current_destination.is_file()
            with Image.open(stale_destination) as stale_thumbnail:
                stale_center = stale_thumbnail.convert("RGB").getpixel((32, 32))

        self.assertNotEqual(stale_destination, current_destination)
        self.assertEqual(renderer_result, [None])
        self.assertTrue(stale_exists)
        self.assertTrue(current_exists)
        self.assertGreater(stale_center[0], stale_center[2])
        self.assertGreater(center[2], center[0])

    def test_gallery_main_builds_cache_before_listening(self):
        import serve

        events = []
        cached = {"model": (Path("/input"), {})}

        def build_cache(sources, cache_root):
            events.append(("cache", dict(sources), cache_root))
            return cached

        class FakeServer:
            def __init__(self, address, handler):
                events.append(
                    ("server", address, handler.pipeline_metadata_cache,
                     handler.thumbnail_cache_root))

            def serve_forever(self):
                events.append(("serve",))
                raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "custom-thumbnail-cache"
            with (
                mock.patch("sys.argv", [
                    "serve.py", "--output-dir", tmp,
                    "--cache-root", str(cache_root),
                ]),
                mock.patch.object(
                    serve, "build_pipeline_metadata_cache",
                    side_effect=build_cache),
                mock.patch.object(serve, "ThreadingHTTPServer", FakeServer),
                mock.patch.object(serve.Handler, "sources", {}),
                mock.patch.object(
                    serve.Handler, "pipeline_metadata_cache", {}),
                mock.patch.object(
                    serve.Handler, "cache_root", DEFAULT_CACHE_ROOT),
                mock.patch.object(
                    serve.Handler, "thumbnail_cache_root",
                    DEFAULT_THUMBNAIL_CACHE_ROOT),
            ):
                serve.main()

        self.assertEqual([event[0] for event in events],
                         ["cache", "server", "serve"])
        self.assertEqual(events[0][2], cache_root)
        self.assertIs(events[1][2], cached)
        self.assertEqual(
            events[1][3], cache_root / THUMBNAIL_CACHE_SUBDIR)

    def test_gallery_rejects_malformed_input_configuration(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "pipeline.yaml").write_text(
                "input_dir: ../../private\n")

            input_dir, metadata = serve.load_pipeline_metadata(output_dir)

        self.assertIsNone(input_dir)
        self.assertEqual(metadata, {})

    def test_gallery_reference_allowlist_supports_input_symlinks(self):
        import serve

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "references"
            target.mkdir()
            Image.new("RGB", (64, 64)).save(target / "sample.png")
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "linked").symlink_to(target, target_is_directory=True)
            output_dir = root / "output"
            (output_dir / "linked").mkdir(parents=True)
            Image.new("RGB", (64, 64)).save(
                output_dir / "linked/sample.jpg")
            (input_dir / "unrelated.txt").write_text("private")
            (input_dir / "spoof.png").symlink_to(input_dir / "unrelated.txt")

            allowed = serve.reference_paths(output_dir, input_dir)
            linked_reference = serve.safe_media_path(
                input_dir, "linked/sample.png", serve.IMAGE_EXTS,
                allowed_paths=allowed, allow_linked_dirs=True)
            spoofed_reference = serve.safe_media_path(
                input_dir, "spoof.png", serve.IMAGE_EXTS,
                allowed_paths={"spoof.png"}, allow_linked_dirs=True)

        self.assertEqual(allowed, {"linked/sample.png"})
        self.assertNotIn("unrelated.txt", allowed)
        self.assertIsNotNone(linked_reference)
        self.assertIsNone(spoofed_reference)

    def test_common_dims_prompts_with_temporary_crop_and_records_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            source = input_dir / "sample.png"
            Image.new("RGB", (2000, 1000), "green").save(source)
            source_hash = sha256_file(source)
            seen = []
            llm = mock.Mock()
            llm.describe.return_value = "fake"

            def chat(_system, _user, image_path, **_kwargs):
                with Image.open(image_path) as image:
                    seen.append((image_path, image.size))
                return ("<prompt>A detailed action photograph of a moving "
                        "subject in a wide landscape.</prompt>")

            llm.chat.side_effect = chat
            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm) as build_llm:
                code = generate_prompts.main([
                    "--output-dir", str(root / "output"),
                    "--stage", "stills", "--common-dims",
                ])
            manifest = load_pipeline(root / "output/pipeline.yaml")
            still = manifest.items[0].still
            add_dir = build_llm.call_args.args[1]

        self.assertEqual(code, 0)
        self.assertEqual((still.width, still.height), (1664, 928))
        self.assertTrue(manifest.common_dims)
        self.assertEqual(manifest.items[0].source_path, Path("sample.png"))
        self.assertEqual(manifest.items[0].source_sha256, source_hash)
        self.assertEqual(seen[0][1], (1664, 928))
        self.assertNotEqual(seen[0][0], source)
        self.assertEqual(seen[0][0].parent, add_dir)

    def test_default_prompting_uses_original_source_and_derived_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            source = input_dir / "sample.jpg"
            Image.new("RGB", (640, 480), "green").save(source)
            llm = mock.Mock()
            llm.describe.return_value = "fake"
            llm.chat.return_value = (
                "<prompt>A detailed action photograph of a moving subject in "
                "a landscape.</prompt>")
            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--output-dir", str(root / "output"),
                    "--stage", "stills",
                ])
            manifest = load_pipeline(root / "output/pipeline.yaml")

        self.assertEqual(code, 0)
        self.assertEqual(llm.chat.call_args.args[2], source.resolve())
        self.assertEqual(
            (manifest.items[0].still.width, manifest.items[0].still.height),
            (1664, 1216))
        self.assertFalse(manifest.common_dims)

    def test_video_resume_rejects_mismatched_common_dims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            source = input_dir / "sample.jpg"
            Image.new("RGB", (640, 480), "green").save(source)
            output_dir = root / "output"
            save_pipeline(output_dir / "pipeline.yaml", PipelineManifest(
                "manual", 1, [PipelineItem(
                    0, "sample", Path("sample.jpg"), sha256_file(source),
                    still=StillSpec(
                        Path("sample.jpg"), 1440, 1088,
                        prompt="A detailed action photograph of a subject."),
                )], common_dims=True))

            with mock.patch.object(generate_prompts, "ROOT", root), \
                    self.assertLogs("generate_prompts", "ERROR") as logs:
                code = generate_prompts.main([
                    "--output-dir", str(output_dir),
                    "--stage", "videos",
                ])

        self.assertEqual(code, 2)
        self.assertIn("matching --common-dims", logs.output[-1])

    def test_prompt_generation_writes_one_manifest_per_image_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            for folder in ("animals", "sports"):
                (input_dir / folder).mkdir(parents=True)
                Image.new("RGB", (640, 480)).save(
                    input_dir / folder / "sample.jpg")
            llm = mock.Mock()
            llm.describe.return_value = "fake"
            llm.chat.side_effect = [
                "<prompt>A detailed action photograph of an animal.</prompt>",
                "<prompt>A detailed action photograph of an athlete.</prompt>",
            ]

            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--output-dir", str(root / "output"),
                    "--stage", "stills",
                ])

            manifest = load_pipeline_tree(
                root / "output", require_stage="stills")

            self.assertTrue(
                (root / "output/animals/pipeline.yaml").is_file())
            self.assertTrue(
                (root / "output/sports/pipeline.yaml").is_file())
            self.assertFalse((root / "output/pipeline.yaml").exists())

        self.assertEqual(code, 0)
        self.assertEqual(manifest.item_count, 2)

    def test_renderer_reads_folder_manifests_and_states(self):
        manifest = PipelineManifest(
            still_mode="manual", item_count=1,
            items=[PipelineItem(
                index=0, item_id="animals/cat",
                source_path=Path("animals/cat.jpg"),
                source_sha256="a" * 64,
                still=StillSpec(
                    Path("animals/cat.jpg"), 1920, 1088,
                    prompt="A detailed action photograph of a moving cat."),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            save_pipeline_tree(output_dir, manifest)
            save_render_state_tree(output_dir, {
                "schema_version": 1, "items": {
                    "animals/cat": {"still": {"output_sha256": "a" * 64}},
                },
            })
            with mock.patch.object(render_media, "render_stills",
                                   return_value=(0, 1, 0)) as render_stills:
                code = render_media.main([
                    "--output-dir", str(output_dir), "--stage", "stills",
                ])
            loaded_state = render_stills.call_args.args[3]

        self.assertEqual(code, 0)
        self.assertIn("animals/cat", loaded_state["items"])

    def test_load_pipeline_still_requires_complete_plans_when_requested(self):
        ready = PipelineItem(
            index=0, item_id="ready", source_path=Path("ready.jpg"),
            source_sha256="a" * 64,
            still=StillSpec(
                Path("ready.jpg"), 1920, 1088,
                prompt="A detailed action photograph of a moving subject."),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.yaml"
            save_pipeline(path, PipelineManifest("manual", 2, [ready]))
            with self.assertRaisesRegex(ValueError, "incomplete still plans"):
                load_pipeline(path, require_stage="stills")
            loaded = load_pipeline(path)

        self.assertEqual([item.item_id for item in loaded.items], ["ready"])

    def test_render_warns_and_continues_when_still_plans_are_incomplete(self):
        ready = PipelineItem(
            index=0, item_id="ready", source_path=Path("ready.jpg"),
            source_sha256="a" * 64,
            still=StillSpec(
                Path("ready.jpg"), 1920, 1088,
                prompt="A detailed action photograph of a moving subject."),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            save_pipeline(
                output_dir / "pipeline.yaml",
                PipelineManifest("manual", 2, [ready]))
            with mock.patch.object(
                    render_media, "render_stills",
                    return_value=(1, 0, 0)) as render_stills, \
                    self.assertLogs("render_media", "WARNING") as logs:
                code = render_media.main([
                    "--output-dir", str(output_dir), "--stage", "stills",
                ])

        self.assertEqual(code, 0)
        render_stills.assert_called_once()
        passed = render_stills.call_args.args[1]
        self.assertEqual([item.item_id for item in passed.items], ["ready"])
        self.assertTrue(any(
            "incomplete still plans" in message
            and "rendering available items" in message
            for message in logs.output))

    def test_render_never_constructs_llm_or_reads_input_tree(self):
        manifest = PipelineManifest(
            still_mode="manual",
            item_count=1,
            items=[PipelineItem(
                index=0,
                item_id="sample",
                source_path=Path("sample.jpg"),
                source_sha256="a" * 64,
                still=StillSpec(
                    output=Path("sample.jpg"), width=1920, height=1088,
                    prompt="A detailed action photograph of a moving subject.",
                ),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            save_pipeline(output_dir / "pipeline.yaml", manifest)
            with mock.patch.object(render_media, "build_llm",
                                   side_effect=AssertionError, create=True), \
                    mock.patch.object(render_media, "render_stills",
                                      return_value=(1, 0, 0)) as render_stills:
                code = render_media.main([
                    "--output-dir", str(output_dir), "--stage", "stills",
                ])

        self.assertEqual(code, 0)
        render_stills.assert_called_once()

    def test_all_stage_delegates_to_serial_still_then_video_renderer(self):
        manifest = PipelineManifest(
            still_mode="manual", item_count=1,
            items=[PipelineItem(
                index=0, item_id="sample", source_path=Path("sample.jpg"),
                source_sha256="a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 1920, 1088,
                    prompt="A detailed action photograph of a moving subject."),
                video=VideoSpec(
                    Path("sample.mp4"),
                    "The subject moves smoothly while the camera tracks; quiet ambience follows.",
                    "reference", "a" * 64),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            save_pipeline(output_dir / "pipeline.yaml", manifest)
            with mock.patch.object(render_media, "render_all",
                                   return_value=(2, 0, 0)) as render_all:
                code = render_media.main([
                    "--output-dir", str(output_dir), "--stage", "all",
                ])

        self.assertEqual(code, 0)
        render_all.assert_called_once()

    def test_renderer_seed_override_is_passed_to_still_workflow(self):
        manifest = PipelineManifest(
            still_mode="manual", item_count=1,
            items=[PipelineItem(
                index=0, item_id="sample", source_path=Path("sample.jpg"),
                source_sha256="a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 1920, 1088,
                    prompt="A detailed action photograph of a moving subject."),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "output"
            output_dir.mkdir()
            cache_root = root / "cache"
            save_pipeline(output_dir / "pipeline.yaml", manifest)
            artifact = ComfyArtifact("885", "sample.jpeg", "", "output")
            fake = mock.Mock()
            fake.ping.return_value = True
            fake.run_workflow.return_value = [artifact]
            image = io.BytesIO()
            Image.new("RGB", (64, 64)).save(image, format="JPEG")
            fake.read_artifact.return_value = image.getvalue()
            with mock.patch("reimagine_pipeline.rendering.ComfyClient",
                            return_value=fake):
                code = render_media.main([
                    "--output-dir", str(output_dir), "--stage", "stills",
                    "--seed", "100",
                    "--thumbnail-cache-root", str(cache_root),
                ])
            workflow = fake.run_workflow.call_args.args[0]
            nested_cache_exists = (
                output_dir / ".thumbnails").exists()
            unified_thumbnails = list(
                (cache_root / THUMBNAIL_CACHE_SUBDIR).rglob("*.webp"))

        self.assertEqual(code, 0)
        self.assertFalse(nested_cache_exists)
        self.assertEqual(len(unified_thumbnails), 1)
        self.assertEqual(workflow["273"]["inputs"]["seed"], 100)
        self.assertEqual(workflow["265"]["inputs"]["seed"], 100)

    def test_video_render_logs_elapsed_time(self):
        manifest = PipelineManifest(
            still_mode="manual", item_count=1,
            items=[PipelineItem(
                index=0, item_id="sample", source_path=Path("sample.jpg"),
                source_sha256="a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 1920, 1088,
                    prompt="A detailed action photograph of a moving subject."),
                video=VideoSpec(
                    Path("sample.mp4"),
                    "The subject moves smoothly while the camera tracks; quiet ambience follows.",
                    "reference", "a" * 64),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            Image.new("RGB", (64, 64)).save(output_dir / "sample.jpg")
            save_pipeline(output_dir / "pipeline.yaml", manifest)
            artifact = ComfyArtifact("1087", "sample.mp4", "video", "output")
            fake = mock.Mock()
            fake.upload_image.return_value = "reimagine/sample.jpg"
            fake.run_workflow.return_value = [artifact]
            fake.read_artifact.return_value = b"video"
            with mock.patch("reimagine_pipeline.rendering.ComfyClient",
                            return_value=fake), \
                    self.assertLogs("reimagine_pipeline.rendering", "INFO") as logs:
                code = render_media.main([
                    "--output-dir", str(output_dir), "--stage", "videos",
                ])

        self.assertEqual(code, 0)
        self.assertTrue(any(
            "video sample: rendered in" in message for message in logs.output))

    def test_rendered_basis_video_is_blocked_when_still_changed(self):
        manifest = PipelineManifest(
            still_mode="manual", item_count=1,
            items=[PipelineItem(
                index=0, item_id="sample", source_path=Path("sample.jpg"),
                source_sha256="a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 1920, 1088,
                    prompt="A detailed action photograph of a moving subject."),
                video=VideoSpec(
                    Path("sample.mp4"),
                    "The subject moves smoothly while the camera tracks; quiet ambience follows.",
                    "rendered", "b" * 64),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            Image.new("RGB", (64, 64)).save(output_dir / "sample.jpg")
            save_pipeline(output_dir / "pipeline.yaml", manifest)
            with mock.patch("reimagine_pipeline.rendering.ComfyClient",
                            side_effect=AssertionError):
                code = render_media.main([
                    "--output-dir", str(output_dir), "--stage", "videos",
                ])

        self.assertEqual(code, 1)

    def test_force_video_generation_preserves_still_plan(self):
        manifest = PipelineManifest(
            still_mode="manual", item_count=1,
            items=[PipelineItem(
                index=0, item_id="sample", source_path=Path("sample.jpg"),
                source_sha256="a" * 64,
                still=StillSpec(
                    Path("sample.jpg"), 1920, 1088,
                    prompt="A detailed action photograph of a moving subject."),
                video=VideoSpec(
                    Path("sample.mp4"),
                    "The subject moves smoothly while a camera tracks; quiet ambience follows.",
                    "reference", "a" * 64),
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (640, 480)).save(input_dir / "sample.jpg")
            source_hash = sha256_file(input_dir / "sample.jpg")
            item = dataclasses.replace(
                manifest.items[0], source_sha256=source_hash,
                video=dataclasses.replace(
                    manifest.items[0].video, basis_sha256=source_hash))
            manifest.items = [item]
            output_dir = root / "output"
            save_pipeline(output_dir / "pipeline.yaml", manifest)
            llm = mock.Mock()
            llm.describe.return_value = "fake"
            llm.chat.return_value = (
                "<video>The subject settles into controlled motion while the "
                "camera tracks steadily; soft ambient sound is audible.</video>")
            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--output-dir", str(output_dir),
                    "--stage", "videos", "--force",
                ])
            loaded = load_pipeline(output_dir / "pipeline.yaml")

        self.assertEqual(code, 0)
        self.assertEqual(loaded.items[0].still, item.still)

    def test_regenerating_still_invalidates_video_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (640, 480)).save(input_dir / "sample.jpg")
            source_hash = sha256_file(input_dir / "sample.jpg")
            output_dir = root / "output"
            manifest = PipelineManifest(
                still_mode="manual", item_count=1,
                items=[PipelineItem(
                    index=0, item_id="sample",
                    source_path=Path("sample.jpg"),
                    source_sha256=source_hash,
                    still=StillSpec(
                        Path("sample.jpg"), 1920, 1088,
                        prompt="An old detailed still prompt for the subject."),
                    video=VideoSpec(
                        Path("sample.mp4"),
                        "An old motion prompt with camera movement and sound.",
                        "reference", source_hash),
                )],
            )
            save_pipeline(output_dir / "pipeline.yaml", manifest)
            llm = mock.Mock()
            llm.describe.return_value = "fake"
            llm.chat.return_value = (
                "<prompt>A new detailed action photograph of the moving subject."
                "</prompt>")
            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--output-dir", str(output_dir),
                    "--stage", "stills", "--force",
                ])
            loaded = load_pipeline(output_dir / "pipeline.yaml")

        self.assertEqual(code, 0)
        self.assertIsNone(loaded.items[0].video)

    def test_force_all_rebuilds_changed_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (640, 480)).save(input_dir / "new.jpg")
            output_dir = root / "output"
            old = PipelineManifest(
                still_mode="manual", item_count=1,
                items=[PipelineItem(
                    index=0, item_id="old", source_path=Path("old.jpg"),
                    source_sha256="a" * 64,
                )],
            )
            save_pipeline(output_dir / "pipeline.yaml", old)
            llm = mock.Mock()
            llm.describe.return_value = "fake"
            llm.chat.side_effect = [
                "<prompt>A detailed action photograph of a moving subject.</prompt>",
                "<video>The subject moves smoothly while the camera tracks; quiet ambience follows.</video>",
            ]
            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--output-dir", str(output_dir),
                    "--stage", "all", "--force",
                ])
            loaded = load_pipeline(output_dir / "pipeline.yaml")

        self.assertEqual(code, 0)
        self.assertEqual(loaded.items[0].item_id, "new")

    def test_partial_manifest_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            Image.new("RGB", (640, 480)).save(input_dir / "a.jpg")
            Image.new("RGB", (640, 480)).save(input_dir / "b.jpg")
            first_hash = sha256_file(input_dir / "a.jpg")
            output_dir = root / "output"
            partial = PipelineManifest(
                still_mode="manual", item_count=2,
                items=[PipelineItem(
                    index=0, item_id="a", source_path=Path("a.jpg"),
                    source_sha256=first_hash,
                    still=StillSpec(
                        Path("a.jpg"), 1664, 1216,
                        prompt="A detailed action photograph of the first subject."),
                    video=VideoSpec(
                        Path("a.mp4"),
                        "The first subject moves smoothly while the camera tracks; quiet ambience follows.",
                        "reference", first_hash),
                )],
            )
            save_pipeline(output_dir / "pipeline.yaml", partial)
            llm = mock.Mock()
            llm.describe.return_value = "fake"
            llm.chat.side_effect = [
                "<prompt>A detailed action photograph of the second subject.</prompt>",
                "<video>The second subject moves smoothly while the camera tracks; quiet ambience follows.</video>",
            ]
            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--output-dir", str(output_dir), "--stage", "all",
                ])
            loaded = load_pipeline(output_dir / "pipeline.yaml")

        self.assertEqual(code, 0)
        self.assertEqual([item.item_id for item in loaded.items], ["a", "b"])

    def test_partial_folder_tree_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            for folder in ("animals", "sports"):
                (input_dir / folder).mkdir(parents=True)
                Image.new("RGB", (640, 480)).save(
                    input_dir / folder / "sample.jpg")
            output_dir = root / "output"
            first_hash = sha256_file(input_dir / "animals/sample.jpg")
            first = PipelineManifest(
                still_mode="manual", item_count=1,
                items=[PipelineItem(
                    index=0, item_id="animals/sample",
                    source_path=Path("animals/sample.jpg"),
                    source_sha256=first_hash,
                    still=StillSpec(
                        Path("animals/sample.jpg"), 1664, 1216,
                        prompt="A detailed action photograph of an animal."),
                )],
            )
            save_pipeline_folder(
                output_dir, Path("animals"), first, item_count=1)
            llm = mock.Mock()
            llm.describe.return_value = "fake"
            llm.chat.return_value = (
                "<prompt>A detailed action photograph of an athlete.</prompt>")

            with mock.patch.object(generate_prompts, "ROOT", root), \
                    mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--output-dir", str(output_dir), "--stage", "stills",
                ])
            loaded = load_pipeline_tree(output_dir, require_stage="stills")

        self.assertEqual(code, 0)
        self.assertEqual(
            [item.item_id for item in loaded.items],
            ["animals/sample", "sports/sample"])
        llm.chat.assert_called_once()


if __name__ == "__main__":
    unittest.main()
