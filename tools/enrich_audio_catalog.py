#!/usr/bin/env python3
import argparse
import csv
import os
import re
import shutil
from collections import Counter
from pathlib import Path


APPEND_FIELDS = [
    "inferred_title",
    "inferred_artist",
    "inferred_album",
    "collection",
    "media_category",
    "style",
    "style_source",
    "style_confidence",
    "duration_minutes",
    "duration_bucket",
    "duplicate_group_count",
    "needs_review",
    "catalog_notes",
]


CHINESE_ARCHIVE_STYLES = {
    "Beijing_Opera": "Beijing opera",
    "Ceremonial_Music": "Chinese ceremonial / patriotic march",
    "Children": "children's music",
    "Culture_Revolution": "Chinese revolutionary song",
    "Current_Hits": "Mandopop / Chinese contemporary hit",
    "Deng_LiJun": "Mandopop / Teresa Teng",
    "Dream_Of_Red_Mansion": "Chinese TV soundtrack / classical-influenced vocal",
    "East_Is_Red": "Chinese revolutionary song / stage musical",
    "Educated_Youth": "Chinese revolutionary / educated-youth song",
    "Folk_Music": "Chinese folk",
    "Foreign_Origin": "translated / foreign-origin popular song",
    "Historical_Voices": "historical speech / spoken-word recording",
    "How_To_Play_Audio": "technical audio instruction",
    "Long_March": "Chinese revolutionary song",
    "Modern_Pops": "Mandopop / Chinese pop",
    "Northwestern_Wind": "Chinese northwest wind pop",
    "Post_Gang_Of_Four": "post-Cultural-Revolution Chinese popular song",
    "Post_Liberation": "post-liberation Chinese song",
    "Pre_Liberation": "pre-liberation Chinese song",
    "Taiwan_Ceremonial": "Taiwan ceremonial / patriotic song",
    "Taiwan_HongKong": "Taiwan / Hong Kong pop",
    "Traditional_Music": "traditional Chinese music",
}


WESTERN_80S_STYLES = {
    "take on me": "1980s synth-pop / new wave",
    "billie jean": "1980s pop / dance-pop",
    "don't stop believin": "arena rock / classic rock",
    "never gonna give you up": "1980s dance-pop / blue-eyed soul",
    "i wanna dance with somebody": "1980s dance-pop / R&B-pop",
    "like a virgin": "1980s dance-pop",
    "brother louie": "Eurodance / dance-pop",
    "simple gifts": "American classical / orchestral",
    "appalachian spring": "American classical / orchestral",
}


CLASSICAL_HINTS = {
    "beethoven",
    "mozart",
    "tchaikovsky",
    "dvorak",
    "dvorak",
    "chopin",
    "smetana",
    "borodin",
    "delius",
    "mendelssohn",
    "albinoni",
    "vivaldi",
    "copland",
    "concerto",
    "symphony",
    "requiem",
    "overture",
    "sonata",
    "nocturne",
    "adagio",
    "suite",
}


SPOKEN_WORD_HINTS = {
    "audio training",
    "audiobook",
    "audio book",
    "soundbook",
    "library",
    "mental toughness",
    "rich dad",
    "guide to investing",
    "john gary",
    "jay abraham",
    "chapter",
    "track ",
}


SYSTEM_SOUND_HINTS = {
    "/program files/",
    "/windows/media/",
    "/resources/",
    "/support files/",
    "/cyberlink/",
    "/filezilla",
    "favsound",
    "click.wav",
    "silent.wav",
    "silence",
    "notify",
    "startup",
    "shutdown",
    "/sounds/",
    "/themes/sounds/",
    "/required/sounds/",
    "doorbell",
    "drip",
    "thud",
    "whoosh",
    "gong",
    "newmail",
    "imsend",
    "imrcv",
    "welcome.wma",
    "bark.aiff",
    "countdown timer",
    "clock ticking",
}


def clean_cell(value: str | None) -> str:
    value = (value or "").strip()
    return re.sub(r"\s+", " ", value)


