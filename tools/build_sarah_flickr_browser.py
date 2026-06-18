#!/usr/bin/env python3
"""Build a static offline browser for Sarah Wang's Flickr archive."""

from __future__ import annotations

import json
import re
import csv
import subprocess
from hashlib import sha1
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path("sarah-flickr")
ASSETS = ROOT / "assets"
THUMBS = ASSETS / "thumbs"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".webm"}
UNKNOWN_YEAR = "Unknown Year"
THUMB_SIZE = 420


def year_sort_key(year: str) -> tuple[int, str]:
    if year.isdigit():
        return (0, year)
    return (1, year)


def js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def display_album_name(name: str) -> tuple[str, str]:
    match = re.match(r"^(\d{4}-\d{2}-\d{2}) - (.+)$", name)
    if not match:
        return name, ""
    date, place = match.groups()
    return date, "" if place == "Unknown Location" else place


def media_type(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "photo"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


def rel_path_from_manifest_target(target: str) -> str | None:
    if not target:
        return None
    parts = Path(target).parts
    for anchor in ("flickrs-organized", ROOT.name):
        if anchor in parts:
            idx = parts.index(anchor)
            return Path(*parts[idx + 1 :]).as_posix()
    try:
        return Path(target).relative_to(ROOT).as_posix()
    except ValueError:
        return None


def load_unknown_date_paths() -> set[str]:
    unknown_paths: set[str] = set()
    for manifest in (ROOT / "manifest.csv", ROOT / "video-manifest.csv"):
        if not manifest.exists():
            continue
        with manifest.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                date_source = (row.get("date_source") or "").strip().lower()
                capture_datetime = (row.get("capture_datetime") or "").strip()
                if date_source != "mtime" and capture_datetime:
                    continue
                rel = rel_path_from_manifest_target(row.get("target", ""))
                if rel:
                    unknown_paths.add(rel)
    return unknown_paths


def thumbnail_path(rel: str) -> Path:
    digest = sha1(rel.encode("utf-8")).hexdigest()[:12]
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(rel).stem).strip("._") or "media"
    year = rel.split("/", 1)[0]
    if not year.isdigit():
        year = "unknown-year"
    return THUMBS / year / f"{safe_stem}-{digest}.jpg"


def thumb_is_current(source: Path, thumb: Path) -> bool:
    return thumb.exists() and thumb.stat().st_mtime >= source.stat().st_mtime and thumb.stat().st_size > 0


