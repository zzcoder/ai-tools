#!/usr/bin/env python3
"""Detect likely crossed-out receipt text rows.

This is intentionally conservative. It finds long dark horizontal strokes and
only reports them when the stroke crosses through an estimated text row, not
when it appears between rows as a receipt separator.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


MAX_ANALYSIS_WIDTH = 1800


def resize_for_analysis(gray: np.ndarray) -> tuple[np.ndarray, float]:
    height, width = gray.shape[:2]
    if width <= MAX_ANALYSIS_WIDTH:
        return gray, 1.0

    scale = MAX_ANALYSIS_WIDTH / float(width)
    resized = cv2.resize(
        gray,
        (MAX_ANALYSIS_WIDTH, max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def merge_bands(bands: list[dict[str, int]], max_gap: int) -> list[dict[str, int]]:
    if not bands:
        return []

    merged = [bands[0]]
    for band in bands[1:]:
        previous = merged[-1]
        if band["top"] - previous["bottom"] <= max_gap:
            previous["bottom"] = max(previous["bottom"], band["bottom"])
            previous["pixels"] += band["pixels"]
        else:
            merged.append(band)
    return merged


def estimate_text_bands(text_mask: np.ndarray) -> list[dict[str, int]]:
    height, width = text_mask.shape[:2]
    projection = np.count_nonzero(text_mask, axis=1).astype(np.float32)
    if height >= 5:
        projection = np.convolve(projection, np.ones(5, dtype=np.float32) / 5.0, mode="same")

    active_threshold = max(6, int(width * 0.004))
    active = projection > active_threshold
    bands: list[dict[str, int]] = []

    start = None
    for y, is_active in enumerate(active):
        if is_active and start is None:
            start = y
        elif not is_active and start is not None:
            end = y - 1
            pixels = int(np.count_nonzero(text_mask[start : end + 1, :]))
            if end - start + 1 >= 3 and pixels >= max(20, int(width * 0.025)):
                bands.append({"top": start, "bottom": end, "pixels": pixels})
            start = None

    if start is not None:
        end = height - 1
        pixels = int(np.count_nonzero(text_mask[start : end + 1, :]))
        if end - start + 1 >= 3 and pixels >= max(20, int(width * 0.025)):
            bands.append({"top": start, "bottom": end, "pixels": pixels})

    return merge_bands(bands, max(2, int(height * 0.0025)))


def stroke_text_overlap(text_mask: np.ndarray, band: dict[str, int], x: int, width: int) -> float:
    top = max(0, band["top"])
    bottom = min(text_mask.shape[0] - 1, band["bottom"])
    if bottom < top:
        return 0.0

    row = text_mask[top : bottom + 1, max(0, x) : min(text_mask.shape[1], x + width)]
    if row.size == 0:
        return 0.0
    columns_with_text = np.count_nonzero(np.count_nonzero(row, axis=0))
    return columns_with_text / float(max(1, width))


def analyze_image(image_path: str, page_index: int) -> dict[str, object]:
    gray = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return {
            "page": page_index + 1,
            "path": image_path,
            "error": "image could not be read",
            "textLineCount": 0,
            "crossedLineIndexes": [],
            "crossedLineRatios": [],
            "strokeCount": 0,
        }

    original_height, original_width = gray.shape[:2]
    gray, scale = resize_for_analysis(gray)
    height, width = gray.shape[:2]

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _threshold, binary = cv2.threshold(
        blurred,
        0,
        255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
    )

    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(35, int(width * 0.08)), max(1, int(height * 0.0015))),
    )
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel, iterations=1)
    horizontal_guard = cv2.dilate(
        horizontal,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, max(3, int(height * 0.003)))),
        iterations=1,
    )
    text_mask = cv2.bitwise_and(binary, cv2.bitwise_not(horizontal_guard))
    bands = estimate_text_bands(text_mask)

    contours, _hierarchy = cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    crossed: dict[int, float] = {}
    strokes = []

    for contour in contours:
        x, y, stroke_width, stroke_height = cv2.boundingRect(contour)
        width_ratio = stroke_width / float(max(1, width))
        if stroke_width < max(35, int(width * 0.08)):
            continue
        if stroke_height > max(12, int(height * 0.016)):
            continue
        if y < int(height * 0.03) or y > int(height * 0.98):
            continue

        center_y = y + stroke_height / 2.0
        best_index = None
        best_distance = None
        for index, band in enumerate(bands):
            top = band["top"]
            bottom = band["bottom"]
            if top <= center_y <= bottom:
                best_index = index
                best_distance = 0
                break
            distance = min(abs(center_y - top), abs(center_y - bottom))
            if best_distance is None or distance < best_distance:
                best_index = index
                best_distance = distance

        if best_index is None:
            continue

        band = bands[best_index]
        max_distance = max(2, int(height * 0.0025), stroke_height)
        if best_distance is not None and best_distance > max_distance:
            continue

        overlap = stroke_text_overlap(text_mask, band, x, stroke_width)
        if overlap < 0.015 and width_ratio < 0.18:
            continue

        confidence = min(0.98, 0.45 + width_ratio + min(0.25, overlap * 4.0))
        crossed[best_index] = max(crossed.get(best_index, 0.0), confidence)
        strokes.append(
            {
                "xRatio": round(x / float(max(1, width)), 4),
                "yRatio": round(center_y / float(max(1, height)), 4),
                "widthRatio": round(width_ratio, 4),
                "lineIndex": best_index,
                "confidence": round(confidence, 3),
            }
        )

    crossed_indexes = sorted(crossed)
    line_count = len(bands)
    return {
        "page": page_index + 1,
        "path": image_path,
        "width": original_width,
        "height": original_height,
        "analysisScale": round(scale, 4),
        "textLineCount": line_count,
        "crossedLineIndexes": crossed_indexes,
        "crossedLineRatios": [
            round(index / float(max(1, line_count - 1)), 4) for index in crossed_indexes
        ],
        "strokeCount": len(strokes),
        "strokes": strokes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", help="Receipt image paths to analyze")
    parser.add_argument("--json", action="store_true", help="Accepted for caller readability")
    args = parser.parse_args()

    pages = [analyze_image(str(Path(image)), index) for index, image in enumerate(args.images)]
    crossed_line_count = sum(len(page.get("crossedLineIndexes", [])) for page in pages)
    payload = {
        "crossedLineCount": crossed_line_count,
        "strokeCount": sum(int(page.get("strokeCount", 0)) for page in pages),
        "textLineCount": sum(int(page.get("textLineCount", 0)) for page in pages),
        "pages": pages,
    }
    print(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(json.dumps({"error": str(exc), "crossedLineCount": 0, "pages": []}))
        raise SystemExit(2)
