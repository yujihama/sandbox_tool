from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sandbox_tool.site_crawler import (
    CrawlPolicy,
    LinkExtractPolicy,
    extract_links_from_listing,
    read_crawled_page,
    run_url_crawl,
    run_site_crawl,
    search_crawl,
)


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        routes = {
            "/robots.txt": (
                "text/plain; charset=utf-8",
                "User-agent: *\nDisallow: /blocked\n",
            ),
            "/index.html": (
                "text/html; charset=utf-8",
                """
                <html>
                  <head><title>脱炭素ポータル</title></head>
                  <body>
                    <main>
                      <h1>脱炭素ポータル</h1>
                      <a href="/carbon/page1.html">企業向け情報</a>
                      <a href="/blocked.html">blocked</a>
                      <a href="https://example.com/outside.html">outside</a>
                    </main>
                  </body>
                </html>
                """,
            ),
            "/carbon/page1.html": (
                "text/html; charset=utf-8",
                """
                <html>
                  <head><title>企業向け脱炭素情報</title></head>
                  <body>
                    <main>
                      <h1>企業向け脱炭素情報</h1>
                      <p>企業の脱炭素経営、補助金、再エネ導入に関する情報です。</p>
                    </main>
                  </body>
                </html>
                """,
            ),
            "/topics/": (
                "text/html; charset=utf-8",
                """
                <html>
                  <head><title>トピックス</title></head>
                  <body>
                    <a class="imgbox" href="/topics/a.html">
                      <h2>2025 article A</h2>
                      <p>掲載日 2025年1月2日 カテゴリ 普及啓発</p>
                    </a>
                    <a class="imgbox" href="/topics/b.html">
                      <h2>2025 article B</h2>
                      <p>掲載日 2025年12月31日 カテゴリ 取組事例</p>
                    </a>
                    <a class="imgbox" href="/topics/c.html">
                      <h2>2024 article C</h2>
                      <p>掲載日 2024年12月31日 カテゴリ 取組事例</p>
                    </a>
                  </body>
                </html>
                """,
            ),
            "/topics/a.html": (
                "text/html; charset=utf-8",
                "<html><head><title>A</title></head><body><main><h1>A</h1><p>補助金と企業向け支援。</p></main></body></html>",
            ),
            "/topics/b.html": (
                "text/html; charset=utf-8",
                "<html><head><title>B</title></head><body><main><h1>B</h1><p>自治体の脱炭素支援。</p></main></body></html>",
            ),
            "/topics/c.html": (
                "text/html; charset=utf-8",
                "<html><head><title>C</title></head><body><main><h1>C</h1><p>古い記事。</p></main></body></html>",
            ),
            "/blocked.html": (
                "text/html; charset=utf-8",
                "<html><body>blocked content should not be fetched</body></html>",
            ),
        }
        if self.path not in routes:
            self.send_response(404)
            self.end_headers()
            return
        content_type, body = routes[self.path]
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


class SiteCrawlerTests(unittest.TestCase):
    def test_crawl_respects_allowlist_and_searches_local_index(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with tempfile.TemporaryDirectory() as temp:
                output_root = Path(temp)
                result = run_site_crawl(
                    output_root,
                    CrawlPolicy(
                        start_url=f"{base_url}/index.html",
                        allowed_domains=["127.0.0.1"],
                        max_pages=5,
                        max_depth=1,
                        request_delay_seconds=0,
                        allow_private_hosts=True,
                    ),
                )

                records = result["records"]
                fetched_urls = {item["final_url"] for item in records}
                self.assertIn(f"{base_url}/index.html", fetched_urls)
                self.assertIn(f"{base_url}/carbon/page1.html", fetched_urls)
                self.assertNotIn(f"{base_url}/blocked.html", fetched_urls)
                self.assertTrue((result["crawl_dir"] / "site_index.sqlite").exists())

                search = search_crawl(output_root, "補助金", max_results=3)
                self.assertEqual(search["total_matches"], 1)
                self.assertIn("企業向け脱炭素情報", search["results"][0]["title"])

                page = read_crawled_page(output_root, str(search["results"][0]["page_id"]))
                self.assertIn("補助金", page["content"])
        finally:
            server.shutdown()
            server.server_close()

    def test_extract_listing_links_and_crawl_explicit_urls(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with tempfile.TemporaryDirectory() as temp:
                output_root = Path(temp)
                extracted = extract_links_from_listing(
                    output_root,
                    LinkExtractPolicy(
                        list_url=f"{base_url}/topics/",
                        allowed_domains=["127.0.0.1"],
                        path_prefixes=["/topics/"],
                        required_year=2025,
                        allow_private_hosts=True,
                    ),
                )
                links = extracted["manifest"]["links"]
                self.assertEqual(len(links), 2)
                self.assertEqual(
                    {item["date"] for item in links},
                    {"2025-01-02", "2025-12-31"},
                )

                crawl = run_url_crawl(
                    output_root,
                    [item["url"] for item in links],
                    CrawlPolicy(
                        start_url=links[0]["url"],
                        allowed_domains=["127.0.0.1"],
                        path_prefixes=["/topics/"],
                        request_delay_seconds=0,
                        allow_private_hosts=True,
                    ),
                )
                fetched_urls = {item["final_url"] for item in crawl["records"]}
                self.assertEqual(
                    fetched_urls,
                    {f"{base_url}/topics/a.html", f"{base_url}/topics/b.html"},
                )
        finally:
            server.shutdown()
            server.server_close()

    def test_extract_listing_links_supports_month_text_url_selector_filters(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with tempfile.TemporaryDirectory() as temp:
                extracted = extract_links_from_listing(
                    Path(temp),
                    LinkExtractPolicy(
                        list_url=f"{base_url}/topics/",
                        allowed_domains=["127.0.0.1"],
                        path_prefixes=["/topics/"],
                        required_year=2025,
                        required_month=12,
                        date_from="2025-12-01",
                        date_to="2025-12-31",
                        include_text_patterns=["article B"],
                        exclude_text_patterns=["article C"],
                        include_url_patterns=[r"/topics/[ab]\.html$"],
                        exclude_url_patterns=[r"/topics/c\.html$"],
                        css_selector="a.imgbox",
                        allowed_extensions=[".html"],
                        allow_private_hosts=True,
                    ),
                )
                links = extracted["manifest"]["links"]
                self.assertEqual(len(links), 1)
                self.assertEqual(links[0]["date"], "2025-12-31")
                self.assertEqual(links[0]["url"], f"{base_url}/topics/b.html")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
