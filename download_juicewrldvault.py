#!/usr/bin/env python3
"""Download the public Juice WRLD Vault music catalog (Python 3.10+, no dependencies).

Examples:
    python download_juicewrldvault.py --list-only
    python download_juicewrldvault.py --output juicewrldvault --artwork
    python download_juicewrldvault.py --limit 2
    python download_juicewrldvault.py --workers 8 --delay 0.1
    python download_juicewrldvault.py --output juicewrldvault --sync-history
    python download_juicewrldvault.py --list-only --search "night" --type raw
    python download_juicewrldvault.py --song-id TRACK_ID
    python download_juicewrldvault.py --retry-failed
    python download_juicewrldvault.py --redownload TRACK_ID

Download history stays in ~/.juicewrldvault/downloaded_songs.json, independently
of the media folder. Run --sync-history before moving existing songs away.
Audio is saved in its original supported format with embedded tags intact.

Only public catalog/download endpoints are used. Hidden files and files requiring
authentication are outside the scope of this script.
"""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass, replace
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
VERSION = "1.6.0"
TRANSIENT = {408, 425, 429, 500, 502, 503, 504}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".opus", ".m4a", ".mp4"}


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

    def open(self, url, headers=None, body=None, method=None):
        self.pace()
        combined = {"User-Agent": f"VaultCatalogDownloader/{VERSION}",
                    "Accept-Encoding": "identity", "Referer": BASE + "/"}
        combined.update(headers or {})
        return urlopen(Request(url, data=body, headers=combined, method=method), timeout=self.timeout)

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
                    if isinstance(error, HTTPError):
                        error.close()
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
    groups = {"songs": "ids", "raw": "rawIds", "instrumental": "instrumentalIds"}
    memberships = {kind: set(map(str, pool.get(key, []))) for kind, key in groups.items()}
    return [dict(songs[song_id], vault_categories=[kind for kind, members in memberships.items()
                                                  if song_id in members]) for song_id in ids]


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


def song_base_name(song):
    return safe_name(f"{song.get('title') or 'Untitled'} - {song.get('author') or 'Unknown'}")


def allocate_filenames(songs, existing=None, output=None):
    """Keep stable title/artist names, numbering collisions without exposing IDs."""
    names = dict(existing or {})
    used = {name.casefold() for name in names.values()}
    occupied = set()
    if output is not None:
        for folder in (output, output / "songs", output / "artwork"):
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                name = path.name
                if name.lower().endswith(".part.json"):
                    name = name[:-10]
                elif name.lower().endswith(".part"):
                    name = name[:-5]
                p = Path(name)
                if p.suffix.lower() in AUDIO_EXTENSIONS | {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".img"}:
                    occupied.add(p.stem.casefold())
    for song in sorted(songs, key=lambda item: str(item["id"])):
        song_id = str(song["id"])
        if song_id in names:
            continue
        base = song_base_name(song)
        stem, number = base, 2
        while stem.casefold() in used or stem.casefold() in occupied:
            stem = f"{base} ({number})"
            number += 1
        names[song_id] = stem
        used.add(stem.casefold())
    return names


