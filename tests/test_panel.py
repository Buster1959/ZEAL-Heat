"""Block 5/6 panel registration and frontend contract tests."""

from __future__ import annotations

import json
from pathlib import Path

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeal.const import (
    CONF_SHOW_IN_SIDEBAR,
    CONF_ZONES,
    DOMAIN,
    PANEL_ASSET_VERSION,
    PANEL_URL_PATH,
)
from homeassistant.components import frontend
from homeassistant.data_entry_flow import FlowResultType


PANEL_FILE = (
    Path(__file__).parents[1]
    / "custom_components"
    / "zeal"
    / "frontend"
    / "zeal-panel.js"
)


def test_panel_cache_version_matches_integration_release():
    """Every release must give the browser a fresh panel module URL."""
    manifest = json.loads(
        (PANEL_FILE.parents[1] / "manifest.json").read_text(encoding="utf-8")
    )
    assert PANEL_ASSET_VERSION == manifest["version"]


async def test_initial_flow_only_names_an_empty_instance(hass):
    """Detailed heating setup belongs to the panel, not a multi-page flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "My ZEAL"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My ZEAL"
    assert result["data"] == {}
    assert result["options"] == {
        CONF_ZONES: [],
        CONF_SHOW_IN_SIDEBAR: True,
    }


async def test_flow_allows_multiple_instances_even_when_names_match(hass):
    """Home Assistant entry IDs, not display names, identify ZEAL instances."""
    for name in ("ZEAL HVAC System", "ZEAL HVAC System"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": name}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_native_options_flow_can_restore_a_hidden_sidebar_link(hass):
    """Home Assistant Configure can recover access when the panel is hidden."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hidden ZEAL",
        data={},
        options={CONF_ZONES: [], CONF_SHOW_IN_SIDEBAR: False},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SHOW_IN_SIDEBAR: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ZONES: [],
        CONF_SHOW_IN_SIDEBAR: True,
    }


async def test_empty_entry_registers_authenticated_panel_and_asset(hass):
    """A fresh install immediately exposes the HTML setup surface."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ZEAL HVAC System",
        data={},
        options={CONF_ZONES: []},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]
    panel = hass.data[frontend.DATA_PANELS][PANEL_URL_PATH].to_response()
    assert panel["require_admin"] is False
    assert panel["config_panel_domain"] == DOMAIN
    assert panel["title"] == "ZEAL"
    assert panel["component_name"] == "custom"
    assert panel["config"]["_panel_custom"]["name"] == "zeal-panel"
    assert panel["config"]["_panel_custom"]["module_url"].endswith(
        f"/zeal-panel.js?v={PANEL_ASSET_VERSION}"
    )

    static_routes = {
        resource.canonical for resource in hass.http.app.router.resources()
    }
    assert "/zeal_static" in static_routes
    assert PANEL_FILE.is_file()

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert PANEL_URL_PATH not in hass.data.get(frontend.DATA_PANELS, {})


async def test_hidden_sidebar_keeps_the_integration_configuration_panel(hass):
    """The Configure route remains available when the sidebar link is hidden."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hidden ZEAL",
        data={},
        options={CONF_ZONES: [], CONF_SHOW_IN_SIDEBAR: False},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    panel = hass.data[frontend.DATA_PANELS][PANEL_URL_PATH].to_response()
    assert panel["title"] is None
    assert panel["config_panel_domain"] is None


async def test_shared_sidebar_link_follows_all_loaded_instances(hass):
    """Any opted-in instance keeps the single shared ZEAL link visible."""
    hidden = MockConfigEntry(
        domain=DOMAIN,
        title="Hidden ZEAL",
        data={},
        options={CONF_ZONES: [], CONF_SHOW_IN_SIDEBAR: False},
    )
    visible = MockConfigEntry(
        domain=DOMAIN,
        title="Visible ZEAL",
        data={},
        options={CONF_ZONES: [], CONF_SHOW_IN_SIDEBAR: True},
    )
    hidden.add_to_hass(hass)
    visible.add_to_hass(hass)

    assert await hass.config_entries.async_setup(hidden.entry_id)
    await hass.async_block_till_done()
    panel = hass.data[frontend.DATA_PANELS][PANEL_URL_PATH].to_response()
    assert panel["title"] == "ZEAL"

    assert await hass.config_entries.async_unload(visible.entry_id)
    panel = hass.data[frontend.DATA_PANELS][PANEL_URL_PATH].to_response()
    assert panel["title"] is None
    assert panel["config_panel_domain"] is None

    assert await hass.config_entries.async_unload(hidden.entry_id)
    assert PANEL_URL_PATH not in hass.data.get(frontend.DATA_PANELS, {})