def parse_duration(row: dict) -> float | None:
    try:
        return float(row.get("duration_seconds") or "")
    except ValueError:
        return None


def duration_bucket(duration: float | None) -> str:
    if duration is None:
        return "unknown"
    if duration < 10:
        return "<10s"
    if duration < 60:
        return "10s-1m"
    if duration < 180:
        return "1-3m"
    if duration < 420:
        return "3-7m"
    if duration < 1200:
        return "7-20m"
    if duration < 3600:
        return "20-60m"
    return "60m+"


def strip_video_noise(name: str) -> str:
    stem = re.sub(r"\[[A-Za-z0-9_-]{6,}\]", "", name)
    stem = re.sub(r"\.(avi|flv|mp4|wmv|mov)$", "", stem, flags=re.I)
    stem = re.sub(
        r"\b(official|video|mv|lyrics?|lyric|karaoke|ktv|hd|full|完整版|官方|歌詞版|字幕|高音質|動態歌詞版|remaster|4k|1080p)\b",
        "",
        stem,
        flags=re.I,
    )
    stem = re.sub(r"[()（）【】《》「」]+", " ", stem)
    return clean_cell(stem.strip(" -_.,"))


def infer_title_artist(path: str, row: dict) -> tuple[str, str, str]:
    existing_title = clean_cell(row.get("title"))
    existing_artist = clean_cell(row.get("artist"))
    existing_album = clean_cell(row.get("album"))
    stem = Path(path).stem

    title = existing_title
    artist = existing_artist
    album = existing_album

    cleaned = strip_video_noise(stem)
    cleaned = re.sub(r"^\d+\s*[-._]\s*", "", cleaned)

    if not title:
        title = cleaned or stem

    if not artist:
        separators = [" - ", " – ", " — "]
        for sep in separators:
            if sep in cleaned:
                left, right = cleaned.split(sep, 1)
                if 1 < len(left) <= 80 and 1 < len(right) <= 160:
                    artist = clean_cell(left)
                    title = clean_cell(right)
                    break

    if not album:
        parts = Path(path).parts
        if len(parts) >= 2:
            album = parts[-2]

    return title, artist, album


def collection_for(path: str) -> str:
    parts = Path(path).parts
    archive_idx = next((i for i, part in enumerate(parts) if part.lower() == "chinese-music-archive"), -1)
    if archive_idx >= 0:
        idx = archive_idx
        if idx + 1 < len(parts):
            if parts[idx + 1].lower() == "chinese-music" and idx + 2 < len(parts):
                return f"Chinese-Music-Archive/{parts[idx + 2]}"
            return f"Chinese-Music-Archive/{parts[idx + 1]}"
    for marker in ("Chinese-Pop-Songs", "class8731-reunion-songs", "classic-music", "bgm", "80s-music"):
        if marker in parts:
            return marker
    if "books" in parts:
        idx = parts.index("books")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    if len(parts) >= 2:
        return parts[-2]
    return ""


