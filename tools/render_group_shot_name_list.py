#!/usr/bin/env python3
"""Render a numbered group-shot photo with an ID-name list panel above it."""

from __future__ import annotations

import argparse
import csv
import io
import math
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


LATIN_REGULAR = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
LATIN_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
CJK_REGULAR = Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add a reusable ID-name list panel above a numbered group photo."
    )
    parser.add_argument(
        "--input",
        default="group-shot-numbered.jpg",
        help="Numbered group-shot image.",
    )
    parser.add_argument(
        "--output",
        default="group-shot-numbered-with-15-column-list.png",
        help="Output image path. Use .png for lossless output or .jpg for JPEG.",
    )
    parser.add_argument(
        "--csv",
        default="group-shot-names.csv",
        help="CSV file path or URL. Expected columns: ID, name, optional notes.",
    )
    parser.add_argument("--columns", type=int, default=15)
    parser.add_argument("--font-size", type=int, default=43)
    parser.add_argument("--line-height", type=int, default=56)
    parser.add_argument("--id-name-gap", type=int, default=122)
    parser.add_argument("--margin-x", type=int, default=42)
    parser.add_argument("--gutter", type=int, default=16)
    parser.add_argument("--photo-strip-height", type=int, default=240)
    parser.add_argument("--placeholder", default="神秘大侠")
    parser.add_argument("--png-compress-level", type=int, default=9)
    return parser.parse_args()


def read_csv_text(source: str) -> str:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=20) as response:
            return response.read().decode("utf-8-sig")
    return Path(source).read_text(encoding="utf-8-sig")


def load_entries(csv_source: str, placeholder: str) -> list[tuple[int, str]]:
    rows = list(csv.reader(io.StringIO(read_csv_text(csv_source))))
    entries: list[tuple[int, str]] = []
    for row in rows[1:]:
        if not row or not row[0].strip().isdigit():
            continue
        ident = int(row[0].strip())
        name = row[1].strip() if len(row) > 1 else ""
        entries.append((ident, name or placeholder))
    return sorted(entries, key=lambda item: item[0])


def is_cjk_char(ch: str) -> bool:
    return ord(ch) > 127


def text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    latin_font: ImageFont.FreeTypeFont,
    cjk_font: ImageFont.FreeTypeFont,
) -> float:
    width = 0.0
    for ch in text:
        font = cjk_font if is_cjk_char(ch) else latin_font
        width += draw.textlength(ch, font=font)
    return width


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    latin_font: ImageFont.FreeTypeFont,
    cjk_font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    if text_width(draw, text, latin_font, cjk_font) <= max_width:
        return text

    ellipsis = "..."
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if text_width(draw, candidate, latin_font, cjk_font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ellipsis


def draw_baseline_text(
    draw: ImageDraw.ImageDraw,
    x: float,
    baseline_y: float,
    text: str,
    latin_font: ImageFont.FreeTypeFont,
    cjk_font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
) -> None:
    cursor = x
    for ch in text:
        font = cjk_font if is_cjk_char(ch) else latin_font
        draw.text((cursor, baseline_y), ch, font=font, fill=fill, anchor="ls")
        cursor += draw.textlength(ch, font=font)


def add_photo_decoration(
    canvas: Image.Image,
    source_image: Image.Image,
    strip_height: int,
) -> None:
    width, height = source_image.size
    strip_src_h = max(360, min(760, height // 3))
    strip = source_image.crop((0, 0, width, strip_src_h)).resize(
        (width, strip_height),
        Image.Resampling.LANCZOS,
    )
    strip = strip.filter(ImageFilter.GaussianBlur(5))
    strip = ImageEnhance.Color(strip).enhance(1.18)
    strip = ImageEnhance.Contrast(strip).enhance(1.24)
    strip = ImageEnhance.Brightness(strip).enhance(0.72)
    canvas.paste(strip, (0, 0))

    draw = ImageDraw.Draw(canvas, "RGBA")
    for y in range(strip_height):
        alpha = int(36 + 80 * (y / max(1, strip_height - 1)))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha), width=1)
    draw.rectangle([0, 0, width, 20], fill=(36, 42, 49, 255))
    draw.rectangle([0, strip_height - 12, width, strip_height], fill=(218, 176, 69, 255))
    for x in range(-width, width * 2, 160):
        draw.line([(x, 0), (x + 500, strip_height)], fill=(255, 220, 105, 54), width=2)
    for x in range(-width, width * 2, 420):
        draw.line([(x, strip_height), (x + 420, 0)], fill=(255, 255, 255, 28), width=1)


def render(args: argparse.Namespace) -> Image.Image:
    entries = load_entries(args.csv, args.placeholder)
    source_image = Image.open(args.input).convert("RGB")
    width, height = source_image.size

    rows_per_col = math.ceil(len(entries) / args.columns)
    col_width = (
        width - args.margin_x * 2 - args.gutter * (args.columns - 1)
    ) / args.columns

    latin_font = ImageFont.truetype(str(LATIN_REGULAR), args.font_size)
    cjk_font = ImageFont.truetype(str(CJK_REGULAR), args.font_size)
    id_font = ImageFont.truetype(str(LATIN_BOLD), args.font_size)
    latin_ascent, _ = latin_font.getmetrics()

    list_top = args.photo_strip_height + 48
    panel_height = list_top + rows_per_col * args.line_height + 58
    canvas = Image.new("RGB", (width, height + panel_height), (13, 15, 18))

    add_photo_decoration(canvas, source_image, args.photo_strip_height)

    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, args.photo_strip_height, width, panel_height], fill=(14, 16, 19))
    draw.rectangle([0, panel_height - 7, width, panel_height], fill=(218, 176, 69))

    for col in range(1, args.columns):
        x = int(args.margin_x + col * col_width + (col - 0.5) * args.gutter)
        draw.line([(x, list_top - 20), (x, panel_height - 34)], fill=(49, 55, 63), width=1)

    for idx, (ident, name) in enumerate(entries):
        col = idx // rows_per_col
        row = idx % rows_per_col
        x = args.margin_x + col * (col_width + args.gutter)
        y = list_top + row * args.line_height
        baseline_y = y + latin_ascent
        name_color = (188, 196, 207) if name == args.placeholder else (239, 241, 244)

        draw.text(
            (x, baseline_y),
            f"{ident:03d}",
            font=id_font,
            fill=(255, 211, 82),
            anchor="ls",
        )

        name_x = x + args.id_name_gap
        max_name_width = int(col_width - args.id_name_gap)
        fitted_name = fit_text(draw, name, latin_font, cjk_font, max_name_width)
        draw_baseline_text(draw, name_x, baseline_y, fitted_name, latin_font, cjk_font, name_color)

    canvas.paste(source_image, (0, panel_height))
    return canvas


def save_image(image: Image.Image, output: str, png_compress_level: int) -> None:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        image.save(output_path, quality=95, subsampling=0, optimize=True)
    else:
        image.save(output_path, format="PNG", compress_level=png_compress_level, optimize=True)


def main() -> None:
    args = parse_args()
    image = render(args)
    save_image(image, args.output, args.png_compress_level)
    print(f"Wrote {args.output} ({image.size[0]}x{image.size[1]})")


if __name__ == "__main__":
    main()
