from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "outputs"
    / "skills"
    / "houjin-bangou-browser-search"
    / "scripts"
    / "parse_houjin_playwright_result.py"
)


class HoujinBrowserSkillParserTests(unittest.TestCase):
    def test_parser_extracts_result_rows_detail_facts_and_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result_path = root / "result.json"
            detail_path = root / "detail.json"
            no_data_path = root / "no_data.json"
            output_path = root / "summary.json"

            result_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "run_id": "result-run",
                        "elapsed_seconds": 1.23,
                        "page": {
                            "url": "https://www.houjin-bangou.nta.go.jp/kensaku-kekka.html",
                            "title": "検索結果｜国税庁法人番号公表サイト",
                            "text_preview": "検索結果",
                            "tables": [
                                [
                                    ["法人番号", "商号又は名称", "所在地", "変更履歴情報等"],
                                    [
                                        "9180001059935",
                                        "アイチトヨタジドウシャ\n愛知トヨタ自動車株式会社",
                                        "愛知県名古屋市昭和区高辻町６番８号",
                                        "履歴等",
                                    ],
                                    [
                                        "1180301018771",
                                        "トヨタジドウシャ\nトヨタ自動車株式会社",
                                        "愛知県豊田市トヨタ町１番地",
                                        "履歴等",
                                    ],
                                ]
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            detail_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "run_id": "detail-run",
                        "elapsed_seconds": 0.78,
                        "page": {
                            "url": "https://www.houjin-bangou.nta.go.jp/henkorireki-johoto.html?selHouzinNo=1180301018771",
                            "title": "トヨタ自動車株式会社の情報｜国税庁法人番号公表サイト",
                            "text_preview": (
                                "最新情報 法人番号 1180301018771 商号又は名称 "
                                "トヨタ自動車株式会社 商号又は名称（フリガナ） "
                                "トヨタジドウシャ 本店又は主たる事務所の所在地 "
                                "愛知県豊田市トヨタ町１番地 最終更新年月日 平成31年4月23日"
                            ),
                            "tables": [],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            no_data_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "run_id": "no-data-run",
                        "page": {
                            "url": "https://www.houjin-bangou.nta.go.jp/kensaku-kekka.html",
                            "title": "検索結果",
                            "text_preview": "No data exists. 国税庁（法人番号7000012050002）",
                            "tables": [],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(result_path),
                    str(detail_path),
                    str(no_data_path),
                    "--output",
                    str(output_path),
                    "--query",
                    "トヨタ自動車株式会社",
                    "--prefecture",
                    "愛知県",
                ],
                check=True,
                text=True,
                capture_output=True,
            )

            summary = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["file_count"], 3)
            self.assertEqual(summary["result_row_count"], 2)
            self.assertEqual(summary["detail_page_count"], 1)
            self.assertEqual(summary["no_data_count"], 1)

            by_run_id = {item["run_id"]: item for item in summary["files"]}

            row = by_run_id["result-run"]["result_rows"][1]
            self.assertEqual(row["corporate_number"], "1180301018771")
            self.assertEqual(row["legal_name"], "トヨタ自動車株式会社")
            self.assertEqual(row["phonetic"], "トヨタジドウシャ")
            self.assertEqual(
                row["detail_url"],
                "https://www.houjin-bangou.nta.go.jp/henkorireki-johoto.html?selHouzinNo=1180301018771",
            )

            detail = by_run_id["detail-run"]["detail"]
            self.assertTrue(detail["is_detail_page"])
            self.assertEqual(detail["corporate_number"], "1180301018771")
            self.assertEqual(detail["legal_name"], "トヨタ自動車株式会社")
            self.assertEqual(detail["address"], "愛知県豊田市トヨタ町１番地")
            self.assertNotIn(
                "7000012050002",
                [item["corporate_number"] for item in summary["corporate_numbers"]],
            )
            self.assertEqual(
                summary["best_matches"][0]["corporate_number"], "1180301018771"
            )
            self.assertEqual(summary["best_matches"][0]["match_type"], "exact")


if __name__ == "__main__":
    unittest.main()
