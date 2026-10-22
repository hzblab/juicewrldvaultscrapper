#!/usr/bin/env python3
"""Download the public Juice WRLD Vault music catalog (Python 3.10+, no dependencies).

Examples:
    python download_juicewrldvault.py --list-only
    python download_juicewrldvault.py --output juicewrldvault --artwork
    python download_juicewrldvault.py --limit 2

Only public catalog/download endpoints are used. Hidden files and files requiring
authentication are outside the scope of this script.
"""

import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

BASE = "https://juicewrldvault.com"
MEDIA = "https://media.juicewrldvault.com"
VERSION = "1.0.0"
TRANSIENT = {408, 425, 429, 500, 502, 503, 504}


class Client:
    def __init__(self, delay=0.5, retries=4, timeout=90):
        self.delay, self.retries, self.timeout = delay, retries, timeout

    def open(self, url, headers=None, body=None):
        time.sleep(self.delay)
        combined = {"User-Agent": f"VaultCatalogDownloader/{VERSION}",
                    "Accept-Encoding": "identity", "Referer": BASE + "/"}
        combined.update(headers or {})
        return urlopen(Request(url, data=body, headers=combined), timeout=self.timeout)

    def backoff(self, attempt, error):
        wait = min(60, 2 ** attempt)
        if isinstance(error, HTTPError):
            value = error.headers.get("Retry-After", "")
            if value.isdigit():
                wait = max(wait, int(value))
            error.close()
        print(f"  Retrying in {wait}s: {error}", file=sys.stderr)
        time.sleep(wait)

    def json(self, path, body=None):
        payload = None if body is None else json.dumps(body).encode()
        for attempt in range(self.retries + 1):
            try:
                with self.open(BASE + path,
                               {"Content-Type": "application/json"}, payload) as response:
                    data = json.load(response)
                # Some deployments return JSON encoded as a JSON string.
                return json.loads(data) if isinstance(data, str) else data
            except (HTTPError, URLError, TimeoutError, ConnectionError,
                    http.client.IncompleteRead) as error:
                if (isinstance(error, HTTPError) and error.code not in TRANSIENT
                        or attempt == self.retries):
                    raise
                self.backoff(attempt, error)


def catalog(client):
    pool = client.json("/api/songs/shuffle-pool")
    keys = ("ids", "rawIds", "instrumentalIds")
    if not isinstance(pool, dict) or any(not isinstance(pool.get(key, []), list) for key in keys):
        raise RuntimeError("Unexpected catalog response; the site's API may have changed.")
    ids = list(dict.fromkeys(str(song_id) for key in keys
                             for song_id in pool.get(key, [])))
    if not ids:
        raise RuntimeError("Public catalog is empty or the site's API has changed.")
    songs = {}
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        result = client.json("/api/player/queue-songs", {"ids": batch})
        if not isinstance(result, dict) or not isinstance(result.get("songs"), list):
            raise RuntimeError("Unexpected track metadata response; stopping.")
        for song in result["songs"]:
            if not isinstance(song, dict) or song.get("id") is None:
                raise RuntimeError("Invalid track metadata; the site's API may have changed.")
            songs[str(song["id"])] = song
        print(f"Catalog: {min(start + 100, len(ids))}/{len(ids)} IDs", flush=True)
    missing = set(ids) - songs.keys()
    if missing:
        raise RuntimeError(f"Metadata missing for {len(missing)} catalog IDs; rerun later.")
    return [songs[song_id] for song_id in ids]


def safe_name(value, limit=110):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
    value = value[:limit].rstrip(" .") or "untitled"
    # Common filesystems cap each filename at 255 bytes, including its suffix.
    while len(value.encode("utf-8")) > 150:
        value = value[:-1]
    if value.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10))
    }:
        value = "_" + value
    return value