def save_image_thumbnail(source: Path, thumb: Path) -> bool:
    if thumb_is_current(source, thumb):
        return True
    thumb.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            width, height = image.size
            side = min(width, height)
            left = max(0, (width - side) // 2)
            top = max(0, (height - side) // 2)
            image = image.crop((left, top, left + side, top + side))
            image = image.resize((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
            image.save(thumb, "JPEG", quality=78, optimize=True, progressive=True)
        return True
    except Exception as exc:
        print(f"thumbnail skipped: {source}: {exc}")
        return False


def save_video_thumbnail(source: Path, thumb: Path) -> bool:
    if thumb_is_current(source, thumb):
        return True
    thumb.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                "00:00:01",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale={THUMB_SIZE}:{THUMB_SIZE}:force_original_aspect_ratio=increase,crop={THUMB_SIZE}:{THUMB_SIZE}",
                "-q:v",
                "5",
                str(thumb),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return True
    except Exception as exc:
        print(f"video thumbnail skipped: {source}: {exc}")
        return False


def ensure_thumbnail(path: Path, rel: str, kind: str) -> str:
    thumb = thumbnail_path(rel)
    ok = save_image_thumbnail(path, thumb) if kind == "photo" else save_video_thumbnail(path, thumb)
    return thumb.relative_to(ROOT).as_posix() if ok else rel


def album_sort_key(album_dir: Path) -> tuple[int, str]:
    if album_dir.name == UNKNOWN_YEAR:
        return (1, album_dir.name)
    return (0, album_dir.name)


def collect_archive() -> dict:
    albums: list[dict] = []
    years: dict[str, dict] = defaultdict(lambda: {"photos": 0, "videos": 0, "albums": 0})
    unknown_date_paths = load_unknown_date_paths()

    for year_dir in sorted(p for p in ROOT.iterdir() if p.is_dir() and p.name != "assets"):
        archive_year = year_dir.name if year_dir.name.isdigit() else UNKNOWN_YEAR
        album_dirs = [p for p in year_dir.iterdir() if p.is_dir()]
        root_media = [p for p in year_dir.iterdir() if p.is_file() and media_type(p)]
        if root_media:
            album_dirs.append(year_dir)
        for album_dir in sorted(album_dirs, key=album_sort_key):
            media = []
            for path in sorted(album_dir.rglob("*")):
                kind = media_type(path)
                if not kind or path.name.startswith("._") or THUMBS in path.parents:
                    continue
                rel = path.relative_to(ROOT).as_posix()
                thumb = ensure_thumbnail(path, rel, kind)
                media.append(
                    {
                        "type": kind,
                        "src": rel,
                        "thumb": thumb,
                        "name": path.name,
                        "size": path.stat().st_size,
                    }
                )
            if not media:
                continue

            title, place = display_album_name(album_dir.name)
            if not year_dir.name.isdigit():
                title = album_dir.name if album_dir != year_dir else UNKNOWN_YEAR
            album_year = archive_year
            if all(item["src"] in unknown_date_paths for item in media):
                album_year = UNKNOWN_YEAR
                title = UNKNOWN_YEAR
                place = ""
            photos = sum(1 for item in media if item["type"] == "photo")
            videos = len(media) - photos
            cover = next((item["thumb"] for item in media if item["type"] == "photo"), media[0]["thumb"])
            album = {
                "id": f"{album_year}-{len(albums)}",
                "year": album_year,
                "title": title,
                "place": place,
                "folder": album_dir.relative_to(ROOT).as_posix(),
                "cover": cover,
                "photos": photos,
                "videos": videos,
                "media": media,
            }
            albums.append(album)
            years[album_year]["photos"] += photos
            years[album_year]["videos"] += videos
            years[album_year]["albums"] += 1

    return {
        "person": {
            "name": "Yaru Sarah Wang",
            "years": "December 25, 1968 - October 27, 2024",
            "background": "assets/memorial-background.png",
        },
        "totals": {
            "albums": len(albums),
            "photos": sum(album["photos"] for album in albums),
            "videos": sum(album["videos"] for album in albums),
        },
        "years": [{"year": year, **data} for year, data in sorted(years.items(), key=lambda item: year_sort_key(item[0]))],
        "albums": albums,
    }


HTML = r"""<!doctype html>
<html class="is-loading" lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Remembering Yaru Sarah Wang</title>
  <style>
    :root {
      --ink: #273028;
      --muted: #667160;
      --line: rgba(64, 78, 64, .16);
      --paper: rgba(255, 253, 247, .88);
      --paper-strong: rgba(255, 253, 247, .96);
      --accent: #8b6f45;
      --accent-soft: #f2e6d2;
      --shadow: 0 18px 60px rgba(66, 57, 42, .14);
      --radius: 8px;
      color-scheme: light;
      font-family: "Avenir Next", Avenir, "Segoe UI", system-ui, -apple-system, sans-serif;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    html.is-loading, html.is-loading *,
    html.is-busy, html.is-busy * { cursor: wait !important; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background: #f7f2e8 url("assets/memorial-background.png") center top / cover fixed no-repeat;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(180deg, rgba(255,255,255,.18), rgba(250,246,238,.92) 620px, #faf7ef 980px);
      z-index: -1;
    }
    a { color: inherit; }
    button, input, select { font: inherit; }
    .shell { width: min(1180px, calc(100% - 32px)); margin: 0 auto; }
    .hero {
      min-height: 560px;
      display: flex;
      align-items: center;
      padding: 64px 0 34px;
    }
    .hero-copy { max-width: 780px; }
    h1 {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      font-size: 92px;
      font-weight: 400;
      letter-spacing: 0;
      line-height: .94;
      color: #263026;
    }
    h1 span { display: block; }
    .name-line { white-space: nowrap; font-size: .92em; }
    .dates { margin: 18px 0 0; font-size: clamp(18px, 2.3vw, 25px); color: #4f5b4d; }
    .intro {
      margin: 26px 0 0;
      max-width: 650px;
      font-size: clamp(18px, 2vw, 22px);
      line-height: 1.55;
      color: #414c3f;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 34px;
      max-width: 600px;
    }
    .stat {
      padding: 16px 18px;
      background: rgba(255,253,247,.72);
      border: 1px solid rgba(255,255,255,.76);
      box-shadow: 0 10px 28px rgba(66,57,42,.08);
    }
    .stat strong { display: block; font-size: 28px; font-weight: 650; }
    .stat span { display: block; margin-top: 4px; color: var(--muted); font-size: 13px; letter-spacing: .08em; text-transform: uppercase; }
    .toolbar {
      position: sticky;
      top: 0;
      z-index: 5;
      backdrop-filter: blur(20px);
      background: rgba(250,247,239,.88);
      border-top: 1px solid rgba(255,255,255,.55);
      border-bottom: 1px solid var(--line);
    }
    .toolbar-inner {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto auto;
      gap: 12px;
      align-items: center;
      padding: 14px 0;
    }
    .search {
      width: 100%;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.72);
      color: var(--ink);
      border-radius: 999px;
      padding: 13px 18px;
      outline: none;
    }
    .search:focus { border-color: rgba(139,111,69,.55); box-shadow: 0 0 0 4px rgba(139,111,69,.11); }
    .select, .view-button, .year-chip {
      border: 1px solid var(--line);
      background: rgba(255,255,255,.7);
      border-radius: 999px;
      min-height: 44px;
      padding: 0 14px;
      color: var(--ink);
    }
    .view-button, .year-chip { cursor: pointer; }
    .view-button.active, .year-chip.active { background: #314033; color: white; border-color: #314033; }
    .year-strip { display: flex; flex-wrap: wrap; gap: 8px; padding: 20px 0 4px; }
    .year-chip { min-height: 38px; font-size: 14px; }
    .section-head { display: flex; justify-content: space-between; align-items: end; gap: 16px; padding: 34px 0 18px; }
    .section-head h2 { margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: 34px; font-weight: 400; }
    .section-head p { margin: 6px 0 0; color: var(--muted); }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; padding-bottom: 56px; }
    .album-card {
      display: grid;
      grid-template-rows: 168px 1fr;
      min-height: 284px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      overflow: hidden;
      background: var(--paper);
      box-shadow: 0 10px 24px rgba(66,57,42,.08);
      cursor: pointer;
      text-align: left;
      padding: 0;
      transition: transform .16s ease, box-shadow .16s ease;
    }
    .album-card:hover { transform: translateY(-2px); box-shadow: 0 16px 38px rgba(66,57,42,.14); }
    .album-cover { width: 100%; height: 100%; object-fit: cover; background: #eee3d2; }
    .album-body { padding: 15px; display: grid; align-content: start; gap: 8px; }
    .album-title { margin: 0; font-size: 18px; font-weight: 640; color: #283429; }
    .album-place, .album-meta { margin: 0; color: var(--muted); font-size: 14px; line-height: 1.35; }
    .empty { display: none; padding: 70px 0 110px; color: var(--muted); text-align: center; }
    .people-panel { display: none; padding-bottom: 70px; }
    .people-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 16px; padding-bottom: 56px; }
    .person-card {
      display: grid;
      grid-template-rows: 188px 1fr;
      min-height: 292px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      overflow: hidden;
      background: var(--paper);
      box-shadow: 0 10px 24px rgba(66,57,42,.08);
      cursor: pointer;
      text-align: left;
      padding: 0;
      transition: transform .16s ease, box-shadow .16s ease;
    }
    .person-card:hover { transform: translateY(-2px); box-shadow: 0 16px 38px rgba(66,57,42,.14); }
    .face-mosaic {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      grid-template-rows: repeat(2, minmax(0, 1fr));
      height: 188px;
      gap: 2px;
      overflow: hidden;
      background: #e8dfd0;
    }
    .face-mosaic img, .person-face { width: 100%; height: 100%; object-fit: cover; display: block; }
    .person-body { padding: 15px; display: grid; align-content: start; gap: 8px; background: var(--paper-strong); }
    .person-title { margin: 0; font-size: 18px; font-weight: 640; color: #283429; }
    .person-meta, .person-note { margin: 0; color: var(--muted); font-size: 14px; line-height: 1.35; }
    .album-view { display: none; padding-bottom: 70px; }
    .album-bar {
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 16px;
      align-items: center;
      padding: 24px 0 18px;
    }
    .back {
      border: 1px solid var(--line);
      background: rgba(255,255,255,.76);
      border-radius: 999px;
      padding: 11px 16px;
      cursor: pointer;
    }
    .album-heading h2 { margin: 0; font-family: Georgia, "Times New Roman", serif; font-weight: 400; font-size: 30px; }
    .album-heading p { margin: 5px 0 0; color: var(--muted); }
    .media-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; }
    .media-tile {
      position: relative;
      display: block;
      aspect-ratio: 1;
      border: 0;
      padding: 0;
      overflow: hidden;
      border-radius: 6px;
      background: #e8dfd0;
      cursor: pointer;
    }
    .media-tile img, .media-tile video { width: 100%; height: 100%; object-fit: cover; display: block; }
    .face-chip {
      position: absolute;
      width: 42px;
      height: 42px;
      right: 7px;
      bottom: 7px;
      border-radius: 999px;
      border: 2px solid rgba(255,255,255,.9);
      box-shadow: 0 4px 12px rgba(0,0,0,.22);
      object-fit: cover;
    }
    .video-mark {
      position: absolute;
      left: 8px;
      bottom: 8px;
      color: white;
      background: rgba(0,0,0,.48);
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 12px;
    }
    .lightbox {
      position: fixed;
      inset: 0;
      display: none;
      grid-template-rows: auto 1fr auto;
      z-index: 20;
      background: #171d18;
      color: white;
    }
    .lightbox.open { display: grid; }
    .light-top, .light-bottom {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 18px;
      background: rgba(17,22,18,.98);
    }
    .light-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: rgba(255,255,255,.88); }
    .light-action {
      border: 1px solid rgba(255,255,255,.22);
      color: white;
      background: rgba(255,255,255,.08);
      border-radius: 999px;
      padding: 10px 14px;
      cursor: pointer;
      text-decoration: none;
    }
    .stage { min-height: 0; display: grid; grid-template-columns: 60px 1fr 60px; align-items: center; }
    .stage-media { min-width: 0; min-height: 0; width: 100%; height: 100%; display: grid; place-items: center; }
    .stage-media img, .stage-media video { max-width: 100%; max-height: 100%; object-fit: contain; box-shadow: 0 24px 80px rgba(0,0,0,.34); }
    .nav-button { height: 100%; border: 0; color: white; background: transparent; cursor: pointer; font-size: 42px; }
    .nav-button:hover { background: rgba(255,255,255,.07); }
    @media (max-width: 760px) {
      body {
        background-position: 28% top;
        background-attachment: scroll;
      }
      body::before {
        background: linear-gradient(180deg, rgba(255,255,255,.54), rgba(250,246,238,.9) 580px, #faf7ef 900px);
      }
      .shell { width: min(100% - 22px, 1180px); }
      .hero { min-height: auto; padding-top: 40px; }
      h1 { font-size: 44px; }
      .stats { grid-template-columns: 1fr 1fr; }
      .toolbar-inner { grid-template-columns: 1fr; }
      .section-head { display: block; }
      .album-bar { grid-template-columns: 1fr; }
      .grid { grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); }
      .media-grid { grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); }
      .stage { grid-template-columns: 44px 1fr 44px; }
      .nav-button { font-size: 32px; }
    }
    @media (max-width: 380px) {
      h1 { font-size: 40px; }
    }
  </style>
</head>
<body>
  <header class="hero shell">
    <div class="hero-copy">
      <h1><span>Remembering</span><span class="name-line">Yaru Sarah Wang</span></h1>
      <p class="dates">December 25, 1968 - October 27, 2024</p>
      <p class="intro">This archive gathers Sarah's Flickr photographs and videos into a simple place for family and friends to browse by year, date, and memory. May these images keep close the warmth, curiosity, care, and everyday joy she shared.</p>
      <div class="stats" aria-label="Archive summary">
        <div class="stat"><strong id="photoTotal">0</strong><span>Photos</span></div>
        <div class="stat"><strong id="videoTotal">0</strong><span>Videos</span></div>
        <div class="stat"><strong id="albumTotal">0</strong><span>Albums</span></div>
      </div>
    </div>
  </header>

  <nav class="toolbar">
    <div class="toolbar-inner shell">
      <input id="search" class="search" type="search" placeholder="Search dates, places, or filenames" autocomplete="off">
      <select id="sort" class="select" aria-label="Sort albums">
        <option value="oldest">Oldest first</option>
        <option value="newest">Newest first</option>
        <option value="largest">Largest albums first</option>
      </select>
      <button id="showAlbums" class="view-button active" type="button">Albums</button>
      <button id="showPeople" class="view-button" type="button">People</button>
    </div>
  </nav>

  <main class="shell">
    <div id="yearStrip" class="year-strip" aria-label="Filter by year"></div>
    <section id="albumsPanel">
      <div class="section-head">
        <div>
          <h2>Photo Albums</h2>
          <p id="resultSummary">Loading archive...</p>
        </div>
      </div>
      <div id="albumGrid" class="grid"></div>
      <div id="empty" class="empty">No albums match this search.</div>
    </section>
    <section id="albumView" class="album-view" aria-live="polite">
      <div class="album-bar">
        <button id="backToAlbums" class="back" type="button">Back to albums</button>
        <div class="album-heading">
          <h2 id="albumTitle"></h2>
          <p id="albumMeta"></p>
        </div>
        <a id="albumFolderLink" class="back" href="#">Open folder</a>
      </div>
      <div id="mediaGrid" class="media-grid"></div>
    </section>
    <section id="peoplePanel" class="people-panel" aria-live="polite">
      <div class="section-head">
        <div>
          <h2>People</h2>
          <p id="peopleSummary">Loading people index...</p>
        </div>
      </div>
      <div id="peopleGrid" class="people-grid"></div>
      <div id="peopleEmpty" class="empty">No people index has been generated yet.</div>
    </section>
    <section id="personView" class="album-view" aria-live="polite">
      <div class="album-bar">
        <button id="backToPeople" class="back" type="button">Back to people</button>
        <div class="album-heading">
          <h2 id="personTitle"></h2>
          <p id="personMeta"></p>
        </div>
      </div>
      <div id="personMediaGrid" class="media-grid"></div>
    </section>
  </main>

  <aside id="lightbox" class="lightbox" aria-modal="true" role="dialog">
    <div class="light-top">
      <div id="lightTitle" class="light-title"></div>
      <button id="closeLightbox" class="light-action" type="button">Close</button>
    </div>
    <div class="stage">
      <button id="prevMedia" class="nav-button" type="button" aria-label="Previous media">&#8249;</button>
      <div id="stageMedia" class="stage-media"></div>
      <button id="nextMedia" class="nav-button" type="button" aria-label="Next media">&#8250;</button>
    </div>
    <div class="light-bottom">
      <span id="lightCount"></span>
      <a id="openOriginal" class="light-action" href="#" target="_blank" rel="noopener">Open original file</a>
    </div>
  </aside>

  <script src="assets/archive-data.js?v=__ARCHIVE_DATA_VERSION__"></script>
  <script src="assets/people-data.js?v=__PEOPLE_DATA_VERSION__"></script>
  <script>
    const archive = window.SARAH_ARCHIVE;
    const peopleIndex = window.SARAH_PEOPLE_INDEX || { metadata: {}, people: [] };
    const state = { year: "all", query: "", sort: "oldest", album: null, person: null, lightboxMedia: [], index: 0 };
    const $ = (id) => document.getElementById(id);

    const fmt = new Intl.NumberFormat();
    $("photoTotal").textContent = fmt.format(archive.totals.photos);
    $("videoTotal").textContent = fmt.format(archive.totals.videos);
    $("albumTotal").textContent = fmt.format(archive.totals.albums);

    function mediaCount(album) {
      const parts = [`${fmt.format(album.photos)} photos`];
      if (album.videos) parts.push(`${fmt.format(album.videos)} videos`);
      return parts.join(" · ");
    }

    function escapeHtml(value) {
      return String(value || "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function searchable(album) {
      return [album.year, album.title, album.place, album.folder, ...album.media.map((m) => m.name)]
        .join(" ")
        .toLowerCase();
    }

    function filteredAlbums() {
      let albums = archive.albums.filter((album) => state.year === "all" || album.year === state.year);
      if (state.query.trim()) {
        const query = state.query.trim().toLowerCase();
        albums = albums.filter((album) => searchable(album).includes(query));
      }
      albums = albums.slice();
      if (state.sort === "newest") albums.reverse();
      if (state.sort === "largest") albums.sort((a, b) => (b.photos + b.videos) - (a.photos + a.videos));
      return albums;
    }

    function renderYears() {
      const strip = $("yearStrip");
      strip.replaceChildren();
      const all = document.createElement("button");
      all.className = `year-chip ${state.year === "all" ? "active" : ""}`;
      all.type = "button";
      all.textContent = "All years";
      all.addEventListener("click", () => selectYear("all"));
      strip.appendChild(all);
      archive.years.forEach((year) => {
        const button = document.createElement("button");
        button.className = `year-chip ${state.year === year.year ? "active" : ""}`;
        button.type = "button";
        button.textContent = `${year.year} (${fmt.format(year.photos + year.videos)})`;
        button.addEventListener("click", () => selectYear(year.year));
        strip.appendChild(button);
      });
    }

    function renderAlbums() {
      const grid = $("albumGrid");
      const albums = filteredAlbums();
      grid.replaceChildren();
      $("resultSummary").textContent = `${fmt.format(albums.length)} albums shown`;
      $("empty").style.display = albums.length ? "none" : "block";
      albums.forEach((album) => {
        const card = document.createElement("button");
        card.className = "album-card";
        card.type = "button";
        card.innerHTML = `
          <img class="album-cover" loading="lazy" src="${album.cover}" alt="">
          <span class="album-body">
            <span class="album-title">${album.title}</span>
            <span class="album-place">${album.place || "Location not recorded"}</span>
            <span class="album-meta">${album.year} · ${mediaCount(album)}</span>
          </span>`;
        card.addEventListener("click", () => openAlbum(album.id));
        grid.appendChild(card);
      });
    }

    function filteredPeople() {
      const people = (peopleIndex.people || []).slice();
      const query = state.query.trim().toLowerCase();
      if (!query) return people;
      return people.filter((person) => {
        const text = [
          person.label,
          ...person.media.map((item) => [item.name, item.albumTitle, item.albumYear, item.albumPlace].join(" "))
        ].join(" ").toLowerCase();
        return text.includes(query);
      });
    }

    function renderPeople() {
      const grid = $("peopleGrid");
      const people = filteredPeople();
      grid.replaceChildren();
      const metadata = peopleIndex.metadata || {};
      if (peopleIndex.people && peopleIndex.people.length) {
        $("peopleSummary").textContent = `${fmt.format(people.length)} groups shown · ${fmt.format(metadata.detectedFaces || 0)} detected faces`;
      } else {
        $("peopleSummary").textContent = "Run the people index builder to detect and group faces.";
      }
      $("peopleEmpty").style.display = people.length ? "none" : "block";
      people.forEach((person) => {
        const card = document.createElement("button");
        card.className = "person-card";
        card.type = "button";
        const samples = (person.samples || []).slice(0, 4);
        const sampleImages = samples.length
          ? samples.map((sample) => `<img class="person-face" loading="lazy" src="${sample.face}" alt="">`).join("")
          : `<img class="person-face" loading="lazy" src="${person.cover}" alt="">`;
        const sourceGroups = person.sourceGroups || [];
        const note = sourceGroups.length > 1 ? `Merged ${fmt.format(sourceGroups.length)} groups` : "Automatic group, review recommended";
        card.innerHTML = `
          <span class="face-mosaic">${sampleImages}</span>
          <span class="person-body">
            <span class="person-title">${escapeHtml(person.label)}</span>
            <span class="person-meta">${fmt.format(person.photoCount)} photos · ${fmt.format(person.faceCount)} faces</span>
            <span class="person-note">${escapeHtml(note)}</span>
          </span>`;
        card.addEventListener("click", () => openPerson(person.id));
        grid.appendChild(card);
      });
    }

    function render() {
      renderYears();
      renderAlbums();
    }

    function setBusy(isBusy) {
      document.documentElement.classList.toggle("is-busy", isBusy);
    }

    function waitForImages(selector, limit) {
      const images = Array.from(document.querySelectorAll(selector)).slice(0, limit);
      if (!images.length) return Promise.resolve();
      const loaders = images.map((img) => {
        if (img.complete) return Promise.resolve();
        return new Promise((resolve) => {
          img.addEventListener("load", resolve, { once: true });
          img.addEventListener("error", resolve, { once: true });
        });
      });
      return Promise.race([
        Promise.all(loaders),
        new Promise((resolve) => window.setTimeout(resolve, 1200))
      ]);
    }

    function waitForVisibleAlbumCovers() {
      return waitForImages(".album-cover", 12);
    }

    function waitForVisibleMediaTiles() {
      return waitForImages(".media-tile img", 18);
    }

    function waitForVisiblePeople() {
      return waitForImages(".person-face", 16);
    }

    function setActiveView(view) {
      $("showAlbums").classList.toggle("active", view === "albums");
      $("showPeople").classList.toggle("active", view === "people");
      $("yearStrip").style.display = view === "people" ? "none" : "flex";
    }

    function showAlbumList() {
      state.album = null;
      state.person = null;
      $("albumView").style.display = "none";
      $("peoplePanel").style.display = "none";
      $("personView").style.display = "none";
      $("albumsPanel").style.display = "block";
      setActiveView("albums");
    }

    function showPeopleList() {
      setBusy(true);
      window.requestAnimationFrame(() => {
        state.album = null;
        state.person = null;
        renderPeople();
        $("albumsPanel").style.display = "none";
        $("albumView").style.display = "none";
        $("personView").style.display = "none";
        $("peoplePanel").style.display = "block";
        setActiveView("people");
        $("peoplePanel").scrollIntoView({ behavior: "smooth", block: "start" });
        waitForVisiblePeople().then(() => setBusy(false));
      });
    }

    function selectYear(year) {
      if (state.year === year && !state.album) {
        $("albumsPanel").scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      setBusy(true);
      state.year = year;
      window.requestAnimationFrame(() => {
        render();
        showAlbumList();
        $("albumsPanel").scrollIntoView({ behavior: "smooth", block: "start" });
        waitForVisibleAlbumCovers().then(() => setBusy(false));
      });
    }

    function openAlbum(id) {
      const album = archive.albums.find((item) => item.id === id);
      if (!album) return;
      setBusy(true);
      state.album = album;
      state.person = null;
      $("albumsPanel").style.display = "none";
      $("peoplePanel").style.display = "none";
      $("personView").style.display = "none";
      $("albumView").style.display = "block";
      setActiveView("albums");
      $("albumTitle").textContent = album.title;
      $("albumMeta").textContent = `${album.place || "Location not recorded"} · ${mediaCount(album)}`;
      $("albumFolderLink").href = album.folder + "/";
      renderMedia(album.media, $("mediaGrid"));
      window.scrollTo({ top: $("albumView").offsetTop - 74, behavior: "smooth" });
      waitForVisibleMediaTiles().then(() => setBusy(false));
    }

    function openPerson(id) {
      const person = (peopleIndex.people || []).find((item) => item.id === id);
      if (!person) return;
      setBusy(true);
      state.album = null;
      state.person = person;
      $("albumsPanel").style.display = "none";
      $("albumView").style.display = "none";
      $("peoplePanel").style.display = "none";
      $("personView").style.display = "block";
      setActiveView("people");
      $("personTitle").textContent = person.label;
      $("personMeta").textContent = `${fmt.format(person.photoCount)} photos · ${fmt.format(person.faceCount)} detected faces`;
      renderMedia(person.media, $("personMediaGrid"), true);
      window.scrollTo({ top: $("personView").offsetTop - 74, behavior: "smooth" });
      waitForVisibleMediaTiles().then(() => setBusy(false));
    }

    function renderMedia(mediaItems, grid, showFaceChip = false) {
      grid.replaceChildren();
      mediaItems.forEach((item, index) => {
        const button = document.createElement("button");
        button.className = "media-tile";
        button.type = "button";
        const faceChip = showFaceChip && item.face ? `<img class="face-chip" loading="lazy" src="${item.face}" alt="">` : "";
        if (item.type === "photo") {
          button.innerHTML = `<img loading="lazy" src="${item.thumb}" alt="${escapeHtml(item.name)}">${faceChip}`;
        } else {
          button.innerHTML = `<img loading="lazy" src="${item.thumb}" alt="${escapeHtml(item.name)}"><span class="video-mark">Video</span>${faceChip}`;
        }
        button.addEventListener("click", () => openLightbox(index, mediaItems));
        grid.appendChild(button);
      });
    }

    function closeAlbum() {
      showAlbumList();
      window.scrollTo({ top: $("albumsPanel").offsetTop - 74, behavior: "smooth" });
    }

    function openLightbox(index, mediaItems) {
      state.index = index;
      state.lightboxMedia = mediaItems || [];
      $("lightbox").classList.add("open");
      renderLightbox();
    }

    function closeLightbox() {
      $("lightbox").classList.remove("open");
      $("stageMedia").replaceChildren();
      state.lightboxMedia = [];
    }

    function move(delta) {
      if (!state.lightboxMedia.length) return;
      state.index = (state.index + delta + state.lightboxMedia.length) % state.lightboxMedia.length;
      renderLightbox();
    }

    function renderLightbox() {
      const item = state.lightboxMedia[state.index];
      if (!item) return;
      $("lightTitle").textContent = item.name;
      $("lightCount").textContent = `${fmt.format(state.index + 1)} of ${fmt.format(state.lightboxMedia.length)}`;
      $("openOriginal").href = item.src;
      const stage = $("stageMedia");
      stage.replaceChildren();
      const media = document.createElement(item.type === "photo" ? "img" : "video");
      media.src = item.src;
      if (item.type === "photo") {
        media.alt = item.name;
      } else {
        media.controls = true;
        media.autoplay = true;
      }
      stage.appendChild(media);
    }

    $("search").addEventListener("input", (event) => {
      state.query = event.target.value;
      if ($("peoplePanel").style.display === "block") {
        renderPeople();
      } else {
        renderAlbums();
      }
    });
    $("sort").addEventListener("change", (event) => { state.sort = event.target.value; renderAlbums(); });
    $("showAlbums").addEventListener("click", () => {
      setBusy(true);
      window.requestAnimationFrame(() => {
        renderAlbums();
        showAlbumList();
        $("albumsPanel").scrollIntoView({ behavior: "smooth", block: "start" });
        waitForVisibleAlbumCovers().then(() => setBusy(false));
      });
    });
    $("showPeople").addEventListener("click", showPeopleList);
    $("backToAlbums").addEventListener("click", closeAlbum);
    $("backToPeople").addEventListener("click", showPeopleList);
    $("closeLightbox").addEventListener("click", closeLightbox);
    $("prevMedia").addEventListener("click", () => move(-1));
    $("nextMedia").addEventListener("click", () => move(1));
    document.addEventListener("keydown", (event) => {
      if (!$("lightbox").classList.contains("open")) return;
      if (event.key === "Escape") closeLightbox();
      if (event.key === "ArrowLeft") move(-1);
      if (event.key === "ArrowRight") move(1);
    });

    function clearLoadingCursor() {
      document.documentElement.classList.remove("is-loading");
    }

    window.addEventListener("load", clearLoadingCursor, { once: true });
    window.setTimeout(clearLoadingCursor, 4000);

    render();
  </script>
</body>
</html>
"""


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    archive = collect_archive()
    archive_data = ASSETS / "archive-data.js"
    archive_data.write_text(
        "window.SARAH_ARCHIVE = " + json.dumps(archive, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    people_data = ASSETS / "people-data.js"
    if not people_data.exists():
        people_data.write_text(
            'window.SARAH_PEOPLE_INDEX = {"metadata":{"indexedPeople":0,"detectedFaces":0,"photosWithFaces":0},"people":[]};\n',
            encoding="utf-8",
        )
    html = (
        HTML.replace("__ARCHIVE_DATA_VERSION__", str(int(archive_data.stat().st_mtime)))
        .replace("__PEOPLE_DATA_VERSION__", str(int(people_data.stat().st_mtime)))
    )
    (ROOT / "index.html").write_text(html, encoding="utf-8")
    print(
        f"Wrote {ROOT / 'index.html'} with "
        f"{archive['totals']['albums']} albums, "
        f"{archive['totals']['photos']} photos, "
        f"{archive['totals']['videos']} videos."
    )


if __name__ == "__main__":
    main()
