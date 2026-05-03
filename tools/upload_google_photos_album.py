#!/usr/bin/env python3
"""Upload local images into a new Google Photos album.

The script uses the existing gog OAuth credential store. The account/client must
already have the Google Photos Library API append-only scope.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import requests


HOME = Path.home()
GOG = os.environ.get("GOG_BIN", str(HOME / ".openclaw/bin/gog"))
TOKEN_URL = "https://oauth2.googleapis.com/token"
ALBUMS_URL = "https://photoslibrary.googleapis.com/v1/albums"
UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
BATCH_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"

PHOTO_EXTS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".ico",
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


def load_env_file(path: Path) -> dict[str, str]:
    env = os.environ.copy()
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip("'\""))
    except FileNotFoundError:
        pass
    return env


def load_credentials(client: str) -> tuple[str, str]:
    client_path = HOME / f".config/gogcli/credentials-{client}.json"
    default_path = HOME / ".config/gogcli/credentials.json"
    path = client_path if client_path.exists() else default_path
    data = json.loads(path.read_text())
    return data["client_id"], data["client_secret"]


def export_refresh_token(account: str, client: str) -> str:
    env = load_env_file(HOME / ".config/gogcli/keyring.env")
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        token_path = Path(handle.name)
    try:
        subprocess.run(
            [
                GOG,
                "auth",
                "tokens",
                "export",
                account,
                "--client",
                client,
                "--out",
                str(token_path),
                "--overwrite",
                "--no-input",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return json.loads(token_path.read_text())["refresh_token"]
    finally:
        token_path.unlink(missing_ok=True)


class TokenProvider:
    def __init__(self, account: str, client: str) -> None:
        self.account = account
        self.client = client
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            client_id, client_secret = load_credentials(self.client)
            refresh_token = export_refresh_token(self.account, self.client)
            response = requests.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"token refresh failed: {response.status_code} {response.text}")
            data = response.json()
            self._token = data["access_token"]
            self._expires_at = time.time() + max(60, int(data.get("expires_in", 3600)) - 120)
            return self._token


def request_json(
    method: str,
    url: str,
    token_provider: TokenProvider,
    *,
    retries: int = 5,
    **kwargs: Any,
) -> dict[str, Any]:
    delay = 2.0
    for attempt in range(retries + 1):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token_provider.token()}"
        response = requests.request(method, url, headers=headers, timeout=60, **kwargs)
        if response.status_code < 400:
            return response.json() if response.content else {}
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == retries:
            raise RuntimeError(f"{method} {url} failed: {response.status_code} {response.text}")
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise AssertionError("unreachable")


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_manifest(path: Path, album_title: str, source_dir: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "album_title": album_title,
        "album_id": "",
        "source_dir": str(source_dir),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "items": {},
    }


def iter_photos(source_dir: Path, recursive: bool) -> list[Path]:
    walker = source_dir.rglob("*") if recursive else source_dir.glob("*")
    photos = [path for path in walker if path.is_file() and path.suffix.lower() in PHOTO_EXTS]
    return sorted(photos, key=lambda path: (path.stat().st_mtime, path.name.lower()))


def create_album(title: str, token_provider: TokenProvider) -> str:
    data = request_json("POST", ALBUMS_URL, token_provider, json={"album": {"title": title}})
    album_id = data.get("id")
    if not album_id:
        raise RuntimeError(f"album create response did not include an id: {data}")
    return album_id


def upload_one(path: Path, token_provider: TokenProvider) -> tuple[str, str]:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Content-type": "application/octet-stream",
        "X-Goog-Upload-Content-Type": content_type,
        "X-Goog-Upload-Protocol": "raw",
    }
    delay = 2.0
    for attempt in range(6):
        headers["Authorization"] = f"Bearer {token_provider.token()}"
        with path.open("rb") as handle:
            response = requests.post(UPLOAD_URL, headers=headers, data=handle, timeout=(30, 600))
        if response.status_code < 400:
            return str(path), response.text
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 5:
            raise RuntimeError(f"upload failed for {path}: {response.status_code} {response.text}")
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise AssertionError("unreachable")


def commit_batch(
    album_id: str,
    records: list[tuple[Path, str]],
    token_provider: TokenProvider,
) -> dict[str, dict[str, Any]]:
    body = {
        "albumId": album_id,
        "newMediaItems": [
            {"simpleMediaItem": {"fileName": path.name, "uploadToken": upload_token}}
            for path, upload_token in records
        ],
    }
    data = request_json("POST", BATCH_CREATE_URL, token_provider, json=body)
    results = data.get("newMediaItemResults", [])
    if len(results) != len(records):
        raise RuntimeError(f"batchCreate returned {len(results)} results for {len(records)} uploads")
    committed: dict[str, dict[str, Any]] = {}
    for (path, upload_token), result in zip(records, results):
        status = result.get("status", {})
        media_item = result.get("mediaItem")
        if status.get("code", 0) != 0 or not media_item:
            committed[str(path)] = {
                "state": "create_failed",
                "upload_token": upload_token,
                "status": status,
            }
            continue
        committed[str(path)] = {
            "state": "created",
            "upload_token": upload_token,
            "media_item_id": media_item.get("id", ""),
            "product_url": media_item.get("productUrl", ""),
        }
    return committed


def chunked(items: list[tuple[Path, str]], size: int) -> list[list[tuple[Path, str]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-dir", type=Path, required=True)
    parser.add_argument("--album-title", required=True)
    parser.add_argument("--account", default=os.environ.get("GOG_ACCOUNT", "zhihong@gmail.com"))
    parser.add_argument("--client", default=os.environ.get("GOG_PHOTOS_CLIENT", "photos"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    source_dir = args.image_dir.resolve()
    if not source_dir.is_dir():
        raise SystemExit(f"not a directory: {source_dir}")
    if not 1 <= args.batch_size <= 50:
        raise SystemExit("--batch-size must be between 1 and 50")

    manifest_path = args.manifest or source_dir / ".google-photos-upload-manifest.json"
    photos = iter_photos(source_dir, args.recursive)
    if args.limit:
        photos = photos[: args.limit]
    manifest = load_manifest(manifest_path, args.album_title, source_dir)
    items = manifest.setdefault("items", {})
    pending = [path for path in photos if items.get(str(path), {}).get("state") != "created"]

    print(f"source_dir={source_dir}")
    print(f"album_title={args.album_title}")
    print(f"manifest={manifest_path}")
    print(f"photos={len(photos)} pending={len(pending)} already_created={len(photos) - len(pending)}")
    if args.dry_run:
        return 0
    if not pending:
        return 0

    token_provider = TokenProvider(args.account, args.client)
    if not manifest.get("album_id"):
        manifest["album_id"] = create_album(args.album_title, token_provider)
        save_manifest(manifest_path, manifest)
        print(f"album_id={manifest['album_id']}")

    uploaded: list[tuple[Path, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(upload_one, path, token_provider): path for path in pending}
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            path = futures[future]
            try:
                _, upload_token = future.result()
            except Exception as exc:
                items[str(path)] = {"state": "upload_failed", "error": str(exc)}
                print(f"upload_failed {path.name}: {exc}", file=sys.stderr)
            else:
                items[str(path)] = {"state": "uploaded", "upload_token": upload_token}
                uploaded.append((path, upload_token))
                print(f"uploaded {index}/{len(pending)} {path.name}")
            if index % 10 == 0:
                save_manifest(manifest_path, manifest)

    save_manifest(manifest_path, manifest)
    uploaded.sort(key=lambda record: photos.index(record[0]))
    created = 0
    for batch_index, batch in enumerate(chunked(uploaded, args.batch_size), start=1):
        updates = commit_batch(manifest["album_id"], batch, token_provider)
        items.update(updates)
        batch_created = sum(1 for item in updates.values() if item.get("state") == "created")
        created += batch_created
        save_manifest(manifest_path, manifest)
        print(f"committed_batch {batch_index}: created={batch_created} total_created={created}")

    failed = sum(1 for item in items.values() if item.get("state") != "created")
    print(f"done created={created} failed_or_pending={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
