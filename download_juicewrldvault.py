#!/usr/bin/env python3
"""Download the public Juice WRLD Vault music catalog (Python 3.10+, no dependencies).

Examples:
    python download_juicewrldvault.py --list-only
    python download_juicewrldvault.py --output juicewrldvault --artwork
    python download_juicewrldvault.py --limit 2
    python download_juicewrldvault.py --workers 8 --delay 0.1
    python download_juicewrldvault.py --output juicewrldvault --sync-history

Download history stays in ~/.juicewrldvault/downloaded_songs.json, independently
of the media folder. Run --sync-history before moving existing songs away.

Only public catalog/download endpoints are used. Hidden files and files requiring
authentication are outside the scope of this script.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

BASE = "https://juicewrldvault.com"
MEDIA = "https://media.juicewrldvault.com"
VERSION = "1.2.0"
TRANSIENT = {408, 425, 429, 500, 502, 503, 504}


class DownloadCancelled(Exception):
    """Stop workers while retaining their partial files."""


class Client:
    def __init__(self, delay=0.1, retries=4, timeout=90):
        self.delay, self.retries, self.timeout = delay, retries, timeout
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._next_request = 0.0
        self._cooldown_until = 0.0

    def check_stopped(self):
        if self.stop.is_set():
            raise DownloadCancelled()

    def wait(self, seconds):
        if self.stop.wait(seconds):
            raise DownloadCancelled()

    def pace(self):
        # One shared request-start limit, independent of the worker count.
        while True:
            self.check_stopped()
            with self._lock:
                now = time.monotonic()
                remaining = max(self._next_request, self._cooldown_until) - now
                if remaining <= 0:
                    self._next_request = now + self.delay
                    return
            self.wait(remaining)

    def open(self, url, headers=None, body=None):
        self.pace()
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
            elif value:
                try:
                    wait = max(wait, parsedate_to_datetime(value).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
            if error.code in {429, 503}:
                with self._lock:
                    self._cooldown_until = max(self._cooldown_until, time.monotonic() + wait)
            error.close()
        print(f"  Retrying in {wait}s: {error}", file=sys.stderr)
        self.wait(wait)

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


def song_identifier(song_id):
    return safe_name(song_id, 35) + "-" + hashlib.sha256(song_id.encode()).hexdigest()[:8]


class DownloadHistory:
    """Only the main thread reads and updates this durable completion record."""

    def __init__(self, path):
        self.path = path
        self.completed = {"audio": set(), "image": set()}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("unsupported history format")
                for kind in self.completed:
                    ids = data[kind]
                    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
                        raise ValueError("invalid song IDs")
                    self.completed[kind] = set(ids)
            except (ValueError, KeyError, TypeError) as error:
                raise RuntimeError(f"Cannot read download history '{path}': {error}. "
                                   "Restore a valid copy before continuing.") from error

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.path, {"version": 1,
                             **{kind: sorted(ids) for kind, ids in self.completed.items()}})

    def scan(self, songs, output):
        # Match stable suffixes even if the catalog's title/author has changed.
        identifiers = {song_identifier(str(song["id"])) + "]": str(song["id"])
                       for song in songs}
        found = {"audio": 0, "image": 0}
        for kind, directory, extensions in (
                ("audio", "songs", {".mp3"}),
                ("image", "artwork", {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".img"})):
            folder = output / directory
            if not folder.exists():
                continue
            for path in folder.iterdir():
                if path.suffix.lower() not in extensions or not path.is_file() or path.stat().st_size == 0:
                    continue
                song_id = identifiers.get(path.stem.rpartition(" [")[2])
                if song_id is not None:
                    self.completed[kind].add(song_id)
                    found[kind] += 1
        return found


def download(client, url, destination, kind):
    """Resume partial files with Range and If-Range; publish only complete files."""
    client.check_stopped()
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
                        client.check_stopped()
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
    parser.add_argument("--history", type=Path,
                        default=Path.home() / ".juicewrldvault" / "downloaded_songs.json",
                        help="Persistent history file (default: ~/.juicewrldvault/downloaded_songs.json)")
    parser.add_argument("--sync-history", action="store_true",
                        help="Record existing files using local catalog.json, then exit without network access")
    parser.add_argument("--limit", type=int, help="Download only the first N tracks (for a trial run)")
    parser.add_argument("--workers", type=int, default=6,
                        help="Simultaneous file downloads, 1-32 (default: 6)")
    parser.add_argument("--delay", type=float, default=0.1,
                        help="Minimum seconds between request starts across all workers (default: 0.1)")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=90,
                        help="Per-request socket timeout in seconds (default: 90)")
    args = parser.parse_args()
    if args.list_only and args.sync_history:
        parser.error("--list-only and --sync-history cannot be combined")
    if (not math.isfinite(args.delay) or args.delay < 0 or args.retries < 0
            or not math.isfinite(args.timeout) or args.timeout <= 0
            or args.limit is not None and args.limit < 1
            or not 1 <= args.workers <= 32):
        parser.error("delay must be finite and nonnegative; retries must be nonnegative; "
                     "timeout must be finite and positive; limit must be positive; workers must be 1-32")
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
    args.history = args.history.expanduser().resolve()
    # A history file must never replace catalog, failure reports or media files.
    if (args.history in {args.output / "catalog.json", args.output / "failures.json"}
            or args.output / "songs" in args.history.parents
            or args.output / "artwork" in args.history.parents):
        parser.error("Choose a --history path separate from catalog.json, failures.json and media folders")
    if args.sync_history:
        try:
            songs = json.loads((args.output / "catalog.json").read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RuntimeError("No catalog.json in the output folder. Run with --list-only first, "
                               "then rerun --sync-history.") from error
        if (not isinstance(songs, list) or not songs
                or any(not isinstance(song, dict) or song.get("id") is None for song in songs)):
            raise RuntimeError("Invalid local catalog.json; refresh it with --list-only.")
        history = DownloadHistory(args.history)
        found = history.scan(songs, args.output)
        if not found["audio"]:
            raise RuntimeError("No completed songs matched the local catalog. Check --output and keep "
                               "the original ID suffixes in the filenames until history is recorded.")
        history.save()
        print(f"Recorded {found['audio']} existing songs and {found['image']} artwork files.")
        print(f"History saved to: {args.history}")
        print("You can now move or rename these files. Keep the history file on this computer.")
        return 0
    client = Client(args.delay, args.retries, args.timeout)
    history = None if args.list_only else DownloadHistory(args.history)
    songs = catalog(client)
    save_json(args.output / "catalog.json", songs)
    print(f"Found {len(songs)} public tracks. Catalog saved to {args.output / 'catalog.json'}")
    if args.list_only:
        return 0
    history.scan(songs, args.output)
    history.save()
    print(f"History: {args.history} ({len(history.completed['audio'])} songs recorded)", flush=True)
    selected = songs if args.limit is None else songs[:args.limit]
    failures, counts = [], {"downloaded": 0, "skipped": 0}
    all_jobs = []
    for song in selected:
        song_id = str(song["id"])
        # Stable ID suffix prevents collisions between versions with identical titles.
        identifier = song_identifier(song_id)
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
            if song_id in history.completed[kind]:
                counts["skipped"] += 1
            else:
                all_jobs.append((song_id, url, destination, kind))
    print(f"Skipping {counts['skipped']} files already recorded in history.", flush=True)
    print(f"Downloading {len(all_jobs)} files with {args.workers} workers "
          f"(request spacing: {args.delay}s).", flush=True)
    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=args.workers)
    try:
        pending = {executor.submit(download, client, url, destination, kind):
                   (song_id, url, destination, kind)
                   for song_id, url, destination, kind in all_jobs}
        for number, future in enumerate(as_completed(pending), 1):
            song_id, url, destination, kind = pending[future]
            try:
                status = future.result()
                counts[status] += 1
                print(f"[{number}/{len(all_jobs)}] {status}: {destination.name}", flush=True)
            except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
                print(f"FAILED: {destination.name}: {error}", file=sys.stderr)
                failures.append({"id": song_id, "url": url, "file": str(destination), "error": str(error)})
            else:
                history.completed[kind].add(song_id)
                history.save()
    finally:
        # Cancel queued work on Ctrl+C; active workers stop between reads or after
        # their socket timeout. Their .part files can be resumed on the next run.
        client.stop.set()
        executor.shutdown(wait=True, cancel_futures=True)
    save_json(args.output / "failures.json", failures)
    elapsed = time.monotonic() - started
    print(f"Done in {elapsed / 60:.1f} min: {counts['downloaded']} downloaded, "
          f"{counts['skipped']} skipped, {len(failures)} failed.")
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
