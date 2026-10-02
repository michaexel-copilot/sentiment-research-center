"""Time helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime, matching the naive UTC timestamps stored in the DB."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
