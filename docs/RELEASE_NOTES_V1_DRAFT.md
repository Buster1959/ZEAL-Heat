# ZEAL-Heat V1 Release Notes — Draft

These notes describe the intended first stable ZEAL-Heat release. They remain a
draft until Block 10 live acceptance passes and the manifest version is changed
to `1.0.0`.

## Highlights

- Native Home Assistant zone/room/TRV/sensor setup through an admin-only panel.
- One canonical ZEAL-Heat room thermostat with synchronized physical TRVs.
- Safe zone-demand control, re-enable delays, Manual override and device-health
  notifications.
- Optional per-room window/door contacts suppress that room's demand while open
  and appear distinctly on Overview without changing thermostat targets.
- Native Home Assistant History shortcuts from Overview: each room opens its
  ZEAL-Heat thermostat, effective demand, zone actuator, temperature sensors and
  opening contacts; each zone opens its actuator with every room-demand entity.
- Seven-day visual schedules with exact editing, day application and room copy.
- Temporary Quick Change for rooms, zones, selections or the whole house.
- Opt-in per-room occupancy setback with manual or Home Assistant Calendar
  occupancy and restart-safe stay-long guest comfort offsets.
- Calendar or exact-date Away mode with an immediate **End Away now** action.
- Downloadable configuration and bounded persistent application audit trail.
- Optional sidebar navigation and clear per-instance management/deletion.
- Opt-in ZEAL-Heat Learning — Schedule Adaptation with bounded, source-aware manual
  change evidence, reviewable suggestions and explicit Accept, Edit and accept,
  Snooze, Dismiss and Revert decisions. ZEAL-Heat never applies a suggestion without
  authorised confirmation.
- HACS metadata, HACS/Hassfest workflows and setup-flow translations for ten
  European languages including English.

## Important safety note

Do not let ZEAL-Heat and another thermostat setpoint scheduler control the same
thermostat entities. Test first with dummy/spare equipment and complete the live
acceptance plan before unattended use.

## Known V1 limits

- Heating only; the reserved cooling fields have no runtime effect.
- Scheduler and ZEAL-Heat panel targets use Celsius in V1.
- The ZEAL-Heat HTML panel is English even when the Home Assistant setup flow is
  translated.
- Schedule Adaptation confidence currently reflects qualifying evidence count;
  Learning can be enabled globally but not per room. Room Thermal Response is
  planned for the next major version and is not part of V1.
- The final `1.0.0` manifest and GitHub release follow Block 10 validation.
- ZEAL-Heat remains under PolyForm Shield and is distributed through HACS as a
  custom repository. The licence is intentionally retained, so this release is
  not intended for the default HACS store.
