from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


_LOCAL_UTC_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?\s+UTC([+-]\d{1,2})(?::?(\d{2}))?$"
)


def parse_datetime_utc(value: object) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    local_match = _LOCAL_UTC_RE.match(raw)
    if local_match:
        date_part, hour, minute, second, offset_hours, offset_minutes = local_match.groups()
        offset_sign = 1 if offset_hours.startswith("+") else -1
        hours = abs(int(offset_hours))
        minutes = int(offset_minutes or "0")
        offset = timezone(offset_sign * timedelta(hours=hours, minutes=minutes))
        local_dt = datetime.fromisoformat(
            f"{date_part}T{hour}:{minute}:{second or '00'}"
        ).replace(tzinfo=offset)
        return local_dt.astimezone(timezone.utc)

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_kickoff_utc(
    *,
    kickoff_utc: object | None = None,
    kickoff_local: object | None = None,
) -> datetime | None:
    return parse_datetime_utc(kickoff_utc) or parse_datetime_utc(kickoff_local)


def format_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")
