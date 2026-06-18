#!/usr/bin/env python3
"""Decorate an event photo with GPT Image, then restore original photo pixels.

This wraps the existing imagegen CLI workflow:
1. Create a mask that protects the human/photo region while allowing GPT to edit decorations.
2. Ask GPT Image to add event text and decorations.
3. Resize the GPT output to the original dimensions.
4. Feather-select the human/photo region from the original and overlay it on the GPT result.
"""

from __future__ import annotations

import argparse
import math
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass


DEFAULT_OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
DEFAULT_IMAGEGEN_CLI = Path.home() / ".codex" / "skills" / ".system" / "imagegen" / "scripts" / "image_gen.py"
DEFAULT_TMP_PYTHON = Path("/tmp/imagegen-cli-venv/bin/python")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decorate an event group photo with GPT Image and restore original people pixels."
    )
    parser.add_argument("--image", required=True, type=Path, help="Source event/group photo.")
    parser.add_argument(
        "--api-image",
        type=Path,
        help="Optional same-size image file to send to the Image API while using --image for masking/compositing.",
    )
    parser.add_argument(
        "--style-reference",
        action="append",
        default=[],
        type=Path,
        help="Additional image to provide to the Image API as a visual style reference; may be repeated.",
    )
    parser.add_argument("--output", required=True, type=Path, help="Final decorated image path.")
    parser.add_argument("--title", required=True, help="Main event title text.")
    parser.add_argument("--year", required=True, help="Year text.")
    parser.add_argument("--subtitle", required=True, help="Subtitle/team text.")
    parser.add_argument("--theme", default="warm event celebration", help="Theme or occasion for the decoration.")
    parser.add_argument(
        "--accent-instructions",
        default=(
            "Add tasteful celebratory accents around the edges only, using colors and textures "
            "that match the original photo."
        ),
        help="Decoration guidance for the GPT edit.",
    )
    parser.add_argument(
        "--skip-gpt-text",
        action="store_true",
        help="Ask GPT to create only decorations/background, leaving final text for a deterministic local overlay.",
    )
    parser.add_argument(
        "--free-decorate-unmasked",
        action="store_true",
        help="Make every unprotected pixel editable instead of limiting GPT to header, side, and bottom bands.",
    )
    parser.add_argument(
        "--allow-extra-gpt-elements",
        action="store_true",
        help="Allow GPT to add mascot-style artwork, logo-like decorations, and extra celebratory text.",
    )
    parser.add_argument("--build-dir", type=Path, help="Directory for mask, prompt, raw GPT output, and manifest.")
    parser.add_argument("--model", default="gpt-image-2", help="GPT Image model.")
    parser.add_argument("--size", default="3072x2048", help="Image API output size.")
    parser.add_argument("--quality", default="high", choices=["low", "medium", "high", "auto"])
    parser.add_argument("--imagegen-cli", type=Path, default=DEFAULT_IMAGEGEN_CLI)
    parser.add_argument(
        "--imagegen-python",
        default=os.environ.get("IMAGEGEN_PYTHON")
        or (str(DEFAULT_TMP_PYTHON) if DEFAULT_TMP_PYTHON.exists() else sys.executable),
        help="Python interpreter with the OpenAI SDK installed for image_gen.py.",
    )
    parser.add_argument("--openclaw-config", type=Path, default=DEFAULT_OPENCLAW_CONFIG)
    parser.add_argument("--reuse-gpt", type=Path, help="Skip API call and composite from this raw GPT output.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output/build artifacts.")
    parser.add_argument("--header-ratio", type=float, default=0.235, help="Editable top/header fraction.")
    parser.add_argument("--side-ratio", type=float, default=0.030, help="Editable side-border fraction.")
    parser.add_argument("--bottom-start-ratio", type=float, default=0.915, help="Editable bottom band start fraction.")
    parser.add_argument(
        "--organic-edit-mask",
        action="store_true",
        help="Use curved, non-rectangular editable header/edge bands instead of straight rectangular bands.",
    )
    parser.add_argument(
        "--mask-feather-px",
        type=int,
        default=20,
        help="Feather radius for organic edit mask boundaries.",
    )
    parser.add_argument("--paste-start-ratio", type=float, default=0.292, help="Original-photo paste-back start fraction.")
    parser.add_argument("--paste-end-ratio", type=float, default=0.902, help="Original-photo paste-back end fraction.")
    parser.add_argument(
        "--human-rect",
        action="append",
        default=[],
        metavar="X1,Y1,X2,Y2",
        help=(
            "Human/source-photo region to protect during GPT edit and overlay from the original afterward; "
            "may be repeated. Defaults to a full-width region from --paste-start-ratio to --paste-end-ratio."
        ),
    )
    parser.add_argument(
        "--auto-people-mask",
        action="store_true",
        help="Detect the main group of people from faces and protect a larger rounded people/body region.",
    )
    parser.add_argument(
        "--people-face-filter-ratio",
        type=float,
        default=0.45,
        help="Keep detected faces at least this fraction of the largest face height when estimating the main people group.",
    )
    parser.add_argument(
        "--people-expand-x-ratio",
        type=float,
        default=0.16,
        help="Horizontal expansion as a fraction of image width for the auto people mask.",
    )
    parser.add_argument(
        "--people-top-heads",
        type=float,
        default=1.25,
        help="How many median face heights to extend the people mask above the detected face group.",
    )
    parser.add_argument(
        "--people-bottom-heads",
        type=float,
        default=8.6,
        help="How many median face heights to extend the people mask below the detected face group.",
    )
    parser.add_argument(
        "--people-min-bottom-ratio",
        type=float,
        default=0.82,
        help="Minimum bottom edge for auto people mask as a fraction of image height.",
    )
    parser.add_argument(
        "--people-max-bottom-ratio",
        type=float,
        default=0.98,
        help="Maximum bottom edge for auto people mask as a fraction of image height.",
    )
    parser.add_argument(
        "--edit-protect-extra-ratio",
        type=float,
        default=0.0,
        help="Extra expansion for the GPT edit protection mask as a fraction of the shorter image edge.",
    )
    parser.add_argument(
        "--edit-protect-extra-px",
        type=int,
        default=0,
        help="Extra expansion in pixels for the GPT edit protection mask.",
    )
    parser.add_argument(
        "--edit-protect-feather-px",
        type=int,
        help="Feather radius for the GPT edit protection mask; defaults to --human-feather-px.",
    )
    parser.add_argument(
        "--human-feather-px",
        type=int,
        default=50,
        help="Pixel feather radius for the original human-region overlay.",
    )
    parser.add_argument("--feather-ratio", type=float, default=0.018, help="Legacy feather fraction used if --human-feather-px is 0.")
    parser.add_argument(
        "--no-auto-preserve-source-structures",
        dest="auto_preserve_source_structures",
        action="store_false",
        help="Disable automatic preservation of poles, flags, buildings, trees, and other real photo structures.",
    )
    parser.add_argument(
        "--auto-preserve-end-ratio",
        type=float,
        help="Bottom edge for automatic source-structure preservation; defaults to the paste-back feather boundary.",
    )
    parser.add_argument(
        "--protect-rect",
        action="append",
        default=[],
        metavar="X1,Y1,X2,Y2",
        help="Additional source-photo rectangle to keep unedited and restore exactly; may be repeated.",
    )
    parser.add_argument(
        "--protect-below-header",
        action="store_true",
        help="Restore the original source photo below --header-ratio after the GPT edit.",
    )
    parser.add_argument(
        "--local-title-overlay",
        action="store_true",
        help="Redraw the event title locally in the header after the GPT edit for crisp, uncropped text.",
    )
    parser.add_argument(
        "--protect-faces",
        action="store_true",
        help="Detect faces, protect them during the GPT edit, and composite original face pixels back afterward.",
    )
    parser.add_argument(
        "--face-detection-max-dim",
        type=int,
        default=1600,
        help="Maximum image dimension used for face detection before boxes are scaled back to the original.",
    )
    parser.add_argument(
        "--face-det-size",
        type=int,
        default=640,
        help="InsightFace detector input size.",
    )
    parser.add_argument(
        "--face-expand-ratio",
        type=float,
        default=0.24,
        help="Fraction of each detected face box used to expand the protected face ellipse.",
    )
    parser.add_argument(
        "--face-feather-px",
        type=int,
        default=18,
        help="Feather radius for detected face preservation.",
    )
    parser.add_argument(
        "--restore-non-sky-rect",
        action="append",
        default=[],
        metavar="X1,Y1,X2,Y2",
        help="Restore only source pixels that do not look like blue sky inside this rectangle; may be repeated.",
    )
    parser.set_defaults(auto_preserve_source_structures=True)
    return parser.parse_args()


