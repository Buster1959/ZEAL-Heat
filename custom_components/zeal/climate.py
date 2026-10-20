"""The ZEAL room thermostat - each active room's single setpoint authority.

One ZealRoomThermostat entity is created per room that has at least one
TRV configured. It is the room's actual source of truth for its target
temperature - not any individual physical TRV. The Coordinator:

  * reads this entity's target_temperature as the room's setpoint for
    demand evaluation (see coordinator.py's _evaluate_zone), instead of
    inferring one from the room's TRVs' own raw setpoints.
  * propagates this entity's target_temperature out to every TRV
    configured for the room whenever it changes - via
    Coordinator.async_propagate_room_setpoint().
  * detects an unexpected change on any *physical* TRV in the room (a
    human adjusted it directly, not us) and both updates this entity to
    match and re-propagates to the room's other TRVs, so a manual change
    on any one TRV becomes the room's setpoint everywhere, not just on
    the TRV someone happened to touch.

This replaces an earlier "highest setpoint among the room's TRVs" default
and a separate planned-but-never-built 2-hour "boost" mechanic (see the
Decisions Log) - both were awkward substitutes for just having a real
per-room entity be the setpoint authority.

Heating only for now: hvac_modes is fixed to HEAT/OFF. Cool mode is a v2
feature contingent on a physical cooling retrofit that doesn't exist yet -
see the wiki's "V2 Future Enhancements" page for the (unbuilt, still being
designed) cooling sequence this entity will need to support later, where
it becomes the thing that puts physical TRVs into a fully-open/manual
"just a valve" state rather than driving their own setpoint.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN, ROOM_ID, ROOM_NAME, ROOM_TRVS, ZONE_ID, ZONE_NAME, ZONE_ROOMS
from .coordinator import ZealCoordinator

_LOGGER = logging.getLogger(__name__)

# Sensible generic bounds - not read from any per-install config yet.
DEFAULT_TARGET_TEMP = 20.0
MIN_TEMP = 5.0
MAX_TEMP = 30.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ZealCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[ZealRoomThermostat] = []
    for zone in coordinator.zones:
        for room in zone.get(ZONE_ROOMS, []):
            if not room.get(ROOM_TRVS):
                # Nothing to actuate for this room - no thermostat needed,
                # same "skip if nothing configured" pattern as switch.py's
                # override switch and sensor.py's demand sensor.
                continue
            entities.append(ZealRoomThermostat(coordinator, entry, zone, room))
    async_add_entities(entities)


class ZealRoomThermostat(CoordinatorEntity[ZealCoordinator], RestoreEntity, ClimateEntity):
    """The master setpoint for one room. Physical TRVs are slaved to this."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = 0.5

    def __init__(
        self,
        coordinator: ZealCoordinator,
        entry: ConfigEntry,
        zone: dict[str, Any],
        room: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._zone_id = zone[ZONE_ID]
        self._room_id = room[ROOM_ID]
        room_name = room.get(ROOM_NAME, self._room_id)

        self._attr_unique_id = f"{entry.entry_id}_{self._room_id}_thermostat"
        # The device this entity belongs to is named after the ZONE (see
        # DeviceInfo below, shared with ZealOverrideSwitch/ZealDemandSensor)
        # - with _attr_has_entity_name True, HA displays "<device name>
        # <this name>". If _attr_name were a fixed class-level string like
        # "Thermostat", every room's thermostat in the same zone would
        # display identically (e.g. two rooms both "Ground Floor
        # Thermostat") with no way to tell them apart. Setting it here,
        # per-instance, to include the room name fixes that:
        # "Ground Floor Living Room Thermostat".
        #
        # The "[ZEAL]" suffix is purely a human-readability aid, added
        # after a real mix-up during testing: a dummy/real TRV can very
        # plausibly share this entity's exact name (e.g. a Generic
        # Thermostat helper or a real TRV integration both commonly default
        # to "<Room> Thermostat"), and in a plain entity picker the only
        # visible difference is a small device-name subtitle easy to miss.
        # This is NOT the safety mechanism against that mix-up, though -
        # that's own_thermostat_entity_ids() in coordinator.py, which
        # checks the entity registry's actual platform/owner (reliable,
        # can't be spoofed or silently broken by a later rename) rather
        # than string-matching on a display name (fragile - would silently
        # stop working if this suffix were ever edited away in the UI).
        # Note "[ZEAL]" only ever appears in this display name, never in
        # the entity_id itself - entity_ids can't contain brackets, HA
        # strips/converts them during its own auto-slugify.
        self._attr_name = f"{room_name} Thermostat [ZEAL]"

        self._attr_target_temperature = DEFAULT_TARGET_TEMP
        self._attr_hvac_mode = HVACMode.HEAT

        # Same zone-level device grouping as ZealOverrideSwitch/
        # ZealDemandSensor - this room's thermostat shows up alongside
        # them under its zone's device page, not as a separate device.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{self._zone_id}")},
            name=zone.get(ZONE_NAME, self._zone_id),
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            last_temp = last_state.attributes.get(ATTR_TEMPERATURE)
            if last_temp is not None:
                try:
                    self._attr_target_temperature = float(last_temp)
                except (TypeError, ValueError):
                    _LOGGER.warning(
                        "Could not restore target temperature for %s: %r",
                        self.entity_id,
                        last_temp,
                    )
            if last_state.state in (HVACMode.HEAT, HVACMode.OFF):
                self._attr_hvac_mode = HVACMode(last_state.state)

        # Register with the coordinator - same live-object pattern as
        # ZealOverrideSwitch's override_switches registry - so it can read
        # our target_temperature/hvac_mode directly and call
        # apply_external_setpoint() when a physical TRV changes.
        self.coordinator.room_thermostats[self._room_id] = self

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.room_thermostats.pop(self._room_id, None)
        await super().async_will_remove_from_hass()

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.room_current_temperature(self._room_id)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        _LOGGER.debug("%s: user set target temperature to %s°C", self.entity_id, temp)
        runtime = self.hass.data.get(DOMAIN, {}).get(
            self._entry.entry_id, {}
        ).get("schedule_runtime")
        handled = bool(
            runtime is not None
            and await runtime.async_handle_guest_target(
                self._room_id,
                float(temp),
                source="guest_thermostat_adjustment",
            )
        )
        applied = handled or await self.coordinator.async_set_room_target(
            self._room_id, float(temp), source="manual_thermostat_change"
        )
        if applied:
            learning = self.hass.data.get(DOMAIN, {}).get(
                self._entry.entry_id, {}
            ).get("schedule_learning")
            if learning is not None:
                await learning.async_record_change(
                    room_id=self._room_id,
                    requested_temperature=float(temp),
                    source="home_assistant",
                    when=dt_util.now(),
                )
            await self.coordinator.async_request_refresh()

    def apply_target_setpoint(self, temp: float, *, source: str) -> None:
        """Update the canonical room target before guarded TRV propagation."""
        _LOGGER.debug(
            "%s: applying %s°C from %s", self.entity_id, temp, source
        )
        self._attr_target_temperature = temp
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        _LOGGER.debug("%s: user set hvac_mode to %s", self.entity_id, hvac_mode)
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()
        # A refresh picks up the mode change immediately (OFF stops this
        # room contributing demand - see coordinator.py's _evaluate_zone)
        # rather than waiting for the next scheduled poll.
        await self.coordinator.async_request_refresh()

    def apply_external_setpoint(self, temp: float) -> None:
        """Called by the Coordinator when a physical TRV in this room
        changed unexpectedly, to keep this entity's displayed setpoint in
        sync with what the room's TRVs are now actually set to."""
        _LOGGER.debug(
            "%s: syncing to %s°C following an external TRV change", self.entity_id, temp
        )
        self._attr_target_temperature = temp
        self.async_write_ha_state()
