# Juice WRLD Vault Downloader

A standalone Python script for downloading the music catalog publicly exposed by
[Juice WRLD Vault](https://juicewrldvault.com/), with optional cover images.
It saves catalog metadata, retries temporary network failures, and skips files
it has already downloaded. No account, API key, or additional Python packages
are required.

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

The default output folder is `Downloads/juicewrldvault` under your home folder.
The script prints the absolute location at startup. A large catalog can take
considerable time and disk space; the script does not estimate its total size.

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
| `--list-only` | Saves the full catalog without downloading music or images | Off |
| `--limit N` | Processes only the first N catalog tracks | All tracks |
| `--delay SECONDS` | Pauses before each HTTP request | `0.5` |
| `--retries N` | Sets retry attempts after the initial request | `4` |
| `--timeout SECONDS` | Sets the socket timeout for each request | `90` |
| `--version` | Prints the script version and exits | |
| `--help` | Prints usage and exits | |

`--limit` still fetches and saves the complete catalog before downloading the
selected tracks. Artwork is saved separately and is not embedded into MP3 tags.
Requests run sequentially. Retry delays are additional to `--delay`.

## Output and resuming

```text
juicewrldvault/
  catalog.json       Full track metadata returned by the site
  failures.json      Failed files from the most recently completed run
  songs/            MP3 files with track IDs to distinguish versions
  artwork/          Cover images, when --artwork is used
```

Press **Ctrl+C** to stop. Run the same command with the same output folder to
continue. Completed, nonempty files are skipped. Interrupted files remain as
`.part` files with `.part.json` metadata. They resume when the server provides
the required range and validation headers; otherwise they restart safely.
Keep these files if you plan to resume.

Existing files are checked for presence and nonzero size, not checked against a
remote checksum. To replace a corrupted completed file or fetch an updated
version with the same filename, remove that file and rerun. Avoid running two
copies of the script against the same output folder at the same time.

`catalog.json` is refreshed each run. `failures.json` is written when a download
run finishes; it may be absent or reflect an earlier run after an interruption
or a `--list-only` run. Rerunning retries failed files while skipping completed
ones. Exit codes are `0` for success, `1` for download/runtime failures, `2` for
invalid arguments, and `130` for Ctrl+C.

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
