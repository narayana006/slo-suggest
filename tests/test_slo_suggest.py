import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from slo_suggest.analyze import (
    analyze,
    choose_latency_threshold,
    filter_requests,
    percentile,
    suggest_target,
)
from slo_suggest.cli import main
from slo_suggest.logs import Request, parse_line, parse_lines, parse_time
from slo_suggest.render import MetricNames, number, render_alert_rules, render_openslo

EXAMPLE = Path(__file__).parent.parent / "examples" / "access.log"


def run(argv, stdin_text=""):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdin=io.StringIO(stdin_text), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def make_requests(total, bad=0, slow=0, duration=0.1):
    requests = [Request(status=200, duration=duration) for _ in range(total - bad - slow)]
    requests += [Request(status=503, duration=duration) for _ in range(bad)]
    requests += [Request(status=200, duration=9.0) for _ in range(slow)]
    return requests


# --- log parsing -----------------------------------------------------------


def test_json_line_with_milliseconds_and_iso_time():
    r = parse_line(
        '{"timestamp": "2026-10-01T12:00:00Z", "status": 200, "duration_ms": 250, '
        '"path": "/cart", "service": "checkout"}'
    )
    assert r.status == 200
    assert r.duration == pytest.approx(0.25)
    assert r.timestamp == datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert (r.path, r.service) == ("/cart", "checkout")


def test_json_field_aliases_and_seconds():
    r = parse_line('{"ts": 1790000000, "status_code": "502", "request_time": 1.5, "uri": "/x"}')
    assert (r.status, r.duration, r.path) == (502, 1.5, "/x")
    assert r.timestamp.year == 2026


def test_combined_log_with_request_time():
    line = (
        '203.0.113.9 - - [01/Oct/2026:12:00:01 +0000] "GET /cart?id=1 HTTP/1.1" 200 512 '
        '"https://shop.example.com/" "ExampleBrowser/1.0" 0.123'
    )
    r = parse_line(line)
    assert (r.status, r.path, r.duration) == (200, "/cart?id=1", 0.123)
    assert r.timestamp == datetime(2026, 10, 1, 12, 0, 1, tzinfo=timezone.utc)


def test_combined_log_without_request_time_has_no_duration():
    r = parse_line('203.0.113.9 - bob [01/Oct/2026:12:00:01 +0000] "POST /pay HTTP/1.1" 503 0')
    assert (r.status, r.duration) == (503, None)


@pytest.mark.parametrize(
    "line",
    ["", "not a log line", "{broken json", '{"path": "/no-status"}', '{"status": 999}', "[1, 2]"],
)
def test_unparseable_lines_return_none(line):
    assert parse_line(line) is None


def test_parse_lines_counts_skipped_and_ignores_blank_lines():
    result = parse_lines(['{"status": 200}', "", "garbage", '{"status": 500}'])
    assert (len(result.requests), result.lines, result.skipped) == (2, 3, 1)


def test_parse_time_handles_epoch_milliseconds_and_rejects_junk():
    assert parse_time(1790000000000) == parse_time(1790000000)
    assert parse_time("yesterday") is None
    assert parse_time(None) is None


# --- analysis --------------------------------------------------------------


def test_percentile_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 95) == 95.0
    assert percentile(values, 50) == 50.0
    assert percentile([3.0], 99) == 3.0


def test_target_keeps_failures_within_half_of_budget():
    # 40 bad of 100000 is 0.04%: within half of the 99.9% budget (50) but not 99.95% (25).
    target, reason = suggest_target(100000 - 40, 100000)
    assert target == 0.999
    assert "40%" in reason


def test_perfect_small_sample_is_capped_by_sample_size():
    # A 99.5% budget on 2000 requests allows exactly 10 bad ones, the minimum;
    # on 1500 requests it allows only 7.5, so the suggestion stops at 99%.
    assert suggest_target(2000, 2000)[0] == 0.995
    target, reason = suggest_target(1500, 1500)
    assert target == 0.99
    assert "more than 1500 requests" in reason


def test_no_target_for_tiny_or_unreliable_samples():
    target, reason = suggest_target(50, 50)
    assert target is None and "too few" in reason
    target, reason = suggest_target(900, 1000)
    assert target is None and "no headroom" in reason
    assert suggest_target(0, 0)[0] is None


def test_latency_threshold_is_smallest_bucket_covering_p95():
    assert choose_latency_threshold(sorted([0.08] * 95 + [3.0] * 5)) == 0.1
    assert choose_latency_threshold(sorted([0.3] * 100)) == 0.5
    assert choose_latency_threshold([42.5]) == 43.0


def test_analyze_produces_availability_and_latency_suggestions():
    analysis = analyze(make_requests(20000, bad=8, slow=60), "checkout")
    availability, latency = analysis.suggestions
    assert availability.kind == "availability"
    assert (availability.good, availability.total, availability.target) == (19992, 20000, 0.999)
    assert latency.threshold == 0.1
    assert latency.good == 19940
    assert latency.target == 0.99
    assert set(latency.percentiles) == {"p50", "p90", "p95", "p99"}


def test_analyze_without_durations_skips_latency():
    requests = [Request(status=200) for _ in range(500)]
    analysis = analyze(requests, "api")
    assert [s.kind for s in analysis.suggestions] == ["availability"]


def test_daily_compliance_counts_days_meeting_target():
    good_day = datetime(2026, 10, 1, tzinfo=timezone.utc)
    bad_day = datetime(2026, 10, 2, tzinfo=timezone.utc)
    requests = [Request(status=200, timestamp=good_day) for _ in range(9000)]
    requests += [Request(status=200, timestamp=bad_day) for _ in range(980)]
    requests += [Request(status=500, timestamp=bad_day) for _ in range(20)]
    availability = analyze(requests, "api").suggestions[0]
    assert availability.target == 0.995
    assert (availability.days_total, availability.days_meeting_target) == (2, 1)


