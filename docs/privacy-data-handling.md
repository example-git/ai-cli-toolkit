# Privacy And Data Handling

## What Is Stored

- Wrapper logs under `~/.ai-cli/logs`.
- Proxied traffic records in `~/.ai-cli/traffic.db`.
- Session metadata/files for active wrapper-managed runs.

## Traffic Capture

- API traffic browsing is available via `ai-cli traffic`.
- Traffic body capture is redacted by default using:
  - `privacy.redact_traffic_bodies: true`

## Retention

- `retention.logs_days` controls wrapper log pruning.
- `retention.traffic_days` controls traffic DB row pruning.
- Housekeeping runs at wrapper startup as best-effort cleanup.

## Recommended Defaults For Public Use

- Keep `privacy.redact_traffic_bodies` enabled.
- Use conservative retention windows (`14`/`30` default is a baseline).
- Limit access to `~/.ai-cli/traffic.db` and `~/.ai-cli/logs` on shared machines.
