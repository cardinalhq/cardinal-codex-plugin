# Cardinal Codex plugin marketplace

> [!NOTE]
> This repository is a **release mirror**. Development happens in
> [cardinal-agent-plugins](https://github.com/cardinalhq/cardinal-agent-plugins) — send PRs there.


This repository publishes one Codex plugin: `cardinal-codex-plugin`.

The plugin source lives at [`plugins/cardinal-codex-plugin`](./plugins/cardinal-codex-plugin), and the marketplace manifest at [`.agents/plugins/marketplace.json`](./.agents/plugins/marketplace.json) exposes only that plugin.

## Install

```bash
codex plugin marketplace add cardinalhq/cardinal-codex-plugin
codex plugin add cardinal-codex-plugin@cardinalhq-codex-plugin
```

For local development, install from a checkout of this repository instead of the GitHub slug.

## Connect

Ask Codex:

```text
Use cardinal-connect
```

The connect skill runs Cardinal's browser-approved device-code flow, configures the Cardinal MCP endpoint, and installs Codex telemetry hooks. See the plugin README for full behavior and script options:

- [`plugins/cardinal-codex-plugin/README.md`](./plugins/cardinal-codex-plugin/README.md)

## License

Apache 2.0. See [LICENSE](./LICENSE).
