from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "outputs"
if str(OUTPUTS) not in sys.path:
    sys.path.insert(0, str(OUTPUTS))

import generic_parent_runner as runner  # noqa: E402


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
                        "deep_recursion_limit: 42",
                        "max_review_rounds: 3",
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
            self.assertEqual(profile.deep_recursion_limit, 42)
            self.assertEqual(profile.max_review_rounds, 3)

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
                [f"{skill_dir}=/input/browser-skills/demo-skill"],
                input_dir,
            )

            self.assertEqual(staged, ["/input/browser-skills"])
            self.assertTrue(
                (input_dir / "browser-skills" / "demo-skill" / "SKILL.md").exists()
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
            toolsets=["review", "file_read", "browser"],
        )
        seal_profile = runner.DeepAgentProfile(
            id="seal",
            tool_name="run_seal_agent",
            description="Seal profile.",
            toolsets=["review", "file_read", "image_inspect"],
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
        self.assertNotIn("crawl_allowed_site", seal_tools)
        self.assertNotIn("run_playwright_task", seal_tools)

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
            ["review", "file_read", "site_crawl", "browser"],
        )
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
        self.assertNotIn("run_browser_use_task", by_id["web_research"].system_prompt)
        self.assertIn("egress guarded", by_id["web_research"].system_prompt)
        self.assertIn("Too Many Requests", by_id["web_research"].system_prompt)
        self.assertEqual(
            by_id["web_research"].skill_source_specs,
            [
                "../skills/houjin-bangou-browser-search=/input/browser-skills/houjin-bangou-browser-search"
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
                materialized["web_research"].skill_sources,
                ["/input/browser-skills"],
            )
            self.assertTrue(
                (
                    input_dir
                    / "browser-skills"
                    / "houjin-bangou-browser-search"
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
            ["review", "file_read", "browser"],
        )
        self.assertEqual(
            explicit_by_id["site_research"].toolsets,
            ["review", "file_read", "site_crawl"],
        )

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

    def test_browser_use_domain_validation_requires_public_allowlist(self) -> None:
        self.assertEqual(
            runner.validate_browser_use_allowed_domains(["houjin-bangou.nta.go.jp"]),
            ["houjin-bangou.nta.go.jp"],
        )
        self.assertEqual(
            runner.validate_browser_use_allowed_domains(["*.example.com"]),
            ["*.example.com"],
        )
        with self.assertRaises(ValueError):
            runner.validate_browser_use_allowed_domains([])
        with self.assertRaises(ValueError):
            runner.validate_browser_use_allowed_domains(["localhost"])
        with self.assertRaises(ValueError):
            runner.validate_browser_use_allowed_domains(["example.*"])

    def test_browser_use_model_normalization_strips_provider_prefix(self) -> None:
        self.assertEqual(runner.normalize_browser_use_model("openai:gpt-5.2"), "gpt-5.2")
        self.assertEqual(runner.normalize_browser_use_model("gpt-5-mini"), "gpt-5-mini")

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
