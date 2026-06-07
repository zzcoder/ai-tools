#!/usr/bin/env python3
"""Generate a GPT Image collage template, then paste original photos into it.

The generator asks GPT Image for a high-resolution collage scene with
machine-readable photo windows. By default each photo window is filled with a
reserved chroma-key marker color. OpenCV detects that marker even when windows
are tilted, then the source photos are perspective-warped into place so the
final poster contains the exact original photo pixels.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
from PIL import Image, ImageOps


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

DEFAULT_OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
MARKER_COLORS = {
    "magenta": {
        "name": "neon magenta",
        "hex": "#ff00ff",
        "rgb": (255, 0, 255),
        "hsv_lower": (135, 70, 120),
        "hsv_upper": (170, 255, 255),
    },
    "green": {
        "name": "chroma green",
        "hex": "#00ff00",
        "rgb": (0, 255, 0),
        "hsv_lower": (45, 70, 120),
        "hsv_upper": (85, 255, 255),
    },
    "cyan": {
        "name": "neon cyan",
        "hex": "#00ffff",
        "rgb": (0, 255, 255),
        "hsv_lower": (82, 70, 120),
        "hsv_upper": (105, 255, 255),
    },
}


@dataclass
class PhotoSlot:
    quad: np.ndarray
    area: float
    score: float


@dataclass
class PhotoPlacement:
    photo: Path
    slot: PhotoSlot
    match_cost: float | None = None
    match_rank: int | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python3 tools/make_gpt_image_collage.py \\
                --image-dir /path/to/photos \\
                --title "Lake Anna 2026" \\
                --output /path/to/lake-anna-collage.png

              python3 tools/make_gpt_image_collage.py \\
                --photos a.jpg b.jpg c.jpg \\
                --title "Weekend Trip" \\
                --output collage.png
            """
        ),
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--image-dir", type=Path, help="Folder of photos to place in the collage.")
    input_group.add_argument("--photos", nargs="+", type=Path, help="Explicit list of photo paths.")
    input_group.add_argument("--photo-list", type=Path, help="Newline-delimited file of photo paths.")
    parser.add_argument("--recursive", action="store_true", help="Search --image-dir recursively.")
    parser.add_argument("--title", required=True, help="Title text to render in the generated collage.")
    parser.add_argument(
        "--theme",
        help="Optional visual theme for the generated poster background and decorative style.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Final collage image path.")
    parser.add_argument(
        "--build-dir",
        type=Path,
        help="Directory for the generated template, prompt, manifest, and debug images.",
    )
    parser.add_argument("--model", default="gpt-image-2", help="OpenAI image generation model.")
    parser.add_argument(
        "--content-aware",
        action="store_true",
        help="Send selected photos as image references so GPT Image can lay out frames based on their content.",
    )
    parser.add_argument(
        "--slot-assignment",
        choices=["auto", "order", "hash", "dedup"],
        default="auto",
        help="How to map photos to detected slots. Auto uses dedup-style matching for --content-aware and order otherwise.",
    )
    parser.add_argument(
        "--reference-max-side",
        type=int,
        default=1600,
        help="Maximum long edge for temporary image references sent to GPT Image in --content-aware mode.",
    )
    parser.add_argument(
        "--reference-jpeg-quality",
        type=int,
        default=88,
        help="JPEG quality for temporary image references sent to GPT Image in --content-aware mode.",
    )
    parser.add_argument(
        "--api-size",
        default="auto",
        help="Size sent to the Images API. Use 'auto' unless your account supports explicit 4K sizes.",
    )
    parser.add_argument("--quality", default="high", choices=["low", "medium", "high", "auto"])
    parser.add_argument(
        "--output-size",
        default="3840x2160",
        help="Final output canvas size. Defaults to 4K UHD landscape.",
    )
    parser.add_argument("--max-photos", type=int, default=10, help="Maximum photos to place.")
    parser.add_argument(
        "--slot-inset",
        type=float,
        default=-0.035,
        help="Fraction to shrink each detected quadrilateral before pasting photos; negative expands slightly.",
    )
    parser.add_argument(
        "--min-slot-area",
        type=float,
        default=0.001,
        help="Minimum detected slot area as a fraction of the output image area.",
    )
    parser.add_argument(
        "--reuse-template",
        type=Path,
        help="Skip the API call and detect slots from this existing generated template.",
    )
    parser.add_argument(
        "--detection-mode",
        choices=["marker", "blank", "auto"],
        default="marker",
        help="Slot detector. 'marker' uses chroma-key windows; 'blank' is legacy pale-rectangle detection.",
    )
    parser.add_argument(
        "--slot-marker",
        choices=sorted(MARKER_COLORS),
        default="magenta",
        help="Reserved marker color used for photo window interiors in marker mode.",
    )
    parser.add_argument(
        "--no-marker-cleanup",
        action="store_true",
        help="Keep any remaining marker-colored pixels after photo placement.",
    )
    parser.add_argument(
        "--debug-slots",
        action="store_true",
        help="Write a debug image showing detected quadrilaterals.",
    )
    parser.add_argument(
        "--openclaw-config",
        type=Path,
        default=DEFAULT_OPENCLAW_CONFIG,
        help="OpenClaw config used to resolve the OpenAI API key when OPENAI_API_KEY is unset.",
    )
    return parser.parse_args()


