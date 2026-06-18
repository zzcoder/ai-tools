#!/usr/bin/env python3
"""Build an offline people index for Sarah Wang's Flickr archive."""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from datetime import datetime
from hashlib import sha1
from pathlib import Path

import cv2
import numpy as np


ROOT = Path("sarah-flickr")
ASSETS = ROOT / "assets"
FACES = ASSETS / "faces"
CROPS = FACES / "crops"
ARCHIVE_JS = ASSETS / "archive-data.js"
PEOPLE_JS = ASSETS / "people-data.js"
CACHE_JSON = FACES / "face-cache.json"
DETECT_MODEL = Path("tools/models/face_detection_yunet_2023mar.onnx")
RECOGNITION_MODEL = Path("tools/models/face_recognition_sface_2021dec.onnx")
DETECT_SIZE = 320
FACE_CROP_SIZE = 180


def load_archive() -> dict:
    text = ARCHIVE_JS.read_text(encoding="utf-8")
    prefix = "window.SARAH_ARCHIVE = "
    if not text.startswith(prefix):
        raise ValueError(f"{ARCHIVE_JS} does not look like generated archive data")
    return json.loads(text[len(prefix) :].strip().removesuffix(";"))


def load_cache(model_signature: str) -> dict:
    if not CACHE_JSON.exists():
        return {"modelSignature": model_signature, "items": {}}
    try:
        cache = json.loads(CACHE_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"modelSignature": model_signature, "items": {}}
    if cache.get("modelSignature") != model_signature:
        return {"modelSignature": model_signature, "items": {}}
    cache.setdefault("items", {})
    return cache


def save_cache(cache: dict) -> None:
    FACES.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(CACHE_JSON)


def model_signature(min_score: float) -> str:
    parts = ["people-index-v1", f"score={min_score:.3f}"]
    for path in (DETECT_MODEL, RECOGNITION_MODEL):
        stat = path.stat()
        parts.append(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}")
    return "|".join(parts)


def photo_items(archive: dict, limit: int | None) -> list[dict]:
    items: list[dict] = []
    for album in archive["albums"]:
        for item in album["media"]:
            if item["type"] != "photo":
                continue
            items.append(
                {
                    "albumId": album["id"],
                    "albumTitle": album["title"],
                    "albumYear": album["year"],
                    "albumPlace": album["place"],
                    "src": item["src"],
                    "thumb": item["thumb"],
                    "name": item["name"],
                    "type": "photo",
                }
            )
            if limit and len(items) >= limit:
                return items
    return items


def safe_stem(path: str) -> str:
    stem = Path(path).stem
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem).strip("._")
    return safe or "face"


def crop_path_for(item: dict, index: int, bbox: list[float]) -> Path:
    digest_input = f"{item['src']}|{index}|{','.join(f'{value:.1f}' for value in bbox[:4])}"
    digest = sha1(digest_input.encode("utf-8")).hexdigest()[:12]
    year = item["src"].split("/", 1)[0]
    if not year.isdigit():
        year = "unknown-year"
    return CROPS / year / f"{safe_stem(item['src'])}-face-{index:02d}-{digest}.jpg"


def cache_key(item: dict, thumb_path: Path) -> str:
    stat = thumb_path.stat()
    return f"{item['thumb']}|{stat.st_size}|{int(stat.st_mtime)}"


def detect_item_faces(
    item: dict,
    detector: cv2.FaceDetectorYN,
    recognizer: cv2.FaceRecognizerSF,
    min_score: float,
) -> list[dict]:
    thumb_path = ROOT / item["thumb"]
    image = cv2.imread(str(thumb_path))
    if image is None:
        return []

    resized = cv2.resize(image, (DETECT_SIZE, DETECT_SIZE), interpolation=cv2.INTER_AREA)
    detector.setInputSize((DETECT_SIZE, DETECT_SIZE))
    _, detections = detector.detect(resized)
    if detections is None:
        return []

    faces: list[dict] = []
    ordered = sorted(detections, key=lambda face: (float(face[1]), float(face[0])))
    for index, face in enumerate(ordered):
        score = float(face[14])
        if score < min_score:
            continue
        x, y, width, height = [float(value) for value in face[:4]]
        if width < 18 or height < 18:
            continue
        try:
            aligned = recognizer.alignCrop(resized, face)
            embedding = recognizer.feature(aligned).flatten().astype("float32")
        except cv2.error:
            continue
        norm = float(np.linalg.norm(embedding))
        if not math.isfinite(norm) or norm == 0:
            continue
        embedding /= norm

        crop_path = crop_path_for(item, index, [x, y, width, height])
        if not crop_path.exists():
            crop_path.parent.mkdir(parents=True, exist_ok=True)
            crop = cv2.resize(aligned, (FACE_CROP_SIZE, FACE_CROP_SIZE), interpolation=cv2.INTER_CUBIC)
            cv2.imwrite(str(crop_path), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 86])

        faces.append(
            {
                "src": item["src"],
                "thumb": item["thumb"],
                "name": item["name"],
                "albumId": item["albumId"],
                "albumTitle": item["albumTitle"],
                "albumYear": item["albumYear"],
                "albumPlace": item["albumPlace"],
                "face": crop_path.relative_to(ROOT).as_posix(),
                "score": round(score, 4),
                "box": [round(x, 2), round(y, 2), round(width, 2), round(height, 2)],
                "embedding": [round(float(value), 6) for value in embedding],
            }
        )
    return faces


