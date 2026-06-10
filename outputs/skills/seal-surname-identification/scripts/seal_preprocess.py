from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


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


def make_bw(crop: Image.Image) -> Image.Image:
    rgb = np.asarray(crop.convert("RGB"))
    mask = clean_mask(red_mask(rgb))
    bw = np.full(mask.shape, 255, dtype=np.uint8)
    bw[mask] = 0
    return Image.fromarray(bw, mode="L")


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
    cols = 3
    rows = len(items)
    sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    font = load_font(14)
    headers = ["original", "crop", "bw red-mask"]
    for col, header in enumerate(headers):
        draw.text((col * thumb_w + 8, 4), header, fill="black", font=font)
    for row, item in enumerate(items):
        y0 = row * (thumb_h + label_h) + label_h
        label = f"{row + 1}: {Path(item['input']).name}"
        draw.text((8, y0 - 24), label, fill="black", font=font)
        for col, key in enumerate(["original_path", "crop_path", "bw_path"]):
            img = Image.open(item[key])
            fitted = fit_image(img, thumb_w - 14, thumb_h - 10)
            sheet.paste(fitted, (col * thumb_w + 7, y0 + 5))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def process_image(path: Path, output_dir: Path, index: int, scale: int) -> dict:
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
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Preprocess red Japanese seal images.")
    parser.add_argument("images", nargs="+", help="Input PNG/JPEG image paths.")
    parser.add_argument("--output-dir", default="/outputs/seal_processing")
    parser.add_argument("--scale", type=int, default=4)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    items = [
        process_image(Path(image_path), output_dir, index, max(1, args.scale))
        for index, image_path in enumerate(args.images, start=1)
    ]
    contact_sheet = output_dir / "seal_contact_sheet.png"
    make_contact_sheet(items, contact_sheet)

    summary = {
        "images": items,
        "contact_sheet": str(contact_sheet),
        "count": len(items),
    }
    summary_path = output_dir / "seal_processing_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
