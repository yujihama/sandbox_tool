from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sandbox_tool.site_crawler import (
    DEFAULT_USER_AGENT,
    CrawlPolicy,
    RobotsCache,
    decode_text,
    domain_matches,
    host_from_url,
    is_private_or_local_host,
    normalize_domain,
    normalize_url,
)


HOUJIN_BANGOU_HOME = "https://www.houjin-bangou.nta.go.jp/"
HOUJIN_BANGOU_SEARCH = "https://www.houjin-bangou.nta.go.jp/kensaku-kekka.html"
HOUJIN_BANGOU_DOMAIN = "www.houjin-bangou.nta.go.jp"
HOUJIN_BANGOU_SOURCE = "National Tax Agency Corporate Number Publication Site"
MAX_QUERY_CHARS = 120
MAX_RESULT_ROWS = 100
NO_DATA_MESSAGE = "\u5165\u529b\u3055\u308c\u305f\u6761\u4ef6\u306b\u8a72\u5f53\u3059\u308b\u30c7\u30fc\u30bf\u304c\u5b58\u5728\u3057\u307e\u305b\u3093"
INVALID_VIEW_COUNT_MESSAGE = "\u8868\u793a\u4ef6\u6570\u304c\u6b63\u3057\u304f\u3042\u308a\u307e\u305b\u3093"
HOUJIN_SEARCH_LOCK = threading.Lock()


@dataclass
class HoujinBangouSearchPolicy:
    query: str
    match_type: str = "prefix"
    include_closed: bool = True
    max_results: int = 20
    try_name_variants: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    respect_robots_txt: bool = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_company_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", "", normalized)


def common_name_variants(query: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", query).strip()
    variants = [query.strip()]
    if normalized and normalized != variants[0]:
        variants.append(normalized)
    designators = [
        "\u682a\u5f0f\u4f1a\u793e",
        "\u6709\u9650\u4f1a\u793e",
        "\u5408\u540c\u4f1a\u793e",
        "\u5408\u540d\u4f1a\u793e",
        "\u5408\u8cc7\u4f1a\u793e",
        "\u4e00\u822c\u793e\u56e3\u6cd5\u4eba",
        "\u4e00\u822c\u8ca1\u56e3\u6cd5\u4eba",
        "\u516c\u76ca\u793e\u56e3\u6cd5\u4eba",
        "\u516c\u76ca\u8ca1\u56e3\u6cd5\u4eba",
    ]
    for source in list(variants):
        for designator in designators:
            if source.startswith(designator) and len(source) > len(designator):
                variants.append(source[len(designator) :].strip())
            if source.endswith(designator) and len(source) > len(designator):
                variants.append(source[: -len(designator)].strip())
    unique: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        cleaned = variant.strip()
        if not cleaned:
            continue
        key = normalize_company_name(cleaned)
        if key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    return unique


def safe_query(value: str) -> str:
    query = (value or "").strip()
    if not query:
        raise ValueError("query is required.")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query is too long; max {MAX_QUERY_CHARS} characters.")
    return query


def match_type_value(match_type: str) -> str:
    normalized = (match_type or "prefix").strip().lower()
    if normalized in {"prefix", "starts_with", "forward", "1"}:
        return "1"
    if normalized in {"partial", "contains", "2"}:
        return "2"
    raise ValueError("match_type must be prefix or partial.")


def clamp_result_limit(max_results: int) -> int:
    try:
        value = int(max_results)
    except Exception as exc:
        raise ValueError("max_results must be an integer.") from exc
    return max(1, min(value, MAX_RESULT_ROWS))


def site_view_count(max_results: int) -> int:
    limit = clamp_result_limit(max_results)
    if limit <= 10:
        return 10
    if limit <= 50:
        return 50
    return 100


def make_search_id(query: str, match_type: str, include_closed: bool) -> str:
    digest = hashlib.sha256(f"{query}|{match_type}|{include_closed}".encode("utf-8")).hexdigest()[:10]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{digest}"


def default_form_params(html: str) -> dict[str, str]:
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("beautifulsoup4 is required for houjin-bangou search") from exc

    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id="appForm") or soup.find("form")
    if form is None:
        raise ValueError("Could not find search form on houjin-bangou page.")

    params: dict[str, str] = {}
    for element in form.find_all(["input", "select"]):
        name = element.get("name")
        if not name:
            continue
        if element.name == "select":
            selected = element.find("option", selected=True) or element.find("option")
            params[name] = selected.get("value", "") if selected else ""
            continue
        input_type = (element.get("type") or "").lower()
        if input_type in {"submit", "button", "image", "file", "reset"}:
            continue
        if input_type in {"checkbox", "radio"} and not element.has_attr("checked"):
            continue
        params[name] = element.get("value", "")
    return params


