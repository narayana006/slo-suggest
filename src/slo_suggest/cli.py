"""Command-line entry point."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import __version__
from .analyze import analyze, filter_requests
from .logs import parse_lines
from .render import (
    MetricNames,
    render_alert_rules,
    render_json,
    render_openslo,
    render_text,
    slug,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slo-suggest",
        description="Propose SLIs and SLO targets from access logs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    s = commands.add_parser(
        "suggest",
        help="measure availability and latency in an access log and suggest SLO targets",
    )
    s.add_argument("--logs", required=True, metavar="FILE", help="access log, or - for stdin")
    s.add_argument("--service", required=True, help="service name used in the generated files")
    s.add_argument("--out", metavar="DIR", help="write OpenSLO YAML and alert rules here")
    s.add_argument("--format", choices=("text", "json"), default="text", help="report format")
    s.add_argument(
        "--exclude-path", metavar="REGEX", help="ignore requests whose path matches, e.g. ^/healthz"
    )
    s.add_argument(
        "--latency-threshold",
        type=float,
        metavar="SECONDS",
        help="latency threshold to use instead of the one picked from the 95th percentile",
    )
    s.add_argument("--window", default="30d", help="SLO time window (default: 30d)")
    defaults = MetricNames()
    s.add_argument("--requests-metric", default=defaults.requests, help="request counter name")
    s.add_argument("--duration-metric", default=defaults.duration, help="latency histogram name")
    s.add_argument("--service-label", default=defaults.service_label, help="label holding the service")
    s.add_argument("--status-label", default=defaults.status_label, help="label holding the HTTP status")
    return parser


def main(
    argv: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)

    if args.latency_threshold is not None and args.latency_threshold <= 0:
        print("error: --latency-threshold must be greater than 0", file=stderr)
        return 2
    try:
        if args.exclude_path:
            re.compile(args.exclude_path)
    except re.error as exc:
        print(f"error: --exclude-path is not a valid regular expression: {exc}", file=stderr)
        return 2
    try:
        if args.logs == "-":
            parsed = parse_lines(stdin)
        else:
            with open(args.logs, encoding="utf-8", errors="replace") as handle:
                parsed = parse_lines(handle)
    except OSError as exc:
        print(f"error: cannot read {args.logs}: {exc.strerror or exc}", file=stderr)
        return 2

    requests = filter_requests(parsed.requests, args.service, args.exclude_path)
    analysis = analyze(requests, args.service, args.latency_threshold)
    render = render_json if args.format == "json" else render_text
    print(render(analysis, parsed.lines, parsed.skipped), file=stdout)

    if args.out:
        metrics = MetricNames(
            requests=args.requests_metric,
            duration=args.duration_metric,
            service_label=args.service_label,
            status_label=args.status_label,
        )
        openslo = render_openslo(analysis, metrics, args.window)
        rules = render_alert_rules(analysis, metrics)
        if not openslo:
            print("No target could be suggested, so no files were written.", file=stderr)
            return 1
        out_dir = Path(args.out)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            slo_path = out_dir / f"{slug(args.service)}.openslo.yaml"
            rules_path = out_dir / f"{slug(args.service)}.burn-rate-rules.yaml"
            slo_path.write_text(openslo, encoding="utf-8")
            rules_path.write_text(rules, encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write to {args.out}: {exc.strerror or exc}", file=stderr)
            return 2
        # Keep stdout clean for --format json; file notices go to stderr.
        print(f"Wrote {slo_path}", file=stderr)
        print(f"Wrote {rules_path}", file=stderr)
    return 0 if analysis.suggestions else 1