def collect_faces(
    items: list[dict],
    min_score: float,
    force: bool,
    progress_every: int,
) -> list[dict]:
    if not DETECT_MODEL.exists() or not RECOGNITION_MODEL.exists():
        raise FileNotFoundError(
            "Missing OpenCV face models. Expected "
            f"{DETECT_MODEL} and {RECOGNITION_MODEL}."
        )

    signature = model_signature(min_score)
    cache = load_cache(signature)
    detector = cv2.FaceDetectorYN.create(
        str(DETECT_MODEL),
        "",
        (DETECT_SIZE, DETECT_SIZE),
        min_score,
        0.3,
        5000,
    )
    recognizer = cv2.FaceRecognizerSF.create(str(RECOGNITION_MODEL), "")

    faces: list[dict] = []
    started = time.time()
    for index, item in enumerate(items, start=1):
        thumb_path = ROOT / item["thumb"]
        if not thumb_path.exists():
            continue
        key = cache_key(item, thumb_path)
        cached = cache["items"].get(item["src"])
        if not force and cached and cached.get("key") == key:
            item_faces = cached.get("faces", [])
        else:
            item_faces = detect_item_faces(item, detector, recognizer, min_score)
            cache["items"][item["src"]] = {"key": key, "faces": item_faces}
        faces.extend(item_faces)

        if progress_every and index % progress_every == 0:
            elapsed = time.time() - started
            print(f"scanned {index}/{len(items)} photos, {len(faces)} faces, elapsed {elapsed:.1f}s", flush=True)

    save_cache(cache)
    return faces


def normalized(values: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(values))
    if norm == 0 or not math.isfinite(norm):
        return values
    return values / norm


def cluster_faces(faces: list[dict], threshold: float) -> list[dict]:
    clusters: list[dict] = []
    centroids = np.empty((0, 128), dtype="float32")

    sorted_faces = sorted(faces, key=lambda face: face["score"], reverse=True)
    for face in sorted_faces:
        embedding = np.asarray(face["embedding"], dtype="float32")
        if not len(clusters):
            clusters.append({"faces": [face], "centroid": embedding})
            centroids = np.vstack([centroids, embedding])
            continue

        similarities = centroids @ embedding
        cluster_index = int(np.argmax(similarities))
        if float(similarities[cluster_index]) >= threshold:
            cluster = clusters[cluster_index]
            cluster["faces"].append(face)
            count = len(cluster["faces"])
            centroid = normalized((cluster["centroid"] * (count - 1) + embedding) / count).astype("float32")
            cluster["centroid"] = centroid
            centroids[cluster_index] = centroid
        else:
            clusters.append({"faces": [face], "centroid": embedding})
            centroids = np.vstack([centroids, embedding])

    return clusters


def unique_media_for_cluster(cluster_faces: list[dict]) -> list[dict]:
    media_by_src: dict[str, dict] = {}
    for face in sorted(cluster_faces, key=lambda item: item["score"], reverse=True):
        if face["src"] in media_by_src:
            continue
        media_by_src[face["src"]] = {
            "type": "photo",
            "src": face["src"],
            "thumb": face["thumb"],
            "name": face["name"],
            "albumId": face["albumId"],
            "albumTitle": face["albumTitle"],
            "albumYear": face["albumYear"],
            "albumPlace": face["albumPlace"],
            "face": face["face"],
        }
    return list(media_by_src.values())


