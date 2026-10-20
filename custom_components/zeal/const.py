"""Constants for the ZEAL HVAC System integration."""

DOMAIN = "zeal"
DEVICE_MANUFACTURER = "ZEAL"
DEVICE_MODEL = "Zoned heating controller"

# Home Assistant panel with administrator-only Setup. The query-string version is deliberately
# independent of the integration version so frontend-only fixes can invalidate
# the browser cache without changing stored data or the manifest.
PANEL_COMPONENT = "zeal-panel"
PANEL_URL_PATH = DOMAIN
PANEL_STATIC_URL = f"/{DOMAIN}_static"
PANEL_ASSET_VERSION = "30"

STORAGE_VERSION = 1
STORAGE_KEY_FMT = f"{DOMAIN}_{{entry_id}}"
SCHEDULE_STORAGE_VERSION = 1
SCHEDULE_STORAGE_KEY_FMT = f"{DOMAIN}.scheduler.{{entry_id}}"
AUDIT_STORAGE_VERSION = 1
AUDIT_STORAGE_KEY_FMT = f"{DOMAIN}.audit.{{entry_id}}"
AUDIT_MAX_ENTRIES = 500
LEARNING_STORAGE_VERSION = 1
# Payload schema is independent of Home Assistant's Store envelope. Version 2
# invalidates pre-room-fingerprint evidence, which cannot be matched safely.
LEARNING_DATA_VERSION = 2
LEARNING_STORAGE_KEY_FMT = f"{DOMAIN}.learning.{{entry_id}}"
LEARNING_MAX_EVENTS = 5000
LEARNING_MAX_PROPOSALS = 500
LEARNING_OBSERVATION_DAYS = 21
LEARNING_RETENTION_DAYS = LEARNING_OBSERVATION_DAYS * 2
LEARNING_EVIDENCE_THRESHOLD = 3
LEARNING_TEMPERATURE_TOLERANCE = 0.5
LEARNING_TIMING_WINDOW_MINUTES = 30

OCCUPANCY_STORAGE_VERSION = 1
OCCUPANCY_STORAGE_KEY_FMT = f"{DOMAIN}.occupancy.{{entry_id}}"

THERMAL_STORAGE_VERSION = 1
THERMAL_DATA_VERSION = 1
THERMAL_MODEL_STORAGE_KEY_FMT = f"{DOMAIN}.thermal.models.{{entry_id}}"
THERMAL_CHECKPOINT_STORAGE_KEY_FMT = f"{DOMAIN}.thermal.active.{{entry_id}}"
THERMAL_ROOM_STORAGE_KEY_FMT = f"{DOMAIN}.thermal.room.{{entry_id}}.{{room_id}}"
THERMAL_SAMPLE_RETENTION_DAYS = 30
THERMAL_MAX_SAMPLES_PER_ROOM = 2000
THERMAL_EPISODE_RETENTION_DAYS = 365
THERMAL_MAX_EPISODES_PER_ROOM = 750
THERMAL_CHECKPOINT_INTERVAL_SECONDS = 15 * 60

# Keys used inside config_entry.options
CONF_ZONES = "zones"
CONF_SHOW_IN_SIDEBAR = "show_in_sidebar"
CONF_STANDARD_USER_SCHEDULE = "standard_user_schedule"
CONF_STANDARD_USER_QUICK_CHANGE = "standard_user_quick_change"
CONF_LEARNING_ENABLED = "learning_enabled"
CONF_LEARNING_PERSISTENT_NOTIFICATIONS = "learning_persistent_notifications"

# Zone dict keys. A Zone is a user-named group of Rooms (e.g. "Ground
# Floor") with its own heating actuator switch(es) - it is NOT tied 1:1 to
# a single HA Area any more.
ZONE_ID = "zone_id"
ZONE_NAME = "name"
ZONE_ROOMS = "rooms"
# A zone has exactly ONE heating actuator switch (pump/relay) - never more.
# Real installs confirm this: a shared single-pump house split into
# ground-floor/first-floor zones, a dual-pump house with one switch per
# zone, a hotel with one switch per level. Holds an entity_id, or None if
# not yet configured for this zone.
ZONE_SWITCH = "switch"
# Heat source powering this zone. Affects the heating profile (how the
# source ramps output and whether cycling on/off is harmful or normal) and
# is used to pick a sensible *suggested* re-enable delay - see
# HEAT_SOURCE_DEFAULT_REENABLE_DELAY below. Also the same field §9.2 of the
# project doc will reuse for flow-temp/preheat regression once that's
# built, so it's not solely a Milestone 2 concern.
ZONE_HEAT_SOURCE = "heat_source"
# User-editable re-enable delay for this zone, in seconds. Pre-filled from
# HEAT_SOURCE_DEFAULT_REENABLE_DELAY[heat_source] when the zone is first
# configured, but stored as its own explicit value from then on - a
# suggestion, not a locked-in consequence of the heat_source choice.
ZONE_REENABLE_DELAY = "reenable_delay"

HEAT_SOURCE_ASHP = "ashp"
HEAT_SOURCE_MODULATING_BOILER = "modulating_boiler"
HEAT_SOURCE_NON_CONDENSING_BOILER = "non_condensing_boiler"
HEAT_SOURCE_OTHER = "other"
HEAT_SOURCE_OPTIONS = [
    HEAT_SOURCE_ASHP,
    HEAT_SOURCE_MODULATING_BOILER,
    HEAT_SOURCE_NON_CONDENSING_BOILER,
    HEAT_SOURCE_OTHER,
]