async def test_options_reload_keeps_active_panel_route(hass, monkeypatch):
    """Saving setup must not remove the route and redirect the active UI."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ZEAL HVAC System",
        data={},
        options={CONF_ZONES: [], CONF_SHOW_IN_SIDEBAR: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    removed_paths = []
    original_remove_panel = frontend.async_remove_panel

    def record_remove_panel(hass, frontend_url_path, **kwargs):
        removed_paths.append(frontend_url_path)
        return original_remove_panel(hass, frontend_url_path, **kwargs)

    monkeypatch.setattr(frontend, "async_remove_panel", record_remove_panel)
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_SHOW_IN_SIDEBAR: False},
    )
    await hass.async_block_till_done()

    assert PANEL_URL_PATH not in removed_paths
    panel = hass.data[frontend.DATA_PANELS][PANEL_URL_PATH].to_response()
    assert panel["title"] is None
    assert panel["config_panel_domain"] is None


def test_frontend_contains_setup_safety_and_responsive_contracts():
    """Protect the Block 5 features that are not executable in pytest's DOM."""
    source = PANEL_FILE.read_text()
    assert 'type: "zeal/list_entries"' in source
    assert 'type: "zeal/get_configuration"' in source
    assert 'type: "zeal/save_hierarchy"' in source
    assert "zeal_room_thermostats" in source
    assert "catalog.physical_room_thermostats" in source
    assert "catalog.climate_entities" not in source
    assert "Zone/Floor scheduling targets" in source
    assert "It is never offered below as a physical thermostat" in source
    assert "expected_revision: this._configuration.revision" in source
    assert "Every zone needs a name" in source
    assert "Do not assign a thermostat to more than one thermostat setpoint scheduler" in source
    assert "another integration, automation, blueprint or schedule" in source
    assert 'data-action="sidebar-toggle"' in source
    assert "Show ZEAL in the Home Assistant sidebar" in source
    assert "show_in_sidebar: this._showInSidebar" in source
    assert "Settings → Devices & Services → ZEAL HVAC System → Configure" in source
    assert "ZEAL instance management" in source
    assert "Delete this ZEAL instance" in source
    assert 'this._hass.callApi(\n        "delete"' in source
    assert "config/config_entries/entry/" in source
    assert "Other ZEAL instances are not removed" in source
    assert "@media (max-width: 760px)" in source
    assert "@media (max-width: 430px)" in source


