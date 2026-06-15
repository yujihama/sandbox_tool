from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def red_mask(rgb: np.ndarray) -> np.ndarray:
    arr = rgb.astype(np.int16)
    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]
    red_score = r - ((g + b) / 2)
    mask = (r > 80) & ((r - g) > 20) & ((r - b) > 20) & (red_score > 35)
    if mask.sum() < 25:
        mask = (r > 100) & (red_score > 25)
    return mask


def clean_mask(mask: np.ndarray) -> np.ndarray:
    try:
        from scipy import ndimage

        structure = np.ones((3, 3), dtype=bool)
        cleaned = ndimage.binary_opening(mask, structure=structure)
        cleaned = ndimage.binary_closing(cleaned, structure=structure)
        if cleaned.sum() >= max(10, mask.sum() * 0.2):
            return cleaned
    except Exception:
        pass
    return mask


def bbox_from_mask(mask: np.ndarray, pad: int, width: int, height: int) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask)
    if len(xs) == 0 or len(ys) == 0:
        return 0, 0, width, height
    left = max(0, int(xs.min()) - pad)
    top = max(0, int(ys.min()) - pad)
    right = min(width, int(xs.max()) + pad + 1)
    bottom = min(height, int(ys.max()) + pad + 1)
    return left, top, right, bottom


