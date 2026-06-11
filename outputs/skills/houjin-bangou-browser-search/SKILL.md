---
name: houjin-bangou-browser-search
description: Use guarded Playwright to search Japan's National Tax Agency Corporate Number Publication Site at houjin-bangou.nta.go.jp, extract corporate-number search results and detail pages, and produce sourced verification reports for company-existence tasks.
---

# Houjin Bangou Browser Search

## Overview

Use this skill when a task asks for company-existence checks or search-result
summaries on the Japanese Corporate Number Publication Site. Keep the work as
generic browser automation: call `run_playwright_task` with an explicit
allowlist and inspect its saved traces before deciding the next action.

## Required Guardrails

- Use only `https://www.houjin-bangou.nta.go.jp/` and pass
  `allowed_domains=["www.houjin-bangou.nta.go.jp"]` to every browser call.
- Do not upload files or send artifact contents through browser fields.
- Keep search values short: company names, corporate numbers, prefectures, or
  other normal search criteria only.
- Do not assert exact existence from a broad/fallback query unless the returned
  legal name, corporate number, and detail page support it.

## Workflow

1. Start with an observation-only call.
   - Go to `https://www.houjin-bangou.nta.go.jp/`.
   - Wait for `body`.
   - Extract visible text, links, forms, inputs, and tables.
   - Read `/outputs/_playwright/<run_id>/result.json` or `summary.md` before
     issuing a second call.

2. Choose the search mode from observed controls.
   - Company-name input is usually `#corp_name`.
   - The search button is usually `#search_condition`.
   - English-name search may use an option such as `#corp_opt_en`; confirm from
     the observed DOM before using it.
   - If selectors differ, use the labels and page structure from the first
     observation instead of guessing.

3. Search.
   - For Japanese names, submit the provided legal/common name directly.
   - For English names, first use the English-name option if present. If the
     site returns no useful results, use a clearly labeled Japanese-name
     fallback only when the mapping is obvious from the task context or common
     official usage. Otherwise report the English-search result and limitation.
   - For corporate numbers, prefer direct number search or detail-page
     navigation when a number is already known.

4. Extract and verify results.
   - Prefer the structured tables in the Playwright `result.json` over repeated
     manual scraping.
   - Capture the result URL, page title, submitted query, result count, and the
     top rows requested by the task.
   - For each selected row, open or navigate to its detail URL if available.
     Detail pages commonly use:
     `https://www.houjin-bangou.nta.go.jp/henkorireki-johoto.html?selHouzinNo=<corporate_number>`
   - Verify that the detail page contains the corporate number and legal name.

5. Report with uncertainty separated from facts.
   - Include original query, submitted query, match type
     (`exact`, `broad`, `fallback`, or `not_found`), corporate number, legal
     name, address/location, source URL, and retrieval time when available.
   - If a fallback query was used, state that explicitly.
   - If only broad candidates were found, do not mark the target as confirmed.

## Self-Check

Before requesting parent review:

- Re-read the saved Playwright `result.json` or `summary.md`.
- Confirm every reported row appears in the saved browser trace.
- Confirm every detail URL stays under `www.houjin-bangou.nta.go.jp`.
- Confirm the final artifact distinguishes exact matches from broad candidates.
