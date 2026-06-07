#!/usr/bin/env python3
"""Draw numbered markers on visible heads in a group photo.

Typical use:

    python3 tools/number_heads.py group-shot.png --detect

That creates a first-pass CSV of head centers and writes a numbered image.
For crowded photos, edit the CSV to add/remove/move points, then rerun without
--detect:

    python3 tools/number_heads.py group-shot.png --points group-shot-heads.csv

CSV format:

    id,x,y,r,label_dx,label_dy,note
    1,3811,807,32,0,0,

Only x and y are required. The radius and label offsets are optional.
"""

from __future__ import annotations

import argparse
import csv
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


Color = tuple[int, int, int]


@dataclass
class HeadPoint:
    x: int
    y: int
    radius: int | None = None
    note: str = ""
    label_dx: int = 0
    label_dy: int = 0


def default_points_path(image_path: Path) -> Path:
    return image_path.with_name(f"{image_path.stem}-heads.csv")


def default_output_path(image_path: Path) -> Path:
    return image_path.with_name(f"{image_path.stem}-numbered.png")


def write_numbered_image(path: Path, image: np.ndarray) -> bool:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return cv2.imwrite(str(path), image, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if suffix == ".png":
        return cv2.imwrite(str(path), image, [int(cv2.IMWRITE_PNG_COMPRESSION), 3])
    return cv2.imwrite(str(path), image)


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1 = max(ax, bx)
    y1 = max(ay, by)
    x2 = min(ax + aw, bx + bw)
    y2 = min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / float(aw * ah + bw * bh - inter)


def nms_boxes(
    boxes: list[tuple[int, int, int, int]],
    overlap_threshold: float = 0.35,
) -> list[tuple[int, int, int, int]]:
    boxes = sorted(boxes, key=lambda box: box[2] * box[3], reverse=True)
    kept: list[tuple[int, int, int, int]] = []
    for box in boxes:
        if all(iou(box, existing) < overlap_threshold for existing in kept):
            kept.append(box)
    return kept


def detect_heads(
    image: np.ndarray,
    scale_factor: float,
    min_neighbors: int,
    min_size: int,
    max_size: int,
) -> list[HeadPoint]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    cascade_dir = Path(cv2.data.haarcascades)
    cascades = [
        cv2.CascadeClassifier(str(cascade_dir / "haarcascade_frontalface_default.xml")),
        cv2.CascadeClassifier(str(cascade_dir / "haarcascade_frontalface_alt2.xml")),
        cv2.CascadeClassifier(str(cascade_dir / "haarcascade_profileface.xml")),
    ]

    boxes: list[tuple[int, int, int, int]] = []
    for cascade in cascades:
        detected = cascade.detectMultiScale(
            gray,
            scaleFactor=scale_factor,
            minNeighbors=min_neighbors,
            minSize=(min_size, min_size),
            maxSize=(max_size, max_size),
        )
        boxes.extend(tuple(map(int, box)) for box in detected)

    flipped = cv2.flip(gray, 1)
    width = image.shape[1]
    profile = cv2.CascadeClassifier(str(cascade_dir / "haarcascade_profileface.xml"))
    flipped_boxes = profile.detectMultiScale(
        flipped,
        scaleFactor=scale_factor,
        minNeighbors=min_neighbors,
        minSize=(min_size, min_size),
        maxSize=(max_size, max_size),
    )
    for x, y, w, h in flipped_boxes:
        boxes.append((int(width - x - w), int(y), int(w), int(h)))

    points: list[HeadPoint] = []
    for x, y, w, h in nms_boxes(boxes):
        radius = max(14, int(round(max(w, h) * 0.45)))
        points.append(HeadPoint(x=int(x + w / 2), y=int(y + h / 2), radius=radius))
    return points


def sort_points(points: list[HeadPoint], row_height: int) -> list[HeadPoint]:
    if not points:
        return []

    rows: list[list[HeadPoint]] = []
    for point in sorted(points, key=lambda p: p.y):
        for row in rows:
            row_y = sum(p.y for p in row) / len(row)
            if abs(point.y - row_y) <= row_height:
                row.append(point)
                break
        else:
            rows.append([point])

    ordered: list[HeadPoint] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda p: p.x))
    return ordered


