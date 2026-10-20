"""Authenticated WebSocket boundary for ZEAL's HTML panel."""

from __future__ import annotations

from typing import Any
from functools import wraps

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_SHOW_IN_SIDEBAR,
    CONF_LEARNING_ENABLED,
    CONF_LEARNING_PERSISTENT_NOTIFICATIONS,
    CONF_STANDARD_USER_QUICK_CHANGE,
    CONF_STANDARD_USER_SCHEDULE,
    DOMAIN,
)
from .configuration import (
    ConfigurationConflictError,
    async_save_away_mode,
    async_save_hierarchy,
    async_save_schedule,
    configuration_snapshot,
    current_revision,
    export_configuration,
)
from .editor import copy_room_schedule, update_room_days
from .learning import apply_proposal

_REGISTERED = f"{DOMAIN}_scheduler_websocket_registered"
ERR_CONFLICT = "conflict"


def async_register_commands(hass: HomeAssistant) -> None:
    """Register commands once even when an entry reloads."""
    if hass.data.get(_REGISTERED):
        return
    for command in (
        ws_list_entries,
        ws_get_configuration,
        ws_get_zone_control,
        ws_update_room_days,
        ws_copy_room_schedule,
        ws_save_away_mode,
        ws_save_hierarchy,
        ws_get_quick_change,
        ws_set_temporary_override,
        ws_clear_temporary_override,
        ws_set_room_occupancy,
        ws_export_configuration,
        ws_get_audit_log,
        ws_get_learning,
        ws_decide_learning_proposal,
    ):
        websocket_api.async_register_command(hass, command)
    hass.data[_REGISTERED] = True


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    return hass.data.get(DOMAIN, {}).get(entry_id)


def _send_not_found(connection, msg) -> None:
    connection.send_error(
        msg["id"], websocket_api.ERR_NOT_FOUND, "ZEAL config entry is not loaded"
    )


def _require_feature(option: str):
    """Allow administrators, or standard users explicitly enabled in Setup."""
    def decorate(func):
        @wraps(func)
        def permitted(hass, connection, msg):
            user = connection.user
            entry = hass.config_entries.async_get_entry(msg.get("entry_id"))
            if user is None or entry is None or (
                not user.is_admin and not entry.options.get(option, False)
            ):
                raise Unauthorized
            return func(hass, connection, msg)

        return permitted

    return decorate


@websocket_api.websocket_command({vol.Required("type"): "zeal/list_entries"})
@callback
def ws_list_entries(hass, connection, msg) -> None:
    entries = [
        {"entry_id": entry.entry_id, "title": entry.title}
        for entry in hass.config_entries.async_entries(DOMAIN)
        if _loaded_entry(hass, entry.entry_id) is not None
    ]
    connection.send_result(msg["id"], {"entries": entries})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/get_configuration",
        vol.Required("entry_id"): str,
    }
)
@callback
def ws_get_configuration(hass, connection, msg) -> None:
    if _loaded_entry(hass, msg["entry_id"]) is None:
        _send_not_found(connection, msg)
        return
    connection.send_result(
        msg["id"], configuration_snapshot(hass, msg["entry_id"])
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/get_zone_control",
        vol.Required("entry_id"): str,
    }
)
@callback
def ws_get_zone_control(hass, connection, msg) -> None:
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    connection.send_result(
        msg["id"], {"zones": data["coordinator"].zone_control_snapshot()}
    )


@_require_feature(CONF_STANDARD_USER_SCHEDULE)
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/get_learning",
        vol.Required("entry_id"): str,
    }
)
@callback
def ws_get_learning(hass, connection, msg) -> None:
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    connection.send_result(msg["id"], data["schedule_learning"].snapshot())