def test_frontend_contains_live_overview_demand_contracts():
    """Overview shows live actuator and room demand without duplicating control state."""
    source = PANEL_FILE.read_text()
    assert "Heating actuator" in source
    assert 'electric_thermostat: "Electric — thermostat controlled"' in source
    assert "Physical room thermostat (no separate actuator)" in source
    assert 'thermostatControlled ? "Heating control"' in source
    assert 'icon="mdi:radiator"' in source
    assert 'label: "On", cssClass: "heating"' in source
    assert 'label: "Off", cssClass: "idle"' in source
    assert "Heat demand" in source
    assert 'hasDemand ? "Present" : "None"' in source
    assert 'type: "zeal/get_zone_control"' in source
    assert "Waiting for re-enable delay" in source
    assert 'second${remaining === 1 ? "" : "s"} remaining' in source
    assert "Held by Zone Manual Override" in source
    assert "Held off because every TRV is closed" in source
    assert "blocked_until" in source
    assert "_refreshZoneControl" in source
    assert "Setpoint ${this._formatScheduleTemperature" in source
    assert "Temperature ${this._formatScheduleTemperature" in source
    assert 'label: demanding ? "Demand" : "Satisfied"' in source
    assert 'label: "Window/door open"' in source
    assert "room.opening_sensors" in source
    assert "does not request heat" in source
    assert 'temperature sensor${room.sensorCount === 1 ? "" : "s"}' in source
    assert 'Open: ${this._escape(room.openOpeningNames.join(", "))}' in source
    assert "Opening sensor unavailable:" in source
    assert 'this._hass?.states?.[thermostat.entity_id]' in source
    assert "overflow-x:auto" in source
    assert "_syncOverviewDemand" in source
    assert "Last schedule" in source
    assert "Manual change via Home Assistant or TRV" in source
    assert "Quick Change · Setpoint" in source
    assert 'if (duration === "2h") return "2 hours"' in source
    assert 'if (duration === "4h") return "4 hours"' in source
    assert 'if (duration === "next_change") return "Until next scheduled change"' in source
    assert "zeal_room_demands" in source
    assert "_historyUrl(entityIds)" in source
    assert 'available.join(",")' in source
    assert 'icon="mdi:chart-timeline-variant"' in source
    assert "Open ${zone.name} demand history" in source
    assert "Open ${name} history" in source


def test_frontend_contains_visual_scheduler_contracts():
    """Protect the Block 6 interactions that require a browser DOM."""
    source = PANEL_FILE.read_text()
    assert 'data-view="schedule"' in source
    assert "Seven-day schedule" in source
    assert 'type: "zeal/update_room_days"' in source
    assert 'type: "zeal/copy_room_schedule"' in source
    assert "expected_revision: this._configuration.revision" in source
    assert "ZEAL scheduling target" in source
    assert "The schedule changes this ZEAL thermostat only" in source
    assert "timeline-point" in source
    assert "pointerdown" in source
    assert "Continues from the previous scheduled day" in source
    assert "Apply to selected days" in source
    assert "Select all days" in source
    assert "Clear all days" in source
    assert 'data-schedule-action="toggle-target-days"' in source
    assert "Copy this seven-day schedule to other rooms" in source
    assert "Their zone, Area, physical equipment and ZEAL thermostat remain unchanged" in source


def test_frontend_contains_quick_change_and_download_contracts():
    """Protect Block 7 selection, hold and browser-download interactions."""
    source = PANEL_FILE.read_text()
    assert 'data-view="quick"' in source
    assert 'type: "zeal/get_quick_change"' in source
    assert 'type: "zeal/set_temporary_override"' in source
    assert 'type: "zeal/clear_temporary_override"' in source
    assert "Select whole house" in source
    assert "Select zone" in source
    assert "Saved weekly schedules are never edited" in source
    assert "Until next scheduled change" in source
    assert 'type: "zeal/export_configuration"' in source
    assert 'type: "zeal/get_audit_log"' in source
    assert 'this._downloadJson("zeal-configuration"' in source
    assert 'this._downloadJson("zeal-audit"' in source
    assert "do not contain Home Assistant credentials or tokens" in source
    assert "new Blob" in source


def test_frontend_contains_away_mode_and_precedence_contracts():
    """Protect Block 8's mutually exclusive sources and safety messaging."""
    source = PANEL_FILE.read_text()
    assert 'type: "zeal/save_away_mode"' in source
    assert 'value="off"' in source
    assert 'value="calendar"' in source
    assert 'value="date_range"' in source
    assert 'type="datetime-local" step="300"' in source
    assert "every active room in all zones" in source
    assert "Home Assistant's configured time zone" in source
    assert "Only active rooms receive this target" in source
    assert "Zone Manual Override remains the highest authority" in source
    assert "Use weekly schedules and Quick Change normally" in source
    assert "Quick Change is unavailable while Away mode is active" in source
    assert "End Away now" in source


