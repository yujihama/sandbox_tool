---
name: seal-surname-identification
description: Identify surnames or names from Japanese red seal, hanko, inkan, stamp, seal PNG/JPEG images. Use for adaptive image preprocessing, red ink extraction, contact sheets, candidate surname reporting, confidence scoring, and self-checking seal-reading artifacts.
compatibility: Python with PIL, numpy, and optionally scipy. Designed for Deep Agents sandbox file tasks.
allowed-tools: read_file write_file edit_file ls glob execute
---

# seal-surname-identification

## Purpose

Use this skill when the task involves identifying a Japanese surname or name
from red seal images: inkan, hanko, stamp, red seal, or seal impression images.

The goal is not to run one fixed preprocessing recipe. The goal is to make the
seal easier to read by trying small, inspectable preprocessing experiments,
then reporting what can and cannot be supported by the image evidence.

## Core Principle

Do not treat any processed image as ground truth.

- Start from the original image and raw split crops.
- Use preprocessing variants to reveal possible stroke structure.
- Compare variants against the raw image.
- If variants disagree, lower confidence and list alternatives.
- Do not force a surname just because one variant looks suggestive.

## Adaptive Workflow

1. Inventory the inputs.
   - Use `ls` or `glob` to find target images.
   - Do not infer names from filenames, prompts, or previous runs.
   - Record dimensions and whether each image opens.

2. Inspect the raw image first.
   - Use vision on the source image or a raw crop before running destructive
     transformations.
   - Identify whether the image is one seal, multiple seals in a table, tilted,
     very small, faint, blurred, or contaminated by table lines/background marks.

3. Choose preprocessing experiments based on the observed problem.
   - For multiple seals in one image, run the helper with `--split-stamps`.
   - For small, blurred, faint, or uncertain seals, add
     `--variant-set adaptive`.
   - For tilt or uncertain orientation, adjust `--angles`.
   - If splitting misses or merges seals, rerun with a different
     `--split-min-area-ratio`.
   - If the helper output is not enough, write a small task-specific script under
     `/outputs` and explain why that extra experiment was needed.
   - When inspecting very tight crops, remember that the vision tool pads
     near-edge image content internally before sending it. Still avoid creating
     crops that cut through strokes or borders.

4. Run a baseline or adaptive helper pass.

Helper path:

```bash
python /input/skills/seal-surname-identification/scripts/seal_preprocess.py
```

For ordinary one-stamp-per-file inputs, a baseline pass is:

```bash
python /input/skills/seal-surname-identification/scripts/seal_preprocess.py \
  --output-dir /outputs/seal_processing \
  --angles=-15,-10,-5,0,5,10,15 \
  /input/test01.png /input/test02.png
```

For a table or approval matrix containing multiple seals:

```bash
python /input/skills/seal-surname-identification/scripts/seal_preprocess.py \
  --output-dir /outputs/seal_processing \
  --split-stamps \
  --angles=-15,-10,-5,0,5,10,15 \
  /input/table_with_stamps.png
```

For small or uncertain seals, let the helper create comparison variants:

```bash
python /input/skills/seal-surname-identification/scripts/seal_preprocess.py \
  --output-dir /outputs/seal_processing \
  --split-stamps \
  --variant-set adaptive \
  --angles=-15,-10,-5,0,5,10,15 \
  /input/table_with_stamps.png
```

The adaptive variant set writes per-seal comparison images such as:

- `raw_lanczos`: smoothly upscaled raw crop
- `raw_nearest`: pixel-edge-preserving raw crop
- `contrast_sharpened`: autocontrast plus mild sharpening
- `red_isolated`: red pixels on white background
- `red_clean_bw`: clean black/white red mask
- `adaptive_threshold`: local adaptive threshold on red emphasis; useful when
  background or lighting varies across the image
- `clahe_red_emphasis`: CLAHE/equalized red emphasis; useful for faint ink while
  limiting noise amplification
- `morph_dilation`: expands red foreground; can reconnect broken or faded lines
- `morph_closing`: closes small gaps/holes in strokes
- `morph_erosion`: shrinks foreground; can reduce ink bleed or overly thick
  strokes
- `raw_tile`: repeated raw crop grid for very small images

5. Inspect and compare, do not just consume the contact sheet.
   - Prefer raw source image and raw split crops for final evidence.
   - Use contact sheets to decide what to inspect next, not as the only input.
   - Inspect the most useful variants individually when a seal remains unclear.
   - Compare at least two substantially different views before raising
     confidence: for example raw crop plus red-isolated, or raw crop plus
     angle-corrected crop.
   - If a BW mask removes or invents strokes, treat it as a warning rather than
     a confirmation.
   - Use morphology variants directionally: dilation/closing can make broken
     strokes readable but may merge separate strokes; erosion can separate
     bleed but may delete thin strokes.

6. Iterate only when the observation justifies it.
   - Do one more helper run if the first run obviously cropped too tightly,
     merged seals, missed a better angle range, or over-cleaned strokes.
   - Keep the loop bounded. Usually one baseline/adaptive pass plus one repair
     pass is enough.
   - Do not spend the whole task generating variants if the raw image is already
     legible or clearly unreadable.

7. Report the reasoning and uncertainty.
   - State which preprocessing experiments were actually used.
   - Identify the evidence that supported or weakened each candidate.
   - Give candidates with confidence. Do not present low-confidence candidates
     as settled names.
   - If the final answer is inline-only, do not create final report artifacts
     unless requested; temporary helper files under `/outputs` are fine.

## Output Conventions

Use these names when the task asks for saved artifacts:

- `/outputs/seal_surname_identification_report.md`
- `/outputs/seal_surname_identification_summary.json`
- `/outputs/seal_surname_identification_contact_sheet.png`
- `/outputs/seal_processing/` for helper outputs
- `/outputs/seal_processing/split_stamps/` for raw per-stamp crops
- `/outputs/seal_processing/seal_angle_contact_sheet.png`
- `/outputs/seal_processing/seal_variant_contact_sheet.png`
- `/outputs/seal_processing/variants/`

## Confidence Discipline

- `0.85-1.00`: highly legible or obvious seal.
- `0.65-0.84`: plausible, with some distortion or missing strokes.
- `0.40-0.64`: uncertain; include multiple candidates and clear limitations.
- `<0.40`: do not assert a surname; report cannot determine, with candidates if
  the image supports them.

If a low-confidence image still needs a best candidate, put it first in the
candidate list and mark it as a weak candidate.

## Self-Check

Your self-check can verify file handling and artifact consistency, not the
semantic correctness of the name unless ground truth is supplied.

At minimum verify:

- all input images open with PIL
- expected split count roughly matches visible seals
- generated crops/contact sheets/variant images open
- each reported seal maps to a visible source position
- low-confidence cases include limitations and alternatives

Explicitly state that visual name correctness remains a human-review item when
the image is low quality or stylized.

## Reading Guidance

When describing visual evidence, be concrete:

- character count and layout: vertical two-character, horizontal two-character,
  2x2 four-character, one large stylized character
- radicals or stroke groups: water-radical-like left strokes, box-like lower
  component, mountain-like top, tree-like branching lower strokes
- variant disagreement: raw crop suggests one candidate, BW mask collapses it,
  angle candidate clarifies or weakens a stroke

Avoid overclaiming:

- Do not say confirmed unless the image is very clear.
- Do not hide uncertainty in a single answer.
- Do not let processed black/white masks override the original image.
- Do not use OCR availability as a reason to skip visual image processing.
