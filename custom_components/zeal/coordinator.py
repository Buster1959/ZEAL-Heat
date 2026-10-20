"""Coordinator for ZEAL HVAC System.

Ports the control loop from the old `ashp_controller.py` (evaluate_floor /
set_switch) onto the new zone/room schema. Two deliberate corrections vs.
the original, both captured with their rationale in PROJECT_MANDATE.md:

  * Anti-hunting uses the **re-enable delay** (a zone switch that just
    turned OFF won't turn back ON for `DEFAULT_REENABLE_DELAY` seconds),
    not hysteresis - hysteresis was already dead code in the version of
    ashp_controller.py that was actually running.
  * The manual "hands-off this zone" override is now an integration-created
    `switch` entity per zone (see switch.py), not a hand-created
    `input_boolean` helper.

New in this schema vs. the original (which was always exactly one TRV and
one sensor per room): a room can have *multiple* TRVs and/or sensors.
  * Room temperature = the **average** of all its active sensors' readings
    (reduces single-sensor noise; standard practice for multi-sensor rooms).
  * Room setpoint = read from that room's ZealRoomThermostat entity (see
    climate.py) - a single per-room master the Coordinator treats as the
    room's actual source of truth. Physical TRVs are slaved to it: this
    Coordinator propagates the thermostat's target_temperature out to
    every TRV in the room whenever it changes, and conversely, detects an
    unexpected change on any *physical* TRV and both updates the
    thermostat to match and re-propagates to the room's other TRVs - so a
    manual adjustment on any one TRV becomes the room's setpoint
    everywhere, not just on the TRV someone happened to touch. This
    supersedes an earlier "highest setpoint among the room's TRVs" default
    and a separate planned-but-never-built 2-hour "boost" mechanic - both
    are obsolete now that there's a real per-room entity to be the
    setpoint authority instead of inferring one from N TRVs' raw states.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ZONES,
    DEFAULT_REENABLE_DELAY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EXTERNAL_SETPOINT_SETTLE_SECONDS,
    MAX_TARGET_TEMPERATURE,
    MIN_TARGET_TEMPERATURE,
    OFFLINE_DEBOUNCE_SECONDS,
    ROOM_ACTIVE,
    ROOM_ID,
    ROOM_NAME,
    ROOM_OPENING_SENSORS,
    ROOM_SENSORS,
    ROOM_TRVS,
    RUNTIME_LAST_OFF,
    SETPOINT_CONFIRMATION_RETRY_MIN_SECONDS,
    SETPOINT_ECHO_TIMEOUT_SECONDS,
    STALE_THRESHOLD_SECONDS,
    ZONE_ID,
    ZONE_HEAT_SOURCE,
    ZONE_NAME,
    ZONE_REENABLE_DELAY,
    ZONE_ROOMS,
    ZONE_SWITCH,
)

_LOGGER = logging.getLogger(__name__)

UNAVAILABLE_STATES = (None, "unavailable", "unknown")


@dataclass
class ZoneStatus:
    """Snapshot of one zone's most recent evaluation, for entities to read."""

    zone_id: str
    zone_name: str
    needs_heat: bool
    demand_lines: list[str] = field(default_factory=list)
    switches_ok: bool = True  # False if every configured switch was unavailable


@dataclass(frozen=True)
class RoomDemandStatus:
    """One room's resolved contribution before zone-level actuator holds."""

    room_id: str
    room_name: str
    needs_heat: bool
    reason: str
    setpoint: float | None = None
    temperature: float | None = None
    open_entities: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingSetpointWrite:
    """One short-lived climate service-call echo that ZEAL expects to see."""

    temperature: float
    expires_at: datetime


@dataclass(frozen=True)
class UnconfirmedSetpointWrite:
    """Latest target accepted by HA but not yet reported by the device."""

    temperature: float
    observed_reported_at: datetime
    last_attempt_at: datetime


