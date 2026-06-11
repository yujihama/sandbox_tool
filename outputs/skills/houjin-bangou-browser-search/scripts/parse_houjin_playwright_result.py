#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DETAIL_URL_TEMPLATE = (
    "https://www.houjin-bangou.nta.go.jp/henkorireki-johoto.html?selHouzinNo={number}"
)
NO_DATA_PATTERNS = [
    "No data",
    "no data",
    "見つかりません",
    "データがありません",
    "該当するデータ",
    "該当する情報",
]


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def first_match(pattern: str, value: str) -> str:
    match = re.search(pattern, value or "")
    return match.group(1) if match else ""


def table_index(header: list[str], candidates: list[str]) -> int | None:
    for index, cell in enumerate(header):
        normalized = normalize_space(cell)
        if any(candidate in normalized for candidate in candidates):
            return index
    return None


def split_name_cell(value: str) -> tuple[str, str, str]:
    parts = [part.strip() for part in str(value or "").splitlines() if part.strip()]
    if not parts:
        return "", "", ""
    legal_name = parts[-1]
    phonetic = " ".join(parts[:-1])
    return legal_name, phonetic, "\n".join(parts)


def parse_result_table(table: Any) -> list[dict[str, Any]]:
    if not isinstance(table, list) or len(table) < 2:
        return []
    header = [normalize_space(cell) for cell in table[0]]
    number_index = table_index(header, ["法人番号", "Corporate Number"])
    name_index = table_index(header, ["商号又は名称", "Name"])
    address_index = table_index(header, ["所在地", "Location", "Address"])

    rows: list[dict[str, Any]] = []
    for position, raw_row in enumerate(table[1:], start=1):
        if not isinstance(raw_row, list):
            continue
        row_text = "\n".join(str(cell or "") for cell in raw_row)
        corporate_number = ""
        if number_index is not None and number_index < len(raw_row):
            corporate_number = first_match(r"(\d{13})", str(raw_row[number_index]))
        if not corporate_number:
            corporate_number = first_match(r"(\d{13})", row_text)
        if not corporate_number:
            continue

        legal_name = ""
        phonetic = ""
        raw_name = ""
        if name_index is not None and name_index < len(raw_row):
            legal_name, phonetic, raw_name = split_name_cell(str(raw_row[name_index]))
        address = (
            normalize_space(raw_row[address_index])
            if address_index is not None and address_index < len(raw_row)
            else ""
        )
        rows.append(
            {
                "position": position,
                "corporate_number": corporate_number,
                "legal_name": legal_name,
                "phonetic": phonetic,
                "raw_name": raw_name,
                "address": address,
                "detail_url": DETAIL_URL_TEMPLATE.format(number=corporate_number),
                "raw_row": raw_row,
            }
        )
    return rows


def extract_between(text: str, start_label: str, end_labels: list[str]) -> str:
    start = text.find(start_label)
    if start < 0:
        return ""
    start += len(start_label)
    end = len(text)
    for label in end_labels:
        found = text.find(label, start)
        if found >= 0:
            end = min(end, found)
    return normalize_space(text[start:end])


def parse_detail_text(text: str, url: str, title: str) -> dict[str, Any]:
    is_detail = "henkorireki-johoto.html" in url or "の情報" in title
    if not is_detail:
        return {
            "is_detail_page": False,
            "corporate_number": "",
            "legal_name": "",
            "phonetic": "",
            "address": "",
        }

    normalized = normalize_space(text)
    corporate_number = extract_between(
        normalized,
        "法人番号",
        ["商号又は名称", "Name", "本店又は主たる事務所"],
    )
    corporate_number = first_match(r"(\d{13})", corporate_number) or first_match(
        r"(\d{13})", normalized
    )
    legal_name = extract_between(
        normalized,
        "商号又は名称",
        ["商号又は名称（フリガナ）", "本店又は主たる事務所", "所在地", "最終更新年月日"],
    )
    phonetic = extract_between(
        normalized,
        "商号又は名称（フリガナ）",
        ["本店又は主たる事務所", "所在地", "最終更新年月日"],
    )
    address = extract_between(
        normalized,
        "本店又は主たる事務所の所在地",
        ["最終更新年月日", "変更履歴情報", "こちらの検索結果"],
    )
    if not legal_name and "の情報" in title:
        legal_name = title.split("の情報", 1)[0].strip()
    is_detail = bool(is_detail and corporate_number and legal_name)
    return {
        "is_detail_page": is_detail,
        "corporate_number": corporate_number,
        "legal_name": legal_name,
        "phonetic": phonetic,
        "address": address,
    }


