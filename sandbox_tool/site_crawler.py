from __future__ import annotations

import html
import io
import ipaddress
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_USER_AGENT = "sandbox-tool-site-research/1.0"
DEFAULT_MAX_BYTES_PER_URL = 2_000_000
HARD_MAX_BYTES_PER_URL = 8_000_000
HARD_MAX_PAGES = 300
SUPPORTED_TEXT_EXTENSIONS = {"", ".html", ".htm", ".txt", ".csv", ".json", ".xml", ".pdf"}


@dataclass
class CrawlPolicy:
    start_url: str
    allowed_domains: list[str]
    max_pages: int = 40
    max_depth: int = 2
    path_prefixes: list[str] | None = None
    exclude_url_patterns: list[str] | None = None
    request_delay_seconds: float = 0.25
    max_bytes_per_url: int = DEFAULT_MAX_BYTES_PER_URL
    user_agent: str = DEFAULT_USER_AGENT
    allow_private_hosts: bool = False
    respect_robots_txt: bool = True
    proxy_url: str = ""


@dataclass
class CrawlRecord:
    page_id: int
    url: str
    final_url: str
    title: str
    content_type: str
    depth: int
    status: str
    fetched_at: str
    text_path: str
    text_chars: int
    outbound_links: int
    error: str = ""


@dataclass
class LinkExtractPolicy:
    list_url: str
    allowed_domains: list[str]
    path_prefixes: list[str] | None = None
    required_year: int | None = None
    required_month: int | None = None
    date_from: str = ""
    date_to: str = ""
    include_text_patterns: list[str] | None = None
    exclude_text_patterns: list[str] | None = None
    include_url_patterns: list[str] | None = None
    exclude_url_patterns: list[str] | None = None
    css_selector: str = ""
    allowed_extensions: list[str] | None = None
    url_contains: str = ""
    max_links: int = 300
    user_agent: str = DEFAULT_USER_AGENT
    allow_private_hosts: bool = False
    respect_robots_txt: bool = True
    proxy_url: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip().lower())
    slug = slug.strip("._-") or "site"
    return slug[:max_len]


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported.")
    if not parsed.netloc:
        raise ValueError("URL host is required.")
    cleaned = parsed._replace(fragment="")
    return urllib.parse.urlunsplit(cleaned)


def host_from_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if not host:
        raise ValueError("URL host is required.")
    return host.lower().strip(".")


def normalize_domain(domain: str) -> str:
    parsed = urllib.parse.urlsplit(domain if "://" in domain else f"https://{domain}")
    host = parsed.hostname or domain
    normalized = host.lower().strip(".")
    if not normalized:
        raise ValueError("Allowed domain is empty.")
    if any(char in normalized for char in "/*?#[ ]"):
        raise ValueError(f"Invalid allowed domain: {domain}")
    return normalized


def is_private_or_local_host(host: str) -> bool:
    lowered = host.lower().strip(".")
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def domain_matches(host: str, allowed_domains: list[str]) -> bool:
    host = host.lower().strip(".")
    for domain in allowed_domains:
        domain = domain.lower().strip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def compile_exclude_patterns(patterns: list[str] | None) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns or []:
        if not pattern.strip():
            continue
        compiled.append(re.compile(pattern))
    return compiled


