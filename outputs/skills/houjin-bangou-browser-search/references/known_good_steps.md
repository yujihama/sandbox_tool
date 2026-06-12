# Known-Good Playwright Steps

Use these recipes before exploratory browsing when the requested site is
`www.houjin-bangou.nta.go.jp`. Replace placeholders with short search values
only. Keep `allowed_domains=["www.houjin-bangou.nta.go.jp"]`.

## Japanese Name Search

Use this when the task provides a Japanese corporate name or a reliable
Japanese fallback name.

```json
[
  {"action": "fill", "selector": "#corp_name", "value": "<company_name>"},
  {"action": "click", "selector": "#search_condition"},
  {"action": "wait", "milliseconds": 2000},
  {"action": "extract_text", "selector": "body"}
]
```

If a prefecture is part of the task or needed to disambiguate a broad result,
insert this before the search click:

```json
{"action": "select", "selector": "#addr_pref", "value": "<prefecture_name>"}
```

## English Name Search

Use this for English names before any Japanese-name fallback.

```json
[
  {"action": "fill", "selector": "#corp_name", "value": "<english_company_name>"},
  {"action": "click", "selector": "#corp_opt_en"},
  {"action": "click", "selector": "#search_condition"},
  {"action": "wait", "milliseconds": 2000},
  {"action": "extract_text", "selector": "body"}
]
```

If the Japanese homepage English option is unavailable, start from:

```text
https://www.houjin-bangou.nta.go.jp/en/kensaku-kekka.html
```

and use:

```json
[
  {"action": "fill", "selector": "#corp_name", "value": "<english_company_name>"},
  {"action": "click", "selector": "button[type='submit']"},
  {"action": "wait", "milliseconds": 2000},
  {"action": "extract_text", "selector": "body"}
]
```

## Direct Detail Verification

After extracting a 13-digit corporate number, open the detail page directly.
This avoids ambiguous result-row clicks.

```text
https://www.houjin-bangou.nta.go.jp/henkorireki-johoto.html?selHouzinNo=<corporate_number>
```

Use:

```json
[
  {"action": "extract_text", "selector": "title"},
  {"action": "extract_text", "selector": "body"}
]
```

## Result Parser

After each useful Playwright run, run:

```bash
python /input/browser-skills/houjin-bangou-browser-search/scripts/parse_houjin_playwright_result.py \
  "/outputs/_playwright/*/result.json" \
  --query "<submitted_or_target_company_name>" \
  --output /outputs/subtasks/houjin_parse_summary.json
```

If a prefecture filter is used, add `--prefecture "<prefecture_name>"`.

Use `best_matches[0]` when it has `match_type: "exact"` for the target query.
Do not use the first visual result row when it is not an exact name match. Use
the parser output to choose detail URLs and to populate the final report. Do
not read large `result.json` files manually unless the parser output is
insufficient.

## Coverage Expansion

When parser output has `coverage.must_continue_before_broad_candidate: true`,
the current result set is incomplete and no exact match was found. Do not return
`broad_candidate` yet. Follow `coverage.recommended_next_step`, but keep the
continuation bounded:

- Use at most one display-count expansion per input query.
- Use at most one stricter search-mode attempt per input query when the form
  visibly provides exact/prefix-style matching controls.
- Use at most two page/filter follow-up attempts per input query.
- When `coverage.minimum_followup_required` is true, attempt at least one
  recommended follow-up before returning `needs_more_search` or
  `coverage_incomplete`. The only exception is when the recommended control or
  action is visibly unavailable or fails; cite that evidence in the notes.
- If the parser still has no exact match after that budget, save the row as
  `coverage_incomplete` or `needs_more_search`, include the parser evidence,
  and request parent review. Do not keep browsing indefinitely.

### Expand Display Count

Rerun the same search and click the largest available display-count control
shown by parser `available_controls` (`100件` preferred, then `50件`).

```json
[
  {"action": "fill", "selector": "#corp_name", "value": "<company_name>"},
  {"action": "click", "selector": "#search_condition"},
  {"action": "wait", "milliseconds": 1500},
  {"action": "click", "text": "100件"},
  {"action": "wait", "milliseconds": 2000},
  {"action": "extract_text", "selector": "body"}
]
```

Then rerun the parser with the same `--query` and use the new
`best_matches`.

### Narrow Search Mode

When the first result page mostly contains names that merely contain the target
query, use a stricter match mode visible on the search form once before
settling for broad candidates. For legal-name verification tasks, prefix or
exact-style matching is usually more useful than paging through many
contains-style matches. Rerun the parser immediately after the stricter search.

### Follow Result Pages

If display expansion fails or is unavailable, follow page controls from the
current results:

```json
[
  {"action": "fill", "selector": "#corp_name", "value": "<company_name>"},
  {"action": "click", "selector": "#search_condition"},
  {"action": "wait", "milliseconds": 1500},
  {"action": "click", "text": "次の10件"},
  {"action": "wait", "milliseconds": 2000},
  {"action": "extract_text", "selector": "body"}
]
```

Repeat for numbered/next pages only within the bounded continuation budget.

### Apply Filters Only From Input Evidence

Use prefecture, city, address, corporate type, or similar filters only when the
input row or task provides that information. Do not infer a prefecture from a
well-known company name unless the task explicitly allows that fallback.