def parse_result_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    text_preview = str(page.get("text_preview") or "")
    url = str(page.get("url") or data.get("final_url") or data.get("start_url") or "")
    title = str(page.get("title") or data.get("title") or "")
    tables = page.get("tables") if isinstance(page.get("tables"), list) else []

    result_rows: list[dict[str, Any]] = []
    for table in tables:
        result_rows.extend(parse_result_table(table))

    detail = parse_detail_text(text_preview, url, title)
    no_data = any(pattern in text_preview for pattern in NO_DATA_PATTERNS)
    if not result_rows and "kensaku-kekka.html" in url and tables == []:
        no_data = no_data or "検索結果" in text_preview or "Search Results" in text_preview

    return {
        "source_path": str(path),
        "run_id": str(data.get("run_id") or path.parent.name),
        "ok": bool(data.get("ok")),
        "elapsed_seconds": data.get("elapsed_seconds"),
        "url": url,
        "title": title,
        "blocked_url_count": data.get("blocked_url_count", 0),
        "no_data": bool(no_data),
        "result_row_count": len(result_rows),
        "result_rows": result_rows,
        "detail": detail,
    }


def score_result_row(
    row: dict[str, Any],
    *,
    query: str,
    prefecture: str,
) -> dict[str, Any]:
    query_key = normalize_for_match(query)
    name_key = normalize_for_match(row.get("legal_name", ""))
    raw_name_key = normalize_for_match(row.get("raw_name", ""))
    address = str(row.get("address") or "")
    score = 0
    match_type = "unscored"
    if query_key:
        if name_key == query_key:
            score = 100
            match_type = "exact"
        elif query_key and query_key in name_key:
            score = 70
            match_type = "name_contains_query"
        elif name_key and name_key in query_key:
            score = 60
            match_type = "query_contains_name"
        elif query_key in raw_name_key:
            score = 50
            match_type = "raw_name_contains_query"
        else:
            match_type = "not_matched"
    if prefecture and prefecture in address:
        score += 10
    return {
        **row,
        "match_score": score,
        "match_type": match_type,
        "prefecture_match": bool(prefecture and prefecture in address),
    }


def expand_paths(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            paths.extend(Path(match) for match in matches)
        else:
            paths.append(Path(pattern))
    return sorted(dict.fromkeys(path.resolve() for path in paths))


def summarize(
    files: list[dict[str, Any]],
    *,
    query: str = "",
    prefecture: str = "",
) -> dict[str, Any]:
    numbers: dict[str, dict[str, Any]] = {}
    scored_rows: list[dict[str, Any]] = []
    for file_result in files:
        for row in file_result["result_rows"]:
            numbers.setdefault(row["corporate_number"], row)
            scored_rows.append(score_result_row(row, query=query, prefecture=prefecture))
        detail_number = file_result["detail"].get("corporate_number")
        if detail_number:
            numbers.setdefault(
                detail_number,
                {
                    "corporate_number": detail_number,
                    "legal_name": file_result["detail"].get("legal_name", ""),
                    "address": file_result["detail"].get("address", ""),
                    "detail_url": file_result["url"],
                },
            )
    best_matches = [
        row
        for row in sorted(
            scored_rows,
            key=lambda item: (
                item["match_score"],
                item["match_type"] == "exact",
                -int(item.get("position") or 0),
            ),
            reverse=True,
        )
        if row["match_score"] > 0
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "prefecture": prefecture,
        "file_count": len(files),
        "no_data_count": sum(1 for item in files if item["no_data"]),
        "result_row_count": sum(item["result_row_count"] for item in files),
        "detail_page_count": sum(1 for item in files if item["detail"]["is_detail_page"]),
        "corporate_numbers": list(numbers.values()),
        "best_matches": best_matches,
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract search rows and detail-page facts from houjin-bangou Playwright result.json files."
    )
    parser.add_argument("result_json", nargs="+", help="result.json path or glob pattern")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--query", default="", help="Submitted or target company name")
    parser.add_argument("--prefecture", default="", help="Optional prefecture filter for ranking")
    args = parser.parse_args()

    parsed_files = [parse_result_file(path) for path in expand_paths(args.result_json)]
    result = summarize(parsed_files, query=args.query, prefecture=args.prefecture)
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
