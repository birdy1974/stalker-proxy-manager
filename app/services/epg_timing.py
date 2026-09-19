"""Source clock interpretation. Fixed corrections are applied only at output time."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

TimezoneMode = Literal["auto", "missing", "override"]


class SourceTiming(BaseModel):
    timezone_mode: TimezoneMode = "auto"
    timezone_name: str | None = Field(default=None, max_length=64)
    offset_minutes: StrictInt = Field(default=0, ge=-1440, le=1440)

    @field_validator("timezone_name")
    @classmethod
    def valid_zone(cls, value):
        value = value.strip() if value else None
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(
                    "Choose a valid IANA timezone, e.g. Europe/Amsterdam"
                ) from exc
        return value or None

    @model_validator(mode="after")
    def require_zone(self):
        if self.timezone_mode != "auto" and not self.timezone_name:
            raise ValueError("A source timezone is required for this mode")
        return self


DEFAULT = SourceTiming()


def timing_key(source, *, applied=False):
    prefix = "applied_" if applied else ""
    mode = getattr(source, prefix + "timezone_mode", None) or "auto"
    return mode, (
        getattr(source, prefix + "timezone_name", None) if mode != "auto" else None
    )


def timing_pending(source):
    return timing_key(source) != timing_key(source, applied=True)


@dataclass(frozen=True)
class ParsedTime:
    utc: datetime
    warning: str | None = None


def local_time(naive, zone):
    """Reject spring-forward holes; deterministically select the first autumn fold."""
    candidates = set()
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(zone).replace(tzinfo=None) == naive:
            candidates.add(candidate)
    if not candidates:
        raise ValueError("Nonexistent local time during the daylight-saving transition")
    return ParsedTime(
        min(candidates),
        "Ambiguous local time: first occurrence used" if len(candidates) > 1 else None,
    )


def parse_time(text, settings=DEFAULT, *, portal=False, default_timezone="UTC"):
    text = str(text or "").strip()
    mode, name = timing_key(settings)
    if portal:
        from ..portal.epg import parse_portal_ts

        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            try:
                return ParsedTime(
                    datetime.fromtimestamp(float(text), timezone.utc),
                    (
                        "Unix timestamps are absolute; timezone interpretation does not change them"
                        if mode != "auto"
                        else None
                    ),
                )
            except (ValueError, OverflowError, OSError) as exc:
                raise ValueError("Invalid Unix timestamp") from exc
        parsed = parse_portal_ts(text, ZoneInfo(default_timezone))
        if parsed is None:
            raise ValueError("Invalid portal timestamp")
        explicit = bool(re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", text))
        if mode == "override" or (mode == "missing" and not explicit):
            return local_time(parsed.replace(tzinfo=None), ZoneInfo(name))
        return ParsedTime(parsed.astimezone(timezone.utc))

    match = re.fullmatch(r"(\d{14})(?:\s*([+-]\d{4}|Z))?", text)
    if not match:
        raise ValueError(
            "Use an XMLTV timestamp: YYYYMMDDhhmmss, optionally followed by +HHMM"
        )
    naive = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    offset = match.group(2)
    if mode == "override" or (mode == "missing" and not offset):
        return local_time(naive, ZoneInfo(name))
    zone = timezone.utc
    if offset and offset != "Z":
        hours, minutes = int(offset[1:3]), int(offset[3:5])
        if hours > 23 or minutes > 59:
            raise ValueError("Invalid XMLTV timezone offset")
        delta = timedelta(hours=hours, minutes=minutes) * (
            1 if offset[0] == "+" else -1
        )
        zone = timezone(delta)
    return ParsedTime(naive.replace(tzinfo=zone).astimezone(timezone.utc))


def programme_inputs(attrs, root, *, portal=False):
    """Original provider clock values, not a previously corrected UTC snapshot."""
    if portal and root.get("spm-raw-times") == "1":
        return (
            attrs.get("spm-start", attrs.get("start", "")),
            attrs.get("spm-stop", attrs.get("stop", "")),
            True,
        )
    return attrs.get("start", ""), attrs.get("stop", ""), False


def programme_times(attrs, root, settings, *, portal=False):
    start, stop, raw_portal = programme_inputs(attrs, root, portal=portal)
    if portal and not raw_portal and timing_key(settings)[0] != "auto":
        raise ValueError(
            "This portal cache has no original timestamps; refresh the portal guide first"
        )
    kwargs = {"portal": raw_portal, "default_timezone": root.get("spm-timezone", "UTC")}
    return parse_time(start, settings, **kwargs), parse_time(stop, settings, **kwargs)
