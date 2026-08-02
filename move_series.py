#!/usr/bin/env python3
"""Safely import one completed TV-series release into a Jellyfin library.

This program is deliberately conservative.  It moves only the *contents* of
one completed release directory, never overwrites an existing destination
item, and refuses paths outside the configured download roots.

Run ``python3 move_series.py --help`` for usage.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import shutil
import sys
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_CONFIG = "config.json"
SEASON_PATTERNS = (
    re.compile(r"(?i)(?<![a-z0-9])s(?P<season>\d{1,2})(?:e\d{1,3})?(?![a-z0-9])"),
    re.compile(r"(?i)(?<![a-z0-9])(?:season|series)[ ._-]*(?P<season>\d{1,2})(?![a-z0-9])"),
    re.compile(r"(?i)(?<![a-z0-9])(?P<season>\d{1,2})x\d{1,3}(?![a-z0-9])"),
)
BAD_SHOW_TOKENS = {
    "1080p", "2160p", "720p", "480p", "bluray", "brrip", "webrip", "webdl",
    "web", "dl", "x264", "x265", "hevc", "h264", "h265", "hdr", "dv", "amzn",
    "nf", "proper", "repack", "complete", "rarbg", "yts",
}


class ImportErrorSafe(Exception):
    """An expected safety or validation failure; no traceback is needed."""


@dataclass(frozen=True)
class Config:
    downloads_series: Path
    downloads_incomplete: Path
    library_series: Path
    log_file: Path
    jellyfin_url: str
    jellyfin_api_key: str
    refresh_jellyfin: bool


@dataclass(frozen=True)
class Release:
    source: Path
    parsed_show: str
    season: int
    marker: str


def load_config(path: Path) -> Config:
    """Load and validate the small JSON configuration file."""
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImportErrorSafe(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ImportErrorSafe(f"Invalid JSON in {path}: {exc}") from exc

    required = ("downloads_series", "downloads_incomplete", "library_series", "log_file")
    missing = [key for key in required if not isinstance(raw.get(key), str) or not raw[key].strip()]
    if missing:
        raise ImportErrorSafe("Missing required configuration value(s): " + ", ".join(missing))

    return Config(
        downloads_series=Path(raw["downloads_series"]).expanduser(),
        downloads_incomplete=Path(raw["downloads_incomplete"]).expanduser(),
        library_series=Path(raw["library_series"]).expanduser(),
        log_file=Path(raw["log_file"]).expanduser(),
        jellyfin_url=str(raw.get("jellyfin_url", "http://localhost:8096")).rstrip("/"),
        jellyfin_api_key=str(raw.get("jellyfin_api_key", "")).strip(),
        refresh_jellyfin=bool(raw.get("refresh_jellyfin", True)),
    )


def make_logger(log_file: Path, verbose: bool) -> logging.Logger:
    """Log to the terminal and retain rotating on-disk diagnostics."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("move_series")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    file_handler = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024,
                                       backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def resolved(path: Path) -> Path:
    """Resolve symlinks and ``..`` before a safety comparison."""
    return path.resolve(strict=False)


def is_below(path: Path, root: Path) -> bool:
    try:
        resolved(path).relative_to(resolved(root))
        return True
    except ValueError:
        return False


