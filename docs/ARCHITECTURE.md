# ZEAL-Heat Architecture

ZEAL-Heat is one self-contained Home Assistant custom integration. It owns its
configuration UI, room thermostat entities, heating-demand coordinator,
scheduler, temporary holds, Away mode, persistence and audit trail.

It does not import or call Visual Climate Scheduler. The two integrations can
be installed independently. They should not both schedule the same physical
thermostat because competing setpoint writers can repeatedly overwrite each
other.

## Runtime layers

1. **Config entry and panel** — `__init__.py`, `config_flow.py`, `panel.py` and
   `frontend/zeal-panel.js` register the integration and its authenticated-user
   panel. Overview is available to every signed-in user; Setup and configuration
   administration remain administrator-only. Schedule, Learning and Quick
   Change can be granted to standard users through explicit Setup permissions.
2. **Validated API** — `scheduler/websocket_api.py` and
   `scheduler/configuration.py` authorize, validate and conflict-protect every
   panel read or write.
3. **Durable models** — zone/room configuration is held in config-entry
   options; coordinator safety state, schedules/Away settings, application audit
   occupancy stay state and Schedule Learning evidence use five separate
   versioned Home Assistant
   Stores. The dormant Thermal Response foundation adds a small model index, a
   separate active-checkpoint Store and lazy per-room history Stores; it does
   not collect observations until that later module is explicitly enabled.
4. **Scheduler runtime** — `scheduler/runtime.py` resolves Away, per-room
   occupancy setback, Quick Change, stay-long guest offsets and weekly
   schedules, then writes only to the canonical ZEAL-Heat room thermostat.
5. **Learning** — `scheduler/learning.py` captures qualified intent above the
   runtime boundary and creates reviewable proposals without writing schedules.
6. **Heating control** — `coordinator.py` propagates that room target to the
   configured physical TRVs, suppresses a room's demand while any configured
   window/door contact is open, evaluates remaining room demand and safely
   controls the zone actuator. Opening suppression never writes a setback.
   `binary_sensor.py` publishes that resolved room contribution as a recorded
   On/Off entity with its current reason, setpoint, temperature and open-contact
   context; zone-level actuator holds remain separate.

Each config entry owns independently keyed Coordinator, schedule, occupancy,
audit, Schedule Learning and Thermal Response Stores. Multiple entries can therefore
run separate heating systems. Removing one entry deletes only its own Stores;
the panel remains registered while another ZEAL-Heat entry is loaded.

## Entity boundary

Each configured room has one stable room ID and one ZEAL-Heat-owned climate entity.
Schedules target that canonical room thermostat, never an individual physical
TRV. Setup keeps two separate catalogs:

- ZEAL-Heat room thermostats are shown as scheduling targets.
- Non-ZEAL-Heat climate entities in the room's Home Assistant Area are shown as
  physical thermostat/TRV choices.
- Window, door and opening-class binary sensors in that Area are offered as
  optional demand-suppression contacts.

The separation uses Home Assistant entity-registry ownership, so renaming an
entity cannot make a ZEAL-Heat thermostat selectable as its own physical TRV.

## Safety and precedence

Room target precedence is:

1. active Away mode;
2. an enabled room's unoccupied setback;
3. active Quick Change hold;
4. an occupied stay's guest comfort offset;
5. the weekly schedule.

Occupancy is disabled by default. A guest adjustment is calculated from the
underlying weekly target, not a previously offset target, and is carried across
schedule boundaries until occupancy ends.

The per-zone Manual override is separate and remains the highest authority over
the physical actuator. Every target passes through ZEAL-Heat's 5–30°C clamp before
it can reach a physical thermostat. Unavailable or stale readings are excluded
from decisions, and an all-valves-closed result forces the zone actuator off.

## Further technical detail

- [ZEAL-Heat Learning roadmap](LEARNING_ROADMAP.md)
- [Panel API architecture](PANEL_API_ARCHITECTURE.md)
- [Scheduler architecture](SCHEDULER_ARCHITECTURE.md)
- [Data model](DATA_MODEL.md)
- [Decision summary](DECISIONS.md)
