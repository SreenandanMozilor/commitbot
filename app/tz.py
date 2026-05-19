"""Tiny timezone utilities.

The DB stores everything in UTC. Rendering and input parsing happen in the
user's preferred zone (User.tz, an IANA name). This module is the only place
that knows about the conversion so the rest of the code stays UTC-only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Curated suggestions surfaced in the settings <datalist>. A free-text field
# still accepts any valid IANA name, so this list doesn't have to be exhaustive.
COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Asia/Dubai",
    "Asia/Shanghai",
    "Australia/Sydney",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Pacific/Auckland",
)


def safe_zone(tz_name: Optional[str]) -> ZoneInfo:
    """Return a ZoneInfo, falling back to UTC if the name is missing or unknown."""
    if not tz_name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def validate_zone(tz_name: str) -> str:
    """Raise ValueError if the name isn't a valid IANA zone."""
    name = (tz_name or "").strip()
    if not name:
        return "UTC"
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"Unknown timezone: {name!r}") from e
    return name


def to_local(dt: Optional[datetime], tz_name: Optional[str]) -> Optional[datetime]:
    """Convert a stored datetime (UTC, possibly naive) into the user's zone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(safe_zone(tz_name))


def local_input_to_utc(value: Optional[str], tz_name: Optional[str]) -> Optional[datetime]:
    """Parse an HTML datetime-local / ISO string entered in the user's tz and
    return an aware UTC datetime. Returns None for blank input.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    return parsed.replace(tzinfo=safe_zone(tz_name)).astimezone(timezone.utc)


def format_deadline(dt: Optional[datetime], tz_name: Optional[str]) -> str:
    """Human-readable deadline in the user's tz. Used by Slack rendering."""
    if dt is None:
        return "no deadline"
    local = to_local(dt, tz_name)
    return local.strftime("%a %b %d, %H:%M %Z")
