---
name: seal-surname-identification
description: Identify surnames or names from Japanese red seal, hanko, inkan, stamp, 印鑑, はんこ, 判子, 印影 PNG/JPEG images. Use for image processing, red ink extraction, contact sheets, candidate surname reporting, confidence scoring, and self-checking seal-reading artifacts.
compatibility: Python with PIL, numpy, and optionally scipy. Designed for Deep Agents sandbox file tasks.
allowed-tools: read_file write_file edit_file ls glob execute
---

# seal-surname-identification

## Purpose

Use this skill when the task involves identifying a Japanese surname or name from red seal images (`印鑑`, `はんこ`, `判子`, `印影`, hanko, inkan, stamp).

The goal is not only to guess the name, but to produce inspectable evidence:

- processed images that make the red strokes easier to read
- a contact sheet comparing each image
- structured JSON with candidates, confidence, evidence, and limitations
- a report that clearly separates high-confidence readings from uncertain ones

## Workflow

1. Inventory the inputs.
   - Use `ls` or `glob` to find all target images.
   - Do not infer the name from the filename or prompt. Use image content.
   - Record image dimensions and whether each image can be opened.

2. Run the helper preprocessing script.
   - Helper script path: `/input/skills/seal-surname-identification/scripts/seal_preprocess.py`
   - Typical command:

```bash
python /input/skills/seal-surname-identification/scripts/seal_preprocess.py \
  --output-dir /outputs/seal_processing \
  /input/test01.png /input/test02.png /input/test03.png
```

   - The script writes cropped images, black/white red-ink masks, a contact sheet, and `seal_processing_summary.json`.
   - If the helper fails, write a small task-specific script under `/outputs` using the same approach: red extraction, crop, binary mask, contact sheet.

3. Inspect processed images.
   - Use the generated contact sheet and per-image `_bw.png` / `_crop.png` artifacts.
   - For each seal, identify likely characters and layout:
     - vertical two-character layout
     - horizontal two-character layout
     - one-character or stylized company seal
     - 印相体 / 篆書-like stylization where strokes may merge
   - If a character is uncertain, list multiple candidates rather than forcing a single answer.

4. Produce a structured result.
   - Write a JSON artifact with one object per image:
     - `file`
     - `estimated_surname`
     - `reading`
     - `confidence` as 0.0 to 1.0
     - `candidates` with candidate surname, reading, and reason
     - `evidence` as concrete visual features
     - `limitations`
     - `processed_files`
   - Write a Japanese Markdown report with:
     - processing method
     - per-image result
     - confidence and uncertainty
     - candidate alternatives
     - evidence tied to visible strokes

5. Confidence discipline.
   - `0.85-1.00`: highly legible or obvious seal.
   - `0.65-0.84`: plausible, with some distortion or missing strokes.
   - `0.40-0.64`: low confidence; do not present a single surname as settled. Set `estimated_surname` to `判読困難（候補あり）` unless the user explicitly requires one best guess, and include multiple candidates with reasons.
   - `<0.40`: do not assert a surname; use `判読困難` and list candidates if any.
   - If a low-confidence image still needs a best candidate, put it in `candidates[0]` rather than treating it as a reliable answer.

6. Self-check.
   - Your self-check can verify file handling and artifact consistency, not the semantic correctness of the name unless ground truth is supplied.
   - At minimum verify:
     - all input images open with PIL
     - processed images/contact sheet open
     - JSON parses and has one result per input image
     - report mentions every input image
     - low-confidence cases include limitations/candidates
   - Explicitly state that visual name correctness remains a human-review item.

## Output Conventions

Use these names unless the user requested different names:

- `/outputs/seal_surname_identification_report.md`
- `/outputs/seal_surname_identification_summary.json`
- `/outputs/seal_surname_identification_contact_sheet.png`
- `/outputs/seal_processing/` for helper outputs

## Reading Guidance

When describing visual evidence, be concrete:

- "下部に縦画3本があり川に近い"
- "右側に外枠と内部区画があり田に近い"
- "下段が多画で藤の塊に近い"
- "上段は佐/加/伊の弁別が難しい"

Avoid overclaiming:

- Do not say "確定" unless the image is very clear.
- Do not hide uncertainty in a single answer.
- Do not let the processed black/white mask override the original image. Always compare original, crop, and mask before naming the surname.
- For low-quality seals, phrase the result as "第一候補はXだが、Y/Zもあり得る" or use `判読困難（候補あり）`.
- Do not use OCR availability as a reason to skip manual image processing.
- If OCR is unavailable, say so and proceed with image processing plus visual reasoning.