@_require_feature(CONF_STANDARD_USER_SCHEDULE)
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/decide_learning_proposal",
        vol.Required("entry_id"): str,
        vol.Required("proposal_id"): str,
        vol.Required("action"): vol.In(
            ["accept", "edit_accept", "dismiss", "snooze", "revert"]
        ),
        vol.Optional("proposed_time"): str,
        vol.Optional("proposed_temperature"): vol.Coerce(float),
        vol.Optional("snoozed_until"): str,
    }
)
@websocket_api.async_response
async def ws_decide_learning_proposal(hass, connection, msg) -> None:
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or not entry.options.get(CONF_LEARNING_ENABLED, False):
        connection.send_error(
            msg["id"],
            websocket_api.ERR_NOT_ALLOWED,
            "ZEAL Learning is disabled; re-enable it before deciding a proposal",
        )
        return
    learning = data["schedule_learning"]
    now = dt_util.now()
    user_id = connection.user.id
    try:
        if msg["action"] == "dismiss":
            await learning.async_set_status(
                msg["proposal_id"],
                "dismissed",
                decided_at=now,
                decided_by=user_id,
            )
        elif msg["action"] == "snooze":
            snoozed_until = dt_util.parse_datetime(msg.get("snoozed_until", ""))
            if snoozed_until is None or snoozed_until <= now:
                raise ValueError("snoozed_until must be a future ISO datetime")
            await learning.async_set_status(
                msg["proposal_id"],
                "snoozed",
                decided_at=now,
                decided_by=user_id,
                snoozed_until=snoozed_until,
            )
        elif msg["action"] == "revert":
            proposal = learning.proposal(msg["proposal_id"])
            if proposal.get("status") != "accepted":
                raise ValueError("Only an accepted proposal can be reverted")
            reverse = {
                **proposal,
                "original_time": proposal["accepted_time"],
                "original_temperature": proposal["accepted_temperature"],
                "proposed_time": proposal["original_time"],
                "proposed_temperature": proposal["original_temperature"],
            }
            configuration = apply_proposal(
                data["schedule_runtime"].configuration, reverse
            )
            reverted_revision = await async_save_schedule(
                hass,
                msg["entry_id"],
                configuration,
                expected_revision=current_revision(hass, msg["entry_id"]),
            )
            await learning.async_set_status(
                msg["proposal_id"],
                "reverted",
                decided_at=now,
                decided_by=user_id,
                details={"reverted_revision": reverted_revision},
            )
        else:
            proposal = learning.proposal(msg["proposal_id"])
            configuration = apply_proposal(
                data["schedule_runtime"].configuration,
                proposal,
                proposed_time=msg.get("proposed_time"),
                proposed_temperature=msg.get("proposed_temperature"),
            )
            new_revision = await async_save_schedule(
                hass,
                msg["entry_id"],
                configuration,
                expected_revision=current_revision(hass, msg["entry_id"]),
            )
            accepted_period = configuration.rooms[str(proposal["room_id"])].days[
                str(proposal["weekday"])
            ]
            accepted = next(
                period
                for period in accepted_period
                if period.id == proposal["period_id"]
            )
            await learning.async_set_status(
                msg["proposal_id"],
                "accepted",
                decided_at=now,
                decided_by=user_id,
                details={
                    "accepted_time": accepted.time,
                    "accepted_temperature": accepted.temperature,
                    "accepted_revision": new_revision,
                    "edited_before_accept": msg["action"] == "edit_accept",
                },
            )
    except ConfigurationConflictError as err:
        try:
            await learning.async_set_status(
                msg["proposal_id"],
                "conflicted",
                decided_at=now,
                decided_by=user_id,
            )
        except ValueError:
            pass
        connection.send_error(msg["id"], ERR_CONFLICT, str(err))
        return
    except (KeyError, StopIteration, ValueError) as err:
        connection.send_error(
            msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err)
        )
        return
    connection.send_result(msg["id"], learning.snapshot())


