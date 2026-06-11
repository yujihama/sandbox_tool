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

    def test_profile_tool_uses_declared_name_and_description(self) -> None:
        profile = runner.DeepAgentProfile(
            id="analysis",
            tool_name="run_analysis_agent",
            description="Analysis profile.",
        )

        generated_tool = runner.make_deep_agent_profile_tool(profile)

        self.assertEqual(generated_tool.name, "run_analysis_agent")
        self.assertIn("Analysis profile.", generated_tool.description)


if __name__ == "__main__":
    unittest.main()
