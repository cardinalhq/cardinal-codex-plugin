---
name: cardinal-connect
description: Connect Codex to Cardinal by running the device-code flow and configuring telemetry hooks plus the unified Cardinal MCP server.
---

# Cardinal Connect

Use this skill when the user asks to connect Codex to Cardinal, enable Cardinal telemetry, enable Cardinal MCP tools, rotate a Cardinal connection, or run Cardinal setup.

Run the repository script:

```bash
python3 scripts/cardinal-connect
```

If the user asks for a non-production Cardinal host, pass `--host <url>`. If the script reports that Cardinal is already connected, ask whether to rotate or rerun with `--rotate` when the user has already asked to overwrite.

Besides the ingest and MCP keys, the script requests a control-plane token (`maestro:act`) with `dashboards:write`, `alerts:write` and `telemetry:query`, so skills like migrate-from-grafana can write dashboards and alert rules and query telemetry as the user — bounded by their org role; `telemetry:query` is read-only in every org they belong to. Mention that when you show the approval URL. The token is stored 0600 in `cardinal-secrets.json` and `cardinal-disconnect` revokes it. Pass `--minimal-scopes` for users who don't want it. A Cardinal server that doesn't offer those scopes yet makes the script connect without them and say so. Users connected before this get the token with `--rotate`.

The script prints an approval URL. Show that URL to the user and wait for the script to finish. On success, tell the user to restart Codex so it reloads `~/.codex/config.toml` and `~/.codex/hooks.json`, review/trust the new Cardinal hooks when Codex prompts, then suggest `cardinal-status`.

Do not claim Codex native OpenTelemetry was enabled. The plugin emits Cardinal-compatible telemetry from Codex hooks and local Codex transcripts.

## A self-hosted Cardinal behind a self-signed certificate

Most Cardinal hosts carry a certificate the machine already trusts, and the script says nothing about TLS. When `--host` presents one it does not trust (self-signed, or a private CA), the script stops **before** the device flow with exit status 3 and prints the certificate: the verification error, subject, issuer, the names it carries (and whether they include the host), validity and its SHA-256 fingerprint, plus the exact re-run command. Show all of that to the user and ask whether they expected it (they run this Cardinal) and want to trust exactly that certificate. Never answer for them. If they say yes, re-run with the fingerprint the script printed:

```bash
python3 scripts/cardinal-connect --host <url> --trust-cert <sha256 fingerprint>
```

That pins the certificate for that hostname in `~/.cardinal/tls/` (delete the file to forget it): every Cardinal hook, script and skill then verifies the host against it, and nothing else is weakened. If the fingerprint does not match what the server presents on the re-run, the script refuses. Codex's own MCP client trusts the system certificate store, which this plugin does not touch: the script prints what to add where. That client also checks that the certificate names the host; the script says when it does not. See `docs/specs/tls-trust.md`.
