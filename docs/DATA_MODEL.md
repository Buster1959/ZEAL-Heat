# ZEAL-Heat V1 Data Model

ZEAL-Heat persists four deliberately separate documents for each Home Assistant
config entry. Separating them prevents runtime timing state, user configuration
and schedule history from accidentally replacing one another.

## Zone and room hierarchy

Zone configuration and the panel-navigation preference are stored in
config-entry options.

```json
{
  "show_in_sidebar": true,
  "zones": [
    {
      "zone_id": "example-zone",
      "name": "Example Floor",
      "switch": "switch.example_heating",
      "heat_source": "ashp",
      "reenable_delay": 300,
      "ashp_capability": "heat_only",
      "rooms": [
        {
          "room_id": "example-area-id",
          "name": "Example Room",
          "trvs": ["climate.example_trv"],
          "sensors": ["sensor.example_temperature"],
          "opening_sensors": ["binary_sensor.example_window"],
          "active": true,
          "occupancy_enabled": false,
          "unoccupied_temperature": 12.0,
          "occupancy_source": "manual",
          "occupancy_calendar": null,
          "cooling_capable": false
        }
      ]
    }
  ]
}
```

`show_in_sidebar` defaults to `true` for entries created before the preference
was introduced. It controls only whether the shared ZEAL-Heat link appears in Home
Assistant navigation; it does not enable or disable the integration. With
multiple loaded instances, the link is visible if any instance requests it.

`room_id` is the stable Home Assistant Area ID. One Area can belong to only one
ZEAL-Heat zone. `ashp_capability` and `cooling_capable` are reserved safe-default V2
fields; no V1 cooling behavior uses them.

`opening_sensors` contains optional window/door `binary_sensor` entities in the
room's Area. If any reports `on`, the room remains active but contributes no
zone heating demand. This state does not change its canonical or physical
thermostat target.

Occupancy setback is opt-in and distinct from `active`. An unoccupied room
remains eligible to request heat at `unoccupied_temperature`. Its source is
`manual` or one validated Home Assistant `calendar` entity. Existing rooms
default to `occupancy_enabled: false`.

## Schedule document

The version-1 scheduler store contains:

- `version` — schema version, currently `1`;
- `temperature_unit` — `°C`, `°F` or `null` until established;
- `rooms` — a map keyed by stable ZEAL-Heat room ID;
- `settings` — integration-wide scheduler settings, including Away mode.

Every room contains exactly Monday through Sunday. Each day is an ordered list
of periods with a stable ID, display names, exact 24-hour `HH:MM` time and a
finite 5–30°C target. A room can have at most four periods per day through the
V1 editor.

A period remains effective until a later period takes over, including across
midnight and across empty days. Copying a week duplicates only the schedule;
the destination room's ID, zone, Area and physical equipment are preserved.

## Runtime state

The Coordinator store contains transient safety state such as each zone's last
actuator-off time. A separate version-1 occupancy Store keeps each enabled
room's last resolved occupancy, calendar manual override, stay start and guest
comfort offset. This lets restart reconciliation retain a stay without changing
the schedule revision. Removed or disabled occupancy rooms are pruned.
Temporary Quick Change holds are runtime state: they do not edit the saved
weekly schedule. Away configuration is durable and is reconciled against
calendar/date state after restart.

## Audit document

The audit store has schema version `1` and retains the newest 500 canonical
room-target outcomes. Each record contains timestamp, stable room identity,
canonical entity ID, previous and requested temperatures, cause and outcome.
It contains no credentials or Home Assistant tokens.

The configuration snapshot exposes the newest successful audit record for each
room as `last_changes`; this is a derived Overview view, not a fourth persisted
document.

### Schedule Adaptation learning audit (`0.14.7`)

Learning uses a separate versioned Store rather than overloading the V1
application audit. The initial event model records a stable event ID, room,
timestamp/local date, source (Home Assistant/canonical thermostat, physical TRV
or Quick Change), requested target, immutable schedule-period baseline and
revision, adaptation type, pattern key and outcome. Source attribution is based
on ZEAL-Heat's control boundary and write-echo guards, not inferred from display text.
Room temperature, demand/actuator context, effective target and override expiry
remain planned enrichments; the detector does not claim to use fields it has not
captured.

The Home Assistant Store envelope remains version 1, while the Learning payload
schema is version 2. Version-1 payload evidence is deliberately discarded on
load because it predates `room_schedule_revision` and cannot participate in a
safe match. This prevents obsolete, permanently unmatchable events lingering
through the 42-day retention window; pre-V1 Learning data has no compatibility
guarantee.

