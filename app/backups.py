from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import config, docker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupSummary:
    count: int
    latest: datetime | None


def _automatic_backups() -> list[tuple[Path, float]]:
    config.ensure_backup_directory()
    backups: list[tuple[Path, float]] = []
    for path in config.BACKUP_DIR.glob("automatic_*.dump"):
        try:
            backups.append((path, path.stat().st_mtime))
        except OSError:
            continue
    return backups


def summary() -> BackupSummary:
    backup_files = _automatic_backups()
    if not backup_files:
        return BackupSummary(0, None)
    latest_timestamp = max(timestamp for _, timestamp in backup_files)
    return BackupSummary(
        len(backup_files),
        datetime.fromtimestamp(latest_timestamp).astimezone(),
    )


def due(interval_minutes: int, last_attempt: datetime | None = None) -> bool:
    latest = summary().latest
    reference = max(
        (value for value in (latest, last_attempt) if value is not None),
        default=None,
    )
    if reference is None:
        return True
    return datetime.now().astimezone() - reference >= timedelta(minutes=interval_minutes)


def create(retention_days: int) -> Path:
    destination = docker.automatic_backup_path("automatic")
    docker.export_database(destination)
    prune(retention_days, preserve=destination)
    return destination


def prune(retention_days: int, preserve: Path | None = None) -> None:
    cutoff = datetime.now().astimezone() - timedelta(days=retention_days)
    for path, timestamp in _automatic_backups():
        if path == preserve:
            continue
        modified = datetime.fromtimestamp(timestamp).astimezone()
        if modified >= cutoff:
            continue
        try:
            path.unlink()
            logger.info("Removed expired automatic backup %s", path)
        except OSError as exc:
            logger.warning("Could not remove expired automatic backup %s: %s", path, exc)
