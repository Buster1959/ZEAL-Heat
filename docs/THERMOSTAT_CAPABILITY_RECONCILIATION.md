# Thermostat capability-aware setpoint reconciliation

## Decision (2026-11-18)
Each integration independently maintains an ephemeral, per-climate-entity capability view derived from Home Assistant state attributes: `target_temp_step`, `min_temp`, `max_temp`, `supported_features`, and available HVAC modes. The schedule remains the canonical requested temperature; physical writes must use device-compatible targets without changing the saved schedule.

When capability metadata is missing, fall back to conservative command handling: accept the Home Assistant service result as *accepted*, never as proof of physical actuation; bound confirmation retries; and avoid an indefinite five-minute write loop. Offline devices must not receive commands, and on recovery must reconcile against the *current* scheduled target, not stale queued values. A change in reported target must not automatically be interpreted as either command confirmation or a manual override without distinguishing delayed reports, rounding, and external writers.

## Implementation acceptance criteria
- Round to supported steps deterministically, respecting advertised min/max and the integration's safety limits. Do not use Python's implicit half-even rounding for halfway cases.
- Match reports against the effective device target rather than the unsnapped schedule value.
- Missing `temperature` while off must not cause infinite resends.
- Limit accepted-but-unconfirmed retries, record timestamps and outcomes, and reset on a new schedule transition.
- Preserve explicit manual adjustments until the next scheduled transition; distinguish a genuine manual adjustment from a delayed device echo where possible.
- Rebuild ephemeral capabilities on startup and refresh after state/attribute changes; no persistent table is required.
- Test integer/half-degree steps, unavailable/recovered devices, missing attributes, sleeping TRVs, command failures, manual overrides, restart, and superseded schedule targets.
- ZEAL-Heat must continue to use independent room measurements and plant safety rules. Never infer heat demand or delivery from a successful service call.
- Keep ZEAL-Heat and Visual Climate Scheduler independent. Do not configure both to write the same physical thermostat.
- Update README, architecture docs, wiki, and regression tests alongside functional implementation before a release.
