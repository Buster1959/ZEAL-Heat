# ZEAL-Heat Panel API Architecture

Status: Block 4 backend, Block 5 Overview/Setup, Block 6 Schedule, Block 7
Quick Change/download and Block 8 Away consumers complete.

## Panel boundary

The integration registers `/zeal` as a versioned custom Home Assistant panel.
While its optional sidebar link is visible, it is also the integration's
Configure destination. When every loaded instance hides the link, Home
Assistant's Configure action opens a native recovery Options Flow that can
restore sidebar access; the direct `/zeal` route remains registered throughout.
The panel route and read-only Overview are available to every authenticated Home
Assistant user. Administrators can separately grant standard users access to
Schedule and Quick Change from Setup; both grants default off. Setup works on a
browser-side copy and submits the entire hierarchy
only when **Save setup** is selected. Existing config-entry options remain the
source of truth, so configurations created with the retired Options Flow require
no migration.

The frontend escapes registry-derived names and entity IDs before rendering,
retains hidden forward-compatible schema fields, filters room equipment by Area,
prevents assigning one Area to multiple zones in the editor and relies on the
backend to revalidate every submitted value. After a successful hierarchy save,
it tolerates the expected short config-entry reload window and accepts the
result only when the returned revision matches.

The catalog has two non-overlapping thermostat collections:

- `zeal_room_thermostats` contains only live ZEAL-Heat-owned canonical room targets,
  identified by registry ownership and mapped to stable room/zone IDs;
- `physical_room_thermostats` contains only non-ZEAL-Heat climate entities available
  to the Area-scoped equipment picker.

Temperature sensors and actuator switches also exclude ZEAL-Heat-owned entities. The
frontend never derives ownership from a display name, and the hierarchy writer
independently rejects a ZEAL-Heat-owned entity submitted as physical equipment.

The Schedule consumer navigates the hierarchy but sends only a stable ZEAL-Heat
room ID and validated daily lists. It names and displays the canonical ZEAL-Heat room
thermostat for clarity; physical thermostat entity IDs never enter a schedule
write or copy request.

Quick Change uses the same stable room IDs and supports room, Zone/Floor,
arbitrary multi-room and whole-house selection. Its responses distinguish the
saved scheduled target, effective target, expiry and active override. Holds are
runtime-only and never share a write path with the schedule document.

Overrides' Away card selects one of Off, a registry-backed Home Assistant calendar
or one start/end period and a 1–30°C global target. The backend validates the
calendar owner/domain and disabled state, interprets offset-free browser values
in Home Assistant's configured time zone, enforces five-minute manual intervals,
and returns live activation status in configuration and Quick Change responses.
The frontend blocks Quick Change
application while Away is active and displays the current authority globally.

## Security boundary

Read-only panel bootstrap, configuration and zone-control commands accept an
authenticated user. Schedule, Quick Change and Learning commands accept an administrator
or a standard user explicitly granted that feature for the selected instance.
Hierarchy, Away settings, downloads and audit commands retain Home Assistant's
administrator requirement. Each command also requires an explicit loaded
config-entry ID; no command silently selects the first ZEAL-Heat instance.

The hierarchy writer treats browser data as untrusted. It validates:

- unique zone IDs, Area/room IDs, zone switches and room equipment;
- Areas and entities against Home Assistant's current registries;
- correct `switch`, `climate` and temperature-`sensor` domains/classes;
- that selected room equipment belongs to the selected Area;
- that disabled or ZEAL-Heat-owned entities cannot be selected as physical devices;
- heat-source values, boolean room state and the 0–3600 second re-enable range;
- schedules, times and temperatures through the versioned model, including the
  1–30°C ZEAL-Heat safety range and physical thermostat limits.

## Conflict protection