def expand_bbox(
    bbox: tuple[int, int, int, int],
    pad: int,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    return (
        max(0, left - pad),
        max(0, top - pad),
        min(width, right + pad),
        min(height, bottom + pad),
    )


def bboxes_overlap(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def merge_close_bboxes(
    bboxes: list[tuple[int, int, int, int]],
    margin: int,
    width: int,
    height: int,
) -> list[tuple[int, int, int, int]]:
    groups = list(bboxes)
    changed = True
    while changed:
        changed = False
        merged: list[tuple[int, int, int, int]] = []
        used = [False] * len(groups)
        for index, box in enumerate(groups):
            if used[index]:
                continue
            current = box
            used[index] = True
            expanded_current = expand_bbox(current, margin, width, height)
            for other_index in range(index + 1, len(groups)):
                if used[other_index]:
                    continue
                other = groups[other_index]
                if bboxes_overlap(expanded_current, expand_bbox(other, margin, width, height)):
                    current = (
                        min(current[0], other[0]),
                        min(current[1], other[1]),
                        max(current[2], other[2]),
                        max(current[3], other[3]),
                    )
                    expanded_current = expand_bbox(current, margin, width, height)
                    used[other_index] = True
                    changed = True
            merged.append(current)
        groups = merged
    return groups


def connected_component_bboxes(mask: np.ndarray) -> list[dict]:
    try:
        from scipy import ndimage

        labels, count = ndimage.label(mask)
        objects = ndimage.find_objects(labels)
        components: list[dict] = []
        for label, slices in enumerate(objects, start=1):
            if slices is None:
                continue
            y_slice, x_slice = slices
            component_mask = labels[slices] == label
            area = int(component_mask.sum())
            components.append(
                {
                    "area": area,
                    "bbox": (
                        int(x_slice.start),
                        int(y_slice.start),
                        int(x_slice.stop),
                        int(y_slice.stop),
                    ),
                }
            )
        return components
    except Exception:
        pass

    height, width = mask.shape
    visited = np.zeros(mask.shape, dtype=bool)
    components = []
    for start_y, start_x in zip(*np.where(mask & ~visited)):
        if visited[start_y, start_x]:
            continue
        stack = [(int(start_y), int(start_x))]
        visited[start_y, start_x] = True
        area = 0
        min_x = max_x = int(start_x)
        min_y = max_y = int(start_y)
        while stack:
            y, x = stack.pop()
            area += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            for ny in range(max(0, y - 1), min(height, y + 2)):
                for nx in range(max(0, x - 1), min(width, x + 2)):
                    if mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
        components.append({"area": area, "bbox": (min_x, min_y, max_x + 1, max_y + 1)})
    return components


def split_stamp_crops(
    path: Path,
    output_dir: Path,
    source_index: int,
    min_area_ratio: float,
) -> list[dict]:
    original = Image.open(path).convert("RGB")
    width, height = original.size
    mask = clean_mask(red_mask(np.asarray(original)))
    red_pixels = int(mask.sum())
    if red_pixels < 25:
        return []

    min_area = max(30, int(red_pixels * min_area_ratio))
    min_extent = max(8, int(min(width, height) * 0.035))
    components = []
    for component in connected_component_bboxes(mask):
        left, top, right, bottom = component["bbox"]
        box_width = right - left
        box_height = bottom - top
        if component["area"] < min_area:
            continue
        if box_width < min_extent or box_height < min_extent:
            continue
        components.append(component["bbox"])
    if not components:
        return []

    merge_margin = max(6, int(min(width, height) * 0.035))
    groups = merge_close_bboxes(components, merge_margin, width, height)
    groups = [
        box
        for box in groups
        if (box[2] - box[0]) >= min_extent and (box[3] - box[1]) >= min_extent
    ]
    groups.sort(key=lambda box: (box[1] // max(1, height // 4), box[0]))
    if len(groups) <= 1:
        return []

    crop_dir = output_dir / "split_stamps"
    crop_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for stamp_index, bbox in enumerate(groups, start=1):
        box_width = bbox[2] - bbox[0]
        box_height = bbox[3] - bbox[1]
        pad = max(8, int(min(box_width, box_height) * 0.18))
        crop_bbox = expand_bbox(bbox, pad, width, height)
        crop = original.crop(crop_bbox)
        crop_path = crop_dir / f"{path.stem}_stamp_{stamp_index:02d}.png"
        crop.save(crop_path)
        records.append(
            {
                "source_input": str(path),
                "source_index": source_index,
                "split_index": stamp_index,
                "source_bbox": list(crop_bbox),
                "split_crop_path": str(crop_path),
                "area_bbox": list(bbox),
            }
        )
    return records


def make_bw(crop: Image.Image) -> Image.Image:
    rgb = np.asarray(crop.convert("RGB"))
    mask = clean_mask(red_mask(rgb))
    bw = np.full(mask.shape, 255, dtype=np.uint8)
    bw[mask] = 0
    return Image.fromarray(bw, mode="L")


def rotate_with_white(img: Image.Image, angle: float) -> Image.Image:
    return img.rotate(
        angle,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor="white",
    )


def angle_label(angle: float) -> str:
    if abs(angle) < 1e-9:
        return "0"
    prefix = "p" if angle > 0 else "m"
    text = f"{abs(angle):g}".replace(".", "p")
    return prefix + text


def parse_angles(value: str) -> list[float]:
    angles: list[float] = []
    for part in value.split(","):
        stripped = part.strip()
        if not stripped:
            continue
        angles.append(float(stripped))
    if 0.0 not in angles:
        angles.append(0.0)
    return sorted(dict.fromkeys(angles), key=lambda item: (abs(item), item))


def projection_score(mask: np.ndarray) -> float:
    if mask.sum() < 25:
        return 0.0
    row = mask.sum(axis=1).astype(np.float64)
    col = mask.sum(axis=0).astype(np.float64)

    def sharpness(values: np.ndarray) -> float:
        total = float(values.sum())
        if total <= 0:
            return 0.0
        normalized = values / max(float(values.max()), 1.0)
        return float(np.var(normalized) + (np.diff(normalized) ** 2).sum() / len(normalized))

    return max(sharpness(row), sharpness(col))


def score_angle_candidate(img: Image.Image) -> float:
    rgb = np.asarray(img.convert("RGB"))
    mask = clean_mask(red_mask(rgb))
    height, width = mask.shape
    margin_x = max(1, int(width * 0.16))
    margin_y = max(1, int(height * 0.16))
    inner = np.zeros_like(mask)
    if margin_x * 2 < width and margin_y * 2 < height:
        inner[margin_y : height - margin_y, margin_x : width - margin_x] = mask[
            margin_y : height - margin_y, margin_x : width - margin_x
        ]
    if inner.sum() < max(25, mask.sum() * 0.08):
        inner = mask
    return projection_score(inner)


def resize_for_output(img: Image.Image, scale: int, resampling: Image.Resampling) -> Image.Image:
    if scale <= 1:
        return img
    return img.resize((img.width * scale, img.height * scale), resampling)


def isolate_red_on_white(crop: Image.Image, clean: bool) -> Image.Image:
    rgb = np.asarray(crop.convert("RGB"))
    mask = red_mask(rgb)
    if clean:
        mask = clean_mask(mask)
    isolated = np.full(rgb.shape, 255, dtype=np.uint8)
    isolated[mask] = rgb[mask]
    return Image.fromarray(isolated, mode="RGB")


def sharpen_for_reading(crop: Image.Image) -> Image.Image:
    enhanced = ImageOps.autocontrast(crop.convert("RGB"), cutoff=1)
    enhanced = ImageEnhance.Color(enhanced).enhance(1.25)
    enhanced = ImageEnhance.Contrast(enhanced).enhance(1.2)
    return enhanced.filter(ImageFilter.UnsharpMask(radius=1.1, percent=180, threshold=2))


def tile_image(img: Image.Image, grid: int) -> Image.Image:
    grid = max(2, min(grid, 10))
    source = img.convert("RGB")
    tiled = Image.new("RGB", (source.width * grid, source.height * grid), "white")
    for y in range(grid):
        for x in range(grid):
            tiled.paste(source, (x * source.width, y * source.height))
    return tiled


def red_emphasis_array(crop: Image.Image) -> np.ndarray:
    rgb = np.asarray(crop.convert("RGB")).astype(np.int16)
    red_score = rgb[:, :, 0] - ((rgb[:, :, 1] + rgb[:, :, 2]) / 2)
    red_score = np.clip(red_score, 0, None)
    max_score = float(red_score.max())
    if max_score <= 0:
        return np.zeros(red_score.shape, dtype=np.uint8)
    return np.clip((red_score / max_score) * 255, 0, 255).astype(np.uint8)


def mask_to_bw(mask: np.ndarray) -> Image.Image:
    bw = np.full(mask.shape, 255, dtype=np.uint8)
    bw[mask.astype(bool)] = 0
    return Image.fromarray(bw, mode="L")


def adaptive_threshold_red(crop: Image.Image) -> Image.Image:
    emphasis = red_emphasis_array(crop)
    block = max(9, (min(emphasis.shape) // 5) | 1)
    try:
        import cv2

        foreground = cv2.adaptiveThreshold(
            emphasis,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            block,
            -3,
        )
        return Image.fromarray(255 - foreground, mode="L")
    except Exception:
        radius = max(3, block // 2)
        local_mean = np.asarray(
            Image.fromarray(emphasis, mode="L").filter(ImageFilter.BoxBlur(radius=radius))
        ).astype(np.int16)
        foreground = emphasis.astype(np.int16) > (local_mean + 5)
        return mask_to_bw(foreground)


def clahe_red_emphasis(crop: Image.Image) -> Image.Image:
    emphasis = red_emphasis_array(crop)
    try:
        import cv2

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(emphasis)
    except Exception:
        enhanced = np.asarray(ImageOps.equalize(Image.fromarray(emphasis, mode="L")))
    return Image.fromarray(255 - enhanced.astype(np.uint8), mode="L")


def morphology_mask(mask: np.ndarray, operation: str) -> np.ndarray:
    try:
        import cv2

        kernel = np.ones((3, 3), dtype=np.uint8)
        source = (mask.astype(np.uint8) * 255)
        if operation == "dilation":
            result = cv2.dilate(source, kernel, iterations=1)
        elif operation == "closing":
            result = cv2.morphologyEx(source, cv2.MORPH_CLOSE, kernel, iterations=1)
        elif operation == "erosion":
            result = cv2.erode(source, kernel, iterations=1)
        else:
            result = source
        return result > 0
    except Exception:
        pass

    try:
        from scipy import ndimage

        structure = np.ones((3, 3), dtype=bool)
        if operation == "dilation":
            return ndimage.binary_dilation(mask, structure=structure)
        if operation == "closing":
            return ndimage.binary_closing(mask, structure=structure)
        if operation == "erosion":
            return ndimage.binary_erosion(mask, structure=structure)
    except Exception:
        pass

    foreground = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    if operation == "dilation":
        result = foreground.filter(ImageFilter.MaxFilter(3))
    elif operation == "closing":
        result = foreground.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    elif operation == "erosion":
        result = foreground.filter(ImageFilter.MinFilter(3))
    else:
        result = foreground
    return np.asarray(result) > 0


def morphology_red(crop: Image.Image, operation: str) -> Image.Image:
    mask = clean_mask(red_mask(np.asarray(crop.convert("RGB"))))
    return mask_to_bw(morphology_mask(mask, operation))


def build_preprocessing_variants(
    crop: Image.Image,
    output_dir: Path,
    stem: str,
    scale: int,
    tile_grid: int,
) -> list[dict]:
    variant_dir = output_dir / "variants"
    variant_dir.mkdir(parents=True, exist_ok=True)
    scaled_size = (crop.width * max(1, scale), crop.height * max(1, scale))
    candidates = [
        {
            "name": "raw_lanczos",
            "description": "Raw crop upscaled smoothly; useful for visual reading when the original is small.",
            "image": crop.convert("RGB").resize(scaled_size, Image.Resampling.LANCZOS)
            if scale > 1
            else crop.convert("RGB"),
        },
        {
            "name": "raw_nearest",
            "description": "Raw crop upscaled without interpolation; preserves hard pixel edges.",
            "image": crop.convert("RGB").resize(scaled_size, Image.Resampling.NEAREST)
            if scale > 1
            else crop.convert("RGB"),
        },
        {
            "name": "contrast_sharpened",
            "description": "Autocontrast plus mild sharpening; can clarify faint red strokes but may amplify noise.",
            "image": resize_for_output(
                sharpen_for_reading(crop),
                max(1, scale),
                Image.Resampling.LANCZOS,
            ),
        },
        {
            "name": "red_isolated",
            "description": "Keeps red pixels on white background; useful when table lines or gray artifacts distract.",
            "image": resize_for_output(
                isolate_red_on_white(crop, clean=False),
                max(1, scale),
                Image.Resampling.NEAREST,
            ),
        },
        {
            "name": "red_clean_bw",
            "description": "Clean red mask as black-on-white; useful for stroke topology but destructive.",
            "image": resize_for_output(make_bw(crop), max(1, scale), Image.Resampling.NEAREST),
        },
        {
            "name": "adaptive_threshold",
            "description": "Local adaptive threshold on red-emphasis image; can help uneven background or lighting.",
            "image": resize_for_output(
                adaptive_threshold_red(crop),
                max(1, scale),
                Image.Resampling.NEAREST,
            ),
        },
        {
            "name": "clahe_red_emphasis",
            "description": "CLAHE/equalized red-emphasis view; can strengthen faint ink while limiting noise growth.",
            "image": resize_for_output(
                clahe_red_emphasis(crop),
                max(1, scale),
                Image.Resampling.NEAREST,
            ),
        },
        {
            "name": "morph_dilation",
            "description": "Red mask with dilation; can reconnect broken or faded strokes.",
            "image": resize_for_output(
                morphology_red(crop, "dilation"),
                max(1, scale),
                Image.Resampling.NEAREST,
            ),
        },
        {
            "name": "morph_closing",
            "description": "Red mask with closing; can fill small gaps and holes in stamp strokes.",
            "image": resize_for_output(
                morphology_red(crop, "closing"),
                max(1, scale),
                Image.Resampling.NEAREST,
            ),
        },
        {
            "name": "morph_erosion",
            "description": "Red mask with erosion; can reduce ink bleed or overly thick strokes.",
            "image": resize_for_output(
                morphology_red(crop, "erosion"),
                max(1, scale),
                Image.Resampling.NEAREST,
            ),
        },
        {
            "name": "raw_tile",
            "description": f"Raw crop repeated in a {tile_grid}x{tile_grid} grid; useful for tiny crops without resampling.",
            "image": tile_image(crop, tile_grid),
        },
    ]

    variants: list[dict] = []
    for candidate in candidates:
        path = variant_dir / f"{stem}_{candidate['name']}.png"
        candidate["image"].save(path)
        variants.append(
            {
                "name": candidate["name"],
                "description": candidate["description"],
                "path": str(path),
                "width": candidate["image"].width,
                "height": candidate["image"].height,
            }
        )
    return variants


def build_angle_candidates(
    crop: Image.Image,
    output_dir: Path,
    stem: str,
    angles: list[float],
    scale: int,
) -> tuple[list[dict], dict]:
    candidates: list[dict] = []
    candidate_dir = output_dir / "angle_candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    for angle in angles:
        rotated = rotate_with_white(crop, angle)
        rgb = np.asarray(rotated.convert("RGB"))
        mask = clean_mask(red_mask(rgb))
        pad = max(6, int(min(rotated.size) * 0.03))
        bbox = bbox_from_mask(mask, pad, rotated.width, rotated.height)
        corrected_crop = rotated.crop(bbox)
        corrected_bw = make_bw(corrected_crop)
        score = score_angle_candidate(corrected_crop)
        label = angle_label(angle)
        bw_path = candidate_dir / f"{stem}_angle_{label}_bw.png"
        resize_for_output(corrected_bw, scale, Image.Resampling.NEAREST).save(bw_path)
        candidates.append(
            {
                "angle_deg": angle,
                "score": score,
                "bw_path": str(bw_path),
            }
        )

    best = max(candidates, key=lambda item: (item["score"], -abs(item["angle_deg"])))
    best_angle = float(best["angle_deg"])
    rotated = rotate_with_white(crop, best_angle)
    rgb = np.asarray(rotated.convert("RGB"))
    mask = clean_mask(red_mask(rgb))
    pad = max(6, int(min(rotated.size) * 0.03))
    bbox = bbox_from_mask(mask, pad, rotated.width, rotated.height)
    corrected_crop = rotated.crop(bbox)
    corrected_bw = make_bw(corrected_crop)
    corrected_crop_path = output_dir / f"{stem}_angle_corrected_crop.png"
    corrected_bw_path = output_dir / f"{stem}_angle_corrected_bw.png"
    resize_for_output(corrected_crop, scale, Image.Resampling.LANCZOS).save(corrected_crop_path)
    resize_for_output(corrected_bw, scale, Image.Resampling.NEAREST).save(corrected_bw_path)

    best = {
        **best,
        "crop_path": str(corrected_crop_path),
        "bw_path": str(corrected_bw_path),
    }
    return candidates, best


def fit_image(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    fitted = img.copy()
    fitted.thumbnail((box_w, box_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (box_w, box_h), "white")
    x = (box_w - fitted.width) // 2
    y = (box_h - fitted.height) // 2
    canvas.paste(fitted.convert("RGB"), (x, y))
    return canvas


def load_font(size: int) -> ImageFont.ImageFont:
    for candidate in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def make_contact_sheet(items: list[dict], output_path: Path) -> None:
    thumb_w, thumb_h = 260, 220
    label_h = 34
    cols = 4
    rows = len(items)
    sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    font = load_font(14)
    headers = ["original", "crop", "bw red-mask", "angle-corrected bw"]
    for col, header in enumerate(headers):
        draw.text((col * thumb_w + 8, 4), header, fill="black", font=font)
    for row, item in enumerate(items):
        y0 = row * (thumb_h + label_h) + label_h
        label = f"{row + 1}: {Path(item['input']).name}"
        draw.text((8, y0 - 24), label, fill="black", font=font)
        for col, key in enumerate(
            ["original_path", "crop_path", "bw_path", "angle_corrected_bw_path"]
        ):
            img = Image.open(item[key])
            fitted = fit_image(img, thumb_w - 14, thumb_h - 10)
            sheet.paste(fitted, (col * thumb_w + 7, y0 + 5))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def make_angle_contact_sheet(items: list[dict], output_path: Path) -> None:
    if not items:
        return
    angles = [candidate["angle_deg"] for candidate in items[0]["angle_candidates"]]
    thumb_w, thumb_h = 170, 170
    label_h = 42
    row_label_w = 120
    sheet = Image.new(
        "RGB",
        (row_label_w + len(angles) * thumb_w, label_h + len(items) * thumb_h),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    font = load_font(13)
    for col, angle in enumerate(angles):
        draw.text((row_label_w + col * thumb_w + 8, 8), f"{angle:g} deg", fill="black", font=font)
    for row, item in enumerate(items):
        y0 = label_h + row * thumb_h
        draw.text((8, y0 + 8), f"{row + 1}: {Path(item['input']).name}", fill="black", font=font)
        draw.text(
            (8, y0 + 28),
            f"best {item['best_angle_deg']:g}",
            fill="black",
            font=font,
        )
        for col, candidate in enumerate(item["angle_candidates"]):
            img = Image.open(candidate["bw_path"])
            fitted = fit_image(img, thumb_w - 10, thumb_h - 10)
            sheet.paste(fitted, (row_label_w + col * thumb_w + 5, y0 + 5))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def make_variant_contact_sheet(items: list[dict], output_path: Path) -> None:
    items_with_variants = [item for item in items if item.get("preprocessing_variants")]
    if not items_with_variants:
        return
    variant_names = [
        variant["name"]
        for variant in items_with_variants[0]["preprocessing_variants"]
    ]
    thumb_w, thumb_h = 170, 170
    label_h = 48
    row_label_w = 120
    sheet = Image.new(
        "RGB",
        (row_label_w + len(variant_names) * thumb_w, label_h + len(items_with_variants) * thumb_h),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    font = load_font(13)
    for col, name in enumerate(variant_names):
        draw.text((row_label_w + col * thumb_w + 8, 8), name, fill="black", font=font)
    for row, item in enumerate(items_with_variants):
        y0 = label_h + row * thumb_h
        draw.text((8, y0 + 8), f"{row + 1}: {Path(item['input']).name}", fill="black", font=font)
        for col, variant in enumerate(item["preprocessing_variants"]):
            img = Image.open(variant["path"])
            fitted = fit_image(img, thumb_w - 10, thumb_h - 10)
            sheet.paste(fitted, (row_label_w + col * thumb_w + 5, y0 + 5))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def process_image(
    path: Path,
    output_dir: Path,
    index: int,
    scale: int,
    angles: list[float],
    variant_set: str,
    variant_tile_grid: int,
) -> dict:
    original = Image.open(path).convert("RGB")
    width, height = original.size
    rgb = np.asarray(original)
    mask = clean_mask(red_mask(rgb))
    pad = max(8, int(min(width, height) * 0.08))
    bbox = bbox_from_mask(mask, pad, width, height)
    crop = original.crop(bbox)
    bw = make_bw(crop)
    if scale > 1:
        bw = bw.resize((bw.width * scale, bw.height * scale), Image.Resampling.NEAREST)
        crop_scaled = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
    else:
        crop_scaled = crop

    stem = f"seal_{index:02d}_{path.stem}"
    original_copy = output_dir / f"{stem}_original.png"
    crop_path = output_dir / f"{stem}_crop.png"
    bw_path = output_dir / f"{stem}_bw.png"
    angle_candidates, best_angle = build_angle_candidates(
        crop,
        output_dir,
        stem,
        angles,
        scale,
    )
    preprocessing_variants = (
        build_preprocessing_variants(crop, output_dir, stem, max(1, scale), variant_tile_grid)
        if variant_set != "none"
        else []
    )
    original.save(original_copy)
    crop_scaled.save(crop_path)
    bw.save(bw_path)

    red_pixels = int(mask.sum())
    return {
        "input": str(path),
        "sha256": sha256_file(path),
        "width": width,
        "height": height,
        "red_pixels": red_pixels,
        "red_ratio": red_pixels / float(width * height),
        "bbox": list(bbox),
        "original_path": str(original_copy),
        "crop_path": str(crop_path),
        "bw_path": str(bw_path),
        "angle_candidates": angle_candidates,
        "best_angle_deg": best_angle["angle_deg"],
        "best_angle_score": best_angle["score"],
        "angle_corrected_crop_path": best_angle["crop_path"],
        "angle_corrected_bw_path": best_angle["bw_path"],
        "preprocessing_variants": preprocessing_variants,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Preprocess red Japanese seal images.")
    parser.add_argument("images", nargs="+", help="Input PNG/JPEG image paths.")
    parser.add_argument("--output-dir", default="/outputs/seal_processing")
    parser.add_argument("--scale", type=int, default=4)
    parser.add_argument(
        "--angles",
        default="-15,-10,-5,0,5,10,15",
        help="Comma-separated deskew/rotation candidate angles in degrees.",
    )
    parser.add_argument(
        "--split-stamps",
        action="store_true",
        help=(
            "Detect multiple red seal blobs in each input image, save per-stamp "
            "crops, and preprocess those crops individually."
        ),
    )
    parser.add_argument(
        "--split-min-area-ratio",
        type=float,
        default=0.015,
        help="Minimum connected-component area as a ratio of total red pixels for split-stamps.",
    )
    parser.add_argument(
        "--variant-set",
        choices=["none", "adaptive"],
        default="none",
        help=(
            "Write additional preprocessing variants for agent comparison. "
            "Use adaptive when image quality is small, blurred, faint, or uncertain."
        ),
    )
    parser.add_argument(
        "--variant-tile-grid",
        type=int,
        default=5,
        help="Grid size for raw_tile preprocessing variants.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    angles = parse_angles(args.angles)
    processing_inputs: list[dict] = []
    split_sources: list[dict] = []
    if args.split_stamps:
        for source_index, image_path in enumerate(args.images, start=1):
            source_path = Path(image_path)
            split_records = split_stamp_crops(
                source_path,
                output_dir,
                source_index,
                max(0.0, args.split_min_area_ratio),
            )
            if split_records:
                split_sources.append(
                    {
                        "source_input": str(source_path),
                        "source_index": source_index,
                        "split_count": len(split_records),
                        "split_stamps": split_records,
                    }
                )
                for record in split_records:
                    processing_inputs.append(
                        {
                            "path": Path(record["split_crop_path"]),
                            "split": record,
                        }
                    )
            else:
                processing_inputs.append({"path": source_path, "split": None})
                split_sources.append(
                    {
                        "source_input": str(source_path),
                        "source_index": source_index,
                        "split_count": 0,
                        "split_stamps": [],
                    }
                )
    else:
        processing_inputs = [
            {"path": Path(image_path), "split": None}
            for image_path in args.images
        ]

    items = []
    for index, item in enumerate(processing_inputs, start=1):
        processed = process_image(
            item["path"],
            output_dir,
            index,
            max(1, args.scale),
            angles,
            args.variant_set,
            args.variant_tile_grid,
        )
        if item["split"]:
            processed.update(item["split"])
        items.append(processed)
    contact_sheet = output_dir / "seal_contact_sheet.png"
    angle_contact_sheet = output_dir / "seal_angle_contact_sheet.png"
    variant_contact_sheet = output_dir / "seal_variant_contact_sheet.png"
    make_contact_sheet(items, contact_sheet)
    make_angle_contact_sheet(items, angle_contact_sheet)
    make_variant_contact_sheet(items, variant_contact_sheet)

    summary = {
        "images": items,
        "contact_sheet": str(contact_sheet),
        "angle_contact_sheet": str(angle_contact_sheet),
        "variant_contact_sheet": str(variant_contact_sheet) if variant_contact_sheet.exists() else "",
        "angles": angles,
        "count": len(items),
        "split_stamps_enabled": bool(args.split_stamps),
        "split_sources": split_sources,
        "variant_set": args.variant_set,
        "variant_tile_grid": max(2, min(args.variant_tile_grid, 10)),
    }
    summary_path = output_dir / "seal_processing_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