def parse_rect(value: str) -> tuple[int, int, int, int]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"Expected X1,Y1,X2,Y2, got: {value}")
    try:
        x1, y1, x2, y2 = (int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Rectangle coordinates must be integers: {value}") from exc
    if x2 <= x1 or y2 <= y1:
        raise argparse.ArgumentTypeError(f"Rectangle must have positive width and height: {value}")
    return x1, y1, x2, y2


def resolved_protect_rects(args: argparse.Namespace, width: int, height: int) -> list[tuple[int, int, int, int]]:
    rects = resolved_rects(args.protect_rect, width, height)
    if args.protect_below_header:
        header_y = max(0, min(height, int(height * args.header_ratio)))
        if header_y < height:
            rects.append((0, header_y, width, height))
    return rects


def resolved_restore_non_sky_rects(args: argparse.Namespace, width: int, height: int) -> list[tuple[int, int, int, int]]:
    return resolved_rects(args.restore_non_sky_rect, width, height)


def resolved_human_rects(args: argparse.Namespace, width: int, height: int) -> list[tuple[int, int, int, int]]:
    rects = resolved_rects(args.human_rect, width, height)
    if rects:
        return rects
    if args.protect_faces or args.auto_people_mask:
        return []
    start = max(0, min(height, int(height * args.paste_start_ratio)))
    end = max(0, min(height, int(height * args.paste_end_ratio)))
    if end <= start:
        return []
    return [(0, start, width, end)]


def resolved_rects(values: list[str], width: int, height: int) -> list[tuple[int, int, int, int]]:
    rects: list[tuple[int, int, int, int]] = []
    for value in values:
        x1, y1, x2, y2 = parse_rect(value)
        x1 = max(0, min(width, x1))
        x2 = max(0, min(width, x2))
        y1 = max(0, min(height, y1))
        y2 = max(0, min(height, y2))
        if x2 > x1 and y2 > y1:
            rects.append((x1, y1, x2, y2))
    return rects


def build_feathered_rect_mask(
    size: tuple[int, int], rects: list[tuple[int, int, int, int]], feather_px: int
) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for rect in rects:
        draw.rectangle(rect, fill=255)
    if feather_px > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(feather_px))
    return mask


