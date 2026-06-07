#!/usr/bin/env python3
"""Build slideshow input images with portrait photos grouped into composite slides."""

from __future__ import annotations

import argparse
import csv
import os
import shutil
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from make_photo_slideshow import image_paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--portrait-group-size",
        type=int,
        default=2,
        choices=(2, 3),
        help="How many consecutive portrait photos to combine on one slide.",
    )
    parser.add_argument(
        "--portrait-threshold",
        type=float,
        default=1.08,
        help="Treat an image as portrait when height / width is at least this value.",
    )
    parser.add_argument("--clean", action="store_true", help="Remove output-dir before rebuilding.")
    parser.add_argument(
        "--group-shot-list",
        type=Path,
        help="Optional text file of group-shot filenames or stems to move to the end.",
    )
    return parser.parse_args()


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        return image.size


def is_portrait(path: Path, threshold: float) -> bool:
    width, height = image_size(path)
    return height / max(1, width) >= threshold


def rounded_photo(image: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, image.width, image.height), radius=radius, fill=255)
    rounded = Image.new("RGBA", image.size, (0, 0, 0, 0))
    rounded.alpha_composite(image.convert("RGBA"))
    rounded.putalpha(mask)
    return rounded


def fit_inside(image: Image.Image, box: tuple[int, int]) -> Image.Image:
    fitted = image.copy()
    fitted.thumbnail(box, Image.Resampling.LANCZOS)
    return fitted


def background_from_group(paths: list[Path], width: int, height: int) -> Image.Image:
    background = Image.new("RGB", (width, height), (15, 23, 20))
    pane_width = width / len(paths)
    for index, path in enumerate(paths):
        with Image.open(path) as opened:
            photo = ImageOps.exif_transpose(opened).convert("RGB")
        pane = ImageOps.fit(
            photo,
            (max(1, round(pane_width) + 80), height),
            method=Image.Resampling.BICUBIC,
        )
        pane = pane.filter(ImageFilter.GaussianBlur(max(22, round(min(width, height) * 0.024))))
        pane = Image.blend(pane, Image.new("RGB", pane.size, (14, 23, 19)), 0.43)
        x = round(index * pane_width) - 40
        background.paste(pane, (x, 0))

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(height):
        alpha = round(40 + 52 * (y / height))
        draw.line((0, y, width, y), fill=(8, 14, 10, alpha))
    return Image.alpha_composite(background.convert("RGBA"), overlay)


