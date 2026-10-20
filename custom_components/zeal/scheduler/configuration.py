"""Validated persistence boundary shared by ZEAL's panel APIs."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_SHOW_IN_SIDEBAR,
    CONF_LEARNING_ENABLED,
    CONF_LEARNING_PERSISTENT_NOTIFICATIONS,
    CONF_STANDARD_USER_QUICK_CHANGE,
    CONF_STANDARD_USER_SCHEDULE,
    CONF_ZONES,
    DEFAULT_REENABLE_DELAY,
    DOMAIN,
    HEAT_SOURCE_ASHP,
    HEAT_SOURCE_DEFAULT_REENABLE_DELAY,
    HEAT_SOURCE_OPTIONS,
    ROOM_ACTIVE,
    ROOM_COOLING_CAPABLE,
    ROOM_ID,
    ROOM_NAME,
    ROOM_OPENING_SENSORS,
    ROOM_OCCUPANCY_CALENDAR,
    ROOM_OCCUPANCY_ENABLED,
    ROOM_OCCUPANCY_SOURCE,
    ROOM_SENSORS,
    ROOM_TRVS,
    ROOM_UNOCCUPIED_TEMPERATURE,
    OCCUPANCY_SOURCE_CALENDAR,
    OCCUPANCY_SOURCE_MANUAL,
    MAX_TARGET_TEMPERATURE,
    MIN_TARGET_TEMPERATURE,
    ASHP_CAPABILITY_HEAT_AND_COOL,
    ASHP_CAPABILITY_HEAT_ONLY,
    ZONE_ASHP_CAPABILITY,
    ZONE_HEAT_SOURCE,
    ZONE_ID,
    ZONE_NAME,
    ZONE_REENABLE_DELAY,
    ZONE_ROOMS,
    ZONE_SWITCH,
)
from .away import AwayModeConfiguration, with_away_mode
from .models import ScheduleConfiguration
from .rooms import reconcile_room_schedules


class ConfigurationConflictError(ValueError):
    """Raised when a browser tries to save an obsolete document."""


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def configuration_revision(
    zones: list[dict[str, Any]],
    schedule: ScheduleConfiguration,
    show_in_sidebar: bool = True,
    standard_user_schedule: bool = False,
    standard_user_quick_change: bool = False,
    learning_enabled: bool = False,
    learning_persistent_notifications: bool = True,
) -> str:
    """Return a stable token for hierarchy, schedule and panel preference."""
    payload = json.dumps(
        {
            "zones": zones,
            "schedule": schedule.to_dict(),
            CONF_SHOW_IN_SIDEBAR: show_in_sidebar,
            CONF_STANDARD_USER_SCHEDULE: standard_user_schedule,
            CONF_STANDARD_USER_QUICK_CHANGE: standard_user_quick_change,
            CONF_LEARNING_ENABLED: learning_enabled,
            CONF_LEARNING_PERSISTENT_NOTIFICATIONS: learning_persistent_notifications,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _entry_data(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    if data is None:
        raise KeyError(entry_id)
    return data


def current_revision(hass: HomeAssistant, entry_id: str) -> str:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise KeyError(entry_id)
    data = _entry_data(hass, entry_id)
    return configuration_revision(
        list(entry.options.get(CONF_ZONES, [])),
        data["schedule_runtime"].configuration,
        entry.options.get(CONF_SHOW_IN_SIDEBAR, True),
        entry.options.get(CONF_STANDARD_USER_SCHEDULE, False),
        entry.options.get(CONF_STANDARD_USER_QUICK_CHANGE, False),
        entry.options.get(CONF_LEARNING_ENABLED, False),
        entry.options.get(CONF_LEARNING_PERSISTENT_NOTIFICATIONS, True),
    )


def configuration_snapshot(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Return the complete non-secret state required by the panel."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise KeyError(entry_id)
    data = _entry_data(hass, entry_id)
    schedule = data["schedule_runtime"].configuration
    zones = _json_copy(list(entry.options.get(CONF_ZONES, [])))
    show_in_sidebar = entry.options.get(CONF_SHOW_IN_SIDEBAR, True)
    standard_user_schedule = entry.options.get(CONF_STANDARD_USER_SCHEDULE, False)
    standard_user_quick_change = entry.options.get(
        CONF_STANDARD_USER_QUICK_CHANGE, False
    )
    learning_enabled = entry.options.get(CONF_LEARNING_ENABLED, False)
    learning_persistent_notifications = entry.options.get(
        CONF_LEARNING_PERSISTENT_NOTIFICATIONS, True
    )
    return {
        "entry_id": entry_id,
        "title": entry.title,
        "revision": configuration_revision(
            zones,
            schedule,
            show_in_sidebar,
            standard_user_schedule,
            standard_user_quick_change,
            learning_enabled,
            learning_persistent_notifications,
        ),
        CONF_SHOW_IN_SIDEBAR: show_in_sidebar,
        CONF_STANDARD_USER_SCHEDULE: standard_user_schedule,
        CONF_STANDARD_USER_QUICK_CHANGE: standard_user_quick_change,
        CONF_LEARNING_ENABLED: learning_enabled,
        CONF_LEARNING_PERSISTENT_NOTIFICATIONS: learning_persistent_notifications,
        "zones": zones,
        "schedule": schedule.to_dict(),
        "away_mode": data["schedule_runtime"].away_mode_state(),
        "quick_change": data["schedule_runtime"].quick_change_state(),
        "zone_control": data["coordinator"].zone_control_snapshot(),
        "last_changes": data["audit_log"].latest_applied_by_room(),
        "catalog": configuration_catalog(hass, entry_id),
    }


def configuration_catalog(
    hass: HomeAssistant, entry_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Return separate canonical and physical equipment catalogs.

    ZEAL's generated room thermostats are the only climate entities exposed as
    scheduling targets. They are deliberately kept out of the physical room
    thermostat catalog, where selecting one would make ZEAL drive itself.
    """
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    areas = sorted(
        (
            {"area_id": area.id, "name": area.name}
            for area in area_registry.async_list_areas()
        ),
        key=lambda item: item["name"].casefold(),
    )
    catalog: dict[str, list[dict[str, Any]]] = {
        "areas": areas,
        "calendars": [],
        "switches": [],
        "zeal_room_thermostats": [],
        "zeal_room_demands": [],
        "physical_room_thermostats": [],
        "temperature_sensors": [],
        "opening_sensors": [],
    }
    for entity in entity_registry.entities.values():
        if entity.disabled_by is not None or entity.platform == DOMAIN:
            continue
        area_id = entity.area_id
        if area_id is None and entity.device_id:
            device = device_registry.async_get(entity.device_id)
            area_id = device.area_id if device else None
        item = {
            "entity_id": entity.entity_id,
            "name": entity.name or entity.original_name or entity.entity_id,
            "area_id": area_id,
        }
        if entity.domain == "switch":
            catalog["switches"].append(item)
        elif entity.domain == "calendar":
            catalog["calendars"].append(item)
        elif entity.domain == "climate":
            catalog["physical_room_thermostats"].append(item)
        elif entity.domain == "sensor" and (
            entity.device_class or entity.original_device_class
        ) == "temperature":
            catalog["temperature_sensors"].append(item)
        elif entity.domain == "binary_sensor" and (
            entity.device_class or entity.original_device_class
        ) in {"door", "garage_door", "opening", "window"}:
            catalog["opening_sensors"].append(item)
    data = _entry_data(hass, entry_id)
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise KeyError(entry_id)
    for zone in entry.options.get(CONF_ZONES, []):
        for room in zone.get(ZONE_ROOMS, []):
            thermostat = data["coordinator"].room_thermostats.get(room[ROOM_ID])
            entity_id = getattr(thermostat, "entity_id", None)
            if not entity_id:
                continue
            registry_entry = entity_registry.async_get(entity_id)
            catalog["zeal_room_thermostats"].append(
                {
                    "entity_id": entity_id,
                    "name": (
                        (registry_entry.name or registry_entry.original_name)
                        if registry_entry
                        else getattr(thermostat, "name", entity_id)
                    )
                    or entity_id,
                    "room_id": room[ROOM_ID],
                    "zone_id": zone[ZONE_ID],
                }
            )
            demand_unique_id = f"{entry_id}_{room[ROOM_ID]}_heat_demand"
            demand_entry = next(
                (
                    candidate
                    for candidate in entity_registry.entities.values()
                    if candidate.domain == "binary_sensor"
                    and candidate.platform == DOMAIN
                    and candidate.unique_id == demand_unique_id
                ),
                None,
            )
            if demand_entry is not None and demand_entry.disabled_by is None:
                catalog["zeal_room_demands"].append(
                    {
                        "entity_id": demand_entry.entity_id,
                        "name": demand_entry.name
                        or demand_entry.original_name
                        or demand_entry.entity_id,
                        "room_id": room[ROOM_ID],
                        "zone_id": zone[ZONE_ID],
                    }
                )
    for key in (
        "calendars",
        "switches",
        "zeal_room_thermostats",
        "zeal_room_demands",
        "physical_room_thermostats",
        "temperature_sensors",
        "opening_sensors",
    ):
        catalog[key].sort(
            key=lambda item: (str(item["name"]).casefold(), item["entity_id"])
        )
    return catalog


