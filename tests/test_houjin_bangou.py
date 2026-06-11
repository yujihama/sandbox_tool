from __future__ import annotations

import unittest

from sandbox_tool.houjin_bangou import (
    HoujinBangouSearchPolicy,
    build_search_params,
    common_name_variants,
    normalize_company_name,
    parse_search_results,
    site_view_count,
)


TOYOTA = "\u30c8\u30e8\u30bf\u81ea\u52d5\u8eca\u682a\u5f0f\u4f1a\u793e"
TOYOTA_KANA = "\u30c8\u30e8\u30bf\u30b8\u30c9\u30a6\u30b7\u30e3"
TOYOTA_LOCATION = "\u611b\u77e5\u770c\u8c4a\u7530\u5e02\u30c8\u30e8\u30bf\u753a\uff11\u756a\u5730"
MUFG = "\u682a\u5f0f\u4f1a\u793e\u4e09\u83f1\uff35\uff26\uff2a\u9280\u884c"
MUFG_WITHOUT_DESIGNATOR = "\u4e09\u83f1UFJ\u9280\u884c"
NO_DATA = (
    "\u5165\u529b\u3055\u308c\u305f\u6761\u4ef6\u306b\u8a72\u5f53\u3059\u308b"
    "\u30c7\u30fc\u30bf\u304c\u5b58\u5728\u3057\u307e\u305b\u3093"
)


class HoujinBangouTests(unittest.TestCase):
    def test_parse_search_results_extracts_exact_match(self) -> None:
        html = f"""
        <html><body>
        <p>1&nbsp;\u4ef6&nbsp;\u898b\u3064\u304b\u308a\u307e\u3057\u305f\u3002</p>
        <table class="fixed normal">
          <tr><th>\u6cd5\u4eba\u756a\u53f7</th><th>\u5546\u53f7\u53c8\u306f\u540d\u79f0</th><th>\u6240\u5728\u5730</th><th>\u5909\u66f4\u5c65\u6b74\u60c5\u5831\u7b49</th></tr>
          <tr>
            <td>1180301018771</td>
            <td>{TOYOTA_KANA}<br>{TOYOTA}</td>
            <td>{TOYOTA_LOCATION}</td>
            <td>\u5c65\u6b74\u7b49</td>
          </tr>
        </table>
        </body></html>
        """

        parsed = parse_search_results(html, TOYOTA)

        self.assertEqual(parsed["result_count"], 1)
        self.assertEqual(parsed["exact_match_count"], 1)
        self.assertEqual(parsed["rows"][0]["corporate_number"], "1180301018771")
        self.assertEqual(parsed["rows"][0]["name"], TOYOTA)
        self.assertTrue(parsed["rows"][0]["exact_name_match"])

    def test_parse_search_results_can_match_against_original_query(self) -> None:
        html = f"""
        <html><body>
        <p>1&nbsp;\u4ef6&nbsp;\u898b\u3064\u304b\u308a\u307e\u3057\u305f\u3002</p>
        <table class="fixed normal">
          <tr><th>\u6cd5\u4eba\u756a\u53f7</th><th>\u5546\u53f7\u53c8\u306f\u540d\u79f0</th><th>\u6240\u5728\u5730</th><th>\u5909\u66f4\u5c65\u6b74\u60c5\u5831\u7b49</th></tr>
          <tr>
            <td>5010001008846</td>
            <td>\u30df\u30c4\u30d3\u30b7\u30e6\u30fc\u30a8\u30d5\u30b8\u30a7\u30a4\u30ae\u30f3\u30b3\u30a6<br>{MUFG}</td>
            <td>\u6771\u4eac\u90fd\u5343\u4ee3\u7530\u533a\u4e38\u306e\u5185\uff11\u4e01\u76ee\uff14\u756a\uff15\u53f7</td>
            <td>\u5c65\u6b74\u7b49</td>
          </tr>
        </table>
        </body></html>
        """

        parsed = parse_search_results(
            html,
            query=MUFG_WITHOUT_DESIGNATOR,
            exact_query=MUFG,
        )

        self.assertEqual(parsed["exact_match_count"], 1)
        self.assertTrue(parsed["rows"][0]["exact_name_match"])

    def test_parse_search_results_detects_no_data(self) -> None:
        parsed = parse_search_results(f"<html><body>{NO_DATA}</body></html>", "no such company")

        self.assertEqual(parsed["result_count"], 0)
        self.assertTrue(parsed["no_data"])
        self.assertEqual(parsed["rows"], [])

    def test_build_search_params_controls_match_type_and_closed_records(self) -> None:
        form_html = """
        <form id="appForm">
          <input type="hidden" name="token" value="abc">
          <input type="radio" name="houzinNmShTypeRbtn" value="2" checked>
          <input type="checkbox" name="closeCkbx" value="1" checked>
          <select name="prefectureLst"><option value="" selected>select</option></select>
        </form>
        """

        params = build_search_params(
            form_html,
            HoujinBangouSearchPolicy(
                query=TOYOTA,
                match_type="prefix",
                include_closed=False,
                max_results=500,
            ),
        )

        self.assertEqual(params["houzinNmTxtf"], TOYOTA)
        self.assertEqual(params["houzinNmShTypeRbtn"], "1")
        self.assertEqual(params["viewNumAnc"], "100")
        self.assertNotIn("closeCkbx", params)

    def test_site_view_count_uses_supported_site_values(self) -> None:
        self.assertEqual(site_view_count(1), 10)
        self.assertEqual(site_view_count(10), 10)
        self.assertEqual(site_view_count(20), 50)
        self.assertEqual(site_view_count(50), 50)
        self.assertEqual(site_view_count(100), 100)
        self.assertEqual(site_view_count(500), 100)

    def test_common_name_variants_remove_leading_designator(self) -> None:
        variants = common_name_variants(MUFG)

        self.assertIn(
            normalize_company_name(MUFG_WITHOUT_DESIGNATOR),
            {normalize_company_name(variant) for variant in variants},
        )

    def test_parse_search_results_detects_site_error(self) -> None:
        html = "<html><body>\u8868\u793a\u4ef6\u6570\u304c\u6b63\u3057\u304f\u3042\u308a\u307e\u305b\u3093</body></html>"

        parsed = parse_search_results(html, TOYOTA)

        self.assertEqual(
            parsed["site_error"],
            "\u8868\u793a\u4ef6\u6570\u304c\u6b63\u3057\u304f\u3042\u308a\u307e\u305b\u3093",
        )
        self.assertIsNone(parsed["result_count"])

    def test_normalize_company_name_ignores_width_and_spaces(self) -> None:
        self.assertEqual(
            normalize_company_name("\uff21 \uff22 \uff23\u682a\u5f0f\u4f1a\u793e"),
            "ABC\u682a\u5f0f\u4f1a\u793e",
        )


if __name__ == "__main__":
    unittest.main()
