"""Render an analysis as a text report, OpenSLO YAML and Prometheus alert rules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .analyze import Analysis, Suggestion

# Multi-window, multi-burn-rate alerts as described in the Google SRE Workbook:
# (long window, short window, burn rate, severity). A burn rate of 14.4 spends
# 2% of a 30-day budget in one hour; 6 spends 5% in six hours; 1 spends 10% in
# three days.
BURN_RATE_WINDOWS = (
    ("1h", "5m", 14.4, "page"),
    ("6h", "30m", 6.0, "page"),
    ("3d", "6h", 1.0, "ticket"),
)


@dataclass(frozen=True)
class MetricNames:
    """Names of the Prometheus metrics and labels the generated queries use."""

    requests: str = "http_requests_total"
    duration: str = "http_request_duration_seconds"
    service_label: str = "service"
    status_label: str = "code"


def q(text: str) -> str:
    """Quote a string for YAML (JSON strings are valid YAML scalars)."""
    return json.dumps(text)


def number(value: float) -> str:
    """Format a float without trailing zeros or exponent: 0.999, 14.4, 1."""
    return f"{value:.6f}".rstrip("0").rstrip(".")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "service"


def duration_label(seconds: float) -> str:
    if seconds < 1:
        return f"{number(seconds * 1000)}ms"
    return f"{number(seconds)}s"


def slo_name(service: str, suggestion: Suggestion) -> str:
    return f"{slug(service)}-{suggestion.kind}"


def counter_queries(service: str, s: Suggestion, m: MetricNames) -> tuple[str, str]:
    """Good and total counter queries for the OpenSLO ratio metric."""
    sel = f'{m.service_label}="{service}"'
    if s.kind == "availability":
        good = f'sum({m.requests}{{{sel},{m.status_label}!~"5.."}})'
        total = f"sum({m.requests}{{{sel}}})"
    else:
        good = f'sum({m.duration}_bucket{{{sel},le="{number(s.threshold)}"}})'
        total = f"sum({m.duration}_count{{{sel}}})"
    return good, total


def error_ratio(service: str, s: Suggestion, m: MetricNames, window: str) -> str:
    """PromQL for the fraction of bad requests over ``window``."""
    sel = f'{m.service_label}="{service}"'
    if s.kind == "availability":
        return (
            f'sum(rate({m.requests}{{{sel},{m.status_label}=~"5.."}}[{window}]))'
            f" / sum(rate({m.requests}{{{sel}}}[{window}]))"
        )
    return (
        f'1 - sum(rate({m.duration}_bucket{{{sel},le="{number(s.threshold)}"}}[{window}]))'
        f" / sum(rate({m.duration}_count{{{sel}}}[{window}]))"
    )


def _description(service: str, s: Suggestion) -> str:
    if s.kind == "availability":
        return f"Share of {service} requests that do not fail with a 5xx status."
    return f"Share of {service} requests answered within {duration_label(s.threshold)}."


def render_openslo(analysis: Analysis, metrics: MetricNames, window: str = "30d") -> str:
    """One OpenSLO v1 SLO document per suggestion that has a target."""
    documents = []
    for s in analysis.suggestions:
        if s.target is None:
            continue
        good, total = counter_queries(analysis.service, s, metrics)
        name = slo_name(analysis.service, s)
        documents.append(
            "\n".join(
                [
                    "apiVersion: openslo/v1",
                    "kind: SLO",
                    "metadata:",
                    f"  name: {name}",
                    f"  displayName: {q(f'{analysis.service} {s.kind}')}",
                    "spec:",
                    f"  description: {q(_description(analysis.service, s))}",
                    f"  service: {q(analysis.service)}",
                    "  indicator:",
                    "    metadata:",
                    f"      name: {name}-sli",
                    "    spec:",
                    "      ratioMetric:",
                    "        counter: true",
                    "        good:",
                    "          metricSource:",
                    "            type: Prometheus",
                    "            spec:",
                    f"              query: {q(good)}",
                    "        total:",
                    "          metricSource:",
                    "            type: Prometheus",
                    "            spec:",
                    f"              query: {q(total)}",
                    "  timeWindow:",
                    f"    - duration: {window}",
                    "      isRolling: true",
                    "  budgetingMethod: Occurrences",
                    "  objectives:",
                    f"    - displayName: {q(f'{s.target * 100:g}% {s.kind}')}",
                    f"      target: {number(s.target)}",
                ]
            )
        )
    return "\n---\n".join(documents) + ("\n" if documents else "")


def _camel(text: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", text) if part)


def render_alert_rules(analysis: Analysis, metrics: MetricNames) -> str:
    """Prometheus rule file with multi-window burn-rate alerts for each SLO."""
    with_target = [s for s in analysis.suggestions if s.target is not None]
    if not with_target:
        return ""
    lines = ["groups:"]
    for s in with_target:
        budget = 1.0 - s.target
        name = slo_name(analysis.service, s)
        lines += [f"  - name: {name}-burn-rate", "    rules:"]
        for long, short, rate, severity in BURN_RATE_WINDOWS:
            threshold = number(rate * budget)
            expr = (
                f"({error_ratio(analysis.service, s, metrics, long)}) > {threshold}"
                f" and ({error_ratio(analysis.service, s, metrics, short)}) > {threshold}"
            )
            summary = (
                f"{analysis.service} {s.kind} is burning its error budget at more than "
                f"{number(rate)}x the sustainable rate (windows {long} and {short})"
            )
            lines += [
                f"      - alert: {_camel(name)}BurnRate{long}",
                f"        expr: {q(expr)}",
                "        labels:",
                f"          severity: {severity}",
                f"          slo: {name}",
                "        annotations:",
                f"          summary: {q(summary)}",
            ]
    return "\n".join(lines) + "\n"


def render_text(analysis: Analysis, lines_read: int, skipped: int) -> str:
    out = [f"Service: {analysis.service}"]
    span = ""
    if analysis.first_seen and analysis.last_seen:
        span = f", {analysis.first_seen[:16]} to {analysis.last_seen[:16]} UTC"
    out.append(f"Requests analysed: {analysis.requests} ({skipped} of {lines_read} lines skipped){span}")
    if not analysis.suggestions:
        out.append("")
        out.append("No requests found, so there is nothing to suggest.")
        return "\n".join(out)
    for s in analysis.suggestions:
        out.append("")
        if s.kind == "availability":
            out.append("Availability SLI: requests that do not return a 5xx status")
        else:
            out.append(f"Latency SLI: requests answered within {duration_label(s.threshold)}")
            pct = "  ".join(f"{k} {duration_label(v)}" for k, v in s.percentiles.items())
            out.append(f"  percentiles:  {pct}")
        out.append(f"  observed:     {s.observed:.3%} ({s.good} of {s.total} good)")
        if s.target is None:
            out.append(f"  suggested:    none ({s.reason})")
            continue
        out.append(f"  suggested:    {s.target * 100:g}% over 30 days")
        out.append(f"  why:          {s.reason}")
        if s.days_total:
            out.append(
                f"  daily check:  {s.days_meeting_target} of {s.days_total} days "
                "in the log met this target"
            )
    return "\n".join(out)


def render_json(analysis: Analysis, lines_read: int, skipped: int) -> str:
    payload = {
        "service": analysis.service,
        "requests": analysis.requests,
        "lines_read": lines_read,
        "lines_skipped": skipped,
        "first_seen": analysis.first_seen,
        "last_seen": analysis.last_seen,
        "suggestions": [
            {
                "kind": s.kind,
                "good": s.good,
                "total": s.total,
                "observed": round(s.observed, 6),
                "target": s.target,
                "reason": s.reason,
                "threshold_seconds": s.threshold,
                "percentiles_seconds": s.percentiles,
                "days_total": s.days_total,
                "days_meeting_target": s.days_meeting_target,
            }
            for s in analysis.suggestions
        ],
    }
    return json.dumps(payload, indent=2)