def compile_filter_patterns(patterns: list[str] | None, field_name: str) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns or []:
        if not pattern.strip():
            continue
        try:
            compiled.append(re.compile(pattern, flags=re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"Invalid {field_name} regex {pattern!r}: {exc}") from exc
    return compiled


def matches_any_pattern(value: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def normalize_extensions(extensions: list[str] | None) -> set[str] | None:
    if not extensions:
        return None
    normalized: set[str] = set()
    for extension in extensions:
        cleaned = extension.strip().lower()
        if not cleaned:
            continue
        if cleaned in {"*", "any"}:
            return None
        if cleaned != "" and not cleaned.startswith("."):
            cleaned = "." + cleaned
        normalized.add(cleaned)
    return normalized or None


def extension_allowed(url: str, allowed_extensions: set[str] | None) -> bool:
    if allowed_extensions is None:
        return True
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    return suffix in allowed_extensions


def is_supported_extension(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path
    suffix = Path(path).suffix.lower()
    return suffix in SUPPORTED_TEXT_EXTENSIONS


def is_allowed_url(
    url: str,
    *,
    allowed_domains: list[str],
    path_prefixes: list[str] | None = None,
    exclude_patterns: list[re.Pattern[str]] | None = None,
    allow_private_hosts: bool = False,
) -> bool:
    try:
        normalized = normalize_url(url)
        parsed = urllib.parse.urlsplit(normalized)
        host = host_from_url(normalized)
    except ValueError:
        return False
    if not allow_private_hosts and is_private_or_local_host(host):
        return False
    if not domain_matches(host, allowed_domains):
        return False
    if path_prefixes:
        path = parsed.path or "/"
        normalized_prefixes = [prefix if prefix.startswith("/") else "/" + prefix for prefix in path_prefixes]
        if not any(path.startswith(prefix) for prefix in normalized_prefixes):
            return False
    for pattern in exclude_patterns or []:
        if pattern.search(normalized):
            return False
    return is_supported_extension(normalized)


def make_crawl_id(start_url: str) -> str:
    host = slugify(host_from_url(start_url), max_len=48)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{host}"


def extract_html_document(raw_html: str, base_url: str) -> dict[str, Any]:
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:  # pragma: no cover - dependency should be installed
        raise RuntimeError("beautifulsoup4 is required for HTML extraction") from exc

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()

    title = ""
    if soup.title:
        title = soup.title.get_text(" ", strip=True)

    headings: list[dict[str, Any]] = []
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        text = heading.get_text(" ", strip=True)
        if text:
            headings.append({"level": int(heading.name[1]), "text": text})

    body = soup.find("main") or soup.body or soup
    text = body.get_text("\n", strip=True)
    text = html.unescape(re.sub(r"\n{3,}", "\n\n", text))

    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = str(tag.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        absolute, _fragment = urllib.parse.urldefrag(absolute)
        if absolute.startswith(("http://", "https://")):
            links.append(absolute)

    return {
        "title": title,
        "headings": headings[:80],
        "text": text,
        "links": list(dict.fromkeys(links)),
    }


def extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - dependency should be installed
        return f"[PDF text extraction unavailable: {exc.__class__.__name__}: {exc}]"

    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for index, page in enumerate(reader.pages[:30], start=1):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"[Page {index}]\n{text.strip()}")
        return "\n\n".join(parts).strip()
    except Exception as exc:
        return f"[PDF text extraction failed: {exc.__class__.__name__}: {exc}]"


def decode_text(data: bytes, content_type: str, fallback_encoding: str = "utf-8") -> str:
    match = re.search(r"charset=([^;]+)", content_type, flags=re.I)
    encoding = match.group(1).strip() if match else fallback_encoding
    return data.decode(encoding, errors="replace")


def fetch_limited(url: str, policy: CrawlPolicy) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": policy.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*;q=0.5",
        },
    )
    with build_url_opener(policy).open(request, timeout=20) as response:
        final_url = normalize_url(response.geturl())
        final_host = host_from_url(final_url)
        if not domain_matches(final_host, policy.allowed_domains):
            raise ValueError(f"Redirected outside allowed domains: {final_url}")
        raw = response.read(policy.max_bytes_per_url + 1)
        if len(raw) > policy.max_bytes_per_url:
            raise ValueError(f"Response exceeded max_bytes_per_url: {policy.max_bytes_per_url}")
        content_type = response.headers.get("Content-Type", "application/octet-stream")
    return {"final_url": final_url, "content_type": content_type, "data": raw}


def build_url_opener(policy: CrawlPolicy) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    if policy.proxy_url:
        handlers.append(
            urllib.request.ProxyHandler(
                {
                    "http": policy.proxy_url,
                    "https": policy.proxy_url,
                }
            )
        )
    return urllib.request.build_opener(*handlers)


class RobotsCache:
    def __init__(self, policy: CrawlPolicy) -> None:
        self.policy = policy
        self.parsers: dict[str, urllib.robotparser.RobotFileParser] = {}

    def can_fetch(self, url: str) -> bool:
        if not self.policy.respect_robots_txt:
            return True
        parsed = urllib.parse.urlsplit(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        parser = self.parsers.get(host_key)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            robots_url = urllib.parse.urljoin(host_key, "/robots.txt")
            parser.set_url(robots_url)
            try:
                request = urllib.request.Request(
                    robots_url,
                    headers={"User-Agent": self.policy.user_agent},
                )
                with build_url_opener(self.policy).open(request, timeout=20) as response:
                    raw = response.read(1_000_000 + 1)
                if len(raw) > 1_000_000:
                    return True
                text = raw.decode("utf-8", errors="replace")
                parser.parse(text.splitlines())
            except Exception:
                return True
            self.parsers[host_key] = parser
        return parser.can_fetch(self.policy.user_agent, url)


def write_page_markdown(crawl_dir: Path, record: CrawlRecord, headings: list[dict[str, Any]], text: str) -> str:
    pages_dir = crawl_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    filename = f"page_{record.page_id:04d}.md"
    relative = f"pages/{filename}"
    body = [
        f"# {record.title or record.final_url}",
        "",
        f"- URL: {record.final_url}",
        f"- Original URL: {record.url}",
        f"- Content-Type: {record.content_type}",
        f"- Depth: {record.depth}",
        f"- Fetched At: {record.fetched_at}",
        "",
    ]
    if headings:
        body.extend(["## Headings", ""])
        body.extend(f"- H{item['level']} {item['text']}" for item in headings[:40])
        body.append("")
    body.extend(["## Extracted Text", "", text])
    (pages_dir / filename).write_text("\n".join(body), encoding="utf-8")
    return relative


def build_sqlite_index(crawl_dir: Path, records: list[dict[str, Any]]) -> None:
    db_path = crawl_dir / "site_index.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pages (
                page_id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                final_url TEXT NOT NULL,
                title TEXT,
                content_type TEXT,
                depth INTEGER,
                status TEXT,
                fetched_at TEXT,
                text_path TEXT,
                text TEXT
            )
            """
        )
        conn.execute("DELETE FROM pages")
        for item in records:
            conn.execute(
                """
                INSERT INTO pages (
                    page_id, url, final_url, title, content_type, depth, status,
                    fetched_at, text_path, text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["page_id"],
                    item["url"],
                    item["final_url"],
                    item["title"],
                    item["content_type"],
                    item["depth"],
                    item["status"],
                    item["fetched_at"],
                    item["text_path"],
                    item.get("text", ""),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def fetch_and_extract_page(url: str, depth: int, page_id: int, policy: CrawlPolicy) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    fetched_at = utc_now()
    title = ""
    text = ""
    links: list[str] = []
    headings: list[dict[str, Any]] = []
    content_type = ""
    final_url = url
    status = "ok"
    error = ""
    try:
        response = fetch_limited(url, policy)
        final_url = response["final_url"]
        content_type = response["content_type"]
        data = response["data"]
        content_main = content_type.split(";", 1)[0].strip().lower()
        suffix = Path(urllib.parse.urlsplit(final_url).path).suffix.lower()
        if content_main in {"text/html", "application/xhtml+xml"} or suffix in {"", ".html", ".htm"}:
            raw_html = decode_text(data, content_type)
            extracted = extract_html_document(raw_html, final_url)
            title = extracted["title"]
            text = extracted["text"]
            links = extracted["links"]
            headings = extracted["headings"]
        elif content_main == "application/pdf" or suffix == ".pdf":
            title = Path(urllib.parse.urlsplit(final_url).path).name or final_url
            text = extract_pdf_text(data)
        elif content_main.startswith("text/") or suffix in {".txt", ".csv", ".json", ".xml"}:
            title = Path(urllib.parse.urlsplit(final_url).path).name or final_url
            text = decode_text(data, content_type)
        else:
            status = "skipped"
            error = f"unsupported_content_type:{content_type}"
            text = ""
    except Exception as exc:
        status = "error"
        error = f"{exc.__class__.__name__}: {exc}"

    record = CrawlRecord(
        page_id=page_id,
        url=url,
        final_url=final_url,
        title=title,
        content_type=content_type,
        depth=depth,
        status=status,
        fetched_at=fetched_at,
        text_path="",
        text_chars=len(text),
        outbound_links=len(links),
        error=error,
    )
    return {**record.__dict__, "text": text}, links, headings


def finalize_crawl(output_root: Path, crawl_id: str, crawl_dir: Path, policy: CrawlPolicy, records: list[dict[str, Any]], skipped: list[dict[str, str]], start_url: str) -> dict[str, Any]:
    build_sqlite_index(crawl_dir, records)

    manifest_records = [{key: value for key, value in item.items() if key != "text"} for item in records]
    (crawl_dir / "pages.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in manifest_records) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "crawl_id": crawl_id,
        "start_url": start_url,
        "allowed_domains": policy.allowed_domains,
        "path_prefixes": policy.path_prefixes,
        "max_pages": policy.max_pages,
        "max_depth": policy.max_depth,
        "respect_robots_txt": policy.respect_robots_txt,
        "created_at": utc_now(),
        "pages_fetched": len(records),
        "skipped_count": len(skipped),
        "skipped": skipped[:100],
        "files": {
            "manifest": "crawl_manifest.json",
            "pages_jsonl": "pages.jsonl",
            "sqlite_index": "site_index.sqlite",
            "pages_dir": "pages/",
        },
    }
    (crawl_dir / "crawl_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = [
        "# Site Crawl Summary",
        "",
        f"- Crawl ID: {crawl_id}",
        f"- Start URL: {start_url}",
        f"- Pages fetched: {len(records)}",
        f"- Allowed domains: {', '.join(policy.allowed_domains)}",
        "",
        "## Pages",
        "",
    ]
    for item in manifest_records:
        status_note = f" ({item['status']})" if item["status"] != "ok" else ""
        summary.append(f"- [{item['page_id']}] {item['title'] or item['final_url']}{status_note} - {item['final_url']}")
    (crawl_dir / "crawl_summary.md").write_text("\n".join(summary), encoding="utf-8")
    return {"manifest": manifest, "records": manifest_records, "crawl_dir": crawl_dir}


def run_site_crawl(output_root: Path, policy: CrawlPolicy) -> dict[str, Any]:
    start_url = normalize_url(policy.start_url)
    allowed_domains = [normalize_domain(domain) for domain in (policy.allowed_domains or [host_from_url(start_url)])]
    policy = CrawlPolicy(
        start_url=start_url,
        allowed_domains=allowed_domains,
        max_pages=max(1, min(int(policy.max_pages), HARD_MAX_PAGES)),
        max_depth=max(0, min(int(policy.max_depth), 8)),
        path_prefixes=policy.path_prefixes or [],
        exclude_url_patterns=policy.exclude_url_patterns or [],
        request_delay_seconds=max(0.0, min(float(policy.request_delay_seconds), 5.0)),
        max_bytes_per_url=max(64_000, min(int(policy.max_bytes_per_url), HARD_MAX_BYTES_PER_URL)),
        user_agent=policy.user_agent or DEFAULT_USER_AGENT,
        allow_private_hosts=policy.allow_private_hosts,
        respect_robots_txt=policy.respect_robots_txt,
        proxy_url=policy.proxy_url,
    )
    exclude_patterns = compile_exclude_patterns(policy.exclude_url_patterns)
    if not is_allowed_url(
        start_url,
        allowed_domains=policy.allowed_domains,
        path_prefixes=policy.path_prefixes,
        exclude_patterns=exclude_patterns,
        allow_private_hosts=policy.allow_private_hosts,
    ):
        raise ValueError("start_url is outside the allowed crawl policy.")

    crawl_id = make_crawl_id(start_url)
    crawl_dir = output_root / "_site_crawl" / crawl_id
    crawl_dir.mkdir(parents=True, exist_ok=True)
    robots = RobotsCache(policy)

    queue: list[tuple[str, int]] = [(start_url, 0)]
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    while queue and len(records) < policy.max_pages:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not is_allowed_url(
            url,
            allowed_domains=policy.allowed_domains,
            path_prefixes=policy.path_prefixes,
            exclude_patterns=exclude_patterns,
            allow_private_hosts=policy.allow_private_hosts,
        ):
            skipped.append({"url": url, "reason": "not_allowed"})
            continue
        if not robots.can_fetch(url):
            skipped.append({"url": url, "reason": "robots_txt"})
            continue

        if records:
            time.sleep(policy.request_delay_seconds)

        page_id = len(records) + 1
        record, links, headings = fetch_and_extract_page(url, depth, page_id, policy)
        temp_record = CrawlRecord(**{key: record[key] for key in CrawlRecord.__dataclass_fields__})
        text_path = write_page_markdown(crawl_dir, temp_record, headings, record["text"])
        record["text_path"] = text_path
        record["text_chars"] = len(record["text"])
        record["outbound_links"] = len(links)
        records.append(record)

        if record["status"] == "ok" and depth < policy.max_depth:
            for link in links:
                if link not in seen and is_allowed_url(
                    link,
                    allowed_domains=policy.allowed_domains,
                    path_prefixes=policy.path_prefixes,
                    exclude_patterns=exclude_patterns,
                    allow_private_hosts=policy.allow_private_hosts,
                ):
                    queue.append((link, depth + 1))

    return finalize_crawl(output_root, crawl_id, crawl_dir, policy, records, skipped, start_url)


def run_url_crawl(output_root: Path, urls: list[str], policy: CrawlPolicy) -> dict[str, Any]:
    if not urls:
        raise ValueError("urls must not be empty.")
    normalized_urls = list(dict.fromkeys(normalize_url(url) for url in urls))[:HARD_MAX_PAGES]
    allowed_domains = [normalize_domain(domain) for domain in (policy.allowed_domains or [host_from_url(normalized_urls[0])])]
    policy = CrawlPolicy(
        start_url=normalized_urls[0],
        allowed_domains=allowed_domains,
        max_pages=max(1, min(int(policy.max_pages or len(normalized_urls)), HARD_MAX_PAGES, len(normalized_urls))),
        max_depth=0,
        path_prefixes=policy.path_prefixes or [],
        exclude_url_patterns=policy.exclude_url_patterns or [],
        request_delay_seconds=max(0.0, min(float(policy.request_delay_seconds), 5.0)),
        max_bytes_per_url=max(64_000, min(int(policy.max_bytes_per_url), HARD_MAX_BYTES_PER_URL)),
        user_agent=policy.user_agent or DEFAULT_USER_AGENT,
        allow_private_hosts=policy.allow_private_hosts,
        respect_robots_txt=policy.respect_robots_txt,
        proxy_url=policy.proxy_url,
    )
    exclude_patterns = compile_exclude_patterns(policy.exclude_url_patterns)
    crawl_id = make_crawl_id(policy.start_url)
    crawl_dir = output_root / "_site_crawl" / crawl_id
    crawl_dir.mkdir(parents=True, exist_ok=True)
    robots = RobotsCache(policy)
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for url in normalized_urls:
        if len(records) >= policy.max_pages:
            break
        if not is_allowed_url(
            url,
            allowed_domains=policy.allowed_domains,
            path_prefixes=policy.path_prefixes,
            exclude_patterns=exclude_patterns,
            allow_private_hosts=policy.allow_private_hosts,
        ):
            skipped.append({"url": url, "reason": "not_allowed"})
            continue
        if not robots.can_fetch(url):
            skipped.append({"url": url, "reason": "robots_txt"})
            continue
        if records:
            time.sleep(policy.request_delay_seconds)
        page_id = len(records) + 1
        record, _links, headings = fetch_and_extract_page(url, 0, page_id, policy)
        temp_record = CrawlRecord(**{key: record[key] for key in CrawlRecord.__dataclass_fields__})
        text_path = write_page_markdown(crawl_dir, temp_record, headings, record["text"])
        record["text_path"] = text_path
        records.append(record)
    return finalize_crawl(output_root, crawl_id, crawl_dir, policy, records, skipped, policy.start_url)


DATE_PATTERNS = [
    re.compile(r"\u63b2\u8f09\u65e5\s*(?P<year>\d{4})\u5e74(?P<month>\d{1,2})\u6708(?P<day>\d{1,2})\u65e5"),
    re.compile(r"\b(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})\b"),
    re.compile(r"(?P<year>\d{4})\u5e74(?P<month>\d{1,2})\u6708(?P<day>\d{1,2})\u65e5"),
]


def extract_date_from_text(text: str) -> str:
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


def parse_date_filter(value: str, field_name: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD: {value}") from exc


def date_matches_policy(date_iso: str, policy: LinkExtractPolicy) -> bool:
    if not any([policy.required_year, policy.required_month, policy.date_from, policy.date_to]):
        return True
    if not date_iso:
        return False
    try:
        parsed = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError:
        return False
    if policy.required_year is not None and parsed.year != policy.required_year:
        return False
    if policy.required_month is not None and parsed.month != policy.required_month:
        return False
    start = parse_date_filter(policy.date_from, "date_from")
    end = parse_date_filter(policy.date_to, "date_to")
    if start and parsed < start:
        return False
    if end and parsed > end:
        return False
    return True


def selected_anchors(soup: Any, css_selector: str) -> list[Any]:
    if not css_selector.strip():
        return list(soup.find_all("a", href=True))
    selected = soup.select(css_selector)
    anchors: list[Any] = []
    for node in selected:
        if getattr(node, "name", "") == "a" and node.has_attr("href"):
            anchors.append(node)
        else:
            anchors.extend(node.find_all("a", href=True))
    return anchors


def extract_links_from_listing(output_root: Path, policy: LinkExtractPolicy) -> dict[str, Any]:
    list_url = normalize_url(policy.list_url)
    allowed_domains = [normalize_domain(domain) for domain in (policy.allowed_domains or [host_from_url(list_url)])]
    crawl_policy = CrawlPolicy(
        start_url=list_url,
        allowed_domains=allowed_domains,
        max_pages=1,
        max_depth=0,
        path_prefixes=policy.path_prefixes or [],
        request_delay_seconds=0,
        max_bytes_per_url=HARD_MAX_BYTES_PER_URL,
        user_agent=policy.user_agent,
        allow_private_hosts=policy.allow_private_hosts,
        respect_robots_txt=policy.respect_robots_txt,
        proxy_url=policy.proxy_url,
    )
    exclude_patterns: list[re.Pattern[str]] = []
    if not is_allowed_url(
        list_url,
        allowed_domains=allowed_domains,
        path_prefixes=policy.path_prefixes,
        exclude_patterns=exclude_patterns,
        allow_private_hosts=policy.allow_private_hosts,
    ):
        raise ValueError("list_url is outside the allowed extraction policy.")
    robots = RobotsCache(crawl_policy)
    if not robots.can_fetch(list_url):
        raise ValueError("robots.txt disallows list_url.")
    response = fetch_limited(list_url, crawl_policy)
    raw_html = decode_text(response["data"], response["content_type"])
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("beautifulsoup4 is required for link extraction") from exc
    soup = BeautifulSoup(raw_html, "html.parser")
    include_text_patterns = compile_filter_patterns(policy.include_text_patterns, "include_text_patterns")
    exclude_text_patterns = compile_filter_patterns(policy.exclude_text_patterns, "exclude_text_patterns")
    include_url_patterns = compile_filter_patterns(policy.include_url_patterns, "include_url_patterns")
    exclude_url_patterns = compile_filter_patterns(policy.exclude_url_patterns, "exclude_url_patterns")
    allowed_extensions = normalize_extensions(policy.allowed_extensions)
    links: list[dict[str, Any]] = []
    for anchor in selected_anchors(soup, policy.css_selector):
        href = urllib.parse.urljoin(response["final_url"], str(anchor.get("href") or ""))
        href, _fragment = urllib.parse.urldefrag(href)
        if not is_allowed_url(
            href,
            allowed_domains=allowed_domains,
            path_prefixes=policy.path_prefixes,
            exclude_patterns=exclude_patterns,
            allow_private_hosts=policy.allow_private_hosts,
        ):
            continue
        if not extension_allowed(href, allowed_extensions):
            continue
        if policy.url_contains and policy.url_contains not in href:
            continue
        text = anchor.get_text(" ", strip=True)
        if include_url_patterns and not matches_any_pattern(href, include_url_patterns):
            continue
        if exclude_url_patterns and matches_any_pattern(href, exclude_url_patterns):
            continue
        if include_text_patterns and not matches_any_pattern(text, include_text_patterns):
            continue
        if exclude_text_patterns and matches_any_pattern(text, exclude_text_patterns):
            continue
        date_iso = extract_date_from_text(text)
        if not date_matches_policy(date_iso, policy):
            continue
        title = text
        if date_iso:
            title = re.split(
                r"\s*\u63b2\u8f09\u65e5\s*|\s*\d{4}\u5e74\d{1,2}\u6708\d{1,2}\u65e5\s*",
                text,
                maxsplit=1,
            )[0].strip() or text
        links.append(
            {
                "url": href,
                "title_or_text": title[:500],
                "date": date_iso,
                "source_list_url": response["final_url"],
            }
        )
        if len(links) >= max(1, min(policy.max_links, HARD_MAX_PAGES)):
            break
    unique: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in links:
        if item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        unique.append(item)

    extract_id = make_crawl_id(list_url).replace(".", "_")
    extract_dir = output_root / "_site_crawl" / "_link_extract" / extract_id
    extract_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "extract_id": extract_id,
        "list_url": list_url,
        "allowed_domains": allowed_domains,
        "path_prefixes": policy.path_prefixes or [],
        "required_year": policy.required_year,
        "required_month": policy.required_month,
        "date_from": policy.date_from,
        "date_to": policy.date_to,
        "include_text_patterns": policy.include_text_patterns or [],
        "exclude_text_patterns": policy.exclude_text_patterns or [],
        "include_url_patterns": policy.include_url_patterns or [],
        "exclude_url_patterns": policy.exclude_url_patterns or [],
        "css_selector": policy.css_selector,
        "allowed_extensions": policy.allowed_extensions or [],
        "url_contains": policy.url_contains,
        "created_at": utc_now(),
        "link_count": len(unique),
        "links": unique,
    }
    (extract_dir / "links.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"manifest": manifest, "extract_dir": extract_dir}


def find_crawl_dir(output_root: Path, crawl_id: str | None = None) -> Path:
    root = output_root / "_site_crawl"
    if crawl_id:
        candidate = (root / crawl_id).resolve()
        if not candidate.exists() or not candidate.is_dir() or root.resolve() not in candidate.parents:
            raise FileNotFoundError(f"Unknown crawl_id: {crawl_id}")
        return candidate
    crawls = sorted([path for path in root.glob("*") if path.is_dir()], key=lambda path: path.name)
    if not crawls:
        raise FileNotFoundError("No site crawls have been created.")
    return crawls[-1]


def load_crawl_records(crawl_dir: Path) -> list[dict[str, Any]]:
    path = crawl_dir / "pages.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def terms_for_query(query: str) -> list[str]:
    terms = [term.strip().lower() for term in re.split(r"\s+", query) if term.strip()]
    return terms or [query.strip().lower()]


def make_snippet(text: str, terms: list[str], max_chars: int = 420) -> str:
    lower = text.lower()
    first = -1
    for term in terms:
        if not term:
            continue
        index = lower.find(term)
        if index >= 0 and (first < 0 or index < first):
            first = index
    if first < 0:
        return text[:max_chars]
    start = max(0, first - max_chars // 3)
    end = min(len(text), start + max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end] + suffix


def search_crawl(output_root: Path, query: str, crawl_id: str | None = None, max_results: int = 8) -> dict[str, Any]:
    crawl_dir = find_crawl_dir(output_root, crawl_id)
    terms = terms_for_query(query)
    records = load_crawl_records(crawl_dir)
    results: list[dict[str, Any]] = []
    for item in records:
        page_path = crawl_dir / item["text_path"]
        text = page_path.read_text(encoding="utf-8", errors="replace") if page_path.exists() else ""
        lower_text = text.lower()
        lower_title = str(item.get("title") or "").lower()
        score = sum(lower_text.count(term) for term in terms if term)
        score += 5 * sum(lower_title.count(term) for term in terms if term)
        if score <= 0:
            continue
        results.append(
            {
                "page_id": item["page_id"],
                "url": item["final_url"],
                "title": item.get("title") or item["final_url"],
                "score": score,
                "text_path": item["text_path"],
                "snippet": make_snippet(text, terms),
            }
        )
    results.sort(key=lambda item: (-item["score"], item["page_id"]))
    return {
        "crawl_id": crawl_dir.name,
        "query": query,
        "results": results[: max(1, min(max_results, 30))],
        "total_matches": len(results),
    }


def read_crawled_page(output_root: Path, page: str, crawl_id: str | None = None, max_chars: int = 12000) -> dict[str, Any]:
    crawl_dir = find_crawl_dir(output_root, crawl_id)
    records = load_crawl_records(crawl_dir)
    selected: dict[str, Any] | None = None
    for item in records:
        if str(item["page_id"]) == str(page) or item["final_url"] == page or item["url"] == page:
            selected = item
            break
    if selected is None:
        raise FileNotFoundError(f"Page not found in crawl {crawl_dir.name}: {page}")
    page_path = crawl_dir / selected["text_path"]
    text = page_path.read_text(encoding="utf-8", errors="replace") if page_path.exists() else ""
    truncated = len(text) > max_chars
    return {
        "crawl_id": crawl_dir.name,
        "page": selected,
        "content": text[:max_chars],
        "truncated": truncated,
    }


def list_crawls(output_root: Path, max_crawls: int = 10) -> dict[str, Any]:
    root = output_root / "_site_crawl"
    crawls = []
    for path in sorted(root.glob("*"), key=lambda item: item.name, reverse=True)[:max_crawls]:
        manifest_path = path / "crawl_manifest.json"
        manifest: dict[str, Any] = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        crawls.append(
            {
                "crawl_id": path.name,
                "manifest": manifest,
                "path": str(path),
            }
        )
    return {"crawls": crawls, "count": len(crawls)}
