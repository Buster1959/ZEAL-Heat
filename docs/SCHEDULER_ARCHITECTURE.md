# ZEAL-Heat Scheduler Architecture

Status: Block 2 model, Block 3 runtime, Block 6 visual editor, Block 7 Quick
Change/audit, Block 8 Away mode and version 0.16 room occupancy complete.

## Independence boundary

ZEAL-Heat owns every scheduler module under `custom_components/zeal/scheduler`.
It does not import Visual Climate Scheduler and does not use that integration's
domain, services or storage. The two integrations remain independently
installable.

## Durable model

Each schedule belongs to one stable ZEAL-Heat `room_id` and contains:

- the room's current display name;
- exactly seven named weekday lists;
- zero or more ordered time/setpoint periods per day;
- configuration-level JSON-safe settings and the Home Assistant temperature
  unit.

Physical TRV entity IDs, zone switches and ZEAL-Heat climate entity IDs are not
persisted in schedule records. That prevents a schedule from bypassing ZEAL-Heat's
canonical room thermostat and safety boundary.

The schema is versioned independently from ZEAL-Heat's Coordinator runtime Store.
Its Store key is `zeal.scheduler.<config-entry-id>` and its current schema and
Store versions are both 1. Pre-versioned prototype documents migrate to V1;
unknown future versions fail closed.

## Pure modules

- `models.py`: validation, JSON conversion, schema migration and detached
  period copies.
- `engine.py`: active-period and next-transition selection, including
  cross-midnight carry and empty days.
- `editor.py`: validated day replacement and one-time schedule copying while
  retaining each destination room's identity.
- `rooms.py`: reconciliation with the current ZEAL-Heat room registry. Renaming a
  room preserves its schedule; a new stable ID gets empty days; a removed ID
  is removed from the schedule document.
- `overrides.py`: temporary absolute/delta targets lasting two hours, four
  hours or until the next scheduled change. Calculations never modify the
  saved weekly schedule.
- `away.py`: validates the mutually exclusive Off, Home Assistant Calendar and
  date-range sources, the global Away target and persisted schedule settings.
- `occupancy.py`: validates and serializes the separate restart-safe per-room
  stay state (occupancy, manual calendar override, stay start and comfort offset).
- `storage.py`: the only Home Assistant-facing module in this package; adapts
  the pure document to a separate versioned `Store`.

## Runtime adapter

On config-entry startup, ZEAL-Heat loads the independent schedule Store and
reconciles it against the current configured room IDs. New schedulable rooms get
empty schedules, renamed rooms keep their periods, and deleted room IDs are
removed before the reconciled document is saved.

`scheduler/runtime.py` immediately restores the period that should currently be
active, then creates one Home Assistant timer for the nearest upcoming room
transition. At a transition it applies changed periods and arranges the next
timer. Timers and Coordinator listeners are cancelled on unload.

The runtime passes only `room_id`, temperature and cause to
`ZealCoordinator.async_set_room_target`. The Coordinator resolves the canonical
ZEAL-Heat room thermostat, clamps the requested temperature, updates that entity and
then uses its existing guarded propagation path for the room's physical TRVs.
The runtime never reads, stores, selects or calls a physical TRV entity ID.

If a configured room thermostat is not currently registered, the application
is skipped and is not marked successful. A later Coordinator update retries the
current period; successfully applied periods are not repeated.

Occupancy configuration lives with the room hierarchy and is disabled by
default. Live stay state uses `zeal.occupancy.<config-entry-id>` rather than the
schedule document, so automatic calendar transitions do not create schedule
revision conflicts. Missing/unavailable calendar state retains the persisted
last value. Manual occupancy takes precedence until Follow calendar is selected.
An occupied guest adjustment is measured from the underlying active schedule
period and the resulting offset follows later periods until occupancy ends.

## Visual editor

The admin-only **ZEAL-Heat → Schedule** page uses the configured Zone/Floor hierarchy
for navigation, but every schedule is still keyed by its stable room ID. The
page explicitly displays the room's canonical ZEAL-Heat thermostat. Physical TRV
entity IDs are neither selectable nor sent by schedule write requests.