def test_filter_by_service_and_path():
    requests = [
        Request(status=200, service="checkout", path="/cart"),
        Request(status=200, service="search", path="/q"),
        Request(status=200, service=None, path="/healthz"),
        Request(status=200, service=None, path="/pay"),
    ]
    kept = filter_requests(requests, "checkout", r"^/healthz")
    assert [r.path for r in kept] == ["/cart", "/pay"]


# --- rendering -------------------------------------------------------------


def test_number_formatting():
    assert number(0.999) == "0.999"
    assert number(14.4 * (1 - 0.999)) == "0.0144"
    assert number(1.0) == "1"


def test_openslo_is_valid_yaml_with_expected_fields():
    analysis = analyze(make_requests(20000, bad=8, slow=60), "checkout")
    docs = list(yaml.safe_load_all(render_openslo(analysis, MetricNames(), "28d")))
    assert [d["metadata"]["name"] for d in docs] == ["checkout-availability", "checkout-latency"]
    slo = docs[0]
    assert slo["apiVersion"] == "openslo/v1" and slo["kind"] == "SLO"
    assert slo["spec"]["service"] == "checkout"
    assert slo["spec"]["timeWindow"] == [{"duration": "28d", "isRolling": True}]
    assert slo["spec"]["objectives"][0]["target"] == 0.999
    ratio = slo["spec"]["indicator"]["spec"]["ratioMetric"]
    assert ratio["good"]["metricSource"]["spec"]["query"] == (
        'sum(http_requests_total{service="checkout",code!~"5.."})'
    )
    latency = docs[1]["spec"]["indicator"]["spec"]["ratioMetric"]
    assert 'le="0.1"' in latency["good"]["metricSource"]["spec"]["query"]


def test_alert_rules_use_burn_rate_thresholds_and_custom_metric_names():
    analysis = analyze(make_requests(20000, bad=8), "checkout")
    metrics = MetricNames(requests="nginx_requests", service_label="app", status_label="status")
    rules = yaml.safe_load(render_alert_rules(analysis, metrics))
    group = rules["groups"][0]
    assert group["name"] == "checkout-availability-burn-rate"
    fast, medium, slow = group["rules"]
    assert fast["alert"] == "CheckoutAvailabilityBurnRate1h"
    assert fast["labels"]["severity"] == "page" and slow["labels"]["severity"] == "ticket"
    assert fast["expr"].count("> 0.0144") == 2
    assert "[1h]" in fast["expr"] and "[5m]" in fast["expr"]
    assert 'nginx_requests{app="checkout",status=~"5.."}' in fast["expr"]
    assert medium["expr"].count("> 0.006") == 2
    assert slow["expr"].count("> 0.001") == 2


def test_no_documents_when_no_target():
    analysis = analyze(make_requests(20), "tiny")
    assert render_openslo(analysis, MetricNames()) == ""
    assert render_alert_rules(analysis, MetricNames()) == ""


# --- command line ----------------------------------------------------------


def test_cli_on_example_log_writes_files(tmp_path):
    code, out, err = run(
        ["suggest", "--logs", str(EXAMPLE), "--service", "checkout", "--out", str(tmp_path),
         "--exclude-path", "^/healthz"]
    )
    assert code == 0
    assert "Availability SLI" in out and "suggested:    99.9% over 30 days" in out
    assert "Latency SLI: requests answered within 500ms" in out
    slos = list(yaml.safe_load_all((tmp_path / "checkout.openslo.yaml").read_text()))
    assert len(slos) == 2
    rules = yaml.safe_load((tmp_path / "checkout.burn-rate-rules.yaml").read_text())
    assert sum(len(g["rules"]) for g in rules["groups"]) == 6
    assert "Wrote" in err


def test_committed_example_output_matches_current_code(tmp_path):
    run(["suggest", "--logs", str(EXAMPLE), "--service", "checkout", "--out", str(tmp_path),
         "--exclude-path", "^/healthz"])
    generated = EXAMPLE.parent / "generated"
    for name in ("checkout.openslo.yaml", "checkout.burn-rate-rules.yaml"):
        assert (tmp_path / name).read_text() == (generated / name).read_text()


def test_cli_json_from_stdin():
    lines = "\n".join(json.dumps({"status": 200, "duration_ms": 40}) for _ in range(3000))
    code, out, _ = run(["suggest", "--logs", "-", "--service", "api", "--format", "json"], lines)
    payload = json.loads(out)
    assert code == 0
    assert payload["requests"] == 3000
    assert payload["suggestions"][0]["target"] == 0.995
    assert payload["suggestions"][1]["threshold_seconds"] == 0.05


def test_cli_errors():
    code, _, err = run(["suggest", "--logs", "/no/such/file", "--service", "x"])
    assert code == 2 and "cannot read" in err
    code, _, err = run(["suggest", "--logs", "-", "--service", "x", "--exclude-path", "("])
    assert code == 2 and "regular expression" in err
    code, out, _ = run(["suggest", "--logs", "-", "--service", "x"], "garbage\n")
    assert code == 1 and "nothing to suggest" in out


def test_cli_out_with_too_little_data_writes_nothing(tmp_path):
    out_dir = tmp_path / "out"
    code, _, err = run(
        ["suggest", "--logs", "-", "--service", "x", "--out", str(out_dir)], '{"status": 200}\n'
    )
    assert code == 1 and "no files were written" in err
    assert not out_dir.exists()