def draw_composite_slide(paths: list[Path], output: Path, width: int, height: int) -> None:
    canvas = background_from_group(paths, width, height)
    count = len(paths)
    outer_margin = round(width * (0.07 if count == 2 else 0.055))
    gap = round(width * (0.034 if count == 2 else 0.026))
    max_panel_w = (width - outer_margin * 2 - gap * (count - 1)) // count
    max_panel_h = round(height * 0.84)
    radius = max(16, round(min(width, height) * 0.018))
    border = max(6, round(min(width, height) * 0.008))

    total_width = max_panel_w * count + gap * (count - 1)
    start_x = (width - total_width) // 2
    center_y = height // 2

    for index, path in enumerate(paths):
        with Image.open(path) as opened:
            photo = ImageOps.exif_transpose(opened).convert("RGB")
        photo = fit_inside(photo, (max_panel_w, max_panel_h))
        framed = Image.new(
            "RGBA",
            (photo.width + border * 2, photo.height + border * 2),
            (238, 235, 219, 255),
        )
        framed.alpha_composite(photo.convert("RGBA"), (border, border))
        framed = rounded_photo(framed, radius)

        shadow_pad = round(min(width, height) * 0.035)
        shadow = Image.new(
            "RGBA",
            (framed.width + shadow_pad, framed.height + shadow_pad),
            (0, 0, 0, 0),
        )
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.rounded_rectangle(
            (
                shadow_pad // 2,
                shadow_pad // 2,
                shadow_pad // 2 + framed.width,
                shadow_pad // 2 + framed.height,
            ),
            radius=radius,
            fill=(0, 0, 0, 140),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(14, shadow_pad // 3)))

        panel_x = start_x + index * (max_panel_w + gap) + (max_panel_w - framed.width) // 2
        panel_y = center_y - framed.height // 2
        canvas.alpha_composite(shadow, (panel_x - shadow_pad // 2 + 16, panel_y - shadow_pad // 2 + 20))
        canvas.alpha_composite(framed, (panel_x, panel_y))

    canvas.convert("RGB").save(output, quality=94, subsampling=1)


def link_single_slide(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    destination.symlink_to(source.resolve())


def grouped_slides(
    items: list[tuple[Path, datetime, str]],
    portrait_threshold: float,
    portrait_group_size: int,
) -> list[list[tuple[Path, datetime, str]]]:
    slides: list[list[tuple[Path, datetime, str]]] = []
    index = 0
    while index < len(items):
        item = items[index]
        if not is_portrait(item[0], portrait_threshold):
            slides.append([item])
            index += 1
            continue

        run: list[tuple[Path, datetime, str]] = []
        while index < len(items) and is_portrait(items[index][0], portrait_threshold):
            run.append(items[index])
            index += 1

        run_index = 0
        while run_index < len(run):
            group = run[run_index : run_index + portrait_group_size]
            if len(group) == 1:
                slides.append(group)
            else:
                slides.append(group)
            run_index += len(group)

    return slides


def group_shot_names(path: Path | None) -> set[str]:
    if not path:
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        names.add(stripped)
        names.add(Path(stripped).name)
        names.add(Path(stripped).stem)
    return names


def is_group_shot(path: Path, names: set[str]) -> bool:
    return path.name in names or path.stem in names


def main() -> int:
    args = parse_args()
    if args.clean and args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    items = image_paths(args.image_dir)
    if not items:
        raise SystemExit(f"No images found in {args.image_dir}")

    group_names = group_shot_names(args.group_shot_list)
    regular_items = [item for item in items if not is_group_shot(item[0], group_names)]
    group_items = [item for item in items if is_group_shot(item[0], group_names)]
    sectioned_slides = [
        ("main", group)
        for group in grouped_slides(regular_items, args.portrait_threshold, args.portrait_group_size)
    ]
    sectioned_slides.extend(
        ("group_shots", group)
        for group in grouped_slides(group_items, args.portrait_threshold, args.portrait_group_size)
    )
    manifest = args.output_dir / "grouped-slides.csv"
    composite_count = 0

    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["order", "section", "kind", "timestamp", "slide_path", "source_paths"],
        )
        writer.writeheader()
        for order, (section, group) in enumerate(sectioned_slides, start=1):
            timestamp = group[0][1]
            if len(group) == 1:
                extension = group[0][0].suffix.lower()
                slide_path = args.output_dir / f"{order:04d}-single{extension}"
                link_single_slide(group[0][0], slide_path)
                kind = "single"
            else:
                slide_path = args.output_dir / f"{order:04d}-portrait-group.jpg"
                draw_composite_slide([item[0] for item in group], slide_path, args.width, args.height)
                os.utime(slide_path, (timestamp.timestamp(), timestamp.timestamp()))
                composite_count += 1
                kind = f"portrait-{len(group)}up"

            writer.writerow(
                {
                    "order": order,
                    "section": section,
                    "kind": kind,
                    "timestamp": timestamp.isoformat(sep=" "),
                    "slide_path": str(slide_path),
                    "source_paths": ";".join(str(item[0]) for item in group),
                }
            )

    print(
        f"wrote {len(sectioned_slides)} slideshow inputs to {args.output_dir} "
        f"({composite_count} portrait composites, {len(items)} source images, {len(group_items)} group-shot sources)"
    )
    print(f"manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