def filter_points(
    points: list[HeadPoint],
    min_x: int | None,
    max_x: int | None,
    min_y: int | None,
    max_y: int | None,
) -> list[HeadPoint]:
    filtered: list[HeadPoint] = []
    for point in points:
        if min_x is not None and point.x < min_x:
            continue
        if max_x is not None and point.x > max_x:
            continue
        if min_y is not None and point.y < min_y:
            continue
        if max_y is not None and point.y > max_y:
            continue
        filtered.append(point)
    return filtered


def read_points(path: Path, default_radius: int) -> list[HeadPoint]:
    points: list[HeadPoint] = []
    with path.open(newline="") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
        if has_header:
            reader = csv.DictReader(line for line in handle if not line.lstrip().startswith("#"))
            for row in reader:
                if not row:
                    continue
                x = int(float(row["x"]))
                y = int(float(row["y"]))
                radius = int(float(row["r"])) if row.get("r") else default_radius
                label_dx = int(float(row.get("label_dx") or 0))
                label_dy = int(float(row.get("label_dy") or 0))
                points.append(
                    HeadPoint(
                        x=x,
                        y=y,
                        radius=radius,
                        note=row.get("note", ""),
                        label_dx=label_dx,
                        label_dy=label_dy,
                    )
                )
        else:
            reader = csv.reader(line for line in handle if not line.lstrip().startswith("#"))
            for row in reader:
                if not row:
                    continue
                x = int(float(row[0]))
                y = int(float(row[1]))
                radius = int(float(row[2])) if len(row) > 2 and row[2] else default_radius
                note = row[3] if len(row) > 3 else ""
                label_dx = int(float(row[4])) if len(row) > 4 and row[4] else 0
                label_dy = int(float(row[5])) if len(row) > 5 and row[5] else 0
                points.append(
                    HeadPoint(
                        x=x,
                        y=y,
                        radius=radius,
                        note=note,
                        label_dx=label_dx,
                        label_dy=label_dy,
                    )
                )
    return points


def write_points(path: Path, points: list[HeadPoint], default_radius: int) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "x", "y", "r", "label_dx", "label_dy", "note"])
        for index, point in enumerate(points, start=1):
            writer.writerow(
                [
                    index,
                    point.x,
                    point.y,
                    point.radius or default_radius,
                    point.label_dx,
                    point.label_dy,
                    point.note,
                ]
            )


def marker_radius(index: int, base_radius: int) -> int:
    digits = int(math.log10(index)) + 1 if index > 0 else 1
    return max(base_radius, 12 + digits * 8)


def parse_color(value: str) -> Color:
    named_colors: dict[str, Color] = {
        "black": (0, 0, 0),
        "blue": (255, 0, 0),
        "cyan": (255, 255, 0),
        "green": (0, 255, 0),
        "lime": (0, 255, 0),
        "magenta": (255, 0, 255),
        "orange": (0, 165, 255),
        "red": (0, 0, 255),
        "white": (255, 255, 255),
        "yellow": (0, 255, 255),
    }
    normalized = value.strip().lower()
    if normalized in named_colors:
        return named_colors[normalized]

    if normalized.startswith("#"):
        normalized = normalized[1:]
    if len(normalized) == 6:
        try:
            red = int(normalized[0:2], 16)
            green = int(normalized[2:4], 16)
            blue = int(normalized[4:6], 16)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Invalid color: {value}") from exc
        return (blue, green, red)

    raise argparse.ArgumentTypeError(
        f"Invalid color: {value}. Use a name like yellow, cyan, magenta, or #RRGGBB."
    )


def resolve_font_file(font_name: str, font_file: Path | None = None) -> Path | None:
    if font_file is not None:
        if font_file.exists():
            return font_file
        raise argparse.ArgumentTypeError(f"Font file does not exist: {font_file}")

    common_paths = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for path in common_paths:
        if path.exists():
            return path

    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", font_name],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None

    path = Path(result.stdout.strip())
    if result.returncode == 0 and path.exists():
        return path
    return None


def load_label_font(font_name: str, font_file: Path | None, font_size: int) -> ImageFont.ImageFont:
    resolved = resolve_font_file(font_name, font_file)
    if resolved is not None:
        return ImageFont.truetype(str(resolved), font_size)
    return ImageFont.load_default()


