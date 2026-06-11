from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import openpyxl
import yaml

from sandbox_tool.output_gate import run_output_gate


class OutputGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.raw_root = self.root / "raw_outputs"
        self.clean_root = self.root / "clean_exports"
        self.quarantine_root = self.root / "quarantine"
        self.log_root = self.root / "gate_logs"
        for path in (self.raw_root, self.clean_root, self.quarantine_root, self.log_root):
            path.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def gate(self, *artifacts: str, action: str = "reject") -> dict:
        return run_output_gate(
            raw_root=self.raw_root,
            clean_root=self.clean_root,
            quarantine_root=self.quarantine_root,
            log_root=self.log_root,
            artifacts=list(artifacts),
            run_id="unit-test",
            xlsx_dangerous_formula_action=action,
        )

    def test_markdown_passes(self) -> None:
        (self.raw_root / "report.md").write_text("# Report\n\nNo active content.\n", encoding="utf-8")

        manifest = self.gate("/outputs/report.md")

        self.assertEqual(manifest["overall_status"], "pass")
        self.assertTrue((self.clean_root / "report.md").exists())
        self.assertEqual(manifest["artifacts"][0]["status"], "pass")

    def test_html_script_is_rejected_and_quarantined(self) -> None:
        (self.raw_root / "index.html").write_text("<html><script>alert(1)</script></html>", encoding="utf-8")

        manifest = self.gate("/outputs/index.html")

        self.assertEqual(manifest["overall_status"], "fail")
        artifact = manifest["artifacts"][0]
        self.assertEqual(artifact["status"], "rejected")
        self.assertEqual(artifact["findings"][0]["code"], "script_tag")
        self.assertTrue((self.quarantine_root / "index.html").exists())

    def test_csv_formula_cells_are_escaped(self) -> None:
        (self.raw_root / "data.csv").write_text("name,value\nsafe,10\nbad,=1+1\n", encoding="utf-8")

        manifest = self.gate("/outputs/data.csv")

        self.assertEqual(manifest["overall_status"], "pass")
        self.assertEqual(manifest["artifacts"][0]["status"], "sanitized")
        with (self.clean_root / "data.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[2][1], "'=1+1")

    def test_json_is_canonicalized(self) -> None:
        (self.raw_root / "summary.json").write_text(
            '{"b": 2, "a": {"ok": true}}\n',
            encoding="utf-8",
        )

        manifest = self.gate("/outputs/summary.json")

        self.assertEqual(manifest["overall_status"], "pass")
        artifact = manifest["artifacts"][0]
        self.assertEqual(artifact["status"], "sanitized")
        self.assertIn("canonicalized_json", artifact["actions"])
        parsed = json.loads((self.clean_root / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(parsed, {"a": {"ok": True}, "b": 2})

    def test_invalid_json_is_rejected(self) -> None:
        (self.raw_root / "summary.json").write_text('{"missing": true', encoding="utf-8")

        manifest = self.gate("/outputs/summary.json")

        self.assertEqual(manifest["overall_status"], "fail")
        artifact = manifest["artifacts"][0]
        self.assertEqual(artifact["status"], "rejected")
        self.assertEqual(artifact["findings"][0]["code"], "json_parse_error")
        self.assertTrue((self.quarantine_root / "summary.json").exists())

    def test_yaml_is_canonicalized(self) -> None:
        (self.raw_root / "summary.yaml").write_text(
            "b: 2\na:\n  ok: true\n",
            encoding="utf-8",
        )

        manifest = self.gate("/outputs/summary.yaml")

        self.assertEqual(manifest["overall_status"], "pass")
        artifact = manifest["artifacts"][0]
        self.assertEqual(artifact["status"], "sanitized")
        self.assertIn("canonicalized_yaml", artifact["actions"])
        parsed = yaml.safe_load((self.clean_root / "summary.yaml").read_text(encoding="utf-8"))
        self.assertEqual(parsed, {"a": {"ok": True}, "b": 2})

    def test_yml_alias_is_rejected(self) -> None:
        (self.raw_root / "summary.yml").write_text(
            "defaults: &defaults\n  ok: true\nitem:\n  <<: *defaults\n",
            encoding="utf-8",
        )

        manifest = self.gate("/outputs/summary.yml")

        self.assertEqual(manifest["overall_status"], "fail")
        artifact = manifest["artifacts"][0]
        self.assertEqual(artifact["status"], "rejected")
        self.assertEqual(artifact["findings"][0]["code"], "yaml_anchor_not_allowed")
        self.assertTrue((self.quarantine_root / "summary.yml").exists())

    def test_xlsx_preserves_safe_formulas(self) -> None:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet["A1"] = 1
        sheet["A2"] = 2
        sheet["A3"] = "=SUM(A1:A2)"
        workbook.save(self.raw_root / "safe.xlsx")
        workbook.close()

        manifest = self.gate("/outputs/safe.xlsx")

        self.assertEqual(manifest["overall_status"], "pass")
        clean_workbook = openpyxl.load_workbook(self.clean_root / "safe.xlsx", data_only=False)
        try:
            self.assertEqual(clean_workbook.active["A3"].value, "=SUM(A1:A2)")
        finally:
            clean_workbook.close()

    def test_xlsx_dangerous_formula_is_rejected(self) -> None:
        workbook = openpyxl.Workbook()
        workbook.active["A1"] = '=WEBSERVICE("https://example.com")'
        workbook.save(self.raw_root / "danger.xlsx")
        workbook.close()

        manifest = self.gate("/outputs/danger.xlsx")

        self.assertEqual(manifest["overall_status"], "fail")
        artifact = manifest["artifacts"][0]
        self.assertEqual(artifact["status"], "rejected")
        self.assertEqual(artifact["findings"][0]["code"], "xlsx_dangerous_formula_function")
        self.assertTrue((self.quarantine_root / "danger.xlsx").exists())


if __name__ == "__main__":
    unittest.main()