class ZealCoordinator(DataUpdateCoordinator[dict[str, ZoneStatus]]):
    """Polls TRVs/sensors, evaluates demand per zone, drives zone switches."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: Store) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.entry = entry
        self.store = store
        self.zones: list[dict[str, Any]] = list(entry.options.get(CONF_ZONES, []))

        # zone_id -> datetime of last OFF transition, restored from Store.
        self._last_off_time: dict[str, datetime] = {}
        # zone_id -> datetime of last ON transition (duration logging only).
        self._last_on_time: dict[str, datetime] = {}

        # Populated by ZealOverrideSwitch.async_added_to_hass() /
        # removed on async_will_remove_from_hass(). Checking the live entity
        # object in-process avoids a hass.states.get() round trip and any
        # guesswork about the override switch's entity_id.
        self.override_switches: dict[str, Any] = {}

        # Populated by ZealRoomThermostat.async_added_to_hass() /
        # removed on async_will_remove_from_hass() - same live-object
        # pattern as override_switches above. Each room's thermostat is
        # the room's actual setpoint authority (see climate.py); physical
        # TRVs are slaved to it, not the other way around.
        self.room_thermostats: dict[str, Any] = {}

        # entity_id (TRV) -> short-lived writes we expect the state listener
        # to echo. More than one can be in flight on slow radio hardware, and
        # the resulting state events can arrive out of order. Each echo is
        # one-shot and expiring so a later real manual turn is never hidden.
        self._pending_setpoint_writes: dict[str, list[PendingSetpointWrite]] = {}

        # Battery Z-Wave thermostats can be asleep when HA accepts a climate
        # service call. Keep only the latest requested value until the entity
        # actually reports it. A retry is allowed only after a new device
        # report, which avoids minute-by-minute writes and battery drain.
        self._unconfirmed_setpoint_writes: dict[
            str, UnconfirmedSetpointWrite
        ] = {}

        # Room propagation is latest-wins. A slow Z-Wave service call must not
        # leave a queue of obsolete temperatures which ZEAL then replays into
        # the room, creating a self-sustaining feedback storm.
        self._queued_room_setpoints: dict[str, float] = {}
        self._rooms_propagating_setpoints: set[str] = set()

        # room_id -> newest external-change generation. Physical dial events
        # are debounced until quiet so a noisy or continuously moving device
        # cannot wake and drain every other battery TRV in the room.
        self._external_setpoint_generation: dict[str, int] = {}

        # Physical TRVs which missed a propagation because they were
        # unavailable (or rejected a service call). They are retried on a
        # Coordinator update and explicitly restored to the canonical room
        # target when their state changes back to usable.
        self._unsynced_trvs: set[str] = set()

        # entity_id -> first unhealthy observation. Notifications are
        # debounced so a brief radio dropout does not alarm the household.
        self._entity_unhealthy_since: dict[str, datetime] = {}
        self._entity_offline_notified: set[str] = set()

        # Updated during every Coordinator evaluation. Binary sensor entities
        # read this immutable snapshot rather than reconstructing historical
        # demand from temperature and setpoint after the fact.
        self._room_demand_statuses: dict[str, RoomDemandStatus] = {}

        self._unsub_state_listener: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------
    async def async_setup(self) -> None:
        """Restore persisted runtime state and start listening for changes."""
        stored = await self.store.async_load() or {}
        for zone_id, iso_ts in stored.get(RUNTIME_LAST_OFF, {}).items():
            parsed = dt_util.parse_datetime(iso_ts)
            if parsed is not None:
                self._last_off_time[zone_id] = parsed

        self._register_state_listener()

    async def async_log_startup_banner(self) -> None:
        """Log a single, visually-bounded block covering every zone and
        room, at .info() level (visible with no debug logging needed).

        Deliberately verbose and deliberately fenced with separator lines
        - this only runs once per HA restart (the user restarts the whole
        of HA on every code change, not just reloads the integration, so
        this reliably marks the start of each real testing session), and
        its whole purpose is to make it visually unmistakable in a raw log
        where a session starts, and give a complete picture of what's
        configured without needing to enable debug logging or inspect every
        card in the ZEAL Setup panel.
        """
        # Read the version straight from the loaded manifest, not a
        # hardcoded/duplicated constant - so this always reflects exactly
        # what's running. Motivated by a real incident where a pasted
        # stack trace's line numbers didn't match any deployed version,
        # with no way to tell from the log alone whether it was current.
        try:
            integration = await async_get_integration(self.hass, DOMAIN)
            version = integration.manifest.get("version", "unknown")
        except Exception:  # noqa: BLE001 - version string is diagnostic only
            version = "unknown"

        sep = "=" * 60
        _LOGGER.info(sep)
        _LOGGER.info("ZEAL starting up - version %s", version)
        _LOGGER.info(sep)

        if not self.zones:
            _LOGGER.info("No zones configured yet - nothing for the Coordinator to action.")
            _LOGGER.info(sep)
            return

        total_actioned = 0
        total_active_rooms = 0

        for zone in self.zones:
            zone_name = zone.get(ZONE_NAME, zone.get(ZONE_ID))
            switch = zone.get(ZONE_SWITCH)
            rooms = zone.get(ZONE_ROOMS, []) or []

            if not switch:
                _LOGGER.info(
                    "Zone '%s': NOT ACTIONED - no switch configured (%d room(s) "
                    "defined but irrelevant until a switch is set)",
                    zone_name,
                    len(rooms),
                )
                continue

            total_actioned += 1
            heat_source = zone.get(ZONE_HEAT_SOURCE, "ashp")
            delay = zone.get(ZONE_REENABLE_DELAY, DEFAULT_REENABLE_DELAY)
            active_rooms = [r for r in rooms if r.get(ROOM_ACTIVE, True)]
            inactive_rooms = [r for r in rooms if not r.get(ROOM_ACTIVE, True)]
            total_active_rooms += len(active_rooms)

            _LOGGER.info(
                "Zone '%s': ACTIONED - switch=%s, heat_source=%s, "
                "reenable_delay=%ss, %d room(s) (%d active, %d inactive)",
                zone_name,
                switch,
                heat_source,
                delay,
                len(rooms),
                len(active_rooms),
                len(inactive_rooms),
            )

            for room in active_rooms:
                room_name = room.get(ROOM_NAME, room.get(ROOM_ID))
                trvs = room.get(ROOM_TRVS, []) or []
                sensors = room.get(ROOM_SENSORS, []) or []
                thermostat = self.room_thermostats.get(room.get(ROOM_ID))
                _LOGGER.info(
                    "  - %s: ACTIVE, %d TRV(s) %s, %d sensor(s) %s, thermostat=%s",
                    room_name,
                    len(trvs),
                    trvs or "(none configured)",
                    len(sensors),
                    sensors or "(none configured)",
                    getattr(thermostat, "entity_id", "not yet registered"),
                )
            for room in inactive_rooms:
                room_name = room.get(ROOM_NAME, room.get(ROOM_ID))
                _LOGGER.info("  - %s: INACTIVE - excluded from demand", room_name)

        _LOGGER.info(sep)
        _LOGGER.info(
            "ZEAL startup complete: %d zone(s) actioned, %d active room(s) "
            "total, %d restored last-off timestamp(s)",
            total_actioned,
            total_active_rooms,
            len(self._last_off_time),
        )
        _LOGGER.info(sep)

    @callback
    def async_teardown(self) -> None:
        """Cancel the state-change listener on unload."""
        if self._unsub_state_listener is not None:
            self._unsub_state_listener()
            self._unsub_state_listener = None

    def _register_state_listener(self) -> None:
        """Watch every active room's TRVs/sensors for near-instant response.

        Mirrors the old per-entity `listen_state` calls in
        ashp_controller.py, layered on top of the periodic poll rather than
        replacing it - either one alone would miss cases the other catches
        (a state change between polls vs. an entity that silently stops
        updating).
        """
        entity_ids: set[str] = set()
        for zone in self.zones:
            switch = zone.get(ZONE_SWITCH)
            if switch:
                # Tracking the switch itself, not just TRVs/sensors, closes
                # a real gap: without this, a switch that's momentarily
                # unavailable at HA startup (e.g. a template-platform
                # switch that hasn't finished initialising yet - see the
                # Decisions Log) only gets re-checked on the next periodic
                # poll, up to 60s later, once it does become available.
                # Tracking it means that transition triggers an immediate
                # re-evaluation instead. Also lets ZEAL react promptly if
                # a switch is toggled manually/externally, outside ZEAL's
                # own control - not something acted on specially yet, but
                # better to see it happen immediately than up to 60s late.
                entity_ids.add(switch)
            for room in zone.get(ZONE_ROOMS, []):
                if not room.get(ROOM_ACTIVE, True):
                    continue
                entity_ids.update(room.get(ROOM_TRVS, []) or [])
                entity_ids.update(room.get(ROOM_SENSORS, []) or [])
                entity_ids.update(room.get(ROOM_OPENING_SENSORS, []) or [])

        if not entity_ids:
            return

        self._unsub_state_listener = async_track_state_change_event(
            self.hass, list(entity_ids), self._async_handle_tracked_state_change
        )

    @callback
    def _async_handle_tracked_state_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        old_state = event.data["old_state"]

        new_val = new_state.state if new_state else None
        old_val = old_state.state if old_state else None
        _LOGGER.debug("Tracked entity changed: %s (%s -> %s)", entity_id, old_val, new_val)

        room = self._find_room_for_trv(entity_id)
        was_usable = old_state is not None and old_state.state not in UNAVAILABLE_STATES
        is_usable = new_state is not None and new_state.state not in UNAVAILABLE_STATES

        # A recovering TRV can still be holding an old local target. The
        # canonical ZEAL room thermostat wins on recovery; otherwise that
        # stale target could be misread below as a new manual adjustment and
        # overwrite every thermostat in the room.
        if room is not None and is_usable and not was_usable:
            self._pending_setpoint_writes.pop(entity_id, None)
            self._unconfirmed_setpoint_writes.pop(entity_id, None)
            self._unsynced_trvs.add(entity_id)
            self.hass.async_create_task(self._async_resync_recovered_trv(room, entity_id))
            self.hass.async_create_task(self.async_request_refresh())
            return

        # Only TRVs carry a "temperature" attribute we care about here;
        # sensor state changes fall through to the plain refresh below.
        if new_state is not None and old_state is not None:
            new_temp = new_state.attributes.get("temperature")
            old_temp = old_state.attributes.get("temperature")
            if new_temp is not None and new_temp != old_temp:
                if room is not None:
                    _LOGGER.debug(
                        "TRV setpoint change detected: %s (%s -> %s°C) in room %s",
                        entity_id,
                        old_temp,
                        new_temp,
                        room.get(ROOM_NAME, room.get(ROOM_ID)),
                    )
                    self.hass.async_create_task(
                        self._async_handle_external_trv_change(room, entity_id, new_temp)
                    )

        self.hass.async_create_task(self.async_request_refresh())

    # ------------------------------------------------------------------
    # Core evaluation loop
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, ZoneStatus]:
        await self._async_check_entity_health()
        await self._async_retry_unconfirmed_setpoint_writes()
        await self._async_retry_unsynced_trvs()
        results: dict[str, ZoneStatus] = {}
        self._room_demand_statuses = {}
        off_time_changed = False
        _LOGGER.debug("Evaluation cycle starting (%d zone(s) configured)", len(self.zones))

        for zone in self.zones:
            zone_id = zone[ZONE_ID]
            zone_name = zone.get(ZONE_NAME, zone_id)

            if not zone.get(ZONE_SWITCH):
                # Nothing to control for this zone - skip entirely, same as
                # ashp_controller.py's `if "switch" not in floor: continue`.
                _LOGGER.debug("[%s] No switch configured, skipping zone entirely", zone_name)
                continue

            needs_heat, demand_lines = self._evaluate_zone(zone)

            if needs_heat and self._zone_all_trvs_off(zone):
                _LOGGER.info(
                    "[%s] Every TRV is off (valves closed) - forcing pump off "
                    "immediately regardless of temperature demand, to avoid "
                    "running against a fully closed loop",
                    zone_name,
                )
                needs_heat = False
                demand_lines = ["All TRVs off - pump held off to avoid a closed loop"]

            _LOGGER.debug(
                "[%s] needs_heat=%s%s",
                zone_name,
                needs_heat,
                f" ({'; '.join(demand_lines)})" if demand_lines else "",
            )
            switches_ok, zone_off_changed = await self._async_apply_zone_switches(
                zone, needs_heat
            )
            if zone_off_changed:
                off_time_changed = True

            results[zone_id] = ZoneStatus(
                zone_id=zone_id,
                zone_name=zone_name,
                needs_heat=needs_heat,
                demand_lines=demand_lines,
                switches_ok=switches_ok,
            )

        if off_time_changed:
            await self._async_persist_runtime_state()

        _LOGGER.debug("Evaluation cycle complete")
        return results

    def _evaluate_zone(self, zone: dict[str, Any]) -> tuple[bool, list[str]]:
        """Return (needs_heat, demand_lines) for one zone.

        needs_heat is True if *any* active room in the zone is colder than
        its setpoint (see module docstring for how multi-TRV/sensor rooms
        are aggregated).
        """
        needs_heat = False
        demand_lines: list[str] = []

        for room in zone.get(ZONE_ROOMS, []):
            status = self._evaluate_room(zone, room)
            self._room_demand_statuses[status.room_id] = status
            if not status.needs_heat:
                continue
            needs_heat = True
            diff = round(float(status.setpoint) - float(status.temperature), 2)
            demand_lines.append(
                f"{status.room_name}: Set {status.setpoint}°C, "
                f"Room {status.temperature}°C (Δ {diff}°C)"
            )

        return needs_heat, demand_lines

    def _evaluate_room(
        self, zone: dict[str, Any], room: dict[str, Any]
    ) -> RoomDemandStatus:
        """Resolve one room's effective demand and an explainable reason."""
        room_id = str(room.get(ROOM_ID, "unknown-room"))
        room_name = str(room.get(ROOM_NAME, room_id))
        if not room.get(ROOM_ACTIVE, True):
            _LOGGER.debug("  %s: inactive, skipping", room_name)
            return RoomDemandStatus(room_id, room_name, False, "inactive")

        open_entities = tuple(self._room_open_openings(room))
        if open_entities:
            _LOGGER.debug(
                "  %s: window/door open (%s), suppressing demand",
                room_name,
                ", ".join(open_entities),
            )
            return RoomDemandStatus(
                room_id,
                room_name,
                False,
                "opening_open",
                open_entities=open_entities,
            )

        thermostat = self.room_thermostats.get(room_id)
        if thermostat is not None:
            _LOGGER.debug(
                "  %s: reading thermostat %s",
                room_name,
                getattr(thermostat, "entity_id", "?"),
            )
            if getattr(thermostat, "hvac_mode", None) == "off":
                _LOGGER.debug("  %s: thermostat is OFF, skipping", room_name)
                return RoomDemandStatus(room_id, room_name, False, "thermostat_off")
            set_temp = getattr(thermostat, "target_temperature", None)
        else:
            set_temp = self._room_setpoint(room)
            _LOGGER.debug(
                "  %s: thermostat not yet loaded, using fallback setpoint %s°C",
                room_name,
                set_temp,
            )

        _LOGGER.debug(
            "  %s: reading %d sensor(s): %s",
            room_name,
            len(room.get(ROOM_SENSORS, []) or []),
            room.get(ROOM_SENSORS, []) or "(none configured)",
        )
        room_temp = self._room_temperature(room)
        if set_temp is None or room_temp is None:
            _LOGGER.debug(
                "[%s] %s has no usable TRV/sensor reading, skipping",
                zone.get(ZONE_NAME),
                room_name,
            )
            return RoomDemandStatus(
                room_id,
                room_name,
                False,
                "unavailable",
                setpoint=set_temp,
                temperature=room_temp,
            )

        set_temp = float(set_temp)
        room_temp = float(room_temp)
        diff = round(set_temp - room_temp, 2)
        _LOGGER.debug(
            "  %s: Setpoint - Temperature = %s - %s = %s",
            room_name,
            set_temp,
            room_temp,
            diff,
        )
        if diff > 0:
            _LOGGER.debug("  %s: Δ %s°C > 0 -> DEMANDING", room_name, diff)
            return RoomDemandStatus(
                room_id,
                room_name,
                True,
                "demanding",
                setpoint=set_temp,
                temperature=room_temp,
            )
        _LOGGER.debug("  %s: Δ %s°C <= 0 -> satisfied", room_name, diff)
        return RoomDemandStatus(
            room_id,
            room_name,
            False,
            "satisfied",
            setpoint=set_temp,
            temperature=room_temp,
        )

    def room_demand_status(self, room_id: str) -> RoomDemandStatus | None:
        """Return the latest immutable room-demand snapshot for entities."""
        return self._room_demand_statuses.get(room_id)

    def _room_open_openings(self, room: dict[str, Any]) -> list[str]:
        """Return configured window/door sensors currently reporting open."""
        return [
            entity_id
            for entity_id in room.get(ROOM_OPENING_SENSORS, []) or []
            if (state := self.hass.states.get(entity_id)) is not None
            and state.state == "on"
        ]

    def _room_setpoint(self, room: dict[str, Any]) -> float | None:
        """Return the highest usable physical-TRV target as a startup fallback."""
        values: list[float] = []
        for trv in room.get(ROOM_TRVS, []) or []:
            state = self._get_usable_state(trv)
            if state is None:
                continue
            raw = state.attributes.get("temperature")
            if raw in UNAVAILABLE_STATES:
                continue
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                _LOGGER.warning("Could not read setpoint from %s: %r", trv, raw)
        return max(values) if values else None

    def _zone_all_trvs_off(self, zone: dict[str, Any]) -> bool:
        """True if every TRV across every active room in this zone is
        confirmed off (climate entity state == "off") - i.e. there is no
        possible flow path anywhere in the zone. Used to force the zone
        switch off immediately regardless of what the temperature-based
        demand calculation says: running a pump against every valve
        closed risks dead-heading it (pushing water around a fully
        closed loop).

        Deliberately conservative: any TRV that's unavailable, or whose
        state isn't literally "off", means we can't be sure the loop is
        actually closed - the override only fires when every TRV is
        *confirmed* off, never on an uncertain reading. A zone with no
        TRVs configured at all is not eligible for this override (nothing
        to confirm), so normal demand logic applies unmodified.
        """
        found_any = False
        for room in zone.get(ZONE_ROOMS, []):
            if not room.get(ROOM_ACTIVE, True):
                continue
            for trv in room.get(ROOM_TRVS, []) or []:
                state = self._get_usable_state(trv)
                if state is None:
                    return False
                found_any = True
                if state.state != "off":
                    return False
        return found_any

    def _room_temperature(self, room: dict[str, Any]) -> float | None:
        """Average reading among the room's active temperature sensors, or None."""
        values: list[float] = []
        for sensor in room.get(ROOM_SENSORS, []) or []:
            state = self._get_usable_state(sensor)
            if state is None:
                _LOGGER.debug("    sensor %s = unavailable/missing, skipped", sensor)
                continue
            try:
                value = float(state.state)
                values.append(value)
                _LOGGER.debug("    sensor %s = %s", sensor, value)
            except (TypeError, ValueError):
                _LOGGER.warning("Could not read temperature from %s: %r", sensor, state.state)
        if not values:
            return None
        result = round(sum(values) / len(values), 2)
        if len(values) > 1:
            _LOGGER.debug(
                "    -> averaged %d sensor(s): %s", len(values), result
            )
        return result

    def _get_usable_state(self, entity_id: str):
        """Return a present, available and recently reported HA state."""
        state, _reason = self._entity_health(entity_id)
        return state

    def _entity_health(self, entity_id: str):
        """Return the usable state and an exact reason when it is unusable."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None, "is missing from Home Assistant"
        if state.state in UNAVAILABLE_STATES:
            return None, f"is reported as {state.state} by Home Assistant"
        last_reported = getattr(state, "last_reported", state.last_updated)
        age_seconds = (dt_util.utcnow() - last_reported).total_seconds()
        if age_seconds > STALE_THRESHOLD_SECONDS:
            age_minutes = max(1, round(age_seconds / 60))
            return (
                None,
                f"has not reported a state to Home Assistant for about {age_minutes} minutes",
            )
        return state, None

    def _configured_active_entities(self) -> list[tuple[str, dict[str, Any], str]]:
        """Return (entity_id, room, kind) for monitored room equipment."""
        entities: list[tuple[str, dict[str, Any], str]] = []
        for zone in self.zones:
            for room in zone.get(ZONE_ROOMS, []):
                if not room.get(ROOM_ACTIVE, True):
                    continue
                entities.extend((entity_id, room, "TRV") for entity_id in room.get(ROOM_TRVS, []) or [])
                entities.extend((entity_id, room, "sensor") for entity_id in room.get(ROOM_SENSORS, []) or [])
        return entities

    async def _async_check_entity_health(self) -> None:
        """Debounce equipment failures, notify once, and dismiss on recovery."""
        now = dt_util.utcnow()
        for entity_id, room, kind in self._configured_active_entities():
            notification_id = f"{DOMAIN}_offline_{entity_id.replace('.', '_')}"
            _state, unhealthy_reason = self._entity_health(entity_id)
            if unhealthy_reason is None:
                self._entity_unhealthy_since.pop(entity_id, None)
                if entity_id in self._entity_offline_notified:
                    await self.hass.services.async_call(
                        "persistent_notification",
                        "dismiss",
                        {"notification_id": notification_id},
                        blocking=True,
                    )
                    self._entity_offline_notified.discard(entity_id)
                continue

            first_seen = self._entity_unhealthy_since.setdefault(entity_id, now)
            if (
                entity_id in self._entity_offline_notified
                or (now - first_seen).total_seconds() <= OFFLINE_DEBOUNCE_SECONDS
            ):
                continue

            if kind == "TRV":
                usable_peers = [
                    peer
                    for peer in room.get(ROOM_TRVS, []) or []
                    if peer != entity_id and self._get_usable_state(peer) is not None
                ]
                coverage = (
                    "Other usable TRV coverage is still active in this room."
                    if usable_peers
                    else "No usable TRV coverage remains in this room."
                )
            else:
                usable_peers = [
                    peer
                    for peer in room.get(ROOM_SENSORS, []) or []
                    if peer != entity_id and self._get_usable_state(peer) is not None
                ]
                coverage = (
                    "Other usable temperature-sensor coverage is still active in this room."
                    if usable_peers
                    else "No usable temperature-sensor coverage remains in this room."
                )

            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "notification_id": notification_id,
                    "title": "ZEAL entity health warning",
                    "message": (
                        f"{entity_id} in {room.get(ROOM_NAME, room.get(ROOM_ID, 'an unknown room'))} "
                        f"{unhealthy_reason}. {coverage}"
                    ),
                },
                blocking=True,
            )
            self._entity_offline_notified.add(entity_id)

    def _find_room(self, room_id: str) -> dict[str, Any] | None:
        for zone in self.zones:
            for room in zone.get(ZONE_ROOMS, []):
                if room.get(ROOM_ID) == room_id:
                    return room
        return None

    def _find_room_for_trv(self, entity_id: str) -> dict[str, Any] | None:
        for zone in self.zones:
            for room in zone.get(ZONE_ROOMS, []):
                if entity_id in (room.get(ROOM_TRVS) or []):
                    return room
        return None

    def room_current_temperature(self, room_id: str) -> float | None:
        """Public wrapper for ZealRoomThermostat.current_temperature."""
        room = self._find_room(room_id)
        if room is None:
            return None
        return self._room_temperature(room)

    def zone_control_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return live, non-secret zone-control timing for the panel."""
        snapshot: dict[str, dict[str, Any]] = {}
        data = self.data or {}
        for zone in self.zones:
            zone_id = zone[ZONE_ID]
            status = data.get(zone_id)
            override = self.override_switches.get(zone_id)
            last_off = self._last_off_time.get(zone_id)
            reenable_delay = int(
                zone.get(ZONE_REENABLE_DELAY, DEFAULT_REENABLE_DELAY)
            )
            snapshot[zone_id] = {
                "needs_heat": status.needs_heat if status is not None else None,
                "switches_ok": status.switches_ok if status is not None else None,
                "manual_override": bool(
                    override is not None and getattr(override, "is_on", False)
                ),
                "last_off_at": last_off.isoformat() if last_off else None,
                "reenable_delay": reenable_delay,
                "blocked_until": (
                    (last_off + timedelta(seconds=reenable_delay)).isoformat()
                    if last_off
                    else None
                ),
                "demand_lines": list(status.demand_lines) if status else [],
            }
        return snapshot

    def own_thermostat_entity_ids(self) -> set[str]:
        """entity_id of every currently-loaded ZealRoomThermostat.

        Used as a hard guard against ever writing a setpoint to one of our
        own entities as if it were a physical TRV - see the incident this
        guards against in the Decisions Log. Belt-and-braces alongside the
        config_flow.py fix that stops such an entity being *selectable* in
        the first place: this catches it even for a config saved before
        that fix existed, without requiring the user to notice and fix
        their saved config first.
        """
        return {
            t.entity_id
            for t in self.room_thermostats.values()
            if getattr(t, "entity_id", None)
        }

    async def async_propagate_room_setpoint(self, room_id: str, temp: float) -> None:
        """Push a new setpoint to every TRV configured for this room.

        Called both when a user adjusts the room's ZealRoomThermostat
        directly, and when an unexpected change on any one physical TRV in
        the room is detected (see _async_handle_external_trv_change) - in
        both cases every TRV in the room should end up showing the same
        setpoint, since the thermostat is the room's single source of
        truth, not any individual TRV.
        """
        temp = min(MAX_TARGET_TEMPERATURE, max(MIN_TARGET_TEMPERATURE, float(temp)))
        if self._find_room(room_id) is None:
            _LOGGER.debug("Can't propagate setpoint - unknown room_id %s", room_id)
            return

        self._queued_room_setpoints[room_id] = temp
        if room_id in self._rooms_propagating_setpoints:
            _LOGGER.debug(
                "Room %s propagation busy; queued latest target %s°C", room_id, temp
            )
            return

        self._rooms_propagating_setpoints.add(room_id)
        try:
            while room_id in self._queued_room_setpoints:
                latest_temp = self._queued_room_setpoints.pop(room_id)
                await self._async_propagate_room_setpoint_once(room_id, latest_temp)
        finally:
            self._rooms_propagating_setpoints.discard(room_id)

    async def _async_propagate_room_setpoint_once(
        self, room_id: str, temp: float
    ) -> None:
        """Run one serialized pass, abandoning it if a newer target arrives."""
        room = self._find_room(room_id)
        if room is None:
            _LOGGER.debug("Can't propagate setpoint - unknown room_id %s", room_id)
            return
        room_name = room.get(ROOM_NAME, room_id)
        own_entities = self.own_thermostat_entity_ids()
        trvs = [t for t in (room.get(ROOM_TRVS, []) or []) if t not in own_entities]
        skipped = (room.get(ROOM_TRVS, []) or [])
        skipped = [t for t in skipped if t in own_entities]
        if skipped:
            _LOGGER.error(
                "[%s] Room's TRV list includes ZEAL's own entity/entities %s - "
                "refusing to propagate to them (this would recurse infinitely). "
                "Open ZEAL Setup for this room and remove them from the TRV "
                "list; they should no longer be offered as an option.",
                room_name,
                skipped,
            )
        _LOGGER.debug("[%s] Propagating %s°C to %d TRV(s)", room_name, temp, len(trvs))
        for trv in trvs:
            if room_id in self._queued_room_setpoints:
                _LOGGER.debug(
                    "[%s] Abandoning superseded %s°C propagation pass",
                    room_name,
                    temp,
                )
                return
            await self._async_write_trv_setpoint(trv, temp, room_name)

    async def _async_write_trv_setpoint(
        self, entity_id: str, temp: float, room_name: str
    ) -> bool:
        """Write one physical climate entity and wait for state confirmation."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            self._unconfirmed_setpoint_writes.pop(entity_id, None)
            self._unsynced_trvs.add(entity_id)
            _LOGGER.warning(
                "[%s] Can't propagate setpoint to unavailable TRV %s; "
                "ZEAL will retry when it recovers",
                room_name,
                entity_id,
            )
            return False

        current_temp = state.attributes.get("temperature")
        try:
            already_at_target = abs(float(current_temp) - temp) < 0.01
        except (TypeError, ValueError):
            already_at_target = False
        if already_at_target:
            self._unconfirmed_setpoint_writes.pop(entity_id, None)
            self._unsynced_trvs.discard(entity_id)
            _LOGGER.debug("  -> %s already at %s°C; no service call needed", entity_id, temp)
            return True

        now = dt_util.utcnow()
        pending = PendingSetpointWrite(
            temperature=temp,
            expires_at=now + timedelta(seconds=SETPOINT_ECHO_TIMEOUT_SECONDS),
        )
        pending_writes = [
            item
            for item in self._pending_setpoint_writes.get(entity_id, [])
            if now <= item.expires_at
        ]
        pending_writes.append(pending)
        self._pending_setpoint_writes[entity_id] = pending_writes
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": temp},
                blocking=True,
            )
        except HomeAssistantError as err:
            remaining = [
                item
                for item in self._pending_setpoint_writes.get(entity_id, [])
                if item is not pending
            ]
            if remaining:
                self._pending_setpoint_writes[entity_id] = remaining
            else:
                self._pending_setpoint_writes.pop(entity_id, None)
            self._unconfirmed_setpoint_writes.pop(entity_id, None)
            self._unsynced_trvs.add(entity_id)
            _LOGGER.warning(
                "[%s] Could not set %s to %s°C; ZEAL will retry: %s",
                room_name,
                entity_id,
                temp,
                err,
            )
            return False

        latest_state = self.hass.states.get(entity_id)
        latest_temp = (
            latest_state.attributes.get("temperature") if latest_state else None
        )
        try:
            confirmed = abs(float(latest_temp) - temp) < 0.01
        except (TypeError, ValueError):
            confirmed = False

        if confirmed:
            self._unconfirmed_setpoint_writes.pop(entity_id, None)
            _LOGGER.debug("  -> %s confirmed at %s°C", entity_id, temp)
        else:
            reported_at = getattr(
                latest_state or state,
                "last_reported",
                (latest_state or state).last_updated,
            )
            self._unconfirmed_setpoint_writes[entity_id] = UnconfirmedSetpointWrite(
                temperature=temp,
                observed_reported_at=reported_at,
                last_attempt_at=now,
            )
            _LOGGER.debug(
                "  -> %s accepted %s°C but has not reported it yet; "
                "waiting for the battery device's next report",
                entity_id,
                temp,
            )

        self._unsynced_trvs.discard(entity_id)
        return True

    async def _async_retry_unconfirmed_setpoint_writes(self) -> None:
        """Retry a queued battery-device target only after a fresh report."""
        now = dt_util.utcnow()
        for entity_id, pending in tuple(self._unconfirmed_setpoint_writes.items()):
            if self._unconfirmed_setpoint_writes.get(entity_id) is not pending:
                continue
            room = self._find_room_for_trv(entity_id)
            if room is None:
                self._unconfirmed_setpoint_writes.pop(entity_id, None)
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in UNAVAILABLE_STATES:
                self._unconfirmed_setpoint_writes.pop(entity_id, None)
                self._unsynced_trvs.add(entity_id)
                continue
            current_temp = state.attributes.get("temperature")
            try:
                confirmed = abs(float(current_temp) - pending.temperature) < 0.01
            except (TypeError, ValueError):
                confirmed = False
            if confirmed:
                self._unconfirmed_setpoint_writes.pop(entity_id, None)
                continue

            reported_at = getattr(state, "last_reported", state.last_updated)
            if reported_at <= pending.observed_reported_at:
                continue
            if (
                now - pending.last_attempt_at
            ).total_seconds() < SETPOINT_CONFIRMATION_RETRY_MIN_SECONDS:
                continue

            room_name = room.get(ROOM_NAME, room.get(ROOM_ID))
            _LOGGER.debug(
                "[%s] %s reported again without confirming %s°C; retrying once",
                room_name,
                entity_id,
                pending.temperature,
            )
            await self._async_write_trv_setpoint(
                entity_id, pending.temperature, room_name
            )

    async def _async_resync_recovered_trv(
        self, room: dict[str, Any], entity_id: str
    ) -> None:
        """Restore a recovered TRV from ZEAL's canonical room target."""
        room_id = room.get(ROOM_ID)
        room_name = room.get(ROOM_NAME, room_id)
        thermostat = self.room_thermostats.get(room_id)
        target = getattr(thermostat, "target_temperature", None)
        if target is None:
            _LOGGER.debug(
                "[%s] Delaying recovery sync for %s until its ZEAL thermostat is ready",
                room_name,
                entity_id,
            )
            self._unsynced_trvs.add(entity_id)
            return
        safe_temp = min(
            MAX_TARGET_TEMPERATURE,
            max(MIN_TARGET_TEMPERATURE, float(target)),
        )
        await self.async_propagate_room_setpoint(room_id, safe_temp)

    async def _async_retry_unsynced_trvs(self) -> None:
        """Retry missed writes without allowing an old TRV target to win."""
        for entity_id in tuple(self._unsynced_trvs):
            if entity_id not in self._unsynced_trvs:
                continue
            room = self._find_room_for_trv(entity_id)
            if room is None:
                self._unsynced_trvs.discard(entity_id)
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state in UNAVAILABLE_STATES:
                continue
            await self._async_resync_recovered_trv(room, entity_id)

    async def async_set_room_target(
        self, room_id: str, temp: float, *, source: str = "unknown"
    ) -> bool:
        """Set one canonical ZEAL room target, then propagate it safely.

        Scheduler callers supply only a stable ZEAL room ID. Physical TRV
        selection remains encapsulated in ``async_propagate_room_setpoint`` and
        retains its own-entity recursion guard and safety clamp.
        """
        room = self._find_room(room_id)
        thermostat = self.room_thermostats.get(room_id)
        if room is None or thermostat is None:
            _LOGGER.warning(
                "Scheduler/setpoint source %s could not resolve ZEAL room %s; "
                "the target will be retried after a Coordinator update",
                source,
                room_id,
            )
            return False
        safe_temp = min(
            MAX_TARGET_TEMPERATURE,
            max(MIN_TARGET_TEMPERATURE, float(temp)),
        )
        thermostat.apply_target_setpoint(safe_temp, source=source)
        await self.async_propagate_room_setpoint(room_id, safe_temp)
        return True

    async def _async_handle_external_trv_change(
        self, room: dict[str, Any], entity_id: str, new_temp: Any
    ) -> None:
        """A physical TRV's setpoint changed and it wasn't us who wrote it.

        Update the room's thermostat to match (so it displays the real
        current setpoint) and propagate that value to every other TRV in
        the room, so a manual change on any one TRV becomes the room's new
        setpoint everywhere, not just on the TRV someone happened to touch.
        """
        try:
            new_temp = float(new_temp)
        except (TypeError, ValueError):
            _LOGGER.debug("Ignoring non-numeric TRV temperature: %r", new_temp)
            return

        now = dt_util.utcnow()
        pending_writes = [
            item
            for item in self._pending_setpoint_writes.get(entity_id, [])
            if now <= item.expires_at
        ]
        matching_pending = next(
            (
                item
                for item in pending_writes
                if abs(item.temperature - new_temp) < 0.01
            ),
            None,
        )
        if matching_pending is not None:
            pending_writes.remove(matching_pending)
            if pending_writes:
                self._pending_setpoint_writes[entity_id] = pending_writes
            else:
                self._pending_setpoint_writes.pop(entity_id, None)
            unconfirmed = self._unconfirmed_setpoint_writes.get(entity_id)
            if (
                unconfirmed is not None
                and abs(unconfirmed.temperature - new_temp) < 0.01
            ):
                self._unconfirmed_setpoint_writes.pop(entity_id, None)
            # This is the immediate, expected echo of one ZEAL write. It is
            # consumed now; a later manual turn to the same value is genuine.
            _LOGGER.debug(
                "%s: change to %s°C is ZEAL's expected write echo, ignoring once",
                entity_id,
                new_temp,
            )
            return
        if pending_writes:
            self._pending_setpoint_writes[entity_id] = pending_writes
        else:
            self._pending_setpoint_writes.pop(entity_id, None)

        unconfirmed = self._unconfirmed_setpoint_writes.get(entity_id)
        if (
            unconfirmed is not None
            and abs(unconfirmed.temperature - new_temp) < 0.01
        ):
            self._unconfirmed_setpoint_writes.pop(entity_id, None)
            self._unsynced_trvs.discard(entity_id)
            _LOGGER.debug(
                "%s: late confirmation of ZEAL's %s°C write, ignoring once",
                entity_id,
                new_temp,
            )
            return

        # A different setpoint is a genuine physical/manual change. It
        # supersedes any older queued target for this entity.
        self._unconfirmed_setpoint_writes.pop(entity_id, None)

        room_id = room.get(ROOM_ID)
        room_name = room.get(ROOM_NAME, room_id)
        _LOGGER.debug(
            "%s: genuine manual change to %s°C, updating room %s and propagating",
            entity_id,
            new_temp,
            room_name,
        )
        thermostat = self.room_thermostats.get(room_id)
        if thermostat is not None:
            thermostat.apply_external_setpoint(new_temp)

        generation = self._external_setpoint_generation.get(room_id, 0) + 1
        self._external_setpoint_generation[room_id] = generation
        if EXTERNAL_SETPOINT_SETTLE_SECONDS > 0:
            await asyncio.sleep(EXTERNAL_SETPOINT_SETTLE_SECONDS)
        if self._external_setpoint_generation.get(room_id) != generation:
            _LOGGER.debug(
                "%s: superseded while the physical dial was still moving; "
                "discarding intermediate %s°C propagation",
                entity_id,
                new_temp,
            )
            return
        self._external_setpoint_generation.pop(room_id, None)
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {})
        runtime = entry_data.get("schedule_runtime")
        handled = bool(
            runtime is not None
            and await runtime.async_handle_guest_target(
                room_id,
                new_temp,
                source="guest_physical_trv_adjustment",
            )
        )
        if not handled:
            await self.async_propagate_room_setpoint(room_id, new_temp)
        learning = entry_data.get("schedule_learning")
        if learning is not None:
            await learning.async_record_change(
                room_id=room_id,
                requested_temperature=new_temp,
                source="physical_trv",
                when=dt_util.now(),
            )

    # ------------------------------------------------------------------
    # Switch control
    # ------------------------------------------------------------------
    async def _async_apply_zone_switches(
        self, zone: dict[str, Any], needs_heat: bool
    ) -> tuple[bool, bool]:
        """Drive this zone's single heating actuator switch toward needs_heat.

        A zone has exactly one switch (pump/relay) - never more. Confirmed
        against real installs: a shared single-pump house split into
        ground-floor/first-floor zones, a dual-pump house with one switch
        per zone, a hotel with one switch per level.

        Returns (available, off_time_changed):
          * available is False if the configured switch is unavailable
            (nothing could be actuated), True otherwise (including the
            "nothing needed to change" case).
          * off_time_changed is True only if this call newly recorded an
            OFF transition for this zone - i.e. an actual state change
            happened just now, not merely that the zone has ever turned
            off at some point in the past. Callers use this to decide
            whether a Store write is actually warranted this cycle.
        """
        zone_id = zone[ZONE_ID]
        zone_name = zone.get(ZONE_NAME, zone_id)
        now = dt_util.utcnow()
        off_time_changed = False

        override = self.override_switches.get(zone_id)
        if override is not None and getattr(override, "is_on", False):
            _LOGGER.debug("[%s] Manual override active — skipping automatic control", zone_name)
            return True, False

        entity_id = zone.get(ZONE_SWITCH)
        state = self.hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            _LOGGER.warning(
                "[%s] Switch %s is unavailable, skipping this cycle. If this "
                "is right after a restart, it's usually transient (e.g. a "
                "template-platform switch that hasn't finished initialising "
                "yet) and self-corrects on the very next state change or "
                "poll. If it persists beyond startup, check the entity "
                "still exists and is selected correctly in ZEAL Setup.",
                zone_name,
                entity_id,
            )
            return False, False

        blocked_by_delay = False
        if needs_heat:
            last_off = self._last_off_time.get(zone_id)
            if last_off is not None:
                reenable_delay = zone.get(ZONE_REENABLE_DELAY, DEFAULT_REENABLE_DELAY)
                elapsed = (now - last_off).total_seconds()
                if elapsed < reenable_delay:
                    blocked_by_delay = True
                    remaining = int(reenable_delay - elapsed)
                    _LOGGER.debug(
                        "[%s] Demand present but waiting %ss before re-enabling",
                        zone_name,
                        remaining,
                    )

        if needs_heat:
            if not blocked_by_delay and state.state != "on":
                _LOGGER.debug("[%s] Turning ON %s", zone_name, entity_id)
                await self.hass.services.async_call(
                    "switch", "turn_on", {"entity_id": entity_id}, blocking=True
                )
                self._last_on_time[zone_id] = now
            elif state.state == "on":
                _LOGGER.debug("[%s] %s already ON, nothing to do", zone_name, entity_id)
        else:
            if state.state != "off":
                _LOGGER.debug("[%s] Turning OFF %s", zone_name, entity_id)
                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": entity_id}, blocking=True
                )
                last_on = self._last_on_time.get(zone_id)
                if last_on is not None:
                    duration_mins = (now - last_on).total_seconds() / 60
                    _LOGGER.debug(
                        "[%s] %s ran for %.1f minutes", zone_name, entity_id, duration_mins
                    )
                self._last_off_time[zone_id] = now
                off_time_changed = True
            else:
                _LOGGER.debug("[%s] %s already OFF, nothing to do", zone_name, entity_id)

        return True, off_time_changed

    async def _async_persist_runtime_state(self) -> None:
        await self.store.async_save(
            {
                CONF_ZONES: self.zones,
                RUNTIME_LAST_OFF: {
                    zone_id: dt.isoformat() for zone_id, dt in self._last_off_time.items()
                },
            }
        )
