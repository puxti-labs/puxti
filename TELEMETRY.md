# Puxti Telemetry

Puxti can collect anonymous usage telemetry to help understand how the tool is
being used and where to focus improvements.

**Telemetry is off by default and opt-in.** Nothing is sent unless you
explicitly enable it with `puxti telemetry on`.

---

## Enabling and disabling

```bash
puxti telemetry on    # opt in
puxti telemetry off   # opt out at any time
puxti telemetry show  # show current state and your install ID
```

---

## What is collected

One event is sent per command invocation:

| Event | When |
|-------|------|
| `command_run` | After any puxti command completes (success or failure) |

### `command_run` properties

| Property | Type | Example | Description |
|----------|------|---------|-------------|
| `command` | string | `"scan"` | Name of the puxti command that was run |
| `version` | string | `"0.7.0"` | puxti version installed |
| `duration_ms` | integer | `3412` | Wall-clock duration of the command in milliseconds |
| `exit_status` | integer | `0` | Exit code — `0` = success, `1` = error, `130` = interrupted |
| `python_version` | string | `"3.12.4"` | Python interpreter version |
| `platform` | string | `"darwin"` | OS platform (`darwin`, `linux`, `win32`) |

---

## What is NOT collected

- Entity IDs, model names, column names, or anything from your dbt project
- File paths or directory names
- SQL content, semantic definitions, or any data from your Knowledge Graph
- API keys, tokens, GitHub repos, or credentials of any kind
- Any personally identifying information
- IP addresses (PostHog is configured not to log them)

The `install_id` is a randomly generated UUID created on first opt-in and
stored locally in `~/.puxti/config.toml`. It has no connection to your
identity, email address, or machine fingerprint.

---

## Where data goes

Events are sent to [PostHog](https://posthog.com) hosted in the EU
(Frankfurt region, `eu.i.posthog.com`). Puxti is registered as a German
entity, and EU data residency is intentional.

PostHog privacy policy: https://posthog.com/privacy

---

## Verifying

The only place data leaves the process is the `_send` function inside
`record_event` in `src/puxti/telemetry.py`. Read that function to verify
exactly what is sent.

---

## Opting out permanently

Run `puxti telemetry off`. The `enabled = false` flag is written to
`~/.puxti/config.toml`. No events will be sent on any subsequent run.
