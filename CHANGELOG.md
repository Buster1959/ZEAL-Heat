# Changelog

## 0.16.0 — 2026-10-20

- Add opt-in per-room occupancy setback with a configurable unoccupied target.
- Add manual occupancy, Home Assistant Calendar occupancy and a manual override
  that can return to calendar control explicitly.
- Convert guest room-thermostat or physical-TRV changes into a stay-long offset
  from the underlying weekly schedule; carry it across schedule transitions and
  clear it when occupancy ends.
- Persist occupancy stay state across Home Assistant restarts and retain the
  last known state while a configured calendar is unavailable.
- Preserve existing disabled-by-default room behaviour and the established
  write-echo, delayed-device, multi-TRV, target-clamp and actuator protections.
