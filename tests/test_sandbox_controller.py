from __future__ import annotations

import unittest

from fastapi import HTTPException

from sandbox_tool.sandbox_controller import normalize_controller_artifact_path


class SandboxControllerValidationTests(unittest.TestCase):
    def test_controller_normalizes_valid_output_artifact(self) -> None:
        self.assertEqual(
            normalize_controller_artifact_path("/outputs/sub/../report.md"),
            "/outputs/report.md",
        )

    def test_controller_rejects_output_root_traversal(self) -> None:
        for artifact in [
            "/outputs/..",
            "/outputs/sub/..",
            "/outputs/../report.md",
            "/input/report.md",
            "../outputs/report.md",
        ]:
            with self.subTest(artifact=artifact):
                with self.assertRaises(HTTPException):
                    normalize_controller_artifact_path(artifact)


if __name__ == "__main__":
    unittest.main()