Each weekday has a step timeline and exact name, 24-hour time and setpoint
fields. Dragging is a convenience control that snaps to 15 minutes and 0.5°C;
the fields preserve exact values supported by the model. Up to four daily
periods are exposed in the V1 editor. Empty days and the start of every day show
the most recent target carried from the previous scheduled day, matching the
engine's cross-midnight selection.

Source-day application changes only the browser-side week until **Save
schedule** is selected. A room-to-room copy first saves that same source editor
state, then replaces only the seven daily lists of selected rooms. Destination
room IDs, names, zones, Areas, physical equipment and ZEAL-Heat thermostat entities
are retained.

Both operations include the configuration revision received when the editor was
loaded. A stale browser is rejected instead of overwriting a newer hierarchy or
schedule. Successful writes persist first, update the running scheduler and
return a fresh complete configuration snapshot. The page is responsive across
desktop, tablet and narrow mobile layouts and warns before discarding unsaved
room or schedule changes.

## Quick Change and audit

The **Quick Change** page is a frontend over `ScheduleRuntime`'s ephemeral
override map. It accepts stable room IDs selected individually, by configured
Zone/Floor or as the whole house. A request contains either a relative delta or
an exact target and one of three durations: two hours, four hours or the next
scheduled transition. The pure override calculator resolves and validates the
entire batch before the runtime changes any room.

An override replaces only the effective runtime target. The immutable
`ScheduleConfiguration` and its Store document are never changed. Clearing or
expiring a hold immediately re-evaluates the active schedule through the same
canonical room boundary. Relative changes require an active scheduled target;
an exact target remains available for an otherwise empty schedule. Every result
still passes ZEAL-Heat's 5–30°C safety range.

Each canonical application attempt is appended to a separate versioned audit
Store with timestamp, stable room identity, canonical ZEAL-Heat thermostat,
previous/requested target, cause and outcome. It records successful and skipped
unavailable outcomes, contains no credentials or physical-TRV service payloads,
survives integration restarts and retains only the newest 500 entries.

**ZEAL-Heat Learning — Schedule Adaptation** adds a source-aware learning pipeline
above this boundary. It groups comparable manual/Quick Change events by room
and exact schedule period across distinct dates, then creates an explainable
schedule proposal after three qualifying dates in 21 days. The pipeline can
recommend but cannot write `ScheduleConfiguration` directly.
Accepting or editing a proposal uses the existing validated, revision-checked
schedule API; dismissal and snooze outcomes are also retained to prevent
repetitive prompting. The separate **ZEAL-Heat Learning — Room Thermal Response**
workstream is defined in [the learning roadmap](LEARNING_ROADMAP.md).

## Away mode and precedence

Away settings live in `ScheduleConfiguration.settings.away_mode`, so they share
the scheduler's versioned persistence, optimistic revision and configuration
export. The activation source is exactly one of `off`, `calendar` or
`date_range`. Date/time values entered without an offset are interpreted in
Home Assistant's configured time zone and persisted as UTC ISO timestamps. A
date range is start-inclusive and end-exclusive.

The runtime listens to the selected calendar entity or adds the next date
boundary to its existing nearest-transition timer. Startup always calculates
the current state before applying targets, so a restart during an Away period
immediately restores the global Away target. Calendar unavailability is shown
to the administrator and does not activate Away. Only configured active rooms
are included.

Room-target precedence is:

1. active global Away target;
2. an enabled room's unoccupied setback;
3. an unexpired Quick Change hold;
4. the occupied stay's guest comfort offset;
5. the active weekly schedule period.

Away, unoccupied setback and Quick Change targets are reasserted if their
canonical ZEAL-Heat room thermostat is manually changed. In an occupied
occupancy-enabled room the same change updates the stay offset instead. A
legacy room's scheduled target retains its earlier until-next-transition manual
behaviour. Quick Change cannot create a new hold while Away is active; a
pre-existing hold remains in memory and resumes when Away ends if it has not
expired. The separate per-zone Manual Override remains the highest actuator
authority: it prevents ZEAL-Heat from changing that zone's pump/relay and is
not bypassed by Away.
