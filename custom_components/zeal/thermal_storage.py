"""Bounded Home Assistant Store persistence for Room Thermal Response."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from inspect import signature
from typing import TYPE_CHECKING, Any

from .const import (
    THERMAL_CHECKPOINT_INTERVAL_SECONDS,
    THERMAL_CHECKPOINT_STORAGE_KEY_FMT,
    THERMAL_DATA_VERSION,
    THERMAL_EPISODE_RETENTION_DAYS,
    THERMAL_MAX_EPISODES_PER_ROOM,
    THERMAL_MAX_SAMPLES_PER_ROOM,
    THERMAL_MODEL_STORAGE_KEY_FMT,
    THERMAL_ROOM_STORAGE_KEY_FMT,
    THERMAL_SAMPLE_RETENTION_DAYS,
    THERMAL_STORAGE_VERSION,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


StoreFactory = Callable[[int, str, bool], Any]


class ThermalStorageVersionError(RuntimeError):
    """Raised rather than overwriting an unsupported thermal payload."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object) -> datetime | None:
    """Parse a timezone-aware ISO timestamp, rejecting unsafe values."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _bounded_records(
    values: object,
    *,
    timestamp_key: str,
    cutoff: datetime,
    maximum: int,
) -> list[dict[str, Any]]:
    """Return valid, recent, ID-deduplicated records in chronological order."""
    if not isinstance(values, list):
        return []
    by_id: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        record = dict(value)
        record_id = record.get("id")
        recorded_at = _timestamp(record.get(timestamp_key))
        if not record_id or recorded_at is None or recorded_at < cutoff:
            continue
        key = str(record_id)
        existing = by_id.get(key)
        if existing is not None:
            existing_at = _timestamp(existing.get(timestamp_key))
            if existing_at is not None and existing_at > recorded_at:
                continue
        by_id[key] = record
    ordered = sorted(
        by_id.values(), key=lambda item: _timestamp(item[timestamp_key]) or cutoff
    )
    return ordered[-maximum:]


class ThermalStorage:
    """Own Thermal Response model, checkpoint and lazy per-room Stores."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        store_factory: StoreFactory | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        if store_factory is None:
            from homeassistant.helpers.storage import Store

            def store_factory(
                version: int, key: str, serialize_in_event_loop: bool
            ) -> Any:
                kwargs = {}
                if "serialize_in_event_loop" in signature(Store).parameters:
                    kwargs["serialize_in_event_loop"] = serialize_in_event_loop
                return Store[dict[str, object]](hass, version, key, **kwargs)

        self._entry_id = entry_id
        self._store_factory = store_factory
        self._now = now
        self._models_store = store_factory(
            THERMAL_STORAGE_VERSION,
            THERMAL_MODEL_STORAGE_KEY_FMT.format(entry_id=entry_id),
            True,
        )
        self._checkpoint_store = store_factory(
            THERMAL_STORAGE_VERSION,
            THERMAL_CHECKPOINT_STORAGE_KEY_FMT.format(entry_id=entry_id),
            True,
        )
        self.models: dict[str, dict[str, Any]] = {}
        self.active: dict[str, dict[str, Any]] = {}
        self.room_ids: set[str] = set()
        self._checkpoint_write_pending = False

    def _room_store(self, room_id: str) -> Any:
        return self._store_factory(
            THERMAL_STORAGE_VERSION,
            THERMAL_ROOM_STORAGE_KEY_FMT.format(
                entry_id=self._entry_id, room_id=room_id
            ),
            False,
        )

    @staticmethod
    def _check_version(data: object, store_name: str) -> None:
        if isinstance(data, Mapping) and data.get("version") != THERMAL_DATA_VERSION:
            raise ThermalStorageVersionError(
                f"Unsupported Thermal Response {store_name} payload version: "
                f"{data.get('version')!r}"
            )

    async def async_load(self) -> None:
        """Load only small current state; room histories remain lazy."""
        models = await self._models_store.async_load()
        self._check_version(models, "model index")
        if (
            isinstance(models, Mapping)
            and models.get("version") == THERMAL_DATA_VERSION
        ):
            stored_models = models.get("models")
            stored_rooms = models.get("room_ids")
            if isinstance(stored_models, Mapping):
                self.models = {
                    str(room_id): dict(model)
                    for room_id, model in stored_models.items()
                    if isinstance(model, Mapping)
                }
            if isinstance(stored_rooms, list):
                self.room_ids = {str(room_id) for room_id in stored_rooms}
        checkpoints = await self._checkpoint_store.async_load()
        self._check_version(checkpoints, "checkpoint")
        if (
            isinstance(checkpoints, Mapping)
            and checkpoints.get("version") == THERMAL_DATA_VERSION
            and isinstance(checkpoints.get("active"), Mapping)
        ):
            self.active = {
                str(room_id): dict(checkpoint)
                for room_id, checkpoint in checkpoints["active"].items()
                if isinstance(checkpoint, Mapping)
            }
            self.room_ids.update(self.active)

    async def async_save_models(self) -> None:
        """Persist the small model index and known room-store inventory."""
        await self._models_store.async_save(
            {
                "version": THERMAL_DATA_VERSION,
                "room_ids": sorted(self.room_ids),
                "models": deepcopy(self.models),
            }
        )

    def _checkpoint_payload(self) -> dict[str, object]:
        # Store calls this at the delayed or final shutdown write. Resetting the
        # flag allows the next change to schedule one new bounded write.
        self._checkpoint_write_pending = False
        return {
            "version": THERMAL_DATA_VERSION,
            "active": deepcopy(self.active),
        }

    def schedule_checkpoint(self) -> None:
        """Schedule one coalesced write containing the newest active state."""
        if self._checkpoint_write_pending:
            return
        self._checkpoint_write_pending = True
        self._checkpoint_store.async_delay_save(
            self._checkpoint_payload, THERMAL_CHECKPOINT_INTERVAL_SECONDS
        )

    async def async_save_checkpoint(self) -> None:
        """Immediately persist an important active-episode transition."""
        self._checkpoint_write_pending = False
        await self._checkpoint_store.async_save(self._checkpoint_payload())

    async def async_set_active(
        self,
        room_id: str,
        checkpoint: Mapping[str, Any],
        *,
        important_transition: bool = False,
    ) -> None:
        """Update one active episode and checkpoint it at the safe cadence."""
        self.active[room_id] = dict(checkpoint)
        self.room_ids.add(room_id)
        if important_transition:
            await self.async_save_checkpoint()
        else:
            self.schedule_checkpoint()

    async def async_clear_active(self, room_id: str) -> None:
        """Remove a completed/abandoned active episode immediately."""
        if self.active.pop(room_id, None) is not None:
            await self.async_save_checkpoint()

    def _prune_history(self, data: object) -> dict[str, object]:
        now = self._now().astimezone(timezone.utc)
        source = data if isinstance(data, Mapping) else {}
        return {
            "version": THERMAL_DATA_VERSION,
            "samples": _bounded_records(
                source.get("samples"),
                timestamp_key="timestamp",
                cutoff=now - timedelta(days=THERMAL_SAMPLE_RETENTION_DAYS),
                maximum=THERMAL_MAX_SAMPLES_PER_ROOM,
            ),
            "episodes": _bounded_records(
                source.get("episodes"),
                timestamp_key="ended_at",
                cutoff=now - timedelta(days=THERMAL_EPISODE_RETENTION_DAYS),
                maximum=THERMAL_MAX_EPISODES_PER_ROOM,
            ),
        }

    async def async_load_room(self, room_id: str) -> dict[str, object]:
        """Load and prune one room without retaining other histories in RAM."""
        store = self._room_store(room_id)
        raw = await store.async_load()
        self._check_version(raw, "room history")
        history = self._prune_history(
            raw
            if isinstance(raw, Mapping)
            and raw.get("version") == THERMAL_DATA_VERSION
            else None
        )
        if raw != history:
            await store.async_save(history)
        return history

    async def async_save_room(
        self,
        room_id: str,
        *,
        samples: list[Mapping[str, Any]],
        episodes: list[Mapping[str, Any]],
    ) -> dict[str, object]:
        """Deduplicate, prune and commit completed evidence for one room."""
        history = self._prune_history(
            {
                "samples": [dict(sample) for sample in samples],
                "episodes": [dict(episode) for episode in episodes],
            }
        )
        self.room_ids.add(room_id)
        await self._room_store(room_id).async_save(history)
        await self.async_save_models()
        return history

    async def async_reset_room(self, room_id: str) -> None:
        """Delete only one room's thermal evidence, model and checkpoint."""
        await self._room_store(room_id).async_remove()
        self.room_ids.discard(room_id)
        self.models.pop(room_id, None)
        self.active.pop(room_id, None)
        await self.async_save_models()
        await self.async_save_checkpoint()

    async def async_reset_all(self) -> None:
        """Delete all Thermal Response data owned by this config entry."""
        for room_id in set(self.room_ids) | set(self.models) | set(self.active):
            await self._room_store(room_id).async_remove()
        self.room_ids.clear()
        self.models.clear()
        self.active.clear()
        self._checkpoint_write_pending = False
        await self._models_store.async_remove()
        await self._checkpoint_store.async_remove()

    async def async_disable(self, *, delete_data: bool) -> None:
        """Keep dormant data by default, or honour confirmed permanent deletion."""
        if delete_data:
            await self.async_reset_all()
            return
        if self.active:
            await self.async_save_checkpoint()

    async def async_remove(self) -> None:
        """Remove this entry's Thermal Response Stores."""
        # Config-entry deletion must also work when a future payload version can
        # no longer be loaded by this ZEAL version. Read only the stable room-ID
        # inventory/keys needed for deletion; never rewrite the unknown data.
        models = await self._models_store.async_load()
        if isinstance(models, Mapping):
            room_ids = models.get("room_ids")
            stored_models = models.get("models")
            if isinstance(room_ids, list):
                self.room_ids.update(str(room_id) for room_id in room_ids)
            if isinstance(stored_models, Mapping):
                self.room_ids.update(str(room_id) for room_id in stored_models)
        checkpoints = await self._checkpoint_store.async_load()
        if isinstance(checkpoints, Mapping) and isinstance(
            checkpoints.get("active"), Mapping
        ):
            self.room_ids.update(str(room_id) for room_id in checkpoints["active"])
        await self.async_reset_all()
