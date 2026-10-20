"""ZEAL scheduler runtime and canonical room-boundary tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.zeal as zeal_integration
from custom_components.zeal.const import (
    CONF_ZONES,
    DOMAIN,
    ROOM_ACTIVE,
    ROOM_ID,
    ROOM_NAME,
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
from custom_components.zeal.coordinator import ZealCoordinator
from custom_components.zeal.scheduler.away import AwayModeConfiguration, with_away_mode
from custom_components.zeal.scheduler.models import (
    WEEKDAYS,
    RoomSchedule,
    ScheduleConfiguration,
    SchedulePeriod,
)
from custom_components.zeal.scheduler.runtime import ScheduleRuntime
from custom_components.zeal.scheduler.occupancy import OccupancyStorage
from homeassistant.helpers.storage import Store


def schedule_room(
    room_id: str,
    room_name: str,
    monday_periods: tuple[SchedulePeriod, ...],
) -> RoomSchedule:
    return RoomSchedule(
        room_id,
        room_name,
        {
            day: monday_periods if day == "monday" else ()
            for day in WEEKDAYS
        },
    )


def schedule_configuration(
    *rooms: RoomSchedule,
    away_mode: AwayModeConfiguration | None = None,
) -> ScheduleConfiguration:
    configuration = ScheduleConfiguration(
        rooms={room.room_id: room for room in rooms}
    )
    return with_away_mode(configuration, away_mode) if away_mode else configuration


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, str]] = []
        self.available: set[str] = set()
        self.listener = None
        self.listener_removed = False
        self.refreshes = 0
        self.room_thermostats = {}

    def async_add_listener(self, listener):
        self.listener = listener

        def unsubscribe():
            self.listener_removed = True

        return unsubscribe

    async def async_set_room_target(self, room_id, temperature, *, source):
        self.calls.append((room_id, temperature, source))
        applied = room_id in self.available
        thermostat = self.room_thermostats.get(room_id)
        if applied and thermostat is not None:
            thermostat.target_temperature = temperature
        return applied

    async def async_request_refresh(self):
        self.refreshes += 1


class FakeCanonicalThermostat:
    entity_id = "climate.zeal_living_room"

    def __init__(self) -> None:
        self.target_temperature = 20.0
        self.hvac_mode = "heat"
        self.applied: list[tuple[float, str]] = []

    def apply_target_setpoint(self, temperature, *, source):
        self.target_temperature = temperature
        self.applied.append((temperature, source))


class MemoryStore:
    def __init__(self, data=None) -> None:
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


def configure_occupancy_room(
    coordinator: FakeCoordinator,
    *,
    source: str = "manual",
    calendar_entity_id: str | None = None,
    enabled: bool = True,
) -> None:
    coordinator.zones = [
        {
            ZONE_ROOMS: [
                {
                    ROOM_ID: "living_room",
                    ROOM_ACTIVE: True,
                    ROOM_OCCUPANCY_ENABLED: enabled,
                    ROOM_UNOCCUPIED_TEMPERATURE: 12.0,
                    ROOM_OCCUPANCY_SOURCE: source,
                    ROOM_OCCUPANCY_CALENDAR: calendar_entity_id,
                }
            ]
        }
    ]


def install_timer_capture(monkeypatch):
    scheduled: list[tuple[datetime, object]] = []
    cancellations: list[datetime] = []

    def fake_track(_hass, callback, when):
        scheduled.append((when, callback))

        def cancel():
            cancellations.append(when)

        return cancel

    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.event_helper.async_track_point_in_time",
        fake_track,
    )
    return scheduled, cancellations


async def test_startup_reconciles_only_stable_zeal_room_ids(hass, monkeypatch):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room", "bedroom"}
    configuration = schedule_configuration(
        schedule_room(
            "living_room",
            "Living Room",
            (SchedulePeriod("morning", "morning", "Morning", "07:00", 20),),
        ),
        schedule_room(
            "bedroom",
            "Bedroom",
            (SchedulePeriod("morning", "morning", "Morning", "08:00", 18),),
        ),
    )
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(configuration)

    assert coordinator.calls == [
        ("living_room", 20.0, "startup_reconciliation"),
        ("bedroom", 18.0, "startup_reconciliation"),
    ]
    assert all(not room_id.startswith("climate.") for room_id, _, _ in coordinator.calls)
    assert coordinator.refreshes == 1


async def test_quick_change_state_reports_active_schedule_start(hass, monkeypatch):
    """Overview can distinguish the active schedule from later overrides."""
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (SchedulePeriod("morning", "morning", "Morning", "07:00", 20),),
            )
        )
    )

    room = runtime.quick_change_state()["rooms"][0]
    assert room["scheduled_temperature"] == 20
    assert room["scheduled_period_started_at"] == "2026-08-24T07:00:00"


async def test_runtime_never_calls_home_assistant_climate_service_directly(
    hass, monkeypatch
):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    service_calls = []

    async def capture_service(call):
        service_calls.append(call)

    hass.services.async_register("climate", "set_temperature", capture_service)
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (SchedulePeriod("morning", "morning", "Morning", "07:00", 20),),
            )
        )
    )
    assert service_calls == []
    assert coordinator.calls[0][0] == "living_room"


async def test_missing_thermostat_is_not_marked_applied_and_retries(
    hass, monkeypatch
):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (SchedulePeriod("morning", "morning", "Morning", "07:00", 20),),
            )
        )
    )
    assert len(coordinator.calls) == 1
    assert coordinator.refreshes == 0

    coordinator.available.add("living_room")
    coordinator.listener()
    await hass.async_block_till_done()
    assert len(coordinator.calls) == 2
    assert coordinator.calls[-1] == (
        "living_room",
        20.0,
        "availability_reconciliation",
    )
    assert coordinator.refreshes == 1

    coordinator.listener()
    await hass.async_block_till_done()
    assert len(coordinator.calls) == 2


async def test_runtime_schedules_the_nearest_room_transition(hass, monkeypatch):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    scheduled, _ = install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room", "bedroom"}
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (
                    SchedulePeriod("morning", "morning", "Morning", "07:00", 20),
                    SchedulePeriod("evening", "evening", "Evening", "18:00", 21),
                ),
            ),
            schedule_room(
                "bedroom",
                "Bedroom",
                (
                    SchedulePeriod("morning", "morning", "Morning", "08:00", 18),
                    SchedulePeriod("night", "night", "Night", "22:00", 16),
                ),
            ),
        )
    )
    assert scheduled[-1][0] == datetime(2026, 8, 24, 18, 0)


async def test_transition_applies_changed_period_and_reschedules(hass, monkeypatch):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    scheduled, _ = install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (
                    SchedulePeriod("morning", "morning", "Morning", "07:00", 20),
                    SchedulePeriod("evening", "evening", "Evening", "18:00", 21),
                ),
            )
        )
    )
    callback = scheduled[-1][1]
    callback(datetime(2026, 8, 24, 18, 0))
    await hass.async_block_till_done()
    assert coordinator.calls[-1] == (
        "living_room",
        21.0,
        "scheduled_transition",
    )


async def test_calendar_away_overrides_only_active_rooms_at_startup(hass, monkeypatch):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room", "guest_room"}
    coordinator.zones = [
        {
            ZONE_ROOMS: [
                {ROOM_ID: "living_room", ROOM_ACTIVE: True},
                {ROOM_ID: "guest_room", ROOM_ACTIVE: False},
            ]
        }
    ]
    hass.states.async_set("calendar.holidays", "on")
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (SchedulePeriod("morning", "morning", "Morning", "07:00", 20),),
            ),
            schedule_room(
                "guest_room",
                "Guest Room",
                (SchedulePeriod("morning", "morning", "Morning", "07:00", 18),),
            ),
            away_mode=AwayModeConfiguration(
                mode="calendar",
                calendar_entity_id="calendar.holidays",
                temperature=12,
            ),
        )
    )
    assert coordinator.calls == [
        ("living_room", 12.0, "away_mode_active"),
        ("guest_room", 18.0, "startup_reconciliation"),
    ]
    assert runtime.away_mode_state(now)["active_room_ids"] == ["living_room"]


async def test_away_suppresses_holds_and_manual_changes_then_resumes_hold(
    hass, monkeypatch
):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    thermostat = FakeCanonicalThermostat()
    coordinator.room_thermostats["living_room"] = thermostat
    hass.states.async_set("calendar.holidays", "off")
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (
                    SchedulePeriod("morning", "morning", "Morning", "07:00", 20),
                    SchedulePeriod("night", "night", "Night", "22:00", 17),
                ),
            ),
            away_mode=AwayModeConfiguration(
                mode="calendar",
                calendar_entity_id="calendar.holidays",
                temperature=12,
            ),
        )
    )
    await runtime.async_set_temporary_override(
        ["living_room"], duration="4h", operation="temperature", value=22
    )
    assert coordinator.calls[-1] == (
        "living_room",
        22.0,
        "temporary_override_applied",
    )

    hass.states.async_set("calendar.holidays", "on")
    await hass.async_block_till_done()
    assert coordinator.calls[-1] == ("living_room", 12.0, "away_mode_activated")
    with pytest.raises(ValueError, match="unavailable while global Away"):
        await runtime.async_set_temporary_override(
            ["living_room"], duration="2h", operation="temperature", value=21
        )

    thermostat.target_temperature = 24
    coordinator.listener()
    await hass.async_block_till_done()
    assert coordinator.calls[-1] == ("living_room", 12.0, "away_reasserted")

    hass.states.async_set("calendar.holidays", "off")
    await hass.async_block_till_done()
    assert coordinator.calls[-1] == (
        "living_room",
        22.0,
        "temporary_override_resumed_after_away",
    )


async def test_manual_change_persists_until_next_schedule_without_higher_mode(
    hass, monkeypatch
):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    thermostat = FakeCanonicalThermostat()
    coordinator.room_thermostats["living_room"] = thermostat
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (SchedulePeriod("morning", "morning", "Morning", "07:00", 20),),
            )
        )
    )
    calls_before_manual_refresh = len(coordinator.calls)
    thermostat.target_temperature = 23
    coordinator.listener()
    await hass.async_block_till_done()
    assert len(coordinator.calls) == calls_before_manual_refresh
    assert thermostat.target_temperature == 23


async def test_date_range_activates_ends_and_reconciles_inside_range_on_restart(
    hass, monkeypatch
):
    before = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: before
    )
    scheduled, _ = install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    away_mode = AwayModeConfiguration(
        mode="date_range",
        starts_at="2026-08-24T13:00:00+00:00",
        ends_at="2026-08-24T15:00:00+00:00",
        temperature=11,
    )
    configuration = schedule_configuration(
        schedule_room(
            "living_room",
            "Living Room",
            (
                SchedulePeriod("morning", "morning", "Morning", "07:00", 20),
                SchedulePeriod("night", "night", "Night", "18:00", 17),
            ),
        ),
        away_mode=away_mode,
    )
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(configuration)
    assert scheduled[-1][0] == datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc)

    scheduled[-1][1](datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc))
    await hass.async_block_till_done()
    assert coordinator.calls[-1] == ("living_room", 11.0, "away_mode_activated")
    assert scheduled[-1][0] == datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)

    scheduled[-1][1](datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc))
    await hass.async_block_till_done()
    assert coordinator.calls[-1] == ("living_room", 20.0, "away_mode_ended")

    inside = datetime(2026, 8, 24, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: inside
    )
    restarted_coordinator = FakeCoordinator()
    restarted_coordinator.available = {"living_room"}
    restarted = ScheduleRuntime(hass, restarted_coordinator)
    await restarted.async_start(configuration)
    assert restarted_coordinator.calls[0] == (
        "living_room",
        11.0,
        "away_mode_active",
    )


async def test_manual_occupancy_setback_is_distinct_from_inactive_room(
    hass, monkeypatch
):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    configure_occupancy_room(coordinator)
    storage = MemoryStore()
    runtime = ScheduleRuntime(
        hass,
        coordinator,
        occupancy_storage=OccupancyStorage(hass, "test", storage),
    )
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (SchedulePeriod("morning", "morning", "Morning", "07:00", 20),),
            )
        )
    )

    await runtime.async_set_room_occupancy("living_room", False)
    assert coordinator.calls[-1] == (
        "living_room",
        12.0,
        "room_occupancy_ended",
    )
    state = runtime.quick_change_state()["rooms"][0]
    assert state["occupancy"]["occupied"] is False
    assert state["effective_source"] == "unoccupied_setback"

    await runtime.async_set_room_occupancy("living_room", True)
    assert coordinator.calls[-1] == (
        "living_room",
        20.0,
        "room_occupancy_started",
    )


async def test_guest_offset_uses_underlying_schedule_and_crosses_boundaries(
    hass, monkeypatch
):
    current = [datetime(2026, 8, 24, 12, 0)]
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: current[0]
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    configure_occupancy_room(coordinator)
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (
                    SchedulePeriod("morning", "morning", "Morning", "07:00", 20),
                    SchedulePeriod("evening", "evening", "Evening", "18:00", 18),
                ),
            )
        )
    )

    assert await runtime.async_handle_guest_target(
        "living_room", 22, source="guest_physical_trv_adjustment"
    )
    assert coordinator.calls[-1][1] == 22.0
    assert await runtime.async_handle_guest_target(
        "living_room", 21, source="guest_physical_trv_adjustment"
    )
    room = runtime.quick_change_state()["rooms"][0]
    assert room["occupancy"]["comfort_offset"] == 1.0

    current[0] = datetime(2026, 8, 24, 18, 0)
    await runtime._async_handle_transition(current[0])
    assert coordinator.calls[-1] == (
        "living_room",
        19.0,
        "scheduled_transition",
    )

    await runtime.async_set_room_occupancy("living_room", False)
    assert runtime.quick_change_state()["rooms"][0]["occupancy"][
        "comfort_offset"
    ] == 0.0


async def test_occupancy_stay_and_offset_survive_restart_without_target_spike(
    hass, monkeypatch
):
    current = [datetime(2026, 8, 24, 12, 0)]
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: current[0]
    )
    install_timer_capture(monkeypatch)
    storage = MemoryStore()
    configuration = schedule_configuration(
        schedule_room(
            "living_room",
            "Living Room",
            (
                SchedulePeriod("morning", "morning", "Morning", "07:00", 20),
                SchedulePeriod("evening", "evening", "Evening", "18:00", 18),
            ),
        )
    )
    first_coordinator = FakeCoordinator()
    first_coordinator.available = {"living_room"}
    configure_occupancy_room(first_coordinator)
    first = ScheduleRuntime(
        hass,
        first_coordinator,
        occupancy_storage=OccupancyStorage(hass, "test", storage),
    )
    await first.async_start(configuration)
    await first.async_handle_guest_target(
        "living_room", 22, source="guest_thermostat_adjustment"
    )
    await first.async_stop()

    current[0] = datetime(2026, 8, 24, 19, 0)
    restarted_coordinator = FakeCoordinator()
    restarted_coordinator.available = {"living_room"}
    configure_occupancy_room(restarted_coordinator)
    restarted = ScheduleRuntime(
        hass,
        restarted_coordinator,
        occupancy_storage=OccupancyStorage(hass, "test", storage),
    )
    await restarted.async_start(configuration)
    assert restarted_coordinator.calls == [
        ("living_room", 20.0, "startup_reconciliation")
    ]
    assert restarted.quick_change_state()["rooms"][0]["occupancy"][
        "comfort_offset"
    ] == 2.0


async def test_calendar_occupancy_manual_precedence_overlap_and_missing_fallback(
    hass, monkeypatch
):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    storage = MemoryStore()
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    configure_occupancy_room(
        coordinator,
        source="calendar",
        calendar_entity_id="calendar.guest_room",
    )
    hass.states.async_set("calendar.guest_room", "off")
    configuration = schedule_configuration(
        schedule_room(
            "living_room",
            "Living Room",
            (SchedulePeriod("morning", "morning", "Morning", "07:00", 20),),
        )
    )
    runtime = ScheduleRuntime(
        hass,
        coordinator,
        occupancy_storage=OccupancyStorage(hass, "test", storage),
    )
    await runtime.async_start(configuration)
    assert coordinator.calls[-1][1] == 12.0

    await runtime.async_set_room_occupancy("living_room", True)
    assert coordinator.calls[-1][1] == 20.0
    calls_before_calendar = len(coordinator.calls)
    hass.states.async_set("calendar.guest_room", "on")
    hass.states.async_set("calendar.guest_room", "off")
    await hass.async_block_till_done()
    assert len(coordinator.calls) == calls_before_calendar

    await runtime.async_set_room_occupancy("living_room", None)
    assert coordinator.calls[-1][1] == 12.0
    hass.states.async_set("calendar.guest_room", "on")
    await hass.async_block_till_done()
    assert coordinator.calls[-1][1] == 20.0
    calls_before_overlap_update = len(coordinator.calls)
    hass.states.async_set("calendar.guest_room", "on", {"message": "overlap"})
    await hass.async_block_till_done()
    assert len(coordinator.calls) == calls_before_overlap_update

    await runtime.async_handle_guest_target(
        "living_room", 22, source="guest_thermostat_adjustment"
    )
    await runtime.async_stop()
    hass.states.async_remove("calendar.guest_room")
    restarted_coordinator = FakeCoordinator()
    restarted_coordinator.available = {"living_room"}
    configure_occupancy_room(
        restarted_coordinator,
        source="calendar",
        calendar_entity_id="calendar.guest_room",
    )
    restarted = ScheduleRuntime(
        hass,
        restarted_coordinator,
        occupancy_storage=OccupancyStorage(hass, "test", storage),
    )
    await restarted.async_start(configuration)
    assert restarted_coordinator.calls[-1][1] == 22.0
    state = restarted.quick_change_state()["rooms"][0]["occupancy"]
    assert state["occupied"] is True
    assert state["calendar_available"] is False


async def test_legacy_room_keeps_manual_target_behaviour_when_occupancy_disabled(
    hass, monkeypatch
):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    configure_occupancy_room(coordinator, enabled=False)
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (SchedulePeriod("morning", "morning", "Morning", "07:00", 20),),
            )
        )
    )
    assert not await runtime.async_handle_guest_target(
        "living_room", 22, source="guest_thermostat_adjustment"
    )
    assert runtime.quick_change_state()["rooms"][0]["occupancy"] == {
        "enabled": False
    }


async def test_stop_cancels_timer_and_coordinator_listener(hass, monkeypatch):
    now = datetime(2026, 8, 24, 12, 0)
    monkeypatch.setattr(
        "custom_components.zeal.scheduler.runtime.dt_util.now", lambda: now
    )
    _, cancellations = install_timer_capture(monkeypatch)
    coordinator = FakeCoordinator()
    coordinator.available = {"living_room"}
    runtime = ScheduleRuntime(hass, coordinator)
    await runtime.async_start(
        schedule_configuration(
            schedule_room(
                "living_room",
                "Living Room",
                (
                    SchedulePeriod("morning", "morning", "Morning", "07:00", 20),
                    SchedulePeriod("night", "night", "Night", "22:00", 17),
                ),
            )
        )
    )
    await runtime.async_stop()
    assert cancellations == [datetime(2026, 8, 24, 22, 0)]
    assert coordinator.listener_removed is True


async def test_coordinator_boundary_updates_canonical_then_guarded_physical_trv(hass):
    room_data = {
        ROOM_ID: "living_room",
        ROOM_NAME: "Living Room",
        ROOM_TRVS: ["climate.physical_trv"],
        ROOM_SENSORS: ["sensor.living_room"],
        ROOM_ACTIVE: True,
    }
    zone = {
        ZONE_ID: "ground_floor",
        ZONE_NAME: "Ground Floor",
        ZONE_SWITCH: "switch.ground_floor",
        ZONE_ROOMS: [room_data],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_ZONES: [zone]})
    entry.add_to_hass(hass)
    coordinator = ZealCoordinator(
        hass, entry, Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")
    )
    thermostat = FakeCanonicalThermostat()
    coordinator.room_thermostats["living_room"] = thermostat
    hass.states.async_set("climate.physical_trv", "heat", {"temperature": 18.0})
    physical_calls = []

    async def capture_set_temperature(call):
        physical_calls.append(dict(call.data))

    hass.services.async_register("climate", "set_temperature", capture_set_temperature)
    applied = await coordinator.async_set_room_target(
        "living_room", 21.0, source="scheduled_transition"
    )
    assert applied is True
    assert thermostat.applied == [(21.0, "scheduled_transition")]
    assert physical_calls == [
        {"entity_id": "climate.physical_trv", "temperature": 21.0}
    ]


async def test_unknown_room_fails_without_any_physical_service_call(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_ZONES: []})
    entry.add_to_hass(hass)
    coordinator = ZealCoordinator(
        hass, entry, Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")
    )
    calls = []

    async def capture_set_temperature(call):
        calls.append(call)

    hass.services.async_register("climate", "set_temperature", capture_set_temperature)
    assert (
        await coordinator.async_set_room_target(
            "missing_room", 21.0, source="scheduled_transition"
        )
        is False
    )
    assert calls == []


async def test_scheduled_target_is_clamped_at_canonical_room_boundary(hass):
    room_data = {
        ROOM_ID: "living_room",
        ROOM_NAME: "Living Room",
        ROOM_TRVS: ["climate.physical_trv"],
        ROOM_SENSORS: [],
        ROOM_ACTIVE: True,
    }
    zone = {
        ZONE_ID: "ground_floor",
        ZONE_NAME: "Ground Floor",
        ZONE_SWITCH: "switch.ground_floor",
        ZONE_ROOMS: [room_data],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_ZONES: [zone]})
    entry.add_to_hass(hass)
    coordinator = ZealCoordinator(
        hass, entry, Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")
    )
    thermostat = FakeCanonicalThermostat()
    coordinator.room_thermostats["living_room"] = thermostat
    hass.states.async_set("climate.physical_trv", "heat", {"temperature": 18.0})
    written = []

    async def capture_set_temperature(call):
        written.append(call.data["temperature"])

    hass.services.async_register("climate", "set_temperature", capture_set_temperature)
    await coordinator.async_set_room_target(
        "living_room", 95.0, source="scheduled_transition"
    )
    assert thermostat.target_temperature == 30.0
    assert written == [30.0]


async def test_config_entry_setup_and_unload_own_scheduler_runtime(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ZEAL HVAC System",
        data={},
        options={CONF_ZONES: []},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    entry_data = hass.data[DOMAIN][entry.entry_id]
    assert "schedule_storage" in entry_data
    assert isinstance(entry_data["schedule_runtime"], ScheduleRuntime)
    assert (
        entry_data["schedule_runtime"].configuration
        == ScheduleConfiguration.empty().with_temperature_unit("°C")
    )

    assert await hass.config_entries.async_unload(entry.entry_id) is True
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_remove_entry_deletes_only_that_instances_private_stores(
    hass, monkeypatch
):
    removed: list[tuple[int, str]] = []

    class RemovingStore:
        def __init__(self, _hass, version, key):
            self.version = version
            self.key = key

        async def async_remove(self):
            removed.append((self.version, self.key))

    monkeypatch.setattr(zeal_integration, "Store", RemovingStore)
    entry = MockConfigEntry(domain=DOMAIN, title="Boiler ZEAL", data={}, options={})

    await zeal_integration.async_remove_entry(hass, entry)

    assert removed == [
        (1, f"zeal_{entry.entry_id}"),
        (1, f"zeal.scheduler.{entry.entry_id}"),
        (1, f"zeal.audit.{entry.entry_id}"),
        (1, f"zeal.learning.{entry.entry_id}"),
        (1, f"zeal.occupancy.{entry.entry_id}"),
    ]
    assert all("another-entry" not in key for _version, key in removed)