def require_below(path: Path, roots: tuple[Path, ...], description: str) -> None:
    if not any(is_below(path, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ImportErrorSafe(f"Unsafe {description}: {path}\nAllowed root(s): {allowed}")


def normalise(text: str, *, ignore_leading_the: bool = False) -> str:
    """Normalise names for conservative matching, including ``&``/``and``."""
    text = text.lower()
    # Treat an ampersand in a library name as the word "and" in a release.
    # Example: "Key & Peele" matches "Key.and.Peele".
    text = text.replace("&", " and ")
    text = re.sub(r"[._-]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if ignore_leading_the:
        text = re.sub(r"^the\s+", "", text)
    return text.replace(" ", "")


def prettify(text: str) -> str:
    """Turn release separators into a human-readable folder name."""
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"-+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ._-")


def find_marker(name: str) -> re.Match[str] | None:
    for pattern in SEASON_PATTERNS:
        match = pattern.search(name)
        if match:
            return match
    return None


def parse_release(source: Path) -> Release:
    """Derive show and season from the release directory's own name.

    V1 intentionally refuses names without a recognizable season marker rather
    than guessing from episode files or metadata.
    """
    marker = find_marker(source.name)
    if marker is None:
        raise ImportErrorSafe(
            f"Could not find a season marker in '{source.name}'. "
            "Expected S01, S01E01, Season 1, Series 1, or 1x01."
        )
    show = prettify(source.name[:marker.start()])
    tokens = set(normalise(show).split())
    if not show or not re.search(r"[A-Za-z]", show):
        raise ImportErrorSafe(f"Unsafe parsed show name: '{show}'")
    # Evaluate words before compact normalisation, so 'WEB DL' is recognized.
    visible_tokens = {word.lower() for word in re.findall(r"[A-Za-z0-9]+", show)}
    if visible_tokens and visible_tokens.issubset(BAD_SHOW_TOKENS):
        raise ImportErrorSafe(f"Unsafe parsed show name: '{show}'")
    season = int(marker.group("season"))
    if not 0 <= season <= 99:
        raise ImportErrorSafe(f"Unsafe season number: {season}")
    return Release(source=source, parsed_show=show, season=season, marker=marker.group(0))


def choose_manual_source(config: Config) -> Path:
    """Use the newest immediate release directory, only when requested."""
    for root in (config.downloads_series, config.downloads_incomplete):
        if root.is_dir():
            candidates = [item for item in root.iterdir() if item.is_dir()]
            if candidates:
                return max(candidates, key=lambda item: item.stat().st_mtime)
    raise ImportErrorSafe("No release directory found in configured download folders.")


def resolve_source(source_arg: str | None, latest: bool, config: Config) -> Path:
    if source_arg and latest:
        raise ImportErrorSafe("Use either a source path or --latest, not both.")
    source = Path(source_arg).expanduser() if source_arg else choose_manual_source(config) if latest else None
    if source is None:
        raise ImportErrorSafe("Supply the completed release path, or explicitly use --latest.")
    source = resolved(source)
    if not source.is_dir():
        raise ImportErrorSafe(f"Source must be a release directory: {source}")
    require_below(source, (config.downloads_series, config.downloads_incomplete), "source path")
    return source


def find_show_folder(release: Release, config: Config, logger: logging.Logger) -> Path:
    """Match an existing series folder exactly, ignoring punctuation and 'The'.

    This intentionally does not use fuzzy matching.  A wrong match is worse
    than creating a new folder, and aliases can be added in a later version.
    """
    library = resolved(config.library_series)
    if not library.is_dir():
        raise ImportErrorSafe(f"Series library folder does not exist: {library}")
    wanted = normalise(release.parsed_show)
    wanted_without_the = normalise(release.parsed_show, ignore_leading_the=True)
    for folder in sorted((item for item in library.iterdir() if item.is_dir()), key=lambda p: p.name.lower()):
        candidate = normalise(folder.name)
        if candidate == wanted or candidate == wanted_without_the:
            logger.info("Matched existing show folder: %s", folder.name)
            return folder
    created = library / release.parsed_show
    logger.info("No existing show matched; new folder will be: %s", created.name)
    return created


def same_filesystem(source: Path, destination_parent: Path) -> bool:
    """Ensure a move remains a local rename rather than an expensive copy."""
    return source.stat().st_dev == destination_parent.stat().st_dev


def ensure_destination(destination: Path, config: Config, dry_run: bool, logger: logging.Logger) -> None:
    require_below(destination, (config.library_series,), "destination path")
    if destination.exists():
        if not destination.is_dir():
            raise ImportErrorSafe(f"Destination exists but is not a directory: {destination}")
        return
    if dry_run:
        logger.info("[DRY RUN] Would create destination: %s", destination)
        return
    destination.mkdir(parents=True, exist_ok=True)
    logger.info("Created destination: %s", destination)


@dataclass
class MoveSummary:
    moved: int = 0
    skipped: int = 0


def move_contents(release: Release, destination: Path, config: Config, dry_run: bool,
                  logger: logging.Logger) -> MoveSummary:
    """Move immediate source contents, including hidden items, without overwrite.

    Directories are moved intact.  This preserves release-provided subtitle or
    extras directories; V1 does not flatten arbitrary internal structures.
    """
    ensure_destination(destination, config, dry_run, logger)
    # During a dry run a new season folder deliberately does not exist yet.
    # The library root always exists (validated by find_show_folder), so it is
    # the stable mount-point check for both preview and real import.
    library_root = resolved(config.library_series)
    if not same_filesystem(release.source, library_root):
        raise ImportErrorSafe(
            "Source and destination are on different filesystems; refusing a "
            "copy-based move. Check that both paths are on the same media disk."
        )
    summary = MoveSummary()
    items = sorted(release.source.iterdir(), key=lambda item: item.name.casefold())
    if not items:
        logger.warning("Source is empty; nothing to move.")
        return summary
    for item in items:
        target = destination / item.name
        if target.exists() or target.is_symlink():
            summary.skipped += 1
            logger.warning("Skipped existing destination item: %s", target.name)
            continue
        if dry_run:
            summary.moved += 1
            logger.info("[DRY RUN] Would move: %s -> %s", item.name, destination)
            continue
        try:
            # Same filesystem was checked above, so rename is an atomic metadata
            # operation and never sends the media through the network.
            item.rename(target)
        except OSError as exc:
            raise ImportErrorSafe(f"Could not move '{item}' to '{target}': {exc}") from exc
        summary.moved += 1
        logger.info("Moved: %s", item.name)
    return summary


def remove_empty_source(source: Path, config: Config, dry_run: bool, logger: logging.Logger) -> None:
    """Remove only the now-empty release folder; never recursively delete."""
    require_below(source, (config.downloads_series, config.downloads_incomplete), "cleanup path")
    if any(source.iterdir()):
        logger.info("Source retained because it contains skipped/unmoved item(s): %s", source)
        return
    if dry_run:
        logger.info("[DRY RUN] Would remove empty source folder: %s", source)
        return
    source.rmdir()
    logger.info("Removed empty source folder: %s", source)


def refresh_jellyfin(config: Config, disabled: bool, logger: logging.Logger) -> None:
    """Request a library refresh only when configured with a Jellyfin API key."""
    if disabled or not config.refresh_jellyfin:
        logger.info("Jellyfin refresh disabled.")
        return
    if not config.jellyfin_api_key:
        logger.warning("Jellyfin refresh skipped: no jellyfin_api_key in config.json.")
        return
    request = Request(
        f"{config.jellyfin_url}/Library/Refresh",
        method="POST",
        headers={"X-Emby-Token": config.jellyfin_api_key},
    )
    try:
        with urlopen(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                logger.warning("Jellyfin refresh returned HTTP %s.", response.status)
                return
        logger.info("Jellyfin refresh requested.")
    except (URLError, OSError) as exc:
        logger.warning("Jellyfin refresh failed (media move remains successful): %s", exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", help="Completed release directory (qBittorrent: use %F).")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config.json (default: %(default)s).")
    parser.add_argument("--latest", action="store_true", help="Manual mode: use newest folder in Downloads/Series, then incomplete.")
    parser.add_argument("--dry-run", action="store_true", help="Log proposed changes without creating or moving anything.")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Validate the full dry-run plan first; move only if that plan succeeds.",
    )
    parser.add_argument("--no-refresh", action="store_true", help="Do not request a Jellyfin refresh.")
    parser.add_argument("--verbose", action="store_true", help="Show verbose terminal logging.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    # Config is intentionally resolved before logger creation so a bad config
    # cannot cause writes in an unexpected location.
    try:
        config = load_config(resolved(Path(args.config)))
        logger = make_logger(config.log_file, args.verbose)
        logger.info("%s", "=" * 72)
        logger.info("move_series started | dry_run=%s preflight=%s", args.dry_run, args.preflight)
        source = resolve_source(args.source, args.latest, config)
        release = parse_release(source)
        logger.info("Source: %s", release.source)
        logger.info("Parsed show: %s | season: %s | marker: %s", release.parsed_show,
                    release.season, release.marker)
        show_folder = find_show_folder(release, config, logger)
        destination = show_folder / f"Season {release.season}"
        logger.info("Destination: %s", destination)
        if args.preflight:
            # This uses exactly the same collision, containment and mount-point
            # checks as a live import, but never creates or moves anything.
            logger.info("Preflight started: validating without changing files.")
            planned = move_contents(release, destination, config, True, logger)
            logger.info("Preflight passed | would_move=%s would_skip=%s", planned.moved, planned.skipped)
            if args.dry_run:
                logger.info("Dry run completed after successful preflight.")
                return 0
        summary = move_contents(release, destination, config, args.dry_run, logger)
        logger.info("Move summary | moved=%s skipped=%s", summary.moved, summary.skipped)
        if not args.dry_run:
            remove_empty_source(release.source, config, args.dry_run, logger)
            if summary.moved:
                refresh_jellyfin(config, args.no_refresh, logger)
            else:
                logger.info("Jellyfin refresh skipped because nothing was moved.")
        logger.info("move_series completed successfully.")
        return 0
    except ImportErrorSafe as exc:
        # Logging might not be initialized if config parsing failed.
        logging.getLogger("move_series").error("Stopped safely: %s", exc)
        print(f"Stopped safely: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; items already moved remain safely in their new locations.", file=sys.stderr)
        return 130
    except Exception as exc:  # Unexpected error: retain traceback in the log.
        logging.getLogger("move_series").exception("Unexpected failure")
        print(f"Unexpected failure: {exc}. Check the log file.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