def detect_faces(image: Image.Image, args: argparse.Namespace) -> list[tuple[int, int, int, int]]:
    if not (args.protect_faces or args.auto_people_mask):
        return []
    try:
        import numpy as np
        from insightface.app import FaceAnalysis
    except ImportError as exc:
        raise SystemExit("--protect-faces requires numpy and insightface in the selected Python environment.") from exc

    rgb = image.convert("RGB")
    width, height = rgb.size
    scale = min(1.0, args.face_detection_max_dim / max(width, height))
    if scale < 1.0:
        detected_image = rgb.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)
    else:
        detected_image = rgb

    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(args.face_det_size, args.face_det_size))
    faces = app.get(np.array(detected_image)[:, :, ::-1])

    boxes: list[tuple[int, int, int, int]] = []
    for face in faces:
        x1, y1, x2, y2 = (float(value) / scale for value in face.bbox)
        face_w = x2 - x1
        face_h = y2 - y1
        expand_x = face_w * args.face_expand_ratio
        expand_y = face_h * args.face_expand_ratio
        bx1 = max(0, int(round(x1 - expand_x)))
        by1 = max(0, int(round(y1 - expand_y * 0.80)))
        bx2 = min(width, int(round(x2 + expand_x)))
        by2 = min(height, int(round(y2 + expand_y * 1.05)))
        if bx2 > bx1 and by2 > by1:
            boxes.append((bx1, by1, bx2, by2))
    return boxes


def build_people_mask(
    image: Image.Image,
    args: argparse.Namespace,
    *,
    extra_px: int = 0,
    feather_px: int | None = None,
) -> Image.Image:
    width, height = image.size
    mask = Image.new("L", (width, height), 0)
    if not args.auto_people_mask:
        return mask

    boxes = detect_faces(image, args)
    if not boxes:
        fallback = build_feathered_rect_mask(
            (width, height), [(0, int(height * args.paste_start_ratio), width, int(height * args.paste_end_ratio))], 0
        )
        return fallback.filter(ImageFilter.GaussianBlur(max(0, args.human_feather_px)))

    heights = [y2 - y1 for _, y1, _, y2 in boxes]
    max_height = max(heights)
    main_boxes = [box for box, box_height in zip(boxes, heights) if box_height >= max_height * args.people_face_filter_ratio]
    if not main_boxes:
        main_boxes = boxes

    sorted_heights = sorted(y2 - y1 for _, y1, _, y2 in main_boxes)
    median_height = sorted_heights[len(sorted_heights) // 2]
    min_x = min(x1 for x1, _, _, _ in main_boxes)
    max_x = max(x2 for _, _, x2, _ in main_boxes)
    min_y = min(y1 for _, y1, _, _ in main_boxes)
    max_y = max(y2 for _, _, _, y2 in main_boxes)

    expand_x = int(width * args.people_expand_x_ratio)
    x1 = max(0, min_x - expand_x - extra_px)
    x2 = min(width, max_x + expand_x + extra_px)
    y1 = max(0, int(min_y - median_height * args.people_top_heads) - extra_px)
    inferred_bottom = int(max_y + median_height * args.people_bottom_heads)
    min_bottom = int(height * args.people_min_bottom_ratio)
    max_bottom = int(height * args.people_max_bottom_ratio)
    y2 = max(inferred_bottom, min_bottom)
    y2 = min(height, min(y2 + extra_px, max_bottom + extra_px))
    if y2 <= y1:
        y2 = min(height, y1 + int(height * 0.45))

    draw = ImageDraw.Draw(mask)
    radius = max(40, int(min(x2 - x1, y2 - y1) * 0.12))
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=255)
    if feather_px is None:
        feather_px = args.human_feather_px
    if feather_px > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(feather_px))
    return mask


