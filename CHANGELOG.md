# Changelog

## 0.16.3 — 2026-11-09

- Tie the ZEAL panel cache identifier to the integration release version so
  Home Assistant browsers load the new Electric heat-source interface after an
  update instead of reusing an older cached panel module.

## 0.16.2 — 2026-11-09

- Keep the active ZEAL panel route registered while saved setup changes reload
  the integration, preventing Home Assistant from redirecting to a default page.
- Update the shared panel registration in place when sidebar visibility changes.

## 0.16.1 — 2026-11-09

- Add thermostat-controlled electric heat sources without a separate actuator.
- Support frost-protection schedule targets down to the physical thermostat's
  advertised minimum temperature.

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
