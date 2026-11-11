# slo-suggest

Read an access log, measure how your service actually performed, and get suggested availability and latency SLO targets, written out as OpenSLO YAML and Prometheus burn-rate alert rules.

> **Status: first working version.** The log-based path works today: availability and latency SLIs from access logs, target suggestions, OpenSLO YAML and multi-window burn-rate alert rules. It does **not** yet connect to Prometheus or Datadog, and it does not discover SLIs from metric names; see the [checklist](#feature-checklist).

## Install

Requires Python 3.10 or newer. No runtime dependencies.

```bash
pip install git+https://github.com/narayana006/slo-suggest
```

## Quick start

Clone the repository to get the sample log (14 days of invented traffic for a fictional `checkout` service), then:

```bash
slo-suggest suggest --logs examples/access.log --service checkout --exclude-path '^/healthz' --out slo
```

```
Service: checkout
Requests analysed: 21638 (0 of 24000 lines skipped), 2026-10-01T00:00 to 2026-10-14T23:59 UTC

Availability SLI: requests that do not return a 5xx status
  observed:     99.954% (21628 of 21638 good)
  suggested:    99.9% over 30 days
  why:          observed failures use 46% of this target's error budget
  daily check:  12 of 14 days in the log met this target

Latency SLI: requests answered within 500ms
  percentiles:  p50 86.2ms  p90 202.3ms  p95 258.4ms  p99 414.6ms
  observed:     99.533% (21537 of 21638 good)
  suggested:    99% over 30 days
  why:          observed failures use 47% of this target's error budget
  daily check:  14 of 14 days in the log met this target
Wrote slo/checkout.openslo.yaml
Wrote slo/checkout.burn-rate-rules.yaml
```

The two files it writes for this log are committed under [`examples/generated/`](examples/generated) so you can read them without running anything. The first SLO looks like this:

```yaml
apiVersion: openslo/v1
kind: SLO
metadata:
  name: checkout-availability
  displayName: "checkout availability"
spec:
  description: "Share of checkout requests that do not fail with a 5xx status."
  service: "checkout"
  indicator:
    metadata:
      name: checkout-availability-sli
    spec:
      ratioMetric:
        counter: true
        good:
          metricSource:
            type: Prometheus
            spec:
              query: "sum(http_requests_total{service=\"checkout\",code!~\"5..\"})"
        total:
          metricSource:
            type: Prometheus
            spec:
              query: "sum(http_requests_total{service=\"checkout\"})"
  timeWindow:
    - duration: 30d
      isRolling: true
  budgetingMethod: Occurrences
  objectives:
    - displayName: "99.9% availability"
      target: 0.999
```

and the first of its three alerts:

```yaml
      - alert: CheckoutAvailabilityBurnRate1h
        expr: "(sum(rate(http_requests_total{service=\"checkout\",code=~\"5..\"}[1h])) / sum(rate(http_requests_total{service=\"checkout\"}[1h]))) > 0.0144 and (sum(rate(http_requests_total{service=\"checkout\",code=~\"5..\"}[5m])) / sum(rate(http_requests_total{service=\"checkout\"}[5m]))) > 0.0144"
        labels:
          severity: page
          slo: checkout-availability
```

## How targets are chosen

The suggestions are simple arithmetic, not a model, so you can check them by hand.

**Availability.** A request is good unless its status is 5xx. 4xx responses count as good because they are usually the caller's mistake.

**Latency.** The threshold is the smallest of `5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s` that at least 95% of requests fit under. Those are the default histogram buckets of the Prometheus client libraries, so the generated query can use an `le` bucket you probably already have. A request is good if it is answered within the threshold. Override it with `--latency-threshold`.

**Target.** The tool walks up the ladder `90%, 95%, 99%, 99.5%, 99.9%, 99.95%, 99.99%` and keeps the strictest target for which both of these hold:

1. The failures seen in the log use no more than half of that target's error budget, leaving headroom for a worse month.
2. The budget allows at least 10 bad requests in a sample of this size. With 1,500 requests a 99.5% target allows 7.5 failures, which the data cannot tell apart from noise, so the suggestion stops at 99% and says so.

If even 90% leaves no headroom, or there are too few requests, no target is suggested and no files are written.

**Daily check.** When the log has timestamps, the report also says how many calendar days (UTC) would have met the suggested target on their own. It is a sanity check, not part of the calculation.

**Alerts.** For each SLO, three alerts in the multi-window, multi-burn-rate style from the Google SRE Workbook: burn rate 14.4 over 1h and 5m (page), 6 over 6h and 30m (page), and 1 over 3d and 6h (ticket). Each fires only when both of its windows are over the threshold. The burn rates assume a 30-day budget.

## Usage

```
slo-suggest suggest --logs FILE --service NAME [options]
```

| Option | Meaning |
| --- | --- |
| `--logs FILE` | Access log to read. `-` reads standard input. |
| `--service NAME` | Service name used in the generated names and queries. If log lines carry a `service` field, lines for other services are dropped. |
| `--out DIR` | Write `NAME.openslo.yaml` and `NAME.burn-rate-rules.yaml` into `DIR`. Without it, only the report is printed. |
| `--format json` | Print the report as JSON instead of text. |
| `--exclude-path REGEX` | Ignore requests whose path matches, for example health checks. |
| `--latency-threshold SECONDS` | Use this latency threshold instead of the one derived from the 95th percentile. |
| `--window 30d` | Time window written into the OpenSLO file. |
| `--requests-metric`, `--duration-metric`, `--service-label`, `--status-label` | Metric and label names used in the generated queries. Defaults: `http_requests_total`, `http_request_duration_seconds`, `service`, `code`. |

Exit status: 0 when at least one SLI was measured, 1 when the log had no usable requests (or `--out` was given and no target could be suggested), 2 for bad arguments or unreadable files. The "Wrote ..." lines go to standard error, so `--format json` output stays parseable.

### Log formats

Each line is detected on its own, so mixed files work. Lines that cannot be parsed are counted as skipped.

- **JSON lines.** The status is read from `status`, `status_code`, `statusCode`, `code`, `response_code` or `http_status`. The duration is read from `duration_ms`, `latency_ms`, `response_time_ms` or `elapsed_ms` (milliseconds), or `duration_s`, `duration_seconds`, `request_time`, `duration`, `latency` or `response_time` (seconds). Optional: `timestamp`/`time`/`ts`/`@timestamp` (ISO 8601 or epoch), `path`/`uri`/`url`, `service`.
- **nginx or Apache combined format**, optionally with the request time in seconds as the last field (add `$request_time` to the end of your nginx `log_format`). Without it you get an availability SLI only.

## Feature checklist

- [x] Derive availability and latency SLIs from structured access logs
- [x] Suggest targets based on historical performance
- [x] Generate OpenSLO YAML
- [x] Generate multi-window burn-rate alert rules (Prometheus rule format)
- [ ] Discover candidate SLIs from metric names and labels
- [ ] Prometheus and Datadog adapters (query history directly instead of reading a log)

## Limitations

- The generated queries are templates. The tool never contacts your metrics system, so it cannot check that `http_requests_total` or your histogram exists, or that the label names match. Set them with the metric options and review the files before applying them.
- The latency query needs a histogram bucket at the chosen threshold. Client libraries also format whole-number bucket labels differently (`le="1"` or `le="1.0"`); the tool writes `1`.
- A target is only as good as the log behind it. Two weeks of quiet traffic says little about a peak season. Treat the suggestion as a starting point for a conversation.
- One service-wide SLO per indicator. There is no per-endpoint breakdown yet; use `--exclude-path` or pre-filter the log.
- The whole log is read into memory. Tens of millions of lines will need sampling first.
- Burn-rate thresholds are the standard ones for a 30-day window and are not rescaled if you change `--window`.
- Datadog monitors are not generated.

## Development

```bash
git clone https://github.com/narayana006/slo-suggest
cd slo-suggest
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest
```

## Contributing

Issues and pull requests are welcome. Sample log lines in formats the parser does not understand (with anything private removed) are especially useful.

## Author

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