def draw_numbered_heads(
    image: np.ndarray,
    points: list[HeadPoint],
    default_radius: int,
    start: int,
    draw_radius: int | None = None,
    font_scale: float | None = None,
    font_name: str = "Arial Bold",
    font_file: Path | None = None,
    circle_alpha: float = 0.65,
    marker_color: Color = (0, 255, 255),
    label_offset_x: int = 0,
    label_offset_y: int = 0,
    leader_lines: bool = True,
    selected_index: int | None = None,
) -> np.ndarray:
    out = image.copy()
    overlay = image.copy()

    for offset, point in enumerate(points):
        number = start + offset
        radius = draw_radius or marker_radius(number, point.radius or default_radius)
        effective_offset_x = label_offset_x + point.label_dx
        effective_offset_y = label_offset_y + point.label_dy
        label_center = (point.x + effective_offset_x, point.y + effective_offset_y)
        if leader_lines and (effective_offset_x or effective_offset_y):
            cv2.line(
                out,
                (point.x, point.y),
                label_center,
                (0, 0, 0),
                max(1, radius // 12),
                lineType=cv2.LINE_AA,
            )
        cv2.circle(overlay, label_center, radius, marker_color, -1, lineType=cv2.LINE_AA)
        cv2.circle(out, label_center, radius, (0, 0, 0), max(1, radius // 10), lineType=cv2.LINE_AA)
        if selected_index == offset:
            cv2.circle(
                out,
                label_center,
                radius + max(5, radius // 4),
                (0, 0, 255),
                max(2, radius // 9),
                lineType=cv2.LINE_AA,
            )

    cv2.addWeighted(overlay, circle_alpha, out, 1.0 - circle_alpha, 0, dst=out)

    font_size = max(10, int(round((font_scale if font_scale is not None else 1.5) * 30)))
    label_font = load_label_font(font_name, font_file, font_size)
    pil_image = Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_image)

    for offset, point in enumerate(points):
        number = start + offset
        text = str(number)
        radius = draw_radius or marker_radius(number, point.radius or default_radius)
        label_x = point.x + label_offset_x + point.label_dx
        label_y = point.y + label_offset_y + point.label_dy
        bbox = draw.textbbox((0, 0), text, font=label_font, stroke_width=0)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        stroke_width = max(1, int(round(radius / 18)))
        draw.text(
            (label_x - text_w / 2, label_y - text_h / 2 - bbox[1]),
            text,
            font=label_font,
            fill=(0, 0, 0),
            stroke_width=stroke_width,
            stroke_fill=(255, 255, 255),
        )
    return cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)


def collect_points_with_clicks(
    image: np.ndarray,
    existing: list[HeadPoint],
    default_radius: int,
    window_width: int,
    draw_radius: int | None = None,
    font_scale: float | None = None,
    font_name: str = "Arial Bold",
    font_file: Path | None = None,
    circle_alpha: float = 0.65,
    marker_color: Color = (0, 255, 255),
    label_offset_x: int = 0,
    label_offset_y: int = 0,
    leader_lines: bool = True,
) -> list[HeadPoint]:
    points = list(existing)
    history: list[list[HeadPoint]] = []
    selected_index: int | None = None
    drag_index: int | None = None
    drag_label_only = False
    drag_anchor_offset_x = 0
    drag_anchor_offset_y = 0
    drag_target_delta_x = 0.0
    drag_target_delta_y = 0.0
    mouse_x: int | None = None
    mouse_y: int | None = None
    scale = min(1.0, window_width / image.shape[1])
    display_size = (int(image.shape[1] * scale), int(image.shape[0] * scale))
    window = "number heads: click marker select, click empty move, shift-click label, right-click remove"

    def draw_text_panel(frame: np.ndarray, lines: list[str], x: int, y: int) -> None:
        if not lines:
            return
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.55
        thickness = 1
        padding = 8
        line_gap = 8
        sizes = [
            cv2.getTextSize(line, font, font_scale, thickness)[0]
            for line in lines
        ]
        width = max(size[0] for size in sizes) + padding * 2
        height = sum(size[1] for size in sizes) + line_gap * (len(lines) - 1) + padding * 2
        x = max(0, min(frame.shape[1] - width - 1, x))
        y = max(0, min(frame.shape[0] - height - 1, y))
        overlay = frame.copy()
        cv2.rectangle(overlay, (x, y), (x + width, y + height), (20, 20, 20), -1)
        cv2.addWeighted(overlay, 0.72, frame, 0.28, 0, dst=frame)
        cv2.rectangle(frame, (x, y), (x + width, y + height), (255, 255, 255), 1)

        text_y = y + padding
        for line, size in zip(lines, sizes):
            text_y += size[1]
            cv2.putText(
                frame,
                line,
                (x + padding, text_y),
                font,
                font_scale,
                (255, 255, 255),
                thickness,
                lineType=cv2.LINE_AA,
            )
            text_y += line_gap

    def redraw() -> np.ndarray:
        preview = cv2.resize(image, display_size, interpolation=cv2.INTER_AREA)
        scaled_points = [
            HeadPoint(
                x=int(round(point.x * scale)),
                y=int(round(point.y * scale)),
                radius=max(8, int(round((point.radius or default_radius) * scale))),
                note=point.note,
                label_dx=int(round(point.label_dx * scale)),
                label_dy=int(round(point.label_dy * scale)),
            )
            for point in points
        ]
        scaled_draw_radius = None
        if draw_radius is not None:
            scaled_draw_radius = max(3, int(round(draw_radius * scale)))
        scaled_font_scale = None
        if font_scale is not None:
            scaled_font_scale = max(0.25, font_scale * scale)
        frame = draw_numbered_heads(
            preview,
            scaled_points,
            max(8, int(default_radius * scale)),
            1,
            draw_radius=scaled_draw_radius,
            font_scale=scaled_font_scale,
            font_name=font_name,
            font_file=font_file,
            circle_alpha=circle_alpha,
            marker_color=marker_color,
            label_offset_x=int(round(label_offset_x * scale)),
            label_offset_y=int(round(label_offset_y * scale)),
            leader_lines=leader_lines,
            selected_index=selected_index,
        )

        return frame

    def snapshot() -> None:
        history.append(
            [
                HeadPoint(
                    point.x,
                    point.y,
                    point.radius,
                    point.note,
                    point.label_dx,
                    point.label_dy,
                )
                for point in points
            ]
        )

    def clear_invalid_selection() -> None:
        nonlocal drag_index, selected_index
        if selected_index is not None and not 0 <= selected_index < len(points):
            selected_index = None
        if drag_index is not None and not 0 <= drag_index < len(points):
            drag_index = None

    def restore_previous() -> None:
        if history:
            points[:] = history.pop()
            clear_invalid_selection()

    def nearest_marker(
        x: int,
        y: int,
        require_close: bool = False,
    ) -> tuple[int | None, int, int]:
        if not points:
            return None, 0, 0

        base_radius = draw_radius if draw_radius is not None else default_radius
        threshold = max(18, int(round(base_radius * scale * 1.6)))
        best_index: int | None = None
        best_anchor_x = 0
        best_anchor_y = 0
        best_distance = float("inf")

        for index, point in enumerate(points):
            point_label_offset_x = label_offset_x + point.label_dx
            point_label_offset_y = label_offset_y + point.label_dy
            targets = [
                (point.x * scale, point.y * scale, 0, 0),
                (
                    point.x * scale + point_label_offset_x * scale,
                    point.y * scale + point_label_offset_y * scale,
                    point_label_offset_x,
                    point_label_offset_y,
                ),
            ]
            for target_x, target_y, anchor_x, anchor_y in targets:
                distance = (target_x - x) ** 2 + (target_y - y) ** 2
                if distance < best_distance:
                    best_distance = distance
                    best_index = index
                    best_anchor_x = anchor_x
                    best_anchor_y = anchor_y

        if require_close and best_distance > threshold**2:
            return None, 0, 0
        return best_index, best_anchor_x, best_anchor_y

    def remove_nearest(x: int, y: int) -> None:
        nonlocal selected_index
        nearest, _anchor_x, _anchor_y = nearest_marker(x, y)
        if nearest is None:
            return
        snapshot()
        points.pop(nearest)
        if selected_index is not None:
            if selected_index == nearest:
                selected_index = None
            elif selected_index > nearest:
                selected_index -= 1

    def move_selected_to(x: int, y: int, label_only: bool) -> None:
        nonlocal selected_index
        if selected_index is None:
            return
        if not 0 <= selected_index < len(points):
            selected_index = None
            return
        snapshot()
        point = points[selected_index]
        target_x = int(round(x / scale))
        target_y = int(round(y / scale))
        if label_only:
            point.label_dx = target_x - point.x - label_offset_x
            point.label_dy = target_y - point.y - label_offset_y
        else:
            point.x = max(0, min(image.shape[1] - 1, target_x))
            point.y = max(0, min(image.shape[0] - 1, target_y))

    def on_mouse(event: int, x: int, y: int, _flags: int, _param: object) -> None:
        nonlocal drag_anchor_offset_x, drag_anchor_offset_y, drag_index, drag_label_only
        nonlocal selected_index
        nonlocal drag_target_delta_x, drag_target_delta_y, mouse_x, mouse_y

        mouse_x = x
        mouse_y = y

        if event == cv2.EVENT_LBUTTONDOWN:
            nearest, anchor_x, anchor_y = nearest_marker(x, y, require_close=True)
            if nearest is not None:
                selected_index = nearest
                snapshot()
                drag_index = nearest
                drag_anchor_offset_x = anchor_x
                drag_anchor_offset_y = anchor_y
                drag_label_only = bool(_flags & cv2.EVENT_FLAG_SHIFTKEY)
                target_x = points[nearest].x + anchor_x
                target_y = points[nearest].y + anchor_y
                drag_target_delta_x = target_x - x / scale
                drag_target_delta_y = target_y - y / scale
                return

            if selected_index is not None and _flags & cv2.EVENT_FLAG_SHIFTKEY:
                move_selected_to(x, y, label_only=True)
                return

            if selected_index is not None and _flags & cv2.EVENT_FLAG_CTRLKEY:
                move_selected_to(x, y, label_only=False)
                return

            snapshot()
            points.append(
                HeadPoint(
                    x=int(round(x / scale)),
                    y=int(round(y / scale)),
                    radius=default_radius,
                )
            )
            selected_index = len(points) - 1
        elif event == cv2.EVENT_MOUSEMOVE and drag_index is not None:
            if _flags & cv2.EVENT_FLAG_LBUTTON:
                if not 0 <= drag_index < len(points):
                    drag_index = None
                    drag_label_only = False
                    return
                target_x = x / scale + drag_target_delta_x
                target_y = y / scale + drag_target_delta_y
                point = points[drag_index]
                if drag_label_only:
                    point.label_dx = int(round(target_x - point.x - label_offset_x))
                    point.label_dy = int(round(target_y - point.y - label_offset_y))
                else:
                    point.x = int(round(target_x - drag_anchor_offset_x))
                    point.y = int(round(target_y - drag_anchor_offset_y))
                    point.x = max(0, min(image.shape[1] - 1, point.x))
                    point.y = max(0, min(image.shape[0] - 1, point.y))
            else:
                drag_index = None
        elif event == cv2.EVENT_LBUTTONUP:
            drag_index = None
            drag_label_only = False
        elif event == cv2.EVENT_RBUTTONDOWN:
            remove_nearest(x, y)

    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window, on_mouse)
    while True:
        cv2.imshow(window, redraw())
        key = cv2.waitKey(50) & 0xFF
        if key == ord("u"):
            restore_previous()
        elif key == ord("c"):
            selected_index = None
        elif key == ord("m") and mouse_x is not None and mouse_y is not None:
            move_selected_to(mouse_x, mouse_y, label_only=False)
        elif key == ord("l") and mouse_x is not None and mouse_y is not None:
            move_selected_to(mouse_x, mouse_y, label_only=True)
        elif key in (ord("s"), ord("q"), 27):
            break
    cv2.destroyWindow(window)
    return points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="Input image path")
    parser.add_argument("--points", type=Path, help="CSV with x/y head centers")
    parser.add_argument("--output", type=Path, help="Numbered image output path")
    parser.add_argument("--detect", action="store_true", help="Create/overwrite the points CSV with OpenCV detections")
    parser.add_argument("--click", action="store_true", help="Open a click-to-edit window before drawing")
    parser.add_argument("--no-sort", action="store_true", help="Keep CSV/detection order instead of row-major order")
    parser.add_argument("--row-height", type=int, default=85, help="Y tolerance for row-major sorting")
    parser.add_argument("--radius", type=int, default=32, help="Default marker radius in pixels")
    parser.add_argument("--draw-radius", type=int, default=45, help="Override all stored/detected marker radii when drawing")
    parser.add_argument("--font-scale", type=float, default=1.5, help="Override number text size")
    parser.add_argument("--font-name", default="Arial Bold", help="TrueType font name to resolve; default is Photoshop-compatible Arial Bold")
    parser.add_argument("--font-file", type=Path, help="Explicit .ttf/.otf file for label text")
    parser.add_argument("--circle-alpha", type=float, default=0.65, help="Marker opacity from 0.0 to 1.0")
    parser.add_argument("--marker-color", type=parse_color, default=parse_color("yellow"), help="Marker color name or #RRGGBB")
    parser.add_argument("--label-offset-x", type=int, default=0, help="Draw labels this many pixels right of each head center")
    parser.add_argument("--label-offset-y", type=int, default=-105, help="Draw labels this many pixels below each head center")
    parser.add_argument("--no-leader-lines", action="store_true", help="Do not draw connector lines from head centers to offset labels")
    parser.add_argument("--start", type=int, default=1, help="First number to draw")
    parser.add_argument("--scale-factor", type=float, default=1.08, help="OpenCV detection scale factor")
    parser.add_argument("--min-neighbors", type=int, default=8, help="OpenCV detection minNeighbors")
    parser.add_argument("--min-size", type=int, default=24, help="Smallest detected face/head box")
    parser.add_argument("--max-size", type=int, default=180, help="Largest detected face/head box")
    parser.add_argument("--min-x", type=int, help="Ignore detected markers left of this image x coordinate")
    parser.add_argument("--max-x", type=int, help="Ignore detected markers right of this image x coordinate")
    parser.add_argument("--min-y", type=int, help="Ignore detected markers above this image y coordinate")
    parser.add_argument("--max-y", type=int, help="Ignore detected markers below this image y coordinate")
    parser.add_argument("--window-width", type=int, default=1800, help="Interactive review window width")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    image_path = args.image
    points_path = args.points or default_points_path(image_path)
    output_path = args.output or default_output_path(image_path)

    image = cv2.imread(str(image_path))
    if image is None:
        raise SystemExit(f"Could not read image: {image_path}")

    if args.detect:
        points = detect_heads(
            image=image,
            scale_factor=args.scale_factor,
            min_neighbors=args.min_neighbors,
            min_size=args.min_size,
            max_size=args.max_size,
        )
        points = filter_points(points, args.min_x, args.max_x, args.min_y, args.max_y)
    elif points_path.exists():
        points = read_points(points_path, args.radius)
    else:
        points = []

    if not args.no_sort:
        points = sort_points(points, args.row_height)

    if args.click:
        points = collect_points_with_clicks(
            image,
            points,
            args.radius,
            args.window_width,
            draw_radius=args.draw_radius,
            font_scale=args.font_scale,
            font_name=args.font_name,
            font_file=args.font_file,
            circle_alpha=args.circle_alpha,
            marker_color=args.marker_color,
            label_offset_x=args.label_offset_x,
            label_offset_y=args.label_offset_y,
            leader_lines=not args.no_leader_lines,
        )
        if not args.no_sort:
            points = sort_points(points, args.row_height)

    write_points(points_path, points, args.radius)
    numbered = draw_numbered_heads(
        image,
        points,
        args.radius,
        args.start,
        draw_radius=args.draw_radius,
        font_scale=args.font_scale,
        font_name=args.font_name,
        font_file=args.font_file,
        circle_alpha=args.circle_alpha,
        marker_color=args.marker_color,
        label_offset_x=args.label_offset_x,
        label_offset_y=args.label_offset_y,
        leader_lines=not args.no_leader_lines,
    )
    ok = write_numbered_image(output_path, numbered)
    if not ok:
        raise SystemExit(f"Could not write output: {output_path}")

    print(f"Wrote {len(points)} head markers to {points_path}")
    print(f"Wrote numbered image to {output_path}")


if __name__ == "__main__":
    main()