def parse_size(size: str) -> tuple[int, int]:
    try:
        width_text, height_text = size.lower().split("x", 1)
        width = int(width_text)
        height = int(height_text)
    except ValueError as exc:
        raise SystemExit(f"Invalid size {size!r}; expected WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0:
        raise SystemExit(f"Invalid size {size!r}; dimensions must be positive")
    return width, height


def collect_photos(args: argparse.Namespace) -> list[Path]:
    if args.image_dir:
        base = args.image_dir.expanduser()
        if not base.exists():
            raise SystemExit(f"Image directory does not exist: {base}")
        iterator = base.rglob("*") if args.recursive else base.iterdir()
        paths = [path for path in iterator if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
    elif args.photos:
        paths = [path.expanduser() for path in args.photos]
    else:
        paths = []
        for line in args.photo_list.expanduser().read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                paths.append(Path(stripped).expanduser())

    clean_paths = []
    seen = set()
    for path in sorted(paths, key=lambda item: str(item).lower()):
        resolved = path.resolve()
        if resolved in seen:
            continue
        if not resolved.exists():
            raise SystemExit(f"Photo does not exist: {path}")
        if resolved.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        clean_paths.append(resolved)
        seen.add(resolved)

    if args.max_photos > 0:
        clean_paths = clean_paths[: args.max_photos]
    if not clean_paths:
        raise SystemExit("No photos found")
    return clean_paths


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

    raise SystemExit(
        "Could not resolve an OpenAI API key from OPENAI_API_KEY or OpenClaw's OpenAI provider config."
    )


def photo_orientations(paths: list[Path]) -> list[str]:
    orientations = []
    for path in paths:
        with Image.open(path) as image:
            width, height = ImageOps.exif_transpose(image).size
        if width > height * 1.12:
            orientations.append("landscape")
        elif height > width * 1.12:
            orientations.append("portrait")
        else:
            orientations.append("square")
    return orientations


def build_prompt(
    title: str,
    photos: list[Path],
    output_size: tuple[int, int],
    detection_mode: str,
    marker_key: str,
    theme: str | None = None,
    content_aware: bool = False,
) -> str:
    width, height = output_size
    orientations = ", ".join(f"{index + 1}: {orientation}" for index, orientation in enumerate(photo_orientations(photos)))
    count = len(photos)
    theme_line = theme or "warm natural light, clean photo scrapbook poster, tasteful event keepsake"
    generation_kind = (
        "Create a polished content-aware 4K landscape travel photo collage poster using the provided reference photos."
        if content_aware
        else "Create a polished 4K landscape travel photo collage poster."
    )
    base = textwrap.dedent(
        f"""\
        {generation_kind}
        Final poster target size: {width}x{height}.
        Theme: {theme_line}.
        Title text must read exactly: {title}

        Include exactly {count} photo windows for later photo insertion.
        The photo windows may be casually tilted or in perspective like printed photos on a table.
        Leave enough spacing between windows so computer vision can detect each rectangle.
        Match this reading-order orientation plan where practical: {orientations}.
        Keep the title outside the photo windows.
        """
    ).strip()
    if content_aware:
        base += (
            "\n"
            + textwrap.dedent(
                """\

                Use every provided reference photo exactly once as a recognizable low-resolution preview
                inside a photo window, choosing frame sizes and positions based on each photo's subject,
                orientation, and visual importance. Do not invent additional photo content and do not omit
                any provided reference photo. The previews are temporary; the original files will be pasted
                back into the detected windows later.
                Arrange the photo windows as separated, non-overlapping frames with clear empty space between
                every pair of frames. A 3-row layout with 3 windows on top, 3 in the middle, and 4 on the
                bottom is preferred for 10 landscape photos. Do not crop any photo window off the canvas.
                """
            ).strip()
        )
    if detection_mode in {"marker", "auto"}:
        marker = MARKER_COLORS[marker_key]
        marker_instruction = (
            f"Outline every photo window with one continuous solid {marker['name']} keyline "
            f"({marker['hex']}), about 16-24 pixels thick, just inside or along the frame edge. "
            f"Keep each reference photo preview visible inside that outline."
            if content_aware
            else f"Fill the full interior of every photo window with one perfectly flat solid {marker['name']} "
            f"chroma-key color ({marker['hex']})."
        )
        return (
            base
            + "\n\n"
            + textwrap.dedent(
                f"""\
                Critical machine-vision requirement:
                {marker_instruction}
                Every {marker['name']} outline must be a separate closed quadrilateral. Magenta outlines must
                not touch, overlap, share edges, or be covered by flowers, leaves, labels, tape, shadows, or
                other decorations.
                Do not use {marker['name']} or {marker['hex']} anywhere else in the poster, including the
                title, decorations, sky, lake, trees, clothing, flowers, or border details.
                """
            ).strip()
        )
    return (
        base
        + "\n\n"
        + textwrap.dedent(
            """\
            Fill every photo window with flat pale gray. Do not place generated people, scenery, icons,
            patterns, text, numbers, watermarks, or decorative objects inside the pale gray interiors.
            """
        ).strip()
    )


def call_image_generation(
    api_key: str,
    model: str,
    prompt: str,
    api_size: str,
    quality: str,
    output_path: Path,
) -> None:
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": api_size,
        "quality": quality,
        "background": "opaque",
        "output_format": "png",
    }
    response = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=300,
    )
    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": {"message": response.text[:1000]}}
        message = payload.get("error", {}).get("message") or payload
        raise SystemExit(f"Image generation failed with HTTP {response.status_code}: {message}")

    payload = response.json()
    try:
        encoded = payload["data"][0]["b64_json"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SystemExit(f"Unexpected image generation response shape: {payload.keys()}") from exc

    output_path.write_bytes(base64.b64decode(encoded))


def write_resized_reference_images(
    photos: list[Path],
    output_dir: Path,
    max_side: int,
    jpeg_quality: int,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    references = []
    for index, photo in enumerate(photos, start=1):
        with Image.open(photo) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        if max_side > 0:
            image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        output_path = output_dir / f"{index:02d}-{photo.stem}.jpg"
        image.save(output_path, format="JPEG", quality=jpeg_quality, optimize=True)
        references.append(output_path)
    return references


def call_image_edit(
    api_key: str,
    model: str,
    prompt: str,
    api_size: str,
    quality: str,
    reference_paths: list[Path],
    output_path: Path,
) -> None:
    data: dict[str, str] = {
        "model": model,
        "prompt": prompt,
        "n": "1",
        "size": api_size,
        "quality": quality,
        "background": "opaque",
        "output_format": "png",
    }
    handles = []
    files = []
    try:
        for path in reference_paths:
            handle = path.open("rb")
            handles.append(handle)
            files.append(("image[]", (path.name, handle, "image/jpeg")))
        response = requests.post(
            "https://api.openai.com/v1/images/edits",
            headers={"Authorization": f"Bearer {api_key}"},
            data=data,
            files=files,
            timeout=600,
        )
    finally:
        for handle in handles:
            handle.close()

    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": {"message": response.text[:1000]}}
        message = payload.get("error", {}).get("message") or payload
        raise SystemExit(f"Image edit failed with HTTP {response.status_code}: {message}")

    payload = response.json()
    try:
        encoded = payload["data"][0]["b64_json"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SystemExit(f"Unexpected image edit response shape: {payload.keys()}") from exc

    output_path.write_bytes(base64.b64decode(encoded))


def order_points(points: np.ndarray) -> np.ndarray:
    points = points.astype("float32")
    sums = points.sum(axis=1)
    diffs = np.diff(points, axis=1).ravel()
    ordered = np.zeros((4, 2), dtype="float32")
    ordered[0] = points[np.argmin(sums)]
    ordered[2] = points[np.argmax(sums)]
    ordered[1] = points[np.argmin(diffs)]
    ordered[3] = points[np.argmax(diffs)]
    return ordered


def polygon_area(points: np.ndarray) -> float:
    return float(abs(cv2.contourArea(points.astype("float32"))))


def quad_aspect(quad: np.ndarray) -> float:
    top = np.linalg.norm(quad[1] - quad[0])
    bottom = np.linalg.norm(quad[2] - quad[3])
    right = np.linalg.norm(quad[2] - quad[1])
    left = np.linalg.norm(quad[3] - quad[0])
    width = max((top + bottom) / 2, 1.0)
    height = max((right + left) / 2, 1.0)
    return width / height


def quad_height(quad: np.ndarray) -> float:
    right = np.linalg.norm(quad[2] - quad[1])
    left = np.linalg.norm(quad[3] - quad[0])
    return float((right + left) / 2)


def sort_slots_reading_order(slots: list[PhotoSlot]) -> list[PhotoSlot]:
    if not slots:
        return []
    ordered = sorted(slots, key=lambda slot: float(slot.quad[:, 1].mean()))
    median_height = float(np.median([quad_height(slot.quad) for slot in ordered]))
    row_tolerance = max(24.0, median_height * 0.60)
    rows: list[list[PhotoSlot]] = []
    for slot in ordered:
        center_y = float(slot.quad[:, 1].mean())
        for row in rows:
            row_y = float(np.mean([existing.quad[:, 1].mean() for existing in row]))
            if abs(center_y - row_y) <= row_tolerance:
                row.append(slot)
                break
        else:
            rows.append([slot])
    rows.sort(key=lambda row: float(np.mean([slot.quad[:, 1].mean() for slot in row])))
    result: list[PhotoSlot] = []
    for row in rows:
        result.extend(sorted(row, key=lambda slot: float(slot.quad[:, 0].mean())))
    return result


def bounding_iou(a: np.ndarray, b: np.ndarray) -> float:
    ax, ay, aw, ah = cv2.boundingRect(a.astype("float32"))
    bx, by, bw, bh = cv2.boundingRect(b.astype("float32"))
    x0 = max(ax, bx)
    y0 = max(ay, by)
    x1 = min(ax + aw, bx + bw)
    y1 = min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    intersection = (x1 - x0) * (y1 - y0)
    union = aw * ah + bw * bh - intersection
    return intersection / max(union, 1)


def contour_to_quad(contour: np.ndarray) -> np.ndarray:
    perimeter = cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, 0.035 * perimeter, True)
    if len(approx) == 4 and cv2.isContourConvex(approx):
        return order_points(approx.reshape(4, 2))
    rect = cv2.minAreaRect(contour)
    return order_points(cv2.boxPoints(rect))


def write_debug_slots(image: Image.Image, slots: list[PhotoSlot], debug_path: Path) -> None:
    bgr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    for index, slot in enumerate(slots, start=1):
        quad = slot.quad.astype("int32")
        cv2.polylines(bgr, [quad], True, (0, 0, 255), 8)
        center = tuple(np.round(slot.quad.mean(axis=0)).astype(int))
        cv2.putText(bgr, str(index), center, cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 0, 0), 5)
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(debug_path), bgr)