def fetch_home(opener: urllib.request.OpenerDirector, policy: HoujinBangouSearchPolicy) -> str:
    request = urllib.request.Request(
        HOUJIN_BANGOU_HOME,
        headers={
            "User-Agent": policy.user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.5",
        },
    )
    with opener.open(request, timeout=20) as response:
        final_url = normalize_url(response.geturl())
        if not domain_matches(host_from_url(final_url), [HOUJIN_BANGOU_DOMAIN]):
            raise ValueError(f"Redirected outside houjin-bangou domain: {final_url}")
        raw = response.read(2_000_000 + 1)
        if len(raw) > 2_000_000:
            raise ValueError("Search form response exceeded 2000000 bytes.")
        return decode_text(raw, response.headers.get("Content-Type", "text/html"))


def post_search(
    opener: urllib.request.OpenerDirector,
    params: dict[str, str],
    policy: HoujinBangouSearchPolicy,
) -> str:
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        HOUJIN_BANGOU_SEARCH,
        data=data,
        headers={
            "User-Agent": policy.user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": HOUJIN_BANGOU_HOME,
        },
    )
    with opener.open(request, timeout=30) as response:
        final_url = normalize_url(response.geturl())
        if not domain_matches(host_from_url(final_url), [HOUJIN_BANGOU_DOMAIN]):
            raise ValueError(f"Redirected outside houjin-bangou domain: {final_url}")
        raw = response.read(4_000_000 + 1)
        if len(raw) > 4_000_000:
            raise ValueError("Search result response exceeded 4000000 bytes.")
        return decode_text(raw, response.headers.get("Content-Type", "text/html"))


def parse_result_count(text: str) -> int | None:
    match = re.search(r"(\d+)\s*\u4ef6\s*\u00a0?\s*\u898b\u3064\u304b\u308a\u307e\u3057\u305f", text)
    if not match:
        return 0 if NO_DATA_MESSAGE in text else None
    return int(match.group(1))


def parse_search_results(html: str, query: str, exact_query: str | None = None) -> dict[str, Any]:
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("beautifulsoup4 is required for houjin-bangou search") from exc

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    site_error = INVALID_VIEW_COUNT_MESSAGE if INVALID_VIEW_COUNT_MESSAGE in text else ""
    result_count = parse_result_count(text)
    rows: list[dict[str, Any]] = []
    table = soup.find("table", class_=lambda value: value and "fixed" in value)
    if table is not None:
        exact_name = exact_query or query
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            corporate_number = re.sub(r"\D+", "", cells[0].get_text(" ", strip=True))
            if not corporate_number:
                continue
            name_lines = [
                item.strip()
                for item in cells[1].get_text("\n", strip=True).split("\n")
                if item.strip()
            ]
            name = name_lines[-1] if name_lines else ""
            furigana = name_lines[0] if len(name_lines) >= 2 else ""
            location = cells[2].get_text(" ", strip=True)
            history = cells[3].get_text(" ", strip=True) if len(cells) >= 4 else ""
            rows.append(
                {
                    "corporate_number": corporate_number,
                    "name": name,
                    "furigana": furigana,
                    "location": location,
                    "history_text": history,
                    "exact_name_match": normalize_company_name(name) == normalize_company_name(exact_name),
                }
            )
    exact_matches = [row for row in rows if row["exact_name_match"]]
    return {
        "result_count": result_count,
        "no_data": NO_DATA_MESSAGE in text,
        "site_error": site_error,
        "rows": rows,
        "exact_match_count": len(exact_matches),
        "exact_matches": exact_matches,
    }


def build_search_params(form_html: str, policy: HoujinBangouSearchPolicy) -> dict[str, str]:
    params = default_form_params(form_html)
    result_limit = site_view_count(policy.max_results)
    params.update(
        {
            "houzinNmTxtf": safe_query(policy.query),
            "houzinNmShTypeRbtn": match_type_value(policy.match_type),
            "search": "true",
            "japanese": "true",
            "searchFlg": "1",
            "houzinKdRbtn": "0",
            "orderRbtn": "1",
            "viewNumAnc": str(result_limit),
        }
    )
    if policy.include_closed:
        params["closeCkbx"] = "1"
    else:
        params.pop("closeCkbx", None)
    return params