def load_people_config() -> dict:
    labels_path = FACES / "people-labels.json"
    if not labels_path.exists():
        return {"labels": {}, "mergeGroups": [], "displayMergeGroups": []}
    try:
        data = json.loads(labels_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"labels": {}, "mergeGroups": [], "displayMergeGroups": []}
    if not isinstance(data, dict):
        return {"labels": {}, "mergeGroups": [], "displayMergeGroups": []}

    labels = data.get("labels")
    if isinstance(labels, dict):
        labels = {str(key): str(value) for key, value in labels.items()}
    else:
        labels = {
            str(key): str(value)
            for key, value in data.items()
            if key
            not in {
                "displayMerge",
                "displayMergeGroups",
                "labels",
                "merge",
                "mergeGroups",
                "postMerge",
                "postMergeGroups",
            }
            and isinstance(value, str)
        }

    merge_groups = data.get("mergeGroups", data.get("merge", []))
    if not isinstance(merge_groups, list):
        merge_groups = []
    display_merge_groups = data.get(
        "displayMergeGroups",
        data.get("displayMerge", data.get("postMergeGroups", data.get("postMerge", []))),
    )
    if not isinstance(display_merge_groups, list):
        display_merge_groups = []
    return {
        "labels": labels,
        "mergeGroups": merge_groups,
        "displayMergeGroups": display_merge_groups,
    }


def person_from_cluster(index: int, cluster_faces_list: list[dict], media: list[dict], labels: dict[str, str]) -> dict:
    sorted_faces = sorted(cluster_faces_list, key=lambda face: face["score"], reverse=True)
    signature = "|".join(sorted(face["src"] for face in sorted_faces[:24]))
    person_id = "person-" + sha1(signature.encode("utf-8")).hexdigest()[:10]
    custom_label = labels.get(person_id)
    samples = [
        {
            "face": face["face"],
            "src": face["src"],
            "thumb": face["thumb"],
            "albumTitle": face["albumTitle"],
            "albumYear": face["albumYear"],
        }
        for face in sorted_faces[:8]
    ]
    return {
        "id": person_id,
        "label": custom_label or f"Person {index}",
        "customLabel": bool(custom_label),
        "faceCount": len(cluster_faces_list),
        "photoCount": len(media),
        "cover": sorted_faces[0]["face"],
        "samples": samples,
        "media": media,
        "sourceGroups": [index],
        "sourceIds": [person_id],
    }


def merge_people(
    people: list[dict],
    merge_groups: list,
    labels: dict[str, str],
    group_name: str = "merge",
) -> tuple[list[dict], list[str]]:
    by_number = {index: person for index, person in enumerate(people, start=1)}
    assigned_numbers: set[int] = set()
    merged_numbers: set[int] = set()
    merged_people: list[tuple[int, dict]] = []
    warnings: list[str] = []

    for merge_index, raw_group in enumerate(merge_groups, start=1):
        warning_prefix = f"{group_name} group {merge_index}"
        if not isinstance(raw_group, list):
            warnings.append(f"{warning_prefix} is not a list")
            continue
        numbers: list[int] = []
        for raw_number in raw_group:
            try:
                number = int(raw_number)
            except (TypeError, ValueError):
                warnings.append(f"{warning_prefix} has non-numeric value {raw_number!r}")
                continue
            if number not in by_number:
                warnings.append(f"{warning_prefix} references missing Person {number}")
                continue
            if number in assigned_numbers:
                warnings.append(f"Person {number} repeated in {group_name}; first merge group wins")
                continue
            assigned_numbers.add(number)
            numbers.append(number)

        if len(numbers) < 2:
            continue

        parts = [by_number[number] for number in numbers]
        media_by_src: dict[str, dict] = {}
        for person in parts:
            for item in person["media"]:
                media_by_src.setdefault(item["src"], item)
        media = list(media_by_src.values())

        sample_by_face: dict[str, dict] = {}
        for person in parts:
            for sample in person.get("samples", []):
                sample_by_face.setdefault(sample["face"], sample)
        samples = list(sample_by_face.values())[:8]

        source_ids = [source_id for person in parts for source_id in person.get("sourceIds", [person["id"]])]
        source_groups = [group for person in parts for group in person.get("sourceGroups", [])]
        merged_id = "person-merged-" + sha1(",".join(map(str, numbers)).encode("utf-8")).hexdigest()[:10]
        custom_label = labels.get(merged_id)
        inherited_label = next(
            (person["label"] for person in parts if person.get("customLabel")),
            None,
        )
        merged_label = custom_label or inherited_label or parts[0]["label"]
        merged_people.append(
            (
                numbers[0],
                {
                    "id": merged_id,
                    "label": merged_label,
                    "customLabel": bool(custom_label or inherited_label),
                    "faceCount": sum(person["faceCount"] for person in parts),
                    "photoCount": len(media),
                    "cover": parts[0]["cover"],
                    "samples": samples,
                    "media": media,
                    "sourceGroups": source_groups,
                    "sourceIds": source_ids,
                },
            )
        )
        merged_numbers.update(numbers)

    final_people: list[tuple[int, dict]] = merged_people
    for index, person in enumerate(people, start=1):
        if index not in merged_numbers:
            final_people.append((index, person))
    final_people.sort(key=lambda item: item[0])
    return [person for _, person in final_people], warnings


