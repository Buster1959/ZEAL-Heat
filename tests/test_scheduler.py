"""Pure ZEAL scheduler model, engine, editor, override and storage tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from custom_components.zeal.scheduler.away import (
    AwayModeConfiguration,
    with_away_mode,
)
from custom_components.zeal.scheduler.editor import (
    copy_room_schedule,
    update_room_days,
)
from custom_components.zeal.scheduler.engine import (
    active_period_at,
    next_transition_after,
)
from custom_components.zeal.scheduler.models import (
    SCHEMA_VERSION,
    WEEKDAYS,
    RoomSchedule,
    ScheduleConfiguration,
    SchedulePeriod,
)
from custom_components.zeal.scheduler.overrides import create_temporary_overrides
from custom_components.zeal.scheduler.rooms import reconcile_room_schedules
from custom_components.zeal.scheduler.storage import ScheduleStorage


def period(
    period_id: str = "morning", at: str = "07:00", temperature: float = 20
) -> SchedulePeriod:
    return SchedulePeriod(period_id, period_id, period_id.title(), at, temperature)


def days(**changes) -> dict[str, tuple[SchedulePeriod, ...]]:
    result = {day: () for day in WEEKDAYS}
    result.update(changes)
    return result


def room(
    room_id: str = "living_room",
    room_name: str = "Living Room",
    room_days=None,
) -> RoomSchedule:
    return RoomSchedule(room_id, room_name, room_days or days())


class MemoryStore:
    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


def test_configuration_round_trip_is_json_safe_and_detached():
    original = ScheduleConfiguration(
        rooms={
            "living_room": room(
                room_days=days(
                    monday=(period(), period("night", "22:30", 17)),
                )
            )
        },
        settings={"enabled": True, "nested": {"value": 1}},
        temperature_unit="°C",
    )
    restored = ScheduleConfiguration.from_dict(
        json.loads(json.dumps(original.to_dict()))
    )
    assert restored == original
    exported = restored.to_dict()
    exported["settings"]["nested"]["value"] = 99
    assert restored.settings["nested"]["value"] == 1


def test_room_schedule_has_no_physical_climate_entity_contract():
    exported = room().to_dict()
    assert set(exported) == {"room_id", "room_name", "days"}
    assert "climate_entity_id" not in json.dumps(exported)
    assert "trv" not in json.dumps(exported).lower()


def test_pre_versioned_document_migrates_to_v1():
    migrated = ScheduleConfiguration.from_dict({"rooms": {}, "settings": {}})
    assert migrated.version == SCHEMA_VERSION == 1
    assert migrated.temperature_unit is None


@pytest.mark.parametrize("bad_version", [True, "1", 2, -1])
def test_invalid_or_future_versions_are_rejected(bad_version):
    with pytest.raises(ValueError):
        ScheduleConfiguration.from_dict(
            {"version": bad_version, "rooms": {}, "settings": {}}
        )


@pytest.mark.parametrize("bad_time", ["7:00", "24:00", "12:60", "noon"])
def test_period_requires_exact_24_hour_time(bad_time):
    with pytest.raises(ValueError):
        period(at=bad_time)


def test_periods_must_be_ordered_and_have_unique_times_and_ids():
    with pytest.raises(ValueError, match="ordered"):
        room(room_days=days(monday=(period("late", "20:00"), period("early", "07:00"))))
    with pytest.raises(ValueError, match="duplicate period times"):
        room(room_days=days(monday=(period("a", "07:00"), period("b", "07:00"))))
    with pytest.raises(ValueError, match="duplicate period ids"):
        room(room_days=days(monday=(period("same", "07:00"), period("same", "08:00"))))


@pytest.mark.parametrize("temperature", [0.9, 30.1, 95])
def test_schedule_temperature_must_be_within_zeal_safety_range(temperature):
    with pytest.raises(ValueError, match="between 1.0 and 30.0"):
        period(temperature=temperature)


def test_schedule_accepts_low_frost_target_for_compatible_thermostats():
    assert period(temperature=1.5).temperature == 1.5


def test_active_period_selects_latest_change_on_same_day():
    scheduled = room(
        room_days=days(
            monday=(period("morning", "07:00", 20), period("night", "22:00", 17))
        )
    )
    active = active_period_at(scheduled, datetime(2026, 8, 24, 22, 0))
    assert active is not None
    assert active.period.id == "night"
    assert active.period.temperature == 17


def test_active_period_carries_across_midnight_and_empty_days():
    scheduled = room(
        room_days=days(monday=(period("night", "22:00", 17),))
    )
    active = active_period_at(scheduled, datetime(2026, 8, 26, 12, 0))
    assert active is not None
    assert active.day == "monday"
    assert active.period.temperature == 17


def test_engine_preserves_timezone_and_finds_next_change():
    tz = timezone(timedelta(hours=1))
    scheduled = room(
        room_days=days(
            monday=(period("morning", "07:00", 20), period("night", "22:00", 17))
        )
    )
    transition = next_transition_after(
        scheduled, datetime(2026, 8, 24, 12, 0, tzinfo=tz)
    )
    assert transition is not None
    assert transition.period.id == "night"
    assert transition.starts_at == datetime(2026, 8, 24, 22, 0, tzinfo=tz)


def test_empty_schedule_has_no_active_or_next_period():
    scheduled = room()
    now = datetime(2026, 8, 24, 12, 0)
    assert active_period_at(scheduled, now) is None
    assert next_transition_after(scheduled, now) is None


def test_update_room_days_preserves_room_identity_and_other_rooms():
    configuration = ScheduleConfiguration(
        rooms={"living_room": room(), "bedroom": room("bedroom", "Bedroom")}
    )
    raw_days = {
        day: ([period().to_dict()] if day == "monday" else []) for day in WEEKDAYS
    }
    updated = update_room_days(configuration, "living_room", raw_days)
    assert updated.rooms["living_room"].room_name == "Living Room"
    assert updated.rooms["living_room"].days["monday"][0].temperature == 20
    assert updated.rooms["bedroom"] == configuration.rooms["bedroom"]


def test_copy_schedule_changes_only_destination_days():
    source = room(
        room_days=days(monday=(period("morning", "07:00", 20),))
    )
    destination = room("bedroom", "Bedroom")
    configuration = ScheduleConfiguration(
        rooms={source.room_id: source, destination.room_id: destination}
    )
    copied = copy_room_schedule(
        configuration,
        source.room_id,
        [destination.room_id],
        source.to_dict()["days"],
    )
    assert copied.rooms["bedroom"].room_id == "bedroom"
    assert copied.rooms["bedroom"].room_name == "Bedroom"
    assert copied.rooms["bedroom"].days == source.days
    assert copied.rooms["bedroom"].days is not source.days


def test_copy_rejects_self_duplicate_empty_and_unknown_targets():
    configuration = ScheduleConfiguration(rooms={"living_room": room()})
    source_days = configuration.rooms["living_room"].to_dict()["days"]
    for targets in ([], ["living_room"], ["missing"], ["missing", "missing"]):
        with pytest.raises((ValueError, KeyError)):
            copy_room_schedule(configuration, "living_room", targets, source_days)


def test_reconcile_uses_stable_ids_preserves_days_and_updates_names():
    scheduled = room(room_days=days(monday=(period(),)))
    configuration = ScheduleConfiguration(rooms={scheduled.room_id: scheduled})
    reconciled = reconcile_room_schedules(
        configuration,
        {"living_room": "Main Lounge", "new_room": "New Room"},
    )
    assert set(reconciled.rooms) == {"living_room", "new_room"}
    assert reconciled.rooms["living_room"].room_name == "Main Lounge"
    assert reconciled.rooms["living_room"].days["monday"] == scheduled.days["monday"]
    assert all(not periods for periods in reconciled.rooms["new_room"].days.values())


def test_reconcile_removes_deleted_room_ids():
    configuration = ScheduleConfiguration(
        rooms={"living_room": room(), "old_room": room("old_room", "Old Room")}
    )
    reconciled = reconcile_room_schedules(
        configuration, {"living_room": "Living Room"}
    )
    assert set(reconciled.rooms) == {"living_room"}


def test_delta_override_uses_active_target_and_does_not_edit_schedule():
    scheduled = room(
        room_days=days(monday=(period("morning", "07:00", 19),))
    )
    configuration = ScheduleConfiguration(rooms={scheduled.room_id: scheduled})
    now = datetime(2026, 8, 24, 12, 0)
    overrides = create_temporary_overrides(
        configuration,
        [scheduled.room_id],
        now=now,
        duration="2h",
        operation="delta",
        value=2,
    )
    assert overrides[0].temperature == 21
    assert overrides[0].expires_at == now + timedelta(hours=2)
    assert configuration.rooms[scheduled.room_id].days["monday"][0].temperature == 19


def test_exact_override_until_next_change():
    scheduled = room(
        room_days=days(
            monday=(period("morning", "07:00", 19), period("night", "22:00", 17))
        )
    )
    configuration = ScheduleConfiguration(rooms={scheduled.room_id: scheduled})
    overrides = create_temporary_overrides(
        configuration,
        [scheduled.room_id],
        now=datetime(2026, 8, 24, 12, 0),
        duration="next_change",
        operation="temperature",
        value=21,
    )
    assert overrides[0].temperature == 21
    assert overrides[0].expires_at == datetime(2026, 8, 24, 22, 0)


def test_temporary_override_cannot_exceed_zeal_safety_range():
    scheduled = room(
        room_days=days(monday=(period("morning", "07:00", 30),))
    )
    configuration = ScheduleConfiguration(rooms={scheduled.room_id: scheduled})
    with pytest.raises(ValueError, match="between 1.0 and 30.0"):
        create_temporary_overrides(
            configuration,
            [scheduled.room_id],
            now=datetime(2026, 8, 24, 12, 0),
            duration="2h",
            operation="delta",
            value=1,
        )


def test_away_modes_are_mutually_exclusive_and_timezone_aware():
    calendar = AwayModeConfiguration(
        mode="calendar",
        calendar_entity_id="calendar.holidays",
        temperature=12,
    )
    assert calendar.active_at(
        datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        calendar_is_on=True,
    )
    assert not calendar.active_at(
        datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        calendar_is_on=False,
    )

    date_range = AwayModeConfiguration(
        mode="date_range",
        starts_at="2026-08-24T13:00:00+00:00",
        ends_at="2026-08-26T11:30:00+00:00",
        temperature=11.5,
    )
    assert not date_range.active_at(
        datetime(2026, 8, 24, 12, 59, tzinfo=timezone.utc)
    )
    assert date_range.active_at(
        datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc)
    )
    assert not date_range.active_at(
        datetime(2026, 8, 26, 11, 30, tzinfo=timezone.utc)
    )


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"mode": "calendar"}, "Choose a Home Assistant calendar"),
        (
            {
                "mode": "date_range",
                "starts_at": "2026-08-25T10:00:00+00:00",
                "ends_at": "2026-08-24T10:00:00+00:00",
            },
            "later than",
        ),
        (
            {
                "mode": "date_range",
                "starts_at": "2026-08-24T10:00:00",
                "ends_at": "2026-08-25T10:00:00",
            },
            "timezone",
        ),
        ({"mode": "off", "temperature": 31}, "between 1.0 and 30.0"),
    ],
)
def test_away_configuration_rejects_ambiguous_or_unsafe_values(kwargs, match):
    with pytest.raises(ValueError, match=match):
        AwayModeConfiguration(**kwargs)


def test_away_settings_round_trip_without_editing_room_schedules():
    configuration = ScheduleConfiguration(
        rooms={"living_room": room(room_days=days(monday=(period(),)))},
        settings={"preserved": True},
        temperature_unit="°C",
    )
    away = AwayModeConfiguration(
        mode="date_range",
        starts_at="2026-08-24T13:00:00+00:00",
        ends_at="2026-08-26T11:30:00+00:00",
        temperature=12,
    )
    updated = with_away_mode(configuration, away)
    loaded = ScheduleConfiguration.from_dict(updated.to_dict())
    assert AwayModeConfiguration.from_settings(loaded.settings) == away
    assert loaded.settings["preserved"] is True
    assert loaded.rooms == configuration.rooms


async def test_storage_empty_load_save_and_reload():
    store = MemoryStore()
    adapter = ScheduleStorage(None, "entry-id", store=store)
    assert await adapter.async_load() == ScheduleConfiguration.empty()
    configuration = ScheduleConfiguration(
        rooms={"living_room": room()}, temperature_unit="°C"
    )
    await adapter.async_save(configuration)
    assert store.data == configuration.to_dict()
    assert await adapter.async_load() == configuration


async def test_storage_migrates_pre_versioned_document_on_load():
    adapter = ScheduleStorage(
        None,
        "entry-id",
        store=MemoryStore({"rooms": {}, "settings": {"enabled": True}}),
    )
    loaded = await adapter.async_load()
    assert loaded.version == SCHEMA_VERSION
    assert loaded.settings == {"enabled": True}
