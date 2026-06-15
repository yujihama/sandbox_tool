---
name: table-image-extraction
description: Extract tables from images, screenshots, scanned sheets, inventory sheets, stock sheets, and photos of handwritten or printed forms. Use when the user asks to read a visible table from PNG/JPEG/PDF page images, reconstruct rows and columns, preserve handwritten values, handle merged cells or side notes, or convert a Japanese or multilingual table image to Markdown/CSV/JSON/Excel-ready data.
---

# Table Image Extraction

## Purpose

Use this skill to improve table extraction from visual documents. It is not a
fixed OCR pipeline. It is a checklist for deciding when to inspect the whole
image, when to create temporary crops or overlays, and how to avoid common table
structure errors.

## Workflow

1. Inspect the whole image first.
   - Identify the visible column headers before extracting rows.
   - Note the row count, the first visible data row, the last visible data row,
     merged cells, group labels, side notes, cropped edges, and shadows.
   - Distinguish visible table headers from user-requested output fields. If a
     requested notes/remarks column is not visible in the image, still use it
     as an output notes column when helpful, but state that it is not a visible
     source header.
   - Do not start the final table until you know how the visible columns align.

2. Create visual aids only when they help.
   - If the table is skewed, faint, cropped, or handwritten, write a short
     task-specific helper under `/outputs` to create useful intermediate images.
   - Good aids include perspective-corrected copies, contrast/sharpened copies,
     header crops, first-row crops, bottom-row crops, column crops, row crops,
     and grid/row-number overlays.
   - Use the simplest transformation that answers the uncertainty. Do not run a
     full fixed pipeline if one crop or overlay is enough.
   - Inspect generated helper images with the image inspection tool before using
     them as evidence.

3. Read in passes.
   - Pass A: headers, merged cells, and table geometry.
   - Pass B: first 2-3 data rows, including any row close to a thick line or
     page fold.
   - Pass C: middle rows and repeated patterns.
   - Pass D: bottom rows and any partially visible final rows.
   - Pass E: uncertain cells, side notes, and arithmetic-like handwritten cells.
   - If a later pass changes a product name, header, or numeric value from an
     earlier pass, treat that as a conflict. Resolve the conflict with a targeted
     crop, zoomed inspection, or an explicit `[unclear: ...]` marker rather than
     silently choosing one reading.

4. Preserve what is visible.
   - Keep blank cells blank.
   - Preserve handwritten arithmetic as written, such as `15 + 31`; do not
     compute or normalize it unless the user asks.
   - Use `[unclear: best guess]` for ambiguous values and explain why in a notes
     column or self-check.
   - Do not invent missing values from formulas or row consistency.
   - Do not propagate a label into every row unless it is clearly a merged/group
     cell. If the label might belong to one row only, keep it on that row or mark
     the group assignment uncertain.

5. Treat side notes cautiously.
   - Include side notes in a notes/remarks column when they are visibly related
     to a row or row group.
   - If a side note is near several rows but not anchored to one row, say that
     the association is uncertain.
   - For side notes that span many row heights, compare their vertical overlap
     with the ruled rows before assigning them. If the anchor is not clear,
     report the note as a table-level or group-level note instead of attaching
     it to the nearest lower row.
   - If a right-margin adjustment mark is outside the ruled table and line
     alignment is weak, collect it in a general notes sentence rather than
     assigning it to a specific row.
   - Never use side notes to overwrite table cells unless the visual evidence is
     clear.

6. Handle tiny trailing marks conservatively.
   - When a handwritten number has a small trailing mark, do not turn that mark
     into a character unless the crop makes it clearly legible.
   - Prefer `[unclear: 36 plus unreadable mark]` over a forced reading such as a
     guessed kanji or kana.
   - If you cannot perform or inspect a targeted crop for the mark, use a
     neutral description such as `unreadable trailing mark`; do not invent a
     reading from context.
   - If multiple inspection passes disagree, use the less specific uncertain
     transcription.

7. Use targeted checks for high-risk cells.
   - Before finalizing, identify cells where a wrong reading would materially
     change the result: first row values, last row values, totals, handwritten
     calculations, product names, and cells with extra marks.
   - If one of those cells remains uncertain after whole-image inspection,
     consider a targeted crop, zoomed copy, contrast copy, or row overlay.
   - If you choose not to create a helper image, explicitly keep the uncertainty
     in the final answer rather than forcing a reading.

8. Self-check before the final answer.
   - Re-check the first visible data row and last visible data row.
   - Re-check that every numeric value is under the correct header.
   - Re-check that row count matches the visible rows.
   - Re-check that group labels or merged cells were not incorrectly copied to
     unrelated rows.
   - Re-check that non-visible requested fields are clearly labeled as derived
     notes, not source headers.
   - List unresolved uncertain cells and cropped or unreadable areas.

## Output

Return a Markdown table inline unless the user asks for a file artifact.

For each extracted table, include:

- the table itself
- row count
- uncertain cells
- notes about cropped edges, shadows, merged cells, or side-note assignment
- a concise self-check stating how column alignment and first/last rows were
  verified

If the requested output is CSV, JSON, or Excel, first create the structured table
using the same evidence discipline, then write the file in the requested format.