def renumber_default_people(people: list[dict]) -> None:
    for display_index, person in enumerate(people, start=1):
        if not person.get("customLabel"):
            person["label"] = f"Person {display_index}"


def build_people_payload(
    faces: list[dict],
    clusters: list[dict],
    threshold: float,
    min_faces: int,
    min_photos: int,
    max_people: int,
) -> dict:
    config = load_people_config()
    labels = config["labels"]
    usable_clusters = []
    for cluster in clusters:
        cluster_faces_list = cluster["faces"]
        media = unique_media_for_cluster(cluster_faces_list)
        if len(cluster_faces_list) < min_faces or len(media) < min_photos:
            continue
        usable_clusters.append((cluster_faces_list, media))

    usable_clusters.sort(key=lambda pair: (len(pair[1]), len(pair[0])), reverse=True)
    if max_people:
        usable_clusters = usable_clusters[:max_people]

    people = []
    for index, (cluster_faces_list, media) in enumerate(usable_clusters, start=1):
        people.append(person_from_cluster(index, cluster_faces_list, media, labels))

    people, merge_warnings = merge_people(people, config["mergeGroups"], labels)
    renumber_default_people(people)
    people, display_merge_warnings = merge_people(
        people,
        config["displayMergeGroups"],
        labels,
        group_name="display merge",
    )
    renumber_default_people(people)
    merge_warnings.extend(display_merge_warnings)

    photos_with_faces = len({face["src"] for face in faces})
    return {
        "metadata": {
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "detectedFaces": len(faces),
            "photosWithFaces": photos_with_faces,
            "indexedPeople": len(people),
            "clusterThreshold": threshold,
            "minClusterFaces": min_faces,
            "minClusterPhotos": min_photos,
            "mergeGroups": len(config["mergeGroups"]),
            "displayMergeGroups": len(config["displayMergeGroups"]),
            "mergeWarnings": merge_warnings,
            "note": "Automatic face groups need human review; labels and merge groups can be overridden in assets/faces/people-labels.json.",
        },
        "people": people,
    }


def write_people_data(payload: dict) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    PEOPLE_JS.write_text(
        "window.SARAH_PEOPLE_INDEX = "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Scan only the first N photos for testing.")
    parser.add_argument("--min-score", type=float, default=0.80, help="Minimum YuNet face confidence.")
    parser.add_argument("--cluster-threshold", type=float, default=0.48, help="Minimum SFace cosine similarity for a cluster match.")
    parser.add_argument("--min-faces", type=int, default=4, help="Minimum detected faces per indexed group.")
    parser.add_argument("--min-photos", type=int, default=3, help="Minimum unique photos per indexed group.")
    parser.add_argument("--max-people", type=int, default=180, help="Maximum groups to write; 0 means no limit.")
    parser.add_argument("--force", action="store_true", help="Ignore cached detections and rescan thumbnails.")
    parser.add_argument("--progress-every", type=int, default=250, help="Print scan progress every N photos.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    archive = load_archive()
    items = photo_items(archive, args.limit)
    started = time.time()
    faces = collect_faces(items, args.min_score, args.force, args.progress_every)
    clusters = cluster_faces(faces, args.cluster_threshold)
    payload = build_people_payload(
        faces,
        clusters,
        args.cluster_threshold,
        args.min_faces,
        args.min_photos,
        args.max_people,
    )
    write_people_data(payload)
    elapsed = time.time() - started
    print(
        f"Wrote {PEOPLE_JS} with {payload['metadata']['indexedPeople']} people groups, "
        f"{payload['metadata']['detectedFaces']} faces across {payload['metadata']['photosWithFaces']} photos "
        f"in {elapsed:.1f}s."
    )


if __name__ == "__main__":
    main()