def marker_color_mask(rgb: np.ndarray, marker_key: str) -> np.ndarray:
    marker = MARKER_COLORS[marker_key]
    hsv = cv2.cvtColor(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), cv2.COLOR_BGR2HSV)
    hsv_mask = cv2.inRange(
        hsv,
        np.array(marker["hsv_lower"], dtype=np.uint8),
        np.array(marker["hsv_upper"], dtype=np.uint8),
    )
    red = rgb[:, :, 0].astype("int16")
    green = rgb[:, :, 1].astype("int16")
    blue = rgb[:, :, 2].astype("int16")
    if marker_key == "magenta":
        rgb_mask = ((red > 135) & (blue > 120) & (green < 165) & (((red + blue) // 2 - green) > 45))
    elif marker_key == "green":
        rgb_mask = ((green > 135) & (red < 165) & (blue < 165) & (green - np.maximum(red, blue) > 35))
    else:
        rgb_mask = ((green > 135) & (blue > 120) & (red < 165) & (((green + blue) // 2 - red) > 45))
    return cv2.bitwise_and(hsv_mask, rgb_mask.astype("uint8") * 255)


def detect_marker_photo_slots(
    image: Image.Image,
    expected_count: int,
    min_area_ratio: float,
    marker_key: str,
    debug_path: Path | None = None,
) -> list[PhotoSlot]:
    rgb = np.array(image.convert("RGB"))
    height, width = rgb.shape[:2]
    image_area = width * height
    min_area = image_area * min_area_ratio
    max_area = image_area * 0.55

    mask = marker_color_mask(rgb, marker_key)
    min_dimension = min(width, height)
    close_size = max(5, int(round(min_dimension * 0.003)) | 1)
    open_size = max(3, int(round(min_dimension * 0.0015)) | 1)
    mask = cv2.medianBlur(mask, 5)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((close_size, close_size), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((open_size, open_size), np.uint8))

    candidates: list[PhotoSlot] = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        contour_area = cv2.contourArea(contour)
        if contour_area < min_area or contour_area > max_area:
            continue
        quad = contour_to_quad(contour)
        area = polygon_area(quad)
        if area < min_area or area > max_area:
            continue
        aspect = quad_aspect(quad)
        if not 0.20 <= aspect <= 5.0:
            continue
        candidate_mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillConvexPoly(candidate_mask, quad.astype("int32"), 255)
        marker_pixels = cv2.countNonZero(cv2.bitwise_and(mask, candidate_mask))
        fill_ratio = contour_area / max(area, 1.0)
        marker_density = marker_pixels / max(area, 1.0)
        if fill_ratio < 0.45 and marker_density < 0.008:
            continue
        x, y, w, h = cv2.boundingRect(quad.astype("float32"))
        if x + w < 0 or y + h < 0 or x > width or y > height:
            continue
        score = area * (1.0 + min(fill_ratio, 1.0) + min(marker_density * 10.0, 1.0))
        candidates.append(PhotoSlot(quad=quad, area=area, score=score))

    candidates.sort(key=lambda slot: slot.score, reverse=True)
    selected: list[PhotoSlot] = []
    for candidate in candidates:
        if all(bounding_iou(candidate.quad, existing.quad) < 0.20 for existing in selected):
            selected.append(candidate)
        if len(selected) >= expected_count:
            break
    selected = sort_slots_reading_order(selected)

    if debug_path:
        write_debug_slots(image, selected, debug_path)
        cv2.imwrite(str(debug_path.with_name("slot-marker-mask.png")), mask)

    if len(selected) < expected_count:
        raise SystemExit(
            f"Detected {len(selected)} marker-colored photo slots, but need {expected_count}. "
            "Try rerunning generation, using fewer photos, or inspecting slot-marker-mask.png."
        )
    return selected[:expected_count]


def detect_blank_photo_slots(
    image: Image.Image,
    expected_count: int,
    min_area_ratio: float,
    debug_path: Path | None = None,
) -> list[PhotoSlot]:
    rgb = np.array(image.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    height, width = bgr.shape[:2]
    image_area = width * height
    min_area = image_area * min_area_ratio
    max_area = image_area * 0.45

    pale_mask = cv2.inRange(hsv, np.array([0, 0, 145]), np.array([179, 92, 255]))
    pale_mask = cv2.morphologyEx(pale_mask, cv2.MORPH_CLOSE, np.ones((19, 19), np.uint8))
    pale_mask = cv2.morphologyEx(pale_mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))

    candidates: list[PhotoSlot] = []
    for mask, source_weight in ((pale_mask, 2.0),):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area or area > max_area:
                continue
            quad = contour_to_quad(contour)
            area = polygon_area(quad)
            if area < min_area or area > max_area:
                continue
            aspect = quad_aspect(quad)
            if not 0.28 <= aspect <= 3.6:
                continue
            candidate_mask = np.zeros((height, width), dtype=np.uint8)
            cv2.fillConvexPoly(candidate_mask, quad.astype("int32"), 255)
            mean_hsv = cv2.mean(hsv, mask=candidate_mask)
            pale_score = max(0.0, (mean_hsv[2] - 120) / 135) + max(0.0, (110 - mean_hsv[1]) / 110)
            rectangularity = min(float(area) / max(cv2.contourArea(contour), 1.0), 1.4)
            score = area * (source_weight + pale_score + rectangularity)
            candidates.append(PhotoSlot(quad=quad, area=area, score=score))

    candidates.sort(key=lambda slot: slot.score, reverse=True)
    selected: list[PhotoSlot] = []
    for candidate in candidates:
        if all(bounding_iou(candidate.quad, existing.quad) < 0.30 for existing in selected):
            selected.append(candidate)
        if len(selected) >= expected_count:
            break

    selected = sort_slots_reading_order(selected)

    if debug_path:
        write_debug_slots(image, selected, debug_path)

    if len(selected) < expected_count:
        raise SystemExit(
            f"Detected {len(selected)} photo slots, but need {expected_count}. "
            "Try rerunning, lowering --min-slot-area, using fewer photos, or inspecting the debug slots image."
        )
    return selected[:expected_count]


def detect_photo_slots(
    image: Image.Image,
    expected_count: int,
    min_area_ratio: float,
    detection_mode: str,
    marker_key: str,
    debug_path: Path | None = None,
) -> list[PhotoSlot]:
    if detection_mode == "blank":
        return detect_blank_photo_slots(image, expected_count, min_area_ratio, debug_path)
    try:
        return detect_marker_photo_slots(image, expected_count, min_area_ratio, marker_key, debug_path)
    except SystemExit:
        if detection_mode != "auto":
            raise
        return detect_blank_photo_slots(image, expected_count, min_area_ratio, debug_path)


def shrink_quad(quad: np.ndarray, ratio: float) -> np.ndarray:
    center = quad.mean(axis=0)
    return center + (quad - center) * (1.0 - ratio)


def extract_slot_patch(image: Image.Image, quad: np.ndarray, inset: float = 0.09) -> Image.Image:
    quad = order_points(shrink_quad(quad, inset))
    top = np.linalg.norm(quad[1] - quad[0])
    bottom = np.linalg.norm(quad[2] - quad[3])
    right = np.linalg.norm(quad[2] - quad[1])
    left = np.linalg.norm(quad[3] - quad[0])
    target_width = max(32, int(round((top + bottom) / 2)))
    target_height = max(32, int(round((right + left) / 2)))
    dst_points = np.array(
        [[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(quad.astype("float32"), dst_points)
    source = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    warped = cv2.warpPerspective(
        source,
        matrix,
        (target_width, target_height),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))


@dataclass(frozen=True)
class DedupSignature:
    dhash: int
    ahash: int
    phash: int


@dataclass(frozen=True)
class DedupComparison:
    cost: float
    dhash_distance: int
    phash_distance: int
    ahash_distance: int
    close_votes: int
    very_close: bool
    variant: str


def dhash_bits(image: Image.Image, hash_size: int = 16) -> int:
    # Same 16x16 difference hash used by the photo-dedup scan.
    grayscale = ImageOps.grayscale(image)
    small = grayscale.resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    value = 0
    for y in range(hash_size):
        row = y * (hash_size + 1)
        for x in range(hash_size):
            value = (value << 1) | (1 if pixels[row + x] > pixels[row + x + 1] else 0)
    return value


def ahash_bits(image: Image.Image, hash_size: int = 16) -> int:
    grayscale = ImageOps.grayscale(image)
    small = grayscale.resize((hash_size, hash_size), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    average = sum(pixels) / len(pixels)
    value = 0
    for pixel in pixels:
        value = (value << 1) | (1 if pixel >= average else 0)
    return value


def phash_bits(image: Image.Image, hash_size: int = 16, highfreq_factor: int = 4) -> int:
    grayscale = ImageOps.grayscale(image).resize(
        (hash_size * highfreq_factor, hash_size * highfreq_factor),
        Image.Resampling.LANCZOS,
    )
    pixels = np.asarray(grayscale, dtype=np.float32)
    dct = cv2.dct(pixels)
    lowfreq = dct[:hash_size, :hash_size]
    values = lowfreq.flatten()
    median = float(np.median(values[1:]))
    bits = 0
    for value in values:
        bits = (bits << 1) | (1 if value >= median else 0)
    return bits


def hamming_distance(left: int, right: int) -> int:
    return int((left ^ right).bit_count())


def image_signature(image: Image.Image) -> DedupSignature:
    prepared = image.convert("RGB")
    return DedupSignature(
        dhash=dhash_bits(prepared),
        ahash=ahash_bits(prepared),
        phash=phash_bits(prepared),
    )


def dedup_signature_comparison(
    left: DedupSignature,
    right: DedupSignature,
    variant: str,
) -> DedupComparison:
    dhash_distance = hamming_distance(left.dhash, right.dhash)
    phash_distance = hamming_distance(left.phash, right.phash)
    ahash_distance = hamming_distance(left.ahash, right.ahash)
    close_votes = int(dhash_distance <= 14) + int(phash_distance <= 18) + int(ahash_distance <= 20)
    very_close = dhash_distance <= 10 and phash_distance <= 14

    # The original photo-dedup tool used thresholds for yes/no grouping. Here
    # all candidates need a rank, so keep its three hash distances as the score.
    cost = (
        0.40 * (dhash_distance / 256.0)
        + 0.35 * (phash_distance / 256.0)
        + 0.25 * (ahash_distance / 256.0)
    )
    if close_votes >= 2 or very_close:
        cost -= 0.08
    elif close_votes == 1:
        cost -= 0.025

    return DedupComparison(
        cost=max(cost, 0.0),
        dhash_distance=dhash_distance,
        phash_distance=phash_distance,
        ahash_distance=ahash_distance,
        close_votes=close_votes,
        very_close=very_close,
        variant=variant,
    )


def aspect_fit_size(aspect: float, long_side: int = 512) -> tuple[int, int]:
    aspect = max(aspect, 0.05)
    if aspect >= 1.0:
        return long_side, max(32, int(round(long_side / aspect)))
    return max(32, int(round(long_side * aspect))), long_side


def source_dedup_variants(source: Image.Image, slot_aspect: float) -> list[tuple[str, Image.Image]]:
    target_size = aspect_fit_size(slot_aspect)
    variants = [
        ("fit-center", ImageOps.fit(source, target_size, Image.Resampling.LANCZOS, centering=(0.5, 0.5))),
        ("fit-top", ImageOps.fit(source, target_size, Image.Resampling.LANCZOS, centering=(0.5, 0.0))),
        ("fit-bottom", ImageOps.fit(source, target_size, Image.Resampling.LANCZOS, centering=(0.5, 1.0))),
        ("fit-left", ImageOps.fit(source, target_size, Image.Resampling.LANCZOS, centering=(0.0, 0.5))),
        ("fit-right", ImageOps.fit(source, target_size, Image.Resampling.LANCZOS, centering=(1.0, 0.5))),
    ]
    source_aspect = source.width / max(source.height, 1)
    if abs(source_aspect - slot_aspect) <= 0.02:
        variants.insert(0, ("full-same-aspect", source.copy()))
    return variants


def dedup_match_comparison(
    slot_signature: DedupSignature,
    source: Image.Image,
    slot_aspect: float,
) -> DedupComparison:
    best: DedupComparison | None = None
    for variant_name, variant in source_dedup_variants(source, slot_aspect):
        comparison = dedup_signature_comparison(slot_signature, image_signature(variant), variant_name)
        if best is None or comparison.cost < best.cost:
            best = comparison
    if best is None:
        raise RuntimeError("No source variants were generated for dedup matching.")
    return best


def solve_assignment(costs: list[list[float]]) -> list[int]:
    slot_count = len(costs)
    if slot_count == 0:
        return []
    photo_count = len(costs[0])
    if photo_count != slot_count:
        raise SystemExit("Hash assignment requires the same number of photos and detected slots.")
    if slot_count > 18:
        raise SystemExit("Hash assignment currently supports up to 18 photos.")

    dp: dict[int, tuple[float, list[int]]] = {0: (0.0, [])}
    for slot_index in range(slot_count):
        next_dp: dict[int, tuple[float, list[int]]] = {}
        for mask, (base_cost, assignment) in dp.items():
            for photo_index in range(photo_count):
                bit = 1 << photo_index
                if mask & bit:
                    continue
                next_mask = mask | bit
                next_cost = base_cost + costs[slot_index][photo_index]
                previous = next_dp.get(next_mask)
                if previous is None or next_cost < previous[0]:
                    next_dp[next_mask] = (next_cost, assignment + [photo_index])
        dp = next_dp
    full_mask = (1 << photo_count) - 1
    return dp[full_mask][1]


def match_photos_to_slots_by_hash(
    template: Image.Image,
    photos: list[Path],
    slots: list[PhotoSlot],
    build_dir: Path,
) -> list[PhotoPlacement]:
    slot_patches_dir = build_dir / "slot-previews"
    slot_patches_dir.mkdir(parents=True, exist_ok=True)
    sources = []
    for photo in photos:
        with Image.open(photo) as opened:
            source = ImageOps.exif_transpose(opened).convert("RGB")
        source.thumbnail((900, 900), Image.Resampling.LANCZOS)
        sources.append(source.copy())

    comparisons: list[list[DedupComparison]] = []
    costs: list[list[float]] = []
    for slot_index, slot in enumerate(slots, start=1):
        patch = extract_slot_patch(template, slot.quad)
        patch.save(slot_patches_dir / f"slot-{slot_index:02d}.jpg", quality=92)
        aspect = quad_aspect(slot.quad)
        slot_signature = image_signature(patch)
        slot_comparisons = [dedup_match_comparison(slot_signature, source, aspect) for source in sources]
        comparisons.append(slot_comparisons)
        costs.append([comparison.cost for comparison in slot_comparisons])

    assignment = solve_assignment(costs)
    placements = []
    diagnostics = []
    for slot_index, photo_index in enumerate(assignment):
        ranked = sorted((cost, index) for index, cost in enumerate(costs[slot_index]))
        comparison = comparisons[slot_index][photo_index]
        rank = [index for _, index in ranked].index(photo_index) + 1
        placement = PhotoPlacement(
            photo=photos[photo_index],
            slot=slots[slot_index],
            match_cost=comparison.cost,
            match_rank=rank,
        )
        placements.append(placement)
        diagnostics.append(
            {
                "slot": slot_index + 1,
                "matched_photo": str(photos[photo_index]),
                "match_cost": round(comparison.cost, 5),
                "match_rank": rank,
                "dedup_distances": {
                    "dhash": comparison.dhash_distance,
                    "phash": comparison.phash_distance,
                    "ahash": comparison.ahash_distance,
                    "close_votes": comparison.close_votes,
                    "very_close": comparison.very_close,
                    "variant": comparison.variant,
                },
                "ranked_candidates": [
                    {
                        "photo": str(photos[index]),
                        "cost": round(cost, 5),
                        "dhash": comparisons[slot_index][index].dhash_distance,
                        "phash": comparisons[slot_index][index].phash_distance,
                        "ahash": comparisons[slot_index][index].ahash_distance,
                        "close_votes": comparisons[slot_index][index].close_votes,
                        "variant": comparisons[slot_index][index].variant,
                    }
                    for cost, index in ranked[: min(5, len(ranked))]
                ],
            }
        )
    (build_dir / "dedup-matches.json").write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
    (build_dir / "hash-matches.json").write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
    return placements


def crop_to_aspect(path: Path, aspect: float, min_target: tuple[int, int]) -> Image.Image:
    with Image.open(path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")
    target_width, target_height = min_target
    if aspect >= 1:
        size = (max(target_width, int(target_height * aspect)), target_height)
    else:
        size = (target_width, max(target_height, int(target_width / aspect)))
    return ImageOps.fit(source, size, Image.Resampling.LANCZOS)


def paste_photo_into_quad(canvas: Image.Image, photo_path: Path, quad: np.ndarray) -> None:
    quad = order_points(quad)
    top = np.linalg.norm(quad[1] - quad[0])
    bottom = np.linalg.norm(quad[2] - quad[3])
    right = np.linalg.norm(quad[2] - quad[1])
    left = np.linalg.norm(quad[3] - quad[0])
    target_width = max(16, int(round((top + bottom) / 2)))
    target_height = max(16, int(round((right + left) / 2)))
    aspect = target_width / max(target_height, 1)

    photo = crop_to_aspect(photo_path, aspect, (target_width, target_height))
    photo = photo.resize((target_width, target_height), Image.Resampling.LANCZOS)
    src = np.array(photo.convert("RGBA"))

    src_points = np.array(
        [[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(src_points, quad.astype("float32"))
    canvas_rgba = np.array(canvas.convert("RGBA"))
    warped = cv2.warpPerspective(
        src,
        matrix,
        (canvas_rgba.shape[1], canvas_rgba.shape[0]),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )
    mask_src = np.full((target_height, target_width), 255, dtype=np.uint8)
    mask = cv2.warpPerspective(
        mask_src,
        matrix,
        (canvas_rgba.shape[1], canvas_rgba.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    mask = cv2.GaussianBlur(mask, (5, 5), 0).astype("float32") / 255.0
    alpha = (warped[:, :, 3].astype("float32") / 255.0) * mask
    canvas_rgb = canvas_rgba[:, :, :3].astype("float32")
    warped_rgb = warped[:, :, :3].astype("float32")
    canvas_rgba[:, :, :3] = (warped_rgb * alpha[:, :, None] + canvas_rgb * (1 - alpha[:, :, None])).astype("uint8")
    canvas_rgba[:, :, 3] = 255
    canvas.paste(Image.fromarray(canvas_rgba, "RGBA"))


def cleanup_marker_pixels(canvas: Image.Image, marker_key: str) -> Image.Image:
    rgb = np.array(canvas.convert("RGB"))
    mask = marker_color_mask(rgb, marker_key)
    if int(cv2.countNonZero(mask)) == 0:
        return canvas
    height, width = mask.shape[:2]
    kernel_size = max(5, int(round(min(width, height) * 0.003)) | 1)
    mask = cv2.dilate(mask, np.ones((kernel_size, kernel_size), np.uint8), iterations=1)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    cleaned = cv2.inpaint(bgr, mask, 3, cv2.INPAINT_TELEA)
    cleaned_rgb = cv2.cvtColor(cleaned, cv2.COLOR_BGR2RGB)
    return Image.fromarray(cleaned_rgb, "RGB").convert("RGBA")


def write_manifest(
    path: Path,
    args: argparse.Namespace,
    photos: list[Path],
    template_path: Path,
    placements: list[PhotoPlacement],
) -> None:
    manifest = {
        "title": args.title,
        "model": args.model,
        "content_aware": args.content_aware,
        "slot_assignment": args.slot_assignment,
        "api_size": args.api_size,
        "quality": args.quality,
        "detection_mode": args.detection_mode,
        "slot_marker": args.slot_marker,
        "marker_cleanup": not args.no_marker_cleanup,
        "output_size": args.output_size,
        "output": str(args.output.resolve()),
        "template": str(template_path.resolve()),
        "photos": [str(path) for path in photos],
        "slot_count": len(placements),
        "slots": [
            {
                "photo": str(placement.photo),
                "quad": [[round(float(x), 2), round(float(y), 2)] for x, y in placement.slot.quad],
                "center": [
                    round(float(placement.slot.quad[:, 0].mean()), 2),
                    round(float(placement.slot.quad[:, 1].mean()), 2),
                ],
                "area": round(placement.slot.area, 2),
                "score": round(placement.slot.score, 2),
                "match_cost": round(placement.match_cost, 5) if placement.match_cost is not None else None,
                "match_rank": placement.match_rank,
            }
            for placement in placements
        ],
    }
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    output_size = parse_size(args.output_size)
    photos = collect_photos(args)
    build_dir = args.build_dir or args.output.resolve().parent / f"{args.output.stem}-build"
    build_dir.mkdir(parents=True, exist_ok=True)

    prompt = build_prompt(
        args.title,
        photos,
        output_size,
        args.detection_mode,
        args.slot_marker,
        args.theme,
        args.content_aware,
    )
    prompt_path = build_dir / "prompt.txt"
    prompt_path.write_text(prompt + "\n", encoding="utf-8")

    template_path = build_dir / "gpt-image-template.png"
    if args.reuse_template:
        template_path = args.reuse_template.expanduser().resolve()
        if not template_path.exists():
            raise SystemExit(f"Template does not exist: {template_path}")
    else:
        api_key = load_openai_api_key(args.openclaw_config)
        if args.content_aware:
            if len(photos) > 16:
                raise SystemExit("--content-aware supports up to 16 reference photos.")
            references = write_resized_reference_images(
                photos,
                build_dir / "reference-inputs",
                args.reference_max_side,
                args.reference_jpeg_quality,
            )
            call_image_edit(api_key, args.model, prompt, args.api_size, args.quality, references, template_path)
        else:
            call_image_generation(api_key, args.model, prompt, args.api_size, args.quality, template_path)

    with Image.open(template_path) as opened:
        canvas = ImageOps.exif_transpose(opened).convert("RGBA")
    if canvas.size != output_size:
        canvas = canvas.resize(output_size, Image.Resampling.LANCZOS)

    debug_path = build_dir / "detected-slots.jpg" if args.debug_slots else None
    slots = detect_photo_slots(
        canvas,
        len(photos),
        args.min_slot_area,
        args.detection_mode,
        args.slot_marker,
        debug_path=debug_path,
    )
    assignment_mode = args.slot_assignment
    if assignment_mode == "auto":
        assignment_mode = "dedup" if args.content_aware else "order"
    if assignment_mode in {"hash", "dedup"}:
        placements = match_photos_to_slots_by_hash(canvas, photos, slots, build_dir)
    else:
        placements = [PhotoPlacement(photo=photo, slot=slot) for photo, slot in zip(photos, slots)]

    for placement in placements:
        paste_photo_into_quad(canvas, placement.photo, shrink_quad(placement.slot.quad, args.slot_inset))
    if args.detection_mode in {"marker", "auto"} and not args.no_marker_cleanup:
        canvas = cleanup_marker_pixels(canvas, args.slot_marker)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(args.output, quality=95)
    write_manifest(build_dir / "manifest.json", args, photos, template_path, placements)

    print(f"wrote {args.output}")
    print(f"wrote {build_dir / 'manifest.json'}")
    if debug_path:
        print(f"wrote {debug_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
