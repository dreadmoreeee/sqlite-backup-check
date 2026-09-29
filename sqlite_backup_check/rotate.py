"""Grandfather-father-son rotation, pure calendar logic plus file deletion."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def select_keep(
    stamps: list[datetime], daily: int, weekly: int, monthly: int
) -> set[datetime]:
    """Return the timestamps to keep.

    Daily: the newest backup of each of the last `daily` calendar days that have
    backups. Weekly: the newest backup of each of the last `weekly` ISO weeks.
    Monthly: the newest backup of each of the last `monthly` calendar months.
    A backup kept by any tier is kept.
    """
    if min(daily, weekly, monthly) < 0:
        raise ValueError("keep counts must be >= 0")
    ordered = sorted(set(stamps), reverse=True)  # newest first
    keep: set[datetime] = set()

    def tier(keyfunc, count: int) -> None:
        seen: list = []
        for s in ordered:  # first hit per bucket is the newest in it
            k = keyfunc(s)
            if k in seen:
                continue
            if len(seen) >= count:
                break
            seen.append(k)
            keep.add(s)

    tier(lambda s: s.date(), daily)
    tier(lambda s: tuple(s.isocalendar())[:2], weekly)
    tier(lambda s: (s.year, s.month), monthly)
    return keep


def rotate(directory, daily: int = 7, weekly: int = 4, monthly: int = 6, dry_run: bool = False):
    """Delete backups outside the retention policy. Returns (kept, removed) paths."""
    from .backup import list_backups

    entries = list_backups(directory)
    by_stamp: dict[datetime, list] = {}
    for e in entries:
        by_stamp.setdefault(e.created, []).append(e)
    keep = select_keep(list(by_stamp), daily, weekly, monthly)
    kept, removed = [], []
    for e in entries:
        (kept if e.created in keep else removed).append(e)
    if not dry_run:
        for e in removed:
            Path(e.path).unlink(missing_ok=True)
            Path(e.manifest_path).unlink(missing_ok=True)
    return kept, removed