def build_face_mask(image: Image.Image, args: argparse.Namespace) -> Image.Image:
    width, height = image.size
    mask = Image.new("L", (width, height), 0)
    if not args.protect_faces:
        return mask
    draw = ImageDraw.Draw(mask)
    for box in detect_faces(image, args):
        draw.ellipse(box, fill=255)
    if args.face_feather_px > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(args.face_feather_px))
    return mask


def organic_wave_points(length: int, base: int, amplitude: int, phase: float, samples: int = 42) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for index in range(samples + 1):
        t = index / samples
        x = int(round(t * length))
        wave = (
            math.sin(t * math.tau * 1.15 + phase) * 0.58
            + math.sin(t * math.tau * 2.7 + phase * 0.47) * 0.30
            + math.sin(t * math.tau * 5.1 + phase * 1.31) * 0.12
        )
        y = int(round(base + amplitude * wave))
        points.append((x, y))
    return points


def organic_side_points(length: int, base: int, amplitude: int, phase: float, samples: int = 42) -> list[tuple[int, int]]:
    points: list[tuple[int, int]] = []
    for index in range(samples + 1):
        t = index / samples
        y = int(round(t * length))
        wave = (
            math.sin(t * math.tau * 1.4 + phase) * 0.55
            + math.sin(t * math.tau * 3.2 + phase * 0.63) * 0.32
            + math.sin(t * math.tau * 6.0 + phase * 1.17) * 0.13
        )
        x = int(round(base + amplitude * wave))
        points.append((x, y))
    return points


def apply_organic_edit_regions(alpha: Image.Image, args: argparse.Namespace, width: int, height: int) -> Image.Image:
    editable = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(editable)

    header_y = int(height * args.header_ratio)
    side_w = int(min(width, height) * args.side_ratio)
    bottom_y = int(height * args.bottom_start_ratio)
    top_amp = max(12, int(header_y * 0.16))
    side_amp = max(6, int(max(1, side_w) * 0.42))
    bottom_amp = max(10, int((height - bottom_y) * 0.32))

    top = [(0, 0), (width, 0)] + list(reversed(organic_wave_points(width, header_y, top_amp, 0.35)))
    draw.polygon(top, fill=255)

    if side_w > 0:
        left_curve = organic_side_points(height, side_w, side_amp, 1.1)
        draw.polygon([(0, 0), *left_curve, (0, height)], fill=255)
        right_curve = [(width - x, y) for x, y in organic_side_points(height, side_w, side_amp, 2.2)]
        draw.polygon([(width, 0), *right_curve, (width, height)], fill=255)

    if bottom_y < height:
        bottom_curve = organic_wave_points(width, bottom_y, bottom_amp, 1.9)
        draw.polygon([*bottom_curve, (width, height), (0, height)], fill=255)

    if args.mask_feather_px > 0:
        editable = editable.filter(ImageFilter.GaussianBlur(args.mask_feather_px))

    return Image.composite(Image.new("L", (width, height), 0), alpha, editable)


def build_source_structure_mask(region: Image.Image) -> Image.Image:
    rgb = region.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    non_sky_mask = Image.new("L", (width, height), 0)
    non_sky_pixels = non_sky_mask.load()
    non_sky_count = 0

    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            sky_blue = b > r + 30 and g > r + 10 and b > 120 and g > 80
            if not sky_blue:
                non_sky_pixels[x, y] = 255
                non_sky_count += 1

    non_sky_mask = non_sky_mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(0.7))

    edge_mask = ImageOps.grayscale(rgb).filter(ImageFilter.FIND_EDGES)
    edge_mask = edge_mask.point(lambda value: 255 if value > 18 else 0)
    edge_mask = edge_mask.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(1.0))

    # Blue-sky detection works well for typical outdoor event photos. If too much
    # of the region is classified as non-sky, fall back to edge preservation so
    # sunset/overcast skies do not erase the generated title.
    non_sky_ratio = non_sky_count / max(1, width * height)
    if non_sky_ratio > 0.45:
        return edge_mask
    return ImageChops.lighter(non_sky_mask, edge_mask)