Repeated-change proposals are stored separately from raw events. A proposal
records its evidence event IDs/count, pattern, immutable current period,
proposed schedule edit and state (`new`, `accepted`, `dismissed`, `snoozed`,
`conflicted` or `reverted`). Accepting or editing a proposal creates a normal
schedule revision and records the approving user and resulting revision; the
learner never mutates the schedule document directly.

The Store retains events for at most 42 days and also caps them at 5,000; it
retains at most 500 proposals. Learning data is
not included in the existing configuration/audit downloads. Any future Learning
export must warn that this history may reveal occupancy routines even though it
contains no Home Assistant credentials.

### Planned room thermal-response documents

Thermal learning uses no database. It will use a small versioned model index for
the config entry, one small versioned active-checkpoint Store for the config
entry, and one bounded Home Assistant Store document per stable room ID.
Partitioning prevents a five-minute Lounge sample from rewriting every other
room's history, while separating the checkpoint prevents a safety save from
rewriting even the Lounge's completed history.

Each room document contains detailed samples and completed episode summaries.
The checkpoint Store contains only active episodes, keyed by stable room ID,
with the minimum state required for restart recovery and deduplication. It is
saved at important transitions, at least every 15 minutes while active, and by
Home Assistant's final shutdown-write path. A completed episode is committed to
its room history when it closes; routine checkpointing never rewrites retained
room history. Samples are recorded every five minutes only while heating,
warm-up, cooldown or an exclusion hold is relevant. They distinguish
measured outdoor temperature from forecast provenance and retain source,
timestamps, room temperature, effective/scheduled target, demand/actuator state,
available heat-input signals, validity/exclusion reason and model version.

A detailed sample is retained only while it satisfies both limits: no older
than 30 days and within the newest 2,000 for that room. Completed intervals are
compacted into at most 750 episode summaries and retained for no more than 365
days. Age and count pruning runs on load and save. Stable sample/episode IDs and
timestamps prevent restart duplication; an unsafe recovery gap closes the
checkpoint as an interrupted episode.

For the eight-room planning home (four bedrooms, two bathrooms, kitchen and
dining room), two two-hour episodes per room/day produce 11,520 detailed
30-day samples and 5,840 annual summaries. The enforced caps are 16,000 samples
and 6,000 summaries across those rooms. Using provisional serialized-record
allowances, ZEAL-Heat expects about 7–12 MB, plans for a 9–14 MB capped payload and
documents a conservative 20 MB allowance per config entry. Detailed history is
loaded per selected room for the administrator graph; it is not all retained in
memory or copied into Recorder entity attributes. Final schema benchmarks must
confirm these estimates before release.

Every document declares its Store envelope and payload/model versions. Schema
migrations are explicit and sequential. A changed derived-model version is
rebuilt from compatible retained evidence; an unknown newer version stops
Thermal learning with an administrator-visible error and is never overwritten.

Each derived room model records its effective thermal mass/heat-loss parameters,
response delay, training range, evidence count, last trained time, model version
and confidence. Raw observations remain the reproducible evidence; derived
parameters can be rebuilt after a model migration. A model never crosses room
IDs, and missing/stale weather data does not modify the saved schedule.

Disabling Thermal Response asks the administrator to keep or permanently delete
the data. Keep is the default and stops collection while retention remains in
force. Delete removes every thermal room document, model and checkpoint for the
entry. Per-room/all-room resets and disable-time deletion never alter schedules,
room configuration, Quick Changes, Away state or the ordinary application
audit.

Ordinary diagnostics expose pseudonymised room/zone/entity aliases, counts,
model health and exclusion totals. A separately requested readable export may
include names and evidence only after an occupancy-privacy warning. Removing the
ZEAL-Heat config entry removes all of its Thermal Response Store documents.

Configuration and audit downloads necessarily include room/zone names and
entity IDs. Review exports before sharing if those names reveal personal
information.

### Recorded room demand

Each actionable room has one derived `binary_sensor` with a stable unique ID.
Its state is On only when the room contributes to zone heat demand after
room-level eligibility, thermostat, temperature-sensor and opening-suppression
checks. Its attributes expose the current reason, setpoint, measured room
temperature and any open configured contacts. Re-enable delay, Zone Manual
Override and pump dead-head protection remain zone-level actuator decisions, so
a room may correctly show demand while the actuator is held off. Home Assistant
Recorder owns this entity's history; ZEAL-Heat creates no duplicate history Store.