@_require_feature(CONF_STANDARD_USER_SCHEDULE)
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/update_room_days",
        vol.Required("entry_id"): str,
        vol.Required("expected_revision"): str,
        vol.Required("room_id"): str,
        vol.Required("days"): dict,
    }
)
@websocket_api.async_response
async def ws_update_room_days(hass, connection, msg) -> None:
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    try:
        updated = update_room_days(
            data["schedule_runtime"].configuration,
            msg["room_id"],
            msg["days"],
        )
        await async_save_schedule(
            hass,
            msg["entry_id"],
            updated,
            expected_revision=msg["expected_revision"],
        )
    except ConfigurationConflictError as err:
        connection.send_error(msg["id"], ERR_CONFLICT, str(err))
        return
    except KeyError:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown room")
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(
        msg["id"], configuration_snapshot(hass, msg["entry_id"])
    )


@_require_feature(CONF_STANDARD_USER_SCHEDULE)
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/copy_room_schedule",
        vol.Required("entry_id"): str,
        vol.Required("expected_revision"): str,
        vol.Required("source_room_id"): str,
        vol.Required("target_room_ids"): [str],
        vol.Required("source_days"): dict,
    }
)
@websocket_api.async_response
async def ws_copy_room_schedule(hass, connection, msg) -> None:
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    try:
        updated = copy_room_schedule(
            data["schedule_runtime"].configuration,
            msg["source_room_id"],
            msg["target_room_ids"],
            msg["source_days"],
        )
        await async_save_schedule(
            hass,
            msg["entry_id"],
            updated,
            expected_revision=msg["expected_revision"],
        )
    except ConfigurationConflictError as err:
        connection.send_error(msg["id"], ERR_CONFLICT, str(err))
        return
    except KeyError:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown room")
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(
        msg["id"], configuration_snapshot(hass, msg["entry_id"])
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/save_away_mode",
        vol.Required("entry_id"): str,
        vol.Required("expected_revision"): str,
        vol.Required("mode"): vol.In(["off", "calendar", "date_range"]),
        vol.Required("calendar_entity_id"): vol.Any(None, str),
        vol.Required("starts_at"): vol.Any(None, str),
        vol.Required("ends_at"): vol.Any(None, str),
        vol.Required("temperature"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def ws_save_away_mode(hass, connection, msg) -> None:
    if _loaded_entry(hass, msg["entry_id"]) is None:
        _send_not_found(connection, msg)
        return
    try:
        await async_save_away_mode(
            hass,
            msg["entry_id"],
            expected_revision=msg["expected_revision"],
            mode=msg["mode"],
            calendar_entity_id=msg["calendar_entity_id"],
            starts_at=msg["starts_at"],
            ends_at=msg["ends_at"],
            temperature=msg["temperature"],
        )
    except ConfigurationConflictError as err:
        connection.send_error(msg["id"], ERR_CONFLICT, str(err))
        return
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(
        msg["id"], configuration_snapshot(hass, msg["entry_id"])
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/save_hierarchy",
        vol.Required("entry_id"): str,
        vol.Required("expected_revision"): str,
        vol.Required("zones"): list,
        vol.Optional("show_in_sidebar"): bool,
        vol.Optional("standard_user_schedule"): bool,
        vol.Optional("standard_user_quick_change"): bool,
        vol.Optional("learning_enabled"): bool,
        vol.Optional("learning_persistent_notifications"): bool,
    }
)
@websocket_api.async_response
async def ws_save_hierarchy(hass, connection, msg) -> None:
    if _loaded_entry(hass, msg["entry_id"]) is None:
        _send_not_found(connection, msg)
        return
    try:
        zones, revision = await async_save_hierarchy(
            hass,
            msg["entry_id"],
            msg["zones"],
            expected_revision=msg["expected_revision"],
            show_in_sidebar=msg.get("show_in_sidebar"),
            standard_user_schedule=msg.get("standard_user_schedule"),
            standard_user_quick_change=msg.get("standard_user_quick_change"),
            learning_enabled=msg.get("learning_enabled"),
            learning_persistent_notifications=msg.get(
                "learning_persistent_notifications"
            ),
        )
    except ConfigurationConflictError as err:
        connection.send_error(msg["id"], ERR_CONFLICT, str(err))
        return
    except (KeyError, ValueError) as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "entry_id": msg["entry_id"],
            "revision": revision,
            "zones": zones,
            CONF_SHOW_IN_SIDEBAR: hass.config_entries.async_get_entry(
                msg["entry_id"]
            ).options.get(CONF_SHOW_IN_SIDEBAR, True),
            CONF_STANDARD_USER_SCHEDULE: hass.config_entries.async_get_entry(
                msg["entry_id"]
            ).options.get(CONF_STANDARD_USER_SCHEDULE, False),
            CONF_STANDARD_USER_QUICK_CHANGE: hass.config_entries.async_get_entry(
                msg["entry_id"]
            ).options.get(CONF_STANDARD_USER_QUICK_CHANGE, False),
            CONF_LEARNING_ENABLED: hass.config_entries.async_get_entry(
                msg["entry_id"]
            ).options.get(CONF_LEARNING_ENABLED, False),
            CONF_LEARNING_PERSISTENT_NOTIFICATIONS: hass.config_entries.async_get_entry(
                msg["entry_id"]
            ).options.get(CONF_LEARNING_PERSISTENT_NOTIFICATIONS, True),
        },
    )