def build_source_preserve_mask(image: Image.Image, args: argparse.Namespace) -> Image.Image:
    width, height = image.size
    preserve_mask = Image.new("L", (width, height), 0)
    rects = resolved_restore_non_sky_rects(args, width, height)

    if args.auto_preserve_source_structures:
        feather = max(1, int(height * args.feather_ratio))
        default_end = int(height * args.paste_start_ratio) + feather
        if args.auto_preserve_end_ratio is not None:
            default_end = int(height * args.auto_preserve_end_ratio)
        auto_end = max(int(height * args.header_ratio), default_end)
        auto_end = max(1, min(height, auto_end))
        rects.append((0, 0, width, auto_end))

    for x1, y1, x2, y2 in rects:
        region = image.crop((x1, y1, x2, y2))
        structure_mask = build_source_structure_mask(region)
        existing = preserve_mask.crop((x1, y1, x2, y2))
        preserve_mask.paste(ImageChops.lighter(existing, structure_mask), (x1, y1))

    return preserve_mask


def load_openai_api_key(openclaw_config: Path) -> str:
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]

    config_path = openclaw_config.expanduser()
    if not config_path.exists():
        raise SystemExit(
            "OPENAI_API_KEY is unset and OpenClaw config was not found. "
            "Set OPENAI_API_KEY or pass --openclaw-config."
        )

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse OpenClaw config: {config_path}") from exc

    provider = config.get("models", {}).get("providers", {}).get("openai", {})
    api_key_config = provider.get("apiKey")
    if isinstance(api_key_config, str) and api_key_config:
        return api_key_config
    if isinstance(api_key_config, dict) and api_key_config.get("source") == "env":
        env_name = str(api_key_config.get("id") or "OPENAI_API_KEY")
        value = os.environ.get(env_name)
        if value:
            return value
        raise SystemExit(
            f"OpenClaw points OpenAI apiKey to environment variable {env_name}, but it is unset."
        )

    raise SystemExit("Could not resolve an OpenAI API key from OPENAI_API_KEY or OpenClaw config.")


def write_mask(source: Path, out: Path, args: argparse.Namespace) -> tuple[int, int]:
    image = Image.open(source).convert("RGB")
    width, height = image.size

    human_mask = build_feathered_rect_mask(
        (width, height), resolved_human_rects(args, width, height), max(0, args.human_feather_px)
    )
    edit_extra_px = max(args.edit_protect_extra_px, int(min(width, height) * args.edit_protect_extra_ratio))
    edit_feather_px = args.edit_protect_feather_px if args.edit_protect_feather_px is not None else args.human_feather_px
    people_mask = build_people_mask(image, args, extra_px=edit_extra_px, feather_px=edit_feather_px)
    human_mask = ImageChops.lighter(human_mask, people_mask)
    face_mask = build_face_mask(image, args)
    preserve_mask = build_source_preserve_mask(image, args)

    if args.free_decorate_unmasked:
        mask = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        alpha = ImageChops.lighter(ImageChops.lighter(human_mask, face_mask), preserve_mask)
        alpha_draw = ImageDraw.Draw(alpha)
        for rect in resolved_protect_rects(args, width, height):
            alpha_draw.rectangle(rect, fill=255)
        mask.putalpha(alpha)
    else:
        mask = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(mask)

        if args.organic_edit_mask:
            mask.putalpha(apply_organic_edit_regions(mask.getchannel("A"), args, width, height))
        else:
            header_y = int(height * args.header_ratio)
            side_w = int(min(width, height) * args.side_ratio)
            bottom_y = int(height * args.bottom_start_ratio)

            draw.rectangle([0, 0, width, header_y], fill=(0, 0, 0, 0))
            draw.rectangle([0, 0, side_w, height], fill=(0, 0, 0, 0))
            draw.rectangle([width - side_w, 0, width, height], fill=(0, 0, 0, 0))
            draw.rectangle([0, bottom_y, width, height], fill=(0, 0, 0, 0))
        mask.putalpha(ImageChops.lighter(mask.getchannel("A"), ImageChops.lighter(human_mask, face_mask)))
        for rect in resolved_protect_rects(args, width, height):
            draw.rectangle(rect, fill=(0, 0, 0, 255))
        mask.putalpha(ImageChops.lighter(mask.getchannel("A"), preserve_mask))

    out.parent.mkdir(parents=True, exist_ok=True)
    mask.save(out)
    return width, height