def test_frontend_moves_away_mode_to_overrides_without_runtime_patching():
    """The normal-operation page owns Quick Change and administrator Away controls."""
    source = PANEL_FILE.read_text()
    assert 'data-view="quick">Overrides</button>' in source
    assert 'data-view="quick">Away settings</button>' in source
    assert "Temporary room changes and Away Mode without editing weekly schedules." in source
    assert "Apply temporary room temperature holds" in source
    assert 'this._isAdmin() ? this._renderAwaySettings() : ""' in source
    setup = source[source.index("  _renderSetup() {") : source.index("  _renderDownloads() {")]
    assert "this._renderAwaySettings()" not in setup
    assert "Discard unsaved Away changes?" in source
    assert "Away Mode remains administrator-only" in source


def test_frontend_places_safety_warning_only_on_setup_page():
    """The precaution belongs to administrator Setup, not normal operation."""
    source = PANEL_FILE.read_text()
    assert source.count("this._warning()") == 1
    content = source[source.index("  _content() {") : source.index("  _header() {")]
    assert '${this._view === "setup" ? this._warning() : ""}' in content
    assert content.index("this._warning()") > content.index("this._renderOverview()")


def test_frontend_refreshes_schedule_navigation_after_setup_reload():
    """A slow config-entry reload must not leave a newly added zone hidden."""
    source = PANEL_FILE.read_text()
    assert 'if (next === "schedule" || next === "overview") {' in source
    assert "await this._loadConfiguration({ preserveNotice: true });" in source
    assert 'data-action="refresh-configuration"' not in source
    assert "for (let attempt = 0; attempt < 40; attempt += 1)" in source
    assert "No active scheduled setpoint" in source
    assert "runtime.scheduled_period_started_at" in source


def test_frontend_limits_setup_navigation_to_administrators():
    source = PANEL_FILE.read_text()
    assert "this._hass?.user?.is_admin === true" in source
    assert 'next === "setup" && !this._isAdmin()' in source
    assert "Setup is available only to Home Assistant administrators." in source
    assert "Ask a Home Assistant administrator to configure ZEAL." in source
    assert "Allow standard users to use Schedule and Learning" in source
    assert "view its evidence and history" in source
    assert "revert proposals" in source
    assert "Allow standard users to use Overrides" in source
    assert "this._configuration?.standard_user_schedule === true" in source
    assert "this._configuration?.standard_user_quick_change === true" in source


def test_frontend_contains_schedule_learning_workflow():
    source = PANEL_FILE.read_text()
    assert "Zoned, Efficient, Adaptive, Learning" in source
    assert "Zone Energy-Aware Logic" not in source
    assert "Enable Schedule Adaptation" in source
    assert "Home Assistant Persistent Notifications" in source
    assert 'type: "zeal/get_learning"' in source
    assert 'type: "zeal/decide_learning_proposal"' in source
    assert "Learning Notifications" in source
    assert "evidence_threshold" in source
    assert "observation_days" in source
    assert "Learning is active" in source
    assert "of ${threshold} qualifying dates" in source
    assert "Oldest evidence expires" in source
    assert 'event.outcome === "applied" && new Date(event.timestamp).getTime() >= cutoff' in source
    assert "Date.now() - observationDays * 24 * 60 * 60 * 1_000" in source
    assert "Apply this change to other days" in source
    assert "This suggestion changes only" in source
    assert "Schedule changed — review manually" in source
    assert "View supporting changes" in source
    assert "Edit and accept" in source
    assert "Snooze 7 days" in source
    assert "Revert schedule change" in source
    assert "Open Schedule" in source


def test_frontend_uses_responsive_tabs_and_non_repeating_period_names():
    source = PANEL_FILE.read_text()
    assert "repeat(auto-fit, minmax(110px, 1fr))" in source
    assert '/^Period (\\d+)$/.exec(period.name || "")' in source
    assert "periods.length + 1" not in source


def test_frontend_polls_zone_control_only_on_visible_overview():
    source = PANEL_FILE.read_text()
    assert 'document.addEventListener("visibilitychange", this._visibilityHandler)' in source
    assert 'document.removeEventListener("visibilitychange", this._visibilityHandler)' in source
    assert 'document.hidden || this._view !== "overview"' in source
    assert "this._stopZoneControlTimer();" in source