def write_search_artifacts(
    output_root: Path,
    search_id: str,
    result: dict[str, Any],
    response_html: str,
) -> dict[str, str]:
    search_dir = output_root / "_official_search" / "houjin_bangou" / search_id
    search_dir.mkdir(parents=True, exist_ok=True)
    (search_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (search_dir / "response.html").write_text(response_html, encoding="utf-8")
    lines = [
        "# Houjin Bangou Search Summary",
        "",
        f"- Query: {result['query']}",
        f"- Actual query used for selected result: {result['actual_query']}",
        f"- Match type: {result['match_type']}",
        f"- Include closed records: {result['include_closed']}",
        f"- Result count reported by site: {result['result_count']}",
        f"- Parsed rows: {len(result['rows'])}",
        f"- Exact name matches: {result['exact_match_count']}",
        f"- Retrieval time: {result['retrieved_at']}",
        f"- Source: {HOUJIN_BANGOU_HOME}",
        "",
        "## Search Attempts",
        "",
    ]
    for attempt in result.get("search_attempts", []):
        lines.append(
            "- "
            f"{attempt['query']}: result_count={attempt['result_count']}, "
            f"exact_match_count={attempt['exact_match_count']}, "
            f"parsed_rows={attempt['parsed_rows']}"
        )
    lines.extend(
        [
            "",
        "## Parsed Results",
        "",
        ]
    )
    for row in result["rows"][:20]:
        lines.extend(
            [
                f"- {row['name']} ({row['corporate_number']})",
                f"  - Location: {row['location']}",
                f"  - Exact name match: {row['exact_name_match']}",
            ]
        )
    (search_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    virtual_root = f"/outputs/_official_search/houjin_bangou/{search_id}"
    return {
        "virtual_root": virtual_root,
        "result_json_path": f"{virtual_root}/result.json",
        "summary_path": f"{virtual_root}/summary.md",
        "response_html_path": f"{virtual_root}/response.html",
    }


def run_houjin_bangou_search(output_root: Path, policy: HoujinBangouSearchPolicy) -> dict[str, Any]:
    query = safe_query(policy.query)
    normalized_domain = normalize_domain(HOUJIN_BANGOU_DOMAIN)
    if is_private_or_local_host(normalized_domain):
        raise ValueError("houjin-bangou domain unexpectedly resolved as private/local host.")
    robots_policy = CrawlPolicy(
        start_url=HOUJIN_BANGOU_HOME,
        allowed_domains=[normalized_domain],
        max_pages=1,
        max_depth=0,
        user_agent=policy.user_agent,
        respect_robots_txt=policy.respect_robots_txt,
    )
    robots = RobotsCache(robots_policy)
    if not robots.can_fetch(HOUJIN_BANGOU_HOME):
        raise ValueError("robots.txt disallows houjin-bangou home page.")
    if not robots.can_fetch(HOUJIN_BANGOU_SEARCH):
        raise ValueError("robots.txt disallows houjin-bangou search page.")

    attempt_queries = common_name_variants(query) if policy.try_name_variants else [query]
    attempts: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    selected_html = ""
    selected_query = query

    with HOUJIN_SEARCH_LOCK:
        for attempt_query in attempt_queries:
            attempt_policy = replace(policy, query=attempt_query)
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            form_html = fetch_home(opener, attempt_policy)
            params = build_search_params(form_html, attempt_policy)
            time.sleep(0.25)
            response_html = post_search(opener, params, attempt_policy)
            parsed = parse_search_results(response_html, attempt_query, exact_query=query)
            if parsed["site_error"]:
                raise RuntimeError(f"houjin-bangou site returned an error message: {parsed['site_error']}")
            for row in parsed["rows"]:
                row["matched_via_query"] = attempt_query
            attempts.append(
                {
                    "query": attempt_query,
                    "normalized_query": normalize_company_name(attempt_query),
                    "result_count": parsed["result_count"],
                    "exact_match_count": parsed["exact_match_count"],
                    "parsed_rows": len(parsed["rows"]),
                    "no_data": parsed["no_data"],
                }
            )
            if selected is None:
                selected = parsed
                selected_html = response_html
                selected_query = attempt_query
            elif parsed["exact_match_count"] > 0:
                selected = parsed
                selected_html = response_html
                selected_query = attempt_query
            elif selected["exact_match_count"] == 0 and not selected["rows"] and parsed["rows"]:
                selected = parsed
                selected_html = response_html
                selected_query = attempt_query
            if parsed["exact_match_count"] > 0:
                break

    if selected is None:
        raise RuntimeError("houjin-bangou search produced no attempts.")
    parsed = selected
    search_id = make_search_id(query, policy.match_type, policy.include_closed)
    result: dict[str, Any] = {
        "ok": True,
        "search_id": search_id,
        "query": query,
        "normalized_query": normalize_company_name(query),
        "actual_query": selected_query,
        "normalized_actual_query": normalize_company_name(selected_query),
        "try_name_variants": bool(policy.try_name_variants),
        "search_attempts": attempts,
        "match_type": "prefix" if match_type_value(policy.match_type) == "1" else "partial",
        "include_closed": bool(policy.include_closed),
        "source_name": HOUJIN_BANGOU_SOURCE,
        "source_url": HOUJIN_BANGOU_HOME,
        "search_url": HOUJIN_BANGOU_SEARCH,
        "retrieved_at": utc_now(),
        "result_count": parsed["result_count"],
        "no_data": parsed["no_data"],
        "exact_match_count": parsed["exact_match_count"],
        "exact_matches": parsed["exact_matches"],
        "rows": parsed["rows"][: clamp_result_limit(policy.max_results)],
        "interpretation": (
            "exact_match_found"
            if parsed["exact_match_count"] > 0
            else "no_result"
            if parsed["no_data"] or parsed["result_count"] == 0
            else "unparsed_response"
            if parsed["result_count"] is None and not parsed["rows"]
            else "candidate_rows_found_without_exact_match"
        ),
    }
    result.update(write_search_artifacts(output_root, search_id, result, selected_html))
    return result
