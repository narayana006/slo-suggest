# slo-from-logs

> Propose SLIs and SLOs from existing Prometheus or Datadog metrics and generate OpenSLO YAML plus burn-rate alerts.

**Status:** 🚧 Early stage — design and roadmap published, implementation in progress. Stars, issues and ideas welcome.

## Why

Teams know they should have SLOs but stall on picking SLIs and writing burn-rate alerts.

## Planned features

- [ ] Discover candidate SLIs from metric names and labels
- [ ] Suggest targets based on historical performance
- [ ] Generate OpenSLO YAML
- [ ] Generate multi-window burn-rate alert rules
- [ ] Prometheus and Datadog adapters

## Intended usage

```
slo-from-logs suggest --prometheus http://prom:9090 --service checkout
```

_The interface above is the target design and may change._

## Roadmap

1. Core implementation of the first feature set
2. Examples and tests
3. CI and first tagged release
4. Docs and integrations

## Contributing

Contributions are welcome. Open an issue to discuss an idea or pick an unchecked item from the feature list.

## License

[MIT](LICENSE)
