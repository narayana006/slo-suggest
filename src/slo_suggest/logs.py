"""Read access logs into a uniform list of requests.

Two formats are understood, detected line by line:

* JSON lines, one object per request, with flexible field names.
* The nginx/Apache "combined" format, optionally followed by the request time
  in seconds (nginx ``$request_time``) as the last field.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

STATUS_FIELDS = ("status", "status_code", "statusCode", "code", "response_code", "http_status")
PATH_FIELDS = ("path", "uri", "url", "request_uri", "request_path")
TIME_FIELDS = ("timestamp", "time", "ts", "@timestamp", "time_local")
SERVICE_FIELDS = ("service", "service_name", "app")

# Duration field name -> multiplier that converts its value to seconds.
DURATION_FIELDS = {
    "duration_ms": 0.001,
    "latency_ms": 0.001,
    "response_time_ms": 0.001,
    "elapsed_ms": 0.001,
    "duration_s": 1.0,
    "duration_seconds": 1.0,
    "request_time": 1.0,
    "duration": 1.0,
    "latency": 1.0,
    "response_time": 1.0,
}

_COMBINED = re.compile(
    r'^\S+ \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" '
    r"(?P<status>\d{3}) \S+"
    r'(?: "[^"]*" "[^"]*")?'
    r"(?: (?P<duration>\d+(?:\.\d+)?))?\s*$"
)


@dataclass(frozen=True)
class Request:
    status: int
    duration: float | None = None  # seconds
    timestamp: datetime | None = None
    path: str | None = None
    service: str | None = None


@dataclass
class ParseResult:
    requests: list[Request]
    lines: int = 0
    skipped: int = 0


def _first(obj: dict, names: Iterable[str]):
    for name in names:
        if name in obj and obj[name] not in (None, ""):
            return obj[name]
    return None


def parse_time(value) -> datetime | None:
    """Parse ISO 8601, epoch seconds/milliseconds or the combined-log time format."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    for parser in (
        lambda t: datetime.fromisoformat(t[:-1] + "+00:00" if t.endswith(("Z", "z")) else t),
        lambda t: datetime.strptime(t, "%d/%b/%Y:%H:%M:%S %z"),
    ):
        try:
            parsed = parser(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _to_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _from_json(obj: dict) -> Request | None:
    try:
        status = int(_first(obj, STATUS_FIELDS))
    except (TypeError, ValueError):
        return None
    if not 100 <= status <= 599:
        return None
    duration = None
    for name, factor in DURATION_FIELDS.items():
        if name in obj:
            number = _to_float(obj[name])
            if number is not None:
                duration = number * factor
                break
    path = _first(obj, PATH_FIELDS)
    service = _first(obj, SERVICE_FIELDS)
    return Request(
        status=status,
        duration=duration,
        timestamp=parse_time(_first(obj, TIME_FIELDS)),
        path=str(path) if path is not None else None,
        service=str(service) if service is not None else None,
    )


def parse_line(line: str) -> Request | None:
    """Parse one log line; return None if it is not a recognisable request."""
    line = line.strip()
    if not line:
        return None
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        return _from_json(obj) if isinstance(obj, dict) else None
    match = _COMBINED.match(line)
    if not match:
        return None
    duration = match.group("duration")
    return Request(
        status=int(match.group("status")),
        duration=float(duration) if duration is not None else None,
        timestamp=parse_time(match.group("time")),
        path=match.group("path"),
    )


def parse_lines(lines: Iterable[str]) -> ParseResult:
    result = ParseResult(requests=[])
    for line in lines:
        if not line.strip():
            continue
        result.lines += 1
        request = parse_line(line)
        if request is None:
            result.skipped += 1
        else:
            result.requests.append(request)
    return result
