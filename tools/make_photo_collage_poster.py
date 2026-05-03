#!/usr/bin/env python3
"""Create a printable photo collage poster from a timestamp-sorted image folder."""

from __future__ import annotations

import argparse
import csv
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
}

DATE_TAGS = (
    (36867, "DateTimeOriginal"),
    (36868, "DateTimeDigitized"),
    (306, "DateTime"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pdf-output", type=Path)
    parser.add_argument("--title", default="Earth Day Volunteering 2026")
    parser.add_argument("--subtitle", default="A community tree-planting photo story")
    parser.add_argument("--album-url", default="")
    parser.add_argument("--sample-count", type=int, default=25)
    parser.add_argument("--width", type=int, default=3300)
    parser.add_argument("--height", type=int, default=5100)
    parser.add_argument("--order-report", type=Path)
    return parser.parse_args()


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(text[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def image_timestamp(path: Path) -> tuple[datetime, str]:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if exif:
                for tag, label in DATE_TAGS:
                    parsed = parse_datetime(exif.get(tag))
                    if parsed:
                        return parsed, label
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime), "mtime"


def image_paths(image_dir: Path) -> list[tuple[Path, datetime, str]]:
    items = []
    for path in image_dir.iterdir():
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            timestamp, source = image_timestamp(path)
            items.append((path, timestamp, source))
    items.sort(key=lambda item: (item[1], item[0].name.lower()))
    return items


def sample_evenly(
    items: list[tuple[Path, datetime, str]],
    count: int,
) -> list[tuple[Path, datetime, str]]:
    if count <= 0 or count >= len(items):
        return items
    if count == 1:
        return [items[len(items) // 2]]
    indexes = sorted({round(index * (len(items) - 1) / (count - 1)) for index in range(count)})
    while len(indexes) < count:
        largest_gap_position = max(
            range(len(indexes) - 1),
            key=lambda position: indexes[position + 1] - indexes[position],
        )
        midpoint = (indexes[largest_gap_position] + indexes[largest_gap_position + 1]) // 2
        indexes.append(midpoint)
        indexes = sorted(set(indexes))
    return [items[index] for index in indexes[:count]]


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    max_width: int,
    start_size: int,
    min_size: int,
) -> ImageFont.FreeTypeFont:
    for size in range(start_size, min_size - 1, -4):
        candidate = font(font_path, size)
        bbox = draw.textbbox((0, 0), text, font=candidate)
        if bbox[2] - bbox[0] <= max_width:
            return candidate
    return font(font_path, min_size)


def draw_centered(
    draw: ImageDraw.ImageDraw,
    y: int,
    text: str,
    selected_font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    canvas_width: int,
) -> int:
    bbox = draw.textbbox((0, 0), text, font=selected_font)
    text_width = bbox[2] - bbox[0]
    draw.text(((canvas_width - text_width) // 2, y - bbox[1]), text, font=selected_font, fill=fill)
    return y + selected_font.size


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    selected_font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        word_bbox = draw.textbbox((0, 0), word, font=selected_font)
        if word_bbox[2] - word_bbox[0] > max_width:
            if current:
                lines.append(current)
                current = ""
            approximate_chars = max(12, int(len(word) * max_width / max(1, word_bbox[2] - word_bbox[0])))
            lines.extend(textwrap.wrap(word, width=approximate_chars, break_long_words=True))
            continue
        trial = word if not current else f"{current} {word}"
        bbox = draw.textbbox((0, 0), trial, font=selected_font)
        if bbox[2] - bbox[0] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def paste_fit(canvas: Image.Image, path: Path, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    with Image.open(path) as opened:
        fitted = ImageOps.fit(
            ImageOps.exif_transpose(opened).convert("RGB"),
            (x1 - x0, y1 - y0),
            Image.Resampling.LANCZOS,
        )
    canvas.alpha_composite(fitted.convert("RGBA"), (x0, y0))


def paste_contain(canvas: Image.Image, path: Path, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    width = x1 - x0
    height = y1 - y0
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    background = ImageOps.fit(image, (width, height), Image.Resampling.BICUBIC)
    background = background.filter(ImageFilter.GaussianBlur(32))
    background = Image.blend(background, Image.new("RGB", (width, height), (19, 45, 34)), 0.28)
    foreground = image.copy()
    foreground.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas.alpha_composite(background.convert("RGBA"), (x0, y0))
    fx = x0 + (width - foreground.width) // 2
    fy = y0 + (height - foreground.height) // 2
    canvas.alpha_composite(foreground.convert("RGBA"), (fx, fy))


def draw_order_report(items: list[tuple[Path, datetime, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["order", "timestamp", "timestamp_source", "path"])
        writer.writeheader()
        for index, (image_path, timestamp, source) in enumerate(items, start=1):
            writer.writerow(
                {
                    "order": index,
                    "timestamp": timestamp.isoformat(sep=" "),
                    "timestamp_source": source,
                    "path": str(image_path),
                }
            )


def main() -> int:
    args = parse_args()
    output_path = args.output.resolve()
    source_items = [item for item in image_paths(args.image_dir) if item[0].resolve() != output_path]
    if not source_items:
        raise SystemExit(f"No images found in {args.image_dir}")
    selected = sample_evenly(source_items, args.sample_count)
    hero_index = 0
    hero = selected[hero_index]
    tiles = selected[:hero_index] + selected[hero_index + 1 :]

    width = args.width
    height = args.height
    margin = round(width * 0.055)
    gap = round(width * 0.018)
    header_h = round(height * 0.155)
    footer_h = round(height * 0.105)
    hero_h = round(height * 0.235)
    grid_top = header_h + hero_h + gap * 2
    grid_bottom = height - footer_h - gap
    grid_h = grid_bottom - grid_top

    canvas = Image.new("RGBA", (width, height), (239, 244, 235, 255))
    draw = ImageDraw.Draw(canvas)

    title_font_path = "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf"
    bold_font_path = "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"
    regular_font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    title_font = fit_text(draw, args.title.upper(), title_font_path, width - 2 * margin, 152, 88)
    subtitle_font = fit_text(draw, args.subtitle, regular_font_path, width - 2 * margin, 68, 44)
    small_font = font(regular_font_path, 34)
    footer_font = font(regular_font_path, 31)
    count_font = font(bold_font_path, 42)

    draw.rectangle((0, 0, width, header_h), fill=(25, 63, 48))
    draw.rectangle((0, header_h - 14, width, header_h), fill=(211, 178, 83))
    y = round(header_h * 0.20)
    y = draw_centered(draw, y, args.title.upper(), title_font, (247, 245, 224), width)
    draw_centered(draw, y + 52, args.subtitle, subtitle_font, (224, 232, 213), width)

    hero_box = (margin, header_h + gap, width - margin, header_h + gap + hero_h)
    paste_contain(canvas, hero[0], hero_box)
    draw.rounded_rectangle(hero_box, radius=0, outline=(247, 245, 224), width=12)
    draw.rectangle(
        (hero_box[0], hero_box[3] - 92, hero_box[2], hero_box[3]),
        fill=(0, 0, 0, 118),
    )
    hero_caption = hero[1].strftime("%B %-d, %Y") if hasattr(hero[1], "strftime") else "Earth Day 2026"
    draw.text((hero_box[0] + 36, hero_box[3] - 66), hero_caption, font=count_font, fill=(255, 255, 242))

    columns = 4
    rows = 6
    tile_w = (width - 2 * margin - (columns - 1) * gap) // columns
    tile_h = (grid_h - (rows - 1) * gap) // rows
    for index, item in enumerate(tiles[: columns * rows]):
        row = index // columns
        col = index % columns
        x = margin + col * (tile_w + gap)
        y = grid_top + row * (tile_h + gap)
        paste_fit(canvas, item[0], (x, y, x + tile_w, y + tile_h))
        draw.rectangle((x, y, x + tile_w, y + tile_h), outline=(255, 255, 245), width=7)

    footer_top = height - footer_h
    draw.rectangle((0, footer_top, width, height), fill=(25, 63, 48))
    stats = f"{len(source_items)} photos • sampled across the day • Google Photos album"
    draw.text((margin, footer_top + 46), stats, font=count_font, fill=(247, 245, 224))
    if args.album_url:
        label = "Album:"
        draw.text((margin, footer_top + 116), label, font=footer_font, fill=(211, 178, 83))
        link_x = margin + draw.textbbox((0, 0), label + "  ", font=footer_font)[2]
        wrapped = wrap_text(draw, args.album_url, footer_font, width - link_x - margin)
        for line_index, line in enumerate(wrapped[:2]):
            draw.text(
                (link_x, footer_top + 116 + line_index * 44),
                line,
                font=footer_font,
                fill=(224, 232, 213),
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    rgb = canvas.convert("RGB")
    rgb.save(args.output, quality=94, dpi=(300, 300))
    if args.pdf_output:
        args.pdf_output.parent.mkdir(parents=True, exist_ok=True)
        rgb.save(args.pdf_output, "PDF", resolution=300.0)
    if args.order_report:
        draw_order_report(selected, args.order_report)
    print(f"wrote {args.output}")
    if args.pdf_output:
        print(f"wrote {args.pdf_output}")
    print(f"selected {len(selected)} of {len(source_items)} images")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
