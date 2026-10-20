"""The ZEAL HVAC System integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    AUDIT_STORAGE_KEY_FMT,
    AUDIT_STORAGE_VERSION,
    CONF_ZONES,
    CONF_LEARNING_ENABLED,
    CONF_LEARNING_PERSISTENT_NOTIFICATIONS,
    LEARNING_STORAGE_KEY_FMT,
    LEARNING_STORAGE_VERSION,
    OCCUPANCY_STORAGE_KEY_FMT,
    OCCUPANCY_STORAGE_VERSION,
    DOMAIN,
    ROOM_ID,
    ROOM_NAME,
    ROOM_TRVS,
    SCHEDULE_STORAGE_KEY_FMT,
    SCHEDULE_STORAGE_VERSION,
    STORAGE_KEY_FMT,
    STORAGE_VERSION,
    ZONE_ID,
    ZONE_ROOMS,
)
from .coordinator import ZealCoordinator
from .panel import async_remove_panel, async_sync_panel
from .scheduler.audit import AuditLog
from .scheduler.configuration import current_revision
from .scheduler.learning import (
    LearningStore,
    ScheduleLearning,
    sync_persistent_notification,
)
from .scheduler.rooms import reconcile_room_schedules
from .scheduler.occupancy import OccupancyStorage
from .scheduler.runtime import ScheduleRuntime
from .scheduler.storage import ScheduleStorage
from .scheduler.websocket_api import async_register_commands
from .thermal_storage import ThermalStorage

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["switch", "sensor", "binary_sensor", "climate"]


async def _async_cleanup_orphaned_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove devices/entities for zones or rooms no longer in config.

    Home Assistant does not automatically remove entities or devices when
    a config entry's saved options shrink - every async_add_entities call
    only ever *adds*. Without this, renaming or removing a zone/room in
    Removing a zone in the ZEAL Setup panel can otherwise leave its old
    device/entities behind permanently, silently accumulating "ghost" devices
    - found via a real incident where two old
    zone devices ("Ground Floor", "First Floor") persisted for good after
    the zones were rebuilt as "Zone 1"/"Zone 2".

    Two separate checks, since they catch different cases:
      * A whole ZONE removed -> its device (identified by zone_id) no
        longer matches any current zone -> remove the device, which
        cascades to remove every entity registered under it (the zone's
        override switch, demand sensor, and every room's thermostat, since
        all of a zone's room thermostats share that zone's device).
      * A ROOM removed from a zone that still exists -> the zone's device
        is still valid, so the check above won't catch it. Its
        ZealRoomThermostat's own unique_id embeds the room_id directly
        (f"{entry.entry_id}_{room_id}_thermostat"), checked independently.
    """
    zones = entry.options.get(CONF_ZONES, [])
    valid_zone_ids = {z[ZONE_ID] for z in zones}
    valid_room_ids = {r[ROOM_ID] for z in zones for r in z.get(ZONE_ROOMS, [])}

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"

    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain != DOMAIN or not identifier.startswith(prefix):
                continue
            zone_id = identifier[len(prefix):]
            if zone_id not in valid_zone_ids:
                _LOGGER.info(
                    "Removing orphaned device for a zone no longer in config: %s (%s)",
                    device.name,
                    device.id,
                )
                device_registry.async_remove_device(device.id)

    room_entity_suffixes = ("_thermostat", "_heat_demand")
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if not entity.unique_id:
            continue
        if not entity.unique_id.startswith(prefix):
            continue
        suffix = next(
            (
                candidate
                for candidate in room_entity_suffixes
                if entity.unique_id.endswith(candidate)
            ),
            None,
        )
        if suffix is None:
            continue
        room_id = entity.unique_id[len(prefix) : -len(suffix)]
        if room_id not in valid_room_ids:
            _LOGGER.info(
                "Removing orphaned room entity for a room no longer in config: %s",
                entity.entity_id,
            )
            entity_registry.async_remove(entity.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ZEAL HVAC System from a config entry."""
    store: Store = Store(
        hass, STORAGE_VERSION, STORAGE_KEY_FMT.format(entry_id=entry.entry_id)
    )
    # Mirror the current panel configuration into the Store. This re-runs on
    # every reload (see _async_update_listener), so the Store is always in
    # sync with the latest saved zones/rooms/TRVs/sensors.
    await store.async_save({CONF_ZONES: entry.options.get(CONF_ZONES, [])})

    coordinator = ZealCoordinator(hass, entry, store)
    await coordinator.async_setup()

    schedule_storage = ScheduleStorage(hass, entry.entry_id)
    stored_schedule = await schedule_storage.async_load()
    configured_rooms = {
        room[ROOM_ID]: room.get(ROOM_NAME, room[ROOM_ID])
        for zone in coordinator.zones
        for room in zone.get(ZONE_ROOMS, [])
        if room.get(ROOM_TRVS)
    }
    schedule_configuration = reconcile_room_schedules(
        stored_schedule, configured_rooms
    )
    if schedule_configuration.temperature_unit is None:
        schedule_configuration = schedule_configuration.with_temperature_unit("°C")
    elif schedule_configuration.temperature_unit != "°C":
        from homeassistant.exceptions import ConfigEntryError

        raise ConfigEntryError(
            "ZEAL room thermostats use Celsius but the stored schedule uses "
            f"{schedule_configuration.temperature_unit}. Remove and re-add ZEAL "
            "before creating schedules in a different unit."
        )
    if schedule_configuration != stored_schedule:
        await schedule_storage.async_save(schedule_configuration)
    audit_log = AuditLog(hass, entry.entry_id)
    await audit_log.async_load()
    occupancy_storage = OccupancyStorage(hass, entry.entry_id)
    schedule_runtime = ScheduleRuntime(
        hass, coordinator, audit_log, occupancy_storage
    )
    learning_store = LearningStore(hass, entry.entry_id)
    await learning_store.async_load()
    schedule_learning = ScheduleLearning(
        learning_store,
        lambda: schedule_runtime.configuration,
        lambda: current_revision(hass, entry.entry_id),
        lambda: entry.options.get(CONF_LEARNING_ENABLED, False),
        lambda _room_id: (
            "away_mode_active"
            if schedule_runtime.away_mode_state().get("active")
            else None
        ),
        lambda state: sync_persistent_notification(hass, entry.entry_id, state)
        if entry.options.get(CONF_LEARNING_PERSISTENT_NOTIFICATIONS, True)
        else None,
    )
    schedule_runtime.learning = schedule_learning
    thermal_storage = ThermalStorage(hass, entry.entry_id)
    await thermal_storage.async_load()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "store": store,
        "coordinator": coordinator,
        "schedule_storage": schedule_storage,
        "schedule_runtime": schedule_runtime,
        "occupancy_storage": occupancy_storage,
        "audit_log": audit_log,
        "learning_store": learning_store,
        "schedule_learning": schedule_learning,
        "thermal_storage": thermal_storage,
    }
    async_register_commands(hass)

    # Re-run setup whenever the HTML panel saves changes, so anything
    # reading hass.data picks up the new zones/rooms immediately.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Runs after platforms load (not before) so the currently-valid
    # entities have already been (re-)registered and won't be mistaken
    # for orphans - only genuinely stale devices/entities from a
    # zone/room that no longer exists in config get removed.
    await _async_cleanup_orphaned_entities(hass, entry)

    # First evaluation + switch pass happens after platforms are set up, so
    # the override switches (switch.py) have already registered themselves
    # with the coordinator and are respected on this very first run.
    await coordinator.async_config_entry_first_refresh()
    await schedule_runtime.async_start(schedule_configuration)

    # Logged here, not inside coordinator.async_setup(), specifically
    # because it needs to run *after* platforms have loaded - the banner
    # reports each room's registered ZealRoomThermostat, which doesn't
    # exist yet at the point async_setup() runs (climate.py hasn't been
    # forwarded/set up that early). Logging it before that point would
    # make every room show "not yet registered" always, regardless of
    # whether anything was actually wrong - misleading noise, not signal.
    await coordinator.async_log_startup_banner()
    await async_sync_panel(hass)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data is not None:
            await data["thermal_storage"].async_disable(delete_data=False)
            await data["schedule_runtime"].async_stop()
            data["coordinator"].async_teardown()
        if not hass.data[DOMAIN]:
            await async_remove_panel(hass)
        else:
            await async_sync_panel(hass)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove only this ZEAL instance's private persisted data."""
    await ThermalStorage(hass, entry.entry_id).async_remove()
    for version, key_format in (
        (STORAGE_VERSION, STORAGE_KEY_FMT),
        (SCHEDULE_STORAGE_VERSION, SCHEDULE_STORAGE_KEY_FMT),
        (AUDIT_STORAGE_VERSION, AUDIT_STORAGE_KEY_FMT),
        (LEARNING_STORAGE_VERSION, LEARNING_STORAGE_KEY_FMT),
        (OCCUPANCY_STORAGE_VERSION, OCCUPANCY_STORAGE_KEY_FMT),
    ):
        await Store(
            hass,
            version,
            key_format.format(entry_id=entry.entry_id),
        ).async_remove()