def write_prompt(out: Path, args: argparse.Namespace) -> None:
    text_lines = [line for line in (args.title, args.year, args.subtitle) if line.strip()]
    exact_text = "\n".join(text_lines)
    if args.skip_gpt_text:
        text_request = (
            "The final text will be overlaid later by another tool. Use it only to reserve enough visual space, "
            "but do not render any letters, words, numbers, calligraphy, date text, signature text, "
            f"or placeholder text. Final text to reserve space for:\n{exact_text}\n"
            "Leave a clean, elegant blank banner/header area for the later text overlay."
        )
    else:
        line_layout = (
            "Render the text as one clean centered line."
            if len(text_lines) == 1
            else "Use balanced centered lines matching the line breaks above."
        )
        text_request = f"""Create the event typography as part of the generated design. Include this exact text, spelled exactly, with no missing or cropped characters:
{exact_text}

{line_layout} Use custom festive typography that feels integrated with the photo, not a plain local text overlay. Keep the text readable with generous safe margins. Every character must be fully inside the image canvas; no text may touch or be cut off by the top, left, right, or bottom edges. Make the text smaller if needed."""
    if args.free_decorate_unmasked and args.auto_people_mask:
        edit_scope = (
            "The mask protects the original people group. Keep every protected masked people/body region unchanged. "
            "Outside the protected people mask, create decorations and typography with broad creative freedom. "
            "Keep all text and important decorations outside the protected mask and outside its soft transition buffer, so nothing will be covered when the original people region is composited back. "
            "You may decorate the surrounding background, sky, floor, sides, and open spaces, but do not place new people or faces in the image. "
            "The final result will composite the original protected people region back over your edit, so make decoration flow naturally around that protected region."
        )
    elif args.free_decorate_unmasked:
        edit_scope = (
            "The mask protects original faces. Keep every protected masked face unchanged. "
            "Outside the protected mask, create decorations and typography as visual overlays integrated with the existing photo. "
            "Do not replace, remove, add, or redraw people. Preserve the original people, bodies, poses, clothing, hands, group arrangement, real objects, perspective, lighting, and campus/background scene. "
            "You may add festive decorations around and between existing elements, but the edited image must still clearly be the same original photo."
        )
    else:
        edit_scope = (
            "Only use the editable mask areas: open header/background space, very slim side borders, and a lower edge accent. "
            "Keep the central group photo, people, faces, bodies, hands, important props, and real background structures unchanged."
        )
    extra_elements = (
        "Mascot-style artwork, UCLA-themed decorative marks, graduation icons, ribbons, and extra short celebratory words are allowed if they improve the design. "
        "The main title text above must remain exact and most prominent."
        if args.allow_extra_gpt_elements
        else "Do not add extra logos or extra event text."
    )
    placement_constraints = (
        "Do not create new faces or people. Keep decorative elements outside or around the protected people group."
        if args.free_decorate_unmasked and args.auto_people_mask
        else (
            "Do not cover or modify faces. Do not create new faces or people. Keep decorative elements secondary to the real photographed subjects."
            if args.free_decorate_unmasked
            else "Do not place header elements over faces, people, lamps, signs, flags, poles, buildings, or other real photo structures."
        )
    )
    prompt = f"""Decorate this existing group photo for this theme: {args.theme}.
Do not move, reframe, crop, replace, or change the photographed people or scene.

{edit_scope}

Input images:
- Image 1 is the photo to edit.
- Any additional images are visual style references only. Do not copy their people or scene; borrow only the decoration style, color energy, typography feel, and layout ideas.

{text_request}

{placement_constraints} {args.accent_instructions} {extra_elements} Do not add a large ribbon or decoration over the people. Do not duplicate or misspell the main title. Do not transform the photo into a different group, different location, or different event. Do not generate black borders, dark borders, black backgrounds, dark vignette frames, oval crop frames, or poster-style solid color fills. No watermark.
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(prompt, encoding="utf-8")


def run_gpt_edit(args: argparse.Namespace, mask_path: Path, prompt_path: Path, raw_gpt_path: Path) -> None:
    if not args.imagegen_cli.exists():
        raise SystemExit(f"image_gen.py not found: {args.imagegen_cli}")
    if not Path(args.imagegen_python).exists() and shutil.which(args.imagegen_python) is None:
        raise SystemExit(f"Python interpreter not found: {args.imagegen_python}")
    api_image = args.api_image or args.image
    if not api_image.exists():
        raise SystemExit(f"API image not found: {api_image}")
    for reference in args.style_reference:
        if not reference.exists():
            raise SystemExit(f"Style reference not found: {reference}")

    env = os.environ.copy()
    env["OPENAI_API_KEY"] = load_openai_api_key(args.openclaw_config)
    cmd = [
        args.imagegen_python,
        str(args.imagegen_cli),
        "edit",
        "--model",
        args.model,
        "--image",
        str(api_image),
    ]
    for reference in args.style_reference:
        cmd.extend(["--image", str(reference)])
    cmd.extend(
        [
            "--mask",
            str(mask_path),
            "--prompt-file",
            str(prompt_path),
            "--size",
            args.size,
            "--quality",
            args.quality,
            "--background",
            "opaque",
            "--output-format",
            "png",
            "--no-augment",
            "--out",
            str(raw_gpt_path),
        ]
    )
    if args.force:
        cmd.append("--force")
    subprocess.run(cmd, check=True, env=env)


def fit_font(text: str, font_path: Path, max_size: int, min_size: int, max_width: int, max_height: int) -> ImageFont.FreeTypeFont:
    size = max(min_size, max_size)
    while size > min_size:
        font = ImageFont.truetype(str(font_path), size=size)
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_width and bbox[3] - bbox[1] <= max_height:
            return font
        size -= 2
    return ImageFont.truetype(str(font_path), size=min_size)


def text_size(text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    stroke_fill: tuple[int, int, int, int],
    stroke_width: int,
) -> None:
    draw.text(
        position,
        text,
        anchor="mm",
        font=font,
        fill=(0, 0, 0, 70),
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, 40),
    )
    draw.text(
        position,
        text,
        anchor="mm",
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )


def apply_local_title_overlay(image: Image.Image, args: argparse.Namespace) -> Image.Image:
    lines = [line.strip() for line in (args.title, args.year, args.subtitle) if line.strip()]
    if not lines:
        return image

    width, height = image.size
    header_h = max(1, int(height * args.header_ratio))
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf")
    if not font_path.exists():
        return image

    main_font = fit_font(lines[0], font_path, int(header_h * 0.42), 24, int(width * 0.28), int(header_h * 0.40))
    main_w, main_h = text_size(lines[0], main_font)

    subtitle_text = " ".join(lines[1:])
    subtitle_font = None
    subtitle_w = subtitle_h = 0
    if subtitle_text:
        subtitle_font = fit_font(
            subtitle_text,
            font_path,
            int(main_font.size * 0.30),
            18,
            int(width * 0.28),
            int(header_h * 0.16),
        )
        subtitle_w, subtitle_h = text_size(subtitle_text, subtitle_font)

    gap = int(header_h * 0.045) if subtitle_text else 0
    total_h = main_h + gap + subtitle_h
    top = int(header_h * 0.17)
    bottom_limit = int(header_h * 0.84)
    block_y1 = top
    block_y2 = min(bottom_limit, block_y1 + total_h)
    if block_y2 - block_y1 < total_h:
        block_y1 = max(int(header_h * 0.08), bottom_limit - total_h)
        block_y2 = block_y1 + total_h

    main_y = block_y1 + main_h // 2
    subtitle_y = main_y + main_h // 2 + gap + subtitle_h // 2 if subtitle_text else main_y

    margin_x = int(width * 0.045)
    margin_y = int(header_h * 0.12)
    content_w = max(main_w, subtitle_w)
    rect_w = min(int(width * 0.50), max(int(width * 0.40), content_w + margin_x * 2))
    rect_x1 = (width - rect_w) // 2
    rect_x2 = rect_x1 + rect_w
    rect_y1 = max(0, block_y1 - margin_y)
    rect_y2 = header_h - 1

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    radius = max(18, int(header_h * 0.18))
    overlay_draw.rounded_rectangle(
        (rect_x1, rect_y1, rect_x2, rect_y2),
        radius=radius,
        fill=(250, 247, 226, 255),
        outline=(255, 209, 0, 190),
        width=max(3, int(header_h * 0.010)),
    )

    draw = ImageDraw.Draw(overlay)
    blue = (0, 72, 132, 255)
    gold = (255, 209, 0, 255)
    dark_blue = (0, 52, 100, 255)
    draw_centered_text(draw, (width // 2, main_y), lines[0], main_font, blue, gold, max(2, int(main_font.size * 0.055)))
    if subtitle_text and subtitle_font:
        draw_centered_text(
            draw,
            (width // 2, subtitle_y),
            subtitle_text,
            subtitle_font,
            dark_blue,
            (255, 255, 255, 230),
            max(1, int(subtitle_font.size * 0.045)),
        )

    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def composite_original_people(args: argparse.Namespace, raw_gpt_path: Path) -> None:
    original = Image.open(args.image).convert("RGB")
    width, height = original.size
    gpt = Image.open(raw_gpt_path).convert("RGB").resize((width, height), Image.Resampling.LANCZOS)

    mask = Image.new("L", (width, height), 0)
    feather = max(0, args.human_feather_px)
    if feather == 0:
        feather = max(1, int(height * args.feather_ratio))
    human_mask = build_feathered_rect_mask((width, height), resolved_human_rects(args, width, height), feather)
    people_mask = build_people_mask(original, args)
    human_mask = ImageChops.lighter(human_mask, people_mask)
    face_mask = build_face_mask(original, args)
    mask = ImageChops.lighter(mask, ImageChops.lighter(human_mask, face_mask))
    draw = ImageDraw.Draw(mask)
    for rect in resolved_protect_rects(args, width, height):
        draw.rectangle(rect, fill=255)
    mask = ImageChops.lighter(mask, build_source_preserve_mask(original, args))

    final = Image.composite(original, gpt, mask)
    if args.local_title_overlay:
        final = apply_local_title_overlay(final, args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    suffix = args.output.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        final.save(args.output, quality=95, subsampling=0, optimize=True)
    else:
        final.save(args.output)


def write_manifest(args: argparse.Namespace, manifest_path: Path, raw_gpt_path: Path, mask_path: Path, prompt_path: Path) -> None:
    manifest: dict[str, Any] = {
        "source": str(args.image),
        "api_image": str(args.api_image) if args.api_image else None,
        "style_reference": [str(path) for path in args.style_reference],
        "output": str(args.output),
        "raw_gpt_output": str(raw_gpt_path),
        "mask": str(mask_path),
        "prompt": str(prompt_path),
        "model": args.model,
        "size": args.size,
        "quality": args.quality,
        "text": {
            "title": args.title,
            "year": args.year,
            "subtitle": args.subtitle,
        },
        "theme": args.theme,
        "accent_instructions": args.accent_instructions,
        "skip_gpt_text": args.skip_gpt_text,
        "free_decorate_unmasked": args.free_decorate_unmasked,
        "allow_extra_gpt_elements": args.allow_extra_gpt_elements,
        "ratios": {
            "header": args.header_ratio,
            "side": args.side_ratio,
            "bottom_start": args.bottom_start_ratio,
            "paste_start": args.paste_start_ratio,
            "paste_end": args.paste_end_ratio,
            "feather": args.feather_ratio,
        },
        "organic_edit_mask": args.organic_edit_mask,
        "mask_feather_px": args.mask_feather_px,
        "human_rects": args.human_rect,
        "auto_people_mask": args.auto_people_mask,
        "people_face_filter_ratio": args.people_face_filter_ratio,
        "people_expand_x_ratio": args.people_expand_x_ratio,
        "people_top_heads": args.people_top_heads,
        "people_bottom_heads": args.people_bottom_heads,
        "people_min_bottom_ratio": args.people_min_bottom_ratio,
        "people_max_bottom_ratio": args.people_max_bottom_ratio,
        "human_feather_px": args.human_feather_px,
        "edit_protect_extra_ratio": args.edit_protect_extra_ratio,
        "edit_protect_extra_px": args.edit_protect_extra_px,
        "edit_protect_feather_px": args.edit_protect_feather_px,
        "protect_rects": args.protect_rect,
        "protect_below_header": args.protect_below_header,
        "local_title_overlay": args.local_title_overlay,
        "protect_faces": args.protect_faces,
        "face_detection_max_dim": args.face_detection_max_dim,
        "face_det_size": args.face_det_size,
        "face_expand_ratio": args.face_expand_ratio,
        "face_feather_px": args.face_feather_px,
        "restore_non_sky_rects": args.restore_non_sky_rect,
        "auto_preserve_source_structures": args.auto_preserve_source_structures,
        "auto_preserve_end_ratio": args.auto_preserve_end_ratio,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    args.image = args.image.expanduser()
    if args.api_image:
        args.api_image = args.api_image.expanduser()
    args.style_reference = [path.expanduser() for path in args.style_reference]
    args.output = args.output.expanduser()
    if not args.image.exists():
        raise SystemExit(f"Source image not found: {args.image}")
    if args.output.exists() and not args.force:
        raise SystemExit(f"Output already exists, pass --force to overwrite: {args.output}")

    build_dir = args.build_dir or args.output.with_suffix("").parent / f"{args.output.stem}-build"
    build_dir = build_dir.expanduser()
    build_dir.mkdir(parents=True, exist_ok=True)

    mask_path = build_dir / "edit-mask.png"
    prompt_path = build_dir / "prompt.txt"
    raw_gpt_path = build_dir / "raw-gpt-decorated.png"
    manifest_path = build_dir / "manifest.json"

    write_mask(args.image, mask_path, args)
    write_prompt(prompt_path, args)

    if args.reuse_gpt:
        raw_source = args.reuse_gpt.expanduser()
        if not raw_source.exists():
            raise SystemExit(f"Raw GPT output not found: {raw_source}")
        if raw_source.resolve() != raw_gpt_path.resolve():
            shutil.copy2(raw_source, raw_gpt_path)
    else:
        run_gpt_edit(args, mask_path, prompt_path, raw_gpt_path)

    composite_original_people(args, raw_gpt_path)
    write_manifest(args, manifest_path, raw_gpt_path, mask_path, prompt_path)
    print(f"Wrote {args.output}")
    print(f"Build artifacts: {build_dir}")


if __name__ == "__main__":
    main()
