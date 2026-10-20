# ZEAL-Heat V1 Decision Summary

This page records the decisions that define the current V1 implementation. The
companion Wiki retains the longer chronological design history.

1. ZEAL-Heat and Visual Climate Scheduler are independent integrations with no
   runtime dependency in either direction.
2. Do not let ZEAL-Heat and another thermostat setpoint scheduler control the same
   thermostat entities.
3. A ZEAL-Heat zone owns exactly one heating actuator and may contain several Home
   Assistant Areas as rooms.
4. Every room schedule targets one ZEAL-Heat-owned canonical thermostat; physical
   room TRVs are downstream equipment, never independent schedule targets.
5. A room uses the average of its usable temperature sensors and the highest
   usable physical-TRV target when the canonical target is unavailable.
6. All physical setpoint writes are clamped to 5–30°C.
7. Each heat source suggests a starting re-enable delay, but the saved delay is
   user-editable.
8. Weekly schedules are seven independent daily timelines with cross-midnight
   carry. Copying replaces schedules only.
9. Quick Change is temporary runtime state and never edits the saved week.
10. Away activation is Off, one Home Assistant calendar or one exact date
    range. The sources are mutually exclusive and Away can be ended early.
11. Room target precedence is Away, per-room unoccupied setback, Quick Change,
    occupied-stay guest comfort offset, then weekly schedule. Occupancy is
    opt-in and separate from active/inactive. Manual occupancy overrides a
    calendar until Follow calendar is selected; an unavailable calendar retains
    the persisted last state. Zone Manual override remains the highest actuator
    authority.
12. Configuration changes use validated admin-only WebSocket commands and an
    optimistic revision token to reject stale browser writes.
13. Audit retention is bounded at 500 outcomes and excludes credentials.
14. V1 is heating-only. Cooling fields are reserved with safe defaults but are
    not exposed or acted upon.
**Occupancy comfort offsets.** A guest thermostat/TRV change in an occupied
room is measured against the underlying scheduled target, persists across later
schedule transitions and restarts, updates on a further adjustment and clears
when occupancy ends. Existing write-echo, delayed-device, multi-TRV and 5–30°C
safety guards apply.

## Next-major-version design constraints

15. Learning uses source-aware, persistent setpoint history and explainable
    evidence; it must not infer patterns solely from the current thermostat
    value.
16. Repetition thresholds, comparable time windows and observation periods are
    named constants with one definition. Advanced settings may expose them only
    after production evidence justifies safe tuning controls.
17. A learned pattern creates a proposal, not a schedule mutation. The saved
    week changes only after explicit user acceptance or editing through the
    normal revision-checked schedule API.
18. Accepted, edited, dismissed and snoozed proposals are audited. Retention is
    bounded and treated as occupancy-sensitive data.
19. The two named learning workstreams are **ZEAL-Heat Learning — Schedule
    Adaptation** and **ZEAL-Heat Learning — Room Thermal Response**.
20. Thermal Response selects standard Home Assistant outdoor-temperature and
    weather entities by capability; Met Office, Open-Meteo and Pirate Weather
    form the initial compatibility matrix, not an allow-list.
21. Actual outdoor observations train the room model; forecasts are used only
    for future optimum-start prediction and remain distinguishable in storage.
22. Thermal models are per room and begin with an explainable first-order model.
    PID is deferred unless ZEAL-Heat later controls a suitable proportional output.
23. Optimum start begins as a recommendation. Any automatic start adjustment
    requires explicit opt-in, sufficient confidence and safe fallback to the
    unchanged weekly schedule.
24. Schedule Adaptation is implemented and tested as one complete
    capture–classify–detect–recommend–confirm–commit–revert pipeline rather than
    shipping an observation-only phase first. Synthetic dated event streams
    exercise the production classifier and proposal engine because a stable
    household may generate too few natural changes for timely passive
    validation. Automatic detection may create advice, but only an explicit,
    authorised, revision-checked confirmation may change the saved schedule.
25. Schedule Adaptation has an administrator Yes/No control in Setup. Setup is
    the administrator's routine evidence-progress surface. **Schedule Updates**
    appears to administrators and Schedule/Learning-authorised standard users
    only when an actionable recommendation exists, and hides from standard
    users after all recommendations are resolved. Diagnostics remain a
    pseudonymised support mechanism, not the normal progress UI.
26. Heating demand has no temperature deadband: any positive difference between
    target and measured room temperature is demand. The per-zone re-enable delay
    bounds actuator cycling; adding a second hysteresis mechanism would obscure
    that control and previously proved ineffective in the source controller.
27. Thermal Response has an administrator Yes/No control in Setup. Enabling it
    asks one plain-language home heat-retention question, selects a preferred
    local outdoor-temperature sensor and fallback weather provider, and confirms
    the participating rooms. EPC bands are only an optional guide; specialist
    building data is never required. Users should consult their local or national
    government's EPC-equivalent guidance where applicable; **Not sure** remains
    available. Continuous per-room observations refine the starting estimate
    without proposal acceptance, while automatic optimum start remains a
    separate future opt-in.
28. Thermal Response is entirely administrator-only. When enabled, Learning
    contains its persistent status and graph page; Setup contains its initiation,
    privacy and confirmed per-room/all-room reset controls. Real names are used
    in the administrator UI, while diagnostics remain pseudonymised.
29. An abrupt per-room temperature rise inconsistent with ZEAL-Heat-observed heating
    creates a **suspected external heat** training hold. ZEAL-Heat excludes the
    remainder of the current schedule period and clears the hold at the next
    scheduled period start; continuing abnormal conditions may create a new
    hold. This initial recovery rule must be revisited after testing. It does not
    change demand, schedules or thermostat/TRV targets. No inventory of log
    burners or other independent heat sources is required.
30. Optimum-start prediction separates measured current outdoor temperature from
    forecast conditions. ZEAL-Heat interpolates the configured provider's standard
    Home Assistant hourly forecast across the candidate warm-up period and
    solves backwards from the next scheduled target. The administrator view
    discloses both sources, forecast range/provider, target, warm-up duration,
    recommended start and confidence; forecast fallback is always labelled.
31. Thermal observations use versioned per-room Home Assistant Stores: relevant
    five-minute samples are limited to 30 days/2,000 and compact episode
    summaries to 365 days/750. Active episodes use a separate small checkpoint
    Store, saved at least every 15 minutes, at state transitions and through
    Home Assistant's orderly-shutdown final write; checkpointing never rewrites
    retained room history. Stable IDs provide restart deduplication. The
    eight-room planning allowance is 20 MB per config entry. Disabling Thermal
    Response asks the administrator to keep data (default), permanently delete
    it, or cancel; no thermal deletion changes heating configuration, schedules
    or overrides.
