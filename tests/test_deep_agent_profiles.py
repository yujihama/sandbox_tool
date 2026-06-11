from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

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
        )

        generated_tool = runner.make_deep_agent_profile_tool(profile)

        self.assertEqual(generated_tool.name, "run_analysis_agent")
        self.assertIn("Analysis profile.", generated_tool.description)

    def test_bundled_profiles_load_with_browser_profile(self) -> None:
        profiles = runner.load_deep_agent_profiles(
            [],
            [str(ROOT / "outputs" / "deep_agent_profiles")],
        )
        by_id = {profile.id: profile for profile in profiles}

        self.assertIn("browser_validation", by_id)
        self.assertIn("browser_research", by_id)
        self.assertIn("site_research", by_id)
        self.assertEqual(
            by_id["browser_validation"].image,
            "localhost/python-browser-sandbox:latest",
        )
        self.assertEqual(
            by_id["browser_validation"].tool_name,
            "run_browser_validation_agent",
        )
        self.assertEqual(
            by_id["site_research"].tool_name,
            "run_site_research_agent",
        )
        self.assertEqual(
            by_id["browser_research"].tool_name,
            "run_browser_research_agent",
        )
        self.assertIn("crawl_allowed_site", by_id["site_research"].system_prompt)
        self.assertNotIn(
            "search_houjin_bangou_by_name",
            by_id["site_research"].system_prompt,
        )
        self.assertIn("run_playwright_task", by_id["browser_research"].system_prompt)
        self.assertNotIn("run_browser_use_task", by_id["browser_research"].system_prompt)
        self.assertIn("egress guarded", by_id["browser_research"].system_prompt)
        self.assertEqual(
            by_id["browser_research"].skill_source_specs,
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
                materialized["browser_research"].skill_sources,
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
            self.assertEqual(materialized["site_research"].skill_sources, [])

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