def validate_away_mode(
    hass: HomeAssistant,
    *,
    mode: str,
    calendar_entity_id: str | None,
    starts_at: str | None,
    ends_at: str | None,
    temperature: float,
) -> AwayModeConfiguration:
    """Validate one mutually-exclusive Away activation source."""

    def normalize_timestamp(value: str | None, field: str) -> str | None:
        if not value:
            return None
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            raise ValueError(f"{field} must be a valid date and time")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        if parsed.minute % 5 or parsed.second or parsed.microsecond:
            raise ValueError(f"{field} must use a five-minute interval")
        return dt_util.as_utc(parsed).isoformat()

    away_mode = AwayModeConfiguration(
        mode=mode,
        calendar_entity_id=calendar_entity_id or None,
        starts_at=normalize_timestamp(starts_at, "Away start"),
        ends_at=normalize_timestamp(ends_at, "Away end"),
        temperature=temperature,
    )
    if away_mode.mode == "calendar":
        entity = er.async_get(hass).async_get(away_mode.calendar_entity_id)
        if entity is None or entity.domain != "calendar":
            raise ValueError(
                f"Unknown Away calendar: {away_mode.calendar_entity_id}"
            )
        if entity.disabled_by is not None:
            raise ValueError(
                f"Away calendar is disabled: {away_mode.calendar_entity_id}"
            )
    return away_mode


