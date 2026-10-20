"""Persistent per-room occupancy stay state.

Configuration belongs to the room hierarchy. This separate Store contains
only live stay state so calendar/manual changes and guest comfort offsets do
not rewrite the user's weekly schedule or its optimistic-lock revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from homeassistant.util import dt as dt_util

from ..const import OCCUPANCY_STORAGE_KEY_FMT, OCCUPANCY_STORAGE_VERSION


@dataclass(slots=True)
class RoomOccupancyState:
    """Restart-safe state for one room's current occupancy stay."""

    occupied: bool = True
    manual_override: bool | None = None
    comfort_offset: float = 0.0
    stay_started_at: datetime | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RoomOccupancyState":
        occupied = value.get("occupied", True)
        manual_override = value.get("manual_override")
        offset = value.get("comfort_offset", 0.0)
        if not isinstance(occupied, bool):
            occupied = True
        if manual_override is not None and not isinstance(manual_override, bool):
            manual_override = None
        if isinstance(offset, bool) or not isinstance(offset, (int, float)):
            offset = 0.0
        raw_started = value.get("stay_started_at")
        started = (
            dt_util.parse_datetime(raw_started)
            if isinstance(raw_started, str)
            else None
        )
        return cls(occupied, manual_override, float(offset), started)

    def to_dict(self) -> dict[str, Any]:
        return {
            "occupied": self.occupied,
            "manual_override": self.manual_override,
            "comfort_offset": self.comfort_offset,
            "stay_started_at": (
                self.stay_started_at.isoformat() if self.stay_started_at else None
            ),
        }


class OccupancyStorage:
    """Small versioned Store adapter for occupancy stay state."""

    def __init__(self, hass, entry_id: str, store: Any | None = None) -> None:
        if store is None:
            from homeassistant.helpers.storage import Store

            store = Store[dict[str, object]](
                hass,
                OCCUPANCY_STORAGE_VERSION,
                OCCUPANCY_STORAGE_KEY_FMT.format(entry_id=entry_id),
            )
        self._store = store

    async def async_load(self) -> dict[str, RoomOccupancyState]:
        raw = await self._store.async_load() or {}
        if not isinstance(raw, Mapping) or raw.get("version", 1) != 1:
            return {}
        rooms = raw.get("rooms", {})
        if not isinstance(rooms, Mapping):
            return {}
        return {
            str(room_id): RoomOccupancyState.from_dict(value)
            for room_id, value in rooms.items()
            if isinstance(value, Mapping)
        }

    async def async_save(self, states: Mapping[str, RoomOccupancyState]) -> None:
        await self._store.async_save(
            {
                "version": 1,
                "rooms": {
                    room_id: state.to_dict() for room_id, state in states.items()
                },
            }
        )
