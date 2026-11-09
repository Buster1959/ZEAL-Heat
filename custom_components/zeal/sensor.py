"""Per-zone diagnostic demand sensor for ZEAL HVAC System.

Mirrors the old `input_text.<floor>_demanding_rooms` helper that
ashp_controller.py updated on every evaluation - a quick way to see, from
the entity list alone, whether a zone currently wants heat and why, without
digging through logs.
"""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    ZONE_ID,
    ZONE_NAME,
    zone_is_actionable,
)
from .coordinator import ZealCoordinator, ZoneStatus


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one demand-summary sensor per configured zone."""
    coordinator: ZealCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = [
        ZealDemandSensor(coordinator, entry, zone)
        for zone in coordinator.zones
        if zone_is_actionable(zone)
    ]
    async_add_entities(entities)


class ZealDemandSensor(CoordinatorEntity[ZealCoordinator], SensorEntity):
    """Read-only "does this zone currently want heat, and why" sensor."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:radiator"
    _attr_should_poll = False

    def __init__(
        self, coordinator: ZealCoordinator, entry: ConfigEntry, zone: dict
    ) -> None:
        super().__init__(coordinator)
        self._zone_id = zone[ZONE_ID]
        zone_name = zone.get(ZONE_NAME, self._zone_id)

        self._attr_unique_id = f"{entry.entry_id}_{self._zone_id}_demand"
        self._attr_name = "Demand"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{self._zone_id}")},
            name=zone_name,
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
        )

    @property
    def _status(self) -> ZoneStatus | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._zone_id)

    @property
    def native_value(self) -> str:
        status = self._status
        if status is None:
            return "Unknown"
        if not status.switches_ok:
            return "Switch unavailable"
        return "Demand" if status.needs_heat else "No demand"

    @property
    def extra_state_attributes(self) -> dict[str, list[str]]:
        status = self._status
        return {"demanding_rooms": status.demand_lines if status else []}