Read responses contain a deterministic `revision` derived from the saved zone
and room hierarchy, complete schedule/Away document, sidebar preference,
standard-user Schedule and Quick Change grants, and Learning enable and
persistent-notification preferences. It deliberately changes for every setting
the panel can save, preventing a stale browser from writing over any newer panel
change. Every hierarchy or schedule write must include
the revision it was edited from. If either document changed in another browser
tab or after a reload, the write fails with `conflict`; the newer data is never
overwritten.

## Commands

- `zeal/list_entries`: loaded ZEAL-Heat instances.
- `zeal/get_configuration`: hierarchy, schedule, Quick Change state, latest
  successful room-target applications, revision and eligible Area/entity
  catalog.
- `zeal/save_hierarchy`: validated full hierarchy/sidebar-preference update and
  safe config-entry reload.
- `zeal/update_room_days`: validated seven-day update for one stable room ID.
- `zeal/copy_room_schedule`: save source editor state and copy only its daily
  periods to selected rooms.
- `zeal/get_quick_change`, `zeal/set_temporary_override` and
  `zeal/clear_temporary_override`: transient targets that never edit schedules.
- `zeal/get_zone_control`: live actuator, demand and re-enable-delay state for
  the Overview zone pane.
- `zeal/get_learning`: retained evidence and Schedule Adaptation proposals.
- `zeal/decide_learning_proposal`: Schedule-authorised accept, edit-and-accept,
  snooze, dismiss and revert decisions; disabled while Learning is off.
- `zeal/save_away_mode`: revision-protected persisted activation source and
  global Away target.
- `zeal/export_configuration`: JSON-ready hierarchy/schedule download document.
- `zeal/get_audit_log`: JSON-ready bounded application history.

Overview History icons do not add a ZEAL-Heat history API. The configuration catalog
returns the registered entity IDs for canonical room thermostats and effective
room-demand binary sensors, including user-renamed IDs. The panel builds a
relative `/history?entity_id=...` link from those IDs plus the configured zone
actuator, temperature sensors and opening contacts. Home Assistant owns the
route, date controls, graph rendering and retention.

The planned presentation boundary keeps the existing authorization model:
Setup owns the administrator Schedule Adaptation Yes/No control and a compact
progress summary. **Schedule Updates** is shown only while actionable proposals
exist and only to an administrator or a standard user with the Schedule/Learning
grant. Hiding the page does not delete retained events or proposal history.

Thermal Response has a separate administrator-only boundary. When enabled its
Learning page remains visible for per-room status, confidence and graph history;
standard users cannot retrieve or operate it. Setup owns onboarding, source and
room scope, privacy disclosure, and confirmed reset-one/reset-all operations.
Reset commands delete only the selected learned data and must not mutate
configuration, schedules or overrides.

## Persistence and runtime ordering

Schedule writes are validated, persisted, then applied to the running scheduler.
Hierarchy writes are validated, reconcile schedule records to the new stable
room IDs, persist the reconciled schedule, update config-entry options and let
the standard ZEAL-Heat update listener reload the integration. Automated coverage
proves the hierarchy and reconciled schedule survive this reload.

## Audit privacy and retention

The audit Store is separate from configuration and Coordinator state. It keeps
the newest 500 canonical room-target outcomes. Records contain timestamp, stable
room ID/name, canonical ZEAL-Heat thermostat ID, previous/requested temperature,
cause and outcome. They do not contain credentials, tokens, location coordinates
or physical-TRV service payloads.

The Overview derives one latest successful change per room from this bounded
audit store; failed/unavailable attempts do not replace the last applied target.

The Setup consumer turns fresh export responses into timestamped JSON browser
downloads. The backend does not write files into Home Assistant's configuration
directory. Configuration exports contain the saved hierarchy, schedule,
revision, current runtime view and eligible entity catalog. Audit downloads
contain the bounded persisted outcome history.

Instance removal deliberately uses Home Assistant's administrator-only config
entry deletion endpoint after an explicit browser confirmation. ZEAL-Heat's standard
`async_remove_entry` hook then removes only the selected entry's hierarchy,
schedule and audit Stores. The panel also links to Home Assistant's native
integration page for per-instance disable controls.