@_require_feature(CONF_STANDARD_USER_QUICK_CHANGE)
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/get_quick_change",
        vol.Required("entry_id"): str,
    }
)
@callback
def ws_get_quick_change(hass, connection, msg) -> None:
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    connection.send_result(msg["id"], data["schedule_runtime"].quick_change_state())


@_require_feature(CONF_STANDARD_USER_QUICK_CHANGE)
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/set_temporary_override",
        vol.Required("entry_id"): str,
        vol.Required("room_ids"): [str],
        vol.Required("duration"): vol.In(["2h", "4h", "next_change"]),
        vol.Required("operation"): vol.In(["delta", "temperature"]),
        vol.Required("value"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def ws_set_temporary_override(hass, connection, msg) -> None:
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    try:
        await data["schedule_runtime"].async_set_temporary_override(
            msg["room_ids"],
            duration=msg["duration"],
            operation=msg["operation"],
            value=msg["value"],
        )
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], data["schedule_runtime"].quick_change_state())


@_require_feature(CONF_STANDARD_USER_QUICK_CHANGE)
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/clear_temporary_override",
        vol.Required("entry_id"): str,
        vol.Required("room_id"): str,
    }
)
@websocket_api.async_response
async def ws_clear_temporary_override(hass, connection, msg) -> None:
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    try:
        await data["schedule_runtime"].async_clear_temporary_override(msg["room_id"])
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], data["schedule_runtime"].quick_change_state())


@_require_feature(CONF_STANDARD_USER_QUICK_CHANGE)
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/set_room_occupancy",
        vol.Required("entry_id"): str,
        vol.Required("room_id"): str,
        vol.Required("occupied"): vol.Any(None, bool),
    }
)
@websocket_api.async_response
async def ws_set_room_occupancy(hass, connection, msg) -> None:
    """Set manual room occupancy or resume its configured calendar."""
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    try:
        await data["schedule_runtime"].async_set_room_occupancy(
            msg["room_id"], msg["occupied"]
        )
    except ValueError as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], data["schedule_runtime"].quick_change_state())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/export_configuration",
        vol.Required("entry_id"): str,
    }
)
@callback
def ws_export_configuration(hass, connection, msg) -> None:
    if _loaded_entry(hass, msg["entry_id"]) is None:
        _send_not_found(connection, msg)
        return
    connection.send_result(msg["id"], export_configuration(hass, msg["entry_id"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "zeal/get_audit_log",
        vol.Required("entry_id"): str,
    }
)
@callback
def ws_get_audit_log(hass, connection, msg) -> None:
    data = _loaded_entry(hass, msg["entry_id"])
    if data is None:
        _send_not_found(connection, msg)
        return
    connection.send_result(msg["id"], data["audit_log"].export())