# --- v2 (long-term goal, hidden for now): ASHP heating + cooling --------
# Reserved schema fields for a future capability where a zone's ASHP can
# also provide cooling (via a physical retrofit - special radiators/fan
# coils rated for cold water without condensation risk). Not exposed in
# the Setup panel yet and NOT acted on by the Coordinator yet - these
# exist purely so the schema doesn't need another breaking migration once
# the real feature is built. See project doc for the full design
# (capability flag, per-room cooling-capable flag, and the "actively close
# non-cooling-capable rooms' TRVs in cooling mode" logic it requires).
ZONE_ASHP_CAPABILITY = "ashp_capability"
ASHP_CAPABILITY_HEAT_ONLY = "heat_only"
ASHP_CAPABILITY_HEAT_AND_COOL = "heat_and_cool"
# Per-room: does this room have cooling-rated emitters installed? Only
# meaningful when its zone's heat_source is ASHP and ashp_capability is
# heat_and_cool. False for every room until the physical retrofit exists
# and a room is deliberately marked otherwise - a standard radiator must
# NOT be sent cold water.
ROOM_COOLING_CAPABLE = "cooling_capable"

# Room dict keys. A Room IS an HA Area assigned to a Zone - ROOM_ID is the
# HA Area's own id, so an Area can only ever belong to one Zone at a time.
ROOM_ID = "room_id"
ROOM_NAME = "name"
ROOM_TRVS = "trvs"
ROOM_SENSORS = "sensors"
# Optional window/door contact sensors. If any configured entity is exactly
# "on" (open), the room remains active but is temporarily excluded from zone
# demand. ZEAL does not change or restore TRV targets for this feature.
ROOM_OPENING_SENSORS = "opening_sensors"
# Manual "does this room take part in heating demand" toggle, ported from
# the `active` flag in the old ashp_rooms.json (e.g. an unoccupied guest
# room, or a room like Ensuite/Bathroom that was permanently disabled).
ROOM_ACTIVE = "active"
# Optional per-room occupancy setback. This is deliberately separate from
# ROOM_ACTIVE: an unoccupied room remains part of ZEAL and can request heat to
# maintain its configured setback temperature.
ROOM_OCCUPANCY_ENABLED = "occupancy_enabled"
ROOM_UNOCCUPIED_TEMPERATURE = "unoccupied_temperature"
ROOM_OCCUPANCY_SOURCE = "occupancy_source"
ROOM_OCCUPANCY_CALENDAR = "occupancy_calendar"
OCCUPANCY_SOURCE_MANUAL = "manual"
OCCUPANCY_SOURCE_CALENDAR = "calendar"

# --- Milestone 2: Coordinator ---------------------------------------------
# ashp_controller.py's own changelog: hysteresis was removed in favour of a
# re-enable delay (see PROJECT_MANDATE.md, Decisions Log). DEFAULT_REENABLE_DELAY
# is now only the fallback used for HEAT_SOURCE_OTHER and for any zone saved
# before the per-zone field existed - see ZONE_REENABLE_DELAY above for the
# per-zone, user-editable value actually used by the Coordinator.
DEFAULT_SCAN_INTERVAL = 60  # seconds - periodic fallback poll
DEFAULT_REENABLE_DELAY = 300  # seconds - min time OFF before allowed back ON
MIN_TARGET_TEMPERATURE = 5.0
MAX_TARGET_TEMPERATURE = 30.0
OFFLINE_DEBOUNCE_SECONDS = 300
STALE_THRESHOLD_SECONDS = 4 * 60 * 60
# A climate.set_temperature service call normally produces its matching state
# event almost immediately. Keep that expected echo only briefly so an actual
# manual turn to the same temperature later is never mistaken for our own write.
SETPOINT_ECHO_TIMEOUT_SECONDS = 30
# A sleeping battery thermostat can accept a Home Assistant service call while
# deferring the radio command until its next wake/report. Never poll it with
# repeated writes: one retry is allowed only after a fresh report, and attempts
# are additionally rate-limited in case a device emits a burst of reports.
SETPOINT_CONFIRMATION_RETRY_MIN_SECONDS = 5 * 60
# A physical dial can emit several intermediate setpoints while it is being
# turned. Wait for it to settle before waking every other battery TRV in the
# room. Continuous/noisy input therefore produces no outbound write storm.
EXTERNAL_SETPOINT_SETTLE_SECONDS = 5

# Suggested re-enable delay per heat source, in seconds - a starting point
# reflecting how each source actually behaves, not a hard rule:
#   * ASHP: long, steady, low-temp runs suit it; short-cycling wears the
#     compressor and wastes energy on defrost cycles. Longest delay.
#   * Modulating (condensing) boiler: no compressor to protect, but
#     condensing efficiency still favours steadier running over rapid
#     on/off, so a moderate delay.
#   * Non-condensing boiler: fixed high flow temp, designed to cycle on
#     and off to satisfy demand - minimal harm from re-enabling quickly.
#   * Other/unknown: fall back to the original global default.
# See README "Heat sources and heating profiles" for the full explanation
# this table is based on.
HEAT_SOURCE_DEFAULT_REENABLE_DELAY = {
    HEAT_SOURCE_ASHP: 300,
    HEAT_SOURCE_MODULATING_BOILER: 120,
    HEAT_SOURCE_NON_CONDENSING_BOILER: 60,
    HEAT_SOURCE_OTHER: DEFAULT_REENABLE_DELAY,
}

# Keys inside the runtime Store payload (separate from CONF_ZONES, which is
# just the mirrored config). Store shape:
#   {CONF_ZONES: [...], RUNTIME_LAST_OFF: {zone_id: iso_timestamp}}
RUNTIME_LAST_OFF = "last_off_time"
