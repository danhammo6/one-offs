import dataclasses
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from PIL import Image
import yaml

from reimagine_pipeline.manifest import (
    load_pipeline, load_pipeline_tree, load_render_state_tree, save_pipeline,
    save_pipeline_tree, save_render_state, save_render_state_tree,
)
from reimagine_pipeline.models import PipelineItem, PipelineManifest, StillSpec, VideoSpec
from reimagine_pipeline.files import iter_images, sha256_file
from reimagine_pipeline.llm import ClaudeCodeLLM, OpenAILLM
from reimagine_pipeline.workflows import patch_ltx_workflow, pick_artifact
from reimagine_pipeline.comfy import ComfyArtifact
from reimagine_pipeline.manifest import load_render_state
from reimagine_pipeline.prompting import (
    generate_still_prompt, generate_tagged, generate_video_prompt,
    load_system_prompt, video_prompt_word_range,
)
from reimagine_pipeline.rendering import _read_still_output

import generate_prompts
import render_media


class PipelineManifestTests(unittest.TestCase):
    def test_round_trip_preserves_still_and_video_specs(self):
        manifest = PipelineManifest(
            still_mode="manual",
            item_count=1,
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
        manifest = PipelineManifest("manual", 2, items)

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

        self.assertEqual([item.item_id for item in loaded.items],
                         ["animals/cat", "sports/run"])

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
            '<regions>\nelements: []\n</regions>',
            '<regions>\nhigh_level_description: "A moving subject"\n'
            'elements:\n  - type: "obj"\n    desc: "subject"\n'
            '    x: 0.1\n    y: 0.1\n    w: 0.5\n    h: 0.5\n'
            '</regions>',
        ]

        result = generate_still_prompt(
            llm, Path("/tmp/sample.jpg"), "regions")

        self.assertEqual(result["high_level_description"], "A moving subject")
        self.assertEqual(llm.chat.call_count, 2)
        self.assertIsNone(llm.chat.call_args_list[0].kwargs["correction"])
        self.assertIn("high_level_description is required",
                      llm.chat.call_args_list[1].kwargs["correction"])
        self.assertIn("elements: []",
                      llm.chat.call_args_list[1].kwargs["correction"])

    def test_region_yaml_formatting_error_is_sent_back_for_correction(self):
        llm = mock.Mock()
        malformed = (
            '<regions>\nhigh_level_description: "A moving subject"\n'
            'elements:\n  - type: "obj"\n    desc: [broken\n</regions>')
        llm.chat.side_effect = [
            malformed,
            '<regions>\nhigh_level_description: "A moving subject"\n'
            'elements:\n  - type: "obj"\n    desc: "subject"\n'
            '    x: 0.1\n    y: 0.1\n    w: 0.5\n    h: 0.5\n'
            '</regions>',
        ]

        result = generate_still_prompt(
            llm, Path("/tmp/sample.jpg"), "regions")

        correction = llm.chat.call_args_list[1].kwargs["correction"]
        self.assertEqual(result["high_level_description"], "A moving subject")
        self.assertIn("invalid region YAML", correction)
        self.assertIn(malformed, correction)
        self.assertIn("region YAML spec", llm.chat.call_args_list[0].args[1])

    def test_region_system_prompt_requires_compact_output_only_yaml(self):
        prompt = load_system_prompt("system_regions.txt")

        self.assertIn("first output token must be <regions>", prompt)
        self.assertIn("under 700 words", prompt)
        self.assertIn("2 to 6 useful regions", prompt)
        self.assertIn("do not use JSON braces or Markdown code fences", prompt)

    def test_region_validation_exhausts_exact_retry_budget(self):
        llm = mock.Mock()
        llm.chat.return_value = '<regions>\nelements: []\n</regions>'

        with self.assertLogs("reimagine_pipeline.prompting", "WARNING") as logs, \
                self.assertRaisesRegex(RuntimeError, "after 3 tries"):
            generate_still_prompt(llm, Path("/tmp/sample.jpg"), "regions")

        self.assertEqual(llm.chat.call_count, 3)
        self.assertEqual(len(logs.output), 3)
        self.assertIn("attempt 3/3", logs.output[-1])

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

    def test_missing_custom_prompt_reports_full_path(self):
        missing = Path("/tmp/reimagine-missing-prompts/system_manual.txt")

        with self.assertRaisesRegex(ValueError, str(missing)):
            load_system_prompt("system_manual.txt", missing.parent)

    def test_openai_retry_payload_keeps_correction_after_image(self):
        client = OpenAILLM("127.0.0.1:9503", model="test")
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
        self.assertEqual(content[0], {"type": "text", "text": "original"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[2], {"type": "text", "text": "retry"})

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
        with mock.patch("reimagine_pipeline.llm.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = envelope
            run.return_value.stderr = ""
            client.chat("system", "request", Path("/tmp/frame.jpg"))

        self.assertIn("/tmp/frame.jpg", run.call_args.kwargs["input"])

    def test_parsers_show_defaults_and_prompt_prefix(self):
        prompt_args = generate_prompts.build_parser().parse_args([])
        self.assertEqual(prompt_args.prompt_path_prefix, Path("prompts"))
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
            with mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm), \
                    mock.patch("reimagine_pipeline.comfy.ComfyClient",
                               side_effect=AssertionError):
                code = generate_prompts.main([
                    "--input-dir", str(input_dir),
                    "--output-dir", str(root / "output"),
                    "--stage", "all",
                    "--video-basis", "reference",
                ])

            manifest = load_pipeline(root / "output" / "pipeline.yaml")

        self.assertEqual(code, 0)
        self.assertIsNotNone(manifest.items[0].still)
        self.assertIsNotNone(manifest.items[0].video)

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

            with mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--input-dir", str(input_dir),
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
            output_dir = Path(tmp)
            save_pipeline(output_dir / "pipeline.yaml", manifest)
            artifact = ComfyArtifact("159", "sample.jpeg", "", "output")
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
                ])
            workflow = fake.run_workflow.call_args.args[0]

        self.assertEqual(code, 0)
        self.assertEqual(workflow["78:75"]["inputs"]["seed"], 100)

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
            with mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--input-dir", str(input_dir),
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
            with mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--input-dir", str(input_dir),
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
            with mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--input-dir", str(input_dir),
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
            with mock.patch.object(generate_prompts, "build_llm",
                                   return_value=llm):
                code = generate_prompts.main([
                    "--input-dir", str(input_dir),
                    "--output-dir", str(output_dir), "--stage", "all",
                ])
            loaded = load_pipeline(output_dir / "pipeline.yaml")

        self.assertEqual(code, 0)
        self.assertEqual([item.item_id for item in loaded.items], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