def style_from_path(path: str, row: dict, inferred_title: str) -> tuple[str, str, str, str, str]:
    lower_path = path.lower()
    lower_title = inferred_title.lower()
    duration = parse_duration(row)
    ext = (row.get("extension") or "").lower()
    notes: list[str] = []

    if "/chinese-music-archive/" in lower_path:
        parts = Path(path).parts
        idx = next((i for i, part in enumerate(parts) if part.lower() == "chinese-music-archive"), -1)
        section = parts[idx + 1] if idx >= 0 and idx + 1 < len(parts) else ""
        if section.lower() == "chinese-music" and idx + 2 < len(parts):
            section = parts[idx + 2]
        style = CHINESE_ARCHIVE_STYLES.get(section)
        if section == "MP3":
            style = "traditional Chinese instrumental / archive track"
        if style:
            media = "music" if "spoken" not in style and "instruction" not in style else "spoken_word"
            return media, style, "archive folder", "high", ""

    if "/chinese-pop-songs/" in lower_path:
        return "music", "Mandopop / Chinese pop ballad", "folder", "high", ""

    if "/class8731-reunion-songs/" in lower_path:
        return "music", "Chinese reunion / nostalgic pop", "folder", "high", ""

    if "/classic-music/" in lower_path:
        return "music", "Western classical / orchestral", "folder", "high", ""

    if "/bgm/" in lower_path:
        if any(hint in lower_title for hint in CLASSICAL_HINTS):
            return "music", "Western classical / instrumental background", "filename", "high", ""
        if "birthday" in lower_title:
            return "music", "birthday song / party music", "filename", "high", ""
        if "sweet dreams" in lower_title:
            return "music", "1980s synth-pop / new wave", "filename", "high", ""
        return "music", "background music / instrumental", "folder", "medium", "BGM folder; style is broad"

    if "/80s-music/" in lower_path:
        for hint, style in WESTERN_80S_STYLES.items():
            if hint in lower_title or hint in lower_path:
                return "music", style, "known title", "high", ""
        if re.search(r"明天|一无所有|冬季|乡恋|年轻的朋友|校园|甜蜜蜜|难忘今宵|金梭", path):
            return "music", "1980s Chinese pop / nostalgic Chinese song", "folder and filename", "high", ""
        return "music", "1980s pop / party playlist", "folder", "medium", "80s-music folder; exact subgenre is broad"

    for hint, style in WESTERN_80S_STYLES.items():
        if hint in lower_title or hint in lower_path:
            return "music", style, "known title", "high", ""

    if "/lu/" in lower_path:
        return "music", "Chinese classic vocal / folk-pop", "folder", "high", ""

    if any(
        token in path
        for token in (
            "一无所有",
            "甜蜜蜜",
            "冬天裡的一把火",
            "年轻的朋友",
            "大约在冬季",
            "童年",
            "光陰的故事",
            "我记得你眼里的依恋",
            "南方二重唱",
            "贝加尔湖畔",
            "鄧麗君",
            "羅大佑",
            "张艾嘉",
        )
    ):
        return "music", "Chinese pop / nostalgic Chinese song", "filename", "high", ""

    if "舞蹈" in path or "dance" in lower_title:
        return "music", "dance music", "filename", "medium", ""

    if "dragon-boat-race" in lower_path or "dashcam" in lower_path:
        return "field_recording", "field recording / ambient audio", "folder", "medium", ""

    if "/multimedia/music/" in lower_path:
        if "qantas" in lower_title or "qantas" in lower_path:
            return "music", "TV advertisement jingle / orchestral pop", "filename", "medium", ""
        if "澳广" in path:
            return "spoken_word", "radio program recording", "filename", "high", ""
        return "music", "music / uncategorized", "music folder", "low", "Music folder but no stronger style hint"

    if "/multimedia/books/" in lower_path or any(h in lower_path for h in SPOKEN_WORD_HINTS):
        return "spoken_word", "audiobook / spoken-word training", "path and title", "high", ""

    if "/archive/download-160/" in lower_path or "/multimedia/movies/" in lower_path:
        return "longform_audio", "film / long-form video audio", "folder and duration", "high", ""

    if "/wfg/" in lower_path or "/documents/training/" in lower_path or "/story_content/" in lower_path:
        return "spoken_word", "business training / narrated course audio", "path", "high", ""

    if any(h in lower_path for h in SYSTEM_SOUND_HINTS):
        return "sound_effect", "system/application sound effect", "path", "high", ""

    if duration is not None and duration < 5 and ("/tests/" in lower_path or "test" in lower_path):
        return "sound_effect", "test sound effect / sample", "path and duration", "medium", ""

    if ext == ".3gp" and ("/photos/" in lower_path or "/backup/" in lower_path):
        return "video_audio", "camera video clip audio", "path and format", "medium", "Audio stream from a 3GP video"

    if "/video/" in lower_path and duration is not None:
        if duration >= 1800:
            return "longform_audio", "video project / DVD audio", "path and duration", "medium", ""
        return "video_audio", "video project audio / clip", "path", "medium", ""

    if duration is not None and duration < 10 and ext in {".wav", ".au", ".aiff", ".snd"}:
        return "sound_effect", "short sound effect", "duration and format", "medium", "Very short untagged audio"

    if duration is not None and duration >= 1800 and (
        "track" in lower_title or ext == ".mp3" or ext in {".rm", ".ra"}
    ):
        return "spoken_word", "spoken-word / long-form audio", "duration and metadata", "medium", "Long track; review if this is actually music"

    if any(hint in lower_title for hint in CLASSICAL_HINTS):
        return "music", "Western classical", "filename", "high", ""

    if any(hint in lower_path for hint in CLASSICAL_HINTS):
        return "music", "Western classical", "filename", "high", ""

    if duration is not None and 90 <= duration <= 600 and ext in {".mp3", ".m4a", ".aac", ".wma"}:
        if re.search(r"[\u3400-\u9fff]", path):
            return "music", "Chinese song / style unspecified", "filename", "medium", ""
        notes.append("Song-length compressed audio but no specific style hint")
        return "music", "music / style unknown", "duration and format", "low", "; ".join(notes)

    return "unknown_audio", "unknown", "insufficient metadata", "low", "Review manually"


