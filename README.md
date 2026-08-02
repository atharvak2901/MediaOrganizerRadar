# move-series

A conservative Python 3 utility for importing one completed TV-series release into a Jellyfin library on a Raspberry Pi. It performs the move on the Pi, so it does not copy video through the phone or Wi-Fi.

## Safety rules

- Only accepts a release folder below `/mnt/media/Downloads/Series` or `/mnt/media/Downloads/incomplete`.
- Only writes below `/mnt/media/Media/Series`.
- Moves the **contents** of the release folder, not the outer release folder.
- Never overwrites a destination item. Existing names are skipped and left in the source folder.
- Refuses to run when source and destination are on different filesystems. That avoids a slow copy-based move.
- Removes the release folder only if it is empty.
- Starts with a dry run. No `rm -rf`, no recursive deletion, and no third-party Python packages.

## Install on the Raspberry Pi

Copy this entire folder to `/home/akhomehub/scripts/move-series` on the Pi. Then run:

```bash
mkdir -p /home/akhomehub/scripts/move-series
cd /home/akhomehub/scripts/move-series
chmod 700 move_series.py
python3 --version
```

Python 3.10 or newer is recommended. Edit `config.json` only if your folder paths or username differ.

## First test: dry run

Use a completed release folder explicitly. Quotes are important for spaces and brackets.

```bash
cd /home/akhomehub/scripts/move-series
python3 move_series.py --dry-run "/mnt/media/Downloads/Series/The.Big.Bang.Theory.S06.1080p.BluRay.x264-SHORTBREHD [PublicHD]"
```

Read the output. It should show the parsed show name, season, selected existing library folder (if any), and proposed destination. A dry run changes nothing.

If it looks correct, run the exact command again without `--dry-run`.

```bash
python3 move_series.py "/mnt/media/Downloads/Series/The.Big.Bang.Theory.S06.1080p.BluRay.x264-SHORTBREHD [PublicHD]"
```

## Release-name formats supported in V1

- `Show.S01E01...`
- `Show.S01...`
- `Show.Season.1...`
- `Show.Series.1...`
- `Show.1x01...`

It extracts the show name from text before the first season marker. It then searches existing library folders using case-insensitive matching that ignores punctuation and an initial `The`. It does **not** use fuzzy matching: a wrong match is more dangerous than making a new folder.

## Manual newest-folder mode

Use this only when you truly want the newest directory. It searches `Downloads/Series` first and then `Downloads/incomplete`.

```bash
python3 move_series.py --dry-run --latest
python3 move_series.py --latest
```

## qBittorrent automation

Do this only after several successful manual tests.

In qBittorrent settings, enable **Run external program on torrent completion**, then use:

```text
python3 /home/akhomehub/scripts/move-series/move_series.py --config /home/akhomehub/scripts/move-series/config.json --preflight "%F"
```

`--preflight` first runs the complete dry-run validation in the same process. Only when it succeeds does the script perform the real local move. If validation fails, it exits safely without moving anything.

`%F` must resolve to the completed content **directory**. qBittorrent versions differ, so test it with a small TV release first and inspect `/home/akhomehub/scripts/logs/move_series.log`. Do not enable this for movies or other download categories: V1 is for one TV-season release directory only.

## Jellyfin refresh

Jellyfin often detects filesystem changes on its own. The script can also request a refresh, but its API requires an API key. Leave `jellyfin_api_key` empty to safely skip that request.

To use it, create a Jellyfin API key in the Jellyfin dashboard, paste it into `config.json`, and keep `refresh_jellyfin` as `true`. The move is never rolled back or marked failed if Jellyfin is unavailable.

To test importing without the refresh request:

```bash
python3 move_series.py --no-refresh "/path/to/release"
```

## Logs and recovery

The log is at `/home/akhomehub/scripts/logs/move_series.log` and rotates automatically. If an item already exists in the destination, the script skips it and retains it in the source release folder. Rename or review it manually before running again.

If interrupted, files that already moved are valid in their destination; remaining files stay in the source. Re-run with the same source path after reviewing the log.

## Useful commands

```bash
# Syntax/compile check; does not run the importer
python3 -m py_compile move_series.py

# Show all options
python3 move_series.py --help

# Inspect the last log entries
tail -n 60 /home/akhomehub/scripts/logs/move_series.log
```
