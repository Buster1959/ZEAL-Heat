const HEAT_SOURCES = {
  ashp: "Air source heat pump (ASHP)",
  modulating_boiler: "Modulating / condensing boiler",
  non_condensing_boiler: "Older non-condensing boiler",
  other: "Other / not sure",
};

const HEAT_SOURCE_DELAYS = {
  ashp: 300,
  modulating_boiler: 120,
  non_condensing_boiler: 60,
  other: 300,
};

const WEEKDAYS = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
];

const MIN_SCHEDULE_TEMPERATURE = 5;
const MAX_SCHEDULE_TEMPERATURE = 30;
const MAX_PERIODS_PER_DAY = 4;

const COMPETING_SCHEDULER_WARNING =
  "Do not assign a thermostat to more than one thermostat setpoint scheduler. " +
  "If ZEAL and another integration, automation, blueprint or schedule both change " +
  "the same thermostat’s target temperature, they may repeatedly overwrite each " +
  "other. Before enabling ZEAL scheduling, disable any other setpoint scheduler " +
  "controlling those thermostat entities.";

class ZealPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._started = false;
    this._view = "overview";
    this._entries = [];
    this._entryId = null;
    this._configuration = null;
    this._draft = [];
    this._loading = true;
    this._saving = false;
    this._deleting = false;
    this._dirty = false;
    this._showInSidebar = true;
    this._standardUserSchedule = false;
    this._standardUserQuickChange = false;
    this._learningEnabled = false;
    this._learningPersistentNotifications = true;
    this._learning = { events: [], proposals: [] };
    this._learningLoading = false;
    this._scheduleZoneId = null;
    this._scheduleRoomId = null;
    this._scheduleDays = null;
    this._scheduleSourceDay = "monday";
    this._scheduleTargetDays = new Set();
    this._scheduleCopyTargets = new Set();
    this._scheduleSelectedPeriod = null;
    this._scheduleDirty = false;
    this._scheduleSaving = false;
    this._drag = null;
    this._quickChange = { rooms: [] };
    this._quickSelected = new Set();
    this._quickDuration = "2h";
    this._quickAction = null;
    this._quickExactTarget = "";
    this._quickLoading = false;
    this._quickSaving = false;
    this._awayMode = "off";
    this._awayCalendarId = "";
    this._awayStartsAt = "";
    this._awayEndsAt = "";
    this._awayTemperature = 12;
    this._awayDirty = false;
    this._awaySaving = false;
    this._downloadBusy = false;
    this._notice = "";
    this._error = "";
    this._overviewDemandSignatures = new Map();
    this._zoneControlLoading = false;
    this._zoneControlLastFetch = 0;
    this._zoneControlTimer = null;
    this._visibilityHandler = () => this._updateZoneControlTimer();
    this.shadowRoot.addEventListener("pointerdown", (event) => this._onPointerDown(event));
    this.shadowRoot.addEventListener("pointermove", (event) => this._onPointerMove(event));
    this.shadowRoot.addEventListener("pointerup", (event) => this._onPointerUp(event));
    this.shadowRoot.addEventListener("pointercancel", (event) => this._onPointerUp(event));
  }

  set hass(value) {
    this._hass = value;
    if (
      (!this._isAdmin() && this._view === "setup") ||
      (this._view === "schedule" && !this._canUseSchedule()) ||
      (this._view === "quick" && !this._canUseQuickChange()) ||
      (this._view === "learning" && !this._canUseLearning())
    ) {
      this._view = "overview";
      this._updateZoneControlTimer();
    }
    if (!this._started && value) {
      this._started = true;
      this._initialLoad();
    } else if (this._view === "overview" && this._configuration) {
      this._syncOverviewDemand();
    }
  }

  set narrow(_value) {}
  set route(_value) {}
  set panel(_value) {}

  connectedCallback() {
    this._render();
    document.addEventListener("visibilitychange", this._visibilityHandler);
    this._updateZoneControlTimer();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._visibilityHandler);
    this._stopZoneControlTimer();
  }

  _stopZoneControlTimer() {
    if (this._zoneControlTimer) window.clearInterval(this._zoneControlTimer);
    this._zoneControlTimer = null;
  }

  _updateZoneControlTimer() {
    if (!this.isConnected || document.hidden || this._view !== "overview") {
      this._stopZoneControlTimer();
      return;
    }
    if (this._zoneControlTimer) return;
    this._zoneControlTimer = window.setInterval(() => {
      if (!this._configuration) return;
      this._syncOverviewDemand();
      this._refreshZoneControl({ minimumInterval: 5_000 });
    }, 1_000);
  }

  async _initialLoad() {
    this._loading = true;
    this._render();
    try {
      const response = await this._hass.callWS({ type: "zeal/list_entries" });
      this._entries = response.entries || [];
      if (!this._entries.length) {
        this._loading = false;
        this._error = "No loaded ZEAL instance was found.";
        this._render();
        return;
      }
      this._entryId = this._entries[0].entry_id;
      await this._loadConfiguration();
    } catch (error) {
      this._loading = false;
      this._error = this._message(error, "ZEAL could not be loaded.");
      this._render();
    }
  }

  async _loadConfiguration({ preserveNotice = false } = {}) {
    if (!this._entryId) return;
    this._loading = true;
    this._error = "";
    if (!preserveNotice) this._notice = "";
    this._render();
    try {
      const configuration = await this._hass.callWS({
        type: "zeal/get_configuration",
        entry_id: this._entryId,
      });
      this._acceptConfiguration(configuration);
    } catch (error) {
      this._loading = false;
      this._error = this._message(error, "ZEAL configuration could not be loaded.");
      this._render();
    }
  }

  _acceptConfiguration(configuration) {
    this._configuration = configuration;
    this._draft = this._copy(configuration.zones || []);
    this._showInSidebar = configuration.show_in_sidebar !== false;
    this._standardUserSchedule = configuration.standard_user_schedule === true;
    this._standardUserQuickChange = configuration.standard_user_quick_change === true;
    this._learningEnabled = configuration.learning_enabled === true;
    this._learningPersistentNotifications = configuration.learning_persistent_notifications !== false;
    this._dirty = false;
    this._loadAwayDraft();
    this._loadScheduleRoom({ keepSelection: true });
    this._acceptQuickChange(configuration.quick_change || { rooms: [] });
    this._loading = false;
    this._error = "";
    this._render();
  }

  _copy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  _isAdmin() {
    return this._hass?.user?.is_admin === true;
  }

  _canUseSchedule() {
    return this._isAdmin() || this._configuration?.standard_user_schedule === true;
  }

  _canUseQuickChange() {
    return this._isAdmin() || this._configuration?.standard_user_quick_change === true;
  }

  _canUseLearning() {
    return this._learningEnabled && this._canUseSchedule();
  }

  _message(error, fallback) {
    return error && (error.message || error.body?.message)
      ? error.message || error.body.message
      : fallback;
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _entityLabel(entity) {
    if (!entity) return "Not selected";
    const name = entity.name && entity.name !== entity.entity_id ? entity.name : "";
    return name ? `${name} · ${entity.entity_id}` : entity.entity_id;
  }

  _areaName(areaId) {
    const area = this._configuration?.catalog?.areas?.find(
      (candidate) => candidate.area_id === areaId
    );
    return area?.name || areaId;
  }

  _zealThermostat(roomId) {
    return (this._configuration?.catalog?.zeal_room_thermostats || []).find(
      (thermostat) => thermostat.room_id === roomId
    );
  }

  _zealRoomDemand(roomId) {
    return (this._configuration?.catalog?.zeal_room_demands || []).find(
      (demand) => demand.room_id === roomId
    );
  }

  _historyUrl(entityIds) {
    const available = [...new Set((entityIds || []).filter(
      (entityId) => entityId && this._hass?.states?.[entityId]
    ))];
    return available.length
      ? `/history?entity_id=${encodeURIComponent(available.join(","))}`
      : "";
  }

  _historyLink(entityIds, label) {
    const url = this._historyUrl(entityIds);
    return url
      ? `<a class="history-link" href="${this._escape(url)}" title="${this._escape(
          label
        )}" aria-label="${this._escape(label)}"><ha-icon icon="mdi:chart-timeline-variant"></ha-icon></a>`
      : "";
  }

  _render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.innerHTML = `${this._styles()}<main>${this._content()}</main>`;
    this._bindEvents();
  }

  _content() {
    if (this._loading) {
      return `<div class="center-state"><div class="spinner"></div><p>Loading ZEAL…</p></div>`;
    }
    if (!this._configuration) {
      return `<div class="shell"><section class="message error">${this._escape(
        this._error || "ZEAL is not available."
      )}</section><button class="primary" data-action="reload-all">Try again</button></div>`;
    }
    return `
      <div class="shell">
        ${this._header()}
        ${this._messages()}
        ${this._renderAwayBanner()}
        ${
          this._view === "setup"
            ? this._renderSetup()
            : this._view === "schedule"
              ? this._renderSchedule()
              : this._view === "quick"
                ? this._renderQuickChange()
              : this._view === "learning"
                ? this._renderLearning()
              : this._renderOverview()
        }
        ${this._view === "setup" ? this._warning() : ""}
      </div>`;
  }

  _header() {
    const entrySelector =
      this._entries.length > 1
        ? `<label class="entry-picker">ZEAL instance
            <select data-action="select-entry">${this._entries
              .map(
                (entry) =>
                  `<option value="${this._escape(entry.entry_id)}" ${
                    entry.entry_id === this._entryId ? "selected" : ""
                  }>${this._escape(entry.title)}</option>`
              )
              .join("")}</select>
          </label>`
        : "";
    return `
      <header>
        <div class="identity">
          <img src="/api/brands/integration/zeal/icon.png" alt="" />
          <div><h1>ZEAL</h1><p>Zoned, Efficient, Adaptive, Learning</p></div>
        </div>
        ${entrySelector}
      </header>
      <nav aria-label="ZEAL sections">
        <button class="tab ${this._view === "overview" ? "active" : ""}" data-view="overview">Overview</button>
        ${this._canUseSchedule() ? `<button class="tab ${this._view === "schedule" ? "active" : ""}" data-view="schedule">Schedule</button>` : ""}
        ${this._canUseQuickChange() ? `<button class="tab ${this._view === "quick" ? "active" : ""}" data-view="quick">Overrides</button>` : ""}
        ${this._canUseLearning() ? `<button class="tab ${this._view === "learning" ? "active" : ""}" data-view="learning">Learning</button>` : ""}
        ${
          this._isAdmin()
            ? `<button class="tab ${this._view === "setup" ? "active" : ""}" data-view="setup">Setup</button>`
            : ""
        }
      </nav>`;
  }

  _messages() {
    return `${
      this._notice
        ? `<section class="message success" role="status">${this._escape(this._notice)}</section>`
        : ""
    }${
      this._error
        ? `<section class="message error" role="alert">${this._escape(this._error)}</section>`
        : ""
    }`;
  }

  _loadAwayDraft() {
    const away = this._configuration?.away_mode || {};
    this._awayMode = away.mode || "off";
    this._awayCalendarId = away.calendar_entity_id || "";
    this._awayStartsAt = this._toHaLocalInput(away.starts_at);
    this._awayEndsAt = this._toHaLocalInput(away.ends_at);
    this._awayTemperature = Number(away.temperature ?? 12);
    this._awayDirty = false;
    this._awaySaving = false;
  }

  _toHaLocalInput(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: this._hass?.config?.time_zone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`;
  }

  _formatAwayDateTime(value) {
    if (!value) return "Not set";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString([], {
      timeZone: this._hass?.config?.time_zone,
      dateStyle: "medium",
      timeStyle: "short",
    });
  }

  _awayStatusText(away = this._configuration?.away_mode || {}) {
    if (away.status === "active") {
      return `Active now at ${this._formatScheduleTemperature(away.temperature)}`;
    }
    if (away.status === "calendar_unavailable") {
      return "The selected Home Assistant calendar is unavailable";
    }
    if (away.status === "scheduled") {
      return `Scheduled from ${this._formatAwayDateTime(away.starts_at)} to ${this._formatAwayDateTime(
        away.ends_at
      )}`;
    }
    if (away.status === "finished") return "The saved date range has finished";
    if (away.status === "waiting") return "Waiting for the selected calendar to turn on";
    return "Away mode is off";
  }

  _renderAwayBanner() {
    const away = this._configuration?.away_mode;
    if (!away || away.mode === "off") return "";
    const active = Boolean(away.active);
    return `<aside class="away-banner ${active ? "active" : ""}"><ha-icon icon="mdi:bag-suitcase"></ha-icon><div><strong>Away mode${
      active ? " is active" : ""
    }</strong><p>${this._escape(this._awayStatusText(away))}. ${
      active
        ? `${away.active_room_ids?.length || 0} active room${
            away.active_room_ids?.length === 1 ? " is" : "s are"
          } using the Away target; weekly schedules and temporary holds are paused.`
        : "Weekly schedules continue until Away activates."
    }</p></div>${
      active
        ? `<button class="primary compact" data-away-action="end">End Away now</button>`
        : this._isAdmin()
          ? `<button class="secondary compact" data-view="quick">Away settings</button>`
          : ""
    }</aside>`;
  }

  _warning() {
    return `<aside class="safety-warning"><span class="warning-icon">!</span><div><strong>Prevent competing setpoint control</strong><p>${this._escape(
      COMPETING_SCHEDULER_WARNING
    )}</p></div></aside>`;
  }

  _renderOverview() {
    const zones = this._configuration.zones || [];
    const rooms = zones.flatMap((zone) => zone.rooms || []);
    const activeRooms = rooms.filter((room) => room.active !== false).length;
    const trvs = rooms.reduce((total, room) => total + (room.trvs || []).length, 0);
    return `
      <section class="page-heading">
        <div><h2>System overview</h2><p>Heating zones and the Home Assistant equipment ZEAL controls.</p></div>
        ${
          this._isAdmin()
            ? `<div><button class="primary" data-view="setup">${zones.length ? "Modify setup" : "Start setup"}</button></div>`
            : ""
        }
      </section>
      <section class="summary-grid" aria-label="Configuration summary">
        ${this._summaryCard("Heating zones", zones.length, "mdi:radiator")}
        ${this._summaryCard("Active rooms", activeRooms, "mdi:home-thermometer")}
        ${this._summaryCard("Physical TRVs", trvs, "mdi:thermostat")}
      </section>
      ${
        zones.length
          ? `<section class="zone-grid">${zones
              .map((zone) => this._overviewZone(zone))
              .join("")}</section>`
          : `<section class="empty-card"><h3>No heating zones yet</h3><p>${
              this._isAdmin()
                ? "Use Setup to connect Home Assistant Areas, thermostatic valves, temperature sensors and a heating actuator."
                : "Ask a Home Assistant administrator to configure ZEAL."
            }</p>${
              this._isAdmin()
                ? '<button class="primary" data-view="setup">Configure ZEAL</button>'
                : ""
            }</section>`
      }`;
  }

  _summaryCard(label, value, icon) {
    return `<article class="summary-card"><ha-icon icon="${icon}"></ha-icon><div><strong>${value}</strong><span>${label}</span></div></article>`;
  }

  _overviewZone(zone) {
    const switchEntity = this._configuration.catalog.switches.find(
      (item) => item.entity_id === zone.switch
    );
    return `<article class="zone-card">
      <div class="zone-title"><div><h3>${this._escape(zone.name)}</h3><p>${this._escape(
        HEAT_SOURCES[zone.heat_source] || zone.heat_source
      )}</p></div><span class="pill">${(zone.rooms || []).length} room${
        (zone.rooms || []).length === 1 ? "" : "s"
      }</span></div>
      ${this._overviewDemand(zone, switchEntity)}
      <dl class="zone-facts">
        <div><dt>Heating actuator</dt><dd>${this._escape(
          this._entityLabel(switchEntity)
        )}</dd></div>
        <div><dt>Re-enable delay</dt><dd>${Number(zone.reenable_delay ?? 300)} seconds</dd></div>
      </dl>
      <div class="room-list">${
        (zone.rooms || []).length
          ? zone.rooms.map((room) => this._overviewRoom(room)).join("")
          : `<p class="muted">No Areas assigned to this zone.</p>`
      }</div>
    </article>`;
  }

  _overviewDemand(zone, switchEntity) {
    const body = this._overviewDemandBody(zone, switchEntity);
    this._overviewDemandSignatures.set(zone.zone_id, body);
    return `<section class="zone-demand" data-zone-demand data-zone-id="${this._escape(
      zone.zone_id
    )}" aria-label="Live heating demand for ${this._escape(zone.name)}">${body}</section>`;
  }

  _overviewDemandBody(zone, switchEntity) {
    const rooms = (zone.rooms || []).map((room) => this._roomDemandOverview(room, zone));
    const hasDemand = rooms.some((room) => room.cssClass === "demanding");
    const actuator = this._actuatorOverviewState(zone.switch);
    const control = this._configuration?.zone_control?.[zone.zone_id];
    const zoneHistory = this._historyLink(
      [
        zone.switch,
        ...(zone.rooms || []).map(
          (room) => this._zealRoomDemand(room.room_id)?.entity_id
        ),
      ],
      `Open ${zone.name} demand history`
    );
    return `<div class="actuator-status ${actuator.cssClass}">
      <ha-icon icon="mdi:radiator"></ha-icon>
      <div><span>Heating actuator</span><strong>${this._escape(actuator.label)}</strong></div>
      <div class="zone-demand-state ${hasDemand ? "demanding" : "satisfied"}">
        <span>Heat demand</span><strong>${hasDemand ? "Present" : "None"}</strong>
      </div>
      ${zoneHistory}
    </div>
    ${
      hasDemand && actuator.cssClass === "idle"
        ? `<p class="actuator-explanation">${this._escape(
            this._actuatorHoldExplanation(control)
          )}</p>`
        : ""
    }
    <div class="demand-strip" tabindex="0" role="list" aria-label="Room demand, setpoint and temperature">
      ${
        rooms.length
          ? rooms.map((room) => this._roomDemandChip(room)).join("")
          : `<span class="demand-empty">No rooms assigned</span>`
      }
    </div>`;
  }

  _actuatorOverviewState(entityId) {
    const state = entityId ? this._hass?.states?.[entityId] : null;
    if (!entityId) return { label: "Not configured", cssClass: "unknown" };
    if (!state || ["unknown", "unavailable"].includes(state.state)) {
      return { label: "Unavailable", cssClass: "unknown" };
    }
    return state.state === "on"
      ? { label: "On", cssClass: "heating" }
      : { label: "Off", cssClass: "idle" };
  }

  _actuatorHoldExplanation(control) {
    if (control?.manual_override) {
      return "Held by Zone Manual Override — automatic actuator control is paused.";
    }
    if (control?.demand_lines?.some((line) => line.startsWith("All TRVs off"))) {
      return "Held off because every TRV is closed, protecting the pump from a closed loop.";
    }
    const blockedUntil = control?.blocked_until
      ? new Date(control.blocked_until).getTime()
      : NaN;
    if (Number.isFinite(blockedUntil)) {
      const remaining = Math.max(0, Math.ceil((blockedUntil - Date.now()) / 1_000));
      if (remaining > 0) {
        return `Waiting for re-enable delay · ${remaining} second${remaining === 1 ? "" : "s"} remaining`;
      }
      if (control?.needs_heat) {
        return "Re-enable delay elapsed · waiting for actuator confirmation.";
      }
    }
    return "Demand is present while the actuator is off; checking control state.";
  }

  async _refreshZoneControl({ minimumInterval = 2_000 } = {}) {
    const now = Date.now();
    if (
      this._zoneControlLoading ||
      !this._entryId ||
      now - this._zoneControlLastFetch < minimumInterval
    ) {
      return;
    }
    this._zoneControlLoading = true;
    this._zoneControlLastFetch = now;
    try {
      const response = await this._hass.callWS({
        type: "zeal/get_zone_control",
        entry_id: this._entryId,
      });
      if (this._configuration) {
        this._configuration.zone_control = response.zones || {};
        this._syncOverviewDemand();
      }
    } catch (_error) {
      // The normal Home Assistant state stream still updates actuator/room state.
    } finally {
      this._zoneControlLoading = false;
    }
  }

  _roomDemandOverview(room, zone) {
    const name = room.name || this._areaName(room.room_id);
    const sensorCount = (room.sensors || []).length;
    const openingContext = (room.opening_sensors || []).reduce(
      (result, entityId) => {
        const state = this._hass?.states?.[entityId];
        const catalogEntity = (this._configuration?.catalog?.opening_sensors || []).find(
          (candidate) => candidate.entity_id === entityId
        );
        const label = catalogEntity?.name || entityId;
        if (state?.state === "on") result.open.push(label);
        else if (!state || ["unknown", "unavailable"].includes(state.state)) {
          result.unavailable.push(label);
        }
        return result;
      },
      { open: [], unavailable: [] }
    );
    const context = {
      sensorCount,
      openOpeningNames: openingContext.open,
      unavailableOpeningNames: openingContext.unavailable,
      historyLink: this._historyLink(
        [
          this._zealThermostat(room.room_id)?.entity_id,
          this._zealRoomDemand(room.room_id)?.entity_id,
          zone?.switch,
          ...(room.sensors || []),
          ...(room.opening_sensors || []),
        ],
        `Open ${name} history`
      ),
    };
    if (room.active === false) {
      return { name, label: "Inactive", cssClass: "inactive", setpoint: null, temperature: null, ...context };
    }
    const thermostat = this._zealThermostat(room.room_id);
    const state = thermostat ? this._hass?.states?.[thermostat.entity_id] : null;
    if (openingContext.open.length) {
      const setpoint = state?.attributes?.temperature == null
        ? null
        : Number(state.attributes.temperature);
      const temperature = state?.attributes?.current_temperature == null
        ? null
        : Number(state.attributes.current_temperature);
      return {
        name,
        label: "Window/door open",
        cssClass: "suppressed",
        setpoint: Number.isFinite(setpoint) ? setpoint : null,
        temperature: Number.isFinite(temperature) ? temperature : null,
        ...context,
      };
    }
    if (!state || ["unknown", "unavailable"].includes(state.state)) {
      return { name, label: "Unavailable", cssClass: "unknown", setpoint: null, temperature: null, ...context };
    }
    const setpoint =
      state.attributes?.temperature == null ? NaN : Number(state.attributes.temperature);
    const temperature =
      state.attributes?.current_temperature == null
        ? NaN
        : Number(state.attributes.current_temperature);
    if (state.state === "off") {
      return { name, label: "Off", cssClass: "inactive", setpoint, temperature, ...context };
    }
    if (!Number.isFinite(setpoint) || !Number.isFinite(temperature)) {
      return { name, label: "Unavailable", cssClass: "unknown", setpoint, temperature, ...context };
    }
    const demanding = setpoint - temperature > 0;
    return {
      name,
      label: demanding ? "Demand" : "Satisfied",
      cssClass: demanding ? "demanding" : "satisfied",
      setpoint,
      temperature,
      ...context,
    };
  }

  _roomDemandChip(room) {
    const readings =
      Number.isFinite(room.setpoint) && Number.isFinite(room.temperature)
        ? `<span>Setpoint ${this._formatScheduleTemperature(
            room.setpoint
          )} · Temperature ${this._formatScheduleTemperature(room.temperature)}</span>`
        : `<span>Temperature data unavailable</span>`;
    const sensorSummary = `${room.sensorCount} temperature sensor${room.sensorCount === 1 ? "" : "s"}`;
    const openingSummary = room.openOpeningNames?.length
      ? `<span class="demand-context opening-open">Open: ${this._escape(room.openOpeningNames.join(", "))}</span>`
      : "";
    const unavailableSummary = room.unavailableOpeningNames?.length
      ? `<span class="demand-context opening-unavailable">Opening sensor unavailable: ${this._escape(
          room.unavailableOpeningNames.join(", ")
        )}</span>`
      : "";
    return `<div class="demand-chip ${room.cssClass}" role="listitem">
      <strong>${this._escape(room.name)}</strong>
      ${room.historyLink || ""}
      ${readings}
      <span class="demand-context">${this._escape(sensorSummary)}</span>
      ${openingSummary}
      ${unavailableSummary}
      <em>${this._escape(room.label)}</em>
    </div>`;
  }

  _syncOverviewDemand() {
    if (!this.shadowRoot || this._view !== "overview") return;
    for (const container of this.shadowRoot.querySelectorAll("[data-zone-demand]")) {
      const zone = (this._configuration?.zones || []).find(
        (candidate) => candidate.zone_id === container.dataset.zoneId
      );
      if (!zone) continue;
      const switchEntity = this._configuration.catalog.switches.find(
        (item) => item.entity_id === zone.switch
      );
      const body = this._overviewDemandBody(zone, switchEntity);
      if (this._overviewDemandSignatures.get(zone.zone_id) === body) continue;
      const scrollLeft = container.querySelector(".demand-strip")?.scrollLeft || 0;
      container.innerHTML = body;
      const strip = container.querySelector(".demand-strip");
      if (strip) strip.scrollLeft = scrollLeft;
      this._overviewDemandSignatures.set(zone.zone_id, body);
    }
  }

  _overviewRoom(room) {
    const zealThermostat = this._zealThermostat(room.room_id);
    const control = this._roomControlSummary(room, zealThermostat);
    return `<div class="room-summary">
      <div><strong>${this._escape(room.name || this._areaName(room.room_id))}</strong><span>${this._escape(
        this._areaName(room.room_id)
      )} · ${(room.trvs || []).length} TRV${(room.trvs || []).length === 1 ? "" : "s"} · ${
        (room.sensors || []).length
      } sensor${(room.sensors || []).length === 1 ? "" : "s"}</span><span>ZEAL target: ${this._escape(
        zealThermostat ? this._entityLabel(zealThermostat) : "Not created"
      )}</span><span>${this._escape(control.schedule)}</span><span class="control-source ${
        control.cssClass
      }">${this._escape(control.effective)}</span></div>
      <span class="state ${room.active === false ? "inactive" : ""}">${
        room.active === false ? "Inactive" : "Active"
      }</span>
    </div>`;
  }

  _roomControlSummary(room, zealThermostat) {
    const runtime = (this._configuration?.quick_change?.rooms || []).find(
      (candidate) => candidate.room_id === room.room_id
    );
    const scheduled =
      runtime?.scheduled_temperature == null
        ? NaN
        : Number(runtime.scheduled_temperature);
    const schedule = Number.isFinite(scheduled)
      ? `Last schedule ${this._formatChangeTime(
          runtime.scheduled_period_started_at
        )} · Setpoint ${this._formatScheduleTemperature(scheduled)}`
      : "No active scheduled setpoint";
    const entityState = zealThermostat
      ? this._hass?.states?.[zealThermostat.entity_id]
      : null;
    const liveTarget =
      entityState?.attributes?.temperature == null
        ? NaN
        : Number(entityState.attributes.temperature);
    const override = runtime?.override;
    if (runtime?.effective_source === "away") {
      const target = Number(runtime.effective_temperature);
      return {
        schedule,
        effective: Number.isFinite(target)
          ? `Away mode · Setpoint ${this._formatScheduleTemperature(target)}`
          : "Away mode",
        cssClass: "away",
      };
    }
    if (runtime?.effective_source === "unoccupied_setback") {
      return {
        schedule,
        effective: `Unoccupied setback · Setpoint ${this._formatScheduleTemperature(runtime.effective_temperature)}`,
        cssClass: "away",
      };
    }
    if (runtime?.effective_source === "occupancy_comfort_offset") {
      const offset = Number(runtime?.occupancy?.comfort_offset || 0);
      return {
        schedule,
        effective: `Guest comfort offset ${offset > 0 ? "+" : ""}${offset}${this._scheduleTemperatureUnit()} · Setpoint ${this._formatScheduleTemperature(runtime.effective_temperature)}`,
        cssClass: "manual",
      };
    }
    if (override) {
      return {
        schedule,
        effective: `Quick Change · Setpoint ${this._formatScheduleTemperature(
          override.temperature
        )} · Duration: ${this._overrideDurationLabel(override.duration)}`,
        cssClass: "temporary",
      };
    }
    if (
      Number.isFinite(liveTarget) &&
      Number.isFinite(scheduled) &&
      Math.abs(liveTarget - scheduled) >= 0.01
    ) {
      return {
        schedule,
        effective: `Manual change via Home Assistant or TRV · Setpoint ${this._formatScheduleTemperature(
          liveTarget
        )} · Duration: Until next scheduled change`,
        cssClass: "manual",
      };
    }
    return {
      schedule,
      effective: Number.isFinite(liveTarget)
        ? `Following schedule · Setpoint ${this._formatScheduleTemperature(liveTarget)}`
        : "Current setpoint unavailable",
      cssClass: "schedule",
    };
  }

  _overrideDurationLabel(duration) {
    if (duration === "2h") return "2 hours";
    if (duration === "4h") return "4 hours";
    if (duration === "next_change") return "Until next scheduled change";
    return "Unknown";
  }

  _acceptQuickChange(state) {
    this._quickChange = this._copy(state || { rooms: [] });
    if (this._configuration) {
      this._configuration.quick_change = this._copy(this._quickChange);
      if (this._quickChange.away_mode) {
        this._configuration.away_mode = this._copy(this._quickChange.away_mode);
      }
    }
    const available = new Set(
      (this._quickChange.rooms || []).map((room) => room.room_id)
    );
    this._quickSelected = new Set(
      [...this._quickSelected].filter((roomId) => available.has(roomId))
    );
    this._quickSaving = false;
    this._syncQuickExactTarget();
  }

  async _loadQuickChange() {
    this._quickLoading = true;
    this._error = "";
    this._render();
    try {
      const state = await this._hass.callWS({
        type: "zeal/get_quick_change",
        entry_id: this._entryId,
      });
      this._acceptQuickChange(state);
    } catch (error) {
      this._error = this._message(error, "Quick Change could not be refreshed.");
    }
    this._quickLoading = false;
    this._render();
  }

  _quickRooms() {
    return this._quickChange?.rooms || [];
  }

  _quickRoom(roomId) {
    return this._quickRooms().find((room) => room.room_id === roomId);
  }

  _quickRoomIdsInZone(zone) {
    const available = new Set(this._quickRooms().map((room) => room.room_id));
    return (zone.rooms || [])
      .map((room) => room.room_id)
      .filter((roomId) => available.has(roomId));
  }

  _formatLocalDateTime(value) {
    if (!value) return "the scheduled change";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString([], {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  _formatChangeTime(value) {
    if (!value) return "unknown";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleTimeString([], {
      timeZone: this._hass?.config?.time_zone,
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  _renderQuickChange() {
    if (this._quickLoading) {
      return `<div class="center-state quick-loading"><div class="spinner"></div><p>Refreshing current targets…</p></div>`;
    }
    const rooms = this._quickRooms();
    const awayActive = Boolean(this._quickChange?.away_mode?.active);
    const description = this._isAdmin()
      ? "Temporary room changes and Away Mode without editing weekly schedules."
      : "Temporary room changes without editing weekly schedules.";
    if (!rooms.length) {
      return `<section class="page-heading"><div><h2>Overrides</h2><p>${description}</p></div><button class="secondary" data-view="setup">Open setup</button></section>
        <section class="empty-card"><ha-icon icon="mdi:thermostat-auto"></ha-icon><h3>No schedulable rooms yet</h3><p>Save a room with at least one physical thermostat before applying a temporary hold.</p></section>${this._isAdmin() ? this._renderAwaySettings() : ""}`;
    }
    const roomIds = rooms.map((room) => room.room_id);
    const wholeHouseSelected = roomIds.every((roomId) =>
      this._quickSelected.has(roomId)
    );
    const activeHolds = rooms.filter((room) => room.override).length;
    const groups = (this._configuration?.zones || [])
      .map((zone) => {
        const zoneRoomIds = this._quickRoomIdsInZone(zone);
        if (!zoneRoomIds.length) return "";
        const allSelected = zoneRoomIds.every((roomId) =>
          this._quickSelected.has(roomId)
        );
        return `<section class="quick-zone"><div class="quick-zone-heading"><div><h3>${this._escape(
          zone.name
        )}</h3><span>${zoneRoomIds.length} room${
          zoneRoomIds.length === 1 ? "" : "s"
        }</span></div><button class="text-button compact" data-quick-action="toggle-zone" data-zone-id="${this._escape(
          zone.zone_id
        )}" ${awayActive ? "disabled" : ""}>${allSelected ? "Clear zone" : "Select zone"}</button></div><div class="quick-room-grid">${zoneRoomIds
          .map((roomId) => this._renderQuickRoom(this._quickRoom(roomId)))
          .join("")}</div></section>`;
      })
      .join("");
    const selectedCount = this._quickSelected.size;
    const actionDescription = this._quickActionDescription();
    return `
      <section class="page-heading quick-heading">
        <div><h2>Overrides</h2><p>${description}</p></div>
        <div class="quick-heading-actions"><button class="secondary" data-quick-action="refresh">Refresh</button><button class="primary" data-quick-action="whole-house" ${awayActive ? "disabled" : ""}>${
          wholeHouseSelected ? "Clear selection" : "Select whole house"
        }</button></div>
      </section>
      <section class="setup-help">
        <strong>Quick Change</strong>
        <p>Apply temporary room temperature holds. Saved weekly schedules are never edited.</p>
      </section>
      ${
        awayActive
          ? `<section class="away-quick-notice"><strong>Away mode controls room targets now</strong><p>Quick Change is paused while active rooms use ${this._escape(
              this._formatScheduleTemperature(this._quickChange.away_mode.temperature)
            )}. Existing holds remain saved and resume after Away ends if they have not expired.</p></section>`
          : ""
      }
      <section class="quick-summary" aria-label="Quick Change summary">
        <span><strong>${selectedCount}</strong> selected</span>
        <span><strong>${activeHolds}</strong> active hold${activeHolds === 1 ? "" : "s"}</span>
        <span>Holds automatically return to the saved schedule.</span>
      </section>
      <section class="quick-zones">${groups}</section>
      <section class="quick-controls">
        <div><h3>Temporary change</h3><p>Choose an adjustment or an exact target, then choose how long it should last.</p></div>
        <div class="quick-control-grid">
          <div class="quick-temperature-actions">
            <button class="secondary" data-quick-action="delta" data-value="-1" ${awayActive ? "disabled" : ""}>−1${this._scheduleTemperatureUnit()}</button>
            <button class="secondary" data-quick-action="delta" data-value="1" ${awayActive ? "disabled" : ""}>+1${this._scheduleTemperatureUnit()}</button>
            <label>Exact target (${this._scheduleTemperatureUnit()})<input type="number" min="${MIN_SCHEDULE_TEMPERATURE}" max="${MAX_SCHEDULE_TEMPERATURE}" step="0.1" data-quick-action="temperature" value="${this._escape(
              this._quickExactTarget
            )}" placeholder="20" ${awayActive ? "disabled" : ""} /></label>
          </div>
          <fieldset class="quick-durations"><legend>Duration</legend>
            <label><input type="radio" name="quick-duration" data-quick-action="duration" value="2h" ${
              this._quickDuration === "2h" ? "checked" : ""
            } ${awayActive ? "disabled" : ""} /> 2 hours</label>
            <label><input type="radio" name="quick-duration" data-quick-action="duration" value="4h" ${
              this._quickDuration === "4h" ? "checked" : ""
            } ${awayActive ? "disabled" : ""} /> 4 hours</label>
            <label><input type="radio" name="quick-duration" data-quick-action="duration" value="next_change" ${
              this._quickDuration === "next_change" ? "checked" : ""
            } ${awayActive ? "disabled" : ""} /> Until next scheduled change</label>
          </fieldset>
        </div>
        <div class="quick-apply-row"><span class="quick-action-state">${this._escape(
          actionDescription || "Choose a temperature change."
        )}</span><button class="primary" data-quick-action="apply" ${
          awayActive || !selectedCount || !this._quickAction || this._quickSaving ? "disabled" : ""
        }>${this._quickSaving ? "Applying…" : "Apply temporary hold"}</button></div>
      </section>${this._isAdmin() ? this._renderAwaySettings() : ""}`;
  }

  _renderQuickRoom(room) {
    if (!room) return "";
    const selected = this._quickSelected.has(room.room_id);
    const override = room.override;
    const scheduled = this._formatScheduleTemperature(room.scheduled_temperature);
    const effective = this._formatScheduleTemperature(room.effective_temperature);
    const awayActive = room.effective_source === "away";
    const occupancy = room.occupancy || { enabled: false };
    const offset = Number(occupancy.comfort_offset || 0);
    const status = awayActive
      ? `Away ${effective}; scheduled ${scheduled}`
      : override
      ? `Holding ${effective} until ${this._formatLocalDateTime(override.expires_at)}`
      : room.scheduled_temperature === null
        ? "No active scheduled target"
        : `Scheduled ${scheduled}`;
    const occupancyControls = occupancy.enabled
      ? `<div class="occupancy-control"><span class="hold-pill">${
          occupancy.occupied ? "Occupied" : "Unoccupied"
        }${Math.abs(offset) >= 0.01 ? ` · ${offset > 0 ? "+" : ""}${this._escape(offset)}${this._scheduleTemperatureUnit()}` : ""}</span><div>
          <button class="text-button compact" data-occupancy-action="set" data-room-id="${this._escape(room.room_id)}" data-occupied="true" ${this._quickSaving ? "disabled" : ""}>Occupied</button>
          <button class="text-button compact" data-occupancy-action="set" data-room-id="${this._escape(room.room_id)}" data-occupied="false" ${this._quickSaving ? "disabled" : ""}>Unoccupied</button>
          ${occupancy.source === "calendar" ? `<button class="text-button compact" data-occupancy-action="follow" data-room-id="${this._escape(room.room_id)}" ${this._quickSaving ? "disabled" : ""}>Follow calendar</button>` : ""}
        </div></div>`
      : "";
    return `<article class="quick-room ${selected ? "selected" : ""} ${
      override ? "holding" : ""
    }"><label><input type="checkbox" data-quick-action="room" value="${this._escape(
      room.room_id
    )}" ${selected ? "checked" : ""} ${awayActive ? "disabled" : ""} /><span><strong>${this._escape(
      room.room_name
    )}</strong><small>${this._escape(status)}</small></span></label><div class="quick-room-state">${
      awayActive
        ? `<span class="hold-pill">Away target</span>${
            override
              ? `<button class="text-button compact" data-quick-action="clear-hold" data-room-id="${this._escape(
                  room.room_id
                )}" ${this._quickSaving ? "disabled" : ""}>Cancel paused hold</button>`
              : ""
          }`
        : override
        ? `<span class="hold-pill">Temporary hold</span><button class="text-button compact" data-quick-action="clear-hold" data-room-id="${this._escape(
            room.room_id
          )}" ${this._quickSaving ? "disabled" : ""}>Cancel hold</button>`
        : `<span>Current target: ${effective}</span>`
    }</div>${occupancyControls}</article>`;
  }

  _quickReferenceTarget() {
    if (this._quickSelected.size !== 1) return null;
    const room = this._quickRoom([...this._quickSelected][0]);
    return room?.effective_temperature ?? room?.scheduled_temperature ?? null;
  }

  _syncQuickExactTarget() {
    const reference = this._quickReferenceTarget();
    if (this._quickAction?.operation === "temperature") {
      this._quickExactTarget = this._quickAction.value;
    } else if (this._quickAction?.operation === "delta") {
      this._quickExactTarget =
        reference === null ? "" : Number(reference) + this._quickAction.value;
    } else {
      this._quickExactTarget = reference ?? "";
    }
  }

  _quickActionDescription() {
    if (!this._quickAction) return "";
    if (this._quickAction.operation === "temperature") {
      return `Exact target ${this._formatScheduleTemperature(
        this._quickAction.value
      )}`;
    }
    const sign = this._quickAction.value > 0 ? "+" : "";
    return `Adjust each selected room by ${sign}${this._quickAction.value}${this._scheduleTemperatureUnit()}`;
  }

  _toggleQuickSelection(roomIds) {
    const allSelected = roomIds.every((roomId) =>
      this._quickSelected.has(roomId)
    );
    for (const roomId of roomIds) {
      if (allSelected) this._quickSelected.delete(roomId);
      else this._quickSelected.add(roomId);
    }
    this._syncQuickExactTarget();
    this._render();
  }

  async _applyQuickChange() {
    if (this._quickChange?.away_mode?.active) {
      this._error = "Quick Change is unavailable while Away mode is active.";
      this._render();
      return;
    }
    if (!this._quickSelected.size) {
      this._error = "Choose one or more rooms, a Zone/Floor, or the whole house.";
      this._render();
      return;
    }
    if (!this._quickAction || !Number.isFinite(this._quickAction.value)) {
      this._error = "Choose a +/− adjustment or enter an exact target temperature.";
      this._render();
      return;
    }
    this._quickSaving = true;
    this._error = "";
    this._notice = "";
    this._render();
    try {
      const state = await this._hass.callWS({
        type: "zeal/set_temporary_override",
        entry_id: this._entryId,
        room_ids: [...this._quickSelected],
        duration: this._quickDuration,
        ...this._quickAction,
      });
      const count = this._quickSelected.size;
      this._quickAction = null;
      this._acceptQuickChange(state);
      this._notice = `Temporary hold applied to ${count} room${
        count === 1 ? "" : "s"
      }. Saved weekly schedules were not changed.`;
    } catch (error) {
      this._quickSaving = false;
      this._error = this._message(error, "The temporary hold could not be applied.");
    }
    this._render();
  }

  async _clearQuickHold(roomId) {
    this._quickSaving = true;
    this._error = "";
    this._notice = "";
    this._render();
    try {
      const state = await this._hass.callWS({
        type: "zeal/clear_temporary_override",
        entry_id: this._entryId,
        room_id: roomId,
      });
      const roomName = this._quickRoom(roomId)?.room_name || "The room";
      this._acceptQuickChange(state);
      this._notice = `${roomName}'s temporary hold was cancelled. Its saved schedule has resumed.`;
    } catch (error) {
      this._quickSaving = false;
      this._error = this._message(error, "The temporary hold could not be cancelled.");
    }
    this._render();
  }

  async _setRoomOccupancy(roomId, occupied) {
    this._quickSaving = true;
    this._error = "";
    this._notice = "";
    this._render();
    try {
      const state = await this._hass.callWS({
        type: "zeal/set_room_occupancy",
        entry_id: this._entryId,
        room_id: roomId,
        occupied,
      });
      const roomName = this._quickRoom(roomId)?.room_name || "The room";
      this._acceptQuickChange(state);
      this._notice = occupied === null
        ? `${roomName} is following its occupancy calendar.`
        : `${roomName} is now ${occupied ? "occupied" : "unoccupied"}.`;
    } catch (error) {
      this._quickSaving = false;
      this._error = this._message(error, "Room occupancy could not be changed.");
    }
    this._render();
  }

  _downloadJson(prefix, data) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const blob = new Blob([`${JSON.stringify(data, null, 2)}\n`], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${prefix}-${stamp}.json`;
    link.hidden = true;
    this.shadowRoot.append(link);
    link.click();
    link.remove();
    globalThis.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  async _downloadConfiguration() {
    if (this._downloadBusy) return;
    this._downloadBusy = true;
    this._error = "";
    try {
      const configuration = await this._hass.callWS({
        type: "zeal/export_configuration",
        entry_id: this._entryId,
      });
      this._downloadJson("zeal-configuration", configuration);
      this._notice = "Saved ZEAL configuration downloaded as JSON.";
    } catch (error) {
      this._error = this._message(error, "The configuration download failed.");
    }
    this._downloadBusy = false;
    this._render();
  }

  async _downloadAuditTrail() {
    if (this._downloadBusy) return;
    this._downloadBusy = true;
    this._error = "";
    try {
      const audit = await this._hass.callWS({
        type: "zeal/get_audit_log",
        entry_id: this._entryId,
      });
      this._downloadJson("zeal-audit", audit);
      this._notice = `Audit trail downloaded (${audit.entries?.length || 0} entries).`;
    } catch (error) {
      this._error = this._message(error, "The audit-trail download failed.");
    }
    this._downloadBusy = false;
    this._render();
  }

  _scheduleRooms() {
    return this._configuration?.schedule?.rooms || {};
  }

  _scheduleRoomIdsInZone(zoneId) {
    const schedules = this._scheduleRooms();
    const zone = (this._configuration?.zones || []).find(
      (candidate) => candidate.zone_id === zoneId
    );
    return (zone?.rooms || [])
      .map((room) => room.room_id)
      .filter((roomId) => schedules[roomId]);
  }

  _scheduleZones() {
    return (this._configuration?.zones || []).filter(
      (zone) => this._scheduleRoomIdsInZone(zone.zone_id).length
    );
  }

  _zoneForScheduleRoom(roomId) {
    return (this._configuration?.zones || []).find((zone) =>
      (zone.rooms || []).some((room) => room.room_id === roomId)
    );
  }

  _loadScheduleRoom({ keepSelection = false } = {}) {
    const schedules = this._scheduleRooms();
    const zones = this._scheduleZones();
    const existingRoom = keepSelection && schedules[this._scheduleRoomId];
    if (existingRoom) {
      this._scheduleZoneId = this._zoneForScheduleRoom(this._scheduleRoomId)?.zone_id || null;
    }
    if (!zones.some((zone) => zone.zone_id === this._scheduleZoneId)) {
      this._scheduleZoneId = zones[0]?.zone_id || null;
    }
    const roomIds = this._scheduleRoomIdsInZone(this._scheduleZoneId);
    if (!roomIds.includes(this._scheduleRoomId)) {
      this._scheduleRoomId = roomIds[0] || null;
    }
    const room = schedules[this._scheduleRoomId];
    this._scheduleDays = room ? this._copy(room.days) : null;
    this._scheduleSourceDay = "monday";
    this._scheduleTargetDays = new Set();
    this._scheduleCopyTargets = new Set();
    this._scheduleSelectedPeriod = null;
    this._scheduleDirty = false;
    this._scheduleSaving = false;
    this._drag = null;
  }

  _renderSchedule() {
    const schedules = this._scheduleRooms();
    if (!Object.keys(schedules).length) {
      return `<section class="page-heading"><div><h2>Schedule</h2><p>Create rooms with physical thermostats before adding schedules.</p></div><button class="primary" data-view="setup">Open setup</button></section>
        <section class="empty-card"><ha-icon icon="mdi:calendar-clock"></ha-icon><h3>No schedulable rooms yet</h3><p>A ZEAL room becomes schedulable after it is saved with at least one physical thermostat/TRV.</p><button class="primary" data-view="setup">Configure rooms</button></section>`;
    }
    const zones = this._scheduleZones();
    const roomIds = this._scheduleRoomIdsInZone(this._scheduleZoneId);
    const zoneOptions = zones
      .map(
        (zone) =>
          `<option value="${this._escape(zone.zone_id)}" ${
            zone.zone_id === this._scheduleZoneId ? "selected" : ""
          }>${this._escape(zone.name)}</option>`
      )
      .join("");
    const roomOptions = roomIds
      .map(
        (roomId) =>
          `<option value="${this._escape(roomId)}" ${
            roomId === this._scheduleRoomId ? "selected" : ""
          }>${this._escape(schedules[roomId].room_name)}</option>`
      )
      .join("");
    const thermostat = this._zealThermostat(this._scheduleRoomId);
    return `
      <section class="page-heading schedule-heading">
        <div><h2>Seven-day schedule</h2><p>Each room has seven independent daily setpoint schedules.</p></div>
        <div class="schedule-navigation">
          <label>Zone / Floor<select data-schedule-action="zone">${zoneOptions}</select></label>
          <label>Room<select data-schedule-action="room">${roomOptions}</select></label>
        </div>
      </section>
      <section class="schedule-target"><ha-icon icon="mdi:thermostat"></ha-icon><div><strong>ZEAL scheduling target</strong><span>${this._escape(
        thermostat ? this._entityLabel(thermostat) : "Canonical ZEAL room thermostat is unavailable"
      )}</span><small>The schedule changes this ZEAL thermostat only. ZEAL then applies its safe canonical target to the room's physical thermostats.</small></div></section>
      <section class="schedule-week">${WEEKDAYS.map((day) => this._renderScheduleDay(day)).join("")}</section>
      <section class="schedule-actions-card">
        <div><h3>Apply a day</h3><p>Choose one Source day, tick Apply here on the destination days, then apply. Save when the week is ready.</p></div>
        <button class="text-button compact" data-schedule-action="toggle-target-days">${WEEKDAYS.filter(
          (day) => day !== this._scheduleSourceDay
        ).every((day) => this._scheduleTargetDays.has(day)) ? "Clear all days" : "Select all days"}</button>
        <button class="secondary compact" data-schedule-action="apply-days">Apply to selected days</button>
        ${this._renderScheduleCopy()}
      </section>
      <div class="save-bar schedule-save-bar">
        <span class="schedule-save-state">${
          this._scheduleDirty ? "Unsaved schedule changes" : "Schedule is up to date"
        }</span>
        <div><button class="text-button" data-schedule-action="discard" ${
          !this._scheduleDirty || this._scheduleSaving ? "disabled" : ""
        }>Discard changes</button><button class="primary" data-schedule-action="save" ${
          !this._scheduleDirty || this._scheduleSaving ? "disabled" : ""
        }>${this._scheduleSaving ? "Saving…" : "Save schedule"}</button></div>
      </div>`;
  }

  _renderScheduleDay(day) {
    const periods = this._scheduleDays?.[day] || [];
    const rows = periods
      .map(
        (period, index) => `<div class="period-row ${
          this._scheduleSelectedPeriod?.day === day &&
          this._scheduleSelectedPeriod?.index === index
            ? "selected"
            : ""
        }">
          <input aria-label="${day} period name" data-schedule-day="${day}" data-schedule-index="${index}" data-schedule-field="name" value="${this._escape(
            period.name
          )}" />
          <input aria-label="${day} period time" type="time" data-schedule-day="${day}" data-schedule-index="${index}" data-schedule-field="time" value="${this._escape(
            period.time
          )}" />
          <input aria-label="${day} period target" type="number" min="${MIN_SCHEDULE_TEMPERATURE}" max="${MAX_SCHEDULE_TEMPERATURE}" step="0.1" data-schedule-day="${day}" data-schedule-index="${index}" data-schedule-field="temperature" value="${this._escape(
            period.temperature
          )}" />
          <span>${this._scheduleTemperatureUnit()}</span>
          <button class="period-remove" data-schedule-action="remove-period" data-day="${day}" data-index="${index}" title="Remove period" aria-label="Remove ${this._escape(
            period.name
          )}">×</button>
        </div>`
      )
      .join("");
    const title = `${day[0].toUpperCase()}${day.slice(1)}`;
    return `<article class="day-card">
      <div class="day-heading"><h3>${title}</h3><label><input type="radio" name="schedule-source-day" data-schedule-action="source-day" value="${day}" ${
        this._scheduleSourceDay === day ? "checked" : ""
      } /> Source</label><label><input type="checkbox" data-schedule-action="target-day" value="${day}" ${
        this._scheduleTargetDays.has(day) ? "checked" : ""
      } ${this._scheduleSourceDay === day ? "disabled" : ""} /> Apply here</label></div>
      ${this._renderTimeline(day)}
      <div class="period-labels"><span>Name</span><span>Time</span><span>Target</span></div>
      ${rows || '<p class="room-empty schedule-empty">No periods yet. The previous day’s final target continues.</p>'}
      <button class="secondary add-period" data-schedule-action="add-period" data-day="${day}" ${
        periods.length >= MAX_PERIODS_PER_DAY ? "disabled" : ""
      }>+ Add period</button>
    </article>`;
  }

  _renderScheduleCopy() {
    const schedules = this._scheduleRooms();
    const destinationIds = Object.keys(schedules).filter(
      (roomId) => roomId !== this._scheduleRoomId
    );
    const allSelected =
      destinationIds.length > 0 &&
      destinationIds.every((roomId) => this._scheduleCopyTargets.has(roomId));
    const groups = this._scheduleZones()
      .map((zone) => {
        const targets = this._scheduleRoomIdsInZone(zone.zone_id).filter(
          (roomId) => roomId !== this._scheduleRoomId
        );
        if (!targets.length) return "";
        return `<fieldset><legend>${this._escape(zone.name)}</legend>${targets
          .map(
            (roomId) =>
              `<label><input type="checkbox" data-schedule-action="copy-target" value="${this._escape(
                roomId
              )}" ${
                this._scheduleCopyTargets.has(roomId) ? "checked" : ""
              } /> ${this._escape(schedules[roomId].room_name)}</label>`
          )
          .join("")}</fieldset>`;
      })
      .join("");
    return `<details class="copy-schedule">
      <summary>Copy this seven-day schedule to other rooms</summary>
      <p>This saves the current room's editor state and replaces only the schedules of the selected rooms. Their zone, Area, physical equipment and ZEAL thermostat remain unchanged.</p>
      <button class="text-button compact" data-schedule-action="toggle-copy-targets" ${
        destinationIds.length ? "" : "disabled"
      }>${allSelected ? "Clear all" : "Select all"}</button>
      <div class="copy-targets">${groups || '<span class="muted">No other schedulable rooms are available.</span>'}</div>
      <button class="primary compact" data-schedule-action="copy-schedule" ${
        this._scheduleCopyTargets.size && !this._scheduleSaving ? "" : "disabled"
      }>Copy to selected rooms</button>
    </details>`;
  }

  _scheduleTemperatureUnit() {
    return this._configuration?.schedule?.temperature_unit || "°C";
  }

  _formatScheduleTemperature(value) {
    return value === null || value === undefined || value === ""
      ? "—"
      : `${Number(value)}${this._scheduleTemperatureUnit()}`;
  }

  _timeToMinutes(value) {
    const [hours, minutes] = String(value).split(":").map(Number);
    return hours * 60 + minutes;
  }

  _timeFromMinutes(value) {
    const minutes = Math.max(0, Math.min(1439, value));
    return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(
      minutes % 60
    ).padStart(2, "0")}`;
  }

  _temperatureAtStartOfDay(day) {
    const dayIndex = WEEKDAYS.indexOf(day);
    if (dayIndex < 0 || !this._scheduleDays) return null;
    for (let offset = 1; offset <= WEEKDAYS.length; offset += 1) {
      const previous =
        this._scheduleDays[
          WEEKDAYS[(dayIndex - offset + WEEKDAYS.length) % WEEKDAYS.length]
        ];
      if (!previous?.length) continue;
      const ordered = [...previous].sort((left, right) =>
        left.time.localeCompare(right.time)
      );
      return Number(ordered[ordered.length - 1].temperature);
    }
    return null;
  }

  _temperatureRange(periods, carryTemperature = null) {
    const values = periods
      .map((period) => Number(period.temperature))
      .filter(Number.isFinite);
    if (Number.isFinite(carryTemperature)) values.push(carryTemperature);
    const middle = values.length
      ? values.reduce((sum, value) => sum + value, 0) / values.length
      : 20;
    let minimum = Math.max(
      MIN_SCHEDULE_TEMPERATURE,
      Math.floor(Math.min(...values, middle) - 2)
    );
    let maximum = Math.min(
      MAX_SCHEDULE_TEMPERATURE,
      Math.ceil(Math.max(...values, middle) + 2)
    );
    if (maximum - minimum < 6) {
      minimum = Math.max(
        MIN_SCHEDULE_TEMPERATURE,
        Math.min(Math.floor(middle - 3), MAX_SCHEDULE_TEMPERATURE - 6)
      );
      maximum = Math.min(MAX_SCHEDULE_TEMPERATURE, minimum + 6);
    }
    return { minimum, maximum };
  }

  _renderTimeline(day) {
    const periods = this._scheduleDays?.[day] || [];
    const carryTemperature = this._temperatureAtStartOfDay(day);
    const { minimum, maximum } = this._temperatureRange(
      periods,
      carryTemperature
    );
    const ordered = periods
      .map((period, index) => ({ period, index }))
      .sort((left, right) => left.period.time.localeCompare(right.period.time));
    const coordinates = ordered.map(({ period, index }) => ({
      index,
      x: (this._timeToMinutes(period.time) / 1440) * 100,
      y:
        ((maximum - Number(period.temperature)) / (maximum - minimum)) * 100,
      temperature: Number(period.temperature),
    }));
    let path = "";
    if (coordinates.length || carryTemperature !== null) {
      const startTemperature = carryTemperature ?? coordinates[0].temperature;
      const startY =
        ((maximum - startTemperature) / (maximum - minimum)) * 100;
      path = `M 0 ${startY.toFixed(2)}`;
      for (const point of coordinates) {
        path += ` H ${point.x.toFixed(2)} V ${point.y.toFixed(2)}`;
      }
      path += " H 100";
    }
    const points = coordinates
      .map(
        ({ index, x, y, temperature }) =>
          `<button class="timeline-point" data-schedule-action="timeline-point" data-day="${day}" data-index="${index}" style="left:${x}%;top:${y}%" title="Drag to change time and target" aria-label="${this._escape(
            day
          )} ${this._escape(periods[index].name)}: ${this._escape(
            periods[index].time
          )}, ${this._formatScheduleTemperature(
            temperature
          )}">${this._formatScheduleTemperature(temperature)}</button>`
      )
      .join("");
    const continuity =
      carryTemperature === null
        ? ""
        : ` Continues from the previous scheduled day at ${this._formatScheduleTemperature(
            carryTemperature
          )}.`;
    return `<div class="visual-editor"><div class="timeline-title">Visual editor <span>Drag a point: left/right changes time; up/down changes target.${continuity}</span></div><div class="timeline-shell"><div class="temperature-scale"><span>${this._formatScheduleTemperature(
      maximum
    )}</span><span>${this._formatScheduleTemperature(
      Math.round((minimum + maximum) / 2)
    )}</span><span>${this._formatScheduleTemperature(
      minimum
    )}</span></div><div><div class="timeline-plot" data-timeline-day="${day}" data-temp-min="${minimum}" data-temp-max="${maximum}"><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><path d="${path}" /></svg>${points}</div><div class="time-scale"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div></div></div></div>`;
  }

  _addSchedulePeriod(day) {
    const periods = this._scheduleDays[day];
    if (periods.length >= MAX_PERIODS_PER_DAY) return;
    const usedTimes = new Set(periods.map((period) => period.time));
    const suggestedTimes = ["06:00", "08:00", "12:00", "16:00", "18:00", "22:00"];
    let time = suggestedTimes.find((candidate) => !usedTimes.has(candidate));
    if (!time) {
      for (let minutes = 0; minutes < 1440; minutes += 15) {
        const candidate = this._timeFromMinutes(minutes);
        if (!usedTimes.has(candidate)) {
          time = candidate;
          break;
        }
      }
    }
    const number = periods.reduce((highest, period) => {
      const match = /^Period (\d+)$/.exec(period.name || "");
      return Math.max(highest, match ? Number(match[1]) : 0);
    }, 0) + 1;
    const name = `Period ${number}`;
    periods.push({
      id: `period-${Date.now()}-${number}`,
      friendly_name: this._slug(name),
      name,
      time: time || "12:00",
      temperature: this._temperatureAtStartOfDay(day) ?? 20,
    });
    this._markScheduleChanged(true);
  }

  _slug(value) {
    return (
      String(value)
        .toLowerCase()
        .trim()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_|_$/g, "") || "period"
    );
  }

  _markScheduleChanged(render = false) {
    this._scheduleDirty = true;
    this._notice = "";
    this._error = "";
    if (render) {
      this._render();
      return;
    }
    const saveButton = this.shadowRoot.querySelector(
      '[data-schedule-action="save"]'
    );
    const discardButton = this.shadowRoot.querySelector(
      '[data-schedule-action="discard"]'
    );
    const state = this.shadowRoot.querySelector(".schedule-save-state");
    if (saveButton) saveButton.disabled = false;
    if (discardButton) discardButton.disabled = false;
    if (state) state.textContent = "Unsaved schedule changes";
  }

  _applyScheduleDays() {
    const targets = [...this._scheduleTargetDays].filter(
      (day) => day !== this._scheduleSourceDay
    );
    if (!this._scheduleSourceDay || !targets.length) {
      this._error =
        "Choose a Source day and tick Apply here on at least one different day.";
      this._render();
      return;
    }
    for (const day of targets) {
      this._scheduleDays[day] = this._copy(
        this._scheduleDays[this._scheduleSourceDay]
      );
    }
    this._notice = `${
      this._scheduleSourceDay[0].toUpperCase() + this._scheduleSourceDay.slice(1)
    } applied to ${targets.join(", ")}. Save schedule to keep it.`;
    this._error = "";
    this._scheduleDirty = true;
    this._render();
  }

  _toggleScheduleTargetDays() {
    const destinations = WEEKDAYS.filter((day) => day !== this._scheduleSourceDay);
    const allSelected = destinations.every((day) => this._scheduleTargetDays.has(day));
    this._scheduleTargetDays = allSelected ? new Set() : new Set(destinations);
    this._render();
  }

  _sortedScheduleDays() {
    const days = this._copy(this._scheduleDays);
    for (const periods of Object.values(days)) {
      periods.sort((left, right) => left.time.localeCompare(right.time));
    }
    return days;
  }

  async _saveSchedule() {
    if (!this._scheduleDirty || this._scheduleSaving) return;
    this._scheduleSaving = true;
    this._error = "";
    this._render();
    try {
      const configuration = await this._hass.callWS({
        type: "zeal/update_room_days",
        entry_id: this._entryId,
        expected_revision: this._configuration.revision,
        room_id: this._scheduleRoomId,
        days: this._sortedScheduleDays(),
      });
      this._configuration = configuration;
      this._draft = this._copy(configuration.zones || []);
      this._loadScheduleRoom({ keepSelection: true });
      this._notice = "Schedule saved and applied to the running ZEAL scheduler.";
      this._error = "";
    } catch (error) {
      this._scheduleSaving = false;
      const conflict = error?.code === "conflict" || error?.body?.code === "conflict";
      this._error = conflict
        ? "ZEAL changed in another browser or process. Reload before saving this schedule."
        : this._message(error, "Schedule could not be saved.");
    }
    this._render();
  }

  async _copySchedule() {
    if (!this._scheduleCopyTargets.size || this._scheduleSaving) return;
    const schedules = this._scheduleRooms();
    const sourceName = schedules[this._scheduleRoomId]?.room_name || "This room";
    const targetNames = [...this._scheduleCopyTargets].map(
      (roomId) => schedules[roomId]?.room_name || roomId
    );
    this._scheduleSaving = true;
    this._render();
    try {
      const configuration = await this._hass.callWS({
        type: "zeal/copy_room_schedule",
        entry_id: this._entryId,
        expected_revision: this._configuration.revision,
        source_room_id: this._scheduleRoomId,
        target_room_ids: [...this._scheduleCopyTargets],
        source_days: this._sortedScheduleDays(),
      });
      this._configuration = configuration;
      this._draft = this._copy(configuration.zones || []);
      this._loadScheduleRoom({ keepSelection: true });
      this._notice = `Copied ${sourceName}'s seven-day schedule to ${targetNames.join(
        ", "
      )}. Room configuration and thermostats were not changed.`;
      this._error = "";
    } catch (error) {
      this._scheduleSaving = false;
      const conflict = error?.code === "conflict" || error?.body?.code === "conflict";
      this._error = conflict
        ? "ZEAL changed in another browser or process. Reload before copying this schedule."
        : this._message(error, "The schedule could not be copied.");
    }
    this._render();
  }

  _toggleScheduleCopyTargets() {
    const destinations = Object.keys(this._scheduleRooms()).filter(
      (roomId) => roomId !== this._scheduleRoomId
    );
    const allSelected =
      destinations.length > 0 &&
      destinations.every((roomId) => this._scheduleCopyTargets.has(roomId));
    this._scheduleCopyTargets = allSelected ? new Set() : new Set(destinations);
    this._render();
  }

  _onPointerDown(event) {
    const point = event.target.closest(".timeline-point");
    if (!point || !this._scheduleDays || this._view !== "schedule") return;
    event.preventDefault();
    this._scheduleSelectedPeriod = {
      day: point.dataset.day,
      index: Number(point.dataset.index),
    };
    this._drag = {
      point,
      day: point.dataset.day,
      index: Number(point.dataset.index),
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    };
    point.setPointerCapture(event.pointerId);
  }

  _onPointerMove(event) {
    if (!this._drag || this._drag.pointerId !== event.pointerId) return;
    if (
      !this._drag.moved &&
      Math.hypot(
        event.clientX - this._drag.startX,
        event.clientY - this._drag.startY
      ) < 4
    )
      return;
    this._drag.moved = true;
    this._updateDrag(event);
  }

  _onPointerUp(event) {
    if (!this._drag || this._drag.pointerId !== event.pointerId) return;
    if (this._drag.moved) this._updateDrag(event);
    this._drag.point.releasePointerCapture?.(event.pointerId);
    const moved = this._drag.moved;
    this._drag = null;
    if (moved) this._markScheduleChanged(true);
  }

  _updateDrag(event) {
    const { day, index, point } = this._drag;
    const plot = this.shadowRoot.querySelector(`[data-timeline-day="${day}"]`);
    if (!plot) return;
    const bounds = plot.getBoundingClientRect();
    const horizontal = Math.max(
      0,
      Math.min(1, (event.clientX - bounds.left) / bounds.width)
    );
    const vertical = Math.max(
      0,
      Math.min(1, (event.clientY - bounds.top) / bounds.height)
    );
    const period = this._scheduleDays[day][index];
    const minutes = Math.min(1439, Math.round((horizontal * 1440) / 15) * 15);
    const minimum = Number(plot.dataset.tempMin);
    const maximum = Number(plot.dataset.tempMax);
    period.time = this._timeFromMinutes(minutes);
    period.temperature = Math.max(
      MIN_SCHEDULE_TEMPERATURE,
      Math.min(
        MAX_SCHEDULE_TEMPERATURE,
        Math.round((maximum - vertical * (maximum - minimum)) * 2) / 2
      )
    );
    point.style.left = `${(minutes / 1440) * 100}%`;
    point.style.top = `${
      ((maximum - period.temperature) / (maximum - minimum)) * 100
    }%`;
    point.textContent = this._formatScheduleTemperature(period.temperature);
    const timeField = this.shadowRoot.querySelector(
      `input[data-schedule-day="${day}"][data-schedule-index="${index}"][data-schedule-field="time"]`
    );
    const temperatureField = this.shadowRoot.querySelector(
      `input[data-schedule-day="${day}"][data-schedule-index="${index}"][data-schedule-field="temperature"]`
    );
    if (timeField) timeField.value = period.time;
    if (temperatureField) temperatureField.value = period.temperature;
  }

  _renderSetup() {
    const usedAreas = new Set(
      this._draft.flatMap((zone) => (zone.rooms || []).map((room) => room.room_id))
    );
    const unassignedAreas = (this._configuration.catalog.areas || []).filter(
      (area) => !usedAreas.has(area.area_id)
    );
    return `
      <section class="page-heading">
        <div><h2>Setup</h2><p>Build each heating zone from Home Assistant Areas and their equipment.</p></div>
        <button class="secondary" data-action="add-zone">+ Add zone</button>
      </section>
      <section class="setup-help">
        <strong>Before you begin</strong>
        <p>Create and assign your Areas, physical climate entities, temperature sensors and actuator switches in Home Assistant first. An Area can belong to one ZEAL zone only.</p>
      </section>
      <section class="setup-zones">
        ${
          this._draft.length
            ? this._draft
                .map((zone, zoneIndex) =>
                  this._setupZone(zone, zoneIndex, unassignedAreas)
                )
                .join("")
            : `<div class="empty-card"><h3>Add your first heating zone</h3><p>A zone normally represents a floor, heating circuit or other group sharing one heating actuator.</p><button class="primary" data-action="add-zone">+ Add zone</button></div>`
        }
      </section>
      ${this._renderDownloads()}
      ${this._renderLearningSetting()}
      ${this._renderStandardUserAccess()}
      ${this._renderSidebarSetting()}
      ${this._renderInstanceManagement()}
      <div class="save-bar">
        <span class="save-state">${this._dirty ? "Unsaved setup changes" : "Setup is up to date"}</span>
        <div><button class="text-button" data-action="reset" ${
          !this._dirty || this._saving ? "disabled" : ""
        }>Discard changes</button><button class="primary" data-action="save" ${
          !this._dirty || this._saving ? "disabled" : ""
        }>${this._saving ? "Saving…" : "Save setup"}</button></div>
      </div>`;
  }

  _renderDownloads() {
    return `<section class="download-card"><div><h3>Downloads</h3><p>Export the saved ZEAL setup and schedules, or the recent canonical thermostat application history, as readable JSON files.</p><small>Downloads contain entity IDs, room and zone names, schedules, targets and outcomes. They do not contain Home Assistant credentials or tokens.${
      this._dirty || this._awayDirty ? " Unsaved edits are not included." : ""
    }</small></div><div class="download-actions"><button class="secondary" data-download-action="configuration" ${
      this._downloadBusy ? "disabled" : ""
    }>Download configuration</button><button class="secondary" data-download-action="audit" ${
      this._downloadBusy ? "disabled" : ""
    }>Download audit trail</button></div></section>`;
  }

  _renderSidebarSetting() {
    return `<section class="sidebar-card"><h3>Home Assistant sidebar</h3>
      <label class="active-toggle"><input type="checkbox" data-action="sidebar-toggle" ${
        this._showInSidebar ? "checked" : ""
      } /><span><strong>Show ZEAL in the Home Assistant sidebar</strong><small>When hidden, open ZEAL from Settings → Devices & Services → ZEAL HVAC System → Configure. If you use multiple ZEAL instances, the shared sidebar link remains visible while any instance has this option enabled.</small></span></label>
    </section>`;
  }

  _renderStandardUserAccess() {
    return `<section class="sidebar-card"><h3>Standard-user access</h3>
      <p>Overview is visible to every signed-in Home Assistant user. Choose which heating controls standard users may also use.</p>
      <label class="active-toggle"><input type="checkbox" data-action="standard-user-schedule" ${
        this._standardUserSchedule ? "checked" : ""
      } /><span><strong>Allow standard users to use Schedule and Learning</strong><small>They can view and save weekly room schedules. When Learning is enabled, they can also view its evidence and history and accept, edit and accept, snooze, dismiss or revert proposals.</small></span></label>
      <label class="active-toggle"><input type="checkbox" data-action="standard-user-quick-change" ${
        this._standardUserQuickChange ? "checked" : ""
      } /><span><strong>Allow standard users to use Overrides</strong><small>They can use Quick Change to apply and clear temporary room temperature changes. Away Mode remains administrator-only.</small></span></label>
      <small>Setup, Away Mode configuration, downloads, audit and instance management remain administrator-only.</small>
    </section>`;
  }

  _renderLearningSetting() {
    return `<section class="sidebar-card"><h3>ZEAL Learning</h3>
      <label class="active-toggle"><input type="checkbox" data-action="learning-enabled" ${
        this._learningEnabled ? "checked" : ""
      } /><span><strong>Enable Schedule Adaptation</strong><small>Capture qualified manual intent and create reviewable schedule suggestions. ZEAL never changes a schedule without explicit confirmation.</small></span></label>
      <label class="active-toggle"><input type="checkbox" data-action="learning-persistent-notifications" ${
        this._learningPersistentNotifications ? "checked" : ""
      } /><span><strong>Home Assistant Persistent Notifications</strong><small>Maintain one aggregated alert when Learning suggestions are ready for review.</small></span></label>
      <small>The current evidence threshold and observation window are shown on the Learning page. Learning history may reveal household routines.</small>
    </section>`;
  }

  async _loadLearning() {
    if (!this._entryId || !this._canUseLearning()) return;
    this._learningLoading = true;
    this._render();
    try {
      this._learning = await this._hass.callWS({
        type: "zeal/get_learning",
        entry_id: this._entryId,
      });
      this._error = "";
    } catch (error) {
      this._error = this._message(error, "ZEAL Learning notifications could not be loaded.");
    } finally {
      this._learningLoading = false;
      this._render();
    }
  }

  _renderLearning() {
    if (this._learningLoading) return `<div class="center-state"><div class="spinner"></div><p>Loading Learning notifications…</p></div>`;
    const proposals = [...(this._learning?.proposals || [])].reverse();
    const actionable = proposals.filter((proposal) =>
      proposal.status === "new" ||
      (proposal.status === "snoozed" && proposal.snoozed_until && new Date(proposal.snoozed_until).getTime() <= Date.now())
    );
    const history = proposals.filter((proposal) => !actionable.includes(proposal));
    const threshold = Number(this._learning?.evidence_threshold);
    const observationDays = Number(this._learning?.observation_days);
    if (!Number.isInteger(threshold) || threshold < 1 || !Number.isInteger(observationDays) || observationDays < 1) {
      return `<section class="empty-card"><h3>Learning policy unavailable</h3><p>Reload ZEAL. The evidence threshold and observation window were not returned by the integration.</p></section>`;
    }
    return `<section class="page-heading"><div><h2>Learning Notifications</h2><p>Review evidence-backed Schedule Adaptation suggestions. Nothing is committed without confirmation.</p></div></section>
      ${this._learningEvidenceProgress()}
      <section class="learning-section-heading"><h3>Schedule suggestions</h3><p>Review, adjust, postpone or dismiss each suggestion.</p></section>
      ${actionable.length ? `<section class="zone-grid">${actionable.map((proposal) => this._learningProposal(proposal, true)).join("")}</section>` : `<section class="empty-card"><h3>No new learning suggestions</h3><p>ZEAL will place a suggestion here after ${threshold} qualifying dates for the same room and comparable schedule period.</p></section>`}
      ${history.length ? `<section class="setup-help"><strong>Proposal history</strong><p>${history.length} accepted, dismissed or conflicted proposal${history.length === 1 ? "" : "s"}.</p></section><section class="zone-grid">${history.map((proposal) => this._learningProposal(proposal, false)).join("")}</section>` : ""}`;
  }

  _learningEvidenceProgress() {
    const observationDays = Number(this._learning.observation_days);
    const threshold = Number(this._learning.evidence_threshold);
    const cutoff = Date.now() - observationDays * 24 * 60 * 60 * 1_000;
    const qualifying = (this._learning?.events || []).filter((event) =>
      event.outcome === "applied" && new Date(event.timestamp).getTime() >= cutoff
    );
    const groups = new Map();
    for (const event of qualifying) {
      const key = `${event.room_id}|${event.pattern_key}|${event.room_schedule_revision}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(event);
    }
    const active = [...groups.values()]
      .map((events) => events.sort((left, right) => String(left.timestamp).localeCompare(String(right.timestamp))))
      .sort((left, right) => String(right[right.length - 1].timestamp).localeCompare(String(left[left.length - 1].timestamp)));
    const roomName = (roomId) => (this._configuration.zones || [])
      .flatMap((zone) => zone.rooms || [])
      .find((room) => room.room_id === roomId)?.name || roomId;
    return `<section class="learning-progress"><div><h3>Learning is active</h3><p>${qualifying.length} qualifying change${qualifying.length === 1 ? "" : "s"} recorded across ${active.length} active pattern${active.length === 1 ? "" : "s"}. A suggestion needs changes on ${threshold} separate dates within ${observationDays} days.</p></div>
      ${active.length ? `<ul>${active.map((events) => {
        const latest = events[events.length - 1];
        const dates = new Set(events.map((event) => event.local_date));
        const expiry = new Date(new Date(events[0].timestamp).getTime() + observationDays * 24 * 60 * 60 * 1_000);
        return `<li><strong>${this._escape(roomName(latest.room_id))}</strong><span>${this._escape(this._learningAdaptationLabel(latest))} · ${Math.min(dates.size, threshold)} of ${threshold} qualifying dates</span><small>Oldest evidence expires ${this._escape(expiry.toLocaleDateString())}</small></li>`;
      }).join("")}</ul>` : `<p class="learning-progress-empty">No qualifying manual changes have been recorded yet.</p>`}
    </section>`;
  }

  _learningAdaptationLabel(item) {
    if (item.adaptation_type === "timing") {
      return item.adaptation_direction === "earlier" ? "Earlier start" : "Later start";
    }
    return "Temperature change";
  }

  _learningStatusLabel(status) {
    return ({
      new: "Ready for review",
      snoozed: "Snoozed",
      accepted: "Accepted",
      dismissed: "Dismissed",
      conflicted: "Schedule changed — review manually",
      reverted: "Reverted",
    })[status] || String(status || "Unknown").replaceAll("_", " ");
  }

  _learningProposal(proposal, actionable) {
    const room = (this._configuration.zones || []).flatMap((zone) => zone.rooms || []).find((item) => item.room_id === proposal.room_id);
    const current = `${proposal.original_time} · ${this._formatScheduleTemperature(proposal.original_temperature)}`;
    const proposed = `${proposal.proposed_time} · ${this._formatScheduleTemperature(proposal.proposed_temperature)}`;
    const evidenceIds = new Set(proposal.evidence_ids || []);
    const evidence = (this._learning?.events || [])
      .filter((event) => evidenceIds.has(event.event_id))
      .sort((left, right) => String(left.timestamp).localeCompare(String(right.timestamp)));
    const weekday = String(proposal.weekday || "");
    const weekdayLabel = weekday ? `${weekday[0].toUpperCase()}${weekday.slice(1)}` : "Scheduled day";
    const dateRange = evidence.length
      ? `${new Date(evidence[0].timestamp).toLocaleDateString()} to ${new Date(evidence[evidence.length - 1].timestamp).toLocaleDateString()}`
      : "the observation window";
    return `<article class="zone-card"><div class="zone-title"><div><h3>${this._escape(room?.name || proposal.room_id)}</h3><p>${this._escape(weekdayLabel)} · ${this._escape(this._learningAdaptationLabel(proposal))}</p></div><span class="pill">${this._escape(this._learningStatusLabel(proposal.status))}</span></div>
      <dl class="zone-facts"><div><dt>Current</dt><dd>${this._escape(current)}</dd></div><div><dt>Proposed</dt><dd>${this._escape(proposed)}</dd></div><div><dt>Evidence</dt><dd>${Number(proposal.evidence_count || 0)} qualifying dates · ${this._escape(proposal.confidence || "")}</dd></div></dl>
      <p class="learning-evidence-summary">${Number(proposal.evidence_count || 0)} qualifying changes from ${this._escape(dateRange)} support this suggestion.</p>
      ${evidence.length ? `<details><summary>View supporting changes</summary><ul>${evidence.map((event) => `<li>${this._escape(new Date(event.timestamp).toLocaleString())} · ${this._escape(String(event.source || "unknown").replaceAll("_", " "))} · ${this._formatScheduleTemperature(event.requested_temperature)}</li>`).join("")}</ul></details>` : ""}
      ${actionable ? `<aside class="learning-day-scope"><strong>Apply this change to other days</strong><p>This suggestion changes only ${this._escape(weekdayLabel)}. To apply it to additional days, use the Schedule page.</p><button class="text-button" data-learning-action="schedule" data-proposal-id="${this._escape(proposal.proposal_id)}">Open Schedule</button></aside><div class="download-actions"><button class="primary" data-learning-action="accept" data-proposal-id="${this._escape(proposal.proposal_id)}">Accept</button><button class="secondary" data-learning-action="edit" data-proposal-id="${this._escape(proposal.proposal_id)}">Edit and accept</button><button class="secondary" data-learning-action="snooze" data-proposal-id="${this._escape(proposal.proposal_id)}">Snooze 7 days</button><button class="text-button" data-learning-action="dismiss" data-proposal-id="${this._escape(proposal.proposal_id)}">Dismiss</button></div>` : proposal.status === "accepted" ? `<div class="download-actions"><button class="secondary" data-learning-action="revert" data-proposal-id="${this._escape(proposal.proposal_id)}">Revert schedule change</button></div>` : ""}
    </article>`;
  }

  _renderInstanceManagement() {
    const entry = this._entries.find((item) => item.entry_id === this._entryId);
    return `<section class="instance-card"><div><h3>ZEAL instance management</h3><p>You are managing <strong>${this._escape(
      entry?.title || this._configuration?.title || "this ZEAL instance"
    )}</strong>. Disabling or deleting it does not affect any other ZEAL instance.</p><small>Deleting permanently removes this instance's zones, schedules, Away settings and audit trail.</small></div><div class="instance-actions"><button class="secondary" data-action="manage-instances">Open integration settings</button><button class="danger-button" data-action="delete-instance" ${
      this._deleting ? "disabled" : ""
    }>${this._deleting ? "Deleting…" : "Delete this ZEAL instance"}</button></div></section>`;
  }

  _renderAwaySettings() {
    const calendars = this._configuration?.catalog?.calendars || [];
    const saved = this._configuration?.away_mode || {};
    return `<section class="away-settings">
      <div class="away-settings-heading"><div><h3>Away mode</h3><p>Temporarily use one global target for every active room in all zones without changing weekly schedules.</p></div><span class="state ${saved.active ? "away-active" : ""}">${this._escape(
        this._awayStatusText(saved)
      )}</span></div>
      <fieldset class="away-source"><legend>How should Away mode activate?</legend>
        <label><input type="radio" name="away-mode" data-away-field="mode" value="off" ${
          this._awayMode === "off" ? "checked" : ""
        } /> <span><strong>Off</strong><small>Use weekly schedules and Quick Change normally.</small></span></label>
        <label><input type="radio" name="away-mode" data-away-field="mode" value="calendar" ${
          this._awayMode === "calendar" ? "checked" : ""
        } /> <span><strong>Home Assistant Calendar</strong><small>Away is active whenever the selected calendar entity is on.</small></span></label>
        <label><input type="radio" name="away-mode" data-away-field="mode" value="date_range" ${
          this._awayMode === "date_range" ? "checked" : ""
        } /> <span><strong>Start and end date/time</strong><small>Choose one exact period using Home Assistant's configured time zone.</small></span></label>
      </fieldset>
      <div class="away-fields">
        ${
          this._awayMode === "calendar"
            ? `<label>Calendar<select data-away-field="calendar_entity_id"><option value="">Choose a calendar</option>${calendars
                .map(
                  (calendar) =>
                    `<option value="${this._escape(calendar.entity_id)}" ${
                      calendar.entity_id === this._awayCalendarId ? "selected" : ""
                    }>${this._escape(this._entityLabel(calendar))}</option>`
                )
                .join("")}</select><small>${
                  calendars.length
                    ? "A dedicated holiday calendar avoids unrelated events activating Away mode."
                    : "No calendar entities were found. Create a Home Assistant Local Calendar first."
                }</small></label>`
            : ""
        }
        ${
          this._awayMode === "date_range"
            ? `<label>Away starts<input type="datetime-local" step="300" data-away-field="starts_at" value="${this._escape(
                this._awayStartsAt
              )}" /></label><label>Away ends<input type="datetime-local" step="300" data-away-field="ends_at" value="${this._escape(
                this._awayEndsAt
              )}" /><small>Times use five-minute intervals. The start is included; the end is the moment normal control resumes.</small></label>`
            : ""
        }
        <label>Away target (${this._scheduleTemperatureUnit()})<input type="number" min="${MIN_SCHEDULE_TEMPERATURE}" max="${MAX_SCHEDULE_TEMPERATURE}" step="0.5" data-away-field="temperature" value="${this._escape(
          this._awayTemperature
        )}" /><small>Default 12°C. Only active rooms receive this target.</small></label>
      </div>
      <aside class="away-precedence"><strong>Control priority</strong><p>Zone Manual Override remains the highest authority for heating actuators. For room temperatures: Away mode, then Quick Change, then a manual thermostat adjustment until the next transition, then the weekly schedule.</p></aside>
      <div class="away-save-row"><span class="away-save-state">${
        this._awayDirty ? "Unsaved Away changes" : "Away settings are up to date"
      }</span><div><button class="text-button" data-away-action="discard" ${
        !this._awayDirty || this._awaySaving ? "disabled" : ""
      }>Discard</button><button class="primary" data-away-action="save" ${
        !this._awayDirty || this._awaySaving ? "disabled" : ""
      }>${this._awaySaving ? "Saving…" : "Save Away settings"}</button></div></div>
    </section>`;
  }

  _setupZone(zone, zoneIndex, unassignedAreas) {
    const selectedSwitches = new Set(
      this._draft
        .filter((_candidate, index) => index !== zoneIndex)
        .map((candidate) => candidate.switch)
        .filter(Boolean)
    );
    const switchOptions = (this._configuration.catalog.switches || []).filter(
      (item) => !selectedSwitches.has(item.entity_id) || item.entity_id === zone.switch
    );
    return `<article class="setup-zone">
      <div class="setup-zone-title"><h3>Zone ${zoneIndex + 1}</h3><button class="danger-link" data-action="remove-zone" data-zone="${zoneIndex}">Remove zone</button></div>
      <div class="form-grid">
        <label>Zone name<input data-zone="${zoneIndex}" data-zone-field="name" value="${this._escape(
          zone.name
        )}" placeholder="e.g. Ground Floor" /></label>
        <label>Heating actuator switch<select data-zone="${zoneIndex}" data-zone-field="switch">
          <option value="">Not selected</option>${switchOptions
            .map(
              (item) =>
                `<option value="${this._escape(item.entity_id)}" ${
                  item.entity_id === zone.switch ? "selected" : ""
                }>${this._escape(this._entityLabel(item))}</option>`
            )
            .join("")}
        </select></label>
        <label>Heat source<select data-zone="${zoneIndex}" data-zone-field="heat_source">
          ${Object.entries(HEAT_SOURCES)
            .map(
              ([value, label]) =>
                `<option value="${value}" ${zone.heat_source === value ? "selected" : ""}>${this._escape(
                  label
                )}</option>`
            )
            .join("")}
        </select><small>Suggested delay: ${HEAT_SOURCE_DELAYS[zone.heat_source] ?? 300} seconds.</small></label>
        <label>Re-enable delay (seconds)<input type="number" min="0" max="3600" step="15" data-zone="${zoneIndex}" data-zone-field="reenable_delay" value="${Number(
          zone.reenable_delay ?? 300
        )}" /><small>Minimum off time before this zone may turn on again.</small></label>
      </div>
      <div class="rooms-heading"><div><h4>Rooms</h4><p>Each room is a Home Assistant Area.</p></div>
        <select class="add-room" data-action="add-room" data-zone="${zoneIndex}" ${
          unassignedAreas.length ? "" : "disabled"
        }><option value="">${unassignedAreas.length ? "+ Add Area as room" : "All Areas are assigned"}</option>${unassignedAreas
          .map(
            (area) =>
              `<option value="${this._escape(area.area_id)}">${this._escape(area.name)}</option>`
          )
          .join("")}</select>
      </div>
      ${this._zoneThermostatBoundary(zone)}
      <div class="room-editors">${
        (zone.rooms || []).length
          ? zone.rooms
              .map((room, roomIndex) => this._setupRoom(room, zoneIndex, roomIndex))
              .join("")
          : `<div class="room-empty">No Areas assigned. Choose an Area above.</div>`
      }</div>
    </article>`;
  }

  _zoneThermostatBoundary(zone) {
    const targets = (zone.rooms || [])
      .map((room) => ({ room, thermostat: this._zealThermostat(room.room_id) }))
      .filter(({ thermostat }) => thermostat);
    return `<aside class="control-boundary">
      <div><strong>Zone/Floor scheduling targets</strong><p>ZEAL schedules only its own canonical room thermostats. These are created automatically and cannot be selected as physical room equipment.</p></div>
      ${
        targets.length
          ? `<ul>${targets
              .map(
                ({ room, thermostat }) =>
                  `<li><span>${this._escape(room.name || this._areaName(room.room_id))}</span><code>${this._escape(
                    thermostat.entity_id
                  )}</code></li>`
              )
              .join("")}</ul>`
          : `<p class="muted">ZEAL room thermostats appear here after rooms with physical thermostats are saved.</p>`
      }
    </aside>`;
  }

  _setupRoom(room, zoneIndex, roomIndex) {
    const trvs = (this._configuration.catalog.physical_room_thermostats || []).filter(
      (item) => item.area_id === room.room_id
    );
    const sensors = (this._configuration.catalog.temperature_sensors || []).filter(
      (item) => item.area_id === room.room_id
    );
    const openingSensors = (this._configuration.catalog.opening_sensors || []).filter(
      (item) => item.area_id === room.room_id
    );
    const zealThermostat = this._zealThermostat(room.room_id);
    const calendars = this._configuration.catalog.calendars || [];
    return `<section class="room-editor">
      <div class="room-editor-title"><div><h4>${this._escape(
        this._areaName(room.room_id)
      )}</h4><p>Home Assistant Area</p></div><button class="danger-link" data-action="remove-room" data-zone="${zoneIndex}" data-room="${roomIndex}">Remove</button></div>
      <label class="active-toggle"><input type="checkbox" data-zone="${zoneIndex}" data-room="${roomIndex}" data-room-field="active" ${
        room.active === false ? "" : "checked"
      } /><span><strong>Room is active</strong><small>Inactive rooms do not request heat.</small></span></label>
      <div class="occupancy-config">
        <label class="active-toggle"><input type="checkbox" data-zone="${zoneIndex}" data-room="${roomIndex}" data-room-field="occupancy_enabled" ${
          room.occupancy_enabled === true ? "checked" : ""
        } /><span><strong>Use occupancy setback</strong><small>The room stays active, but uses a lower target while unoccupied.</small></span></label>
        ${room.occupancy_enabled === true ? `<div class="form-grid">
          <label>Unoccupied target (${this._scheduleTemperatureUnit()})<input type="number" min="${MIN_SCHEDULE_TEMPERATURE}" max="${MAX_SCHEDULE_TEMPERATURE}" step="0.5" data-zone="${zoneIndex}" data-room="${roomIndex}" data-room-field="unoccupied_temperature" value="${this._escape(room.unoccupied_temperature ?? 12)}" /></label>
          <label>Occupancy source<select data-zone="${zoneIndex}" data-room="${roomIndex}" data-room-field="occupancy_source"><option value="manual" ${room.occupancy_source !== "calendar" ? "selected" : ""}>Manual</option><option value="calendar" ${room.occupancy_source === "calendar" ? "selected" : ""}>Home Assistant Calendar</option></select></label>
          ${room.occupancy_source === "calendar" ? `<label>Occupancy calendar<select data-zone="${zoneIndex}" data-room="${roomIndex}" data-room-field="occupancy_calendar"><option value="">Choose a calendar</option>${calendars.map((calendar) => `<option value="${this._escape(calendar.entity_id)}" ${calendar.entity_id === room.occupancy_calendar ? "selected" : ""}>${this._escape(this._entityLabel(calendar))}</option>`).join("")}</select><small>Calendar on means occupied. A manual choice in Overrides takes precedence until Follow calendar is selected.</small></label>` : ""}
        </div>` : ""}
      </div>
      <div class="canonical-target"><ha-icon icon="mdi:thermostat"></ha-icon><div><strong>ZEAL room thermostat</strong><span>${this._escape(
        zealThermostat
          ? this._entityLabel(zealThermostat)
          : "Created automatically after this room is saved with a physical thermostat"
      )}</span><small>This is the room's canonical scheduling target. It is never offered below as a physical thermostat.</small></div></div>
      <div class="equipment-grid">
        ${this._multiSelect(
          "Physical room thermostats / TRVs",
          trvs,
          room.trvs || [],
          zoneIndex,
          roomIndex,
          "trvs",
          "Choose every physical climate entity in this Area that ZEAL should control."
        )}
        ${this._multiSelect(
          "Temperature sensors",
          sensors,
          room.sensors || [],
          zoneIndex,
          roomIndex,
          "sensors",
          "Choose the Area sensors ZEAL should use to measure room temperature."
        )}
        ${this._multiSelect(
          "Window / door sensors",
          openingSensors,
          room.opening_sensors || [],
          zoneIndex,
          roomIndex,
          "opening_sensors",
          "While any selected contact is open, this room is shown as window/door open and does not request heat. ZEAL does not change its thermostat target."
        )}
      </div>
    </section>`;
  }

  _multiSelect(label, items, selected, zoneIndex, roomIndex, field, help) {
    const selectedSet = new Set(selected);
    return `<label>${label}<select multiple size="${Math.max(2, Math.min(5, items.length || 2))}" data-zone="${zoneIndex}" data-room="${roomIndex}" data-room-field="${field}" ${
      items.length ? "" : "disabled"
    }>${items
      .map(
        (item) =>
          `<option value="${this._escape(item.entity_id)}" ${
            selectedSet.has(item.entity_id) ? "selected" : ""
          }>${this._escape(this._entityLabel(item))}</option>`
      )
      .join("")}</select><small>${
      items.length ? this._escape(help) : "No eligible entities are assigned to this Area in Home Assistant."
    }</small></label>`;
  }

  _bindEvents() {
    this.shadowRoot.querySelectorAll("[data-view]").forEach((button) => {
      button.addEventListener("click", async () => {
        const next = button.dataset.view;
        if (next === "setup" && !this._isAdmin()) {
          this._view = "overview";
          this._error = "Setup is available only to Home Assistant administrators.";
          this._render();
          return;
        }
        if (
          (next === "schedule" && !this._canUseSchedule()) ||
          (next === "quick" && !this._canUseQuickChange()) ||
          (next === "learning" && !this._canUseLearning())
        ) {
          this._view = "overview";
          this._error = "Your Home Assistant administrator has not enabled this ZEAL feature for standard users.";
          this._render();
          return;
        }
        if (next === this._view) return;
        if (this._view === "quick" && next !== "quick" && this._awayDirty && !window.confirm("Discard unsaved Away changes?")) return;
        if ((this._dirty || this._awayDirty) && this._view === "setup" && !window.confirm("Discard unsaved setup or Away changes?")) return;
        if (this._scheduleDirty && this._view === "schedule" && !window.confirm("Discard unsaved schedule changes?")) return;
        this._view = next;
        this._updateZoneControlTimer();
        this._dirty = false;
        this._draft = this._copy(this._configuration.zones || []);
        this._showInSidebar = this._configuration.show_in_sidebar !== false;
        this._learningEnabled = this._configuration.learning_enabled === true;
        this._learningPersistentNotifications = this._configuration.learning_persistent_notifications !== false;
        this._loadAwayDraft();
        if (next === "schedule" || next === "overview") {
          await this._loadConfiguration({ preserveNotice: true });
          return;
        }
        this._scheduleDirty = false;
        this._notice = "";
        this._error = "";
        if (next === "quick") {
          await this._loadQuickChange();
          return;
        }
        if (next === "learning") {
          await this._loadLearning();
          return;
        }
        this._render();
      });
    });
    this.shadowRoot.querySelector('[data-action="reload-all"]')?.addEventListener("click", () => {
      this._started = false;
      this._initialLoad();
    });
    this.shadowRoot.querySelector('[data-action="select-entry"]')?.addEventListener("change", async (event) => {
      if ((this._dirty || this._scheduleDirty || this._awayDirty) && !window.confirm("Discard unsaved changes?")) {
        event.target.value = this._entryId;
        return;
      }
      this._entryId = event.target.value;
      await this._loadConfiguration();
    });
    this.shadowRoot.querySelector('[data-schedule-action="zone"]')?.addEventListener("change", (event) => {
      if (this._scheduleDirty && !window.confirm("Discard unsaved schedule changes?")) {
        event.target.value = this._scheduleZoneId;
        return;
      }
      this._scheduleZoneId = event.target.value;
      this._scheduleRoomId = null;
      this._loadScheduleRoom();
      this._notice = "";
      this._error = "";
      this._render();
    });
    this.shadowRoot.querySelector('[data-schedule-action="room"]')?.addEventListener("change", (event) => {
      if (this._scheduleDirty && !window.confirm("Discard unsaved schedule changes?")) {
        event.target.value = this._scheduleRoomId;
        return;
      }
      this._scheduleRoomId = event.target.value;
      this._loadScheduleRoom({ keepSelection: true });
      this._notice = "";
      this._error = "";
      this._render();
    });
    this.shadowRoot.querySelectorAll('[data-schedule-action="source-day"]').forEach((control) => {
      control.addEventListener("change", () => {
        this._scheduleSourceDay = control.value;
        this._scheduleTargetDays.delete(control.value);
        this._render();
      });
    });
    this.shadowRoot.querySelectorAll('[data-schedule-action="target-day"]').forEach((control) => {
      control.addEventListener("change", () => {
        if (control.checked) this._scheduleTargetDays.add(control.value);
        else this._scheduleTargetDays.delete(control.value);
        this._render();
      });
    });
    this.shadowRoot.querySelector('[data-schedule-action="toggle-target-days"]')?.addEventListener("click", () => this._toggleScheduleTargetDays());
    this.shadowRoot.querySelectorAll("[data-schedule-field]").forEach((control) => {
      control.addEventListener("change", () => {
        const period =
          this._scheduleDays[control.dataset.scheduleDay][
            Number(control.dataset.scheduleIndex)
          ];
        const field = control.dataset.scheduleField;
        period[field] = field === "temperature" ? Number(control.value) : control.value;
        if (field === "name") period.friendly_name = this._slug(control.value);
        this._markScheduleChanged();
      });
    });
    this.shadowRoot.querySelectorAll('[data-schedule-action="copy-target"]').forEach((control) => {
      control.addEventListener("change", () => {
        if (control.checked) this._scheduleCopyTargets.add(control.value);
        else this._scheduleCopyTargets.delete(control.value);
        const copyButton = this.shadowRoot.querySelector(
          '[data-schedule-action="copy-schedule"]'
        );
        if (copyButton) copyButton.disabled = !this._scheduleCopyTargets.size;
      });
    });
    this.shadowRoot.querySelectorAll('[data-schedule-action="timeline-point"]').forEach((button) => {
      button.addEventListener("click", () => {
        this._scheduleSelectedPeriod = {
          day: button.dataset.day,
          index: Number(button.dataset.index),
        };
        this._render();
      });
    });
    this.shadowRoot.querySelectorAll('[data-schedule-action="add-period"]').forEach((button) => {
      button.addEventListener("click", () => this._addSchedulePeriod(button.dataset.day));
    });
    this.shadowRoot.querySelectorAll('[data-schedule-action="remove-period"]').forEach((button) => {
      button.addEventListener("click", () => {
        this._scheduleDays[button.dataset.day].splice(Number(button.dataset.index), 1);
        this._scheduleSelectedPeriod = null;
        this._markScheduleChanged(true);
      });
    });
    this.shadowRoot.querySelector('[data-schedule-action="apply-days"]')?.addEventListener("click", () => this._applyScheduleDays());
    this.shadowRoot.querySelector('[data-schedule-action="toggle-copy-targets"]')?.addEventListener("click", () => this._toggleScheduleCopyTargets());
    this.shadowRoot.querySelector('[data-schedule-action="copy-schedule"]')?.addEventListener("click", () => this._copySchedule());
    this.shadowRoot.querySelector('[data-schedule-action="discard"]')?.addEventListener("click", () => {
      this._loadScheduleRoom({ keepSelection: true });
      this._notice = "";
      this._error = "";
      this._render();
    });
    this.shadowRoot.querySelector('[data-schedule-action="save"]')?.addEventListener("click", () => this._saveSchedule());
    this.shadowRoot.querySelectorAll('[data-quick-action="room"]').forEach((control) => {
      control.addEventListener("change", () => {
        if (control.checked) this._quickSelected.add(control.value);
        else this._quickSelected.delete(control.value);
        this._syncQuickExactTarget();
        this._render();
      });
    });
    this.shadowRoot.querySelectorAll('[data-quick-action="duration"]').forEach((control) => {
      control.addEventListener("change", () => {
        this._quickDuration = control.value;
      });
    });
    this.shadowRoot.querySelector('[data-quick-action="temperature"]')?.addEventListener("change", (event) => {
      this._quickExactTarget = event.target.value;
      this._quickAction = event.target.value === ""
        ? null
        : { operation: "temperature", value: Number(event.target.value) };
      const applyButton = this.shadowRoot.querySelector(
        '[data-quick-action="apply"]'
      );
      const actionState = this.shadowRoot.querySelector(".quick-action-state");
      if (applyButton) {
        applyButton.disabled =
          !this._quickSelected.size || !this._quickAction || this._quickSaving;
      }
      if (actionState) {
        actionState.textContent =
          this._quickActionDescription() || "Choose a temperature change.";
      }
    });
    this.shadowRoot.querySelectorAll('[data-quick-action="delta"]').forEach((button) => {
      button.addEventListener("click", () => {
        const step = Number(button.dataset.value);
        if (this._quickAction?.operation === "temperature") {
          this._quickAction = {
            operation: "temperature",
            value: this._quickAction.value + step,
          };
        } else {
          this._quickAction = {
            operation: "delta",
            value:
              (this._quickAction?.operation === "delta"
                ? this._quickAction.value
                : 0) + step,
          };
        }
        this._syncQuickExactTarget();
        this._render();
      });
    });
    this.shadowRoot.querySelector('[data-quick-action="whole-house"]')?.addEventListener("click", () => {
      this._toggleQuickSelection(this._quickRooms().map((room) => room.room_id));
    });
    this.shadowRoot.querySelectorAll('[data-quick-action="toggle-zone"]').forEach((button) => {
      button.addEventListener("click", () => {
        const zone = (this._configuration?.zones || []).find(
          (candidate) => candidate.zone_id === button.dataset.zoneId
        );
        this._toggleQuickSelection(zone ? this._quickRoomIdsInZone(zone) : []);
      });
    });
    this.shadowRoot.querySelector('[data-quick-action="refresh"]')?.addEventListener("click", () => this._loadQuickChange());
    this.shadowRoot.querySelector('[data-quick-action="apply"]')?.addEventListener("click", () => this._applyQuickChange());
    this.shadowRoot.querySelectorAll('[data-quick-action="clear-hold"]').forEach((button) => {
      button.addEventListener("click", () => this._clearQuickHold(button.dataset.roomId));
    });
    this.shadowRoot.querySelectorAll("[data-occupancy-action]").forEach((button) => {
      button.addEventListener("click", () => this._setRoomOccupancy(
        button.dataset.roomId,
        button.dataset.occupancyAction === "follow"
          ? null
          : button.dataset.occupied === "true"
      ));
    });
    this.shadowRoot.querySelectorAll("[data-away-field]").forEach((control) => {
      control.addEventListener("change", () => {
        const field = control.dataset.awayField;
        if (field === "mode") this._awayMode = control.value;
        else if (field === "calendar_entity_id") this._awayCalendarId = control.value;
        else if (field === "starts_at") this._awayStartsAt = control.value;
        else if (field === "ends_at") this._awayEndsAt = control.value;
        else if (field === "temperature") this._awayTemperature = Number(control.value);
        this._markAwayChanged();
      });
    });
    this.shadowRoot.querySelector('[data-away-action="discard"]')?.addEventListener("click", () => {
      this._loadAwayDraft();
      this._notice = "";
      this._error = "";
      this._render();
    });
    this.shadowRoot.querySelector('[data-away-action="save"]')?.addEventListener("click", () => this._saveAwayMode());
    this.shadowRoot.querySelector('[data-away-action="end"]')?.addEventListener("click", async () => {
      if (!window.confirm("End Away mode now and resume normal temperature control?")) return;
      await this._saveAwayMode({ forceOff: true });
    });
    this.shadowRoot.querySelector('[data-download-action="configuration"]')?.addEventListener("click", () => this._downloadConfiguration());
    this.shadowRoot.querySelector('[data-download-action="audit"]')?.addEventListener("click", () => this._downloadAuditTrail());
    this.shadowRoot.querySelectorAll("[data-zone-field]").forEach((control) => {
      control.addEventListener("change", () => {
        const zone = this._draft[Number(control.dataset.zone)];
        const field = control.dataset.zoneField;
        zone[field] = field === "reenable_delay" ? Number(control.value) : control.value || null;
        this._markChanged();
      });
    });
    this.shadowRoot.querySelectorAll("[data-room-field]").forEach((control) => {
      control.addEventListener("change", () => {
        const room = this._draft[Number(control.dataset.zone)].rooms[Number(control.dataset.room)];
        const field = control.dataset.roomField;
        room[field] =
          field === "active" || field === "occupancy_enabled"
            ? control.checked
            : field === "unoccupied_temperature"
              ? Number(control.value)
              : field === "occupancy_source"
                ? control.value
                : field === "occupancy_calendar"
                  ? control.value || null
                  : Array.from(control.selectedOptions).map((option) => option.value);
        this._markChanged();
      });
    });
    this.shadowRoot.querySelectorAll('[data-action="add-room"]').forEach((control) => {
      control.addEventListener("change", () => this._addRoom(control));
    });
    this.shadowRoot.querySelectorAll('[data-action="remove-room"]').forEach((button) => {
      button.addEventListener("click", () => this._removeRoom(button));
    });
    this.shadowRoot.querySelectorAll('[data-action="remove-zone"]').forEach((button) => {
      button.addEventListener("click", () => this._removeZone(button));
    });
    this.shadowRoot.querySelectorAll('[data-action="add-zone"]').forEach((button) => {
      button.addEventListener("click", () => this._addZone());
    });
    this.shadowRoot.querySelector('[data-action="reset"]')?.addEventListener("click", () => {
      this._draft = this._copy(this._configuration.zones || []);
      this._showInSidebar = this._configuration.show_in_sidebar !== false;
      this._standardUserSchedule = this._configuration.standard_user_schedule === true;
      this._standardUserQuickChange = this._configuration.standard_user_quick_change === true;
      this._learningEnabled = this._configuration.learning_enabled === true;
      this._learningPersistentNotifications = this._configuration.learning_persistent_notifications !== false;
      this._dirty = false;
      this._error = "";
      this._render();
    });
    this.shadowRoot.querySelector('[data-action="sidebar-toggle"]')?.addEventListener("change", (event) => {
      this._showInSidebar = event.target.checked;
      this._markChanged();
    });
    this.shadowRoot.querySelector('[data-action="standard-user-schedule"]')?.addEventListener("change", (event) => {
      this._standardUserSchedule = event.target.checked;
      this._markChanged();
    });
    this.shadowRoot.querySelector('[data-action="standard-user-quick-change"]')?.addEventListener("change", (event) => {
      this._standardUserQuickChange = event.target.checked;
      this._markChanged();
    });
    this.shadowRoot.querySelector('[data-action="learning-enabled"]')?.addEventListener("change", (event) => {
      this._learningEnabled = event.target.checked;
      this._markChanged();
    });
    this.shadowRoot.querySelector('[data-action="learning-persistent-notifications"]')?.addEventListener("change", (event) => {
      this._learningPersistentNotifications = event.target.checked;
      this._markChanged();
    });
    this.shadowRoot.querySelectorAll('[data-learning-action]')?.forEach((button) => {
      button.addEventListener("click", () => this._handleLearningAction(button));
    });
    this.shadowRoot.querySelector('[data-action="manage-instances"]')?.addEventListener("click", () => {
      window.location.assign("/config/integrations/integration/zeal");
    });
    this.shadowRoot.querySelector('[data-action="delete-instance"]')?.addEventListener("click", () => this._deleteInstance());
    this.shadowRoot.querySelector('[data-action="save"]')?.addEventListener("click", () => this._save());
  }

  _markChanged() {
    this._dirty = true;
    this._notice = "";
    this._error = "";
    this._render();
  }

  _markAwayChanged() {
    this._awayDirty = true;
    this._notice = "";
    this._error = "";
    this._render();
  }

  async _deleteInstance() {
    if (this._deleting || !this._entryId) return;
    const entry = this._entries.find((item) => item.entry_id === this._entryId);
    const title = entry?.title || this._configuration?.title || "this ZEAL instance";
    if (!window.confirm(`Permanently delete ${title}?\n\nThis removes its zones, schedules, Away settings and audit trail. Other ZEAL instances are not removed. This cannot be undone.`)) return;
    this._deleting = true;
    this._error = "";
    this._render();
    try {
      await this._hass.callApi(
        "delete",
        `config/config_entries/entry/${encodeURIComponent(this._entryId)}`
      );
      this._entries = this._entries.filter((item) => item.entry_id !== this._entryId);
      if (!this._entries.length) {
        window.location.assign("/config/integrations");
        return;
      }
      this._entryId = this._entries[0].entry_id;
      this._configuration = null;
      this._deleting = false;
      this._notice = `${title} was deleted. Other ZEAL instances were not changed.`;
      await this._loadConfiguration({ preserveNotice: true });
    } catch (error) {
      this._deleting = false;
      this._error = this._message(error, `${title} could not be deleted.`);
      this._render();
    }
  }

  async _saveAwayMode({ forceOff = false } = {}) {
    if (this._awaySaving || (!this._awayDirty && !forceOff)) return;
    if (this._dirty && !forceOff) {
      this._error = "Save or discard the room and zone setup changes before saving Away settings.";
      this._render();
      return;
    }
    const mode = forceOff ? "off" : this._awayMode;
    const temperature = forceOff
      ? Number(this._configuration?.away_mode?.temperature ?? 12)
      : this._awayTemperature;
    const hierarchyDraft = forceOff && this._dirty ? this._copy(this._draft) : null;
    if (mode === "calendar" && !this._awayCalendarId) {
      this._error = "Choose a Home Assistant calendar for Away mode.";
      this._render();
      return;
    }
    if (mode === "date_range" && (!this._awayStartsAt || !this._awayEndsAt)) {
      this._error = "Choose both the Away start and end date/time.";
      this._render();
      return;
    }
    if (
      !Number.isFinite(temperature) ||
      temperature < MIN_SCHEDULE_TEMPERATURE ||
      temperature > MAX_SCHEDULE_TEMPERATURE
    ) {
      this._error = `Away target must be from ${MIN_SCHEDULE_TEMPERATURE} to ${MAX_SCHEDULE_TEMPERATURE}${this._scheduleTemperatureUnit()}.`;
      this._render();
      return;
    }
    this._awaySaving = true;
    this._notice = "";
    this._error = "";
    this._render();
    try {
      const response = await this._hass.callWS({
        type: "zeal/save_away_mode",
        entry_id: this._entryId,
        expected_revision: this._configuration.revision,
        mode,
        calendar_entity_id: mode === "calendar" ? this._awayCalendarId : null,
        starts_at: mode === "date_range" ? this._awayStartsAt : null,
        ends_at: mode === "date_range" ? this._awayEndsAt : null,
        temperature,
      });
      this._configuration = response;
      this._draft = hierarchyDraft || this._copy(response.zones || []);
      this._acceptQuickChange(response.quick_change || { rooms: [] });
      this._loadAwayDraft();
      this._notice = forceOff
        ? "Away ended. Normal temperature control has resumed."
        : response.away_mode?.active
        ? "Away settings saved and the Away target is active."
        : "Away settings saved.";
    } catch (error) {
      this._awaySaving = false;
      const conflict = error?.code === "conflict" || error?.body?.code === "conflict";
      this._error = conflict
        ? "ZEAL changed in another browser or process. Reload the latest settings before trying again."
        : this._message(error, "Away settings could not be saved.");
    }
    this._render();
  }

  _addZone() {
    const id = globalThis.crypto?.randomUUID
      ? globalThis.crypto.randomUUID()
      : `zone-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    this._draft.push({
      zone_id: id,
      name: `Zone ${this._draft.length + 1}`,
      switch: null,
      heat_source: "ashp",
      reenable_delay: 300,
      ashp_capability: "heat_only",
      rooms: [],
    });
    this._markChanged();
  }

  _removeZone(button) {
    const index = Number(button.dataset.zone);
    const zone = this._draft[index];
    if (!window.confirm(`Remove ${zone.name} and its room assignments?`)) return;
    this._draft.splice(index, 1);
    this._markChanged();
  }

  _addRoom(control) {
    const areaId = control.value;
    if (!areaId) return;
    const area = this._configuration.catalog.areas.find((item) => item.area_id === areaId);
    this._draft[Number(control.dataset.zone)].rooms.push({
      room_id: areaId,
      name: area?.name || areaId,
      trvs: [],
      sensors: [],
      opening_sensors: [],
      active: true,
      occupancy_enabled: false,
      unoccupied_temperature: 12,
      occupancy_source: "manual",
      occupancy_calendar: null,
      cooling_capable: false,
    });
    this._markChanged();
  }

  _removeRoom(button) {
    const zone = this._draft[Number(button.dataset.zone)];
    const roomIndex = Number(button.dataset.room);
    const room = zone.rooms[roomIndex];
    if (!window.confirm(`Remove ${room.name || this._areaName(room.room_id)} from this zone?`)) return;
    zone.rooms.splice(roomIndex, 1);
    this._markChanged();
  }

  async _handleLearningAction(button) {
    const proposal = (this._learning.proposals || []).find(
      (item) => item.proposal_id === button.dataset.proposalId
    );
    if (!proposal) return;
    const action = button.dataset.learningAction;
    if (action === "schedule") {
      this._view = "schedule";
      this._scheduleRoomId = proposal.room_id;
      const zone = (this._configuration.zones || []).find((item) =>
        (item.rooms || []).some((room) => room.room_id === proposal.room_id)
      );
      this._scheduleZoneId = zone?.zone_id || null;
      this._loadScheduleRoom({ keepSelection: true });
      this._render();
      return;
    }
    const payload = {
      type: "zeal/decide_learning_proposal",
      entry_id: this._entryId,
      proposal_id: proposal.proposal_id,
      action: action === "edit" ? "edit_accept" : action,
    };
    if (action === "accept" && !window.confirm(
      `Apply ${proposal.proposed_time} · ${this._formatScheduleTemperature(proposal.proposed_temperature)} to ${proposal.weekday}?`
    )) return;
    if (action === "edit") {
      const proposedTime = window.prompt("Proposed start time (HH:MM)", proposal.proposed_time);
      if (proposedTime === null) return;
      const proposedTemperature = window.prompt("Proposed temperature", proposal.proposed_temperature);
      if (proposedTemperature === null) return;
      payload.proposed_time = proposedTime;
      payload.proposed_temperature = Number(proposedTemperature);
      if (!Number.isFinite(payload.proposed_temperature)) {
        this._error = "The proposed temperature must be a number.";
        this._render();
        return;
      }
      if (!window.confirm(
        `Apply ${proposedTime} · ${this._formatScheduleTemperature(payload.proposed_temperature)} to ${proposal.weekday}?`
      )) return;
    }
    if (action === "dismiss" && !window.confirm("Dismiss this learning suggestion?")) return;
    if (action === "revert" && !window.confirm("Revert this accepted schedule change?")) return;
    if (action === "snooze") {
      payload.snoozed_until = new Date(Date.now() + 7 * 24 * 60 * 60 * 1_000).toISOString();
    }
    try {
      this._learning = await this._hass.callWS(payload);
      this._notice = action === "accept" || action === "edit"
        ? "Schedule Adaptation accepted and committed."
        : action === "revert"
          ? "Accepted Schedule Adaptation reverted."
        : action === "dismiss"
          ? "Learning suggestion dismissed."
          : "Learning suggestion snoozed for 7 days.";
      if (action === "accept" || action === "edit" || action === "revert") {
        await this._loadConfiguration({ preserveNotice: true });
        this._view = "learning";
        await this._loadLearning();
        return;
      }
      this._render();
    } catch (error) {
      this._error = this._message(error, "The Learning decision could not be saved.");
      this._render();
    }
  }

  async _save() {
    if (this._saving || !this._dirty) return;
    if (this._awayDirty) {
      this._error = "Save or discard the Away settings before saving room and zone setup changes.";
      this._render();
      return;
    }
    const invalidName = this._draft.find((zone) => !String(zone.name || "").trim());
    if (invalidName) {
      this._error = "Every zone needs a name.";
      this._render();
      return;
    }
    const invalidDelay = this._draft.find(
      (zone) => !Number.isInteger(Number(zone.reenable_delay)) || Number(zone.reenable_delay) < 0 || Number(zone.reenable_delay) > 3600
    );
    if (invalidDelay) {
      this._error = "Every re-enable delay must be a whole number from 0 to 3600 seconds.";
      this._render();
      return;
    }
    const invalidOccupancy = this._draft
      .flatMap((zone) => zone.rooms || [])
      .find((room) => room.occupancy_enabled === true && (
        !Number.isFinite(Number(room.unoccupied_temperature)) ||
        Number(room.unoccupied_temperature) < MIN_SCHEDULE_TEMPERATURE ||
        Number(room.unoccupied_temperature) > MAX_SCHEDULE_TEMPERATURE ||
        (room.occupancy_source === "calendar" && !room.occupancy_calendar)
      ));
    if (invalidOccupancy) {
      this._error = `Each occupancy room needs a valid setback from ${MIN_SCHEDULE_TEMPERATURE} to ${MAX_SCHEDULE_TEMPERATURE}${this._scheduleTemperatureUnit()} and a calendar when calendar control is selected.`;
      this._render();
      return;
    }
    this._saving = true;
    this._error = "";
    this._render();
    try {
      const response = await this._hass.callWS({
        type: "zeal/save_hierarchy",
        entry_id: this._entryId,
        expected_revision: this._configuration.revision,
        zones: this._draft,
        show_in_sidebar: this._showInSidebar,
        standard_user_schedule: this._standardUserSchedule,
        standard_user_quick_change: this._standardUserQuickChange,
        learning_enabled: this._learningEnabled,
        learning_persistent_notifications: this._learningPersistentNotifications,
      });
      this._configuration.zones = this._copy(response.zones);
      this._configuration.revision = response.revision;
      this._configuration.show_in_sidebar = response.show_in_sidebar;
      this._configuration.standard_user_schedule = response.standard_user_schedule;
      this._configuration.standard_user_quick_change = response.standard_user_quick_change;
      this._configuration.learning_enabled = response.learning_enabled;
      this._configuration.learning_persistent_notifications = response.learning_persistent_notifications;
      this._showInSidebar = response.show_in_sidebar;
      this._standardUserSchedule = response.standard_user_schedule;
      this._standardUserQuickChange = response.standard_user_quick_change;
      this._learningEnabled = response.learning_enabled;
      this._learningPersistentNotifications = response.learning_persistent_notifications;
      this._draft = this._copy(response.zones);
      this._dirty = false;
      this._notice = "Setup saved. ZEAL is reloading the updated configuration.";
      await this._reloadAfterSave(response.revision);
    } catch (error) {
      this._saving = false;
      const conflict = error?.code === "conflict" || error?.body?.code === "conflict";
      this._error = conflict
        ? "ZEAL changed in another browser or process. Reload the latest setup before trying again."
        : this._message(error, "Setup could not be saved.");
      this._render();
    }
  }

  async _reloadAfterSave(expectedRevision) {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 350));
      try {
        const configuration = await this._hass.callWS({
          type: "zeal/get_configuration",
          entry_id: this._entryId,
        });
        if (configuration.revision === expectedRevision) {
          this._saving = false;
          this._acceptConfiguration(configuration);
          return;
        }
      } catch (_error) {
        // A short not-loaded window is expected while Home Assistant reloads.
      }
    }
    this._saving = false;
    this._notice = "Setup saved. Reload this page if the refreshed values do not appear shortly.";
    this._render();
  }

  _styles() {
    return `<style>
      :host { display:block; min-height:100%; color:var(--primary-text-color); background:var(--primary-background-color); font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
      * { box-sizing:border-box; }
      button, input, select { font:inherit; }
      main { padding:24px 18px 110px; }
      .shell { max-width:1500px; margin:0 auto; }
      header { display:flex; align-items:center; justify-content:space-between; gap:24px; margin-bottom:18px; }
      .identity { display:flex; align-items:center; gap:14px; }
      .identity img { width:52px; height:52px; object-fit:contain; }
      h1,h2,h3,h4,p { margin-top:0; }
      h1 { font-size:30px; line-height:1; margin:0 0 6px; letter-spacing:.02em; }
      .identity p, .page-heading p, .rooms-heading p, .room-editor-title p { color:var(--secondary-text-color); margin:0; }
      .entry-picker { min-width:230px; }
      nav { display:flex; gap:8px; margin-bottom:22px; border-bottom:1px solid var(--divider-color); }
      .tab { appearance:none; background:transparent; border:0; border-bottom:3px solid transparent; color:var(--secondary-text-color); cursor:pointer; padding:12px 18px; }
      .tab.active { border-bottom-color:var(--primary-color); color:var(--primary-color); font-weight:600; }
      .page-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin-bottom:18px; }
      .page-heading h2 { font-size:24px; margin-bottom:5px; }
      button { border-radius:6px; border:0; min-height:40px; padding:0 16px; cursor:pointer; }
      button:disabled { opacity:.5; cursor:not-allowed; }
      .primary, .secondary { background:var(--primary-color); color:var(--text-primary-color, white); }
      .secondary { background:var(--secondary-background-color); color:var(--primary-text-color); border:1px solid var(--divider-color); }
      .text-button, .danger-link { background:transparent; color:var(--primary-color); }
      .danger-link { color:var(--error-color); min-height:34px; padding:0 6px; }
      .safety-warning { display:flex; gap:13px; padding:16px; margin:0 0 20px; border:1px solid var(--warning-color, #f4b400); border-radius:10px; background:color-mix(in srgb, var(--warning-color, #f4b400) 12%, var(--card-background-color)); }
      .safety-warning p { margin:5px 0 0; line-height:1.45; color:var(--secondary-text-color); }
      .warning-icon { flex:0 0 26px; width:26px; height:26px; border-radius:50%; display:grid; place-items:center; font-weight:800; color:#111; background:var(--warning-color, #f4b400); }
      .away-banner { display:flex; align-items:center; gap:13px; padding:14px 16px; margin:0 0 18px; border:1px solid var(--divider-color); border-radius:10px; background:var(--card-background-color); }
      .away-banner.active { border-color:var(--primary-color); background:color-mix(in srgb, var(--primary-color) 10%, var(--card-background-color)); }
      .away-banner ha-icon { flex:none; color:var(--primary-color); --mdc-icon-size:28px; }
      .away-banner div { flex:1; }
      .away-banner p { margin:4px 0 0; color:var(--secondary-text-color); }
      .summary-grid { display:grid; grid-template-columns:repeat(3, 1fr); gap:14px; margin-bottom:18px; }
      .summary-card, .zone-card, .setup-zone, .empty-card, .setup-help { background:var(--card-background-color); border:1px solid var(--divider-color); box-shadow:var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,.08)); border-radius:12px; }
      .summary-card { display:flex; align-items:center; gap:14px; padding:18px; }
      .summary-card ha-icon { color:var(--primary-color); --mdc-icon-size:30px; }
      .summary-card strong { display:block; font-size:26px; line-height:1; }
      .summary-card span { display:block; margin-top:5px; color:var(--secondary-text-color); }
      .zone-grid { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:16px; }
      .zone-card { padding:18px; }
      .zone-title, .setup-zone-title, .room-editor-title, .rooms-heading { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; }
      .zone-title h3, .setup-zone-title h3, .room-editor-title h4, .rooms-heading h4 { margin-bottom:4px; }
      .zone-title p { color:var(--secondary-text-color); margin:0; }
      .pill, .state { white-space:nowrap; border-radius:999px; padding:5px 9px; font-size:12px; background:var(--secondary-background-color); }
      .state { color:var(--success-color, #2e7d32); }
      .state.inactive { color:var(--secondary-text-color); }
      .zone-demand { margin:14px 0 0; padding:11px; border:1px solid var(--divider-color); border-radius:10px; background:var(--secondary-background-color); }
      .actuator-status { display:flex; align-items:center; gap:9px; margin-bottom:9px; }
      .actuator-status > ha-icon { flex:none; --mdc-icon-size:27px; }
      .actuator-status span, .actuator-status strong { display:block; }
      .actuator-status span { color:var(--secondary-text-color); font-size:11px; }
      .actuator-status strong { margin-top:1px; font-size:13px; }
      .zone-demand-state { margin-left:auto; text-align:right; }
      .zone-demand-state.demanding strong { color:var(--warning-color, #ef6c00); }
      .zone-demand-state.satisfied strong { color:var(--success-color, #2e7d32); }
      .actuator-status.heating > ha-icon, .actuator-status.heating > div:first-of-type strong { color:var(--warning-color, #ef6c00); }
      .actuator-status.idle > ha-icon { color:var(--secondary-text-color); }
      .actuator-status.unknown > ha-icon, .actuator-status.unknown > div:first-of-type strong { color:var(--error-color); }
      .history-link { display:inline-flex; align-items:center; justify-content:center; flex:none; width:34px; height:34px; border-radius:50%; color:var(--primary-color); text-decoration:none; }
      .history-link:hover, .history-link:focus-visible { background:var(--card-background-color); outline:2px solid var(--primary-color); outline-offset:1px; }
      .history-link ha-icon { --mdc-icon-size:22px; }
      .actuator-explanation { margin:-2px 0 9px; color:var(--secondary-text-color); font-size:11px; line-height:1.35; }
      .demand-strip { display:flex; gap:8px; overflow-x:auto; overscroll-behavior-inline:contain; scrollbar-width:thin; padding:1px 0 5px; scroll-snap-type:x proximity; }
      .demand-chip { flex:0 0 auto; min-width:245px; display:grid; grid-template-columns:1fr auto auto; gap:3px 9px; padding:8px 10px; border-left:4px solid var(--divider-color); border-radius:7px; background:var(--card-background-color); scroll-snap-align:start; }
      .demand-chip strong { overflow:hidden; text-overflow:ellipsis; }
      .demand-chip span { grid-column:1 / 3; color:var(--secondary-text-color); font-size:11px; }
      .demand-chip .demand-context { font-size:10px; }
      .demand-chip .opening-open { color:var(--info-color, #039be5); }
      .demand-chip .opening-unavailable { color:var(--warning-color, #ef6c00); }
      .demand-chip em { grid-column:2; grid-row:1; align-self:center; font-size:11px; font-style:normal; font-weight:700; }
      .demand-chip .history-link { grid-column:3; grid-row:1 / span 4; align-self:center; }
      .demand-chip.demanding { border-left-color:var(--warning-color, #ef6c00); }
      .demand-chip.demanding em { color:var(--warning-color, #ef6c00); }
      .demand-chip.satisfied { border-left-color:var(--success-color, #2e7d32); }
      .demand-chip.satisfied em { color:var(--success-color, #2e7d32); }
      .demand-chip.suppressed { border-left-color:var(--info-color, #039be5); }
      .demand-chip.suppressed em { color:var(--info-color, #039be5); }
      .demand-chip.inactive, .demand-chip.unknown { opacity:.72; }
      .demand-chip.unknown { border-left-color:var(--error-color); }
      .demand-empty { color:var(--secondary-text-color); font-size:12px; }
      .zone-facts { margin:16px 0; padding:12px 0; border-top:1px solid var(--divider-color); border-bottom:1px solid var(--divider-color); }
      .zone-facts div { display:grid; grid-template-columns:125px minmax(0,1fr); gap:10px; padding:4px 0; }
      .learning-progress { margin-bottom:20px; padding:18px; border:1px solid var(--divider-color); border-left:4px solid var(--primary-color); border-radius:12px; background:var(--card-background-color); }
      .learning-progress h3, .learning-section-heading h3 { margin:0 0 5px; }
      .learning-progress p, .learning-section-heading p { margin:0; color:var(--secondary-text-color); }
      .learning-progress ul { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:8px; margin:14px 0 0; padding:0; list-style:none; }
      .learning-progress li { padding:10px; border-radius:8px; background:var(--secondary-background-color); }
      .learning-progress li strong, .learning-progress li span, .learning-progress li small { display:block; }
      .learning-progress li span { margin:3px 0; }
      .learning-progress li small, .learning-progress-empty { color:var(--secondary-text-color); }
      .learning-section-heading { margin:0 0 12px; }
      .learning-evidence-summary { color:var(--secondary-text-color); }
      .learning-day-scope { margin:14px 0; padding:12px; border-radius:9px; border:1px solid var(--divider-color); background:var(--secondary-background-color); }
      .learning-day-scope p { margin:5px 0; color:var(--secondary-text-color); }
      .learning-day-scope button { min-height:32px; padding:0; }
      dt { color:var(--secondary-text-color); }
      dd { margin:0; overflow-wrap:anywhere; }
      .room-list { display:grid; gap:8px; }
      .room-summary { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:10px; border-radius:8px; background:var(--secondary-background-color); }
      .room-summary strong, .room-summary span { display:block; }
      .room-summary div > span { margin-top:3px; font-size:12px; color:var(--secondary-text-color); }
      .room-summary .control-source { color:var(--primary-text-color); }
      .room-summary .control-source.temporary, .room-summary .control-source.manual { color:var(--warning-color, #ef6c00); font-weight:600; }
      .room-summary .control-source.away { color:var(--primary-color); font-weight:600; }
      .empty-card { padding:28px; text-align:center; }
      .empty-card > ha-icon { color:var(--primary-color); --mdc-icon-size:42px; margin-bottom:10px; }
      .empty-card p { color:var(--secondary-text-color); }
      .schedule-heading { align-items:flex-end; }
      .schedule-navigation { display:flex; gap:12px; min-width:min(100%, 500px); }
      .schedule-navigation label { flex:1; }
      .schedule-target { display:flex; align-items:flex-start; gap:11px; margin:0 0 18px; padding:14px; border-radius:10px; background:var(--card-background-color); border:1px solid var(--divider-color); }
      .schedule-target ha-icon { color:var(--primary-color); --mdc-icon-size:28px; }
      .schedule-target strong, .schedule-target span, .schedule-target small { display:block; }
      .schedule-target span { margin:3px 0; overflow-wrap:anywhere; }
      .schedule-week { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:14px; }
      .day-card, .schedule-actions-card { background:var(--card-background-color); border:1px solid var(--divider-color); box-shadow:var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,.08)); border-radius:12px; padding:15px; }
      .day-heading { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
      .day-heading h3 { margin:0 auto 0 0; }
      .day-heading label { display:flex; flex-direction:row; align-items:center; gap:4px; color:var(--secondary-text-color); font-size:12px; white-space:nowrap; }
      .day-heading input, .copy-targets input { width:auto; min-height:auto; margin:0; }
      .visual-editor { margin:0 0 14px; }
      .timeline-title { display:flex; justify-content:space-between; gap:8px; align-items:baseline; font-size:13px; font-weight:700; }
      .timeline-title span { color:var(--secondary-text-color); font-size:10px; font-weight:400; text-align:right; }
      .timeline-shell { display:grid; grid-template-columns:31px 1fr; gap:6px; margin-top:8px; }
      .temperature-scale { height:130px; display:flex; flex-direction:column; justify-content:space-between; align-items:flex-end; color:var(--secondary-text-color); font-size:10px; padding:1px 0; }
      .timeline-plot { height:130px; position:relative; overflow:visible; border-left:1px solid var(--divider-color); border-bottom:1px solid var(--divider-color); background:repeating-linear-gradient(90deg, transparent 0, transparent calc(25% - 1px), var(--divider-color) calc(25% - 1px), var(--divider-color) 25%), repeating-linear-gradient(0deg, transparent 0, transparent calc(25% - 1px), var(--divider-color) calc(25% - 1px), var(--divider-color) 25%); }
      .timeline-plot svg { position:absolute; inset:0; width:100%; height:100%; overflow:visible; pointer-events:none; }
      .timeline-plot path { fill:none; stroke:var(--primary-color); stroke-width:2; vector-effect:non-scaling-stroke; }
      .timeline-point { position:absolute; transform:translate(-50%, -50%); z-index:1; width:34px; min-width:34px; min-height:34px; height:34px; padding:0; border:2px solid var(--card-background-color); border-radius:50%; background:var(--primary-color); color:var(--text-primary-color, white); box-shadow:0 0 0 1px var(--primary-color); font-size:10px; font-weight:700; touch-action:none; cursor:grab; }
      .timeline-point:active { cursor:grabbing; }
      .time-scale { display:flex; justify-content:space-between; color:var(--secondary-text-color); font-size:10px; margin-top:4px; }
      .period-labels, .period-row { display:grid; grid-template-columns:minmax(80px,1fr) 80px 68px 14px 30px; gap:5px; align-items:center; }
      .period-labels { color:var(--secondary-text-color); font-size:11px; margin-bottom:4px; padding:0 3px; }
      .period-row { margin:7px 0; border-radius:6px; }
      .period-row.selected { outline:2px solid var(--primary-color); outline-offset:2px; }
      .period-row input { min-width:0; min-height:38px; padding:6px; }
      .period-remove { min-width:30px; min-height:36px; padding:0; background:transparent; color:var(--error-color); font-size:23px; }
      .add-period { width:100%; margin-top:8px; }
      .schedule-empty { padding:12px 8px; margin:8px 0; }
      .schedule-actions-card { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:center; margin-top:18px; }
      .schedule-actions-card h3 { margin-bottom:4px; }
      .schedule-actions-card p, .copy-schedule p { margin:0; color:var(--secondary-text-color); }
      .compact { width:auto; min-height:36px; }
      .copy-schedule { grid-column:1 / -1; padding:13px; border:1px solid var(--divider-color); border-radius:8px; }
      .copy-schedule summary { cursor:pointer; font-weight:700; }
      .copy-schedule > p { margin:8px 0; }
      .copy-targets { display:flex; gap:10px 20px; flex-wrap:wrap; margin:10px 0 13px; }
      .copy-targets fieldset { display:flex; gap:8px 14px; flex-wrap:wrap; min-width:220px; padding:8px 10px; border:1px solid var(--divider-color); border-radius:7px; }
      .copy-targets legend { color:var(--secondary-text-color); font-size:12px; }
      .copy-targets label { flex-direction:row; align-items:center; gap:5px; font-weight:400; }
      .schedule-save-bar { margin-top:18px; }
      .quick-heading { align-items:flex-end; }
      .quick-heading-actions, .download-actions { display:flex; gap:9px; flex-wrap:wrap; }
      .quick-summary { display:flex; align-items:center; gap:12px 24px; flex-wrap:wrap; margin:0 0 18px; padding:13px 15px; border-radius:10px; background:var(--secondary-background-color); color:var(--secondary-text-color); }
      .quick-summary strong { color:var(--primary-text-color); }
      .quick-zones { display:grid; gap:16px; }
      .quick-zone, .quick-controls, .download-card, .sidebar-card, .instance-card { background:var(--card-background-color); border:1px solid var(--divider-color); box-shadow:var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,.08)); border-radius:12px; padding:16px; }
      .quick-zone-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }
      .quick-zone-heading h3 { margin:0 0 3px; }
      .quick-zone-heading span { color:var(--secondary-text-color); font-size:12px; }
      .quick-room-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(275px, 1fr)); gap:10px; }
      .quick-room { display:grid; gap:9px; padding:13px; border:1px solid var(--divider-color); border-radius:9px; background:var(--primary-background-color); }
      .quick-room.selected { border-color:var(--primary-color); box-shadow:0 0 0 1px var(--primary-color); }
      .quick-room.holding { border-left:5px solid var(--primary-color); }
      .quick-room > label { display:flex; flex-direction:row; align-items:flex-start; gap:9px; cursor:pointer; }
      .quick-room > label input { width:20px; min-height:20px; margin:1px 0 0; }
      .quick-room > label span, .quick-room > label strong, .quick-room > label small { display:block; }
      .quick-room > label small { margin-top:4px; }
      .quick-room-state { display:flex; justify-content:space-between; align-items:center; gap:8px; padding-left:29px; color:var(--secondary-text-color); font-size:12px; }
      .occupancy-control { display:flex; justify-content:space-between; align-items:center; gap:8px; padding-left:29px; }
      .occupancy-control > div { display:flex; flex-wrap:wrap; gap:6px; }
      .hold-pill { color:var(--primary-color); font-weight:700; }
      .quick-controls { max-width:900px; margin-top:18px; }
      .quick-controls h3 { margin-bottom:4px; }
      .quick-controls > div > p { color:var(--secondary-text-color); margin:0; }
      .quick-control-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(260px,.8fr); gap:18px; margin-top:16px; }
      .quick-temperature-actions { display:flex; gap:9px; align-items:flex-end; flex-wrap:wrap; }
      .quick-temperature-actions label { min-width:160px; }
      .quick-temperature-actions button { min-width:76px; }
      .quick-durations { display:flex; gap:9px 16px; align-items:center; flex-wrap:wrap; margin:0; padding:11px; border:1px solid var(--divider-color); border-radius:8px; }
      .quick-durations legend { padding:0 5px; color:var(--secondary-text-color); }
      .quick-durations label { flex-direction:row; align-items:center; gap:4px; font-weight:400; }
      .quick-durations input { width:auto; min-height:auto; margin:0; }
      .quick-apply-row { display:flex; justify-content:space-between; align-items:center; gap:14px; margin-top:16px; padding-top:14px; border-top:1px solid var(--divider-color); }
      .quick-apply-row span { color:var(--secondary-text-color); }
      .quick-loading { min-height:38vh; }
      .away-quick-notice { margin:0 0 18px; padding:14px 16px; border-left:5px solid var(--primary-color); border-radius:8px; background:color-mix(in srgb, var(--primary-color) 10%, var(--card-background-color)); }
      .away-quick-notice p { margin:4px 0 0; color:var(--secondary-text-color); }
      .setup-help { padding:16px; margin-bottom:16px; }
      .setup-help p { margin:5px 0 0; color:var(--secondary-text-color); }
      .setup-zones { display:grid; gap:18px; }
      .setup-zone { padding:18px; }
      .setup-zone-title { border-bottom:1px solid var(--divider-color); padding-bottom:10px; margin-bottom:15px; }
      .form-grid, .equipment-grid { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:14px; }
      label { display:flex; flex-direction:column; gap:6px; font-weight:500; }
      input, select { width:100%; min-height:42px; padding:8px 10px; color:var(--primary-text-color); background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:6px; }
      select[multiple] { min-height:76px; padding:4px; }
      select[multiple] option { padding:7px; border-radius:4px; }
      small { color:var(--secondary-text-color); font-weight:400; line-height:1.35; }
      .rooms-heading { align-items:center; margin:22px 0 12px; }
      .add-room { width:auto; min-width:230px; }
      .room-editors { display:grid; gap:12px; }
      .room-editor { border:1px solid var(--divider-color); border-radius:10px; padding:15px; }
      .occupancy-config { margin:12px 0; padding:10px 12px; border:1px solid var(--divider-color); border-radius:9px; background:var(--secondary-background-color); }
      .control-boundary { margin:0 0 14px; padding:13px; border-left:4px solid var(--primary-color); border-radius:6px; background:var(--secondary-background-color); }
      .control-boundary p { margin:4px 0 0; color:var(--secondary-text-color); }
      .control-boundary ul { display:grid; gap:5px; padding:0; margin:10px 0 0; list-style:none; }
      .control-boundary li { display:flex; justify-content:space-between; gap:12px; }
      .control-boundary code { color:var(--secondary-text-color); overflow-wrap:anywhere; text-align:right; }
      .active-toggle { flex-direction:row; align-items:flex-start; margin:12px 0; }
      .active-toggle input { width:20px; min-height:20px; margin:1px 2px 0 0; }
      .active-toggle span, .active-toggle strong, .active-toggle small { display:block; }
      .canonical-target { display:flex; align-items:flex-start; gap:10px; padding:12px; margin:0 0 14px; border-radius:8px; background:var(--secondary-background-color); }
      .canonical-target ha-icon { color:var(--primary-color); margin-top:1px; }
      .canonical-target strong, .canonical-target span, .canonical-target small { display:block; }
      .canonical-target span { margin:3px 0; overflow-wrap:anywhere; }
      .room-empty { padding:18px; text-align:center; color:var(--secondary-text-color); border:1px dashed var(--divider-color); border-radius:8px; }
      .download-card { display:flex; align-items:center; justify-content:space-between; gap:18px; margin-top:18px; }
      .download-card h3 { margin-bottom:5px; }
      .download-card p { margin:0 0 5px; color:var(--secondary-text-color); }
      .download-card small { display:block; }
      .download-actions { flex:none; }
      .sidebar-card, .instance-card { margin-top:18px; }
      .sidebar-card h3, .instance-card h3 { margin-bottom:5px; }
      .sidebar-card .active-toggle { margin-bottom:0; }
      .instance-card { display:flex; align-items:center; justify-content:space-between; gap:18px; }
      .instance-card p { margin:0 0 5px; color:var(--secondary-text-color); }
      .instance-card small { display:block; }
      .instance-actions { display:flex; gap:9px; flex:none; }
      .danger-button { background:var(--error-color); color:white; }
      .away-settings { margin-top:18px; padding:18px; background:var(--card-background-color); border:1px solid var(--divider-color); box-shadow:var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,.08)); border-radius:12px; }
      .away-settings-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:14px; }
      .away-settings-heading h3 { margin-bottom:5px; }
      .away-settings-heading p { margin:0; color:var(--secondary-text-color); }
      .state.away-active { color:var(--primary-color); font-weight:700; }
      .away-source { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:10px; margin:18px 0 14px; padding:0; border:0; }
      .away-source legend { margin-bottom:9px; font-weight:700; }
      .away-source label { display:flex; flex-direction:row; align-items:flex-start; gap:9px; padding:13px; border:1px solid var(--divider-color); border-radius:8px; cursor:pointer; }
      .away-source input { flex:none; width:19px; min-height:19px; margin:1px 0 0; }
      .away-source span, .away-source strong, .away-source small { display:block; }
      .away-source small { margin-top:4px; }
      .away-fields { display:grid; grid-template-columns:repeat(auto-fit, minmax(240px,1fr)); gap:14px; }
      .away-precedence { margin-top:15px; padding:13px; border-radius:8px; background:var(--secondary-background-color); }
      .away-precedence p { margin:4px 0 0; color:var(--secondary-text-color); }
      .away-save-row { display:flex; align-items:center; justify-content:space-between; gap:14px; margin-top:15px; padding-top:14px; border-top:1px solid var(--divider-color); }
      .away-save-row > div { display:flex; gap:8px; }
      .away-save-state { color:var(--secondary-text-color); }
      .save-bar { position:sticky; bottom:12px; z-index:2; display:flex; justify-content:space-between; align-items:center; gap:14px; padding:12px 14px; margin-top:18px; background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:10px; box-shadow:0 5px 20px rgba(0,0,0,.18); }
      .save-bar > div { display:flex; gap:8px; }
      .save-state, .schedule-save-state { color:var(--secondary-text-color); }
      .message { border-radius:8px; padding:12px 14px; margin:0 0 16px; }
      .message.error { color:var(--error-color); background:color-mix(in srgb, var(--error-color) 10%, var(--card-background-color)); border:1px solid var(--error-color); }
      .message.success { color:var(--success-color, #2e7d32); background:color-mix(in srgb, var(--success-color, #2e7d32) 10%, var(--card-background-color)); border:1px solid var(--success-color, #2e7d32); }
      .center-state { min-height:55vh; display:grid; place-content:center; justify-items:center; color:var(--secondary-text-color); }
      .spinner { width:34px; height:34px; border:4px solid var(--divider-color); border-top-color:var(--primary-color); border-radius:50%; animation:spin .8s linear infinite; }
      .muted { color:var(--secondary-text-color); }
      @keyframes spin { to { transform:rotate(360deg); } }
      @media (max-width: 760px) {
        main { padding:16px 10px 100px; }
        header, .page-heading { align-items:stretch; flex-direction:column; }
        .identity img { width:46px; height:46px; }
        .entry-picker { min-width:0; }
        nav { display:grid; grid-template-columns:repeat(auto-fit, minmax(110px, 1fr)); }
        .tab { padding:12px 8px; }
        .summary-grid, .zone-grid, .form-grid, .equipment-grid { grid-template-columns:1fr; }
        .schedule-navigation { min-width:0; }
        .schedule-actions-card { grid-template-columns:1fr; }
        .schedule-actions-card > button { width:100%; }
        .quick-control-grid { grid-template-columns:1fr; }
        .download-card, .instance-card { align-items:stretch; flex-direction:column; }
        .away-banner, .away-settings-heading, .away-save-row { align-items:stretch; flex-direction:column; }
        .away-banner button { width:100%; }
        .away-source { grid-template-columns:1fr; }
        .away-save-row > div { display:grid; grid-template-columns:1fr 1fr; }
        .download-actions button, .instance-actions button { flex:1; }
        .summary-grid { grid-template-columns:repeat(3, 1fr); }
        .summary-card { display:block; padding:12px; text-align:center; }
        .summary-card ha-icon { margin-bottom:6px; }
        .summary-card strong { font-size:22px; }
        .summary-card span { font-size:12px; }
        .zone-facts div { grid-template-columns:1fr; gap:2px; }
        .control-boundary li { align-items:flex-start; flex-direction:column; }
        .control-boundary code { text-align:left; }
        .room-summary { align-items:flex-start; }
        .rooms-heading { align-items:stretch; flex-direction:column; }
        .add-room { width:100%; }
        .save-bar { align-items:stretch; flex-direction:column; bottom:8px; }
        .save-bar > div { display:grid; grid-template-columns:1fr 1fr; }
        .safety-warning { padding:13px; }
      }
      @media (max-width: 430px) {
        .summary-grid { grid-template-columns:1fr; }
        .summary-card { display:flex; text-align:left; }
        .setup-zone { padding:14px; }
        .room-editor { padding:12px; }
        .schedule-navigation { flex-direction:column; }
        .day-card { padding:12px; }
        .timeline-title { display:block; }
        .timeline-title span { display:block; text-align:left; margin-top:3px; }
        .period-labels, .period-row { grid-template-columns:minmax(72px,1fr) 76px 61px 12px 28px; gap:3px; }
        .period-row input { padding:5px 4px; }
        .quick-heading-actions, .download-actions { display:grid; grid-template-columns:1fr; }
        .quick-room-grid { grid-template-columns:1fr; }
        .quick-apply-row { align-items:stretch; flex-direction:column; }
        .quick-apply-row button { width:100%; }
      }
    </style>`;
  }
}

if (!customElements.get("zeal-panel")) {
  customElements.define("zeal-panel", ZealPanel);
}
