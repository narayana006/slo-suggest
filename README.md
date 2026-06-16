# slo-from-logs

Propose SLIs and SLOs from your existing access logs and Prometheus or Datadog metrics, and generate OpenSLO YAML plus burn-rate alerts.

> **Project status: planning. There is no working code in this repository yet.**
> This README describes what the project will do. Nothing below can be installed or run today.
> Features are ticked off in the checklist as they are built.

## What it is

A command-line tool.

**The problem it solves:** Teams know they should have SLOs but stall on picking SLIs and writing burn-rate alerts.

## Who it is for

Service owners who want service level objectives (SLOs) but have not chosen indicators or written alerts yet.

## How it will work

1. You point the tool at your metrics system and name a service.
2. It proposes indicators and targets based on how the service has actually performed.
3. It writes OpenSLO files and matching burn-rate alert rules for you to review.

## Planned features

Unticked items are not built yet.

- [ ] Derive availability and latency SLIs from structured access logs
- [ ] Discover candidate SLIs from metric names and labels
- [ ] Suggest targets based on historical performance
- [ ] Generate OpenSLO YAML
- [ ] Generate multi-window burn-rate alert rules
- [ ] Prometheus and Datadog adapters

## Planned usage

This is the intended interface, shown so you can judge the design. **It does not work yet**, and names may change before the first release.

```bash
slo-from-logs suggest --logs access.log --prometheus http://prom:9090 --service checkout
```

## Contributing

Feedback on the design is the most useful contribution right now. Open an issue to suggest a feature, question an approach or describe your use case. Code contributions are welcome once the first version lands.

## License

[MIT](LICENSE)