def save_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def download(client, url, destination, kind):
    """Resume partial files with Range and If-Range; publish only complete files."""
    if destination.is_file() and destination.stat().st_size > 0:
        return "skipped"
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    state_path = partial.with_name(partial.name + ".json")
    for attempt in range(client.retries + 1):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            state = {}
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if not isinstance(state, dict):
                        state = {}
                except (ValueError, OSError):
                    pass
            validator = state.get("validator") if state.get("url") == url else None
            headers = {"Range": f"bytes={offset}-", "If-Range": validator} if offset and validator else {}
            with client.open(url, headers) as response:
                content_type = response.headers.get_content_type()
                if (content_type in {"text/html", "application/json", "text/plain"}
                        or kind == "image" and not content_type.startswith("image/")):
                    raise RuntimeError(f"Unexpected {content_type} instead of {kind}: {url}")
                if response.status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                         response.headers.get("Content-Range", ""))
                    if not match or not headers or int(match[1]) != offset:
                        raise RuntimeError("Invalid partial-content response")
                    total = int(match[3])
                    mode = "ab"
                elif response.status == 200:
                    offset, mode = 0, "wb"
                    length = response.headers.get("Content-Length")
                    total = int(length) if length else None
                else:
                    raise RuntimeError(f"Unexpected HTTP status {response.status}")
                validator = response.headers.get("ETag")
                if validator and validator.startswith("W/"):
                    validator = None
                validator = validator or response.headers.get("Last-Modified")
                with partial.open(mode) as output:
                    save_json(state_path, {"url": url, "validator": validator})
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                size = partial.stat().st_size
                if not size or total is not None and size != total:
                    raise ConnectionError(f"Incomplete file: {size} of {total} bytes")
            os.replace(partial, destination)
            state_path.unlink(missing_ok=True)
            return "downloaded"
        except (HTTPError, URLError, TimeoutError, ConnectionError,
                http.client.IncompleteRead) as error:
            # A complete partial file may produce 416. Restart to verify it.
            if isinstance(error, HTTPError) and error.code == 416:
                partial.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
            elif isinstance(error, HTTPError) and error.code not in TRANSIENT:
                raise
            if attempt == client.retries:
                raise
            client.backoff(attempt, error)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--output", type=Path,
                        default=Path.home() / "Downloads" / "juicewrldvault",
                        help="Download folder (default: ~/Downloads/juicewrldvault)")
    parser.add_argument("--artwork", action="store_true", help="Also download each track's cover image")
    parser.add_argument("--list-only", action="store_true", help="Save catalog.json without downloading media")
    parser.add_argument("--limit", type=int, help="Download only the first N tracks (for a trial run)")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between requests (default: 0.5)")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=90,
                        help="Per-request socket timeout in seconds (default: 90)")
    args = parser.parse_args()
    if (not math.isfinite(args.delay) or args.delay < 0 or args.retries < 0
            or not math.isfinite(args.timeout) or args.timeout <= 0
            or args.limit is not None and args.limit < 1):
        parser.error("delay must be finite and nonnegative; retries must be nonnegative; "
                     "timeout must be finite and positive; limit must be positive")
    args.output = args.output.expanduser().resolve()
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        # Check write access before spending time loading the catalog.
        with tempfile.TemporaryFile(dir=args.output):
            pass
    except OSError as error:
        raise RuntimeError(
            f"Cannot create or write to download folder '{args.output}'. "
            "Choose a writable folder with --output. "
            f"System reported: {error}"
        ) from error
    print(f"Download folder: {args.output}", flush=True)
    client = Client(args.delay, args.retries, args.timeout)
    songs = catalog(client)
    save_json(args.output / "catalog.json", songs)
    print(f"Found {len(songs)} public tracks. Catalog saved to {args.output / 'catalog.json'}")
    if args.list_only:
        return 0
    selected = songs if args.limit is None else songs[:args.limit]
    failures, counts = [], {"downloaded": 0, "skipped": 0}
    for number, song in enumerate(selected, 1):
        song_id = str(song["id"])
        # Stable ID suffix prevents collisions between versions with identical titles.
        identifier = safe_name(song_id, 35) + "-" + hashlib.sha256(song_id.encode()).hexdigest()[:8]
        stem = safe_name(f"{song.get('title', 'Untitled')} - {song.get('author', 'Unknown')}")
        stem += f" [{identifier}]"
        jobs = [(BASE + "/api/download-song?" + urlencode({"id": song_id}),
                 args.output / "songs" / (stem + ".mp3"), "audio")]
        if args.artwork and song.get("image_path"):
            image = song["image_path"]
            if image.startswith(("https://", "http://")):
                image_url = image
            else:
                image_path = image.lstrip("/")
                if not image_path.startswith(("images/", "playlist-covers/", "liked-songs/")):
                    image_path = "images/" + image_path
                image_url = MEDIA + "/" + quote(image_path, safe="/")
            extension = Path(urlsplit(image_url).path).suffix.lower()
            if extension not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}:
                extension = ".img"
            jobs.append((image_url, args.output / "artwork" / (stem + extension), "image"))
        for url, destination, kind in jobs:
            try:
                status = download(client, url, destination, kind)
                counts[status] += 1
                print(f"[{number}/{len(selected)}] {status}: {destination.name}", flush=True)
            except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
                print(f"FAILED: {destination.name}: {error}", file=sys.stderr)
                failures.append({"id": song_id, "url": url, "file": str(destination), "error": str(error)})
    save_json(args.output / "failures.json", failures)
    print(f"Done: {counts['downloaded']} downloaded, {counts['skipped']} skipped, {len(failures)} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Run the same command to resume.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
