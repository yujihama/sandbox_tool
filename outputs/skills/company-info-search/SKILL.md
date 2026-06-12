---
name: company-info-search
description: Use guarded public-site workflows to verify company existence and registry facts. Includes a Japan National Tax Agency Corporate Number Publication Site recipe and parser for sourced company-information reports.
---

# Company Info Search

## Overview

Use this skill when a task asks for company-existence checks, registry lookups,
or search-result summaries on public company-information sites. Keep the work as
generic guarded browser automation: call `run_playwright_task` with an explicit
allowlist, and prefer the known-good recipes and parsers bundled with this skill
before exploratory browsing.

The current bundled recipe covers Japan's National Tax Agency Corporate Number
Publication Site.

## Required Guardrails

- Use only `https://www.houjin-bangou.nta.go.jp/` and pass
  `allowed_domains=["www.houjin-bangou.nta.go.jp"]` to every browser call.
- Do not upload files or send artifact contents through browser fields.
- Keep search values short: company names, corporate numbers, prefectures, or
  other normal search criteria only.
- Do not assert exact existence from a broad/fallback query unless the returned
  legal name, corporate number, and detail page support it.

## Workflow

1. Load the known-good recipe.
   - Read
     `/input/profile-skills/company-info-search/references/known_good_steps.md`.
   - Use the recipe steps first when the task fits name search or direct detail
     verification. Do not re-discover the same selectors unless the recipe
     fails.

2. Search using the closest recipe.
   - For Japanese names, submit the provided legal/common name directly.
   - For English names, first use the English-name option if present. If the
     site returns no useful results, use a clearly labeled Japanese-name
     fallback only when the mapping is obvious from the task context or common
     official usage. Otherwise report the English-search result and limitation.
   - For corporate numbers, prefer direct number search or detail-page
     navigation when a number is already known.

3. Parse saved browser results mechanically.
   - After useful Playwright runs, execute:

```bash
python /input/profile-skills/company-info-search/scripts/parse_houjin_playwright_result.py \
  "/outputs/_playwright/*/result.json" \
  --query "<submitted_or_target_company_name>" \
  --output /outputs/subtasks/houjin_parse_summary.json
```

   - Add `--prefecture "<prefecture_name>"` when a prefecture filter was used.
   - Use `/outputs/subtasks/houjin_parse_summary.json` to extract result rows,
     ranked `best_matches`, corporate numbers, detail URLs, no-data signals,
     titles, and detail-page facts.
   - Use `best_matches[0]` only when it has `match_type: "exact"` for the
     target query. Do not use the first visual result row if its legal name is
     not an exact match.
   - If `coverage.must_continue_before_broad_candidate` is `true`, do not close
     the task as `broad_candidate`. Continue with
     `coverage.recommended_next_step`.
   - If `coverage.minimum_followup_required` is `true`, do not close as
     `needs_more_search` or `coverage_incomplete` until at least one
     recommended follow-up browser run has been attempted, or until the browser
     result shows that the recommended control/action is unavailable.
   - Keep this continuation bounded. For each input query, use at most
     `coverage.max_additional_search_runs_per_query` additional search/browser
     runs after the initial broad result. If no exact match is found after that
     budget, report `coverage_incomplete` or `needs_more_search` and request
     parent review instead of continuing to browse.
   - Do not spend model turns manually reading large Playwright JSON unless the
     parser output is insufficient.

4. Verify selected candidates.
   - For each exact or likely candidate that needs verification, open its direct
     detail URL:
     `https://www.houjin-bangou.nta.go.jp/henkorireki-johoto.html?selHouzinNo=<corporate_number>`
   - Run the parser again after detail-page navigation and compare the detail
     page name, corporate number, and address against the result row.

5. Explore only on recipe failure.
   - If a known selector is missing, run one observation-oriented Playwright
     call to inspect visible text, forms, inputs, and tables.
   - Update the next Playwright call from the observed DOM. Do not repeat a
     failed action unchanged.

6. Use broad candidates only after coverage is sufficient.
   - A first-page broad match is not enough when the parser says coverage is
     incomplete.
   - Broad candidate is acceptable only when the visible result set has been
     covered enough to rule out an exact match, or when the report explicitly
     states the site/control limitation that prevented further coverage.
   - If the output schema permits it, prefer `needs_more_search` or
     `coverage_incomplete` over `broad_candidate` when coverage remains
     incomplete.
   - Once the bounded continuation budget is spent, stop browsing, write the
     artifact with the incomplete-coverage status, and call
     `request_parent_review`.

7. Report with uncertainty separated from facts.
   - Include original query, submitted query, match type
     (`exact`, `broad`, `fallback`, or `not_found`), corporate number, legal
     name, address/location, source URL, and retrieval time when available.
   - If a fallback query was used, state that explicitly.
   - If only broad candidates were found, do not mark the target as confirmed.

## Self-Check

Before requesting parent review:

- Re-read the saved Playwright `result.json` or `summary.md`.
- Re-run the parser and use its JSON output as the main evidence index.
- Confirm every reported row appears in the saved browser trace.
- Confirm every detail URL stays under `www.houjin-bangou.nta.go.jp`.
- Confirm no row is closed as `broad_candidate` while parser coverage says
  `must_continue_before_broad_candidate: true`.
- Confirm no row is closed as `needs_more_search` or `coverage_incomplete`
  while parser coverage says `minimum_followup_required: true` unless the final
  notes cite the follow-up run attempted or the unavailable control/action.
- Confirm the final artifact distinguishes exact matches from broad candidates.