class RunLock:
    """OS-managed lock: released even if the process crashes; keep the lock file."""

    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            self.file.seek(0, os.SEEK_END)
            if not self.file.tell():
                self.file.write(b"\0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            self.file = None
            raise RuntimeError(f"Cannot lock '{self.path}'. Another downloader may be using "
                               "this output folder or history file. Close it and retry.") from error
        return self

    def __exit__(self, *exc):
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()


def validate_mp3(stream, size):
    """Check complete MPEG Layer III frames, allowing ID3 and APE tags."""
    end = size
    if end >= 128:
        stream.seek(end - 128)
        if stream.read(3) == b"TAG":
            end -= 128
    if end >= 32:
        stream.seek(end - 32)
        footer = stream.read(32)
        if footer[:8] == b"APETAGEX":
            tag_size = int.from_bytes(footer[12:16], "little")
            if not 32 <= tag_size <= end:
                raise RuntimeError("Invalid APE tag length")
            end -= tag_size
            if end >= 32:
                stream.seek(end - 32)
                if stream.read(8) == b"APETAGEX":
                    end -= 32
    stream.seek(0)
    header = stream.read(10)
    position = 0
    if header[:3] == b"ID3":
        if len(header) != 10 or header[3] not in {2, 3, 4} or any(b & 0x80 for b in header[6:]):
            raise RuntimeError("Invalid ID3 header")
        position = 10 + sum(b << shift for b, shift in zip(header[6:], (21, 14, 7, 0)))
        if header[3] == 4 and header[5] & 0x10:
            position += 10
    frames = 0
    while position < end:
        stream.seek(position)
        header = stream.read(min(4, end - position))
        if header and not any(header):
            # Some encoders append zero padding after the final frame.
            remaining = end - position - len(header)
            while remaining:
                block = stream.read(min(65536, remaining))
                if not block:
                    raise RuntimeError("Truncated MP3 padding")
                if any(block):
                    raise RuntimeError("Invalid data after MP3 frames")
                remaining -= len(block)
            break
        if len(header) != 4:
            # Some files from the catalog carry an empty JSON suffix after audio.
            # Preserve the source bytes; this is not an incomplete MPEG frame.
            if frames >= 2 and header == b"{}":
                break
            raise RuntimeError("Truncated MP3 frame header")
        bits = int.from_bytes(header, "big")
        version, layer = (bits >> 19) & 3, (bits >> 17) & 3
        bitrate_index, sample_index = (bits >> 12) & 15, (bits >> 10) & 3
        if (bits >> 21 != 0x7ff or version == 1 or layer != 1
                or bitrate_index in {0, 15} or sample_index == 3):
            raise RuntimeError("Invalid or unsupported MP3 frame")
        bitrates = ((32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
                    if version == 3 else (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
        sample_rate = (44100, 48000, 32000)[sample_index] // {3: 1, 2: 2, 0: 4}[version]
        frame_size = ((144000 if version == 3 else 72000) * bitrates[bitrate_index - 1]
                      // sample_rate + ((bits >> 9) & 1))
        if position + frame_size > end:
            raise RuntimeError("Truncated MP3 frame")
        position += frame_size
        frames += 1
    if frames < 2:
        raise RuntimeError("Audio must contain at least two complete MP3 frames")


def validate_wav(stream, size):
    """Validate standard RIFF/WAVE chunk boundaries without rewriting tags."""
    stream.seek(0)
    header = stream.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise RuntimeError("Invalid or unsupported WAV header")
    end = int.from_bytes(header[4:8], "little") + 8
    if end < 12 or end > size:
        raise RuntimeError("Truncated WAV container")
    position, has_format, has_audio = 12, False, False
    while position < end:
        if end - position < 8:
            raise RuntimeError("Truncated WAV chunk header")
        stream.seek(position)
        chunk = stream.read(8)
        chunk_size = int.from_bytes(chunk[4:8], "little")
        chunk_end = position + 8 + chunk_size
        if chunk_end > end:
            raise RuntimeError("Truncated WAV chunk")
        if chunk[:4] == b"fmt ":
            if chunk_size < 16:
                raise RuntimeError("Invalid WAV format chunk")
            fmt = stream.read(16)
            if (not int.from_bytes(fmt[:2], "little") or not int.from_bytes(fmt[2:4], "little")
                    or not int.from_bytes(fmt[4:8], "little") or not int.from_bytes(fmt[12:14], "little")):
                raise RuntimeError("Invalid WAV format details")
            has_format = True
        elif chunk[:4] == b"data" and chunk_size:
            has_audio = True
        position = chunk_end + (chunk_size & 1)
        if position > end:
            raise RuntimeError("Truncated WAV chunk padding")
    if not has_format or not has_audio:
        raise RuntimeError("WAV is missing a format or audio data chunk")


def validate_flac(stream, size):
    stream.seek(0)
    if stream.read(4) != b"fLaC":
        raise RuntimeError("Invalid FLAC header")
    first, last, position = True, False, 4
    while not last:
        stream.seek(position)
        header = stream.read(4)
        if len(header) != 4:
            raise RuntimeError("Truncated FLAC metadata")
        last = bool(header[0] & 0x80)
        block_type, length = header[0] & 0x7f, int.from_bytes(header[1:4], "big")
        if first and (block_type != 0 or length != 34):
            raise RuntimeError("Invalid FLAC STREAMINFO")
        first = False
        position += 4 + length
        if position > size:
            raise RuntimeError("Truncated FLAC metadata")
    if position >= size:
        raise RuntimeError("FLAC contains no audio data")


def validate_opus(stream, size):
    position, pages = 0, 0
    while position < size:
        stream.seek(position)
        header = stream.read(27)
        if len(header) != 27 or header[:4] != b"OggS" or header[4] != 0:
            raise RuntimeError("Invalid or truncated Ogg page")
        lacing = stream.read(header[26])
        if len(lacing) != header[26]:
            raise RuntimeError("Truncated Ogg segment table")
        length = sum(lacing)
        if position + 27 + len(lacing) + length > size:
            raise RuntimeError("Truncated Ogg page")
        if pages == 0 and (length < 8 or stream.read(8) != b"OpusHead"):
            raise RuntimeError("Ogg stream is not Opus audio")
        position += 27 + len(lacing) + length
        pages += 1
    if not pages:
        raise RuntimeError("Empty Ogg stream")


def validate_mp4(stream, size):
    position, boxes = 0, set()
    while position < size:
        stream.seek(position)
        header = stream.read(8)
        if len(header) != 8:
            raise RuntimeError("Truncated MP4 box header")
        length = int.from_bytes(header[:4], "big")
        box, minimum = header[4:8], 8
        if length == 1:
            extended = stream.read(8)
            if len(extended) != 8:
                raise RuntimeError("Truncated MP4 extended box header")
            length, minimum = int.from_bytes(extended, "big"), 16
        elif length == 0:
            length = size - position
        if length < minimum or position + length > size:
            raise RuntimeError("Invalid or truncated MP4 box")
        if box == b"ftyp" and length < minimum + 8:
            raise RuntimeError("Invalid MP4 file type box")
        if box == b"mdat" and length == minimum:
            raise RuntimeError("Empty MP4 media box")
        boxes.add(box)
        position += length
    if not {b"ftyp", b"moov", b"mdat"}.issubset(boxes):
        raise RuntimeError("MP4 is missing a required container box")


def audio_format(path, hint=None):
    """Determine the real format from bytes, including legacy misnamed files."""
    with path.open("rb") as stream:
        header = stream.read(12)
    if header.startswith(b"RIFF"):
        return ".wav"
    if header.startswith(b"fLaC"):
        return ".flac"
    if header.startswith(b"OggS"):
        return ".opus"  # Validation confirms the Ogg stream is Opus.
    if header[4:8] == b"ftyp":
        if hint in {".m4a", ".mp4"}:
            return hint
        return ".m4a" if header[8:12] in {b"M4A ", b"M4B "} else ".mp4"
    if header.startswith(b"ID3") or len(header) >= 2 and header[0] == 0xff and header[1] & 0xe0 == 0xe0:
        return ".mp3"
    raise RuntimeError("Unsupported audio format; expected MP3, WAV, FLAC, Opus or MP4/M4A")


def validate_media(path, kind, expected_size=None, format_hint=None):
    size = path.stat().st_size
    if not size or expected_size is not None and size != expected_size:
        raise RuntimeError(f"Invalid {kind} size: {size}, expected {expected_size or 'nonempty'}")
    with path.open("rb") as stream:
        if kind == "audio":
            fmt = audio_format(path, format_hint or path.suffix.lower())
            {".mp3": validate_mp3, ".wav": validate_wav, ".flac": validate_flac,
             ".opus": validate_opus, ".m4a": validate_mp4, ".mp4": validate_mp4}[fmt](stream, size)
        else:
            header = stream.read(64)
            stream.seek(max(0, size - 12))
            tail = stream.read()
            valid = (header.startswith(b"\x89PNG\r\n\x1a\n") and tail == b"\0\0\0\0IEND\xaeB`\x82"
                     or header.startswith(b"\xff\xd8\xff") and tail.endswith(b"\xff\xd9")
                     or header[:6] in {b"GIF87a", b"GIF89a"} and tail.endswith(b";")
                     or header[:4] == b"RIFF" and header[8:12] == b"WEBP"
                     and int.from_bytes(header[4:8], "little") + 8 == size
                     or header[4:8] == b"ftyp" and b"avif" in header[8:64])
            if not valid:
                raise RuntimeError("Invalid or unsupported artwork signature")
    return size


class DownloadHistory:
    """Only the main thread reads and updates this durable completion record."""

    def __init__(self, path):
        self.path = path
        self.completed = {"audio": set(), "image": set()}
        self.sizes = {"audio": {}, "image": {}}
        # The catalog path is a change signal, not a guaranteed content hash.
        self.audio_sources = {}
        self.filenames = {}
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
                sizes = data.get("sizes", {})
                if not isinstance(sizes, dict):
                    raise ValueError("invalid size records")
                for kind in self.sizes:
                    records = sizes.get(kind, {})
                    if (not isinstance(records, dict) or any(not isinstance(key, str)
                            or type(value) is not int or value <= 0 for key, value in records.items())):
                        raise ValueError("invalid size records")
                    self.sizes[kind] = records
                sources = data.get("audio_sources", {})
                if (not isinstance(sources, dict) or any(not isinstance(key, str)
                        or not isinstance(value, str) or not value for key, value in sources.items())):
                    raise ValueError("invalid audio source records")
                self.audio_sources = sources
                names = data.get("filenames", {})
                if (not isinstance(names, dict) or any(not isinstance(key, str)
                        or not isinstance(value, str) or not value or value in {".", ".."}
                        or value != value.strip(" .") or len(value.encode("utf-8")) > 220
                        or value.split(".")[0].upper() in {
                            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                            *(f"LPT{i}" for i in range(10))}
                        or re.search(r'[<>:"/\\|?*\x00-\x1f]', value) for key, value in names.items())
                        or len({name.casefold() for name in names.values()}) != len(names)):
                    raise ValueError("invalid or conflicting filename records")
                self.filenames = names
            except (ValueError, KeyError, TypeError) as error:
                raise RuntimeError(f"Cannot read download history '{path}': {error}. "
                                   "Restore a valid copy before continuing.") from error

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        save_json(self.path, {"version": 1, "sizes": self.sizes, "audio_sources": self.audio_sources,
                             "filenames": self.filenames,
                             **{kind: sorted(ids) for kind, ids in self.completed.items()}})

    def record(self, song_id, kind, path, source=None):
        self.sizes[kind][song_id] = validate_media(path, kind)
        self.completed[kind].add(song_id)
        if kind == "audio" and source:
            self.audio_sources[song_id] = source
        if (song_id not in self.filenames
                and not path.stem.endswith(f" [{song_identifier(song_id)}]")
                and path.stem.casefold() not in {name.casefold() for name in self.filenames.values()}):
            self.filenames[song_id] = path.stem

    def forget(self, song_id, kind):
        self.completed[kind].discard(song_id)
        self.sizes[kind].pop(song_id, None)
        # Keep the previous audio source until its replacement succeeds.

    def seed_sources(self, songs, previous):
        """Use the last saved catalog as a best-effort baseline for legacy history."""
        current = {str(song["id"]): song.get("song_path") for song in songs}
        count = 0
        for song_id in self.completed["audio"]:
            if song_id in self.audio_sources:
                continue
            source = previous.get(song_id) or current.get(song_id)
            if isinstance(source, str) and source:
                self.audio_sources[song_id] = source
                count += 1
        return count

    def scan(self, songs, output, allow_untracked_names=False, pending_failures=None):
        # Match stable suffixes even if the catalog's title/author has changed.
        identifiers = {song_identifier(str(song["id"])) + "]": str(song["id"])
                       for song in songs}
        mapped_names = {stem.casefold(): song_id for song_id, stem in self.filenames.items()}
        grouped = {}
        if allow_untracked_names:
            for song in songs:
                grouped.setdefault(song_base_name(song).casefold(), []).append(str(song["id"]))
        unambiguous = {stem: ids[0] for stem, ids in grouped.items() if len(ids) == 1}
        found = {"audio": 0, "image": 0}
        for kind, directory, extensions in (
                ("audio", "songs", AUDIO_EXTENSIONS),
                ("image", "artwork", {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".img"})):
            folder = output / directory
            if not folder.exists():
                continue
            valid_ids, invalid_ids = set(), set()
            for path in folder.iterdir():
                if path.suffix.lower() not in extensions or not path.is_file():
                    continue
                song_id = identifiers.get(path.stem.rpartition(" [")[2])
                if song_id is None:
                    match = mapped_names.get(path.stem.casefold())
                    if match is not None and (allow_untracked_names or match in self.completed[kind]
                                              or (match, kind) in (pending_failures or {})):
                        song_id = match
                    elif allow_untracked_names:
                        song_id = unambiguous.get(path.stem.casefold())
                if song_id is not None:
                    try:
                        validate_media(path, kind, self.sizes[kind].get(song_id))
                    except RuntimeError as error:
                        print(f"Ignoring invalid file {path.name}: {error}", file=sys.stderr)
                        invalid_ids.add(song_id)
                    else:
                        valid_ids.add(song_id)
                        self.sizes[kind][song_id] = path.stat().st_size
                        if allow_untracked_names and not identifiers.get(path.stem.rpartition(" [")[2]):
                            if song_id not in self.filenames and path.stem.casefold() not in mapped_names:
                                self.filenames[song_id] = path.stem
                                mapped_names[path.stem.casefold()] = song_id
                        found[kind] += 1
            for song_id in invalid_ids - valid_ids:
                self.forget(song_id, kind)
            self.completed[kind].update(valid_ids)
        return found


class DownloadResult(str):
    """Status plus actual path, which can differ from the catalog's extension."""

    def __new__(cls, status, path):
        result = super().__new__(cls, status)
        result.path = path
        return result


def download(client, url, destination, kind, force=False, progress=None, fresh=False):
    """Resume partial files with Range and If-Range; publish only complete files."""
    client.check_stopped()
    if not force and destination.is_file():
        try:
            validate_media(destination, kind)
        except RuntimeError:
            pass
        else:
            return DownloadResult("skipped", destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    state_path = partial.with_name(partial.name + ".json")
    if fresh:
        partial.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
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
                    if (not match or not headers or int(match[1]) != offset
                            or not offset <= int(match[2]) < int(match[3])):
                        raise RuntimeError("Invalid partial-content response")
                    total = int(match[3])
                    expected_body = int(match[2]) - offset + 1
                    length = response.headers.get("Content-Length")
                    if length and int(length) != expected_body:
                        raise RuntimeError("Content-Length does not match Content-Range")
                    returned_validator = response.headers.get(
                        "ETag" if str(validator).startswith('"') else "Last-Modified")
                    if returned_validator and returned_validator != validator:
                        raise RuntimeError("Server changed the partial-file validator; use --redownload ID")
                    mode = "ab"
                elif response.status == 200:
                    offset, mode = 0, "wb"
                    length = response.headers.get("Content-Length")
                    total = int(length) if length else None
                    expected_body = total
                else:
                    raise RuntimeError(f"Unexpected HTTP status {response.status}")
                new_validator = response.headers.get("ETag")
                if new_validator and new_validator.startswith("W/"):
                    new_validator = None
                new_validator = new_validator or response.headers.get("Last-Modified")
                validator = new_validator or (validator if response.status == 206 else None)
                with partial.open(mode) as output:
                    save_json(state_path, {"url": url, "validator": validator})
                    while chunk := response.read(65536):
                        output.write(chunk)
                        if progress is not None:
                            progress.advance(len(chunk))
                        client.check_stopped()
                size = partial.stat().st_size
                if expected_body is not None and size - offset != expected_body:
                    raise ConnectionError("Response body length does not match its headers")
                if not size or total is not None and size != total:
                    raise ConnectionError(f"Incomplete file: {size} of {total} bytes")
            try:
                validate_media(partial, kind, total, destination.suffix.lower() if kind == "audio" else None)
            except RuntimeError:
                # Invalid complete payloads cannot be repaired by appending bytes.
                partial.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
                raise
            actual = destination.with_suffix(audio_format(partial, destination.suffix.lower())) if kind == "audio" else destination
            if actual != destination and actual.is_file() and not force:
                try:
                    validate_media(actual, kind)
                except RuntimeError as error:
                    raise RuntimeError(f"Existing destination '{actual}' is invalid; "
                                       "it was not overwritten. Inspect it before retrying.") from error
                else:
                    partial.unlink(missing_ok=True)
                    state_path.unlink(missing_ok=True)
                    return DownloadResult("skipped", actual)
            os.replace(partial, actual)
            state_path.unlink(missing_ok=True)
            return DownloadResult("downloaded", actual)
        except (HTTPError, URLError, TimeoutError, ConnectionError,
                http.client.IncompleteRead) as error:
            # A complete partial file may produce 416. Restart to verify it.
            if isinstance(error, HTTPError) and error.code == 416:
                partial.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
            elif isinstance(error, HTTPError) and error.code not in TRANSIENT:
                error.close()
                raise
            if attempt == client.retries:
                if isinstance(error, HTTPError):
                    error.close()
                raise
            client.backoff(attempt, error)


@dataclass(frozen=True)
class Job:
    song_id: str
    url: str
    destination: Path
    kind: str
    force: bool = False
    fresh: bool = False
    source: str = ""

    @property
    def key(self):
        return self.song_id, self.kind

    def failure(self, error):
        return {"id": self.song_id, "kind": self.kind, "url": self.url,
                "file": str(self.destination), "force": self.force, "error": str(error)}


def load_failures(path, required=False):
    if not path.exists():
        if required:
            raise RuntimeError("No failures.json in the output folder; there is nothing to retry.")
        return {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("expected a list")
        failures = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise ValueError("invalid failure record")
            kind = record.get("kind")
            if kind is None:
                # Read reports from v1.0/v1.2 without trusting their URLs or paths.
                suffix = Path(str(record.get("file", ""))).suffix.lower()
                kind = "audio" if suffix in AUDIO_EXTENSIONS else "image" if suffix in {
                    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".img"} else None
            if kind not in {"audio", "image"}:
                raise ValueError("invalid failure kind")
            failures[record["id"], kind] = dict(record, kind=kind)
        return failures
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"Cannot read failure report '{path}': {error}. "
                           "Restore a valid report or move it aside before continuing.") from error


def select_songs(songs, args, failures):
    targets = set(args.song_id or []) | set(args.redownload or [])
    available = {str(song["id"]) for song in songs}
    missing = targets - available
    if missing:
        raise RuntimeError("Requested song IDs are absent from the catalog: " + ", ".join(sorted(missing)))
    if args.retry_failed:
        unavailable = {key[0] for key in failures} - available
        if unavailable:
            print("Unavailable IDs retained in failures.json: " + ", ".join(sorted(unavailable)), file=sys.stderr)
    selected = []
    for song in songs:
        song_id = str(song["id"])
        if targets and song_id not in targets:
            continue
        if args.retry_failed and not any((song_id, kind) in failures for kind in ("audio", "image")):
            continue
        if args.track_type != "all" and args.track_type not in song.get("vault_categories", []):
            continue
        text = f"{song.get('title', '')} {song.get('author', '')}".casefold()
        if args.search and args.search.casefold() not in text:
            continue
        selected.append(song)
    return selected if args.limit is None else selected[:args.limit]


def build_jobs(songs, args, failures, filenames=None):
    jobs = []
    filenames = filenames if filenames is not None else allocate_filenames(songs)
    for song in songs:
        song_id = str(song["id"])
        stem = filenames[song_id]
        force = song_id in (args.redownload or []) or args.retry_failed
        fresh = song_id in (args.redownload or [])
        if not args.retry_failed or (song_id, "audio") in failures:
            source = song.get("song_path") if isinstance(song.get("song_path"), str) else ""
            extension = Path(urlsplit(source).path).suffix.lower()
            if extension not in AUDIO_EXTENSIONS:
                extension = ".mp3"  # The response bytes determine the final extension.
            jobs.append(Job(song_id, BASE + "/api/download-song?" + urlencode({"id": song_id}),
                            args.output / "songs" / (stem + extension), "audio", force, fresh, source))
        want_image = ((song_id, "image") in failures if args.retry_failed else args.artwork)
        image = song.get("image_path")
        if want_image and not image and args.retry_failed:
            raise RuntimeError(f"Cannot retry artwork for {song_id}: artwork is absent from the catalog.")
        if want_image and image:
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
            jobs.append(Job(song_id, image_url, args.output / "artwork" / (stem + extension), "image", force, fresh))
    return jobs


class Progress:
    def __init__(self, total, skipped=None):
        self.total = total
        self.finished = 0
        self.bytes = 0
        self.started = time.monotonic()
        self.last_print = self.started
        self.lock = threading.Lock()
        self.counts = {kind: {"downloaded": 0, "skipped": 0, "failed": 0}
                       for kind in ("audio", "image")}
        for kind, count in (skipped or {}).items():
            self.counts[kind]["skipped"] = count

    def advance(self, size):
        with self.lock:
            self.bytes += size

    def finish(self, kind, status):
        self.counts[kind][status] += 1
        self.finished += 1

    def report(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_print < 1:
            return
        self.last_print = now
        elapsed = max(0.001, now - self.started)
        with self.lock:
            rate = self.bytes / elapsed / (1024 * 1024)
        eta = ("0s" if self.finished == self.total else
               f"{(self.total - self.finished) * elapsed / self.finished:.0f}s"
               if self.finished else "calculating")
        parts = []
        for kind, label in (("audio", "Songs"), ("image", "Artwork")):
            c = self.counts[kind]
            parts.append(f"{label}: {c['downloaded']} downloaded, {c['skipped']} skipped, {c['failed']} failed")
        print(f"[{self.finished}/{self.total}] " + " | ".join(parts)
              + f" | {rate:.2f} MiB/s | elapsed {elapsed:.0f}s | ETA {eta}", flush=True)


def run_downloads(client, jobs, history, failure_path, failures, workers, skipped):
    progress = Progress(len(jobs), skipped)
    for job in jobs:
        failures[job.key] = job.failure("Pending or interrupted; rerun with --retry-failed")
        if job.force:
            history.forget(job.song_id, job.kind)
    history.save()
    save_json(failure_path, list(failures.values()))
    executor = ThreadPoolExecutor(max_workers=workers)
    pending = {}
    remaining = iter(jobs)

    def record(future, job):
        if future.cancelled():
            return
        try:
            status = future.result()
            history.record(job.song_id, job.kind, getattr(status, "path", job.destination),
                           job.source if status == "downloaded" else None)
        except (OSError, ValueError, RuntimeError, http.client.HTTPException, DownloadCancelled) as error:
            progress.finish(job.kind, "failed")
            failures[job.key] = job.failure(str(error) or "Interrupted")
            print(f"FAILED: {job.destination.name}: {str(error) or 'Interrupted'}", file=sys.stderr)
        else:
            progress.finish(job.kind, status)
            failures.pop(job.key, None)
            history.save()
        save_json(failure_path, list(failures.values()))

    try:
        exhausted = False
        while pending or not exhausted:
            # Keep only a small window of submitted futures, even for large catalogs.
            while not exhausted and len(pending) < workers * 2:
                job = next(remaining, None)
                if job is None:
                    exhausted = True
                    break
                pending[executor.submit(download, client, job.url, job.destination,
                                        job.kind, job.force, progress, job.fresh)] = job
            if not pending:
                break
            done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for future in done:
                record(future, pending.pop(future))
            progress.report()
    finally:
        client.stop.set()
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        finally:
            # Harvest work that completed while the main thread was interrupted.
            for future, job in list(pending.items()):
                if future.done():
                    record(future, job)
            save_json(failure_path, list(failures.values()))
            progress.report(force=True)
    return 1 if any(progress.counts[kind]["failed"] for kind in progress.counts) else 0


def previous_catalog_sources(path):
    """Read the prior catalog before replacing it; old histories have no source record."""
    if not path.exists():
        return {}
    try:
        songs = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(songs, list):
            raise ValueError("expected a list")
        return {str(song["id"]): song["song_path"] for song in songs
                if isinstance(song, dict) and song.get("id") is not None
                and isinstance(song.get("song_path"), str) and song["song_path"]}
    except (ValueError, OSError) as error:
        print(f"Warning: Cannot use previous catalog as an update baseline: {error}", file=sys.stderr)
        return {}


def recover_failure_filenames(songs, failures, history, output):
    """Recover names for pending work without trusting arbitrary stored paths."""
    by_id = {str(song["id"]): song for song in songs}
    used = {name.casefold() for name in history.filenames.values()}
    for (song_id, kind), item in failures.items():
        if song_id in history.filenames or song_id not in by_id:
            continue
        filename = item.get("file")
        if not isinstance(filename, str):
            continue
        path = Path(filename)
        folder = output / ("songs" if kind == "audio" else "artwork")
        if path.parent.resolve() != folder.resolve():
            continue
        extensions = AUDIO_EXTENSIONS if kind == "audio" else {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".img"}
        if path.suffix.lower() not in extensions:
            continue
        base = song_base_name(by_id[song_id])
        stem = path.stem
        suffix = stem[len(base):] if stem.startswith(base) else "?"
        if (suffix and not (re.fullmatch(r" \(\d+\)", suffix) and int(suffix[2:-1]) >= 2)) or stem.casefold() in used:
            continue
        history.filenames[song_id] = stem
        used.add(stem.casefold())


def local_audio_sizes(songs, history, output):
    """Find known sizes without reading audio bytes or following nested folders."""
    identifiers = {song_identifier(str(song["id"])) + "]": str(song["id"]) for song in songs}
    mapped = {name.casefold(): song_id for song_id, name in history.filenames.items()}
    sizes = {song_id: {size} for song_id, size in history.sizes["audio"].items()
             if song_id in history.completed["audio"]}
    for folder in (output, output / "songs"):
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
                continue
            song_id = identifiers.get(path.stem.rpartition(" [")[2])
            if song_id is None:
                song_id = mapped.get(path.stem.casefold())
            if song_id in history.completed["audio"]:
                size = path.stat().st_size
                if size:
                    sizes.setdefault(song_id, set()).add(size)
    return sizes


def remote_audio_size(client, song_id):
    """Use HEAD only; no audio body is transferred."""
    url = BASE + "/api/download-song?" + urlencode({"id": song_id})
    for attempt in range(client.retries + 1):
        try:
            with client.open(url, method="HEAD") as response:
                content_type = response.headers.get_content_type()
                if content_type in {"text/html", "application/json", "text/plain"}:
                    raise RuntimeError(f"Unexpected {content_type} during update check")
                value = response.headers.get("Content-Length", "")
                return int(value) if value.isdigit() and int(value) > 0 else None
        except (HTTPError, URLError, TimeoutError, ConnectionError,
                http.client.IncompleteRead) as error:
            if (isinstance(error, HTTPError) and error.code not in TRANSIENT
                    or attempt == client.retries):
                if isinstance(error, HTTPError):
                    error.close()
                raise
            client.backoff(attempt, error)


def check_remote_sizes(client, songs, history, output, workers):
    """Check completed tracks with known local sizes, using bounded concurrent HEADs."""
    sizes = local_audio_sizes(songs, history, output)
    candidates = [str(song["id"]) for song in songs if str(song["id"]) in history.completed["audio"]]
    checkable = [song_id for song_id in candidates if sizes.get(song_id)]
    unavailable = len(candidates) - len(checkable)
    if unavailable:
        print(f"Cannot compare {unavailable} completed tracks: no local or recorded size.", file=sys.stderr)
    print(f"Checking remote sizes for {len(checkable)} tracks; no audio is downloaded during this check.", flush=True)
    changed, errors, pending = set(), [], {}
    remaining = iter(checkable)
    executor = ThreadPoolExecutor(max_workers=workers)
    checked, exhausted = 0, False
    try:
        while pending or not exhausted:
            while not exhausted and len(pending) < workers * 2:
                song_id = next(remaining, None)
                if song_id is None:
                    exhausted = True
                    break
                pending[executor.submit(remote_audio_size, client, song_id)] = song_id
            if not pending:
                break
            done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for future in done:
                song_id = pending.pop(future)
                try:
                    remote_size = future.result()
                    if remote_size is None:
                        errors.append(song_id)
                        print(f"Cannot check ID {song_id}: server gave no usable size.", file=sys.stderr)
                    elif remote_size not in sizes[song_id]:
                        changed.add(song_id)
                except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
                    errors.append(song_id)
                    print(f"Cannot check ID {song_id}: {error}", file=sys.stderr)
                checked += 1
                if checked % 50 == 0 or checked == len(checkable):
                    print(f"Checked {checked}/{len(checkable)} tracks; {len(changed)} size changes found.", flush=True)
    except BaseException:
        client.stop.set()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return changed, unavailable + len(errors)


def run(args):
    if args.sync_history:
        try:
            songs = json.loads((args.output / "catalog.json").read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RuntimeError("No catalog.json in the output folder. Run with --list-only first.") from error
        if (not isinstance(songs, list) or not songs
                or any(not isinstance(song, dict) or song.get("id") is None for song in songs)):
            raise RuntimeError("Invalid local catalog.json; refresh it with --list-only.")
        history = DownloadHistory(args.history)
        found = history.scan(songs, args.output, allow_untracked_names=True)
        if not found["audio"]:
            raise RuntimeError("No valid completed songs matched the local catalog. Check --output "
                               "and keep original filenames until history is recorded.")
        history.seed_sources(songs, {})
        history.save()
        print(f"Recorded {found['audio']} songs and {found['image']} artwork files. History: {args.history}")
        return 0
    failure_path = args.output / "failures.json"
    failures = {} if args.list_only else load_failures(failure_path, args.retry_failed)
    if args.retry_failed and not failures:
        print("No failed files to retry.")
        return 0
    history = None if args.list_only else DownloadHistory(args.history)
    previous = previous_catalog_sources(args.output / "catalog.json") if history else {}
    client = Client(args.delay, args.retries, args.timeout)
    songs = catalog(client)
    selected = select_songs(songs, args, failures)
    if history:
        recover_failure_filenames(songs, failures, history, args.output)
        history.filenames = allocate_filenames(songs, history.filenames, args.output)
        seeded = history.seed_sources(songs, previous)
        if seeded:
            print(f"Initialized update tracking for {seeded} existing tracks from available catalog data. "
                  "Earlier changes may not be detectable.", flush=True)
        # Save the baseline before replacing the old catalog, even for an empty selection.
        history.save()
    save_json(args.output / "catalog.json", songs)
    print(f"Catalog: {len(songs)} tracks; selected: {len(selected)}. Saved to {args.output / 'catalog.json'}")
    if args.list_only:
        for song in selected:
            print(f"{song['id']} | {song.get('title', 'Untitled')} | {song.get('author', 'Unknown')} "
                  f"| {', '.join(song.get('vault_categories', []))}")
        return 0
    if not selected:
        print("No tracks matched the selected filters.")
        return 1 if args.retry_failed and failures else 0
    history.scan(songs, args.output, pending_failures=failures)
    history.seed_sources(songs, previous)
    size_changes, unchecked = (check_remote_sizes(client, selected, history, args.output, args.workers)
                               if args.check_updates else (set(), 0))
    jobs, skipped = [], {"audio": 0, "image": 0}
    for job in build_jobs(selected, args, failures, history.filenames):
        old_source = history.audio_sources.get(job.song_id) if job.kind == "audio" else None
        if old_source and job.source and old_source != job.source:
            print(f"Updated audio detected for ID {job.song_id}; downloading the new version.", flush=True)
            job = replace(job, force=True, fresh=True)
        elif job.kind == "audio" and job.song_id in size_changes:
            print(f"Audio size changed for ID {job.song_id}; downloading the new version.", flush=True)
            job = replace(job, force=True, fresh=True)
        if job.song_id in history.completed[job.kind] and not job.force:
            skipped[job.kind] += 1
            # A preserved original is not proof that a requested replacement succeeded.
            if not failures.get(job.key, {}).get("force"):
                failures.pop(job.key, None)
        else:
            jobs.append(job)
    print(f"Downloading {len(jobs)} files with {args.workers} workers. History: {args.history}")
    result = run_downloads(client, jobs, history, failure_path, failures, args.workers, skipped)
    filtered = args.song_id or args.search or args.track_type != "all" or args.limit
    return 1 if unchecked or args.retry_failed and not filtered and failures else result


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--output", type=Path, default=Path.home() / "Downloads" / "juicewrldvault",
                        help="Download folder (default: ~/Downloads/juicewrldvault)")
    parser.add_argument("--artwork", action="store_true", help="Also download cover images")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list-only", action="store_true", help="Save full catalog and print matching tracks without media downloads")
    modes.add_argument("--sync-history", action="store_true", help="Record verified local files without network access")
    modes.add_argument("--retry-failed", action="store_true", help="Retry only files in the output folder's failures.json")
    parser.add_argument("--history", type=Path,
                        default=Path.home() / ".juicewrldvault" / "downloaded_songs.json",
                        help="History file (default: ~/.juicewrldvault/downloaded_songs.json)")
    parser.add_argument("--search", help="Case-insensitive substring in track title or author")
    parser.add_argument("--song-id", action="append", help="Download an exact catalog ID; repeat for multiple IDs")
    parser.add_argument("--type", dest="track_type", choices=("all", "songs", "raw", "instrumental"),
                        default="all", help="Filter using catalog group membership (default: all)")
    parser.add_argument("--redownload", action="append", metavar="ID",
                        help="Fetch an ID again, ignoring history; repeat for multiple IDs")
    parser.add_argument("--check-updates", action="store_true",
                        help="Also compare remote sizes of existing tracks (one HEAD request per track)")
    parser.add_argument("--limit", type=int, help="Process the first N matching tracks")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent downloads, 1-32 (default: 6)")
    parser.add_argument("--delay", type=float, default=0.1, help="Shared request-start spacing in seconds (default: 0.1)")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=90, help="Socket timeout in seconds (default: 90)")
    args = parser.parse_args()
    if (not math.isfinite(args.delay) or args.delay < 0 or args.retries < 0
            or not math.isfinite(args.timeout) or args.timeout <= 0
            or args.limit is not None and args.limit < 1 or not 1 <= args.workers <= 32):
        parser.error("delay must be finite and nonnegative; retries must be nonnegative; "
                     "timeout must be finite and positive; limit must be positive; workers must be 1-32")
    if args.redownload and (args.list_only or args.sync_history or args.retry_failed):
        parser.error("--redownload cannot be combined with --list-only, --sync-history or --retry-failed")
    if args.sync_history and (args.search or args.song_id or args.track_type != "all" or args.limit):
        parser.error("--sync-history cannot be combined with selection filters")
    if args.check_updates and (args.list_only or args.sync_history or args.retry_failed or args.redownload):
        parser.error("--check-updates cannot be combined with --list-only, --sync-history, --retry-failed or --redownload")
    args.output = args.output.expanduser().resolve()
    args.history = args.history.expanduser().resolve()
    reserved = {args.output / name for name in (
        "catalog.json", "catalog.json.tmp", "failures.json", "failures.json.tmp", ".download.lock")}
    media_roots = {(args.output / name).resolve() for name in ("songs", "artwork")}
    history_paths = {args.history, Path(str(args.history) + ".tmp"), Path(str(args.history) + ".lock")}
    if (history_paths & reserved or args.history == args.output or args.history in media_roots
            or any(root in args.history.parents for root in media_roots)):
        parser.error("Choose a --history path separate from catalog, failures, locks and media folders")
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=args.output):
            pass
    except OSError as error:
        raise RuntimeError(f"Cannot create or write to download folder '{args.output}'. "
                           f"Choose a writable folder with --output. System reported: {error}") from error
    print(f"Download folder: {args.output}", flush=True)
    locks = {args.output / ".download.lock"}
    if not args.list_only:
        locks.add(Path(str(args.history) + ".lock"))
    with ExitStack() as stack:
        for path in sorted(locks, key=str):
            stack.enter_context(RunLock(path))
        return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Rerun the same command to resume or use --retry-failed.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