def enrich_row(row: dict, duplicate_counts: Counter) -> dict:
    path = row.get("path") or ""
    duration = parse_duration(row)
    title, artist, album = infer_title_artist(path, row)
    media, style, source, confidence, notes = style_from_path(path, row, title)
    duplicate_key = (
        row.get("size_bytes") or "",
        round(duration or -1, 2),
        Path(path).name.lower(),
    )
    duplicate_count = duplicate_counts[duplicate_key]
    review = "yes" if confidence == "low" or media == "unknown_audio" else "no"

    enriched = dict(row)
    enriched.update(
        {
            "inferred_title": title,
            "inferred_artist": artist,
            "inferred_album": album,
            "collection": collection_for(path),
            "media_category": media,
            "style": style,
            "style_source": source,
            "style_confidence": confidence,
            "duration_minutes": f"{duration / 60:.2f}" if duration is not None else "",
            "duration_bucket": duration_bucket(duration),
            "duplicate_group_count": str(duplicate_count),
            "needs_review": review,
            "catalog_notes": notes,
        }
    )
    return enriched


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row")
        return list(reader.fieldnames), rows


def duplicate_counts(rows: list[dict]) -> Counter:
    counts: Counter = Counter()
    for row in rows:
        duration = parse_duration(row)
        key = (
            row.get("size_bytes") or "",
            round(duration or -1, 2),
            Path(row.get("path") or "").name.lower(),
        )
        counts[key] += 1
    return counts


def write_rows(output: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Append practical style and catalog enrichment columns.")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--backup", action="store_true", help="Back up the input before replacing it.")
    args = parser.parse_args()

    base_fields, rows = read_rows(args.csv_path)
    counts = duplicate_counts(rows)
    fieldnames = [field for field in base_fields if field not in APPEND_FIELDS] + APPEND_FIELDS
    enriched_rows = [enrich_row(row, counts) for row in rows]

    output = args.output or args.csv_path
    temp_output = output.with_suffix(output.suffix + ".tmp")
    write_rows(temp_output, fieldnames, enriched_rows)

    if args.backup and output == args.csv_path:
        backup_path = args.csv_path.with_suffix(args.csv_path.suffix + ".bak")
        if not backup_path.exists():
            shutil.copy2(args.csv_path, backup_path)

    os.replace(temp_output, output)

    by_category = Counter(row["media_category"] for row in enriched_rows)
    by_confidence = Counter(row["style_confidence"] for row in enriched_rows)
    print(f"Wrote {len(enriched_rows)} rows to {output}")
    print("media_category:", ", ".join(f"{k}={v}" for k, v in by_category.most_common()))
    print("style_confidence:", ", ".join(f"{k}={v}" for k, v in by_confidence.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
