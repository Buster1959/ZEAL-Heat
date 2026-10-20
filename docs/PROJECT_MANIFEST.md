# ZEAL-Heat Project Manifest

## Identity

- Repository: `Buster1959/ZEAL-Heat`
- Home Assistant domain: `zeal`
- Integration folder: `custom_components/zeal`
- Integration name: ZEAL-Heat HVAC System
- Integration type: hub
- Distribution target: HACS custom integration
- Current release-candidate branch: `main`
- Current manifest version: `0.16.0` (pre-V1, with room occupancy setback and
  stay-long guest comfort offsets)

## Independence boundary

ZEAL-Heat includes its own scheduler and works without Visual Climate Scheduler.
Visual Climate Scheduler works without ZEAL-Heat. Users should not let ZEAL-Heat or any
other thermostat setpoint scheduler control the same thermostat entities.

## V1 status

Blocks 0–9 are implemented and the V1 candidate has been fast-forwarded to
`main`. Block 10 live regression testing is in progress; feedback fixes must be
retested before version finalisation, tagging and release. Privacy-reviewed
desktop screenshots from the generic test installation are now documented;
the mobile captures remain a release gate. PolyForm Shield remains the
project licence for V1. ZEAL-Heat is therefore distributed through HACS as a custom
repository rather than submitted to the default HACS store.
The automated and live gates are tracked in `docs/V1_ACCEPTANCE_RECORD.md`.

## Validation commands

```bash
python -m pytest tests/ -q
python -m compileall -q custom_components tests
```

GitHub runs both HACS validation and Home Assistant Hassfest on pushes and pull
requests. Hassfest passes. HACS currently reports only the unrecognised
PolyForm Shield licence. That expected policy result is retained for visibility
but is not a custom-repository or V1 release blocker. All technical metadata
checks and Hassfest must still pass; none replace live thermostat/actuator
testing.
