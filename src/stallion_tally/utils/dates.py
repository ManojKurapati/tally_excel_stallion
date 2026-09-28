"""Date helpers shared by the Tally layer and the sync layer."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")


def to_tally_date(value: date) -> str:
    """Tally expects dates as YYYYMMDD."""
    return value.strftime("%Y%m%d")


def date_windows(start: date, end: date, days: int) -> Iterator[tuple[date, date]]:
    """Split [start, end] (inclusive) into consecutive windows of at most `days` days."""
    if days < 1:
        raise ValueError("days must be >= 1")
    if end < start:
        return
    current = start
    while current <= end:
        window_end = min(current + timedelta(days=days - 1), end)
        yield current, window_end
        current = window_end + timedelta(days=1)


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value.strip())
