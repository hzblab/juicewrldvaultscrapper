# Juice WRLD Vault Downloader

A standalone Python script for downloading the music catalog publicly exposed by
[Juice WRLD Vault](https://juicewrldvault.com/), with optional cover images.
It saves catalog metadata, retries temporary network failures, and skips files
it has already downloaded. No account, API key, or additional Python packages
are required.

Version **1.6.0** uses title-and-artist filenames without appended track IDs.
It saves MP3, RIFF/WAV, FLAC, Ogg/Opus, and MP4/M4A in their original formats
and preserves their embedded metadata.

This is an unofficial tool. It downloads public catalog tracks, rather than
mirroring the entire website or discovering hidden files. The site's endpoints
can change, so catalog size and availability can vary.

## Requirements

- Python **3.10 or newer**, available from [python.org](https://www.python.org/).
- An internet connection and a writable output folder with enough free space.
- Windows, macOS, or Linux. Live download testing has been performed on Windows;
  macOS and Linux have not been tested directly.

Only download content you have permission to download, and respect the site's
terms and request limits. The script does not bypass authentication or access
restrictions.

## Quick start

Download `download_juicewrldvault.py` and open a terminal in the folder containing
it. On Windows PowerShell, check Python and try one track first:

```powershell
py -3 --version
py -3 .\download_juicewrldvault.py --limit 1 --artwork
```

If `py` is unavailable, use `python` or the full path to your Python executable.
Check that the version shown is at least 3.10.

On macOS or Linux:

```sh
python3 --version
python3 download_juicewrldvault.py --limit 1 --artwork
```

To download the full public catalog, remove `--limit 1`:

```powershell
py -3 .\download_juicewrldvault.py --artwork
```

The script supports MP3, RIFF/WAV, FLAC, Ogg/Opus, and MP4/M4A. If the site
supplies another format, it is reported as unsupported rather than saved with
a misleading extension.

The default output folder is `Downloads/juicewrldvault` under your home folder.
The script prints the absolute location at startup. A large catalog can take
considerable time and disk space; the script does not estimate its total size.

## Find and select tracks

List matching tracks and their catalog IDs before downloading:

```powershell
py -3 .\download_juicewrldvault.py --list-only --search "night"
py -3 .\download_juicewrldvault.py --list-only --type instrumental
```

`--search` matches a case-insensitive substring in the title or author.
`--type` accepts `all`, `songs`, `raw`, or `instrumental`, based on membership in
the site's catalog pools. A track can belong to more than one pool.
`--list-only` always saves the complete catalog and prints only the matches.

Copy an ID from that listing in place of `TRACK_ID`, or combine filters:

```powershell
py -3 .\download_juicewrldvault.py --song-id TRACK_ID --artwork
py -3 .\download_juicewrldvault.py --search "night" --type raw --limit 5
```

Repeat `--song-id` to choose multiple IDs. Exact IDs, text, and type filters
intersect; `--limit` applies after filtering in catalog order. Unknown explicit
IDs cause an error before any media downloads. Filenames use the catalog's title
and artist, such as `Song - Artist.wav`; duplicate names receive `(2)`, `(3)`,
and so on, rather than a track ID. Characters a filesystem cannot use are
replaced. The ID and the assigned filename stay in history so later runs can
recognize the song and keep duplicate numbering stable. Assigned filenames do
not automatically change when a catalog title changes. The full catalog metadata
remains in `catalog.json`. Audio bytes and
embedded tags are left exactly as supplied by the server, so the original tags
may differ from the catalog title or artist.

## Retry or replace files

Retry just the unfinished files recorded in the chosen output folder:

```powershell
py -3 .\download_juicewrldvault.py --output "D:\Music\Juice Vault" --retry-failed
```

This also retries failed artwork without requiring `--artwork`, and resumes
partial files when the server supports it. URLs and destinations are rebuilt
from the current catalog, so reports from older versions and moved folders can
be used. Add `--song-id`, `--search`, `--type`, or `--limit` to narrow retries;
unselected failures stay in the report. IDs no longer available in the catalog
are reported and retained while other available files are retried.

To fetch a specific track afresh even if it is already in history:

```powershell
py -3 .\download_juicewrldvault.py --redownload TRACK_ID
py -3 .\download_juicewrldvault.py --redownload TRACK_ID --artwork
```

Repeat `--redownload` for multiple IDs. It bypasses history and discards that
selection's previous partial transfer. Add `--artwork` to replace its cover too.
An existing completed file remains until its replacement passes verification;
failed replacements stay in `failures.json` for `--retry-failed`. The replacement
uses the current catalog filename; an older copy with a different title is kept.
If both ID flags are used, their IDs form the selection, and only IDs passed to
`--redownload` are forced. It cannot be combined with `--retry-failed`,
`--list-only`, or `--sync-history`.

## Detect updated audio

On a normal download run, the script compares each completed track's current
`song_path` with its saved source path. If the catalog path changes, the track
is downloaded again even when its ID is already in history. The old completed
file remains in place until the replacement passes verification. If you moved
the old file out of `songs/`, the new copy goes into `songs/` and the moved
copy is left alone. A failed replacement keeps the old source in history so a
later run can try again.

Older history contains no source paths. On the first normal run after upgrading,
the script uses the previous `catalog.json` as a best-effort baseline; where no
previous path is available, it uses the current path. Run a normal download
command first, before `--list-only` refreshes the old catalog. Changes that
occurred before that baseline cannot be reliably identified from history alone.

To also compare remote file sizes with known local or recorded sizes, add
`--check-updates` to a normal download command:

```powershell
py -3 .\download_juicewrldvault.py --output "D:\Music\Juice Vault" --check-updates
```

This uses one HTTP HEAD request per completed selected track with a known size;
it does not download audio merely to compare it. It checks files with original
known filenames or legacy ID suffixes in the output folder and its `songs/` subfolder, as well as sizes
stored in history. It takes at least about 6½ minutes for 3,900 tracks with the
default 0.1-second request spacing, plus network and download time. Missing
sizes or failed checks are reported, and the exit code is `1` if any could not
be checked. You can narrow the check with `--search`, `--type`, `--song-id`, or
`--limit`.

A path and size are useful change signals, not content hashes: if the site
changes audio without changing its path or size, neither check can prove the
file changed. Use `--redownload ID` for a track you know has changed. The
download endpoint may not provide an ETag or modification date.

## Choose an output folder

```powershell
py -3 .\download_juicewrldvault.py --output "D:\Music\Juice Vault" --artwork
```

```sh
python3 download_juicewrldvault.py --output "$HOME/Music/Juice Vault" --artwork
```

Relative output paths are resolved from the terminal's current folder. Quote
paths containing spaces. Custom locations help when Documents or other folders
are restricted by the operating system.

## Options

| Option | What it does | Default |
| --- | --- | --- |
| `--output PATH` | Sets the output folder | `~/Downloads/juicewrldvault` |
| `--artwork` | Downloads each track's cover as a separate image | Off |
| `--list-only` | Saves the full catalog and prints matching tracks and IDs without downloading media | Off |
| `--history PATH` | Sets the persistent download history file | `~/.juicewrldvault/downloaded_songs.json` |
| `--sync-history` | Records existing files from the local catalog and exits without network access | Off |
| `--search TEXT` | Matches title or author, ignoring case | All tracks |
| `--song-id ID` | Selects an exact ID; may be repeated | All tracks |
| `--type TYPE` | Filters by `all`, `songs`, `raw`, or `instrumental` catalog pools | `all` |
| `--retry-failed` | Retries only unfinished files in `failures.json` | Off |
| `--redownload ID` | Fetches an ID afresh, ignoring history; may be repeated | Off |
| `--check-updates` | Compares remote and known local sizes of completed tracks | Off |
| `--limit N` | Processes only the first N matching tracks | All matches |
| `--workers N` | Sets simultaneous file downloads (1–32) | `6` |
| `--delay SECONDS` | Sets minimum request-start spacing across all workers | `0.1` |
| `--retries N` | Sets retry attempts after the initial request | `4` |
| `--timeout SECONDS` | Sets the socket timeout for each request | `90` |
| `--version` | Prints the script version and exits | |
| `--help` | Prints usage and exits | |

`--limit` still fetches and saves the complete catalog before downloading the
selected tracks. Artwork is saved separately and is not embedded into MP3 tags.
Files download concurrently. All workers share request pacing and a cooldown
after HTTP 429/503 responses; retry waits also respect `Retry-After` headers.
Use `--workers 1` for sequential downloads, or adjust concurrency with:

```powershell
py -3 .\download_juicewrldvault.py --workers 8 --delay 0.1
```

Progress shows separate song/artwork downloaded, skipped, and failed counts,
average transferred MiB/s, elapsed time, and an estimated time remaining.
The estimate uses completed-file timing and starts after the first completion;
file sizes and retries can make it fluctuate. The transfer rate includes bytes
sent again during retries. The elapsed timer covers the media phase.

## Output and resuming

```text
juicewrldvault/
  catalog.json       Full track metadata returned by the site
  failures.json      Unresolved failed, queued, or interrupted files
  .download.lock     Output-folder lock (leave this file in place)
  songs/            Audio files named with title and artist
  artwork/          Cover images, when --artwork is used
```

Press **Ctrl+C** to stop. Run the same command with the same output folder to
continue. Completed IDs are saved to a persistent history file outside the media
folder, so moving or renaming downloaded music does not cause it to download
again. Existing files matched by saved filenames or legacy ID suffixes are
checked and valid ones are recorded automatically. Interrupted files remain as
`.part` files with `.part.json` metadata. They resume when the server provides
the required range and validation headers; otherwise they restart safely.
Keep these files if you plan to resume.

Audio checks cover nonempty content, HTTP sizes when supplied, MP3 frames,
RIFF/WAV chunks, FLAC metadata, Ogg/Opus pages, and MP4/M4A container boxes.
MP3 support includes MPEG-1/2/2.5, variable bitrates, ID3 tags, and APE tags.
Free-format MP3 bitrates and RF64 WAV files are unsupported. The detected bytes
determine the format; the catalog extension distinguishes MP4 from M4A when
both use the same container. Audio is not transcoded. Previously downloaded WAV
audio mistakenly named `.mp3` can be recognized when scanning history, but is
not renamed automatically. Artwork uses basic format
signatures/end markers or container sizes. Newly recorded file sizes are also
kept in history to detect changed local files. Invalid local copies lose their
completion record and can be fetched again. Legacy history files remain readable.
These checks do not decode audio/images or compare against remote checksums;
they cannot detect every kind of corruption. Moved files remain recorded because
their contents are no longer available to inspect in the output folder.

The output folder and history each have a process lock, so a second copy cannot
write to either resource while the first is running, even with a different output
or history setting. Locks release automatically after exit or a crash. Do not
delete lock files while a downloader is running; close the other process first.

Before moving music, record existing files using the saved `catalog.json` and
the original filenames:

```powershell
py -3 .\download_juicewrldvault.py --output "D:\Music\Juice Vault" --sync-history
```

This operation is offline and requires at least one matching completed song.
If filename mappings are missing, unambiguous title-and-artist names and older
ID-suffixed names can be matched. Duplicate titles without a saved mapping are
ambiguous and are not guessed. Keep and back up the history file: it contains
the song IDs and filename mappings needed for reliable matching after files move.
Keep the history file after moving the music. Use the same `--history PATH` for
future runs if you choose a custom location. `--sync-history` cannot be combined
with selection filters. The history path must be separate from catalog,
failure-report, lock, temporary, and media paths.

`catalog.json` is refreshed on catalog-fetching runs. `failures.json` is saved
before work starts and after each result, including on interruption. Successful
files are removed; unrelated unresolved entries remain. `--list-only` and
`--sync-history` leave that report alone. Exit codes are `0` for a successful
selection, `1` for download/runtime failures (or unresolved entries after an
unfiltered retry), `2` for invalid arguments, and `130` for Ctrl+C.

## Troubleshooting

- **Python is not found:** Install Python 3.10+ and reopen the terminal. On
  Windows, try `py -3`, `python`, or the full executable path.
- **Cannot create or write to the output folder / WinError 2:** Use `--output`
  with a writable location. The default Downloads folder avoids using Documents.
- **HTTP 429 or repeated timeouts:** Increase `--delay`, for example
  `--delay 2 --timeout 180`, or try again later. Temporary failures are retried.
- **HTTP 401/403:** Access is restricted. The script cannot bypass that restriction.
- **Catalog/metadata errors:** The site may be temporarily unavailable or its API
  may have changed. Retry later; persistent changes require updating the script.
- **Disk full or very long paths:** Free disk space or choose a shorter output
  path, then rerun with the same output folder.
- **Cannot lock output/history:** Close the other downloader that uses that
  folder or history. A leftover lock file itself does not block a new run.
- **Invalid/unsupported audio or artwork:** The response may be incomplete, an
  error page, or a format the script does not support. Review `failures.json` and try
  `--retry-failed` later; use `--redownload ID` for a fresh transfer.

## Offline tests

```sh
python -m unittest -v
```

Tests use synthetic media and mocked network responses, including concurrent
workers, partial resuming, rate-limit cooldowns, interruption recovery, and
separate processes contending for locks. They do not download music.
