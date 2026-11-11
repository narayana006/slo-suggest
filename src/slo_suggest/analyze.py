"""Turn parsed requests into SLI measurements and suggested SLO targets."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from .logs import Request

# Targets a suggestion may land on, loosest first.
TARGET_LADDER = (0.90, 0.95, 0.99, 0.995, 0.999, 0.9995, 0.9999)

# Candidate latency thresholds in seconds. They match the default histogram
# buckets of the Prometheus client libraries, so the generated latency query
# can use an existing ``le`` bucket.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# The error budget must allow at least this many bad requests in the sample;
# otherwise the data cannot tell that target apart from a stricter one.
MIN_BUDGET_EVENTS = 10

# Keep observed failures at or below this share of the suggested budget.
HEADROOM = 0.5


@dataclass
class Suggestion:
    """One proposed SLO: an indicator, what was observed and a target."""

    kind: str  # "availability" or "latency"
    good: int
    total: int
    target: float | None
    reason: str
    threshold: float | None = None  # seconds, latency only
    percentiles: dict[str, float] = field(default_factory=dict)
    days_total: int = 0
    days_meeting_target: int = 0

    @property
    def observed(self) -> float:
        return self.good / self.total if self.total else 0.0


@dataclass
class Analysis:
    service: str
    requests: int
    first_seen: str | None
    last_seen: str | None
    suggestions: list[Suggestion]


def filter_requests(
    requests: Iterable[Request], service: str | None = None, exclude_path: str | None = None
) -> list[Request]:
    """Drop other services' requests and paths matching ``exclude_path`` (a regex).

    Requests with no service field are kept: a log file without one is assumed
    to belong to a single service.
    """
    pattern = re.compile(exclude_path) if exclude_path else None
    kept = []
    for request in requests:
        if service and request.service and request.service != service:
            continue
        if pattern and request.path and pattern.search(request.path):
            continue
        kept.append(request)
    return kept


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an ascending list; q is between 0 and 100."""
    if not sorted_values:
        raise ValueError("no values")
    rank = max(1, math.ceil(q / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


def suggest_target(good: int, total: int) -> tuple[float | None, str]:
    """Pick the strictest ladder target the observed data supports.

    A target qualifies when the observed failures use at most HEADROOM of its
    error budget and the budget still allows MIN_BUDGET_EVENTS bad requests in
    a sample of this size.
    """
    if total == 0:
        return None, "no requests to measure"
    bad = total - good
    chosen = None
    limited_by_sample = False
    for target in TARGET_LADDER:
        budget = (1.0 - target) * total
        if bad > budget * HEADROOM + 1e-9:
            break
        if budget + 1e-9 < MIN_BUDGET_EVENTS:
            limited_by_sample = True
            break
        chosen = target
    if chosen is None:
        if total * (1.0 - TARGET_LADDER[0]) < MIN_BUDGET_EVENTS:
            return None, f"only {total} requests; too few to suggest a target"
        return None, (
            f"observed {good / total:.2%} leaves no headroom under even a "
            f"{TARGET_LADDER[0]:.0%} target; fix reliability before setting an SLO"
        )
    used = bad / ((1.0 - chosen) * total)
    reason = f"observed failures use {used:.0%} of this target's error budget"
    if limited_by_sample:
        reason += f"; a stricter target needs more than {total} requests to justify"
    return chosen, reason


def choose_latency_threshold(sorted_durations: list[float]) -> float:
    """Smallest standard bucket that at least 95% of requests fit under."""
    p95 = percentile(sorted_durations, 95)
    for bucket in LATENCY_BUCKETS:
        if p95 <= bucket:
            return bucket
    return float(math.ceil(p95))


def _daily_compliance(requests: list[Request], is_good, target: float) -> tuple[int, int]:
    days: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for request in requests:
        if request.timestamp is None:
            continue
        counts = days[request.timestamp.date().isoformat()]
        counts[1] += 1
        if is_good(request):
            counts[0] += 1
    meeting = sum(1 for good, total in days.values() if good / total >= target)
    return len(days), meeting


def analyze(
    requests: list[Request], service: str, latency_threshold: float | None = None
) -> Analysis:
    suggestions: list[Suggestion] = []

    def available(request: Request) -> bool:
        return request.status < 500

    if requests:
        good = sum(1 for r in requests if available(r))
        target, reason = suggest_target(good, len(requests))
        availability = Suggestion("availability", good, len(requests), target, reason)
        if target is not None:
            availability.days_total, availability.days_meeting_target = _daily_compliance(
                requests, available, target
            )
        suggestions.append(availability)

    timed = [r for r in requests if r.duration is not None]
    if timed:
        durations = sorted(r.duration for r in timed)
        threshold = latency_threshold or choose_latency_threshold(durations)

        def fast(request: Request) -> bool:
            return request.duration <= threshold

        good = sum(1 for r in timed if fast(r))
        target, reason = suggest_target(good, len(timed))
        latency = Suggestion(
            "latency",
            good,
            len(timed),
            target,
            reason,
            threshold=threshold,
            percentiles={f"p{q}": percentile(durations, q) for q in (50, 90, 95, 99)},
        )
        if target is not None:
            latency.days_total, latency.days_meeting_target = _daily_compliance(
                timed, fast, target
            )
        suggestions.append(latency)

    stamps = sorted(r.timestamp for r in requests if r.timestamp is not None)
    return Analysis(
        service=service,
        requests=len(requests),
        first_seen=stamps[0].isoformat() if stamps else None,
        last_seen=stamps[-1].isoformat() if stamps else None,
        suggestions=suggestions,
    )
