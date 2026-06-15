from __future__ import annotations

import base64
from io import BytesIO
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
if str(OUTPUTS) not in sys.path:
    sys.path.insert(0, str(OUTPUTS))

import generic_parent_runner as runner  # noqa: E402
from sandbox_tool.egress_proxy import verify_egress_token  # noqa: E402


class DeepAgentProfileTests(unittest.TestCase):
    def test_safe_tool_name_normalizes_profile_id(self) -> None:
        self.assertEqual(runner.safe_tool_name("heavy data/profile"), "heavy_data_profile")
        self.assertEqual(runner.safe_tool_name("123"), "run_123")

    def test_profile_loads_yaml_and_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "prompt.txt").write_text("Use this profile carefully.", encoding="utf-8")
            profile_path = root / "profile.yaml"
            profile_path.write_text(
                "\n".join(
                    [
                        "id: demo-profile",
                        "description: Demo profile.",
                        "toolsets:",
                        "  - review",
                        "  - file_read",
                        "system_prompt: Inline prompt.",
                        "system_prompt_file: prompt.txt",
                        "deep_model: openai:gpt-5.2",
                        "vision_model: openai:gpt-5.5",
                        "deep_recursion_limit: 42",
                        "max_review_rounds: 3",
                        "result_mode: inline",
                        "self_check_policy: checklist",
                    ]
                ),
                encoding="utf-8",
            )

            profile = runner.load_deep_agent_profile(profile_path)

            self.assertEqual(profile.id, "demo-profile")
            self.assertEqual(profile.tool_name, "run_demo-profile_agent")
            self.assertEqual(profile.toolsets, ["review", "file_read"])
            self.assertTrue(profile.expose_to_parent)
            self.assertIn("Inline prompt.", profile.system_prompt)
            self.assertIn("Use this profile carefully.", profile.system_prompt)
            self.assertEqual(profile.deep_model, "openai:gpt-5.2")
            self.assertEqual(profile.vision_model, "openai:gpt-5.5")
            self.assertEqual(profile.deep_recursion_limit, 42)
            self.assertEqual(profile.max_review_rounds, 3)
            self.assertEqual(profile.result_mode, "inline")
            self.assertEqual(profile.self_check_policy, "checklist")

    def test_profile_skill_sources_are_staged_relative_to_profile_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile_dir = root / "profiles"
            skills_dir = root / "skills"
            input_dir = root / "run" / "input"
            profile_dir.mkdir(parents=True)
            (skills_dir / "demo-skill").mkdir(parents=True)
            (skills_dir / "demo-skill" / "SKILL.md").write_text(
                "# Demo Skill\n", encoding="utf-8"
            )
            profile_path = profile_dir / "profile.yaml"
            profile_path.write_text(
                "\n".join(
                    [
                        "id: demo",
                        "tool_name: run_demo_agent",
                        "description: Demo profile.",
                        "toolsets:",
                        "  - review",
                        "  - file_read",
                        "skill_sources:",
                        "  - ../skills=/input/profile-skills",
                    ]
                ),
                encoding="utf-8",
            )

            profiles = runner.load_deep_agent_profiles([str(profile_path)], [])
            runner.materialize_deep_agent_profiles(profiles, input_dir, [])

            self.assertEqual(profiles[0].skill_sources, ["/input/profile-skills"])
            self.assertTrue(
                (input_dir / "profile-skills" / "demo-skill" / "SKILL.md").exists()
            )

    def test_single_skill_source_is_staged_under_parent_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill_dir = root / "skills" / "demo-skill"
            input_dir = root / "run" / "input"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Demo Skill\n", encoding="utf-8")

            staged = runner.stage_skill_sources(
                [f"{skill_dir}=/input/profile-skills/demo-skill"],
                input_dir,
            )

            self.assertEqual(staged, ["/input/profile-skills"])
            self.assertTrue(
                (input_dir / "profile-skills" / "demo-skill" / "SKILL.md").exists()
            )

    def test_profile_tool_uses_declared_name_and_description(self) -> None:
        profile = runner.DeepAgentProfile(
            id="analysis",
            tool_name="run_analysis_agent",
            description="Analysis profile.",
            toolsets=["review", "file_read"],
        )

        generated_tool = runner.make_deep_agent_profile_tool(profile)

        self.assertEqual(generated_tool.name, "run_analysis_agent")
        self.assertIn("Analysis profile.", generated_tool.description)
        self.assertIn("Available toolsets: review, file_read", generated_tool.description)

    def test_profile_requires_explicit_known_toolsets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing_path = root / "missing.yaml"
            missing_path.write_text(
                "\n".join(
                    [
                        "id: missing",
                        "description: Missing toolsets.",
                    ]
                ),
                encoding="utf-8",
            )
            unknown_path = root / "unknown.yaml"
            unknown_path.write_text(
                "\n".join(
                    [
                        "id: unknown",
                        "description: Unknown toolset.",
                        "toolsets:",
                        "  - review",
                        "  - does_not_exist",
                    ]
                ),
                encoding="utf-8",
            )
            no_review_path = root / "no_review.yaml"
            no_review_path.write_text(
                "\n".join(
                    [
                        "id: no-review",
                        "description: Missing review.",
                        "toolsets:",
                        "  - file_read",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "non-empty toolsets"):
                runner.load_deep_agent_profile(missing_path)
            with self.assertRaisesRegex(ValueError, "Unknown toolsets"):
                runner.load_deep_agent_profile(unknown_path)
            with self.assertRaisesRegex(ValueError, "review toolset"):
                runner.load_deep_agent_profile(no_review_path)

    def test_profile_rejects_unknown_result_mode_and_self_check_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad_mode = root / "bad_mode.yaml"
            bad_mode.write_text(
                "\n".join(
                    [
                        "id: bad-mode",
                        "description: Bad mode.",
                        "result_mode: sidecar",
                        "toolsets:",
                        "  - review",
                        "  - file_read",
                    ]
                ),
                encoding="utf-8",
            )
            bad_policy = root / "bad_policy.yaml"
            bad_policy.write_text(
                "\n".join(
                    [
                        "id: bad-policy",
                        "description: Bad policy.",
                        "self_check_policy: exhaustive",
                        "toolsets:",
                        "  - review",
                        "  - file_read",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "result_mode"):
                runner.load_deep_agent_profile(bad_mode)
            with self.assertRaisesRegex(ValueError, "self_check_policy"):
                runner.load_deep_agent_profile(bad_policy)

    def test_profile_disallows_file_read_without_full_input_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile_path = root / "network.yaml"
            profile_path.write_text(
                "\n".join(
                    [
                        "id: network",
                        "description: Network profile.",
                        "input_access: skills_only",
                        "toolsets:",
                        "  - review",
                        "  - file_read",
                        "  - browser",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must not include the file_read toolset"):
                runner.load_deep_agent_profile(profile_path)

    def test_deep_agent_tools_are_profile_scoped(self) -> None:
        site_profile = runner.DeepAgentProfile(
            id="site",
            tool_name="run_site_agent",
            description="Site profile.",
            toolsets=["review", "file_read", "site_crawl"],
        )
        browser_profile = runner.DeepAgentProfile(
            id="browser",
            tool_name="run_browser_agent",
            description="Browser profile.",
            toolsets=["review", "browser"],
        )
        seal_profile = runner.DeepAgentProfile(
            id="seal",
            tool_name="run_seal_agent",
            description="Seal profile.",
            toolsets=["review", "file_read", "image_inspect"],
            result_mode="inline",
        )

        site_tools = {tool.name for tool in runner.deep_agent_tools_for_profile(site_profile)}
        browser_tools = {
            tool.name for tool in runner.deep_agent_tools_for_profile(browser_profile)
        }
        seal_tools = {tool.name for tool in runner.deep_agent_tools_for_profile(seal_profile)}

        self.assertIn("crawl_allowed_site", site_tools)
        self.assertNotIn("run_playwright_task", site_tools)
        self.assertIn("run_playwright_task", browser_tools)
        self.assertNotIn("crawl_allowed_site", browser_tools)
        self.assertIn("inspect_sandbox_image", seal_tools)
        self.assertNotIn("request_parent_review", seal_tools)
        self.assertNotIn("crawl_allowed_site", seal_tools)
        self.assertNotIn("run_playwright_task", seal_tools)

    def test_inline_profile_system_prompt_disables_review_contract(self) -> None:
        profile = runner.DeepAgentProfile(
            id="seal",
            tool_name="run_seal_agent",
            description="Seal profile.",
            toolsets=["review", "file_read", "image_inspect"],
            result_mode="inline",
            self_check_policy="checklist",
        )

        prompt = runner.build_deep_agent_system_prompt(profile)

        self.assertIn("disabled for inline result mode", prompt)
        self.assertIn("Do not create final reviewed artifacts", prompt)
        self.assertIn("do not call request_parent_review", prompt)

    def test_graceful_finalize_filters_to_completion_tools(self) -> None:
        middleware = runner.GracefulFinalizeMiddleware(
            profile_id="web_research",
            expected_artifacts=["/outputs/result.csv"],
            warning_model_calls=3,
            finalize_model_calls=4,
            warning_tool_calls=3,
            finalize_tool_calls=4,
            warning_message_count=10,
            finalize_message_count=12,
        )

        tools = [
            SimpleNamespace(name="crawl_allowed_site"),
            SimpleNamespace(name="run_playwright_task"),
            SimpleNamespace(name="read_crawled_page"),
            SimpleNamespace(name="write_file"),
            SimpleNamespace(name="execute"),
            {"function": {"name": "request_parent_review"}},
        ]

        kept = middleware.filter_finalize_tools(tools)
        kept_names = [middleware._tool_name(item) for item in kept]

        self.assertEqual(kept_names, ["write_file", "execute", "request_parent_review"])

    def test_graceful_finalize_instruction_names_artifacts_and_review(self) -> None:
        middleware = runner.GracefulFinalizeMiddleware(
            profile_id="web_research",
            expected_artifacts=["/outputs/result.csv"],
            warning_model_calls=3,
            finalize_model_calls=4,
            warning_tool_calls=3,
            finalize_tool_calls=4,
            warning_message_count=10,
            finalize_message_count=12,
        )

        instruction = middleware.finalize_instruction(model_calls=4, message_count=12)

        self.assertIn("/outputs/result.csv", instruction)
        self.assertIn("/outputs/subtasks/self_check_plan.md", instruction)
        self.assertIn("/outputs/subtasks/self_check_report.md", instruction)
        self.assertIn("request_parent_review", instruction)
        self.assertIn("Do not perform new crawling", instruction)

    def test_graceful_finalize_instruction_supports_inline_result_mode(self) -> None:
        middleware = runner.GracefulFinalizeMiddleware(
            profile_id="seal_vision",
            expected_artifacts=[],
            result_mode="inline",
            self_check_policy="checklist",
            warning_model_calls=3,
            finalize_model_calls=4,
            warning_tool_calls=3,
            finalize_tool_calls=4,
            warning_message_count=10,
            finalize_message_count=12,
        )

        instruction = middleware.finalize_instruction(model_calls=4, message_count=12)

        self.assertIn("inline answer", instruction)
        self.assertIn("Do not create final reviewed artifacts", instruction)
        self.assertIn("do not call request_parent_review", instruction)

    def test_graceful_finalize_blocks_exploration_tool_calls(self) -> None:
        middleware = runner.GracefulFinalizeMiddleware(
            profile_id="web_research",
            expected_artifacts=["/outputs/result.csv"],
            warning_model_calls=3,
            finalize_model_calls=4,
            warning_tool_calls=1,
            finalize_tool_calls=2,
            warning_message_count=10,
            finalize_message_count=12,
        )
        request = SimpleNamespace(
            tool=SimpleNamespace(name="crawl_allowed_site"),
            tool_call={"id": "call-1", "name": "crawl_allowed_site"},
            state={"graceful_finalize_model_calls": 0, "messages": [None] * 12},
        )

        def should_not_execute(_: object) -> object:
            raise AssertionError("blocked exploration tool should not execute")

        result = middleware.wrap_tool_call(request, should_not_execute)

        self.assertEqual(result.tool_call_id, "call-1")
        self.assertIn("graceful_finalize_blocked_tool", result.content)

    def test_graceful_finalize_thresholds_are_derived_from_recursion_limit(self) -> None:
        thresholds = runner.graceful_finalize_thresholds(120)

        self.assertEqual(thresholds["warning_model_calls"], 22)
        self.assertEqual(thresholds["finalize_model_calls"], 30)
        self.assertEqual(thresholds["warning_tool_calls"], 18)
        self.assertEqual(thresholds["finalize_tool_calls"], 24)
        self.assertEqual(thresholds["warning_message_count"], 66)
        self.assertEqual(thresholds["finalize_message_count"], 84)

    def test_expected_artifacts_can_be_empty_only_when_allowed(self) -> None:
        old_config = runner.CONFIG
        try:
            runner.CONFIG = SimpleNamespace(expected_artifacts=["/outputs/result.md"])

            self.assertEqual(
                runner.normalize_tool_expected_artifacts([], allow_empty=True),
                [],
            )
            with self.assertRaisesRegex(ValueError, "at least one artifact"):
                runner.normalize_tool_expected_artifacts([], allow_empty=False)
        finally:
            runner.CONFIG = old_config

    def test_inspect_sandbox_image_schema_hides_detail_and_sends_original(self) -> None:
        from PIL import Image

        schema = runner.inspect_sandbox_image.args_schema.model_json_schema()
        self.assertNotIn("detail", schema["properties"])

        old_config = runner.CONFIG
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                input_dir = root / "input"
                output_dir = root / "outputs"
                export_dir = root / "exports"
                input_dir.mkdir()
                output_dir.mkdir()
                export_dir.mkdir()
                image_path = input_dir / "sample.png"
                Image.new("RGB", (2, 2), "red").save(image_path)

                runner.CONFIG = SimpleNamespace(
                    input_dir=input_dir,
                    output_dir=output_dir,
                    clean_export_dir=export_dir,
                    deep_model="openai:gpt-5.4",
                    vision_model="",
                )

                with patch.object(runner, "OpenAI") as openai_cls:
                    client = openai_cls.return_value
                    client.responses.create.return_value = SimpleNamespace(
                        output_text="ok"
                    )

                    result = runner.inspect_sandbox_image.invoke(
                        {
                            "path": "/input/sample.png",
                            "question": "read",
                        }
                    )

                _, kwargs = client.responses.create.call_args
                image_payload = kwargs["input"][0]["content"][1]
                self.assertEqual(image_payload["detail"], "original")
                self.assertEqual(result["detail"], "original")
                self.assertNotIn("requested_detail", result)
                self.assertNotIn("detail_source", result)
        finally:
            runner.CONFIG = old_config

    def test_inspect_sandbox_image_adds_padding_before_send(self) -> None:
        from PIL import Image

        old_config = runner.CONFIG
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                input_dir = root / "input"
                output_dir = root / "outputs"
                export_dir = root / "exports"
                input_dir.mkdir()
                output_dir.mkdir()
                export_dir.mkdir()
                image_path = input_dir / "tight.png"
                Image.new("RGB", (4, 4), "black").save(image_path)

                runner.CONFIG = SimpleNamespace(
                    input_dir=input_dir,
                    output_dir=output_dir,
                    clean_export_dir=export_dir,
                    deep_model="openai:gpt-5.4",
                    vision_model="",
                )

                with patch.object(runner, "OpenAI") as openai_cls:
                    client = openai_cls.return_value
                    client.responses.create.return_value = SimpleNamespace(
                        output_text="ok"
                    )

                    result = runner.inspect_sandbox_image.invoke(
                        {
                            "path": "/input/tight.png",
                            "question": "read",
                        }
                    )

                _, kwargs = client.responses.create.call_args
                image_payload = kwargs["input"][0]["content"][1]
                prefix, encoded = image_payload["image_url"].split(",", 1)
                self.assertEqual(prefix, "data:image/png;base64")
                sent = Image.open(BytesIO(base64.b64decode(encoded)))
                self.assertEqual(sent.size, (204, 204))
                self.assertEqual(result["vision_transform"]["transform"], "pad_to_min_margin")
                self.assertEqual(result["vision_transform"]["padding"]["min_margin"], 100)
                self.assertEqual(result["vision_transform"]["padding"]["padding"]["left"], 100)
                self.assertEqual(result["vision_transform"]["padding"]["padding"]["right"], 100)
                self.assertEqual(result["vision_transform"]["sent_width"], 204)
                self.assertEqual(result["vision_transform"]["sent_height"], 204)
                self.assertTrue((output_dir / "_vision_prepared").exists())
        finally:
            runner.CONFIG = old_config

    def test_inspect_sandbox_image_tiles_small_images_when_configured(self) -> None:
        from PIL import Image

        old_config = runner.CONFIG
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                input_dir = root / "input"
                output_dir = root / "outputs"
                export_dir = root / "exports"
                input_dir.mkdir()
                output_dir.mkdir()
                export_dir.mkdir()
                image_path = input_dir / "tiny.png"
                Image.new("RGB", (2, 3), "red").save(image_path)

                runner.CONFIG = SimpleNamespace(
                    input_dir=input_dir,
                    output_dir=output_dir,
                    clean_export_dir=export_dir,
                    deep_model="openai:gpt-5.4",
                    vision_model="",
                    tile_small_images_for_vision=True,
                    vision_tile_max_side=16,
                    vision_tile_grid=5,
                )

                with patch.object(runner, "OpenAI") as openai_cls:
                    client = openai_cls.return_value
                    client.responses.create.return_value = SimpleNamespace(
                        output_text="ok"
                    )

                    result = runner.inspect_sandbox_image.invoke(
                        {
                            "path": "/input/tiny.png",
                            "question": "read",
                        }
                    )

                _, kwargs = client.responses.create.call_args
                image_payload = kwargs["input"][0]["content"][1]
                prefix, encoded = image_payload["image_url"].split(",", 1)
                self.assertEqual(prefix, "data:image/png;base64")
                sent = Image.open(BytesIO(base64.b64decode(encoded)))
                self.assertEqual(sent.size, (1010, 1015))
                self.assertEqual(image_payload["detail"], "original")
                self.assertEqual(result["sent_mime"], "image/png")
                self.assertEqual(result["vision_transform"]["transform"], "tile_5x5")
                self.assertEqual(result["vision_transform"]["source_width"], 2)
                self.assertEqual(result["vision_transform"]["source_height"], 3)
                self.assertTrue(result["vision_transform"]["padding"]["applied"])
                self.assertEqual(result["vision_transform"]["padding"]["min_margin"], 100)
                self.assertEqual(result["vision_transform"]["padding"]["padded_width"], 202)
                self.assertEqual(result["vision_transform"]["padding"]["padded_height"], 203)
                self.assertEqual(result["vision_transform"]["sent_width"], 1010)
                self.assertEqual(result["vision_transform"]["sent_height"], 1015)
                self.assertTrue((output_dir / "_vision_tiles").exists())
        finally:
            runner.CONFIG = old_config

    def test_extract_skill_usage_from_trace_detects_read_and_execute(self) -> None:
        usage = runner.extract_skill_usage_from_trace(
            [
                {
                    "index": 2,
                    "tool_call_args": [
                        {
                            "name": "read_file",
                            "args": {
                                "file_path": "/input/skills/seal-surname-identification/SKILL.md"
                            },
                        }
                    ],
                },
                {
                    "index": 5,
                    "tool_call_args": [
                        {
                            "name": "execute",
                            "args": {
                                "command": (
                                    "python /input/skills/seal-surname-identification/"
                                    "scripts/seal_preprocess.py /input/test05.png"
                                )
                            },
                        }
                    ],
                },
            ],
            ["/input/skills"],
        )

        self.assertTrue(usage["configured"])
        self.assertTrue(usage["referenced"])
        self.assertTrue(usage["executed"])
        self.assertEqual(usage["skill_names"], ["seal-surname-identification"])
        self.assertEqual(
            usage["references"][0]["path"],
            "/input/skills/seal-surname-identification/SKILL.md",
        )
        self.assertIn("seal_preprocess.py", usage["executions"][0]["command"])

    def test_active_vision_model_prefers_active_then_config_then_deep_model(self) -> None:
        old_config = runner.CONFIG
        old_active = runner.ACTIVE_DEEP_AGENT_VISION_MODEL
        try:
            runner.CONFIG = SimpleNamespace(
                vision_model="openai:gpt-5.4",
                deep_model="openai:gpt-5.2",
            )
            runner.ACTIVE_DEEP_AGENT_VISION_MODEL = "openai:gpt-5.5"
            self.assertEqual(runner.active_vision_model_name(), "gpt-5.5")

            runner.ACTIVE_DEEP_AGENT_VISION_MODEL = ""
            self.assertEqual(runner.active_vision_model_name(), "gpt-5.4")

            runner.CONFIG.vision_model = ""
            self.assertEqual(runner.active_vision_model_name(), "gpt-5.2")
        finally:
            runner.CONFIG = old_config
            runner.ACTIVE_DEEP_AGENT_VISION_MODEL = old_active

    def test_hidden_profiles_are_skipped_from_profile_dir_but_explicit_load_works(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hidden_path = root / "hidden.yaml"
            hidden_path.write_text(
                "\n".join(
                    [
                        "id: hidden",
                        "description: Hidden profile.",
                        "expose_to_parent: false",
                        "toolsets:",
                        "  - review",
                        "  - file_read",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(runner.load_deep_agent_profiles([], [str(root)]), [])
            explicit = runner.load_deep_agent_profiles([str(hidden_path)], [])
            self.assertEqual([profile.id for profile in explicit], ["hidden"])

    def test_profile_input_dir_skills_only_excludes_user_inputs(self) -> None:
        old_config = runner.CONFIG
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                input_dir = root / "input"
                run_root = root / "run"
                (input_dir / "profile-skills" / "demo").mkdir(parents=True)
                (input_dir / "profile-skills" / "demo" / "SKILL.md").write_text(
                    "# Demo\n", encoding="utf-8"
                )
                (input_dir / "secret.csv").write_text("secret,value\n", encoding="utf-8")
                run_root.mkdir()
                runner.CONFIG = SimpleNamespace(input_dir=input_dir, run_root=run_root)
                profile = runner.DeepAgentProfile(
                    id="web",
                    tool_name="run_web_agent",
                    description="Web profile.",
                    toolsets=["review", "browser"],
                    input_access="skills_only",
                    skill_sources=["/input/profile-skills"],
                )

                profile_dir = runner.profile_input_dir(profile)

                self.assertTrue(
                    (profile_dir / "profile-skills" / "demo" / "SKILL.md").exists()
                )
                self.assertFalse((profile_dir / "secret.csv").exists())
        finally:
            runner.CONFIG = old_config

    def test_bundled_profiles_load_with_browser_profile(self) -> None:
        profiles = runner.load_deep_agent_profiles(
            [],
            [str(ROOT / "outputs" / "deep_agent_profiles")],
        )
        by_id = {profile.id: profile for profile in profiles}

        self.assertIn("browser_validation", by_id)
        self.assertIn("web_research", by_id)
        self.assertNotIn("browser_research", by_id)
        self.assertNotIn("site_research", by_id)
        self.assertEqual(
            by_id["quick_eval"].toolsets,
            ["review", "file_read", "image_inspect"],
        )
        self.assertEqual(by_id["quick_eval"].result_mode, "inline")
        self.assertEqual(by_id["quick_eval"].self_check_policy, "checklist")
        self.assertNotIn("request_parent_review", runner.tool_names_for_profile(by_id["quick_eval"]))
        self.assertEqual(
            by_id["quick_eval"].skill_source_specs,
            [
                "../skills/table-image-extraction=/input/profile-skills/table-image-extraction"
            ],
        )
        self.assertEqual(by_id["seal_vision"].result_mode, "inline")
        self.assertEqual(by_id["seal_vision"].self_check_policy, "checklist")
        self.assertEqual(by_id["seal_vision"].deep_model, "openai:gpt-5.4")
        self.assertEqual(by_id["seal_vision"].vision_model, "openai:gpt-5.4")
        self.assertNotIn("request_parent_review", runner.tool_names_for_profile(by_id["seal_vision"]))
        self.assertEqual(
            by_id["document_artifact"].toolsets,
            ["review", "file_read", "image_inspect"],
        )
        self.assertEqual(
            by_id["heavy_data_analysis"].toolsets,
            ["review", "file_read", "image_inspect"],
        )
        self.assertEqual(
            by_id["web_research"].toolsets,
            ["review", "site_crawl", "browser"],
        )
        self.assertEqual(by_id["web_research"].input_access, "skills_only")
        self.assertEqual(by_id["web_research"].result_mode, "artifact")
        self.assertEqual(by_id["web_research"].self_check_policy, "checklist")
        self.assertEqual(by_id["browser_validation"].input_access, "none")
        self.assertEqual(
            by_id["web_research"].tool_name,
            "run_web_research_agent",
        )
        self.assertEqual(
            by_id["browser_validation"].image,
            "localhost/python-browser-sandbox:latest",
        )
        self.assertEqual(
            by_id["browser_validation"].tool_name,
            "run_browser_validation_agent",
        )
        self.assertIn("crawler-first", by_id["web_research"].system_prompt)
        self.assertIn("run_playwright_task", by_id["web_research"].system_prompt)
        self.assertNotIn(
            "search_houjin_bangou_by_name",
            by_id["web_research"].system_prompt,
        )
        self.assertIn("egress guarded", by_id["web_research"].system_prompt)
        self.assertIn("Too Many Requests", by_id["web_research"].system_prompt)
        self.assertEqual(
            by_id["web_research"].skill_source_specs,
            [
                "../skills/company-info-search=/input/profile-skills/company-info-search"
            ],
        )

        with tempfile.TemporaryDirectory() as temp:
            input_dir = Path(temp) / "input"
            materialized_profiles = runner.load_deep_agent_profiles(
                [],
                [str(ROOT / "outputs" / "deep_agent_profiles")],
            )
            runner.materialize_deep_agent_profiles(materialized_profiles, input_dir, [])
            materialized = {profile.id: profile for profile in materialized_profiles}
            self.assertEqual(
                materialized["quick_eval"].skill_sources,
                ["/input/profile-skills"],
            )
            self.assertTrue(
                (
                    input_dir
                    / "profile-skills"
                    / "table-image-extraction"
                    / "SKILL.md"
                ).exists()
            )
            self.assertEqual(
                materialized["web_research"].skill_sources,
                ["/input/profile-skills"],
            )
            self.assertTrue(
                (
                    input_dir
                    / "profile-skills"
                    / "company-info-search"
                    / "SKILL.md"
                ).exists()
            )
            self.assertEqual(materialized["browser_validation"].skill_sources, [])

        explicit_profiles = runner.load_deep_agent_profiles(
            [
                str(ROOT / "outputs" / "deep_agent_profiles" / "browser_research.yaml"),
                str(ROOT / "outputs" / "deep_agent_profiles" / "site_research.yaml"),
            ],
            [],
        )
        explicit_by_id = {profile.id: profile for profile in explicit_profiles}
        self.assertFalse(explicit_by_id["browser_research"].expose_to_parent)
        self.assertFalse(explicit_by_id["site_research"].expose_to_parent)
        self.assertEqual(
            explicit_by_id["browser_research"].toolsets,
            ["review", "browser"],
        )
        self.assertEqual(explicit_by_id["browser_research"].input_access, "skills_only")
        self.assertEqual(
            explicit_by_id["site_research"].toolsets,
            ["review", "site_crawl"],
        )
        self.assertEqual(explicit_by_id["site_research"].input_access, "none")

    def test_playwright_delay_clamping(self) -> None:
        self.assertEqual(
            runner.clamp_playwright_delay_ms(None, default_ms=1000, max_ms=10000),
            1000,
        )
        self.assertEqual(
            runner.clamp_playwright_delay_ms(-5, default_ms=1000, max_ms=10000),
            0,
        )
        self.assertEqual(
            runner.clamp_playwright_delay_ms(20000, default_ms=1000, max_ms=10000),
            10000,
        )
        self.assertEqual(
            runner.clamp_playwright_delay_ms("bad", default_ms=1000, max_ms=10000),
            1000,
        )

    def test_playwright_url_allowlist_blocks_private_and_cross_domain(self) -> None:
        domains = runner.validate_public_allowed_domains(
            ["houjin-bangou.nta.go.jp", "*.example.com"],
            tool_name="Playwright",
        )
        self.assertTrue(
            runner.is_url_allowed_by_domains(
                "https://www.example.com/path",
                domains,
            )
        )
        self.assertTrue(
            runner.is_url_allowed_by_domains(
                "https://houjin-bangou.nta.go.jp/",
                domains,
            )
        )
        self.assertFalse(
            runner.is_url_allowed_by_domains(
                "https://evil.example.net/",
                domains,
            )
        )
        with self.assertRaises(ValueError):
            runner.validate_public_allowed_domains(["localhost"], tool_name="Playwright")

    def test_network_tool_proxy_url_uses_signed_allowlist_token(self) -> None:
        old_config = runner.CONFIG
        try:
            runner.CONFIG = SimpleNamespace(
                egress_proxy_url="http://egress-proxy:8888",
                egress_proxy_signing_secret="unit-secret",
            )

            proxy_url = runner.network_tool_proxy_url(
                ["houjin-bangou.nta.go.jp"],
                purpose="unit-test",
            )
            parsed = runner.urllib.parse.urlsplit(proxy_url)
            token = runner.urllib.parse.unquote(parsed.username or "")

            self.assertEqual(parsed.hostname, "egress-proxy")
            self.assertEqual(parsed.port, 8888)
            self.assertEqual(
                verify_egress_token(token, "unit-secret"),
                ["houjin-bangou.nta.go.jp"],
            )
            self.assertEqual(
                runner.playwright_proxy_settings(proxy_url)["server"],
                "http://egress-proxy:8888",
            )
        finally:
            runner.CONFIG = old_config

    def test_playwright_egress_allows_short_search_values(self) -> None:
        runner.validate_playwright_steps_egress(
            [
                {"action": "fill", "selector": "#corp_name", "value": "Toyota Motor Corporation"},
                {"action": "fill", "selector": "#corp_name", "value": "トヨタ自動車"},
                {"action": "goto", "url": "https://www.houjin-bangou.nta.go.jp/kensaku-kekka.html?selHouzinNo=9180001059935"},
                {"action": "press", "selector": "#corp_name", "key": "Enter"},
            ],
            allowed_domains=["www.houjin-bangou.nta.go.jp"],
        )

    def test_playwright_egress_rejects_large_or_secret_fill_values(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_playwright_steps_egress(
                [{"action": "fill", "selector": "textarea", "value": "x" * 121}],
                allowed_domains=["example.com"],
            )
        with self.assertRaises(ValueError):
            runner.validate_playwright_steps_egress(
                [
                    {
                        "action": "fill",
                        "selector": "input",
                        "value": "OPENAI_API_KEY=sk-proj-" + "A" * 40,
                    }
                ],
                allowed_domains=["example.com"],
            )
        with self.assertRaises(ValueError):
            runner.validate_playwright_steps_egress(
                [
                    {
                        "action": "fill",
                        "selector": "input",
                        "value": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/==",
                    }
                ],
                allowed_domains=["example.com"],
            )

    def test_playwright_egress_rejects_long_query_and_unsafe_key(self) -> None:
        with self.assertRaises(ValueError):
            runner.validate_playwright_steps_egress(
                [
                    {
                        "action": "goto",
                        "url": "https://example.com/search?q=" + ("x" * 181),
                    }
                ],
                allowed_domains=["example.com"],
            )
        with self.assertRaises(ValueError):
            runner.validate_playwright_steps_egress(
                [{"action": "press", "key": "Control+V"}],
                allowed_domains=["example.com"],
            )


if __name__ == "__main__":
    unittest.main()
