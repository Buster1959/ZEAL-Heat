"""Per-room effective heating-demand entities for ZEAL."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    ROOM_ID,
    ROOM_NAME,
    ROOM_SENSORS,
    ROOM_TRVS,
    ZONE_ID,
    ZONE_NAME,
    ZONE_ROOMS,
    zone_is_actionable,
)
from .coordinator import RoomDemandStatus, ZealCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one demand entity for every actionable configured room."""
    coordinator: ZealCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = [
        ZealRoomDemandBinarySensor(coordinator, entry, zone, room)
        for zone in coordinator.zones
        if zone_is_actionable(zone)
        for room in zone.get(ZONE_ROOMS, [])
        if room.get(ROOM_TRVS) and room.get(ROOM_SENSORS)
    ]
    async_add_entities(entities)


class ZealRoomDemandBinarySensor(
    CoordinatorEntity[ZealCoordinator], BinarySensorEntity
):
    """Whether one room currently contributes to its zone's heat demand."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:radiator"
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ZealCoordinator,
        entry: ConfigEntry,
        zone: dict[str, Any],
        room: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._room_id = room[ROOM_ID]
        room_name = room.get(ROOM_NAME, self._room_id)
        zone_id = zone[ZONE_ID]
        self._attr_unique_id = f"{entry.entry_id}_{self._room_id}_heat_demand"
        self._attr_name = f"{room_name} Heat Demand"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{zone_id}")},
            name=zone.get(ZONE_NAME, zone_id),
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
        )

    @property
    def _status(self) -> RoomDemandStatus | None:
        return self.coordinator.room_demand_status(self._room_id)

    @property
    def is_on(self) -> bool | None:
        status = self._status
        return status.needs_heat if status is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._status
        if status is None:
            return {"reason": "not_evaluated"}
        return {
            "reason": status.reason,
            "setpoint": status.setpoint,
            "temperature": status.temperature,
            "open_entities": list(status.open_entities),
        }
