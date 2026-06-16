from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "outputs"
    / "skills"
    / "seal-surname-identification"
    / "scripts"
    / "seal_preprocess.py"
)


class SealPreprocessTests(unittest.TestCase):
    def test_preprocess_writes_angle_candidates_and_corrected_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_path = root / "stamp.png"
            output_dir = root / "out"

            image = Image.new("RGB", (160, 160), "white")
            draw = ImageDraw.Draw(image)
            draw.ellipse((20, 20, 140, 140), outline=(190, 20, 30), width=8)
            draw.line((80, 45, 80, 115), fill=(190, 20, 30), width=8)
            draw.line((55, 80, 105, 80), fill=(190, 20, 30), width=8)
            image = image.rotate(10, resample=Image.Resampling.BICUBIC, fillcolor="white")
            image.save(image_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output_dir),
                    "--scale",
                    "1",
                    "--angles=-10,0,10",
                    str(image_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            summary_path = output_dir / "seal_processing_summary.json"
            self.assertTrue(summary_path.exists())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

            self.assertTrue((output_dir / "seal_contact_sheet.png").exists())
            self.assertTrue((output_dir / "seal_angle_contact_sheet.png").exists())
            self.assertEqual(summary["angles"], [0.0, -10.0, 10.0])
            item = summary["images"][0]
            self.assertIn("best_angle_deg", item)
            self.assertIn("angle_candidates", item)
            self.assertEqual(len(item["angle_candidates"]), 3)
            self.assertTrue(Path(item["angle_corrected_crop_path"]).exists())
            self.assertTrue(Path(item["angle_corrected_bw_path"]).exists())
            for candidate in item["angle_candidates"]:
                self.assertTrue(Path(candidate["bw_path"]).exists())
                self.assertIn("score", candidate)

    def test_split_stamps_preprocesses_detected_stamp_crops(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_path = root / "multi.png"
            output_dir = root / "out"

            image = Image.new("RGB", (360, 120), "white")
            draw = ImageDraw.Draw(image)
            for index, center_x in enumerate([45, 115, 185, 255, 325]):
                draw.ellipse(
                    (center_x - 24, 36, center_x + 24, 84),
                    outline=(190, 20, 30),
                    width=5,
                )
                draw.line(
                    (center_x, 47, center_x, 73),
                    fill=(190, 20, 30),
                    width=4,
                )
                if index % 2 == 0:
                    draw.line(
                        (center_x - 12, 60, center_x + 12, 60),
                        fill=(190, 20, 30),
                        width=4,
                    )
            image.save(image_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output_dir),
                    "--scale",
                    "1",
                    "--split-stamps",
                    "--angles=-5,0,5",
                    str(image_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(
                (output_dir / "seal_processing_summary.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertTrue(summary["split_stamps_enabled"])
            self.assertEqual(summary["count"], 5)
            self.assertEqual(summary["split_sources"][0]["split_count"], 5)
            self.assertTrue((output_dir / "split_stamps").exists())
            self.assertTrue((output_dir / "seal_contact_sheet.png").exists())
            self.assertTrue((output_dir / "seal_angle_contact_sheet.png").exists())

            for item in summary["images"]:
                self.assertIn("source_input", item)
                self.assertIn("split_index", item)
                self.assertTrue(Path(item["split_crop_path"]).exists())
                self.assertTrue(Path(item["crop_path"]).exists())
                self.assertTrue(Path(item["bw_path"]).exists())
                self.assertTrue(Path(item["angle_corrected_bw_path"]).exists())

    def test_adaptive_variant_set_writes_comparison_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_path = root / "stamp.png"
            output_dir = root / "out"

            image = Image.new("RGB", (80, 80), "white")
            draw = ImageDraw.Draw(image)
            draw.ellipse((12, 12, 68, 68), outline=(190, 20, 30), width=4)
            draw.line((40, 22, 40, 58), fill=(190, 20, 30), width=3)
            draw.line((28, 40, 52, 40), fill=(190, 20, 30), width=3)
            image.save(image_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--output-dir",
                    str(output_dir),
                    "--scale",
                    "2",
                    "--variant-set",
                    "adaptive",
                    "--variant-tile-grid",
                    "5",
                    str(image_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(
                (output_dir / "seal_processing_summary.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(summary["variant_set"], "adaptive")
            self.assertEqual(summary["variant_tile_grid"], 5)
            self.assertTrue((output_dir / "seal_variant_contact_sheet.png").exists())
            item = summary["images"][0]
            variants = item["preprocessing_variants"]
            names = {variant["name"] for variant in variants}
            self.assertGreaterEqual(
                names,
                {
                    "raw_lanczos",
                    "raw_nearest",
                    "contrast_sharpened",
                    "red_isolated",
                    "red_clean_bw",
                    "adaptive_threshold",
                    "clahe_red_emphasis",
                    "morph_dilation",
                    "morph_closing",
                    "morph_erosion",
                    "raw_tile",
                },
            )
            for variant in variants:
                self.assertTrue(Path(variant["path"]).exists())
                self.assertIn("description", variant)


if __name__ == "__main__":
    unittest.main()