def export_configuration(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Build a directly downloadable, JSON-safe configuration document."""
    return {
        "format": "zeal-configuration",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **configuration_snapshot(hass, entry_id),
    }


def _require_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_unique_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a list of entity IDs")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} contains duplicate entity IDs")
    return list(value)


def validate_hierarchy(
    hass: HomeAssistant, raw_zones: Any
) -> list[dict[str, Any]]:
    """Validate the complete Zone -> Area/Room -> equipment document."""
    if not isinstance(raw_zones, list):
        raise ValueError("zones must be a list")
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    zone_ids: set[str] = set()
    room_ids: set[str] = set()
    used_switches: set[str] = set()
    used_room_entities: set[str] = set()
    normalized: list[dict[str, Any]] = []

    def registry_entity(entity_id: str, domain: str, field: str):
        entity = entity_registry.async_get(entity_id)
        if entity is None or entity.domain != domain:
            raise ValueError(f"{field} contains an unknown {domain} entity: {entity_id}")
        if entity.platform == DOMAIN:
            raise ValueError(f"{field} cannot contain a ZEAL-owned entity: {entity_id}")
        if entity.disabled_by is not None:
            raise ValueError(f"{field} contains a disabled entity: {entity_id}")
        return entity

    def effective_area_id(entity) -> str | None:
        if entity.area_id is not None:
            return entity.area_id
        device = device_registry.async_get(entity.device_id) if entity.device_id else None
        return device.area_id if device else None

    for zone_index, raw_zone in enumerate(raw_zones):
        if not isinstance(raw_zone, Mapping):
            raise ValueError(f"zones[{zone_index}] must be an object")
        zone_id = _require_name(raw_zone.get(ZONE_ID), f"zones[{zone_index}].zone_id")
        if zone_id in zone_ids:
            raise ValueError(f"duplicate zone_id: {zone_id}")
        zone_ids.add(zone_id)
        name = _require_name(raw_zone.get(ZONE_NAME), f"zones[{zone_index}].name")
        switch = raw_zone.get(ZONE_SWITCH) or None
        if switch is not None:
            if not isinstance(switch, str):
                raise ValueError(f"zones[{zone_index}].switch must be an entity ID")
            registry_entity(switch, "switch", f"zones[{zone_index}].switch")
            if switch in used_switches:
                raise ValueError(f"zone switch is assigned more than once: {switch}")
            used_switches.add(switch)
        heat_source = raw_zone.get(ZONE_HEAT_SOURCE, HEAT_SOURCE_ASHP)
        if heat_source not in HEAT_SOURCE_OPTIONS:
            raise ValueError(f"zones[{zone_index}].heat_source is invalid")
        delay = raw_zone.get(
            ZONE_REENABLE_DELAY,
            HEAT_SOURCE_DEFAULT_REENABLE_DELAY.get(
                heat_source, DEFAULT_REENABLE_DELAY
            ),
        )
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not float(delay).is_integer()
        ):
            raise ValueError(
                f"zones[{zone_index}].reenable_delay must be a whole number"
            )
        if not 0 <= float(delay) <= 3600:
            raise ValueError(f"zones[{zone_index}].reenable_delay must be 0 to 3600")
        raw_rooms = raw_zone.get(ZONE_ROOMS, [])
        if not isinstance(raw_rooms, list):
            raise ValueError(f"zones[{zone_index}].rooms must be a list")
        rooms: list[dict[str, Any]] = []
        for room_index, raw_room in enumerate(raw_rooms):
            field = f"zones[{zone_index}].rooms[{room_index}]"
            if not isinstance(raw_room, Mapping):
                raise ValueError(f"{field} must be an object")
            room_id = _require_name(raw_room.get(ROOM_ID), f"{field}.room_id")
            area = area_registry.async_get_area(room_id)
            if area is None:
                raise ValueError(f"{field}.room_id is not a Home Assistant Area")
            if room_id in room_ids:
                raise ValueError(f"Area is assigned to more than one zone: {room_id}")
            room_ids.add(room_id)
            trvs = _require_unique_strings(raw_room.get(ROOM_TRVS, []), f"{field}.trvs")
            sensors = _require_unique_strings(
                raw_room.get(ROOM_SENSORS, []), f"{field}.sensors"
            )
            opening_sensors = _require_unique_strings(
                raw_room.get(ROOM_OPENING_SENSORS, []),
                f"{field}.opening_sensors",
            )
            for entity_id in trvs:
                entity = registry_entity(entity_id, "climate", f"{field}.trvs")
                if effective_area_id(entity) != room_id:
                    raise ValueError(f"{entity_id} does not belong to Area {room_id}")
            for entity_id in sensors:
                entity = registry_entity(entity_id, "sensor", f"{field}.sensors")
                device_class = entity.device_class or entity.original_device_class
                if device_class != "temperature":
                    raise ValueError(f"{entity_id} is not a temperature sensor")
                if effective_area_id(entity) != room_id:
                    raise ValueError(f"{entity_id} does not belong to Area {room_id}")
            for entity_id in opening_sensors:
                entity = registry_entity(
                    entity_id, "binary_sensor", f"{field}.opening_sensors"
                )
                device_class = entity.device_class or entity.original_device_class
                if device_class not in {"door", "garage_door", "opening", "window"}:
                    raise ValueError(f"{entity_id} is not a window/door sensor")
                if effective_area_id(entity) != room_id:
                    raise ValueError(f"{entity_id} does not belong to Area {room_id}")
            duplicates = used_room_entities.intersection(
                (*trvs, *sensors, *opening_sensors)
            )
            if duplicates:
                raise ValueError(
                    "room equipment is assigned more than once: "
                    + ", ".join(sorted(duplicates))
                )
            used_room_entities.update((*trvs, *sensors, *opening_sensors))
            active = raw_room.get(ROOM_ACTIVE, True)
            if not isinstance(active, bool):
                raise ValueError(f"{field}.active must be true or false")
            occupancy_enabled = raw_room.get(ROOM_OCCUPANCY_ENABLED, False)
            if not isinstance(occupancy_enabled, bool):
                raise ValueError(
                    f"{field}.occupancy_enabled must be true or false"
                )
            unoccupied_temperature = raw_room.get(
                ROOM_UNOCCUPIED_TEMPERATURE, 12.0
            )
            if (
                isinstance(unoccupied_temperature, bool)
                or not isinstance(unoccupied_temperature, (int, float))
                or not MIN_TARGET_TEMPERATURE
                <= float(unoccupied_temperature)
                <= MAX_TARGET_TEMPERATURE
            ):
                raise ValueError(
                    f"{field}.unoccupied_temperature must be from "
                    f"{MIN_TARGET_TEMPERATURE} to {MAX_TARGET_TEMPERATURE}"
                )
            occupancy_source = raw_room.get(
                ROOM_OCCUPANCY_SOURCE, OCCUPANCY_SOURCE_MANUAL
            )
            if occupancy_source not in (
                OCCUPANCY_SOURCE_MANUAL,
                OCCUPANCY_SOURCE_CALENDAR,
            ):
                raise ValueError(f"{field}.occupancy_source is invalid")
            occupancy_calendar = raw_room.get(ROOM_OCCUPANCY_CALENDAR) or None
            if (
                occupancy_enabled
                and occupancy_source == OCCUPANCY_SOURCE_CALENDAR
            ):
                if not isinstance(occupancy_calendar, str):
                    raise ValueError(
                        f"{field}.occupancy_calendar is required for calendar occupancy"
                    )
                registry_entity(
                    occupancy_calendar,
                    "calendar",
                    f"{field}.occupancy_calendar",
                )
            cooling_capable = raw_room.get(ROOM_COOLING_CAPABLE, False)
            if not isinstance(cooling_capable, bool):
                raise ValueError(
                    f"{field}.cooling_capable must be true or false"
                )
            rooms.append(
                {
                    ROOM_ID: room_id,
                    ROOM_NAME: area.name,
                    ROOM_TRVS: trvs,
                    ROOM_SENSORS: sensors,
                    ROOM_OPENING_SENSORS: opening_sensors,
                    ROOM_ACTIVE: active,
                    ROOM_OCCUPANCY_ENABLED: occupancy_enabled,
                    ROOM_UNOCCUPIED_TEMPERATURE: float(
                        unoccupied_temperature
                    ),
                    ROOM_OCCUPANCY_SOURCE: occupancy_source,
                    ROOM_OCCUPANCY_CALENDAR: (
                        occupancy_calendar
                        if occupancy_enabled
                        and occupancy_source == OCCUPANCY_SOURCE_CALENDAR
                        else None
                    ),
                    ROOM_COOLING_CAPABLE: cooling_capable,
                }
            )
        capability = raw_zone.get(
            ZONE_ASHP_CAPABILITY, ASHP_CAPABILITY_HEAT_ONLY
        )
        if capability not in (
            ASHP_CAPABILITY_HEAT_ONLY,
            ASHP_CAPABILITY_HEAT_AND_COOL,
        ):
            raise ValueError(f"zones[{zone_index}].ashp_capability is invalid")
        normalized.append(
            {
                ZONE_ID: zone_id,
                ZONE_NAME: name,
                ZONE_SWITCH: switch,
                ZONE_HEAT_SOURCE: heat_source,
                ZONE_REENABLE_DELAY: int(delay),
                ZONE_ASHP_CAPABILITY: capability,
                ZONE_ROOMS: rooms,
            }
        )
    return normalized


async def async_save_schedule(
    hass: HomeAssistant,
    entry_id: str,
    configuration: ScheduleConfiguration,
    *,
    expected_revision: str,
) -> str:
    """Persist validated schedule data and update the live runtime atomically."""
    if current_revision(hass, entry_id) != expected_revision:
        raise ConfigurationConflictError("Configuration changed; reload and try again")
    data = _entry_data(hass, entry_id)
    await data["schedule_storage"].async_save(configuration)
    await data["schedule_runtime"].async_set_configuration(configuration)
    return current_revision(hass, entry_id)


async def async_save_away_mode(
    hass: HomeAssistant,
    entry_id: str,
    *,
    expected_revision: str,
    mode: str,
    calendar_entity_id: str | None,
    starts_at: str | None,
    ends_at: str | None,
    temperature: float,
) -> tuple[AwayModeConfiguration, str]:
    """Validate and persist global Away settings through the schedule Store."""
    away_mode = validate_away_mode(
        hass,
        mode=mode,
        calendar_entity_id=calendar_entity_id,
        starts_at=starts_at,
        ends_at=ends_at,
        temperature=temperature,
    )
    data = _entry_data(hass, entry_id)
    updated = with_away_mode(
        data["schedule_runtime"].configuration,
        away_mode,
    )
    revision = await async_save_schedule(
        hass,
        entry_id,
        updated,
        expected_revision=expected_revision,
    )
    return away_mode, revision


async def async_save_hierarchy(
    hass: HomeAssistant,
    entry_id: str,
    raw_zones: Any,
    *,
    expected_revision: str,
    show_in_sidebar: bool | None = None,
    standard_user_schedule: bool | None = None,
    standard_user_quick_change: bool | None = None,
    learning_enabled: bool | None = None,
    learning_persistent_notifications: bool | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Validate/save hierarchy, reconcile schedules, then trigger safe reload."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise KeyError(entry_id)
    if current_revision(hass, entry_id) != expected_revision:
        raise ConfigurationConflictError("Configuration changed; reload and try again")
    if show_in_sidebar is not None and not isinstance(show_in_sidebar, bool):
        raise ValueError("show_in_sidebar must be true or false")
    if standard_user_schedule is not None and not isinstance(standard_user_schedule, bool):
        raise ValueError("standard_user_schedule must be true or false")
    if standard_user_quick_change is not None and not isinstance(standard_user_quick_change, bool):
        raise ValueError("standard_user_quick_change must be true or false")
    if learning_enabled is not None and not isinstance(learning_enabled, bool):
        raise ValueError("learning_enabled must be true or false")
    if learning_persistent_notifications is not None and not isinstance(
        learning_persistent_notifications, bool
    ):
        raise ValueError("learning_persistent_notifications must be true or false")
    effective_show_in_sidebar = (
        entry.options.get(CONF_SHOW_IN_SIDEBAR, True)
        if show_in_sidebar is None
        else show_in_sidebar
    )
    effective_standard_user_schedule = (
        entry.options.get(CONF_STANDARD_USER_SCHEDULE, False)
        if standard_user_schedule is None
        else standard_user_schedule
    )
    effective_standard_user_quick_change = (
        entry.options.get(CONF_STANDARD_USER_QUICK_CHANGE, False)
        if standard_user_quick_change is None
        else standard_user_quick_change
    )
    effective_learning_enabled = (
        entry.options.get(CONF_LEARNING_ENABLED, False)
        if learning_enabled is None
        else learning_enabled
    )
    effective_learning_persistent_notifications = (
        entry.options.get(CONF_LEARNING_PERSISTENT_NOTIFICATIONS, True)
        if learning_persistent_notifications is None
        else learning_persistent_notifications
    )
    zones = validate_hierarchy(hass, raw_zones)
    data = _entry_data(hass, entry_id)
    current_schedule = data["schedule_runtime"].configuration
    configured_rooms = {
        room[ROOM_ID]: room[ROOM_NAME]
        for zone in zones
        for room in zone[ZONE_ROOMS]
        if room[ROOM_TRVS]
    }
    schedule = reconcile_room_schedules(current_schedule, configured_rooms)
    await data["schedule_storage"].async_save(schedule)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_ZONES: zones,
            CONF_SHOW_IN_SIDEBAR: effective_show_in_sidebar,
            CONF_STANDARD_USER_SCHEDULE: effective_standard_user_schedule,
            CONF_STANDARD_USER_QUICK_CHANGE: effective_standard_user_quick_change,
            CONF_LEARNING_ENABLED: effective_learning_enabled,
            CONF_LEARNING_PERSISTENT_NOTIFICATIONS: effective_learning_persistent_notifications,
        },
    )
    if not effective_learning_persistent_notifications:
        from homeassistant.components import persistent_notification

        persistent_notification.async_dismiss(
            hass, f"zeal_learning_{entry_id}"
        )
    return zones, configuration_revision(
        zones,
        schedule,
        effective_show_in_sidebar,
        effective_standard_user_schedule,
        effective_standard_user_quick_change,
        effective_learning_enabled,
        effective_learning_persistent_notifications,
    )
