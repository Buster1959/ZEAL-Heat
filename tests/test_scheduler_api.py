"""Block 4 validation, persistence, audit and admin WebSocket tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeal.const import (
    AUDIT_MAX_ENTRIES,
    CONF_LEARNING_ENABLED,
    CONF_SHOW_IN_SIDEBAR,
    CONF_STANDARD_USER_QUICK_CHANGE,
    CONF_STANDARD_USER_SCHEDULE,
    CONF_ZONES,
    DOMAIN,
    ROOM_ACTIVE,
    ROOM_ID,
    ROOM_NAME,
    ROOM_OPENING_SENSORS,
    ROOM_OCCUPANCY_CALENDAR,
    ROOM_OCCUPANCY_ENABLED,
    ROOM_OCCUPANCY_SOURCE,
    ROOM_SENSORS,
    ROOM_TRVS,
    ROOM_UNOCCUPIED_TEMPERATURE,
    ZONE_ID,
    ZONE_NAME,
    ZONE_ROOMS,
    ZONE_SWITCH,
)
from custom_components.zeal.scheduler.audit import AuditLog
from custom_components.zeal.scheduler.configuration import (
    ConfigurationConflictError,
    async_save_hierarchy,
    async_save_schedule,
    configuration_revision,
    configuration_snapshot,
    export_configuration,
    validate_hierarchy,
)
from custom_components.zeal.scheduler.models import (
    WEEKDAYS,
    RoomSchedule,
    ScheduleConfiguration,
    SchedulePeriod,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er


class MemoryStore:
    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


def create_registry_fixture(hass):
    area = ar.async_get(hass).async_create("Living Room")
    registry = er.async_get(hass)
    switch = registry.async_get_or_create(
        "switch",
        "test",
        "heating_switch",
        suggested_object_id="heating_switch",
        original_name="Heating Switch",
    )
    trv = registry.async_get_or_create(
        "climate",
        "test",
        "living_trv",
        suggested_object_id="living_trv",
        original_name="Living TRV",
    )
    sensor = registry.async_get_or_create(
        "sensor",
        "test",
        "living_temperature",
        suggested_object_id="living_temperature",
        original_name="Living Temperature",
        original_device_class="temperature",
    )
    registry.async_update_entity(trv.entity_id, area_id=area.id)
    registry.async_update_entity(sensor.entity_id, area_id=area.id)
    zone = {
        ZONE_ID: "ground_floor",
        ZONE_NAME: "Ground Floor",
        ZONE_SWITCH: switch.entity_id,
        "heat_source": "ashp",
        "reenable_delay": 300,
        ZONE_ROOMS: [
            {
                ROOM_ID: area.id,
                ROOM_NAME: "Untrusted Browser Name",
                ROOM_TRVS: [trv.entity_id],
                ROOM_SENSORS: [sensor.entity_id],
                ROOM_ACTIVE: True,
            }
        ],
    }
    return area, switch, trv, sensor, zone


async def setup_loaded_entry(hass, zones):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ZEAL HVAC System",
        data={},
        options={CONF_ZONES: zones},
    )
    entry.add_to_hass(hass)
    for zone in zones:
        if zone.get(ZONE_SWITCH):
            hass.states.async_set(zone[ZONE_SWITCH], "off")
        for room in zone.get(ZONE_ROOMS, []):
            for trv in room.get(ROOM_TRVS, []):
                hass.states.async_set(trv, "heat", {"temperature": 20.0})
            for sensor in room.get(ROOM_SENSORS, []):
                hass.states.async_set(sensor, "20.0")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def monday_schedule(room_id, room_name="Living Room", temperature=20):
    periods = (
        SchedulePeriod("morning", "morning", "Morning", "00:00", temperature),
    )
    return RoomSchedule(
        room_id,
        room_name,
        {day: periods if day == "monday" else () for day in WEEKDAYS},
    )


def test_revision_is_deterministic_and_changes_with_either_document():
    schedule = ScheduleConfiguration.empty()
    first = configuration_revision([], schedule)
    assert first == configuration_revision([], schedule)
    assert first != configuration_revision(
        [], ScheduleConfiguration(settings={"changed": True})
    )
    assert first != configuration_revision(
        [{ZONE_ID: "one"}], schedule
    )
    assert first != configuration_revision([], schedule, False)
    assert first != configuration_revision([], schedule, True, True, False)
    assert first != configuration_revision([], schedule, True, False, True)


def test_valid_hierarchy_is_normalized_from_home_assistant_registries(hass):
    area, switch, trv, sensor, zone = create_registry_fixture(hass)
    registry = er.async_get(hass)
    opening = registry.async_get_or_create(
        "binary_sensor",
        "test",
        "living_window",
        suggested_object_id="living_window",
        original_name="Living Window",
        original_device_class="window",
    )
    registry.async_update_entity(opening.entity_id, area_id=area.id)
    zone[ZONE_ROOMS][0][ROOM_OPENING_SENSORS] = [opening.entity_id]
    normalized = validate_hierarchy(hass, [zone])
    room = normalized[0][ZONE_ROOMS][0]
    assert room[ROOM_NAME] == area.name
    assert room[ROOM_TRVS] == [trv.entity_id]
    assert room[ROOM_SENSORS] == [sensor.entity_id]
    assert room[ROOM_OPENING_SENSORS] == [opening.entity_id]
    assert room[ROOM_OCCUPANCY_ENABLED] is False
    assert room[ROOM_UNOCCUPIED_TEMPERATURE] == 12.0
    assert room[ROOM_OCCUPANCY_SOURCE] == "manual"
    assert room[ROOM_OCCUPANCY_CALENDAR] is None
    assert normalized[0][ZONE_SWITCH] == switch.entity_id


def test_occupancy_calendar_configuration_is_validated_and_normalized(hass):
    _, _, _, _, zone = create_registry_fixture(hass)
    calendar = er.async_get(hass).async_get_or_create(
        "calendar",
        "test",
        "guest_room",
        suggested_object_id="guest_room",
    )
    room = zone[ZONE_ROOMS][0]
    room.update(
        {
            ROOM_OCCUPANCY_ENABLED: True,
            ROOM_UNOCCUPIED_TEMPERATURE: 11.5,
            ROOM_OCCUPANCY_SOURCE: "calendar",
            ROOM_OCCUPANCY_CALENDAR: calendar.entity_id,
        }
    )

    normalized = validate_hierarchy(hass, [zone])[0][ZONE_ROOMS][0]
    assert normalized[ROOM_OCCUPANCY_ENABLED] is True
    assert normalized[ROOM_UNOCCUPIED_TEMPERATURE] == 11.5
    assert normalized[ROOM_OCCUPANCY_SOURCE] == "calendar"
    assert normalized[ROOM_OCCUPANCY_CALENDAR] == calendar.entity_id

    room[ROOM_OCCUPANCY_CALENDAR] = "calendar.missing"
    with pytest.raises(ValueError, match="unknown calendar"):
        validate_hierarchy(hass, [zone])


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda zones: zones.append(dict(zones[0])), "duplicate zone_id"),
        (
            lambda zones: zones[0][ZONE_ROOMS].append(dict(zones[0][ZONE_ROOMS][0])),
            "more than one zone",
        ),
        (lambda zones: zones[0].update({"reenable_delay": 3601}), "0 to 3600"),
        (lambda zones: zones[0].update({"reenable_delay": 1.5}), "whole number"),
        (lambda zones: zones[0].update({"heat_source": "nuclear"}), "invalid"),
        (lambda zones: zones[0].update({"ashp_capability": "unsafe"}), "invalid"),
    ],
)
def test_hierarchy_rejects_duplicates_and_invalid_controls(hass, mutate, match):
    _, _, _, _, zone = create_registry_fixture(hass)
    zones = [zone]
    mutate(zones)
    with pytest.raises(ValueError, match=match):
        validate_hierarchy(hass, zones)


def test_hierarchy_rejects_zeal_owned_thermostat_as_physical_trv(hass):
    _, _, trv, _, zone = create_registry_fixture(hass)
    registry = er.async_get(hass)
    registry.async_remove(trv.entity_id)
    own = registry.async_get_or_create(
        "climate",
        DOMAIN,
        "own_thermostat",
        suggested_object_id="own_thermostat",
    )
    registry.async_update_entity(
        own.entity_id, area_id=zone[ZONE_ROOMS][0][ROOM_ID]
    )
    zone[ZONE_ROOMS][0][ROOM_TRVS] = [own.entity_id]
    with pytest.raises(ValueError, match="ZEAL-owned"):
        validate_hierarchy(hass, [zone])


async def test_catalog_separates_zeal_targets_from_physical_room_equipment(hass):
    """Selectors cannot mix canonical ZEAL targets with physical devices."""
    area, _, trv, sensor, zone = create_registry_fixture(hass)
    calendar = er.async_get(hass).async_get_or_create(
        "calendar",
        "test",
        "holidays",
        suggested_object_id="holidays",
        original_name="Holidays",
    )
    opening = er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "test",
        "living_window",
        suggested_object_id="living_window",
        original_name="Living Window",
        original_device_class="window",
    )
    er.async_get(hass).async_update_entity(opening.entity_id, area_id=area.id)
    zone[ZONE_ROOMS][0][ROOM_OPENING_SENSORS] = [opening.entity_id]
    entry = await setup_loaded_entry(hass, [zone])

    catalog = configuration_snapshot(hass, entry.entry_id)["catalog"]
    physical_ids = {
        item["entity_id"] for item in catalog["physical_room_thermostats"]
    }
    zeal_targets = catalog["zeal_room_thermostats"]
    zeal_demands = catalog["zeal_room_demands"]
    sensor_ids = {item["entity_id"] for item in catalog["temperature_sensors"]}
    calendar_ids = {item["entity_id"] for item in catalog["calendars"]}
    opening_ids = {item["entity_id"] for item in catalog["opening_sensors"]}

    assert trv.entity_id in physical_ids
    assert sensor.entity_id in sensor_ids
    assert calendar.entity_id in calendar_ids
    assert opening.entity_id in opening_ids
    assert configuration_snapshot(hass, entry.entry_id)["zones"][0][ZONE_ROOMS][0][
        ROOM_OPENING_SENSORS
    ] == [opening.entity_id]
    assert len(zeal_targets) == 1
    assert len(zeal_demands) == 1
    assert zeal_demands[0]["room_id"] == area.id
    assert zeal_demands[0]["zone_id"] == "ground_floor"
    assert zeal_demands[0]["entity_id"].startswith("binary_sensor.")
    entity_registry = er.async_get(hass)
    entity_registry.async_update_entity(
        zeal_demands[0]["entity_id"],
        new_entity_id="binary_sensor.renamed_room_demand",
    )
    renamed_demands = configuration_snapshot(hass, entry.entry_id)["catalog"][
        "zeal_room_demands"
    ]
    assert renamed_demands[0]["entity_id"] == "binary_sensor.renamed_room_demand"
    assert zeal_targets[0]["room_id"] == area.id
    assert zeal_targets[0]["zone_id"] == "ground_floor"
    assert zeal_targets[0]["entity_id"].startswith("climate.")
    assert zeal_targets[0]["entity_id"] not in physical_ids
    assert all(
        er.async_get(hass).async_get(entity_id).platform != DOMAIN
        for entity_id in (*physical_ids, *sensor_ids)
    )


async def test_schedule_save_updates_store_runtime_and_rejects_stale_revision(hass):
    area, _, _, _, zone = create_registry_fixture(hass)
    entry = await setup_loaded_entry(hass, [zone])
    snapshot = configuration_snapshot(hass, entry.entry_id)
    assert "zone_control" in snapshot
    assert "ground_floor" in snapshot["zone_control"]
    configuration = ScheduleConfiguration(
        rooms={area.id: monday_schedule(area.id)},
        settings={"saved": True},
        temperature_unit="°C",
    )
    new_revision = await async_save_schedule(
        hass,
        entry.entry_id,
        configuration,
        expected_revision=snapshot["revision"],
    )
    data = hass.data[DOMAIN][entry.entry_id]
    assert data["schedule_runtime"].configuration == configuration
    assert await data["schedule_storage"].async_load() == configuration
    assert new_revision != snapshot["revision"]
    with pytest.raises(ConfigurationConflictError):
        await async_save_schedule(
            hass,
            entry.entry_id,
            ScheduleConfiguration(settings={"stale": True}, temperature_unit="°C"),
            expected_revision=snapshot["revision"],
        )


async def test_configuration_export_is_json_safe_and_complete(hass):
    _, _, _, _, zone = create_registry_fixture(hass)
    entry = await setup_loaded_entry(hass, [zone])
    exported = export_configuration(hass, entry.entry_id)
    assert exported["format"] == "zeal-configuration"
    assert exported["entry_id"] == entry.entry_id
    assert exported["zones"][0][ZONE_ID] == "ground_floor"
    assert exported["schedule"]["version"] == 1
    assert "revision" in exported
    assert "generated_at" in exported


async def test_hierarchy_save_persists_and_survives_automatic_reload(hass):
    area, _, _, _, zone = create_registry_fixture(hass)
    entry = await setup_loaded_entry(hass, [zone])
    revision = configuration_snapshot(hass, entry.entry_id)["revision"]
    assert configuration_snapshot(hass, entry.entry_id)[CONF_SHOW_IN_SIDEBAR] is True
    edited_zone = deepcopy(zone)
    edited_zone[ZONE_NAME] = "Renamed Ground Floor"
    zones, new_revision = await async_save_hierarchy(
        hass,
        entry.entry_id,
        [edited_zone],
        expected_revision=revision,
        show_in_sidebar=False,
        standard_user_schedule=True,
        standard_user_quick_change=True,
    )
    assert zones[0][ZONE_NAME] == "Renamed Ground Floor"
    assert new_revision != revision
    await hass.async_block_till_done()
    reloaded_entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert reloaded_entry.options[CONF_ZONES][0][ZONE_NAME] == "Renamed Ground Floor"
    assert reloaded_entry.options[CONF_SHOW_IN_SIDEBAR] is False
    assert reloaded_entry.options[CONF_STANDARD_USER_SCHEDULE] is True
    assert reloaded_entry.options[CONF_STANDARD_USER_QUICK_CHANGE] is True
    assert configuration_snapshot(hass, entry.entry_id)[CONF_SHOW_IN_SIDEBAR] is False
    assert configuration_snapshot(hass, entry.entry_id)[CONF_STANDARD_USER_SCHEDULE] is True
    assert configuration_snapshot(hass, entry.entry_id)[CONF_STANDARD_USER_QUICK_CHANGE] is True
    assert area.id in hass.data[DOMAIN][entry.entry_id][
        "schedule_runtime"
    ].configuration.rooms


async def test_audit_load_record_export_and_retention():
    store = MemoryStore(
        {
            "version": 1,
            "entries": [
                {"sequence": index} for index in range(AUDIT_MAX_ENTRIES + 1)
            ],
        }
    )
    audit = AuditLog(None, "entry", store=store)
    await audit.async_load()
    assert len(audit.export()["entries"]) == AUDIT_MAX_ENTRIES
    assert audit.export()["entries"][0]["sequence"] == 1
    await audit.async_record(
        timestamp=datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc),
        room_id="living_room",
        room_name="Living Room",
        canonical_entity_id="climate.zeal_living_room",
        previous_temperature=18,
        requested_temperature=20,
        cause="scheduled_transition",
        outcome="applied",
    )
    last = audit.export()["entries"][-1]
    assert last["room_id"] == "living_room"
    assert last["requested_temperature"] == 20.0
    assert len(store.data["entries"]) == AUDIT_MAX_ENTRIES
    assert audit.latest_applied_by_room()["living_room"] == last


async def test_audit_latest_change_ignores_failed_attempts():
    store = MemoryStore()
    audit = AuditLog(None, "entry", store=store)
    await audit.async_load()
    for minute, temperature, outcome in (
        (0, 19, "applied"),
        (5, 21, "skipped_unavailable"),
        (10, 20, "applied"),
    ):
        await audit.async_record(
            timestamp=datetime(2026, 8, 24, 7, minute, tzinfo=timezone.utc),
            room_id="living_room",
            room_name="Living Room",
            canonical_entity_id="climate.zeal_living_room",
            previous_temperature=18,
            requested_temperature=temperature,
            cause="scheduled_transition",
            outcome=outcome,
        )
    latest = audit.latest_applied_by_room()["living_room"]
    assert latest["timestamp"] == "2026-08-24T07:10:00+00:00"
    assert latest["requested_temperature"] == 20.0


async def test_audit_records_survive_a_fresh_runtime_instance():
    """A download after restart sees the same bounded persisted history."""
    store = MemoryStore()
    first = AuditLog(None, "entry", store=store)
    await first.async_load()
    await first.async_record(
        timestamp=datetime(2026, 8, 24, 22, 30, tzinfo=timezone.utc),
        room_id="bedroom",
        room_name="Bedroom",
        canonical_entity_id="climate.zeal_bedroom",
        previous_temperature=20,
        requested_temperature=17,
        cause="scheduled_transition",
        outcome="applied",
    )

    restarted = AuditLog(None, "entry", store=store)
    await restarted.async_load()
    exported = restarted.export()
    assert len(exported["entries"]) == 1
    assert exported["entries"][0]["room_id"] == "bedroom"
    assert exported["entries"][0]["requested_temperature"] == 17.0


async def test_admin_websocket_reads_configuration_and_downloads(
    hass, hass_ws_client
):
    _, _, _, _, zone = create_registry_fixture(hass)
    entry = await setup_loaded_entry(hass, [zone])
    client = await hass_ws_client()
    await client.send_json_auto_id(
        {"type": "zeal/get_configuration", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["entry_id"] == entry.entry_id
    assert response["result"]["catalog"]["areas"]

    await client.send_json_auto_id(
        {"type": "zeal/export_configuration", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["format"] == "zeal-configuration"

    await client.send_json_auto_id(
        {"type": "zeal/get_audit_log", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["maximum_entries"] == AUDIT_MAX_ENTRIES


async def test_admin_websocket_applies_and_clears_quick_change(hass, hass_ws_client):
    area, _, _, _, zone = create_registry_fixture(hass)
    entry = await setup_loaded_entry(hass, [zone])
    snapshot = configuration_snapshot(hass, entry.entry_id)
    schedule = ScheduleConfiguration(
        rooms={area.id: monday_schedule(area.id, temperature=20)},
        temperature_unit="°C",
    )
    await async_save_schedule(
        hass,
        entry.entry_id,
        schedule,
        expected_revision=snapshot["revision"],
    )
    saved_schedule = hass.data[DOMAIN][entry.entry_id][
        "schedule_runtime"
    ].configuration
    client = await hass_ws_client()
    await client.send_json_auto_id(
        {
            "type": "zeal/set_temporary_override",
            "entry_id": entry.entry_id,
            "room_ids": [area.id],
            "duration": "2h",
            "operation": "temperature",
            "value": 22,
        }
    )
    response = await client.receive_json()
    assert response["success"] is True
    room_state = response["result"]["rooms"][0]
    assert room_state["effective_temperature"] == 22
    assert room_state["override"]["duration"] == "2h"
    assert room_state["scheduled_period_started_at"] is not None
    assert (
        hass.data[DOMAIN][entry.entry_id]["schedule_runtime"].configuration
        == saved_schedule
    )

    await client.send_json_auto_id(
        {
            "type": "zeal/clear_temporary_override",
            "entry_id": entry.entry_id,
            "room_id": area.id,
        }
    )
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["rooms"][0]["override"] is None
    assert (
        hass.data[DOMAIN][entry.entry_id]["schedule_runtime"].configuration
        == saved_schedule
    )
    audit_entries = hass.data[DOMAIN][entry.entry_id]["audit_log"].export()[
        "entries"
    ]
    assert audit_entries
    assert {entry["cause"] for entry in audit_entries} >= {
        "temporary_override_applied",
        "temporary_override_cleared",
    }


async def test_admin_websocket_accepts_learning_proposal_with_revision_check(
    hass, hass_ws_client
):
    area, _, _, _, zone = create_registry_fixture(hass)
    entry = await setup_loaded_entry(hass, [zone])
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_LEARNING_ENABLED: True}
    )
    await hass.async_block_till_done()
    snapshot = configuration_snapshot(hass, entry.entry_id)
    await async_save_schedule(
        hass,
        entry.entry_id,
        ScheduleConfiguration(
            rooms={area.id: monday_schedule(area.id, temperature=20)},
            temperature_unit="°C",
        ),
        expected_revision=snapshot["revision"],
    )
    learning = hass.data[DOMAIN][entry.entry_id]["schedule_learning"]
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=10, second=0, microsecond=0
    )
    proposal = None
    for offset in (0, 7, 14):
        proposal = await learning.async_record_change(
            room_id=area.id,
            requested_temperature=21.5,
            source="home_assistant",
            when=monday - timedelta(days=14 - offset),
        )
    assert proposal is not None

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {"type": "zeal/get_learning", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["evidence_threshold"] == 3
    assert response["result"]["observation_days"] == 21
    assert response["result"]["proposals"][0]["status"] == "new"

    await client.send_json_auto_id(
        {
            "type": "zeal/decide_learning_proposal",
            "entry_id": entry.entry_id,
            "proposal_id": proposal["proposal_id"],
            "action": "accept",
        }
    )
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["proposals"][0]["status"] == "accepted"
    saved = hass.data[DOMAIN][entry.entry_id]["schedule_runtime"].configuration
    assert saved.rooms[area.id].days["monday"][0].temperature == 21.5

    # An unrelated panel preference changes the global optimistic revision but
    # must not prevent reverting the exact accepted schedule period.
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_SHOW_IN_SIDEBAR: not entry.options.get(CONF_SHOW_IN_SIDEBAR, True),
        },
    )
    await hass.async_block_till_done()
    await client.send_json_auto_id(
        {
            "type": "zeal/decide_learning_proposal",
            "entry_id": entry.entry_id,
            "proposal_id": proposal["proposal_id"],
            "action": "revert",
        }
    )
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["proposals"][0]["status"] == "reverted"
    saved = hass.data[DOMAIN][entry.entry_id]["schedule_runtime"].configuration
    assert saved.rooms[area.id].days["monday"][0].temperature == 20


async def test_standard_user_gets_overview_and_only_enabled_controls(
    hass, hass_ws_client, hass_read_only_access_token
):
    _, _, _, _, zone = create_registry_fixture(hass)
    entry = await setup_loaded_entry(hass, [zone])
    client = await hass_ws_client(access_token=hass_read_only_access_token)
    await client.send_json_auto_id(
        {"type": "zeal/get_configuration", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"][CONF_STANDARD_USER_SCHEDULE] is False
    assert response["result"][CONF_STANDARD_USER_QUICK_CHANGE] is False

    await client.send_json_auto_id(
        {"type": "zeal/get_quick_change", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_STANDARD_USER_QUICK_CHANGE: True},
    )
    await client.send_json_auto_id(
        {"type": "zeal/get_quick_change", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()
    assert response["success"] is True

    await client.send_json_auto_id(
        {
            "type": "zeal/save_hierarchy",
            "entry_id": entry.entry_id,
            "expected_revision": configuration_snapshot(hass, entry.entry_id)["revision"],
            "zones": [zone],
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"


async def test_websocket_rejects_stale_and_malformed_schedule_writes(
    hass, hass_ws_client
):
    area, _, _, _, zone = create_registry_fixture(hass)
    entry = await setup_loaded_entry(hass, [zone])
    client = await hass_ws_client()
    snapshot = configuration_snapshot(hass, entry.entry_id)
    bad_days = {day: [] for day in WEEKDAYS}
    bad_days["monday"] = [
        {
            "id": "bad",
            "friendly_name": "bad",
            "name": "Bad",
            "time": "25:00",
            "temperature": 20,
        }
    ]
    await client.send_json_auto_id(
        {
            "type": "zeal/update_room_days",
            "entry_id": entry.entry_id,
            "expected_revision": snapshot["revision"],
            "room_id": area.id,
            "days": bad_days,
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_format"

    valid_days = {day: [] for day in WEEKDAYS}
    await client.send_json_auto_id(
        {
            "type": "zeal/update_room_days",
            "entry_id": entry.entry_id,
            "expected_revision": "stale-revision",
            "room_id": area.id,
            "days": valid_days,
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "conflict"


async def test_websocket_saves_editor_days_and_copies_only_the_schedule(
    hass, hass_ws_client
):
    """The Block 6 editor can save and copy without changing room identity."""
    area, _, _, _, zone = create_registry_fixture(hass)
    bedroom = ar.async_get(hass).async_create("Bedroom")
    registry = er.async_get(hass)
    bedroom_trv = registry.async_get_or_create(
        "climate",
        "test",
        "bedroom_trv",
        suggested_object_id="bedroom_trv",
        original_name="Bedroom TRV",
    )
    bedroom_sensor = registry.async_get_or_create(
        "sensor",
        "test",
        "bedroom_temperature",
        suggested_object_id="bedroom_temperature",
        original_name="Bedroom Temperature",
        original_device_class="temperature",
    )
    registry.async_update_entity(bedroom_trv.entity_id, area_id=bedroom.id)
    registry.async_update_entity(bedroom_sensor.entity_id, area_id=bedroom.id)
    bedroom_switch = registry.async_get_or_create(
        "switch",
        "test",
        "bedroom_heating_switch",
        suggested_object_id="bedroom_heating_switch",
        original_name="Bedroom Heating Switch",
    )
    second_zone = {
        ZONE_ID: "first_floor",
        ZONE_NAME: "First Floor",
        ZONE_SWITCH: bedroom_switch.entity_id,
        "heat_source": "ashp",
        "reenable_delay": 300,
        ZONE_ROOMS: [
            {
                ROOM_ID: bedroom.id,
                ROOM_NAME: bedroom.name,
                ROOM_TRVS: [bedroom_trv.entity_id],
                ROOM_SENSORS: [bedroom_sensor.entity_id],
                ROOM_ACTIVE: True,
            }
        ],
    }
    entry = await setup_loaded_entry(hass, [zone, second_zone])
    client = await hass_ws_client()
    snapshot = configuration_snapshot(hass, entry.entry_id)
    edited_days = {day: [] for day in WEEKDAYS}
    edited_days["monday"] = [
        {
            "id": "wake",
            "friendly_name": "wake",
            "name": "Wake",
            "time": "06:15",
            "temperature": 20.5,
        },
        {
            "id": "night",
            "friendly_name": "night",
            "name": "Night",
            "time": "22:45",
            "temperature": 17,
        },
    ]

    await client.send_json_auto_id(
        {
            "type": "zeal/update_room_days",
            "entry_id": entry.entry_id,
            "expected_revision": snapshot["revision"],
            "room_id": area.id,
            "days": edited_days,
        }
    )
    response = await client.receive_json()
    assert response["success"] is True
    saved = response["result"]
    assert saved["revision"] != snapshot["revision"]
    assert saved["schedule"]["rooms"][area.id]["days"]["monday"] == edited_days["monday"]
    assert [saved_zone[ZONE_ID] for saved_zone in saved["zones"]] == [
        "ground_floor",
        "first_floor",
    ]
    assert set(saved["schedule"]["rooms"]) == {area.id, bedroom.id}

    await client.send_json_auto_id(
        {
            "type": "zeal/copy_room_schedule",
            "entry_id": entry.entry_id,
            "expected_revision": saved["revision"],
            "source_room_id": area.id,
            "target_room_ids": [bedroom.id],
            "source_days": edited_days,
        }
    )
    response = await client.receive_json()
    assert response["success"] is True
    copied = response["result"]
    source_schedule = copied["schedule"]["rooms"][area.id]
    destination_schedule = copied["schedule"]["rooms"][bedroom.id]
    assert destination_schedule["days"] == source_schedule["days"]
    assert destination_schedule["room_id"] == bedroom.id
    assert destination_schedule["room_name"] == bedroom.name
    bedroom_configuration = copied["zones"][1][ZONE_ROOMS][0]
    assert bedroom_configuration[ROOM_TRVS] == [bedroom_trv.entity_id]
    assert bedroom_configuration[ROOM_SENSORS] == [bedroom_sensor.entity_id]


async def test_websocket_saves_calendar_and_date_range_away_modes_across_reload(
    hass, hass_ws_client
):
    """Both mutually-exclusive activation choices use persisted schedule settings."""
    _, _, _, _, zone = create_registry_fixture(hass)
    calendar = er.async_get(hass).async_get_or_create(
        "calendar",
        "test",
        "holidays",
        suggested_object_id="holidays",
        original_name="Holidays",
    )
    hass.states.async_set(calendar.entity_id, "on")
    entry = await setup_loaded_entry(hass, [zone])
    client = await hass_ws_client()
    snapshot = configuration_snapshot(hass, entry.entry_id)

    await client.send_json_auto_id(
        {
            "type": "zeal/save_away_mode",
            "entry_id": entry.entry_id,
            "expected_revision": snapshot["revision"],
            "mode": "calendar",
            "calendar_entity_id": calendar.entity_id,
            "starts_at": None,
            "ends_at": None,
            "temperature": 12,
        }
    )
    response = await client.receive_json()
    assert response["success"] is True
    saved = response["result"]
    assert saved["away_mode"]["mode"] == "calendar"
    assert saved["away_mode"]["active"] is True
    assert saved["quick_change"]["away_mode"]["active"] is True

    await client.send_json_auto_id(
        {
            "type": "zeal/save_away_mode",
            "entry_id": entry.entry_id,
            "expected_revision": saved["revision"],
            "mode": "date_range",
            "calendar_entity_id": calendar.entity_id,
            # datetime-local browser values are interpreted in HA's time zone.
            "starts_at": "2030-08-24T13:00",
            "ends_at": "2030-08-30T11:30",
            "temperature": 11.5,
        }
    )
    response = await client.receive_json()
    assert response["success"] is True
    date_saved = response["result"]
    assert date_saved["away_mode"]["mode"] == "date_range"
    assert date_saved["away_mode"]["status"] == "scheduled"
    assert date_saved["schedule"]["settings"]["away_mode"]["temperature"] == 11.5

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = configuration_snapshot(hass, entry.entry_id)
    assert reloaded["away_mode"]["mode"] == "date_range"
    assert reloaded["away_mode"]["starts_at"] == date_saved["away_mode"][
        "starts_at"
    ]
    assert reloaded["away_mode"]["starts_at"].endswith("+00:00")
    assert reloaded["away_mode"]["temperature"] == 11.5
    assert export_configuration(hass, entry.entry_id)["away_mode"] == reloaded[
        "away_mode"
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "mode": "calendar",
            "calendar_entity_id": "calendar.missing",
            "starts_at": None,
            "ends_at": None,
            "temperature": 12,
        },
        {
            "mode": "date_range",
            "calendar_entity_id": None,
            "starts_at": "2026-08-26T10:00:00+00:00",
            "ends_at": "2026-08-25T10:00:00+00:00",
            "temperature": 12,
        },
        {
            "mode": "date_range",
            "calendar_entity_id": None,
            "starts_at": "2026-08-26T10:03:00+00:00",
            "ends_at": "2026-08-26T11:00:00+00:00",
            "temperature": 12,
        },
        {
            "mode": "off",
            "calendar_entity_id": None,
            "starts_at": None,
            "ends_at": None,
            "temperature": 31,
        },
    ],
)
async def test_websocket_rejects_invalid_or_unsafe_away_settings(
    hass, hass_ws_client, payload
):
    _, _, _, _, zone = create_registry_fixture(hass)
    entry = await setup_loaded_entry(hass, [zone])
    client = await hass_ws_client()
    snapshot = configuration_snapshot(hass, entry.entry_id)
    await client.send_json_auto_id(
        {
            "type": "zeal/save_away_mode",
            "entry_id": entry.entry_id,
            "expected_revision": snapshot["revision"],
            **payload,
        }
    )
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "invalid_format"
